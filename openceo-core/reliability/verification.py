"""Fix verification by exposure, fix cycle and regressions (design v2 11).

Epic TASK-2026-00365, ticket TASK-2026-00388. A Problem is only resolved
after enough fix-containing exposures without recurrence; a later
recurrence regresses and reopens the SAME ticket.

Cycle: ticketed -> fix_merged (ticket completed carrying the fix SHA) ->
verifying (the running release contains the SHA, via
``git merge-base --is-ancestor``) -> resolved (exposures >= required with
0 recurrences) or regressed (incident in a release containing the fix).
A regressed Problem goes back to diagnosis with budget and reopens its
ticket. Rule of three (11.3): n = clamp(ceil(3/p), 20, 500) with
p = occurrences / exposures while the Problem was open, 0.05 without
data. No resolution by time; fix.exposures_since grows with each
exposure of the component.

Evaluation triggers are explicit calls, never a poll: core boot (engine
scope, piggybacking the restart every deploy needs, so a new release is
evaluated when it starts running), the completion path (fix SHA carried
by the ticket, in params._hunter.fix_sha by convention when the fix was
integrated outside the engine) and the incident path (same-release fast
path in the worker; cross-release containment is resolved by the boot
pass, which owns the git checkout). Pure transitions plus thin async
flows behind injected stores; tests use fakes, never live git except
the synthetic-repo ancestor checks.
"""

from __future__ import annotations

import asyncio
import logging
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .config import RELIABILITY_DEFAULTS
from .models import ProblemStatus

logger = logging.getLogger(__name__)

BOOT_EVALUATE_LIMIT = 500
INCIDENTS_PER_PROBLEM_LIMIT = 200

FIX_SHA_PARAM_KEY = "_hunter"
FIX_SHA_FIELD = "fix_sha"

VERIFIABLE_STATUSES = frozenset({
    ProblemStatus.FIX_MERGED.value,
    ProblemStatus.VERIFYING.value,
    ProblemStatus.RESOLVED.value,
})


class TicketSink(Protocol):
    """Minimal surface to reopen the SAME hunter ticket; fakes in tests."""

    async def reopen_ticket(
            self, business_ref: str, note: str) -> dict[str, Any]:
        ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Any) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment
    return _utcnow()


