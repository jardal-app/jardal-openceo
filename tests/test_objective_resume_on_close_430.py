"""capability_gap parks resume on new child evidence (TASK-2026-00430).

Contract fixed here:

- A child close under a ``waiting``/``capability_gap`` objective resumes
  it (active, park cleared) and replans: the completion is terminal
  evidence the park never considered. Every other park
  (blocked/data_gaps/erp_error/stalled/config) keeps its own trigger
  and stays parked; terminal objectives never replan.
- A closing child without ``objective_id`` is attributed through its
  ERP parent: when ``parent_task`` names a ``converted`` group ticket
  carrying ``objective_id``, the close adopts it (one patch). Without
  a group link the close stays unattributed, as before.

Real engine + real funnel trigger, dict-backed stores with real
update semantics, scripted planner; no models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402
from settings import Settings  # noqa: E402
from task_service import TaskService  # noqa: E402


def _env(tasks: dict[str, dict[str, Any]]):
    objectives = engine_mod.InMemoryObjectiveStore()
    return engine_mod.ObjectiveEnv(
        objectives=objectives,
        tasks=engine_mod.InMemoryTaskLister(dict(tasks)),
        create_ticket=AsyncMock(return_value={"task_id": "t"}),
        patch_task=AsyncMock(),
        executor=MagicMock(),
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=Path.cwd(),
    ), objectives


class ResumeOnCloseTests(unittest.IsolatedAsyncioTestCase):
    """New child evidence resumes a capability_gap park, nothing else."""

    async def test_capability_gap_close_resumes_and_replans(self) -> None:
        env, objectives = _env({
            "kid-1": {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-430A"},
        })
        await objectives.create_objective({
            "objective_id": "OBJ-430A", "project": "p",
            "status": "waiting",
            "waiting_reason": "capability_gap: agent return unresolved",
            "anchor": {"goal": "g", "acceptance": []}})
        ticket = {"task_id": "kid-1", "status": "completed",
                  "objective_id": "OBJ-430A"}
        close = engine_mod.TicketCloseContext(
            task_id="kid-1", title="w", tier="tier_a", sha="s")

        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True,
                                                      "planned": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(env, ticket, close)

        planned.assert_awaited_once_with(env, "OBJ-430A")
        self.assertEqual(result, {"ok": True, "planned": True})
        resumed = await objectives.get_objective("OBJ-430A")
        assert resumed is not None
        self.assertEqual(resumed["status"], "active")
        self.assertIsNone(resumed["waiting_reason"])

    async def test_other_parks_stay_parked(self) -> None:
        for index, reason in enumerate([
                "blocked: owner question open",
                "data_gaps: evidence missing",
                "erp_error: group conversion failed",
                "stalled: planning without progress",
                "config: planner registry empty",
        ]):
            with self.subTest(reason=reason):
                obj_id = f"OBJ-430B{index}"
                env, objectives = _env({
                    "kid-1": {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id},
                })
                await objectives.create_objective({
                    "objective_id": obj_id, "project": "p",
                    "status": "waiting", "waiting_reason": reason,
                    "anchor": {"goal": "g", "acceptance": []}})
                with patch.object(engine_mod, "plan_objective",
                                  new=AsyncMock(return_value={"ok": True})
                                  ) as planned:
                    result = await engine_mod.on_ticket_closed(
                        env, {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id}, None)
                planned.assert_not_awaited()
                self.assertEqual(result["action"], "objective-not-active")
                parked = await objectives.get_objective(obj_id)
                assert parked is not None
                self.assertEqual(parked["status"], "waiting")

    async def test_terminal_objective_ignores_close(self) -> None:
        env, objectives = _env({
            "kid-1": {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-430C"},
        })
        await objectives.create_objective({
            "objective_id": "OBJ-430C", "project": "p", "status": "closed",
            "anchor": {"goal": "g", "acceptance": []}})
        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(
                env, {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-430C"}, None)
        planned.assert_not_awaited()
        self.assertEqual(result["action"], "objective-not-active")


class _AttrRepo:
    """Minimal repo: business-name lookup + patch recording."""

    def __init__(self, groups: dict[str, dict[str, Any]]) -> None:
        self._groups = groups
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def find_task_by_business_name(
            self, name: str) -> dict[str, Any] | None:
        row = self._groups.get(name)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        self.patches.append((task_id, dict(updates)))
        return {"task_id": task_id, **updates}


def _service(repo: Any) -> TaskService:
    return TaskService(
        settings=Settings(),
        repo=repo,
        graph=MagicMock(),
        realtime_hub=MagicMock(),
        config_loader=MagicMock(),
    )


def _child(**over: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "task_id": "kid-9", "status": "completed", "project": "p",
        "title": "manual discovery", "params": {"_mirrors": {"business": {
            "name": "TASK-2026-00999",
            "parent_task": "TASK-2026-00418"}}},
    }
    ticket.update(over)
    return ticket


class AttributeGroupChildTests(unittest.IsolatedAsyncioTestCase):
    """Manual ERP children adopt the group objective on close."""

    async def test_backfills_objective_id_from_converted_group(self) -> None:
        repo = _AttrRepo({"TASK-2026-00418": {
            "task_id": "group-1", "status": "converted",
            "objective_id": "OBJ-430D"}})
        service = _service(repo)

        attributed = await service._attribute_objective_child(_child())

        self.assertEqual(attributed["objective_id"], "OBJ-430D")
        self.assertEqual(repo.patches,
                         [("kid-9", {"objective_id": "OBJ-430D"})])

    async def test_without_group_link_unchanged(self) -> None:
        group = {"task_id": "group-1", "status": "converted",
                 "objective_id": "OBJ-430E"}
        cases = {
            "already attributed": (
                _child(objective_id="OBJ-OLD"), {"TASK-2026-00418": group},
                "OBJ-OLD", 0),
            "no parent": (
                _child(params={"_mirrors": {"business": {
                    "name": "TASK-2026-00999"}}}),
                {"TASK-2026-00418": group}, None, 0),
            "group unknown": (
                _child(), {"TASK-2026-00000": group}, None, 0),
            "group without objective": (
                _child(), {"TASK-2026-00418": {"task_id": "group-1",
                                               "status": "converted"}},
                None, 0),
            "group still executing": (
                _child(), {"TASK-2026-00418": dict(
                    group, status="in_progress")}, None, 0),
        }
        for name, (ticket, groups, expected, patches) in cases.items():
            with self.subTest(name=name):
                repo = _AttrRepo(groups)
                attributed = await _service(repo
                                            )._attribute_objective_child(ticket)
                self.assertEqual(attributed.get("objective_id"), expected)
                self.assertEqual(len(repo.patches), patches)

    async def test_trigger_close_passes_attributed_ticket(self) -> None:
        repo = _AttrRepo({"TASK-2026-00418": {
            "task_id": "group-1", "status": "converted",
            "objective_id": "OBJ-430F"}})
        service = _service(repo)
        service._objective_service = MagicMock()  # noqa: SLF001
        service._objective_service.env_for.return_value = MagicMock()

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock(return_value={"ok": True})) as notified:
            await service._trigger_objective_close(_child(), {})

        notified.assert_awaited_once()
        args, _ = notified.call_args
        self.assertEqual(args[1]["objective_id"], "OBJ-430F")


if __name__ == "__main__":
    unittest.main()
