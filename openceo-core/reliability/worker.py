"""Reliability worker: Problems, triage, release storm, mute, reconcile.

Design v2 sections 6.3-6.6 and 13.2 (epic TASK-2026-00365, ticket
TASK-2026-00378). One asyncio task per core, supervised with the capture
supervisor; woken by record_incident through the notify hook. No periodic
queries or timers while idle: mute/claim expiry is lazy, evaluated when
the next incident for that Problem arrives or at the single bounded boot
reconciliation.

Engine Problems cross projects carrying only engine.* fields: no business
text leaves OpenCEO before the F3 publisher. The reliability component
never self-diagnoses; incidents from reliability:* tickets attach as
failed fix attempts instead of triggering new diagnosis.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import capture as _capture
from . import diagnosis_exec as _diagnosis_exec
from . import verification as _verification
from .capture import _scope_for as _capture_scope
from .config import RELIABILITY_DEFAULTS
from .fingerprint import FINGERPRINT_VERSION, problem_key
from .models import Attribution, Component, ProblemStatus

logger = logging.getLogger(__name__)

PROBLEMS_COLLECTION = "reliability_problems"
BUDGET_COLLECTION = "reliability_budget"

RECONCILE_DEFAULT_LIMIT = 500
DRAIN_PER_WAKE = 100

HANDS_OFF_STATUSES = frozenset({
    ProblemStatus.DIAGNOSING.value,
    ProblemStatus.DIAGNOSED.value,
    ProblemStatus.NEEDS_SPLIT.value,
    ProblemStatus.TICKETED.value,
    ProblemStatus.FIX_MERGED.value,
    ProblemStatus.VERIFYING.value,
    ProblemStatus.RESOLVED.value,
    ProblemStatus.REGRESSED.value,
    ProblemStatus.NO_ACTION.value,
    ProblemStatus.REJECTED.value,
    ProblemStatus.SUPERSEDED.value,
})

TRIAGE_EXCLUDED = frozenset({
    ProblemStatus.MUTED.value,
    ProblemStatus.SUPERSEDED.value,
})


def _key(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Any) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment
    return _utcnow()


def problem_id_for(scope: str, fingerprint: str,
                   version: int = FINGERPRINT_VERSION) -> str:
    """Conditional-write id of section 13.2."""
    return problem_key(str(scope), str(fingerprint), version)


def storm_problem_id(release_sha: str) -> str:
    return f"release_regression:{release_sha}"


def _release_sha(doc: dict[str, Any]) -> str:
    release = doc.get("release") or {}
    return str(release.get("sha") or "unknown")


def triage_counts(incidents: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate live incidents; fix attempts do not count as occurrences."""
    live = [doc for doc in incidents
            if not bool(doc.get("from_reliability_ticket"))]
    tickets = {str(doc.get("task_id") or doc.get("engine", {}).get("task_id")
                   or doc.get("_id"))
               for doc in live}
    projects: set[str] = set()
    for doc in live:
        engine = doc.get("engine") or {}
        project = engine.get("project_key") or doc.get("project_key")
        if project:
            projects.add(str(project))
    return {
        "occurrences": len(live),
        "distinct_tickets": len(tickets),
        "distinct_projects": len(projects),
        "critical": sum(1 for doc in live
                        if str(doc.get("severity") or "") == "critical"),
        "fix_attempts_failed": sum(
            1 for doc in incidents if bool(doc.get("from_reliability_ticket"))),
    }


_SEVERITY_RANK = {"normal": 0, "high": 1, "critical": 2}


def _max_severity(first: str, second: str) -> str:
    left, right = str(first or "normal"), str(second or "normal")
    if _SEVERITY_RANK.get(right, 0) > _SEVERITY_RANK.get(left, 0):
        return right
    return left


