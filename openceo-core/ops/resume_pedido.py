"""Resume a terminally failed pedido for one more planning round.

F4 of epic TASK-2026-00346 (ticket TASK-2026-00349): after the F2
planner-enum fix and the F3 CEO-wake fix, the failed pedido
TASK-2026-00344 needs its ``planner_attempts`` counter reset and its
error/waiting markers cleared so the next CEO review can replan it
instead of seeing a spent 3/3 ticket.

The helper below is deterministic and dependency-free (stdlib plus the
``objectives.pedido`` detector): it drives only the repository
interface the live ``MongoRepository`` already exposes
(``get_task``/``patch_task``/``get_ceo_trigger``/``save_ceo_trigger``)
and never touches Jardal ERP business state. The live run against the
real service stays a closing-session step; this module plus its tests
make that step a reviewed, rehearsed operation.

D5 (ticket #580): after persisting, the writer pokes the core wake
route best-effort so the kernel re-reads the trigger without waiting
for the previous due. Mongo stays the truth, the wake is a hint: a
failed poke loses nothing and the report carries ``woken: False``.

No CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from objectives.pedido import is_pedido
from state import TaskStatus

logger = logging.getLogger(__name__)

#: Planner bookkeeping cleared on resume; anything else in ``params``
#: survives untouched.
_PLANNER_RESUME_KEYS = ("planner_attempts", "planner_last_invalid")

#: Agenda and route the out-of-process wake pokes (D5).
CEO_WAKE_AGENDA = "ceo"
CEO_WAKE_PATH = "/api/ceo/wake"
CEO_WAKE_TIMEOUT_SECONDS = 2.0

#: Environment carrying the core base URL and the exclusive machine
#: Bearer for the wake route. Both must be set or the poke is skipped
#: (``woken: False``); the secret never reaches logs or reports.
CEO_WAKE_BASE_URL_ENV = "OPENCEO_CORE_URL"
CEO_WAKE_BEARER_ENV = "OPENCEO_CEO_WAKE_BEARER"

#: A wake poke: sync or async, True only when the kernel was poked.
Waker = Callable[[], bool | Awaitable[bool]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request_ceo_wake(
    base_url: str | None = None,
    bearer: str | None = None,
    timeout: float = CEO_WAKE_TIMEOUT_SECONDS,
) -> bool:
    """POST the CEO wake to the core. Best-effort hint, never raises.

    Returns True only on a 2xx from the wake route. Missing config,
    network errors, timeouts and non-2xx all return False: Mongo stays
    the truth and the kernel re-reads no later than the previous due.
    The Bearer travels only in the Authorization header, never in logs.
    """
    base = base_url if base_url is not None else os.environ.get(
        CEO_WAKE_BASE_URL_ENV, "")
    token = bearer if bearer is not None else os.environ.get(
        CEO_WAKE_BEARER_ENV, "")
    base = str(base or "").strip().rstrip("/")
    token = str(token or "").strip()
    if not base or not token:
        return False
    body = json.dumps({"agenda": CEO_WAKE_AGENDA}).encode("utf-8")
    request = urllib.request.Request(
        base + CEO_WAKE_PATH,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            return 200 <= status < 300
    except Exception as exc:  # noqa: BLE001 - best-effort hint: every failure maps to woken=False
        logger.debug("CEO wake failed (best-effort): %s", type(exc).__name__)
        return False


async def _attempt_wake(waker: Waker | None) -> bool:
    """Run the wake hint, sync or async, mapping every failure to False."""
    try:
        if waker is None:
            return await asyncio.to_thread(request_ceo_wake)
        result = waker()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception as exc:  # noqa: BLE001 - best-effort hint: every failure maps to woken=False
        logger.debug("CEO wake failed (best-effort): %s", type(exc).__name__)
        return False


async def resume_failed_pedido(
    repo: Any,
    task_id: str,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    reason: str = "TASK-2026-00349 resume after F2+F3 fixes",
    waker: Waker | None = None,
) -> dict[str, Any]:
    """Reset one FAILED pedido for replanning and advance the CEO trigger.

    The ticket goes back to ``queued`` so the dispatcher admits it;
    without that it would stall in ``failed`` forever. Returns a
    report dict with ``ok`` plus what changed. Refusals (unknown
    ticket, not a pedido, not failed, trigger missing) return
    ``ok: False`` with a ``reason`` and write nothing. ``dry_run``
    computes the report without writing anything.

    D5: on success the trigger persists first and the wake pokes after,
    so a dead core or a failed poke loses nothing and the report
    carries ``woken: False``. ``waker`` overrides the default route
    poke (used by tests); ``dry_run`` and refusals never poke.
    """
    task = await repo.get_task(task_id)
    if not isinstance(task, Mapping):
        return {"ok": False, "task_id": task_id, "reason": "task_not_found",
                "woken": False}
    if not is_pedido(task):
        return {"ok": False, "task_id": task_id, "reason": "not_a_pedido",
                "woken": False}
    if str(task.get("status") or "") != "failed":
        return {
            "ok": False,
            "task_id": task_id,
            "reason": "not_failed",
            "status": str(task.get("status") or ""),
            "woken": False,
        }
    trigger = await repo.get_ceo_trigger()
    if not isinstance(trigger, Mapping):
        return {"ok": False, "task_id": task_id, "reason": "trigger_missing",
                "woken": False}

    moment = now or _now()
    params = task.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    cleared = sorted(key for key in _PLANNER_RESUME_KEYS if key in params)
    for key in cleared:
        params.pop(key, None)
    patch = {
        "params": params,
        "status": TaskStatus.QUEUED.value,
        "error": None,
        "waiting_reason": None,
        "resume_at": None,
        "completed_at": None,
    }
    trigger_doc = {**dict(trigger), "next_scheduled_at": moment}
    report: dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "dry_run": dry_run,
        "cleared_params": cleared,
        "trigger_advanced_to": moment.isoformat(),
        "reason": reason,
    }
    if dry_run:
        report["woken"] = False
        return report
    await repo.patch_task(task_id, patch)
    await repo.save_ceo_trigger(trigger_doc)
    report["woken"] = await _attempt_wake(waker)
    reread = await repo.get_task(task_id)
    if isinstance(reread, Mapping):
        report["verified_attempts"] = (reread.get("params") or {}).get(
            "planner_attempts")
        report["verified_status"] = reread.get("status")
    else:
        report["verified_attempts"] = None
        report["verified_status"] = None
    return report
