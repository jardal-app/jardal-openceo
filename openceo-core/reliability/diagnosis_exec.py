"""Tool-less diagnosis execution and durable budget (design v2 sections
8.1, 8.3, 8.5 and 13.4; epic TASK-2026-00365, ticket TASK-2026-00386).

One bounded diagnosis per Problem: at most two sequential processes (one
attempt plus one JSON repair), nested provider recovery disabled, stdin or
API entry, JSON exit, and no partial output accepted. The budget lives in
``reliability_budget`` so it survives restarts: one concurrent diagnosis,
3 starts per hour, 20 per day and 3 per Problem, consumed before launch
with a conditioned ``$inc``; expired claims return their Problem to open
without refunding the counters, and DeadlineWatch serves the next slot.

Executors are injected behind a two-method protocol, so tests use a
scripted fake and the shipped tree never needs a live model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol

from . import diagnosis as _diagnosis
from . import deadlines as _deadlines
from .models import ProblemStatus

logger = logging.getLogger(__name__)

PROFILE_NAME = "reliability_diagnosis"

TIMEOUT_SECONDS = 300
MAX_OUTPUT_TOKENS = 2000
MAX_PROCESSES_PER_DIAGNOSIS = 2

BUDGET_COLLECTION = "reliability_budget"

_HOUR_FMT = "%Y-%m-%dT%H"
_DAY_FMT = "%Y-%m-%d"

CERTIFIABLE_INPUTS = frozenset({"stdin", "api"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Any) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment
    return _utcnow()


def hour_id(now: datetime) -> str:
    return f"diagnosis|hour|{_as_utc(now).strftime(_HOUR_FMT)}"


def day_id(now: datetime) -> str:
    return f"diagnosis|day|{_as_utc(now).strftime(_DAY_FMT)}"


def _field(candidate: Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def certify_toolless(candidates: list[Any] | tuple[Any, ...] | None
                     ) -> tuple[bool, list[str]]:
    """One candidate must run the profile without tools.

    Each candidate states its own contract (name, toolless, input_via,
    output, max_output_tokens, timeout_s, nested_recovery). Certified when
    entry is stdin or API, exit is JSON, the timeout covers 300 s, no
    nested recovery exists, and the output cap is 2.000 tokens where the
    provider supports one. The runtime resolves provider and model.
    """
    reasons: list[str] = []
    for candidate in candidates or []:
        name = str(_field(candidate, "name", "?"))
        problems: list[str] = []
        if not _field(candidate, "toolless", False):
            problems.append("needs toolless=true")
        if str(_field(candidate, "input_via", "")) not in CERTIFIABLE_INPUTS:
            problems.append("needs input_via stdin or api")
        if str(_field(candidate, "output", "")) != "json":
            problems.append("needs output json")
        cap = _field(candidate, "max_output_tokens", None)
        if cap is not None:
            try:
                if int(cap) < MAX_OUTPUT_TOKENS:
                    problems.append(
                        f"needs max_output_tokens>={MAX_OUTPUT_TOKENS}")
            except (TypeError, ValueError):
                problems.append("bad max_output_tokens")
        try:
            if int(_field(candidate, "timeout_s", 0)) < TIMEOUT_SECONDS:
                problems.append(f"needs timeout_s>={TIMEOUT_SECONDS}")
        except (TypeError, ValueError):
            problems.append("bad timeout_s")
        if bool(_field(candidate, "nested_recovery", True)):
            problems.append("needs nested_recovery=false")
        if problems:
            reasons.append(f"{name}: {', '.join(problems)}")
            continue
        return True, []
    if not reasons:
        reasons.append("no candidates")
    return False, reasons


def default_candidates() -> list[dict[str, Any]]:
    """The §20.4 candidate: the tool-less API client when configured."""
    if not os.environ.get("CEO_LLM_BASE_URL", "").strip():
        return []
    return [{"name": "api-direct", "toolless": True, "input_via": "api",
             "output": "json", "max_output_tokens": MAX_OUTPUT_TOKENS,
             "timeout_s": TIMEOUT_SECONDS, "nested_recovery": False}]


def validate_diagnosis_runtime(diag_cfg: dict[str, Any] | None, *,
                               toolless_ok: bool) -> list[str]:
    """Fail closed when diagnosis is enabled without a certified executor."""
    if not isinstance(diag_cfg, dict) or not bool(diag_cfg.get("enabled")):
        return []
    if not toolless_ok:
        return ["diagnosis: enabled without a certified tool-less executor "
                "(fail-closed)"]
    return []


def may_diagnose(problem: dict[str, Any], *,
                 owner_requested: bool = False) -> tuple[bool, str]:
    """Initial gate plus the re-diagnosis rule (§8.1)."""
    if owner_requested:
        return True, "owner_requested"
    status = str(problem.get("status") or "")
    if status not in (ProblemStatus.OPEN.value,
                      ProblemStatus.DIAGNOSED.value,
                      ProblemStatus.REGRESSED.value):
        return False, f"status_{status or 'missing'}"
    diagnosis = problem.get("diagnosis") or {}
    try:
        count = int(diagnosis.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return True, "first_diagnosis"
    allowed, reason = rediagnosis_allowed(problem)
    if not allowed:
        return False, reason
    return True, reason


def rediagnosis_allowed(problem: dict[str, Any], *,
                        owner_requested: bool = False) -> tuple[bool, str]:
    """Again only on regressed, doubled occurrences, or Owner request."""
    if owner_requested:
        return True, "owner_requested"
    if str(problem.get("status") or "") == ProblemStatus.REGRESSED.value:
        return True, "regressed"
    counts = problem.get("counts") or {}
    diagnosis = problem.get("diagnosis") or {}
    try:
        occurrences = int(counts.get("occurrences", 0))
        at_last = int(diagnosis.get("occurrences_at_last", 0))
    except (TypeError, ValueError):
        return False, "needs_unknown_trigger"
    if at_last > 0 and occurrences >= 2 * at_last:
        return True, "occurrences_doubled"
    return False, "needs_unknown_trigger"


class ToollessExecutor(Protocol):
    """Single-shot tool-less run: one prompt in, (returncode, stdout) out."""

    nested_recovery: bool

    async def run_text(self, prompt: str, *, timeout_s: int
                       ) -> tuple[int, str]: ...

    async def abort(self) -> None: ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


class SubprocessDiagnosisExecutor:
    """One diagnosis process: bundle on stdin, JSON on stdout.

    Started in its own session so a timeout kills the whole group
    (``start_new_session`` + ``killpg``). No retries inside: the caller
    owns the single allowed JSON repair, for two processes at most.
    """

    nested_recovery = False

    def __init__(self, argv: list[str]) -> None:
        self._argv = list(argv)
        self._proc: asyncio.subprocess.Process | None = None
        self.killed = False
        self.runs = 0

    async def run_text(self, prompt: str, *, timeout_s: int
                       ) -> tuple[int, str]:
        self.runs += 1
        proc = await asyncio.create_subprocess_exec(
            *self._argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        self._proc = proc
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            await self.abort()
            raise
        except BaseException:
            await self.abort()
            raise
        code = proc.returncode if proc.returncode is not None else 1
        return code, stdout.decode("utf-8", errors="replace")

    async def abort(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self.killed = True
        try:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            logger.warning("diagnosis killpg failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (asyncio.TimeoutError, TimeoutError, Exception):
            logger.warning("diagnosis reap failed", exc_info=True)


def _repair_prompt(bundle_text: str, error: str) -> str:
    return (f"{bundle_text}\nPrevious output was not valid JSON: {error}. "
            "Reply with only the corrected JSON object.")


async def run_diagnosis(executor: ToollessExecutor, bundle: dict[str, Any],
                        manifest_refs: set[str] | None = None, *,
                        timeout_s: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run one bounded diagnosis; reject timeouts and partial output.

    Exit != 0 is rejected even when stdout parses: a partial answer is
    never a verdict. Exit == 0 with invalid JSON earns the single repair.
    """
    refs = manifest_refs if manifest_refs is not None \
        else _diagnosis.manifest_refs(bundle)
    bundle_text = _diagnosis.canonical_json(bundle)
    processes = 0
    try:
        code, stdout = await asyncio.wait_for(
            executor.run_text(bundle_text, timeout_s=timeout_s),
            timeout=timeout_s + 5)
    except (asyncio.TimeoutError, TimeoutError):
        with _suppress():
            await executor.abort()
        return {"ok": False, "error": "timeout",
                "processes_used": processes}
    processes += 1
    if code != 0:
        return {"ok": False, "error": "nonzero_exit",
                "returncode": code, "processes_used": processes}
    payload, error = _diagnosis.parse_output(stdout)
    if error is not None and processes < MAX_PROCESSES_PER_DIAGNOSIS:
        try:
            code, stdout = await asyncio.wait_for(
                executor.run_text(_repair_prompt(bundle_text, error),
                                  timeout_s=timeout_s),
                timeout=timeout_s + 5)
        except (asyncio.TimeoutError, TimeoutError):
            with _suppress():
                await executor.abort()
            return {"ok": False, "error": "timeout",
                    "processes_used": processes}
        processes += 1
        if code != 0:
            return {"ok": False, "error": "nonzero_exit",
                    "returncode": code, "processes_used": processes}
        payload, error = _diagnosis.parse_output(stdout)
    if error is not None or not isinstance(payload, dict):
        return {"ok": False, "error": error or "invalid_json",
                "processes_used": processes}
    valid, errors = _diagnosis.validate_output(payload, refs)
    return {"ok": True, "decision": _diagnosis.decide(valid, payload),
            "payload": payload if valid else None, "errors": errors,
            "processes_used": processes}