def triage_eligible(attribution: str | Attribution,
                    counts: dict[str, int],
                    triage_cfg: dict[str, Any] | None = None,
                    *,
                    now: datetime | None = None,
                    first_seen: Any = None,
                    window_days: int | None = None) -> tuple[bool, str]:
    """Decide whether a Problem deserves diagnosis (section 6.4)."""
    name = _key(attribution)
    cfg = triage_cfg if isinstance(triage_cfg, dict) \
        else RELIABILITY_DEFAULTS["triage"]
    if name == Attribution.ENVIRONMENT.value:
        return False, "environment_never"
    if name == Attribution.ENGINE.value:
        severity_critical = bool(counts.get("critical", False))
        if severity_critical:
            return True, "engine_first_critical"
        need = cfg.get("engine_normal", {})
        if int(counts.get("occurrences", 0)) >= int(need.get("occurrences", 3)) \
                and int(counts.get("distinct_tickets", 0)) >= int(
                    need.get("tickets", 2)):
            return True, "engine_normal_threshold"
        return False, "engine_below_threshold"
    if name == Attribution.TASK.value:
        need = cfg.get("task", {})
        moment = _utcnow() if now is None else _as_utc(now)
        start = _as_utc(first_seen) if first_seen is not None else moment
        span_days = int(window_days) if window_days is not None \
            else int(need.get("window_days", 14))
        if moment - start > timedelta(days=span_days):
            return False, "task_window_expired"
        if int(counts.get("distinct_tickets", 0)) >= int(
                need.get("tickets", 5)):
            return True, "task_threshold"
        return False, "task_below_threshold"
    need = cfg.get("project_or_unknown", {})
    if int(counts.get("occurrences", 0)) >= int(need.get("occurrences", 3)) \
            and int(counts.get("distinct_tickets", 0)) >= int(
                need.get("tickets", 2)):
        return True, "project_threshold"
    return False, "project_below_threshold"


