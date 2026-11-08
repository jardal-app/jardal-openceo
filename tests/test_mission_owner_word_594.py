"""The Owner's word reaches whoever judges a mission (TASK-2026-00594).

Live case (MIS-47F6240D): the Owner authorized budget, scheduler and the
demo as notes on the mission ticket; the CEO never read them, asked
nobody and left the mission idle. Information for the judge, one
definition of "nothing in flight" and a generic liveness pass; no rule
per path. Fakes only, no live models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo.review import run_review  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives.missions import (  # noqa: E402
    MISSION_NOTES_HEADING,
    InMemoryMissionStore,
    mission_activity_key,
    mission_idle,
    mission_idle_reason,
    served_mission_reason,
    served_missions,
)
from objectives.service import ObjectiveService  # noqa: E402
from test_ceo_review import _RecordedCEO, _make_env, _scratch_repo  # noqa: E402
from test_owner_channel_206 import _make_service, _SvcRepo  # noqa: E402

MISSION = "MIS-47F6240D"
GROUP = "TASK-2026-00544"
ORDER = ("Orden del Owner (2026-11-06): autorizaciones: demo B con feed; "
         "RESEARCH_SCHEDULER_ENABLED=true; paid_budget_usd=2.00.")
CUT = "Recorte Owner (2026-11-06): la lectura X sale de esta misión."
NOTICE = f"Mission {MISSION} created (top_down) in ops. Goal: research"


def _thread(comments: list[dict[str, Any]]) -> Any:
    """Business thread reader returning comments newest first."""
    reads: list[tuple[str, int]] = []

    async def thread_of(name: str, *, limit: int = 20) -> dict[str, Any]:
        reads.append((name, limit))
        return {"comments": list(comments)}

    thread_of.reads = reads  # type: ignore[attr-defined]
    return thread_of


def _comments() -> list[dict[str, Any]]:
    return [
        {"content": CUT, "createdAt": "2026-11-06T23:17:32.970Z"},
        {"content": ORDER, "createdAt": "2026-11-06T15:52:10.012Z"},
        {"content": NOTICE, "createdAt": "2026-11-06T15:48:13.297Z"},
    ]


def _mission(status: str = "closed") -> dict[str, Any]:
    return {
        "mission_id": MISSION, "project": "ops", "status": "active",
        "goal": "research alive", "acceptance": ["deep live"],
        "acceptance_status": "set", "erp_group_ref": GROUP,
        "objectives": [{"objective_id": "OBJ-F3AAEE49", "status": status}],
    }


class OwnerNotesTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_keep_the_owner_and_drop_engine_notices(self) -> None:
        reader = _thread(_comments())
        rows = await engine_mod.ticket_note_rows(reader, GROUP)
        self.assertEqual([text for _when, text in rows], [ORDER, CUT])
        self.assertEqual(reader.reads, [(GROUP, engine_mod.OBJECTIVE_NOTES_SCAN)])

    async def test_unreadable_thread_means_no_notes(self) -> None:
        async def broken(name: str, *, limit: int = 20) -> Any:
            raise RuntimeError("erp down")

        self.assertEqual(await engine_mod.ticket_note_rows(broken, GROUP), [])

    async def test_planner_mission_section_carries_the_notes(self) -> None:
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission())
        env = SimpleNamespace(
            missions=missions,
            erp=SimpleNamespace(thread_of=_thread(_comments())))
        section = await engine_mod._objective_mission_section(
            env, {"mission_id": MISSION})
        self.assertIn(MISSION_NOTES_HEADING, section)
        self.assertIn("paid_budget_usd=2.00", section)
        self.assertNotIn("created (top_down)", section)

    async def test_pedido_guide_carries_the_notes(self) -> None:
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission())
        env = SimpleNamespace(
            missions=missions,
            erp=SimpleNamespace(thread_of=_thread(_comments())))
        guide = await engine_mod._mission_guide_section(
            env, {"params": {"mission_id": MISSION}})
        self.assertIn("RESEARCH_SCHEDULER_ENABLED=true", guide)

    async def test_ceo_judges_the_mission_with_its_notes(self) -> None:
        repo = _scratch_repo(self)
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission(status="active"))
        executor = _RecordedCEO(
            repo, [{"action": "keep_going", "summary": "objective runs"}])
        env, _calls = _make_env(self, repo, executor=executor,
                                missions=missions,
                                thread_of=_thread(_comments()))
        await env.objectives.create_objective({
            "objective_id": "OBJ-F3AAEE49", "project": "ops",
            "status": "active", "mission_id": MISSION})
        await run_review(env, kind="scheduled", reasons=[])
        prompt = executor.prompts[0]
        self.assertIn(MISSION_NOTES_HEADING, prompt)
        self.assertIn("paid_budget_usd=2.00", prompt)
        self.assertNotIn("created (top_down)", prompt)


class InFlightTests(unittest.TestCase):
    def test_an_active_objective_is_work_in_flight(self) -> None:
        self.assertFalse(mission_idle(["closed", "active"], []))

    def test_an_open_ticket_naming_the_mission_is_in_flight(self) -> None:
        for status in ("queued", "in_progress", "waiting"):
            self.assertFalse(mission_idle(["closed"], [status]), status)

    def test_ended_objectives_and_dead_tickets_leave_it_idle(self) -> None:
        self.assertTrue(mission_idle(
            ["closed", "cancelled"],
            ["completed", "converted", "failed", "interrupted"]))
        self.assertTrue(mission_idle([], []))

    def test_an_unknown_objective_counts_as_in_flight(self) -> None:
        self.assertFalse(mission_idle(["unknown"], []))

    def test_the_activity_key_moves_with_ticket_and_note(self) -> None:
        mission = _mission()
        base = mission_activity_key(mission, latest_ticket="t1",
                                    newest_note="2026-11-06T15:52")
        self.assertNotEqual(base, mission_activity_key(
            mission, latest_ticket="t2", newest_note="2026-11-06T15:52"))
        self.assertNotEqual(base, mission_activity_key(
            mission, latest_ticket="t1", newest_note="2026-11-08T19:00"))

    def test_the_liveness_reason_is_a_judging_reason(self) -> None:
        self.assertEqual(served_missions([mission_idle_reason(MISSION)]),
                         [MISSION])


class JudgedIdleMissionTests(unittest.IsolatedAsyncioTestCase):
    async def _env(self, payloads: list[dict[str, Any]], *,
                   objective_status: str = "closed") -> Any:
        repo = _scratch_repo(self)
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission(status=objective_status))
        holder: dict[str, Any] = {}

        async def mission_tickets(mission_id: str) -> list[dict[str, Any]]:
            created = holder["calls"]["created_tickets"]
            return [{"task_id": f"task-{index}", "status": "queued"}
                    for index, payload in enumerate(created, 1)
                    if (payload.get("params") or {}).get("mission_id")
                    == mission_id][::-1]

        executor = _RecordedCEO(repo, payloads)
        env, calls = _make_env(self, repo, executor=executor,
                               missions=missions,
                               thread_of=_thread(_comments()),
                               mission_tickets=mission_tickets)
        holder["calls"] = calls
        await env.objectives.create_objective({
            "objective_id": "OBJ-F3AAEE49", "project": "ops",
            "status": objective_status, "mission_id": MISSION,
            "roadmap_remaining": ["Tramo deep; decisión Owner"]})
        return env, calls, executor, missions

    async def test_keep_going_is_rejected_and_the_ceo_continues_it(
            self) -> None:
        pedido = {
            "action": "crear_pedido", "summary": "next tramo per notes",
            "pedido": {
                "project": "ops", "title": f"{MISSION} tramo deep live",
                "description": "Deep live under the Owner's 2.00 budget",
                "acceptance": ["deep live verified"],
                "justificacion": "Owner authorized it on 2026-11-06",
                "mission_id": MISSION,
            },
        }
        env, calls, executor, _missions = await self._env(
            [{"action": "keep_going", "summary": "criteria pending Owner"},
             pedido])
        outcome = await run_review(
            env, kind="reactive", reasons=[served_mission_reason(MISSION)])
        self.assertEqual(outcome["action"], "pedido_created")
        self.assertEqual(executor.runs, 2)
        self.assertIn("has nothing in flight", executor.prompts[1])
        self.assertEqual(
            calls["created_tickets"][0]["params"]["mission_id"], MISSION)
        self.assertEqual(calls["escalations"], [])
        self.assertNotIn("idle_missions_asked", outcome)

    async def test_insisting_asks_the_owner_with_the_ceo_summary(
            self) -> None:
        env, calls, _executor, missions = await self._env(
            [{"action": "keep_going", "summary": "waiting on the Owner"}])
        outcome = await run_review(
            env, kind="reactive", reasons=[mission_idle_reason(MISSION)])
        self.assertEqual(outcome["action"], "rejected")
        self.assertEqual(outcome["idle_missions_asked"], [MISSION])
        escalation = calls["escalations"][0]
        self.assertEqual(escalation["mission_id"], MISSION)
        self.assertIn("waiting on the Owner",
                      escalation["decision"].human_action)
        stored = await missions.get_mission(MISSION)
        assert stored is not None
        self.assertTrue(stored["idle_question"]["key"])

    async def test_a_mission_with_work_in_flight_may_keep_going(
            self) -> None:
        env, calls, executor, _missions = await self._env(
            [{"action": "keep_going", "summary": "objective runs"}],
            objective_status="active")
        outcome = await run_review(
            env, kind="reactive", reasons=[served_mission_reason(MISSION)])
        self.assertEqual(outcome["action"], "keep_going")
        self.assertEqual(executor.runs, 1)
        self.assertEqual(calls["escalations"], [])

    async def test_a_review_not_woken_for_it_is_not_forced(self) -> None:
        env, calls, executor, _missions = await self._env(
            [{"action": "keep_going", "summary": "nothing new"}])
        outcome = await run_review(env, kind="scheduled", reasons=[])
        self.assertEqual(outcome["action"], "keep_going")
        self.assertEqual(executor.runs, 1)
        self.assertEqual(calls["escalations"], [])
        self.assertIn("NOTHING IN FLIGHT", executor.prompts[0])

    async def test_an_escalation_can_name_its_mission(self) -> None:
        escalate = {
            "action": "escalate", "summary": "ask the Owner the X session",
            "escalation": {"kind": "mission_scope",
                           "summary": "Sesión X para la lectura C",
                           "strategic_replan": True, "mission_id": MISSION},
        }
        env, calls, _executor, _missions = await self._env([escalate])
        outcome = await run_review(
            env, kind="reactive", reasons=[served_mission_reason(MISSION)])
        self.assertEqual(outcome["action"], "escalated")
        self.assertEqual(calls["escalations"][0]["mission_id"], MISSION)
        self.assertNotIn("idle_missions_asked", outcome)

    async def test_an_escalation_naming_an_unknown_mission_is_rejected(
            self) -> None:
        escalate = {
            "action": "escalate", "summary": "ask",
            "escalation": {"kind": "mission_scope", "summary": "Pregunta",
                           "strategic_replan": True,
                           "mission_id": "MIS-UNKNOWN"},
        }
        env, calls, _executor, _missions = await self._env(
            [escalate], objective_status="active")
        outcome = await run_review(env, kind="scheduled", reasons=[])
        self.assertEqual(outcome["action"], "rejected")
        self.assertIn("MIS-UNKNOWN", outcome["reason"])
        self.assertEqual(calls["escalations"], [])


class _LivenessRepo:
    def __init__(self, objective_status: str = "closed") -> None:
        self.missions = {MISSION: _mission(status=objective_status)}
        self.objectives = {"OBJ-F3AAEE49": {"status": objective_status}}
        self.tickets: list[dict[str, Any]] = [
            {"task_id": "pedido-2", "status": "completed"}]

    async def list_active_missions(self, project: Any = None,
                                   limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.missions.values()
                if row.get("status") == "active"]

    async def get_objective(self, objective_id: str) -> Any:
        return self.objectives.get(objective_id)

    async def list_mission_tickets(self, mission_id: str,
                                   limit: int = 50) -> list[dict[str, Any]]:
        return list(self.tickets)

    async def patch_mission(self, mission_id: str,
                            updates: dict[str, Any]) -> Any:
        self.missions[mission_id].update(updates)
        return dict(self.missions[mission_id])


class MissionLivenessTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, repo: _LivenessRepo,
                 comments: list[dict[str, Any]]) -> tuple[Any, list[str]]:
        adapter = SimpleNamespace(is_enabled=True, thread_of=_thread(comments))
        service = ObjectiveService(repo, SimpleNamespace(_business=adapter),
                                   None, None)
        wakes: list[str] = []
        service.set_ceo_wake(wakes.append)
        return service, wakes

    async def test_an_idle_mission_wakes_the_ceo_once_per_episode(
            self) -> None:
        repo = _LivenessRepo()
        comments = _comments()
        service, wakes = self._service(repo, comments)
        first = await service.wake_idle_missions()
        self.assertEqual(first["woken"], [MISSION])
        self.assertEqual(wakes, [mission_idle_reason(MISSION)])
        self.assertEqual(served_missions(wakes), [MISSION])
        again = await service.wake_idle_missions()
        self.assertEqual(again["woken"], [])
        self.assertEqual(len(wakes), 1)

    async def test_a_new_owner_note_opens_a_new_episode(self) -> None:
        repo = _LivenessRepo()
        comments = _comments()
        service, wakes = self._service(repo, comments)
        await service.wake_idle_missions()
        comments.insert(0, {"content": "Sigue con el deep ya.",
                            "createdAt": "2026-11-08T19:30:00.000Z"})
        later = await service.wake_idle_missions()
        self.assertEqual(later["woken"], [MISSION])
        self.assertEqual(len(wakes), 2)

    async def test_work_in_flight_wakes_nobody(self) -> None:
        repo = _LivenessRepo()
        repo.tickets.insert(0, {"task_id": "pedido-3", "status": "queued"})
        service, wakes = self._service(repo, _comments())
        self.assertEqual((await service.wake_idle_missions())["woken"], [])
        active = _LivenessRepo(objective_status="active")
        service, wakes = self._service(active, _comments())
        self.assertEqual((await service.wake_idle_missions())["woken"], [])
        self.assertEqual(wakes, [])

    async def test_an_unreadable_objective_wakes_nobody(self) -> None:
        repo = _LivenessRepo()
        repo.objectives.clear()
        service, wakes = self._service(repo, _comments())
        self.assertEqual((await service.wake_idle_missions())["woken"], [])
        self.assertEqual(wakes, [])


class _AnswerRepo(_SvcRepo):
    async def get_mission(self, mission_id: str) -> Any:
        return {"mission_id": mission_id, "erp_group_ref": GROUP}


class OwnerAnswerTests(unittest.IsolatedAsyncioTestCase):
    def _escalation(self, **params: Any) -> dict[str, Any]:
        return {
            "task_id": "esc-1", "project": "ops", "status": "waiting",
            "phase": "owner_escalation", "title": f"Misión {MISSION} parada",
            "waiting_reason": "owner_escalation:strategic_replan",
            "workflow_phases": [], "params": {"escalation": {}, **params},
        }

    async def test_the_answer_wakes_the_ceo_to_judge_its_mission(
            self) -> None:
        repo = _AnswerRepo()
        repo.tasks["esc-1"] = self._escalation(mission_id=MISSION)
        service = _make_service(repo)
        wakes: list[str] = []
        service.set_ceo_wake(wakes.append)
        result = await service.apply_owner_decision(
            "esc-1", approve=True, actor_id="owner",
            comment="Sigue con deep y scheduler")
        self.assertTrue(result["applied"])
        self.assertEqual(served_missions(wakes), [MISSION])
        self.assertIn("Sigue con deep y scheduler", wakes[0])

    async def test_an_answer_without_mission_still_returns_to_the_ceo(
            self) -> None:
        repo = _AnswerRepo()
        repo.tasks["esc-1"] = self._escalation()
        service = _make_service(repo)
        wakes: list[str] = []
        service.set_ceo_wake(wakes.append)
        await service.apply_owner_decision(
            "esc-1", approve=False, actor_id="owner", comment="No gastes")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(served_missions(wakes), [])
        self.assertIn("No gastes", wakes[0])

    async def test_an_escalation_about_a_mission_names_it(self) -> None:
        from company_mandate import OwnerAction, requires_owner
        from test_ceo_review import _mandate

        repo = _AnswerRepo()
        repo.tasks["esc-1"] = {"task_id": "esc-1", "status": "queued",
                               "params": {}}
        service = _make_service(repo)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "esc-1", "project": payload.project}

        service.create_task = fake_create  # type: ignore[method-assign]
        action = OwnerAction(kind="mission_idle", summary="Misión parada",
                             project="ops", strategic_replan=True)
        await service.create_owner_escalation(
            action=action, decision=requires_owner(action, _mandate()),
            source_id="mission-idle:test", mission_id=MISSION)
        self.assertEqual((seen[0].params or {})["mission_id"], MISSION)
        await service.create_owner_escalation(
            action=action, decision=requires_owner(action, _mandate()),
            source_id="ceo:test")
        self.assertNotIn("mission_id", seen[1].params or {})

    async def test_the_answer_joins_the_mission_record(self) -> None:
        repo = _AnswerRepo()
        service = _make_service(repo)
        posted: list[tuple[str, str]] = []

        async def add_comment(name: str, content: str) -> dict[str, Any]:
            posted.append((name, content))
            return {"ok": True}

        service._business = SimpleNamespace(is_enabled=True,
                                            add_comment=add_comment)
        await service._return_owner_answer(
            self._escalation(mission_id=MISSION), verdict="aprobada",
            comment="Sigue con deep")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], GROUP)
        self.assertTrue(posted[0][1].startswith("Respuesta del Owner"))
        self.assertIn("aprobada. Sigue con deep", posted[0][1])
        self.assertFalse(posted[0][1].startswith(
            engine_mod.AUTOMATED_NOTE_HEADS))


if __name__ == "__main__":
    unittest.main()
