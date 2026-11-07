"""Planner business memory and agent reach (TASK-2026-00571).

Contract fixed here (live case: OBJ-8E2257EF, research mission, where the
planner re-created a paste the Owner had closed and kept assigning it to
agents whose badge refuses other tickets):

- Every planning cycle sees the closed children with their business
  evidence (agent cycle, Owner session or ERP desk), returns a later close
  settled as SUPERSEDED, the people's notes on the objective ticket (engine
  notices left out) and the badge reach of the AI agents.
- The recovery re-check tests the park against that evidence instead of
  pushing a different plan.
- A child a person completed in the business reaches its objective (Owner
  transition and ERP desk) and resumes a blocked park; a park never drops a
  close from memory.
- The final review takes a business completion as the delivery verdict of
  a non-dev child.
- An agent return on a badge refusal an earlier return already hit does not
  replan: the ticket waits ``needs_owner`` and the generic stall rule hands
  it to the mechanic.

Recorded planner, dict-backed stores and a fake ERP; no models.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from mechanic.stall import streak_outcome  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from state import TaskSource  # noqa: E402
from task_service import TaskService  # noqa: E402

OBJ = "OBJ-571A"
GROUP = "TASK-2026-00545"
OWNER_NOTE = ("Desbloqueo Owner (sesión 2026-11-07): las 3 acciones "
              "pendientes están hechas y verificadas; cierre del objetivo en "
              "manos del motor.")
ENGINE_NOTICE = ("Objective waiting (blocked): falta solo el cierre humano "
                 "en ERP")


def _repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return repo


def _mirror(name: str, transition: dict[str, Any] | None = None,
            **extra: Any) -> dict[str, Any]:
    business: dict[str, Any] = {"name": name, **extra}
    if transition is not None:
        business["transition"] = transition
    return {"_mirrors": {"business": business}}


def _owner_close(to: str, reason: str, evidence: str = "") -> dict[str, Any]:
    return {"at": "2026-11-07T14:22:55+00:00", "from": "Working", "to": to,
            "reason": reason, "evidence": evidence}


def _children() -> dict[str, dict[str, Any]]:
    return {
        "shell": {"task_id": "shell", "status": "converted",
                  "objective_id": OBJ, "title": "Tramo inicial"},
        "limits": {
            "task_id": "limits", "status": "completed", "objective_id": OBJ,
            "title": "Elevar max_tickets 20 a 30", "coding_tool": "tier_c",
            "completed_at": "2026-11-06T17:00:00+00:00",
            "params": {"target_agent": "dev",
                       **_mirror("TASK-2026-00547")}},
        "paste": {
            "task_id": "paste", "status": "completed", "objective_id": OBJ,
            "title": "Publicar veredicto del épico en Jardal ERP",
            "completed_at": "2026-11-07T14:22:55+00:00",
            "params": {"target_agent": "secretary", **_mirror(
                "TASK-2026-00551", _owner_close(
                    "Completed", "Los 5 comentarios del veredicto publicados",
                    "IDs en comentario 01a1b663"))}},
        "smoke": {
            "task_id": "smoke", "status": "completed", "objective_id": OBJ,
            "title": "Smoke de research en producción", "coding_tool": "tier_c",
            "completed_at": "2026-11-07T14:22:45+00:00",
            "params": {"target_agent": "dev", **_mirror(
                "TASK-2026-00552", _owner_close(
                    "Completed", "Smoke ejecutado por la sesión Owner",
                    "submit, get y cancel verificados"))}},
        "dup": {
            "task_id": "dup", "status": "cancelled", "objective_id": OBJ,
            "title": "Publicar veredicto research vivo (4 comentarios)",
            "completed_at": "2026-11-07T15:02:19+00:00",
            "error": "Cancelado por transicion de TASK-2026-00554",
            "params": {"target_agent": "secretary", **_mirror(
                "TASK-2026-00554", _owner_close(
                    "Cancelled", "Duplicado exacto de TASK-2026-00551"))}},
    }


def _objective(**extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "objective_id": OBJ, "project": "p", "status": "active",
        "erp_group_ref": GROUP, "conversion_source_task_id": "shell",
        "anchor": {"goal": "research vivo", "acceptance": ["epico cerrado"]},
        "tickets_log": [{"task_id": "limits", "title": "Elevar max_tickets",
                         "tier": "tier_c", "sha": "e068afb",
                         "evidencia": "pytest 110 passed"}],
        "planner_returns": [{
            "task_id": "smoke", "title": "Smoke de research en producción",
            "agent": "dev", "resultado": "bloqueado",
            "motivo": "RESEARCH_ENABLED=false en producción",
            "necesidad": "Owner activa el flag y reinicia"}],
    }
    doc.update(extra)
    return doc


class _Erp:
    """Business reader: the objective ticket thread, newest first."""

    def __init__(self, comments: list[str] | None = None,
                 error: Exception | None = None) -> None:
        self._comments = comments or []
        self._error = error
        self.reads: list[tuple[str, int]] = []

    async def thread_of(self, task_name: str, *, limit: int = 20):
        self.reads.append((task_name, limit))
        if self._error is not None:
            raise self._error
        return {"comments": [
            {"content": text, "createdAt": f"2026-11-07T{14 + i:02d}:00:00Z"}
            for i, text in reversed(list(enumerate(self._comments)))]}


class _RecordedPlanner:
    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self.prompts: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del role, workdir, timeout_s
        self.prompts.append(prompt)
        target = next(line for line in prompt.splitlines()
                      if line.startswith("OUTPUT: write "))
        rel = target[len("OUTPUT: write "):].split(" with exactly")[0]
        path = self._repo / rel.strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "objective_status": "blocked",
            "state_summary": "waiting on the Owner"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _planner_env(test: unittest.TestCase, erp: Any = None):
    repo = _repo(test)
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle.\n", encoding="utf-8")
    planner = _RecordedPlanner(repo)
    env = engine_mod.ObjectiveEnv(
        objectives=engine_mod.InMemoryObjectiveStore(),
        tasks=engine_mod.InMemoryTaskLister(_children()),
        create_ticket=AsyncMock(return_value={"task_id": "new"}),
        patch_task=AsyncMock(),
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(), slots=MagicMock(), reviews=None, inbox=None,
        notify=AsyncMock(), repo=repo, erp=erp,
        planner_cfg={"timeout_minutes": 15, "policy_path": str(policy),
                     "agents": {"dev": {"enabled": True},
                                "secretary": {"enabled": True}}},
    )
    return env, planner


def _section(prompt: str, head: str, end: str) -> str:
    start = prompt.index(head)
    return prompt[start:prompt.index(end, start)]


class PlannerMemoryPromptTests(unittest.IsolatedAsyncioTestCase):
    async def _plan(self, erp: Any = None, **objective: Any) -> str:
        env, planner = _planner_env(self, erp)
        await env.objectives.create_objective(_objective(**objective))
        with patch.object(engine_mod, "ensure_erp_group", new=AsyncMock()):
            await engine_mod.plan_objective(env, OBJ)
        self.assertEqual(len(planner.prompts), 1)
        return planner.prompts[0]

    async def test_closed_children_carry_their_business_evidence(self):
        prompt = await self._plan()
        closed = _section(prompt, "CLOSED CHILDREN", "OBJECTIVE NOTES")
        lines = [line for line in closed.splitlines() if line.startswith("- ")]
        self.assertEqual([line.split()[1] for line in lines], [
            "TASK-2026-00547", "TASK-2026-00552", "TASK-2026-00551",
            "TASK-2026-00554"])
        self.assertIn("COMPLETED by business decision (Owner session", lines[2])
        self.assertIn("IDs en comentario 01a1b663", lines[2])
        self.assertIn("CANCELLED by business decision", lines[3])
        self.assertIn("Duplicado exacto de TASK-2026-00551", lines[3])
        self.assertIn("COMPLETED: pytest 110 passed", lines[0])
        self.assertNotIn("Tramo inicial", closed)

    async def test_settled_return_reads_as_superseded(self):
        prompt = await self._plan()
        returns = _section(prompt, "AGENT RETURNS", "INBOX")
        self.assertIn("SUPERSEDED, since completed: Smoke ejecutado por la "
                      "sesión Owner", returns)

    async def test_owner_notes_reach_the_planner_without_engine_notices(self):
        erp = _Erp([ENGINE_NOTICE, OWNER_NOTE, "Objective OBJ-571A resumed"])
        prompt = await self._plan(erp)
        notes = _section(prompt, "OBJECTIVE NOTES", "OPEN TICKETS")
        self.assertIn(GROUP, notes)
        self.assertIn("cierre del objetivo en manos del motor", notes)
        self.assertNotIn("falta solo el cierre humano", notes)
        self.assertNotIn("resumed", notes)
        self.assertEqual(erp.reads, [(GROUP, engine_mod.OBJECTIVE_NOTES_SCAN)])

    async def test_unreadable_notes_never_block_planning(self):
        prompt = await self._plan(_Erp(error=RuntimeError("erp down")))
        notes = _section(prompt, "OBJECTIVE NOTES", "OPEN TICKETS")
        self.assertIn("- (none)", notes)

    async def test_agents_section_states_the_badge_reach(self):
        prompt = await self._plan()
        self.assertIn("BUSINESS-RECORD REACH", prompt)
        self.assertIn("only on its OWN ticket", prompt)
        self.assertIn("Never route it to an AI agent", prompt)

    async def test_recovery_rechecks_the_evidence_first(self):
        prompt = await self._plan(recovery={
            "attempts": 1, "last_reason": "blocked: falta el pegado ERP"})
        recovery = " ".join(prompt[prompt.index("RECOVERY CONTEXT"):].split())
        self.assertIn("automatic re-check", recovery)
        self.assertIn("complete_candidate when the acceptance is met",
                      recovery)
        self.assertIn("never hand an AI agent work its reach denies",
                      recovery)
        self.assertNotIn("Plan differently", prompt)


class BusinessDecisionTests(unittest.TestCase):
    def test_a_reopened_child_carries_no_decision(self):
        child = dict(_children()["paste"], status="queued")
        self.assertIsNone(engine_mod.business_decision(child))
        self.assertEqual(engine_mod.business_completion(child), "")

    def test_an_agent_cycle_close_is_not_a_business_decision(self):
        self.assertEqual(
            engine_mod.business_completion(_children()["limits"]), "")

    def test_desk_decision_names_its_channel(self):
        child = dict(_children()["limits"], params={**_mirror(
            "TASK-2026-00547", {"to": "Completed", "channel": "desk",
                                "reason": "Decidido en el desk"})})
        decision = engine_mod.business_decision(child)
        assert decision is not None
        self.assertEqual(decision["channel"], "ERP desk")
        self.assertEqual(engine_mod.business_completion(child),
                         "Decidido en el desk")

    def test_repeated_denials_need_an_earlier_identical_refusal(self):
        refusal = "openceo_add_ticket_comment: Agents only write on their own ticket"
        objective = {"planner_returns": [
            {"task_id": "a", "denials": [refusal]}, {"task_id": "b"}]}
        self.assertEqual(engine_mod.repeated_boundary_denials(
            objective, [refusal, "openceo_get_ticket: other"]), [refusal])
        self.assertEqual(engine_mod.repeated_boundary_denials(
            {"planner_returns": [{"task_id": "a"}]}, [refusal]), [])


class FinalReviewBusinessEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_completion_verifies_a_non_dev_child(self):
        children = list(_children().values())
        report = await engine_mod.verify_evidence(_objective(), children, {})
        self.assertEqual(report["verified"], {"paste": "business"})
        self.assertEqual(report["unverified_domains"], {})

    async def test_agent_close_still_needs_its_domain_verifier(self):
        child = {"task_id": "mail-1", "status": "completed",
                 "objective_id": OBJ, "title": "Enviar correo",
                 "params": {"target_agent": "secretary",
                            "agent_result": {"resultado": "hecho"}}}
        report = await engine_mod.verify_evidence(_objective(), [child], {})
        self.assertEqual(report["unverified_domains"],
                         {"secretary": ["mail-1"]})


class BusinessCloseTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def _parked(self) -> tuple[engine_mod.ObjectiveEnv, Any]:
        objectives = engine_mod.InMemoryObjectiveStore()
        env = engine_mod.ObjectiveEnv(
            objectives=objectives,
            tasks=engine_mod.InMemoryTaskLister({
                "paste": {"task_id": "paste", "status": "completed",
                          "objective_id": OBJ}}),
            create_ticket=AsyncMock(), patch_task=AsyncMock(),
            executor=MagicMock(), quota=MagicMock(), slots=MagicMock(),
            reviews=None, inbox=None, notify=AsyncMock(), repo=Path.cwd())
        await objectives.create_objective(_objective(
            status="waiting", waiting_reason="blocked: falta el pegado ERP",
            recovery={"attempts": 1, "exhausted": False,
                      "due_at": "2026-11-08T04:00:00+00:00"}))
        return env, objectives

    def _close(self, business: bool) -> engine_mod.TicketCloseContext:
        return engine_mod.TicketCloseContext(
            task_id="paste", title="Publicar veredicto", tier="", sha="",
            handoff_excerpt="Business decision: publicado",
            evidencia="IDs en comentario 01a1b663", business=business)

    async def test_a_person_closing_a_child_resumes_a_blocked_park(self):
        env, objectives = await self._parked()
        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True})) as planned:
            await engine_mod.on_ticket_closed(
                env, {"task_id": "paste", "status": "completed",
                      "objective_id": OBJ}, self._close(business=True))
        planned.assert_awaited_once_with(env, OBJ)
        resumed = await objectives.get_objective(OBJ)
        assert resumed is not None
        self.assertEqual(resumed["status"], "active")
        self.assertIn("Publicar veredicto", resumed["capsule"]["text"])

    async def test_a_park_keeps_a_cycle_close_in_memory(self):
        env, objectives = await self._parked()
        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock()) as planned:
            result = await engine_mod.on_ticket_closed(
                env, {"task_id": "paste", "status": "completed",
                      "objective_id": OBJ}, self._close(business=False))
        planned.assert_not_awaited()
        self.assertEqual(result["action"], "objective-not-active")
        parked = await objectives.get_objective(OBJ)
        assert parked is not None
        self.assertEqual(parked["status"], "waiting")
        self.assertEqual(parked["tickets_log"][-1]["evidencia"],
                         "IDs en comentario 01a1b663")
        self.assertIn("Publicar veredicto", parked["capsule"]["text"])


class _Adapter:
    """Jardal ERP double for the explicit transition."""

    is_enabled = True

    def __init__(self, status: str = "Working") -> None:
        self.doc = {"name": "TASK-2026-00551", "status": status,
                    "openceo_ref": "api:TASK-2026-00551",
                    "openceo_task_id": "kid", "is_group": False}

    async def resolve_task(self, identifier):
        return dict(self.doc)

    async def transition_status(self, identifier, to_status, *,
                                work_dates=None):
        self.doc["status"] = to_status
        return {"ok": True}


class _Repo:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = dict(task)

    async def get_task(self, task_id):
        return dict(self.task) if task_id == self.task["task_id"] else None

    async def patch_task(self, task_id, updates):
        self.task = {**self.task, **dict(updates)}
        return dict(self.task)

    async def find_task_by_source(self, source, source_id):
        return None


class _Hub:
    async def broadcast(self, event, payload):
        return None


def _kid(status: str = "in_progress", **extra: Any) -> dict[str, Any]:
    task = {"task_id": "kid", "status": status, "project": "p",
            "objective_id": OBJ, "source": TaskSource.SPILLOVER.value,
            "source_id": "obj:x", "tags": [],
            "params": {"_mirrors": {"erpnext": {
                "name": "TASK-2026-00551", "status_pushed": status}}}}
    task.update(extra)
    return task


def _service(task: dict[str, Any], adapter: Any = None) -> TaskService:
    service = TaskService.__new__(TaskService)
    service._business = adapter or _Adapter()
    service._repo = _Repo(task)
    service._hub = _Hub()
    service._horizon_terminal = None
    service._config_loader = None
    service._mechanic_wake = None
    service._settings = SimpleNamespace(
        TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=False)
    service._trigger_objective_close = AsyncMock()  # type: ignore[method-assign]
    return service


class TaskServiceBusinessCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_transition_completed_reaches_the_objective(self):
        service = _service(_kid())
        result = await service.transition_ticket(
            "TASK-2026-00551", "Completed", "Comentarios publicados",
            evidence="IDs en comentario 01a1b663")
        await asyncio.sleep(0)
        self.assertEqual(result["outcome"], "applied")
        trigger = service._trigger_objective_close
        trigger.assert_awaited_once()
        ticket, stash = trigger.await_args.args
        self.assertEqual(ticket["objective_id"], OBJ)
        close = stash["_objective_close"]
        self.assertTrue(close["business"])
        self.assertEqual(close["evidencia"], "IDs en comentario 01a1b663")
        self.assertIn("Comentarios publicados", close["handoff_excerpt"])

    async def test_transition_outside_an_objective_or_not_completed_stays_put(self):
        service = _service(_kid(objective_id=None))
        await service.transition_ticket(
            "TASK-2026-00551", "Completed", "fin", evidence="tests verdes")
        service2 = _service(_kid())
        await service2.transition_ticket(
            "TASK-2026-00551", "Pending Review", "revisar",
            human_action="El Owner aprueba la demo")
        await asyncio.sleep(0)
        service._trigger_objective_close.assert_not_called()
        service2._trigger_objective_close.assert_not_called()

    async def test_desk_completed_is_audited_and_reaches_the_objective(self):
        service = _service(_kid("waiting"))
        result = await service.apply_business_status(
            task_id="kid", business_status="Completed")
        await asyncio.sleep(0)
        self.assertEqual(result["task_status"], "completed")
        mirror = service._repo.task["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["transition"]["to"], "Completed")
        self.assertEqual(mirror["transition"]["channel"], "desk")
        decision = engine_mod.business_decision(service._repo.task)
        assert decision is not None
        self.assertEqual(decision["channel"], "ERP desk")
        service._trigger_objective_close.assert_awaited_once()
        self.assertTrue(service._trigger_objective_close.await_args.args[1][
            "_objective_close"]["business"])

    async def test_desk_cancel_is_audited_without_a_close(self):
        service = _service(_kid("waiting"))
        await service.apply_business_status(
            task_id="kid", business_status="Cancelled")
        await asyncio.sleep(0)
        mirror = service._repo.task["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["transition"]["to"], "Cancelled")
        service._trigger_objective_close.assert_not_called()


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def limit(self, count: int) -> "_Cursor":
        return _Cursor(self._rows[:count])

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _Calls:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def find(self, query: dict[str, Any], projection: Any = None) -> _Cursor:
        return _Cursor([row for row in self.rows
                        if all(row.get(key) == value
                               for key, value in query.items())])


REFUSAL = ("openceo_add_ticket_comment", "Agents only write on their own ticket")


class RepeatedDenialReturnTests(unittest.IsolatedAsyncioTestCase):
    def _stub(self, previous_denials: list[str]):
        calls = _Calls([
            {"ticket": "TASK-2026-00566", "ok": False, "kind": "agent",
             "tool": REFUSAL[0], "reason": REFUSAL[1]},
            {"ticket": "TASK-2026-00566", "ok": False, "kind": "agent",
             "tool": "tools/call", "reason": "surface not exposed"},
            {"ticket": "TASK-2026-00999", "ok": False, "kind": "agent",
             "tool": "openceo_get_ticket", "reason": "other ticket"},
        ])
        repo = MagicMock()
        repo.db = {"mcp_calls": calls}
        repo.get_task = AsyncMock(return_value={"task_id": "paste-3",
                                                "status": "failed"})
        service = MagicMock()
        service.get_objective = AsyncMock(return_value={
            "objective_id": OBJ, "planner_returns": [
                {"task_id": "paste-1", "denials": previous_denials}]})
        service.plan_return = AsyncMock(return_value={"ok": True})
        service.park_return_gap = AsyncMock()
        service.record_return = AsyncMock(return_value={})
        stub = SimpleNamespace(_repo=repo, _objective_service=service,
                               comment_on_ticket=AsyncMock(return_value=True),
                               patch_task=AsyncMock(return_value={}))
        stub._hold_for_owner = MethodType(TaskService._hold_for_owner, stub)
        ticket = {"task_id": "paste-3", "title": "Publicar veredicto",
                  "agent": "dev", "status": "failed", "objective_id": OBJ,
                  "params": _mirror("TASK-2026-00566")}
        verdict = {"agent_result": {
            "resultado": "bloqueado",
            "motivo": "las boundary tools denegaron la escritura en 00070",
            "necesidad": "pegar los textos con permisos sobre 00070"}}
        return stub, service, ticket, verdict

    async def test_a_repeated_refusal_waits_for_the_owner_via_the_mechanic(self):
        refusal = f"{REFUSAL[0]}: {REFUSAL[1]}"
        stub, service, ticket, verdict = self._stub([refusal])
        await TaskService._trigger_objective_return(stub, ticket, verdict)
        service.plan_return.assert_not_called()
        service.park_return_gap.assert_not_called()
        service.record_return.assert_awaited_once()
        entry = service.record_return.await_args.args[1]
        self.assertEqual(entry["denials"], [refusal])
        task_id, updates = stub.patch_task.await_args.args
        self.assertEqual(task_id, "paste-3")
        self.assertEqual(updates["status"], "waiting")
        self.assertIsNone(updates["resume_at"])
        self.assertTrue(updates["waiting_reason"].startswith("needs_owner:"))
        self.assertIn("permisos sobre 00070", updates["waiting_reason"])
        stub.comment_on_ticket.assert_awaited_once()
        fields, trigger = streak_outcome(
            status="waiting", reason=updates["waiting_reason"],
            resume_at=None, params={}, now=datetime.now(timezone.utc),
            max_reparks=3, gap=timedelta(hours=12))
        self.assertEqual(trigger, "gave_up")

    async def test_a_first_refusal_replans_once_with_its_denials(self):
        stub, service, ticket, verdict = self._stub([])
        await TaskService._trigger_objective_return(stub, ticket, verdict)
        service.plan_return.assert_awaited_once()
        entry = service.plan_return.await_args.args[1]
        self.assertEqual(entry["denials"], [f"{REFUSAL[0]}: {REFUSAL[1]}"])
        service.record_return.assert_not_called()
        stub.patch_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