def build_problem_doc(
    *,
    scope: str,
    fingerprint: str,
    fingerprint_version: int,
    first: dict[str, Any],
    counts: dict[str, int],
    eligible: bool,
    triage_reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """New Problem doc; engine scope carries no business text."""
    moment = _utcnow() if now is None else _as_utc(now)
    engine = first.get("engine") or {}
    doc: dict[str, Any] = {
        "_id": problem_id_for(scope, fingerprint, fingerprint_version),
        "schema_version": int(first.get("schema_version", 1)),
        "scope": scope,
        "kind": str(first.get("kind") or "unknown"),
        "component": str(first.get("component") or "unknown"),
        "attribution": str(first.get("attribution") or "unknown"),
        "attribution_rule": first.get("attribution_rule"),
        "severity_max": str(first.get("severity") or "normal"),
        "status": ProblemStatus.OPEN.value,
        "counts": {
            "occurrences": int(counts.get("occurrences", 0)),
            "suppressed": 0,
            "distinct_tickets": int(counts.get("distinct_tickets", 0)),
            "distinct_projects": int(counts.get("distinct_projects", 0)),
        },
        "first_seen": _as_utc(first.get("created_at")),
        "last_seen": _as_utc(first.get("created_at")),
        "first_release": _release_sha(first),
        "releases_seen": sorted({_release_sha(first)}),
        "triage": {
            "eligible": bool(eligible),
            "reason": triage_reason,
            "count": 1,
            "last_at": moment,
            "occurrences_at_last": int(counts.get("occurrences", 0)),
        },
        "diagnosis": {
            "count": 0, "last_at": None, "occurrences_at_last": 0,
            "claim_until": None, "result": None,
        },
        "ticket": {
            "business_ref": None, "source_id": None, "type": None,
        },
        "fix": {
            "sha": None, "merged_at": None, "deployed_release": None,
            "baseline_rate": None, "required_exposures": None,
            "exposures_since": 0, "occurrences_since": 0,
        },
        "links": {"duplicate_of": None, "grouped_under": None},
        "mute": {"reason": None, "actor": None, "until": None},
        "affected_open_tasks": [],
        "fingerprint": fingerprint,
        "fingerprint_version": int(fingerprint_version),
        "problem_key": problem_id_for(scope, fingerprint,
                                      fingerprint_version),
        "version": 1,
        "updated_at": moment,
    }
    if scope == Attribution.ENGINE.value \
            or scope == Attribution.ENVIRONMENT.value:
        doc["engine"] = {
            key: engine.get(key) for key in (
                "exc_type", "reason_code", "guard_rule", "brake",
                "invariant_id", "frames",
            ) if engine.get(key) is not None
        }
    else:
        project = engine.get("project_key") or first.get("project_key")
        if project:
            doc["project_key"] = str(project)
    return doc


def build_storm_doc(
    *,
    release_sha: str,
    members: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Critical release_regression Problem grouping a release storm."""
    moment = _utcnow() if now is None else _as_utc(now)
    fingerprint = hashlib.sha256(
        f"release_regression|{release_sha}".encode("utf-8")).hexdigest()
    return {
        "_id": storm_problem_id(release_sha),
        "schema_version": 1,
        "scope": Attribution.ENGINE.value,
        "kind": "release_regression",
        "component": str((members[0].get("component")
                          if members else "core_process")),
        "attribution": Attribution.ENGINE.value,
        "attribution_rule": "storm",
        "severity_max": "critical",
        "status": ProblemStatus.OPEN.value,
        "counts": {
            "occurrences": 0,
            "suppressed": 0,
            "distinct_tickets": 0,
            "distinct_projects": 0,
        },
        "first_seen": moment,
        "last_seen": moment,
        "first_release": release_sha,
        "releases_seen": [release_sha],
        "triage": {
            "eligible": True,
            "reason": "release_storm",
            "count": 1,
            "last_at": moment,
            "occurrences_at_last": 0,
        },
        "diagnosis": {
            "count": 0, "last_at": None, "occurrences_at_last": 0,
            "claim_until": None, "result": None,
        },
        "ticket": {
            "business_ref": None, "source_id": None, "type": None,
        },
        "fix": {
            "sha": None, "merged_at": None, "deployed_release": None,
            "baseline_rate": None, "required_exposures": None,
            "exposures_since": 0, "occurrences_since": 0,
        },
        "links": {"duplicate_of": None, "grouped_under": None},
        "mute": {"reason": None, "actor": None, "until": None},
        "affected_open_tasks": [],
        "members": [member.get("_id") for member in members
                    if member.get("_id")],
        "fingerprint": fingerprint,
        "fingerprint_version": FINGERPRINT_VERSION,
        "problem_key": storm_problem_id(release_sha),
        "version": 1,
        "updated_at": moment,
    }


def find_storm_members(
    problems: list[dict[str, Any]],
    release_sha: str,
    *,
    now: datetime | None = None,
    window_hours: int = 24,
) -> list[dict[str, Any]]:
    """Engine Problems sharing first_release inside the storm window."""
    moment = _utcnow() if now is None else _as_utc(now)
    window = timedelta(hours=max(1, int(window_hours)))
    members = []
    for problem in problems:
        if problem.get("scope") != Attribution.ENGINE.value:
            continue
        if problem.get("kind") == "release_regression":
            continue
        if str(problem.get("first_release") or "") != str(release_sha):
            continue
        if str(problem.get("status") or "") in TRIAGE_EXCLUDED:
            continue
        if bool((problem.get("links") or {}).get("grouped_under")):
            continue
        if moment - _as_utc(problem.get("first_seen")) <= window:
            members.append(problem)
    return members


class MuteDenied(ValueError):
    """The mute request is not an Owner decision with a valid token."""


def apply_mute(
    problem: dict[str, Any],
    *,
    actor: str,
    token: str | None,
    expected_token: str | None,
    reason: str,
    until: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mute a Problem; Owner-only through a channel holding its token."""
    moment = _utcnow() if now is None else _as_utc(now)
    if str(actor or "").strip().lower() != "owner":
        raise MuteDenied("mute needs the Owner as actor")
    if not expected_token:
        raise MuteDenied("no owner token configured (fail-closed)")
    if not token or not hmac.compare_digest(
            str(token), str(expected_token)):
        raise MuteDenied("invalid owner token")
    if not str(reason or "").strip():
        raise MuteDenied("mute needs a reason")
    expiry = _as_utc(until)
    if expiry <= moment:
        raise MuteDenied("mute needs a future expiry")
    updated = dict(problem)
    updated["status"] = ProblemStatus.MUTED.value
    updated["mute"] = {
        "reason": str(reason).strip(),
        "actor": "owner",
        "until": expiry,
    }
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = moment
    return updated


def mute_expired(problem: dict[str, Any],
                 now: datetime | None = None) -> bool:
    """True when a muted Problem has its first post-expiry incident."""
    if str(problem.get("status") or "") != ProblemStatus.MUTED.value:
        return False
    until = (problem.get("mute") or {}).get("until")
    if until is None:
        return False
    moment = _utcnow() if now is None else _as_utc(now)
    return moment >= _as_utc(until)


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _stores(source: Any) -> tuple[Any | None, Any | None]:
    try:
        if source is None:
            return None, None
        if hasattr(source, "insert_one") and hasattr(
                source, "count_documents"):
            return source, None
        if hasattr(source, "__getitem__"):
            try:
                return (source[_capture.COLLECTION],
                        source[PROBLEMS_COLLECTION])
            except Exception:
                return None, None
    except Exception:
        return None, None
    return None, None


async def _find_incidents(incidents: Any, problem_id: str,
                          since: datetime) -> list[dict[str, Any]]:
    filt = {"problem_key": problem_id, "created_at": {"$gte": since}}
    try:
        cursor = incidents.find(filt)
        docs = await _maybe_await(cursor.to_list(length=10000))
        return [doc for doc in docs if isinstance(doc, dict)]
    except AttributeError:
        docs = await _maybe_await(incidents.find(filt))
        return [doc for doc in docs if isinstance(doc, dict)]
    except Exception:
        logger.warning("Problem incident lookup failed", exc_info=True)
        return []


async def _supersede_stale_versions(
    problems: Any,
    scope: str,
    fingerprint: str,
    version: int,
    *,
    now: datetime | None = None,
) -> int:
    """Mark OPEN Problems of older fingerprint versions as superseded."""
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        cursor = problems.find({"scope": scope, "fingerprint": fingerprint})
        stored = await _maybe_await(cursor.to_list(length=10000))
    except AttributeError:
        stored = await _maybe_await(
            problems.find({"scope": scope, "fingerprint": fingerprint}))
    except Exception:
        return 0
    stale = 0
    for item in stored:
        if not isinstance(item, dict):
            continue
        if int(item.get("fingerprint_version", FINGERPRINT_VERSION)) == int(
                version):
            continue
        if str(item.get("status") or "") != ProblemStatus.OPEN.value:
            continue
        with _suppress():
            await _maybe_await(problems.update_one(
                {"_id": item.get("_id")},
                {"$set": {"status": ProblemStatus.SUPERSEDED.value,
                          "updated_at": moment},
                 "$inc": {"version": 1}}))
            stale += 1
    return stale


async def _mark_processed(incidents: Any, incident: dict[str, Any]) -> None:
    if incidents is None:
        return
    with _suppress():
        await _maybe_await(incidents.update_one(
            {"_id": incident.get("_id")},
            {"$set": {"processed": True}}))


async def process_incident(
    source: Any,
    incident: dict[str, Any],
    *,
    triage_cfg: dict[str, Any] | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold one incident into its Problem; never raises."""
    outcome: dict[str, Any] = {"problem": None, "created": False,
                               "diagnosable": False, "reason": "ignored"}
    try:
        return await _process(source, incident, triage_cfg=triage_cfg,
                              retention_days=retention_days, now=now)
    except Exception:
        logger.warning("Problem processing failed", exc_info=True)
        return outcome


async def _process(
    source: Any,
    incident: dict[str, Any],
    *,
    triage_cfg: dict[str, Any] | None = None,
    retention_days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _utcnow() if now is None else _as_utc(now)
    outcome: dict[str, Any] = {"problem": None, "created": False,
                               "diagnosable": False, "reason": "ignored"}
    if str(incident.get("component") or "") == Component.RELIABILITY.value:
        return outcome
    incidents, problems = _stores(source)
    try:
        attribution = Attribution(str(incident.get("attribution") or "unknown"))
    except ValueError:
        attribution = Attribution.UNKNOWN
    scope = _capture_scope(
        attribution,
        (incident.get("engine") or {}).get("project_key")
        or incident.get("project_key"))
    problem_id = problem_id_for(
        scope, str(incident.get("fingerprint") or "?"),
        int(incident.get("fingerprint_version", FINGERPRINT_VERSION)))
    incident["problem_key"] = problem_id
    if problems is None:
        outcome.update({"problem": problem_id, "reason": "no_store"})
        return outcome
    existing = await _maybe_await(
        problems.find_one({"_id": problem_id}))
    if existing is None:
        await _maybe_await(_supersede_stale_versions(
            problems, scope, str(incident.get("fingerprint") or "?"),
            int(incident.get("fingerprint_version", FINGERPRINT_VERSION)),
            now=moment))
    if not isinstance(existing, dict):
        counts = triage_counts([incident])
        eligible, reason = triage_eligible(
            str(incident.get("attribution") or "unknown"), counts,
            triage_cfg)
        doc = build_problem_doc(
            scope=scope, fingerprint=str(incident.get("fingerprint") or "?"),
            fingerprint_version=int(incident.get(
                "fingerprint_version", FINGERPRINT_VERSION)),
            first=incident, counts=counts, eligible=eligible,
            triage_reason=reason, now=moment)
        if str(doc.get("attribution") or "") == Attribution.ENVIRONMENT.value:
            doc["status"] = ProblemStatus.NO_ACTION.value
            doc["triage"] = {**doc["triage"], "eligible": False,
                             "reason": "environment_never"}
        try:
            await _maybe_await(problems.insert_one(doc))
        except Exception as exc:
            if "uplicate" not in type(exc).__name__ \
                    and getattr(exc, "code", None) != 11000:
                raise
            existing = await _maybe_await(
                problems.find_one({"_id": problem_id}))
        else:
            outcome.update({"problem": problem_id, "created": True,
                            "diagnosable": bool(doc["triage"]["eligible"]),
                            "reason": str(doc["triage"]["reason"])})
            storm_id = await _maybe_await(_check_storm(
                problems, doc, triage_cfg=triage_cfg, now=moment))
            if storm_id is not None:
                outcome.update({"diagnosable": False,
                                "reason": "grouped_under_storm"})
            await _maybe_await(_mark_processed(incidents, incident))
            return outcome
    problem = existing if isinstance(existing, dict) else None
    if problem is None:
        outcome.update({"problem": problem_id, "reason": "insert_race"})
        return outcome
    if mute_expired(problem, now=moment):
        await _maybe_await(problems.update_one(
            {"_id": problem_id},
            {"$set": {"status": ProblemStatus.OPEN.value,
                      "mute": {"reason": None, "actor": None,
                               "until": None},
                      "updated_at": moment},
             "$inc": {"version": 1}}))
        problem = dict(problem)
        problem["status"] = ProblemStatus.OPEN.value
        problem["mute"] = {"reason": None, "actor": None, "until": None}
        problem["version"] = int(problem.get("version", 1)) + 1
    if str(problem.get("status") or "") in (
            ProblemStatus.VERIFYING.value, ProblemStatus.RESOLVED.value):
        try:
            fix = problem.get("fix") if isinstance(
                problem.get("fix"), dict) else {}
            release = incident.get("release") if isinstance(
                incident.get("release"), dict) else {}
            if not bool(incident.get("from_reliability_ticket")) \
                    and str(fix.get("deployed_release") or "") \
                    and str(release.get("sha") or "") \
                    == str(fix.get("deployed_release")):
                checked = await _maybe_await(_verification.check_incident(
                    problem, incident, release_contains_fix=True,
                    problems=problems, ticket_sink=None, now=moment))
                if checked.get("status") == "regressed":
                    await _maybe_await(_mark_processed(incidents, incident))
                    outcome.update({"problem": problem_id,
                                    "reason": "regressed_same_release",
                                    "problem_update": checked.get(
                                        "problem_update")})
                    return outcome
        except Exception:
            logger.warning("verification fast path failed", exc_info=True)
    live: list[dict[str, Any]] = []
    if incidents is not None:
        live = await _find_incidents(
            incidents, problem_id,
            moment - timedelta(days=max(1, int(retention_days))))
    if not any(doc.get("_id") == incident.get("_id") for doc in live):
        live.append(incident)
    counts = triage_counts(live)
    update: dict[str, Any] = {
        "last_seen": _as_utc(incident.get("created_at")),
        "counts.occurrences": int(counts["occurrences"]),
        "counts.distinct_tickets": int(counts["distinct_tickets"]),
        "counts.distinct_projects": int(counts["distinct_projects"]),
        "severity_max": _max_severity(
            str(problem.get("severity_max") or "normal"),
            str(incident.get("severity") or "normal")),
        "updated_at": moment,
    }
    releases = sorted({*(problem.get("releases_seen") or []),
                       _release_sha(incident)})
    update["releases_seen"] = releases[:10]
    if str(problem.get("status") or "") == ProblemStatus.OPEN.value \
            and not bool((problem.get("links") or {}).get("grouped_under")):
        eligible, reason = triage_eligible(
            str(problem.get("attribution") or "unknown"), counts,
            triage_cfg, now=moment, first_seen=problem.get("first_seen"))
        update["triage.eligible"] = bool(eligible)
        update["triage.reason"] = reason
        update["triage.last_at"] = moment
        update["triage.occurrences_at_last"] = int(counts["occurrences"])
        outcome["diagnosable"] = bool(eligible)
        outcome["reason"] = reason
    elif bool((problem.get("links") or {}).get("grouped_under")):
        outcome["diagnosable"] = False
        outcome["reason"] = "grouped_under_storm"
    else:
        outcome["reason"] = f"status_{problem.get('status')}"
    if str(problem.get("status") or "") not in HANDS_OFF_STATUSES:
        await _maybe_await(problems.update_one(
            {"_id": problem_id, "version": int(problem.get("version", 1))},
            {"$set": update, "$inc": {"version": 1}}))
    await _maybe_await(_mark_processed(incidents, incident))
    outcome["problem"] = problem_id
    return outcome


@contextmanager
def _suppress() -> Any:
    try:
        yield
    except Exception:
        logger.warning("Incident processed flag failed", exc_info=True)


async def _check_storm(
    problems: Any,
    doc: dict[str, Any],
    *,
    triage_cfg: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    moment = _utcnow() if now is None else _as_utc(now)
    if doc.get("scope") != Attribution.ENGINE.value:
        return None
    cfg = triage_cfg if isinstance(triage_cfg, dict) \
        else RELIABILITY_DEFAULTS["triage"]
    storm_cfg = cfg.get("release_storm", {})
    needed = int(storm_cfg.get("new_problems", 3))
    window = int(storm_cfg.get("window_hours", 24))
    release_sha = str(doc.get("first_release") or "")
    if not release_sha or release_sha == "unknown":
        return None
    storm_id = storm_problem_id(release_sha)
    try:
        seen = await _maybe_await(problems.find_one({"_id": storm_id}))
    except Exception:
        logger.warning("Release storm lookup failed", exc_info=True)
        return None
    if isinstance(seen, dict):
        with _suppress():
            await _maybe_await(problems.update_one(
                {"_id": doc.get("_id")},
                {"$set": {"links.grouped_under": storm_id,
                          "triage.eligible": False,
                          "triage.reason": "grouped_under_storm",
                          "updated_at": moment},
                 "$inc": {"version": 1}}))
        return storm_id
    try:
        cursor = problems.find({"scope": Attribution.ENGINE.value})
        stored = await _maybe_await(cursor.to_list(length=10000))
    except AttributeError:
        stored = await _maybe_await(
            problems.find({"scope": Attribution.ENGINE.value}))
    except Exception:
        logger.warning("Release storm lookup failed", exc_info=True)
        return None
    members = find_storm_members(
        [item for item in stored if isinstance(item, dict)],
        release_sha, now=moment, window_hours=window)
    if len(members) < needed:
        return None
    storm = build_storm_doc(
        release_sha=release_sha, members=members, now=moment)
    try:
        await _maybe_await(problems.insert_one(storm))
    except Exception as exc:
        if "uplicate" not in type(exc).__name__ \
                and getattr(exc, "code", None) != 11000:
            logger.warning("Release storm insert failed", exc_info=True)
        return storm_id
    for member in members:
        with _suppress():
            await _maybe_await(problems.update_one(
                {"_id": member.get("_id")},
                {"$set": {"links.grouped_under": storm_id,
                          "triage.eligible": False,
                          "triage.reason": "grouped_under_storm",
                          "updated_at": moment},
                 "$inc": {"version": 1}}))
    return storm_id


async def ensure_problem_indexes(problems: Any) -> None:
    """Create the section 13.2 indexes; never raises (boot must not fail)."""
    try:
        if problems is None:
            return
        from pymongo import ASCENDING

        for keys, options in (
            ([("status", ASCENDING), ("component", ASCENDING)], {}),
            ([("scope", ASCENDING), ("status", ASCENDING)], {}),
            ([("first_release", ASCENDING),
              ("first_seen", ASCENDING)], {}),
        ):
            try:
                result = problems.create_index(keys, **options)
                await _maybe_await(result)
            except Exception:
                logger.warning("Problem index setup failed",
                               exc_info=True)
    except Exception:
        logger.warning("Problem index setup failed", exc_info=True)


class ReliabilityWorker:
    """Single supervised asyncio task folding incidents into Problems."""

    def __init__(
        self,
        source: Any,
        *,
        runtime_dir: str | Path | None = None,
        triage_cfg: dict[str, Any] | None = None,
        owner_token: Callable[[], str | None] | None = None,
        name: str = "reliability-worker",
    ) -> None:
        self._source = source
        self._runtime_dir = runtime_dir
        self._triage = triage_cfg
        self._owner_token = owner_token
        self._name = name
        self._event = asyncio.Event()
        self._queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._stopping = False
        self._processed = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self.restarts = 0

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def notify(self, doc: dict[str, Any]) -> None:
        """Wake hook for record_incident; never raises, never blocks."""
        try:
            if not isinstance(doc, dict):
                return
            with self._lock:
                if len(self._queue) >= 10000:
                    self._queue.pop(0)
                self._queue.append(doc)
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._event.set)
            else:
                try:
                    self._event.set()
                except RuntimeError:
                    pass
        except Exception:
            logger.warning("Worker notify failed", exc_info=True)

    def install(self) -> None:
        """Register the wake hook with capture."""
        _capture.set_notify_hook(self.notify)

    def uninstall(self) -> None:
        try:
            _capture.set_notify_hook(None)
        except Exception:
            logger.warning("Worker uninstall failed", exc_info=True)

    async def _drain(self) -> int:
        with self._lock:
            batch = self._queue[:DRAIN_PER_WAKE]
            del self._queue[:len(batch)]
            if not self._queue:
                self._event.clear()
        for doc in batch:
            await process_incident(
                self._source, doc, triage_cfg=self._triage)
            self._processed += 1
        return len(batch)

    async def run_forever(self) -> dict[str, Any]:
        """Wait for incidents; no queries or timers while idle."""
        from .supervisor import LoopSupervisor

        self._loop = asyncio.get_running_loop()
        supervisor = LoopSupervisor(
            name=self._name, component=Component.RELIABILITY)
        while not self._stopping:
            try:
                await self._event.wait()
                await self._drain()
                supervisor.success()
            except Exception as exc:
                wait = await supervisor.failure(
                    exc if isinstance(exc, BaseException)
                    else RuntimeError(str(exc)),
                    source=None)
                self.restarts += 1
                await asyncio.sleep(wait)
        return {"processed": self._processed, "restarts": self.restarts}

    def start(self) -> asyncio.Task[dict[str, Any]]:
        task = asyncio.get_running_loop().create_task(self.run_forever())
        self._task = task
        return task

    async def stop(self) -> dict[str, Any]:
        self._stopping = True
        self._event.set()
        if self._task is not None:
            with _suppress():
                await asyncio.wait_for(self._task, timeout=5.0)
        self.uninstall()
        return {"processed": self._processed, "restarts": self.restarts}

    async def mute(self, problem_id: str, *, actor: str, token: str | None,
                   reason: str, until: datetime) -> dict[str, Any]:
        """Owner-only mute through the token channel; never raises."""
        try:
            _, problems = _stores(self._source)
            if problems is None:
                return {"muted": False, "reason": "no_store"}
            problem = await _maybe_await(
                problems.find_one({"_id": problem_id}))
            if not isinstance(problem, dict):
                return {"muted": False, "reason": "unknown_problem"}
            expected = self._owner_token() if self._owner_token else None
            updated = apply_mute(
                problem, actor=actor, token=token,
                expected_token=expected, reason=reason, until=until)
            await _maybe_await(problems.update_one(
                {"_id": problem_id},
                {"$set": {"status": updated["status"],
                          "mute": updated["mute"],
                          "updated_at": updated["updated_at"]},
                 "$inc": {"version": 1}}))
            return {"muted": True, "reason": str(reason).strip(),
                    "until": updated["mute"]["until"]}
        except MuteDenied as exc:
            return {"muted": False, "reason": str(exc)}
        except Exception:
            logger.warning("Problem mute failed", exc_info=True)
            return {"muted": False, "reason": "internal"}


async def reconcile_on_boot(
    source: Any,
    *,
    runtime_dir: str | Path | None = None,
    release: dict[str, Any] | None = None,
    limit: int = RECONCILE_DEFAULT_LIMIT,
    triage_cfg: dict[str, Any] | None = None,
    now: datetime | None = None,
    diagnosis_cfg: dict[str, Any] | None = None,
    toolless_ok: bool = False,
) -> dict[str, Any]:
    """Single bounded pass: spool, unprocessed, claims, last-exit, queue."""
    summary: dict[str, Any] = {
        "spool": {"replayed": 0, "pending": 0, "failed": 0},
        "unprocessed": 0,
        "claims_expired": 0,
        "unclean_exit": None,
        "queue": _diagnosis_exec.diagnosis_queue_state(
            diagnosis_cfg, toolless_ok=toolless_ok,
            now=now),
    }
    try:
        summary["spool"] = await _capture.replay_spool(
            source, runtime_dir=runtime_dir)
    except Exception:
        logger.warning("Boot spool replay failed", exc_info=True)
    try:
        summary["unclean_exit"] = await _capture.record_unclean_exit(
            source, runtime_dir=runtime_dir, release=release)
    except Exception:
        logger.warning("Boot last-exit check failed", exc_info=True)
    try:
        summary["unprocessed"] = await _reconcile_unprocessed(
            source, limit=limit, triage_cfg=triage_cfg, now=now)
    except Exception:
        logger.warning("Boot unprocessed sweep failed", exc_info=True)
    try:
        summary["claims_expired"] = await _expire_claims(source, now=now)
    except Exception:
        logger.warning("Boot claim expiry failed", exc_info=True)
    try:
        if isinstance(diagnosis_cfg, dict) and diagnosis_cfg.get("enabled") \
                and toolless_ok and source is not None:
            budget = source[_diagnosis_exec.BUDGET_COLLECTION]
            _diagnosis_exec.install_budget_hook(
                budget, lambda: dict(diagnosis_cfg))
    except Exception:
        logger.warning("Boot budget hook failed", exc_info=True)
    return summary


async def _reconcile_unprocessed(
    source: Any,
    *,
    limit: int,
    triage_cfg: dict[str, Any] | None,
    now: datetime | None,
) -> int:
    incidents, _ = _stores(source)
    if incidents is None:
        return 0
    try:
        cursor = incidents.find({"processed": False})
        pending = await _maybe_await(cursor.to_list(
            length=max(1, int(limit))))
    except AttributeError:
        pending = await _maybe_await(
            incidents.find({"processed": False}))
    except Exception:
        return 0
    docs = [doc for doc in pending if isinstance(doc, dict)]
    for doc in docs[:max(1, int(limit))]:
        await process_incident(
            source, doc, triage_cfg=triage_cfg, now=now)
    return len(docs[:max(1, int(limit))])


async def _expire_claims(source: Any,
                         now: datetime | None = None) -> int:
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        budget = source[BUDGET_COLLECTION]
    except Exception:
        return 0
    try:
        cursor = budget.find({})
        docs = await _maybe_await(cursor.to_list(length=10000))
    except AttributeError:
        try:
            docs = await _maybe_await(budget.find({}))
        except Exception:
            return 0
    except Exception:
        return 0
    expired = 0
    _, problems = _stores(source)
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if _as_utc(doc.get("until")) > moment:
            continue
        expired += 1
        if problems is not None and doc.get("problem_id"):
            with _suppress():
                current = await _maybe_await(problems.find_one(
                    {"_id": doc.get("problem_id")}))
                patch: dict[str, Any] = {
                    "diagnosis.claim_until": None, "updated_at": moment}
                if isinstance(current, dict) and current.get("status") == \
                        ProblemStatus.DIAGNOSING.value:
                    patch["status"] = ProblemStatus.OPEN.value
                await _maybe_await(problems.update_one(
                    {"_id": doc.get("problem_id")}, {"$set": patch}))
        if doc.get("_id") is not None:
            with _suppress():
                await _maybe_await(budget.delete_one({"_id": doc.get("_id")}))
    return expired