@contextmanager
def _suppress() -> Any:
    try:
        yield
    except Exception:
        logger.warning("diagnosis cleanup failed", exc_info=True)


def _live_claim_until(doc: Any) -> datetime | None:
    """Claim expiry of a diagnosing Problem, None when absent or stale."""
    if not isinstance(doc, dict):
        return None
    raw = (doc.get("diagnosis") or {}).get("claim_until")
    if not isinstance(raw, datetime):
        return None
    return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)


async def concurrent_diagnosing(problems: Any,
                                now: datetime | None = None) -> int:
    """Problems holding a live diagnosing claim."""
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        cursor = problems.find({"status": ProblemStatus.DIAGNOSING.value})
        docs = await _maybe_await(cursor.to_list(length=10000))
    except AttributeError:
        docs = await _maybe_await(
            problems.find({"status": ProblemStatus.DIAGNOSING.value}))
    except Exception:
        logger.warning("diagnosing count failed", exc_info=True)
        return 0
    live = 0
    for doc in docs:
        until = _live_claim_until(doc)
        if until is not None and until > moment:
            live += 1
    return live


async def earliest_claim_release(problems: Any,
                                 now: datetime | None = None
                                 ) -> datetime | None:
    """Nearest claim expiry freeing the concurrent slot; None when unknown."""
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        cursor = problems.find({"status": ProblemStatus.DIAGNOSING.value})
        docs = await _maybe_await(cursor.to_list(length=10000))
    except AttributeError:
        docs = await _maybe_await(
            problems.find({"status": ProblemStatus.DIAGNOSING.value}))
    except Exception:
        logger.warning("claim release probe failed", exc_info=True)
        return None
    nearest: datetime | None = None
    for doc in docs:
        until = _live_claim_until(doc)
        if until is not None and until > moment \
                and (nearest is None or until < nearest):
            nearest = until
    return nearest