def verification_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rule-of-three bounds; the package import already fail-closes."""
    if isinstance(cfg, dict):
        return cfg
    return RELIABILITY_DEFAULTS["verification"]


def baseline_rate_for(
        occurrences: Any,
        exposures: Any,
        default: float = 0.05) -> float:
    """p = occurrences / exposures while open; default without data."""
    try:
        seen = max(0, int(occurrences or 0))
        runs = max(0, int(exposures or 0))
    except (TypeError, ValueError):
        return float(default)
    if runs <= 0 or seen < 0:
        return float(default)
    rate = seen / runs
    if not 0 < rate <= 1:
        return float(default)
    return rate


def required_exposures_for(
        baseline_rate: Any,
        cfg: dict[str, Any] | None = None) -> tuple[int, bool]:
    """(n, capped_at_max) with n = clamp(ceil(3/p), min, max)."""
    bounds = verification_cfg(cfg)
    try:
        low = max(1, int(bounds.get("min_exposures", 20)))
        high = max(low, int(bounds.get("max_exposures", 500)))
    except (TypeError, ValueError):
        low, high = 20, 500
    try:
        rate = float(baseline_rate)
    except (TypeError, ValueError):
        rate = 0.0
    if not 0 < rate <= 1:
        rate = float(bounds.get("default_baseline_rate", 0.05)) or 0.05
    need = math.ceil(3.0 / rate)
    if need >= high:
        return high, True
    return max(low, need), False


def _fix_block(problem: dict[str, Any]) -> dict[str, Any]:
    fix = problem.get("fix")
    return dict(fix) if isinstance(fix, dict) else {}


def fix_sha_of(task: Any) -> str | None:
    """Fix SHA carried by a completed ticket (params._hunter.fix_sha)."""
    if not isinstance(task, dict):
        return None
    params = task.get("params")
    hunter = (params or {}).get(FIX_SHA_PARAM_KEY) if isinstance(
        params, dict) else None
    if not isinstance(hunter, dict):
        return None
    sha = str(hunter.get(FIX_SHA_FIELD) or "").strip()
    return sha or None


def is_hunter_task(task: Any) -> bool:
    if not isinstance(task, dict):
        return False
    if str(task.get("source_id") or "").startswith("reliability:"):
        return True
    params = task.get("params")
    hunter = (params or {}).get(FIX_SHA_PARAM_KEY) if isinstance(
        params, dict) else None
    return isinstance(hunter, dict) and bool(hunter)


def register_fix(
        problem: dict[str, Any],
        sha: str,
        *,
        occurrences: Any = 0,
        exposures: Any = 0,
        cfg: dict[str, Any] | None = None,
        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """ticketed -> fix_merged with baseline and required exposures."""
    clean = str(sha or "").strip()
    if not clean:
        raise ValueError("register_fix needs a fix SHA")
    moment = _utcnow() if now is None else _as_utc(now)
    if str(problem.get("status") or "") in VERIFIABLE_STATUSES \
            and str(_fix_block(problem).get("sha") or "") == clean:
        return problem, "already_registered"
    rate = baseline_rate_for(occurrences, exposures)
    required, capped = required_exposures_for(rate, cfg)
    updated = dict(problem)
    updated["status"] = ProblemStatus.FIX_MERGED.value
    updated["fix"] = {
        **_fix_block(problem),
        "sha": clean,
        "merged_at": moment,
        "deployed_release": None,
        "baseline_rate": rate,
        "required_exposures": required,
        "required_capped": capped,
        "exposures_since": 0,
        "occurrences_since": 0,
    }
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    return updated, "fix_merged"


def note_release(
        problem: dict[str, Any],
        release_sha: str,
        contains_fix: bool,
        *,
        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """fix_merged -> verifying once the running release holds the fix."""
    moment = _utcnow() if now is None else _as_utc(now)
    status = str(problem.get("status") or "")
    if status != ProblemStatus.FIX_MERGED.value:
        return problem, f"not_fix_merged_{status}" if status else "ignored"
    if not contains_fix:
        return problem, "waiting_for_release"
    updated = dict(problem)
    fix = _fix_block(problem)
    fix["deployed_release"] = str(release_sha or "").strip() or "unknown"
    updated["fix"] = fix
    updated["status"] = ProblemStatus.VERIFYING.value
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    return updated, "verifying"


def note_exposure(
        problem: dict[str, Any],
        *,
        release_sha: str | None = None,
        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """Count one component exposure; resolve when the quota is met."""
    moment = _utcnow() if now is None else _as_utc(now)
    if str(problem.get("status") or "") != ProblemStatus.VERIFYING.value:
        return problem, "ignored"
    fix = _fix_block(problem)
    fix["exposures_since"] = int(fix.get("exposures_since", 0) or 0) + 1
    try:
        required = int(fix.get("required_exposures") or 0)
    except (TypeError, ValueError):
        required = 0
    if required <= 0:
        required, _ = required_exposures_for(fix.get("baseline_rate"))
        fix["required_exposures"] = required
    updated = dict(problem)
    updated["fix"] = fix
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    if int(fix.get("occurrences_since", 0) or 0) == 0 \
            and int(fix["exposures_since"]) >= required:
        updated["status"] = ProblemStatus.RESOLVED.value
        fix["resolved_at"] = moment
        if release_sha:
            fix["resolved_release"] = str(release_sha)
        updated["fix"] = fix
        return updated, "resolved"
    return updated, "counted"


def note_incident(
        problem: dict[str, Any],
        release_contains_fix: bool,
        *,
        release_sha: str | None = None,
        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """A live incident in a fix-containing release regresses the Problem."""
    moment = _utcnow() if now is None else _as_utc(now)
    status = str(problem.get("status") or "")
    if status not in VERIFIABLE_STATUSES:
        return problem, "ignored"
    if not release_contains_fix:
        return problem, "not_regression"
    updated = dict(problem)
    fix = _fix_block(problem)
    fix["occurrences_since"] = int(fix.get("occurrences_since", 0) or 0) + 1
    fix["regressed_at"] = moment
    if release_sha:
        fix["regressed_release"] = str(release_sha)
    if not fix.get("deployed_release") and release_sha:
        fix["deployed_release"] = str(release_sha)
    ticket = updated.get("ticket")
    updated["ticket"] = dict(ticket) if isinstance(ticket, dict) else {}
    updated["ticket"]["reopen_pending"] = True
    updated["fix"] = fix
    updated["status"] = ProblemStatus.REGRESSED.value
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    return updated, "regressed"


def build_reopen(problem: dict[str, Any]) -> dict[str, Any] | None:
    """Same-ticket reopen payload for a regressed Problem, if addressable."""
    if str(problem.get("status") or "") != ProblemStatus.REGRESSED.value:
        return None
    ticket = problem.get("ticket") if isinstance(
        problem.get("ticket"), dict) else {}
    business_ref = str(ticket.get("business_ref") or "").strip()
    if not business_ref:
        return None
    fix = _fix_block(problem)
    key = str(problem.get("problem_key") or problem.get("_id") or "")
    return {
        "business_ref": business_ref,
        "note": (f"Regresion del Problem {key}: el fallo volvio en la "
                 f"release {fix.get('regressed_release') or fix.get('deployed_release') or '?'} "
                 f"con el fix {fix.get('sha') or '?\u2019'}; re-diagnostico con presupuesto."),
    }


def git_is_ancestor(
        repo_root: str | Path,
        fix_sha: str,
        release_sha: str,
        *,
        timeout_seconds: int = 30) -> bool:
    """True when release contains fix; False when not OR git fails."""
    try:
        if not str(fix_sha or "").strip() \
                or not str(release_sha or "").strip():
            return False
        done = subprocess.run(
            ["git", "merge-base", "--is-ancestor",
             str(fix_sha).strip(), str(release_sha).strip()],
            cwd=str(repo_root), capture_output=True, timeout=timeout_seconds)
        return done.returncode == 0
    except Exception:
        logger.warning("fix containment check failed (fail-open: no)",
                       exc_info=True)
        return False


async def _persist(store: Any, problem_id: str,
                   updated: dict[str, Any]) -> None:
    if store is None:
        return
    try:
        await _maybe_await(store.update_one(
            {"_id": problem_id}, {"$set": {
                "status": updated.get("status"),
                "fix": updated.get("fix"),
                "ticket": updated.get("ticket"),
                "updated_at": updated.get("updated_at"),
                "version": updated.get("version"),
            }}))
    except Exception:
        logger.warning("verification persist failed", exc_info=True)


async def _reopen_same_ticket(
        sink: TicketSink | None, problem: dict[str, Any]) -> str:
    """Ask the sink to reopen; record pending when it cannot. Never raises."""
    payload = build_reopen(problem)
    if payload is None:
        return "no_ticket"
    if sink is None:
        return "reopen_pending"
    try:
        await _maybe_await(sink.reopen_ticket(
            payload["business_ref"], payload["note"]))
        return "reopened"
    except Exception:
        logger.warning("same-ticket reopen failed (pending)", exc_info=True)
        return "reopen_pending"


async def record_fix_merged(
        problems: Any,
        problem_key: str,
        sha: str,
        *,
        occurrences: Any = 0,
        exposures: Any = 0,
        cfg: dict[str, Any] | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Register the fix SHA on integration; never raises."""
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        stored = await _maybe_await(
            problems.find_one({"_id": str(problem_key)}))
        if not isinstance(stored, dict):
            return {"status": "unknown", "reason": "no_problem",
                    "problem_key": str(problem_key)}
        updated, event = register_fix(
            stored, sha, occurrences=occurrences, exposures=exposures,
            cfg=cfg, now=moment)
        if event != "already_registered":
            await _persist(problems, str(stored.get("_id")), updated)
        return {"status": event, "problem_key": str(problem_key),
                "fix_sha": str(sha or "").strip() or None}
    except ValueError:
        return {"status": "refused", "reason": "no_fix_sha",
                "problem_key": str(problem_key)}
    except Exception:
        logger.warning("fix registration failed", exc_info=True)
        return {"status": "unknown", "reason": "store_failed",
                "problem_key": str(problem_key)}


