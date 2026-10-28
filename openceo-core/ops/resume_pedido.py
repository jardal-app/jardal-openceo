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

No network, no CLI, no LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from objectives.pedido import is_pedido

#: Planner bookkeeping cleared on resume; anything else in ``params``
#: survives untouched.
_PLANNER_RESUME_KEYS = ("planner_attempts", "planner_last_invalid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def resume_failed_pedido(
    repo: Any,
    task_id: str,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    reason: str = "TASK-2026-00349 resume after F2+F3 fixes",
) -> dict[str, Any]:
    """Reset one FAILED pedido for replanning and advance the CEO trigger.

    Returns a report dict with ``ok`` plus what changed. Refusals
    (unknown ticket, not a pedido, not failed, trigger missing) return
    ``ok: False`` with a ``reason`` and write nothing. ``dry_run``
    computes the report without writing anything.
    """
    task = await repo.get_task(task_id)
    if not isinstance(task, Mapping):
        return {"ok": False, "task_id": task_id, "reason": "task_not_found"}
    if not is_pedido(task):
        return {"ok": False, "task_id": task_id, "reason": "not_a_pedido"}
    if str(task.get("status") or "") != "failed":
        return {
            "ok": False,
            "task_id": task_id,
            "reason": "not_failed",
            "status": str(task.get("status") or ""),
        }
    trigger = await repo.get_ceo_trigger()
    if not isinstance(trigger, Mapping):
        return {"ok": False, "task_id": task_id, "reason": "trigger_missing"}

    moment = now or _now()
    params = task.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    cleared = sorted(key for key in _PLANNER_RESUME_KEYS if key in params)
    for key in cleared:
        params.pop(key, None)
    patch = {
        "params": params,
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
        return report
    await repo.patch_task(task_id, patch)
    await repo.save_ceo_trigger(trigger_doc)
    reread = await repo.get_task(task_id)
    report["verified_attempts"] = (reread.get("params") or {}).get(
        "planner_attempts") if isinstance(reread, Mapping) else None
    return report