async def _inc_if_below(budget: Any, bucket_id: str, limit: int, *,
                        bucket_start: datetime) -> bool:
    """Conditioned $inc: one counted start per bucket, losers wait."""
    try:
        updated = await _maybe_await(budget.update_one(
            {"_id": bucket_id, "count": {"$lt": int(limit)}},
            {"$inc": {"count": 1}}))
        if int(getattr(updated, "matched_count", 0) or 0) >= 1:
            return True
    except Exception:
        logger.warning("budget $inc failed", exc_info=True)
        return False
    try:
        seen = await _maybe_await(budget.find_one({"_id": bucket_id}))
    except Exception:
        logger.warning("budget bucket probe failed", exc_info=True)
        return False
    if seen is None:
        try:
            await _maybe_await(budget.insert_one(
                {"_id": bucket_id, "count": 1,
                 "until": bucket_start + timedelta(days=2)}))
            return True
        except Exception as exc:
            if "uplicate" not in type(exc).__name__ \
                    and getattr(exc, "code", None) != 11000:
                logger.warning("budget insert failed", exc_info=True)
            return False
    return False


def next_slot_for_counts(hour_count: int, max_per_hour: int,
                         now: datetime) -> datetime:
    """Next allowed start: now inside the hourly budget, else next hour."""
    moment = _as_utc(now)
    if int(hour_count) < int(max_per_hour):
        return moment
    return (moment.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1))


async def next_budget_slot(budget: Any, diag_cfg: dict[str, Any], *,
                           now: datetime | None = None) -> datetime | None:
    """Read-only probe for the DeadlineWatch hook; None when unknown."""
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        limit = int(diag_cfg.get("max_starts_per_hour", 3))
        seen = await _maybe_await(budget.find_one({"_id": hour_id(moment)}))
        count = int((seen or {}).get("count", 0))
        return next_slot_for_counts(count, limit, moment)
    except Exception:
        logger.warning("budget slot probe failed", exc_info=True)
        return None


def install_budget_hook(budget: Any,
                        get_cfg: Callable[[], dict[str, Any]]) -> None:
    """DeadlineWatch asks here for the next diagnosis window (§8.5).

    The hook is synchronous and store-backed, so it answers only when no
    event loop is running; inside a running loop callers await
    :func:`next_budget_slot` directly. Never raises.
    """

    def _sync_hook(deadline: datetime,
                   now: datetime | None = None) -> datetime | None:
        del deadline
        moment = _utcnow() if now is None else _as_utc(now)
        try:
            if asyncio.get_running_loop().is_running():
                return None
        except RuntimeError:
            pass
        try:
            cfg = get_cfg()
        except Exception:
            logger.warning("budget hook config failed", exc_info=True)
            return None
        try:
            return asyncio.run(next_budget_slot(budget, cfg, now=moment))
        except Exception:
            logger.warning("budget hook probe failed", exc_info=True)
            return None

    _deadlines.set_budget_deadline_hook(_sync_hook)