async def note_ticket_completed(
        task: Any,
        business_ref: str,
        problems: Any,
        *,
        occurrences: Any = 0,
        exposures: Any = 0,
        cfg: dict[str, Any] | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Completion-path hook: a completed hunter ticket carries its fix SHA.

    Explicit call from the finalization path; never raises, never changes
    ticket flows. Fixes integrated outside the engine travel in the ticket
    by convention (params._hunter.fix_sha); without it there is nothing
    to register.
    """
    try:
        if not is_hunter_task(task):
            return {"status": "ignored", "reason": "not_hunter",
                    "ticket": str(business_ref or "")}
        sha = fix_sha_of(task)
        if sha is None:
            return {"status": "ignored", "reason": "no_fix_sha",
                    "ticket": str(business_ref or "")}
        claim = task.get("params", {}).get(FIX_SHA_PARAM_KEY, {}) \
            if isinstance(task.get("params"), dict) else {}
        problem_key = str(claim.get("problem_key") or "").strip()
        if not problem_key:
            return {"status": "ignored", "reason": "no_problem_key",
                    "ticket": str(business_ref or "")}
        return await record_fix_merged(
            problems, problem_key, sha, occurrences=occurrences,
            exposures=exposures, cfg=cfg, now=now)
    except Exception:
        logger.warning("ticket completion hook failed", exc_info=True)
        return {"status": "unknown", "reason": "hook_failed",
                "ticket": str(business_ref or "")}


async def check_incident(
        problem: dict[str, Any],
        incident: dict[str, Any],
        *,
        release_contains_fix: bool | None,
        problems: Any = None,
        ticket_sink: TicketSink | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Fold one live incident into verification; never raises.

    Unknown containment (None) never regresses: a recurrence in a release
    without the fix is not a regression. Fix-attempt incidents from
    reliability tickets never regress either; they are failed attempts,
    not fresh evidence.
    """
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        if bool(incident.get("from_reliability_ticket")):
            return {"status": "ignored", "reason": "fix_attempt",
                    "problem": str(problem.get("_id") or "")}
        if release_contains_fix is None:
            return {"status": "ignored", "reason": "containment_unknown",
                    "problem": str(problem.get("_id") or "")}
        release = incident.get("release") \
            if isinstance(incident.get("release"), dict) else {}
        updated, event = note_incident(
            problem, bool(release_contains_fix),
            release_sha=str(release.get("sha") or "") or None, now=moment)
        if event != "regressed":
            return {"status": event,
                    "problem": str(problem.get("_id") or "")}
        if problems is not None:
            await _persist(problems, str(problem.get("_id")), updated)
        reopen = await _reopen_same_ticket(ticket_sink, updated)
        return {"status": "regressed", "reopen": reopen,
                "problem": str(problem.get("_id") or ""),
                "problem_update": updated}
    except Exception:
        logger.warning("incident verification check failed", exc_info=True)
        return {"status": "unknown", "reason": "check_failed",
                "problem": str(problem.get("_id") or "")}


async def _collect(store: Any, filt: dict[str, Any],
                   limit: int) -> list[dict[str, Any]]:
    try:
        cursor = store.find(filt)
        try:
            docs = await _maybe_await(cursor.to_list(length=limit))
        except AttributeError:
            docs = await _maybe_await(cursor)
        return [doc for doc in (docs or []) if isinstance(doc, dict)]
    except Exception:
        logger.warning("verification lookup failed", exc_info=True)
        return []


def _exposure_filter(problem: dict[str, Any],
                     merged_day: str) -> dict[str, Any]:
    filt: dict[str, Any] = {
        "component": str(problem.get("component") or ""),
        "day": {"$gte": merged_day},
    }
    scope = str(problem.get("scope") or "")
    if scope.startswith("project:") and len(scope) > len("project:"):
        filt["project"] = scope.split(":", 1)[1]
    return filt


def _day_of(moment: Any) -> str:
    try:
        return _as_utc(moment).strftime("%Y-%m-%d")
    except Exception:
        return _utcnow().strftime("%Y-%m-%d")


async def _recount_exposures(
        exposures: Any, problem: dict[str, Any]) -> int:
    """Sum component exposures since the fix merged; 0 without a store."""
    if exposures is None:
        return int(_fix_block(problem).get("exposures_since", 0) or 0)
    merged_day = _day_of(_fix_block(problem).get("merged_at"))
    docs = await _collect(exposures, _exposure_filter(problem, merged_day),
                          10000)
    total = 0
    for doc in docs:
        try:
            total += max(0, int(doc.get("count", 0)))
        except (TypeError, ValueError):
            continue
    return total


async def _scan_regressions(
        incidents: Any,
        problem: dict[str, Any],
        contains: Callable[[str, str], bool],
        *,
        now: datetime) -> dict[str, Any] | None:
    """Newest live incident in a fix-containing release, if any."""
    fix = _fix_block(problem)
    sha = str(fix.get("sha") or "")
    if not sha or incidents is None:
        return None
    try:
        since = _as_utc(fix.get("merged_at"))
    except Exception:
        since = now
    docs = await _collect(
        incidents,
        {"problem_key": str(problem.get("problem_key")
                            or problem.get("_id") or ""),
         "created_at": {"$gte": since}},
        INCIDENTS_PER_PROBLEM_LIMIT)
    live = [doc for doc in docs
            if not bool(doc.get("from_reliability_ticket"))]
    live.sort(key=lambda doc: str(doc.get("created_at") or ""),
              reverse=True)
    for doc in live:
        release = doc.get("release") \
            if isinstance(doc.get("release"), dict) else {}
        release_sha = str(release.get("sha") or "")
        if not release_sha or release_sha == "unknown":
            continue
        try:
            hit = bool(contains(sha, release_sha))
        except Exception:
            logger.warning("regression containment failed", exc_info=True)
            continue
        if hit:
            return doc
    return None


async def evaluate_release(
        problems: Any,
        exposures: Any,
        release_sha: str,
        *,
        incidents: Any = None,
        contains: Callable[[str, str], bool] | None = None,
        repo_root: str | Path | None = None,
        scope: str | None = None,
        limit: int = BOOT_EVALUATE_LIMIT,
        ticket_sink: TicketSink | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Evaluate every fix_merged/verifying Problem against a release.

    The explicit entry point for core boot (engine scope: every deploy
    restarts the core, so a new release is evaluated when it starts
    running) and for the runner on a new release. Bounded single pass,
    never raises, never a periodic sweep.
    """
    moment = _utcnow() if now is None else _as_utc(now)
    outcome: dict[str, Any] = {
        "release": str(release_sha or ""), "evaluated": 0,
        "resolved": 0, "regressed": 0, "waiting": 0,
    }
    try:
        if problems is None or not str(release_sha or "").strip():
            return outcome
        filt: dict[str, Any] = {"status": {"$in": sorted(VERIFIABLE_STATUSES)}}
        if scope:
            filt["scope"] = str(scope)

        def _contains(fix_sha: str, candidate: str) -> bool:
            if contains is not None:
                return bool(contains(fix_sha, candidate))
            if repo_root is None:
                return False
            return git_is_ancestor(repo_root, fix_sha, candidate)

        stored = await _collect(problems, filt, limit)
        for problem in stored:
            outcome["evaluated"] += 1
            try:
                result = await _evaluate_one(
                    problems, exposures, incidents, problem,
                    str(release_sha), _contains,
                    ticket_sink=ticket_sink, now=moment)
                outcome[result] = int(outcome.get(result, 0)) + 1
            except Exception:
                logger.warning("problem evaluation failed", exc_info=True)
        return outcome
    except Exception:
        logger.warning("release evaluation failed", exc_info=True)
        return outcome


async def _evaluate_one(
        problems: Any,
        exposures: Any,
        incidents: Any,
        problem: dict[str, Any],
        release_sha: str,
        contains: Callable[[str, str], bool],
        *,
        ticket_sink: TicketSink | None,
        now: datetime) -> str:
    fix = _fix_block(problem)
    sha = str(fix.get("sha") or "")
    status = str(problem.get("status") or "")
    if status == ProblemStatus.FIX_MERGED.value:
        if not sha:
            return "waiting"
        if not contains(sha, release_sha):
            return "waiting"
        updated, _ = note_release(problem, release_sha, True, now=now)
        await _persist(problems, str(problem.get("_id")), updated)
        problem = updated
        status = str(problem.get("status") or "")
    if status != ProblemStatus.VERIFYING.value:
        if status == ProblemStatus.RESOLVED.value and incidents is not None \
                and sha:
            hit = await _scan_regressions(
                incidents, problem, contains, now=now)
            if hit is not None:
                updated, _ = note_incident(
                    problem, True,
                    release_sha=str((hit.get("release") or {}).get("sha") or ""),
                    now=now)
                await _persist(problems, str(problem.get("_id")), updated)
                await _reopen_same_ticket(ticket_sink, updated)
                return "regressed"
        return "waiting"
    if sha and incidents is not None:
        hit = await _scan_regressions(incidents, problem, contains, now=now)
        if hit is not None:
            updated, _ = note_incident(
                problem, True,
                release_sha=str((hit.get("release") or {}).get("sha") or ""),
                now=now)
            await _persist(problems, str(problem.get("_id")), updated)
            await _reopen_same_ticket(ticket_sink, updated)
            return "regressed"
    total = await _recount_exposures(exposures, problem)
    current = dict(problem)
    fix = _fix_block(current)
    fix["exposures_since"] = total
    current["fix"] = fix
    updated, event = note_exposure_counted(current, total, now=now)
    await _persist(problems, str(problem.get("_id")), updated)
    if event == "resolved":
        return "resolved"
    if str(updated.get("status") or "") == ProblemStatus.VERIFYING.value:
        return "waiting"
    return "waiting"


def note_exposure_counted(
        problem: dict[str, Any],
        exposures_since: Any,
        *,
        release_sha: str | None = None,
        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """Set the recounted exposure total, resolving when the quota is met."""
    moment = _utcnow() if now is None else _as_utc(now)
    if str(problem.get("status") or "") != ProblemStatus.VERIFYING.value:
        return problem, "ignored"
    try:
        total = max(0, int(exposures_since))
    except (TypeError, ValueError):
        return problem, "ignored"
    updated = dict(problem)
    fix = _fix_block(updated)
    fix["exposures_since"] = total
    try:
        required = int(fix.get("required_exposures") or 0)
    except (TypeError, ValueError):
        required = 0
    if required <= 0:
        required, _ = required_exposures_for(fix.get("baseline_rate"))
        fix["required_exposures"] = required
    updated["fix"] = fix
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    if int(fix.get("occurrences_since", 0) or 0) == 0 \
            and total >= required:
        updated["status"] = ProblemStatus.RESOLVED.value
        fix["resolved_at"] = moment
        if release_sha:
            fix["resolved_release"] = str(release_sha)
        updated["fix"] = fix
        return updated, "resolved"
    return updated, "counted"
