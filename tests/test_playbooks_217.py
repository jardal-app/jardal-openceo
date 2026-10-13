"""Playbooks: objective-level crystallization with Owner approval.

TASK-2026-00217. On objective close the planner judges recurrence and
proposes a playbook (stages with agent+skill, order, inputs/outputs,
stop conditions); the Owner may also request one for a closed
objective. Approval saves to playbooks/<name> in git with the approval
reference; the planner receives approved playbooks in its index,
follows them by default, explains deviations and proposes updates.

No network, no LLM: simulated planner runs, fixture git origin.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for _path in (CORE_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from objectives import playbooks as pb  # noqa: E402
from objectives.engine import (  # noqa: E402
    _planner_prompt,
    create_playbook_update_questions,
)
from objectives.planner_models import (  # noqa: E402
    PlannerResult,
    validate_planner_result,
)
from test_company_objectives import (  # noqa: E402
    _ExplodingExecutor,
    _child,
    _env_for,
    _mirrored,
    _plan_result,
    _scratch_repo,
)


def draft_playbook(name: str = "onboard-client") -> dict[str, Any]:
    return {
        "name": name,
        "summary": "Onboard a new client end to end.",
        "when_to_use": "A new client signs and needs setup.",
        "stages": [
            {"order": 1, "agent": "operations",
             "skill": "crear-proyecto",
             "goal": "Create the project workspace.",
             "inputs": ["project_key"], "outputs": ["git_repo"]},
            {"order": 2, "agent": "secretary",
             "skill": "enviar-correo",
             "goal": "Welcome the client by email.",
             "inputs": ["to"], "outputs": ["send_confirmation"]},
        ],
        "stop": ["welcome email sent"],
    }


def write_playbook(repo: Path, payload: dict[str, Any]) -> Path:
    import yaml
    skill_dir = repo / "playbooks" / str(payload["name"])
    skill_dir.mkdir(parents=True)
    target = skill_dir / "playbook.yaml"
    target.write_text(yaml.safe_dump(payload, sort_keys=False),
                      encoding="utf-8")
    return target


class PlaybookFormatTests(unittest.TestCase):
    def test_valid_playbook_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_playbook(repo, draft_playbook())
            found = pb.load_playbooks(repo)
        self.assertEqual(list(found), ["onboard-client"])
        book = found["onboard-client"]
        self.assertEqual(book.version, 1)
        self.assertEqual(len(book.stages), 2)
        self.assertEqual(book.stages[0].agent, "operations")

    def test_invalid_playbook_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_playbook(repo, draft_playbook("good"))
            bad_dir = repo / "playbooks" / "bad"
            bad_dir.mkdir(parents=True)
            (bad_dir / "playbook.yaml").write_text(
                "name: [unclosed\n", encoding="utf-8")
            found = pb.load_playbooks(repo)
        self.assertEqual(list(found), ["good"])

    def test_index_lists_names_and_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            write_playbook(repo, draft_playbook())
            text = pb.playbooks_index_text(pb.load_playbooks(repo))
        self.assertIn("onboard-client", text)
        self.assertIn("A new client signs", text)
        self.assertIn("operations/crear-proyecto", text)

    def test_empty_index_renders_empty(self) -> None:
        self.assertEqual(pb.playbooks_index_text({}), "")


class ObjectiveBriefTests(unittest.TestCase):
    def test_brief_carries_goal_stages_and_stop(self) -> None:
        objective = {"objective_id": "OBJ-1",
                     "anchor": {"goal": "Ship v2",
                                "acceptance": ["v2 live"]}}
        children = [
            {"task_id": "a", "title": "build", "status": "completed",
             "params": {"target_agent": "dev", "skill": None,
                        "agent_result": {
                            "resultado": "hecho", "motivo": "built",
                            "entregables": ["code"],
                            "evidencia": "tests green"},
                        "ticket_outputs": {"texts": ["code"],
                                           "files": []}}},
            {"task_id": "b", "title": "mail", "status": "completed",
             "params": {"target_agent": "secretary",
                        "skill": "enviar-correo",
                        "agent_result": {
                            "resultado": "hecho", "motivo": "sent",
                            "entregables": ["send_confirmation"],
                            "evidencia": "sent"},
                        "ticket_outputs": {"texts": [],
                                           "files": []}}},
        ]
        brief = pb.build_objective_brief(objective, children)
        self.assertIn("Ship v2", brief)
        self.assertIn("dev", brief)
        self.assertIn("secretary/enviar-correo", brief)
        self.assertIn("v2 live", brief)


class ProposalModelTests(unittest.TestCase):
    def test_recurrent_needs_a_playbook(self) -> None:
        with self.assertRaises(ValueError):
            pb.PlaybookProposal.model_validate(
                {"recurrent": True, "reason": "repeats",
                 "playbook": None})
        with self.assertRaises(ValueError):
            pb.PlaybookProposal.model_validate(
                {"recurrent": False, "reason": "one-off",
                 "playbook": draft_playbook()})
        ok = pb.PlaybookProposal.model_validate(
            {"recurrent": False, "reason": "one-off", "playbook": None})
        self.assertFalse(ok.recurrent)

    def test_proposal_validation_checks_agents_and_skills(self) -> None:
        proposal = pb.PlaybookProposal.model_validate(
            {"recurrent": True, "reason": "repeats",
             "playbook": draft_playbook()})
        agents = {"operations": {"enabled": True},
                  "secretary": {"enabled": True}}
        skills = {"operations": ["crear-proyecto"],
                  "secretary": ["enviar-correo"]}
        self.assertEqual(pb.validate_proposal(proposal, agents, skills),
                         [])
        bad_agents = dict(agents)
        bad_agents["operations"] = {"enabled": False}
        errors = pb.validate_proposal(proposal, bad_agents, skills)
        self.assertTrue(any("operations" in item for item in errors))
        errors = pb.validate_proposal(
            proposal, agents, {"operations": [], "secretary": []})
        self.assertTrue(any("crear-proyecto" in item for item in errors))


class _ProposalPlanner:
    """Simulated headless run writing the recorded proposal (0 tokens)."""

    def __init__(self, repo: Path, payload: dict[str, Any]) -> None:
        self._repo = repo
        self._payload = payload
        self.prompts: list[str] = []
        self.runs = 0

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        from types import SimpleNamespace
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        target = None
        for line in prompt.splitlines():
            if line.startswith("OUTPUT: write "):
                rel = line[len("OUTPUT: write "):].split(
                    " with exactly")[0]
                target = self._repo / rel.strip()
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self._payload), encoding="utf-8")
        self.runs += 1
        return SimpleNamespace(returncode=0, output_tail="PROPOSAL_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _closed_objective(env: Any) -> None:
    async def go() -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "closed",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
    import asyncio
    asyncio.run(go())


class ProposePlaybookTests(unittest.IsolatedAsyncioTestCase):
    def _parent(self) -> dict[str, Any]:
        return _mirrored("task-parent", "TASK-1", status="converted",
                         objective_id="OBJ-1")

    def _tasks(self) -> dict[str, dict[str, Any]]:
        kid = _child("kid-1", "completed", dev=False)
        kid["params"]["target_agent"] = "secretary"
        kid["params"]["skill"] = "enviar-correo"
        kid["params"]["agent_result"] = {
            "resultado": "hecho", "motivo": "sent",
            "entregables": ["send_confirmation"], "evidencia": "sent"}
        return {"task-parent": self._parent(), "kid-1": kid}

    async def test_recurrent_proposal_opens_owner_question(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=self._tasks())
        payload = {"recurrent": True, "reason": "monthly repeat",
                   "playbook": draft_playbook()}
        env.executor = _ProposalPlanner(repo, payload)  # type: ignore
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "closed",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        agents = {"operations": {"enabled": True},
                  "secretary": {"enabled": True}}
        skills = {"operations": ["crear-proyecto"],
                  "secretary": ["enviar-correo"]}
        result = await pb.propose_playbook(
            env, "OBJ-1", "close", agents=agents, skills=skills)
        self.assertEqual(result["action"], "proposed")
        self.assertEqual(len(created), 1)
        spec = created[0]
        self.assertEqual(spec["source_id"], "playbook:OBJ-1")
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "playbook")
        self.assertEqual(block["playbook"]["name"], "onboard-client")
        self.assertEqual(len(block["playbook"]["stages"]), 2)

    async def test_owner_request_proposes_on_demand(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=self._tasks())
        payload = {"recurrent": True, "reason": "owner asked",
                   "playbook": draft_playbook()}
        env.executor = _ProposalPlanner(repo, payload)  # type: ignore
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "closed",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        agents = {"operations": {"enabled": True},
                  "secretary": {"enabled": True}}
        skills = {"operations": ["crear-proyecto"],
                  "secretary": ["enviar-correo"]}
        result = await pb.propose_playbook(
            env, "OBJ-1", "owner_request", agents=agents, skills=skills)
        self.assertEqual(result["action"], "proposed")
        self.assertEqual(created[0]["source_id"], "playbook:OBJ-1")

    async def test_not_recurrent_asks_nothing(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=self._tasks())
        payload = {"recurrent": False, "reason": "one-off",
                   "playbook": None}
        env.executor = _ProposalPlanner(repo, payload)  # type: ignore
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "closed",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        result = await pb.propose_playbook(env, "OBJ-1", "close")
        self.assertEqual(result["action"], "not_recurrent")
        self.assertEqual(created, [])

    async def test_open_objective_never_runs_the_planner(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=self._tasks())
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        result = await pb.propose_playbook(env, "OBJ-1", "close")
        self.assertEqual(result["action"], "not_closed")
        self.assertEqual(created, [])

    async def test_invalid_proposal_asks_nothing(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=self._tasks())
        bad = draft_playbook()
        bad["stages"][0]["agent"] = "ghost"
        payload = {"recurrent": True, "reason": "repeats",
                   "playbook": bad}
        env.executor = _ProposalPlanner(repo, payload)  # type: ignore
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "closed",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        agents = {"operations": {"enabled": True},
                  "secretary": {"enabled": True}}
        skills = {"operations": ["crear-proyecto"],
                  "secretary": ["enviar-correo"]}
        result = await pb.propose_playbook(
            env, "OBJ-1", "close", agents=agents, skills=skills)
        self.assertEqual(result["action"], "proposal_invalid")
        self.assertEqual(created, [])


def _git_ok(workdir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True,
        timeout=60)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_origin(root: Path) -> tuple[Path, Path]:
    origin = root / "origin.git"
    _git_ok(root, "init", "--bare", "-b", "main", str(origin))
    repo = root / "repo"
    _git_ok(root, "clone", str(origin), str(repo))
    _git_ok(repo, "config", "user.email", "test@jardal.app")
    _git_ok(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git_ok(repo, "add", "README.md")
    _git_ok(repo, "commit", "-m", "seed")
    _git_ok(repo, "push", "origin", "main")
    return origin, repo


def origin_show(origin: Path, ref_path: str) -> str:
    return _git_ok(origin.parent, "--git-dir", str(origin), "show",
                   f"main:{ref_path}")


class SavePlaybookTests(unittest.TestCase):
    def test_approve_saves_versioned_playbook_with_approval_ref(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = make_origin(Path(tmp))
            saved = pb.save_approved_playbook(
                repo, draft_playbook(),
                approval={"question": "TASK-9", "actor": "owner",
                          "at": "2026-10-13T00:00:00+00:00"})
            self.assertEqual(saved["version"], 1)
            text = origin_show(origin,
                               "playbooks/onboard-client/playbook.yaml")
            stored = yaml.safe_load(text)
            self.assertEqual(stored["approved_by"]["question"], "TASK-9")
            self.assertEqual(len(stored["stages"]), 2)

    def test_update_bumps_version(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = make_origin(Path(tmp))
            approval = {"question": "TASK-9", "actor": "owner",
                        "at": "2026-10-13T00:00:00+00:00"}
            pb.save_approved_playbook(repo, draft_playbook(),
                                      approval=approval)
            changed = draft_playbook()
            changed["stages"].append(
                {"order": 3, "agent": "finance",
                 "skill": "registrar-gasto",
                 "goal": "Book the onboarding cost.",
                 "inputs": ["dictated_text"], "outputs": ["journal_entry"]})
            saved = pb.save_approved_playbook(
                repo, changed, approval=approval, update=True)
            self.assertEqual(saved["version"], 2)
            text = origin_show(origin,
                               "playbooks/onboard-client/playbook.yaml")
            self.assertEqual(len(yaml.safe_load(text)["stages"]), 3)

    def test_update_without_existing_playbook_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _origin, repo = make_origin(Path(tmp))
            with self.assertRaises(pb.PlaybookError):
                pb.save_approved_playbook(
                    repo, draft_playbook(),
                    approval={"question": "TASK-9", "actor": "owner",
                              "at": "now"},
                    update=True)

    def test_bad_payload_never_touches_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = make_origin(Path(tmp))
            before = _git_ok(repo, "rev-parse", "origin/main")
            bad = draft_playbook()
            bad["stages"] = []
            with self.assertRaises(pb.PlaybookError):
                pb.save_approved_playbook(
                    repo, bad,
                    approval={"question": "TASK-9", "actor": "owner",
                              "at": "now"})
            self.assertEqual(_git_ok(repo, "rev-parse", "origin/main"),
                             before)


def _valid_result(**overrides: Any) -> PlannerResult:
    from objectives.planner_models import PlannedTicket
    base: dict[str, Any] = {
        "objective_status": "active",
        "state_summary": "plan",
        "tickets": [PlannedTicket(
            local_ref="t1", title="Do it", target_agent="secretary",
            work_type="execution", goal="g", why_now="now",
            acceptance_criteria=["done"],
            expected_artifacts=["mail"])],
    }
    base.update(overrides)
    return PlannerResult.model_validate(base)


class PlannerReferenceTests(unittest.TestCase):
    def test_prompt_carries_playbook_index(self) -> None:
        text = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"acceptance": []}},
            "", [], "", [], playbooks_md="PLAYBOOKS:\n- onboard-client")
        self.assertIn("PLAYBOOKS:", text)
        self.assertIn("onboard-client", text)
        self.assertIn('"playbook?"', text)

    def test_prompt_without_playbooks_has_no_section(self) -> None:
        text = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"acceptance": []}},
            "", [], "", [])
        self.assertNotIn("PLAYBOOKS:", text)

    def test_known_playbook_with_deviations_validates(self) -> None:
        import asyncio

        async def go() -> None:
            errors = await validate_planner_result(
                _valid_result(playbook="onboard-client",
                              playbook_deviations=["skipped mail"]),
                agents={"secretary": {"enabled": True}},
                open_titles=[], task_exists=_never_exists,
                playbooks={"onboard-client"})
            self.assertEqual(errors, [])
        asyncio.run(go())

    def test_unknown_playbook_rejected(self) -> None:
        import asyncio

        async def go() -> None:
            errors = await validate_planner_result(
                _valid_result(playbook="ghost"),
                agents={"secretary": {"enabled": True}},
                open_titles=[], task_exists=_never_exists,
                playbooks={"onboard-client"})
            self.assertTrue(any("ghost" in item for item in errors))
        asyncio.run(go())

    def test_update_proposal_validated(self) -> None:
        import asyncio

        async def go() -> None:
            good = await validate_planner_result(
                _valid_result(proposed_playbook_update={
                    "playbook": "onboard-client",
                    "change": "Add a finance stage.",
                    "reason": "Cost was missed twice."}),
                agents={"secretary": {"enabled": True}},
                open_titles=[], task_exists=_never_exists,
                playbooks={"onboard-client"})
            self.assertEqual(good, [])
            bad = await validate_planner_result(
                _valid_result(proposed_playbook_update={
                    "playbook": "ghost",
                    "change": "Whatever.",
                    "reason": "Because."}),
                agents={"secretary": {"enabled": True}},
                open_titles=[], task_exists=_never_exists,
                playbooks={"onboard-client"})
            self.assertTrue(any("ghost" in item for item in bad))
        asyncio.run(go())


async def _never_exists(task_id: str) -> bool:
    del task_id
    return False


class UpdateQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_proposal_opens_owner_question(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks={})
        result = _valid_result(proposed_playbook_update={
            "playbook": "onboard-client",
            "change": "Add a finance stage.",
            "reason": "Cost was missed twice."})
        task_ids = await create_playbook_update_questions(
            env, "OBJ-1", "proj", result)
        self.assertEqual(len(task_ids), 1)
        self.assertEqual(len(created), 1)
        block = created[0]["params"]["owner_question"]
        self.assertEqual(block["kind"], "playbook")
        self.assertEqual(block["playbook"]["update"], "onboard-client")
        self.assertIn("finance", block["playbook"]["change"])


class PlaybookApproveTests(unittest.IsolatedAsyncioTestCase):
    def _question(self, repo: Any, block_playbook: dict[str, Any]) -> None:
        from test_owner_channel_206 import _question_task
        question = _question_task("q9", kind="playbook", origin=None)
        question["params"]["owner_question"]["playbook"] = block_playbook
        repo.tasks["q9"] = question

    async def test_approve_saves_playbook_and_closes(self) -> None:
        from test_owner_channel_206 import _SvcRepo, _make_service
        repo = _SvcRepo()
        self._question(repo, draft_playbook())
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        with patch("task_service.save_approved_playbook",
                   return_value={"ok": True, "name": "onboard-client",
                                 "version": 1,
                                 "path": "playbooks/onboard-client"}) as save:
            result = await service.apply_owner_decision(
                "q9", approve=True, actor_id="owner", comment="va")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_close")
        save.assert_called_once()
        args, kwargs = save.call_args
        self.assertEqual(args[1]["name"], "onboard-client")
        self.assertEqual(kwargs["approval"]["actor"], "owner")
        stored = await repo.get_task("q9")
        assert stored is not None
        self.assertEqual(stored["status"], "completed")

    async def test_save_failure_keeps_question_open(self) -> None:
        from test_owner_channel_206 import _SvcRepo, _make_service
        repo = _SvcRepo()
        self._question(repo, draft_playbook())
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        with patch("task_service.save_approved_playbook",
                   side_effect=pb.PlaybookError("push rejected")):
            with self.assertRaises(pb.PlaybookError):
                await service.apply_owner_decision(
                    "q9", approve=True, actor_id="owner", comment="va")
        stored = await repo.get_task("q9")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertNotIn("decision", stored["params"]["owner_question"])

    async def test_deny_saves_nothing(self) -> None:
        from test_owner_channel_206 import _SvcRepo, _make_service
        repo = _SvcRepo()
        self._question(repo, draft_playbook())
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        with patch("task_service.save_approved_playbook") as save:
            result = await service.apply_owner_decision(
                "q9", approve=False, actor_id="owner", comment="no")
        self.assertTrue(result["applied"])
        save.assert_not_called()


class CloseHookTests(unittest.IsolatedAsyncioTestCase):
    async def _closed_world(self, test: Any, executor: Any,
                            proposals: bool):
        repo = _scratch_repo(test)
        kid = _child("kid-1", "completed", dev=False)
        kid["params"]["target_agent"] = "secretary"
        tasks = {
            "task-parent": _mirrored(
                "task-parent", "TASK-1", status="converted",
                objective_id="OBJ-1"),
            "kid-1": kid,
        }
        env, _planner, created, _patches, _notices = _env_for(
            test, repo, [_plan_result()], tasks=tasks)
        env.executor = executor
        env.playbook_proposals = proposals
        env.planner_cfg["agents"] = {
            "dev": {"enabled": True},
            "operations": {"enabled": True},
            "secretary": {"enabled": True},
        }

        async def _ok(child: dict[str, Any]) -> dict[str, Any]:
            del child
            return {"ok": True, "missing_artifacts": [],
                    "unmet_criteria": []}

        env.delivery_verifiers = {"secretary": _ok}
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj",
            "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        return env, created

    async def test_close_proposes_playbook(self) -> None:
        from objectives.engine import run_final_review
        payload = {"recurrent": True, "reason": "repeats",
                   "playbook": draft_playbook()}
        env, created = await self._closed_world(
            self, _ExplodingExecutor(), True)
        env.executor = _ProposalPlanner(env.repo, payload)
        result = await run_final_review(env, "OBJ-1")
        self.assertEqual(result["action"], "closed")
        kinds = [row["params"]["owner_question"]["kind"]
                 for row in created]
        self.assertEqual(kinds, ["playbook"])

    async def test_close_without_flag_asks_nothing(self) -> None:
        from objectives.engine import run_final_review
        env, created = await self._closed_world(
            self, _ExplodingExecutor(), False)
        result = await run_final_review(env, "OBJ-1")
        self.assertEqual(result["action"], "closed")
        self.assertEqual(created, [])

    async def test_proposal_failure_never_breaks_close(self) -> None:
        from objectives.engine import run_final_review
        env, created = await self._closed_world(
            self, _ExplodingExecutor(), True)
        result = await run_final_review(env, "OBJ-1")
        self.assertEqual(result["action"], "closed")
        self.assertEqual(created, [])