async def consume_budget(budget: Any, problems: Any, problem_id: str,
                         diag_cfg: dict[str, Any], *,
                         now: datetime | None = None) -> dict[str, Any]:
    """Claim one diagnosis start, consuming budget before launching.

    Order: concurrency, hourly, daily and per-Problem checks, then the
    conditioned hour/day $inc, then the Problem claim. Refusals carry the
    next slot DeadlineWatch should program.
    """
    moment = _utcnow() if now is None else _as_utc(now)
    try:
        max_concurrent = int(diag_cfg.get("max_concurrent", 1))
        max_hour = int(diag_cfg.get("max_starts_per_hour", 3))
        max_day = int(diag_cfg.get("max_starts_per_day", 20))
        max_per_problem = int(diag_cfg.get("max_per_problem", 3))
        claim_seconds = int(diag_cfg.get("claim_seconds", 600))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_diagnosis_config",
                "next_slot": None}
    live = await concurrent_diagnosing(problems, now=moment)
    if live >= max(1, max_concurrent):
        slot = await earliest_claim_release(problems, now=moment)
        if slot is None:
            slot = await next_budget_slot(budget, diag_cfg, now=moment)
        return {"ok": False, "reason": "concurrent_exhausted",
                "next_slot": slot}
    try:
        problem = await _maybe_await(
            problems.find_one({"_id": str(problem_id)}))
    except Exception:
        logger.warning("budget problem lookup failed", exc_info=True)
        problem = None
    if not isinstance(problem, dict):
        return {"ok": False, "reason": "unknown_problem", "next_slot": None}
    allowed, gate = may_diagnose(problem)
    if not allowed:
        return {"ok": False, "reason": gate, "next_slot": None}
    diagnosis = problem.get("diagnosis") or {}
    try:
        used = int(diagnosis.get("count", 0))
    except (TypeError, ValueError):
        used = 0
    if used >= max(0, max_per_problem):
        return {"ok": False, "reason": "per_problem_exhausted",
                "next_slot": None}
    hour = hour_id(moment)
    if not await _inc_if_below(
            budget, hour, max_hour,
            bucket_start=moment.replace(
                minute=0, second=0, microsecond=0)):
        return {"ok": False, "reason": "hourly_budget_exhausted",
                "next_slot": next_slot_for_counts(max_hour, max_hour,
                                                 moment)}
    day = day_id(moment)
    if not await _inc_if_below(
            budget, day, max_day,
            bucket_start=moment.replace(
                hour=0, minute=0, second=0, microsecond=0)):
        day_start = moment.replace(
            hour=0, minute=0, second=0, microsecond=0)
        return {"ok": False, "reason": "daily_budget_exhausted",
                "next_slot": day_start + timedelta(days=1)}
    claim_until = moment + timedelta(seconds=max(60, claim_seconds))
    try:
        await _maybe_await(problems.update_one(
            {"_id": str(problem_id)},
            {"$set": {"status": ProblemStatus.DIAGNOSING.value,
                      "diagnosis.claim_until": claim_until,
                      "diagnosis.last_at": moment,
                      "diagnosis.occurrences_at_last": int(
                          (problem.get("counts") or {}).get(
                              "occurrences", 0)),
                      "updated_at": moment},
             "$inc": {"diagnosis.count": 1,
                      "version": 1}}))
        await _maybe_await(budget.insert_one(
            {"_id": f"claim|{uuid.uuid4().hex}",
             "problem_id": str(problem_id), "until": claim_until}))
    except Exception:
        logger.warning("budget claim failed", exc_info=True)
        return {"ok": False, "reason": "claim_failed", "next_slot": None}
    return {"ok": True, "reason": "claimed", "next_slot": None,
            "claim_until": claim_until}


def diagnosis_queue_state(diag_cfg: dict[str, Any] | None, *,
                          toolless_ok: bool,
                          next_slot: datetime | None = None,
                          now: datetime | None = None) -> str:
    """What the worker reports: ready, waiting, or disabled."""
    if not isinstance(diag_cfg, dict) or not bool(diag_cfg.get("enabled")):
        return "diagnosis_disabled"
    if not toolless_ok:
        return "diagnosis_disabled"
    if next_slot is not None:
        moment = _utcnow() if now is None else _as_utc(now)
        if _as_utc(next_slot) > moment:
            return f"diagnosis_wait:{_as_utc(next_slot).isoformat()}"
    return "diagnosis_ready"
