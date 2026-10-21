"""Structured agent result + return to planner (TASK-2026-00202).

The agent ends hecho, bloqueado or no_aplica with its reason; a bloqueado
or no_aplica objective child returns once to the same planner, and a second
unresolved return parks the objective as capability_gap for the CEO review.
Loose tickets record the verdict without returning (TASK-2026-00204).

No network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    CodingStepResult,
    DevSessionManager,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.agent_result import (  # noqa: E402
    AGENT_RESULT_JSON_SCHEMA,
    SCHEMA_PATH,
    AgentResult,
    legacy_blocked_result,
    parse_agent_result,
)
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _planner_prompt,
    plan_objective,
    record_planner_return,
)
from objectives.service import ObjectiveService  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "real"
    loader = MagicMock()
    loader.validate_project = MagicMock(return_value=[])
    return DevSessionManager(settings, loader, repo=None)


def _dev_config() -> dict[str, Any]:
    return {
        "project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]},
        "dev": {
            "tiers": {
                "tier_a": {"account": "claudej"},
                "tier_b": {"account": "codexj"},
                "tier_c": {
                    "chain": ["muse", "dsh", "claudep"],
                    "muse": {"command": ["musej", "exec"]},
                    "dsh": {"command": ["node", "/opt/dsh/bin.js", "-"]},
                    "claudep": {"command": ["claudep"]},
                },
            },
            "providers": {
                "muse": {"command": ["musej", "exec"], "cwd": "/tmp/repo",
                         "env": {}},
            },
        },
    }


class AgentResultSchemaTests(unittest.TestCase):
    def test_schema_file_matches_module_dict(self) -> None:
        on_disk = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, AGENT_RESULT_JSON_SCHEMA)

    def test_schema_shape_is_flat_and_strict(self) -> None:
        schema = AGENT_RESULT_JSON_SCHEMA
        self.assertEqual(schema.get("type"), "object")
        # Flat: no $defs/$ref for the --output-schema consumer.
        dumped = json.dumps(schema)
        self.assertNotIn("$defs", dumped)
        self.assertNotIn("$ref", dumped)
        self.assertEqual(set(schema.get("required", [])),
                         {"resultado", "motivo"})
        props = schema.get("properties", {})
        self.assertEqual(set(props["resultado"].get("enum", [])),
                         {"hecho", "bloqueado", "no_aplica"})
        self.assertEqual(props["motivo"].get("type"), "string")
        self.assertEqual(props["necesidad"].get("type"), "string")
        self.assertEqual(props["entregables"].get("type"), "array")
        self.assertEqual(props["evidencia"].get("type"), "string")
        self.assertFalse(schema.get("additionalProperties", True))

    def test_model_accepts_minimal_payload(self) -> None:
        parsed = AgentResult.model_validate(
            {"resultado": "no_aplica", "motivo": "wrong agent"})
        self.assertEqual(parsed.resultado.value, "no_aplica")
        self.assertEqual(parsed.necesidad, "")
        self.assertEqual(parsed.entregables, [])
        self.assertEqual(parsed.evidencia, "")

    def test_model_rejects_unknown_resultado(self) -> None:
        with self.assertRaises(Exception):
            AgentResult.model_validate(
                {"resultado": "casi", "motivo": "x"})

    def test_model_rejects_blank_motivo(self) -> None:
        with self.assertRaises(Exception):
            AgentResult.model_validate(
                {"resultado": "hecho", "motivo": "  "})


class ParseAgentResultTests(unittest.TestCase):
    def test_fenced_block_parses(self) -> None:
        text = ("did some work\n```agent-result\n"
                '{"resultado": "bloqueado", "motivo": "need DNS", '
                '"necesidad": "un registro TXT", "entregables": ["a"], '
                '"evidencia": "log"}\n```\n')
        parsed = parse_agent_result(text)
        assert parsed is not None
        self.assertEqual(parsed.resultado.value, "bloqueado")
        self.assertEqual(parsed.motivo, "need DNS")
        self.assertEqual(parsed.necesidad, "un registro TXT")
        self.assertEqual(parsed.entregables, ["a"])
        self.assertEqual(parsed.evidencia, "log")

    def test_raw_json_parses(self) -> None:
        parsed = parse_agent_result(
            '{"resultado": "hecho", "motivo": "done"}')
        assert parsed is not None
        self.assertEqual(parsed.resultado.value, "hecho")

    def test_trailing_json_after_prose_parses(self) -> None:
        parsed = parse_agent_result(
            "some prose\nmore prose\n"
            '{"resultado": "no_aplica", "motivo": "not mine"}')
        assert parsed is not None
        self.assertEqual(parsed.resultado.value, "no_aplica")

    def test_last_block_wins(self) -> None:
        text = ("```agent-result\n"
                '{"resultado": "hecho", "motivo": "draft"}\n```\n'
                "reconsidered\n```agent-result\n"
                '{"resultado": "bloqueado", "motivo": "final"}\n```\n')
        parsed = parse_agent_result(text)
        assert parsed is not None
        self.assertEqual(parsed.resultado.value, "bloqueado")
        self.assertEqual(parsed.motivo, "final")

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(parse_agent_result("just prose, no verdict"))
        self.assertIsNone(parse_agent_result(""))
        self.assertIsNone(parse_agent_result(None))

    def test_malformed_returns_none(self) -> None:
        self.assertIsNone(parse_agent_result(
            "```agent-result\n{not json\n```"))

    def test_bad_resultado_returns_none(self) -> None:
        self.assertIsNone(parse_agent_result(
            '{"resultado": "HECHO", "motivo": "loud"}'))
        self.assertIsNone(parse_agent_result(
            '{"resultado": "maybe", "motivo": "x"}'))

    def test_blank_motivo_returns_none(self) -> None:
        self.assertIsNone(parse_agent_result(
            '{"resultado": "hecho", "motivo": "   "}'))

    def test_extra_keys_are_ignored(self) -> None:
        parsed = parse_agent_result(
            '{"resultado": "hecho", "motivo": "ok", "extra": 1}')
        assert parsed is not None
        self.assertEqual(parsed.motivo, "ok")


class LegacyBlockedResultTests(unittest.TestCase):
    def test_shape_matches_model_dump(self) -> None:
        payload = legacy_blocked_result("ERP down")
        self.assertEqual(
            set(payload),
            {"resultado", "motivo", "necesidad", "entregables",
             "evidencia"})
        self.assertEqual(payload["resultado"], "bloqueado")
        self.assertEqual(payload["motivo"], "ERP down")
        # It must always validate: the funnel stores it blind.
        AgentResult.model_validate(payload)

    def test_blank_motivo_falls_back(self) -> None:
        payload = legacy_blocked_result("   ")
        AgentResult.model_validate(payload)
        self.assertTrue(payload["motivo"].strip())


class CodingPromptResultTests(unittest.TestCase):
    def test_prompt_orders_agent_result_block(self) -> None:
        prompt = _manager()._build_coding_prompt(
            {"task_id": "t", "title": "t", "description": "d"}, {})
        self.assertIn("```agent-result", prompt)
        self.assertIn('"resultado"', prompt)
        self.assertIn("hecho", prompt)
        self.assertIn("bloqueado", prompt)
        self.assertIn("no_aplica", prompt)
        self.assertIn('"motivo"', prompt)
        self.assertIn("necesidad", prompt)


class TierExecutorSchemaFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_muse_link_carries_output_schema(self) -> None:
        manager = _manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=True)
        executor = await manager._resolve_tier_executor(
            CodingTool.TIER_C, _dev_config())
        self.assertEqual(
            executor.argv,
            ["musej", "exec", "--json", "--output-schema", str(SCHEMA_PATH)])
        self.assertEqual(executor.label, "tier_c:muse")
        self.assertTrue(SCHEMA_PATH.is_file())

    async def test_non_muse_links_unchanged(self) -> None:
        executor = await _manager()._resolve_tier_executor(
            CodingTool.TIER_B, _dev_config())
        self.assertEqual(
            executor.argv, ["codexj", "exec", "--ephemeral", "-"])
        manager = _manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=False)
        executor = await manager._resolve_tier_executor(
            CodingTool.TIER_C, {
                "project": {"repos": []},
                "dev": {"tiers": {"tier_c": {
                    "chain": ["claudep"],
                    "claudep": {"command": ["claudep"]},
                }}}})
        self.assertEqual(executor.argv, ["claudep", "--print"])

    async def test_flag_is_idempotent(self) -> None:
        argv = DevSessionManager._with_output_schema(
            ["musej", "exec", "--output-schema", "/tmp/s.json"])
        self.assertEqual(
            argv, ["musej", "exec", "--output-schema", "/tmp/s.json"])

    async def test_custom_command_without_exec_untouched(self) -> None:
        argv = DevSessionManager._with_output_schema(["musej"])
        self.assertEqual(argv, ["musej"])


def _stub_role_badge(manager):
    """Badge seam stub: these tests pin verdict parsing, not badges."""
    provider_stub = MagicMock()
    provider_stub.revoke = AsyncMock()
    manager._ticket_badge_provider = MagicMock(  # type: ignore[method-assign]
        return_value=provider_stub)
    manager._muse_role_overlays = AsyncMock(  # type: ignore[method-assign]
        return_value=({}, "badge-stub"))


class StepCodeParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdout_verdict_is_attached(self) -> None:
        manager = _manager()
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(argv=["musej", "exec"], prompt_via="argv",
                                   cwd=None, env={}, label="tier_c:muse"))

        async def _ok(command, stdin_payload=None, timeout=120, cwd=None,
                      env=None, sandbox=True, config_argv=None,
                      home_file_overlays=None):
            return True, ('done\n```agent-result\n{"resultado": "no_aplica", '
                          '"motivo": "needs sales data"}\n```\n'), ""

        manager._run_process = _ok  # type: ignore[method-assign]
        _stub_role_badge(manager)
        result = await manager._step_code(
            {"task_id": "t", "title": "t", "description": "d"}, {},
            CodingTool.TIER_C)
        assert result.agent_result is not None
        self.assertEqual(result.agent_result["resultado"], "no_aplica")
        self.assertEqual(result.agent_result["motivo"], "needs sales data")

    async def test_stdout_without_verdict_attaches_none(self) -> None:
        manager = _manager()
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(argv=["musej", "exec"], prompt_via="argv",
                                   cwd=None, env={}, label="tier_c:muse"))

        async def _ok(command, stdin_payload=None, timeout=120, cwd=None,
                      env=None, sandbox=True, config_argv=None,
                      home_file_overlays=None):
            return True, "did the thing", ""

        manager._run_process = _ok  # type: ignore[method-assign]
        _stub_role_badge(manager)
        result = await manager._step_code(
            {"task_id": "t", "title": "t", "description": "d"}, {},
            CodingTool.TIER_C)
        self.assertIsNone(result.agent_result)


class SimpleToolVerdictTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_verdict(self, verdict: dict[str, Any] | None):
        manager = _manager()
        manager._step_code = AsyncMock(  # type: ignore[method-assign]
            return_value=CodingStepResult(
                step={"step": "code", "status": "ok"}, prompt="p",
                summary="s", agent_result=verdict))
        task = {"task_id": "task-1", "title": "t", "description": "d",
                "params": {"target_agent": "dev"}, "coding_tool": "tier_c"}
        return await manager._run_simple_tool(
            task, {"project": {"repos": []}}, CodingTool.TIER_C, {})

    async def test_no_aplica_fails_with_verdict(self) -> None:
        output = await self._run_with_verdict(
            {"resultado": "no_aplica", "motivo": "not dev work",
             "necesidad": "sales data", "entregables": [],
             "evidencia": ""})
        self.assertEqual(output.status, TaskStatus.FAILED)
        assert output.params is not None
        self.assertEqual(output.params["agent_result"]["resultado"],
                         "no_aplica")
        # The planner keys survive (00178 funnel contract).
        self.assertEqual(output.params["target_agent"], "dev")

    async def test_bloqueado_fails_with_verdict(self) -> None:
        output = await self._run_with_verdict(
            {"resultado": "bloqueado", "motivo": "waiting on DNS",
             "necesidad": "", "entregables": [], "evidencia": ""})
        self.assertEqual(output.status, TaskStatus.FAILED)
        assert output.params is not None
        self.assertEqual(output.params["agent_result"]["resultado"],
                         "bloqueado")

    async def test_hecho_keeps_legacy_completion(self) -> None:
        output = await self._run_with_verdict(
            {"resultado": "hecho", "motivo": "done", "necesidad": "",
             "entregables": [], "evidencia": ""})
        # No repo: legacy single code step completes.
        self.assertEqual(output.status, TaskStatus.COMPLETED)


class _RecordedPlanner:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0
        self._target = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        target = None
        for line in prompt.splitlines():
            if line.startswith("OUTPUT: write "):
                rel = line[len("OUTPUT: write "):].split(
                    " with exactly")[0]
                target = self._repo / rel.strip()
        if target is None:
            assert self._target is not None
            target = self._target
        self._target = target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _return_env(test: unittest.TestCase, payloads: list[dict[str, Any]],
                tasks: dict[str, dict[str, Any]] | None = None,
                ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                           list[dict[str, Any]], InMemoryObjectiveStore]:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    policy = repo / "policy.md"
    policy.write_text("POLICY: test.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        return {"task_id": f"task-child-{counter['n']}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    objectives = InMemoryObjectiveStore()
    planner = _RecordedPlanner(repo, payloads)
    env = ObjectiveEnv(
        objectives=objectives,
        tasks=InMemoryTaskLister(dict(tasks or {})),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=repo,
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True},
                       "finance": {"enabled": True}},
        },
    )
    return env, planner, created, objectives


def _active_payload(ticket: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "objective_status": "active",
        "state_summary": "relaunch",
        "tickets": [],
        "roadmap_remaining": [],
    }
    if ticket is not None:
        payload["tickets"] = [ticket]
    return payload


def _dev_ticket(local_ref: str = "t2", title: str = "Relaunch") -> dict[str, Any]:
    return {
        "local_ref": local_ref, "title": title, "target_agent": "dev",
        "work_type": "implementation", "goal": "g", "why_now": "now",
        "acceptance_criteria": ["done"], "expected_artifacts": ["code"],
        "coding_tool": "tier_c", "tier_reason": "simple",
    }


class PlannerReturnLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_appends_entry(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective(
            {"objective_id": "OBJ-1", "status": "active"})
        await record_planner_return(
            store, "OBJ-1", task_id="task-1", title="Do", agent="finance",
            resultado="no_aplica", motivo="not mine", necesidad="sales")
        doc = await store.get_objective("OBJ-1")
        assert doc is not None
        entries = doc.get("planner_returns") or []
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["task_id"], "task-1")
        self.assertEqual(entry["agent"], "finance")
        self.assertEqual(entry["resultado"], "no_aplica")
        self.assertEqual(entry["motivo"], "not mine")
        self.assertEqual(entry["necesidad"], "sales")
        self.assertTrue(entry["at"])

    async def test_prompt_carries_returns_and_options(self) -> None:
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {},
             "planner_returns": [
                 {"task_id": "task-1", "title": "Do", "agent": "finance",
                  "resultado": "no_aplica", "motivo": "not mine",
                  "necesidad": "sales data", "at": "t"}]},
            "", [], "INBOX\n(none)\n", [])
        self.assertIn("AGENT RETURNS", prompt)
        self.assertIn("Do [finance/no_aplica]", prompt)
        self.assertIn("not mine", prompt)
        self.assertIn("sales data", prompt)
        self.assertIn("relaunch with guidance", prompt)
        self.assertIn("capability_gap", prompt)

    async def test_prompt_without_returns_shows_none(self) -> None:
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {}}, "", [],
            "INBOX\n(none)\n", [])
        self.assertIn("AGENT RETURNS", prompt)


class ReturnReplanTaggingTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, store: InMemoryObjectiveStore) -> None:
        await store.create_objective({
            "objective_id": "OBJ-1", "status": "active", "project": "p",
            "anchor": {"goal": "g", "acceptance": []}})

    async def test_return_replan_tags_created_tickets(self) -> None:
        env, planner, created, store = _return_env(
            self, [_active_payload(_dev_ticket())],
            {"task-1": {"task_id": "task-1", "title": "Old",
                        "status": "failed", "objective_id": "OBJ-1"}})
        await self._objective(store)
        result = await plan_objective(env, "OBJ-1", return_from="task-1")
        self.assertTrue(result.get("ok"))
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["params"].get("returns_from"), "task-1")

    async def test_plain_plan_tags_nothing(self) -> None:
        env, planner, created, store = _return_env(
            self, [_active_payload(_dev_ticket())])
        await self._objective(store)
        result = await plan_objective(env, "OBJ-1")
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(created), 1)
        self.assertNotIn("returns_from", created[0]["params"])


def _return_entry(**overrides: Any) -> dict[str, Any]:
    entry = {"task_id": "task-1", "title": "Do", "agent": "finance",
             "resultado": "no_aplica", "motivo": "not mine",
             "necesidad": "sales data"}
    entry.update(overrides)
    return entry


class ServiceReturnTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, env: ObjectiveEnv,
                 objective: dict[str, Any] | None) -> ObjectiveService:
        repo = MagicMock()
        repo.get_objective = AsyncMock(return_value=objective)
        config = MagicMock()
        config.has_project = MagicMock(return_value=True)
        service = ObjectiveService(repo, MagicMock(), config, MagicMock())
        service.env_for = MagicMock(return_value=env)  # type: ignore[method-assign]
        return service

    async def test_plan_return_missing_objective(self) -> None:
        env, planner, _, _ = _return_env(self, [_active_payload()])
        service = self._service(env, None)
        result = await service.plan_return("OBJ-9", _return_entry())
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "no-objective")
        self.assertEqual(planner.runs, 0)

    async def test_plan_return_inactive_records_only(self) -> None:
        env, planner, _, store = _return_env(
            self, [_active_payload(_dev_ticket())])
        await store.create_objective(
            {"objective_id": "OBJ-1", "status": "waiting",
             "project": "p"})
        service = self._service(
            env, {"objective_id": "OBJ-1", "status": "waiting",
                  "project": "p"})
        result = await service.plan_return("OBJ-1", _return_entry())
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "objective-not-active")
        self.assertEqual(planner.runs, 0)
        doc = await store.get_objective("OBJ-1")
        assert doc is not None
        self.assertEqual(len(doc.get("planner_returns") or []), 1)

    async def test_plan_return_active_plans_once_and_tags(self) -> None:
        env, planner, created, store = _return_env(
            self, [_active_payload(_dev_ticket())])
        await store.create_objective(
            {"objective_id": "OBJ-1", "status": "active", "project": "p",
             "anchor": {"goal": "g", "acceptance": []}})
        service = self._service(
            env, {"objective_id": "OBJ-1", "status": "active",
                  "project": "p"})
        result = await service.plan_return("OBJ-1", _return_entry())
        self.assertTrue(result.get("ok"))
        self.assertEqual(planner.runs, 1)
        self.assertEqual(created[0]["params"].get("returns_from"),
                         "task-1")

    async def test_park_return_gap_shape_and_wake(self) -> None:
        wakes: list[str] = []
        env, _, _, store = _return_env(self, [_active_payload()])
        env.ceo_wake = wakes.append  # type: ignore[method-assign]
        await store.create_objective(
            {"objective_id": "OBJ-1", "status": "active", "project": "p"})
        service = self._service(
            env, {"objective_id": "OBJ-1", "status": "active",
                  "project": "p"})
        result = await service.park_return_gap("OBJ-1", _return_entry())
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "capability_gap")
        doc = await store.get_objective("OBJ-1")
        assert doc is not None
        self.assertEqual(doc.get("status"), "waiting")
        gap = doc.get("capability_gap") or {}
        self.assertIn("finance/no_aplica", gap.get("summary", ""))
        self.assertIn("sales data", gap.get("summary", ""))
        self.assertEqual(gap["gaps"][0]["task_id"], "task-1")
        self.assertTrue(gap.get("declared_at"))
        assert isinstance(env.notify, AsyncMock)
        env.notify.assert_awaited_once()
        self.assertEqual(len(wakes), 1)
        self.assertIn("capability_gap", wakes[0])


class TriggerReturnHookTests(unittest.IsolatedAsyncioTestCase):
    def _stub(self, fresh_status: str = "failed",
              objective_id: str = "OBJ-1"):
        repo = MagicMock()
        repo.get_task = AsyncMock(
            return_value={"task_id": "task-1", "status": fresh_status})
        service = MagicMock()
        service.plan_return = AsyncMock(return_value={"ok": True})
        service.park_return_gap = AsyncMock(
            return_value={"ok": False, "reason": "capability_gap"})
        stub = SimpleNamespace(
            _repo=repo, _objective_service=service,
            comment_on_ticket=AsyncMock(return_value=True))
        ticket = {"task_id": "task-1", "title": "Do", "agent": "finance",
                  "status": "failed", "objective_id": objective_id}
        return stub, service, ticket

    def _verdict(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "agent_result": {"resultado": "no_aplica", "motivo": "not mine",
                             "necesidad": "sales", "entregables": [],
                             "evidencia": ""}}
        params.update(overrides)
        return params

    async def test_no_verdict_is_ignored(self) -> None:
        stub, service, ticket = self._stub()
        await TaskService._trigger_objective_return(stub, ticket, {})
        service.plan_return.assert_not_called()
        service.park_return_gap.assert_not_called()
        stub.comment_on_ticket.assert_not_called()

    async def test_material_failure_skips_double_replan(self) -> None:
        stub, service, ticket = self._stub()
        params = self._verdict(material_failure={"tier": "tier_c"})
        await TaskService._trigger_objective_return(stub, ticket, params)
        service.plan_return.assert_not_called()
        service.park_return_gap.assert_not_called()

    async def test_cancelled_is_ignored(self) -> None:
        stub, service, ticket = self._stub(fresh_status="cancelled")
        await TaskService._trigger_objective_return(
            stub, ticket, self._verdict())
        service.plan_return.assert_not_called()
        service.park_return_gap.assert_not_called()

    async def test_loose_ticket_records_without_return(self) -> None:
        stub, service, ticket = self._stub(objective_id="")
        await TaskService._trigger_objective_return(
            stub, ticket, self._verdict())
        service.plan_return.assert_not_called()
        service.park_return_gap.assert_not_called()

    async def test_first_return_plans_once(self) -> None:
        stub, service, ticket = self._stub()
        await TaskService._trigger_objective_return(
            stub, ticket, self._verdict())
        service.plan_return.assert_awaited_once()
        service.park_return_gap.assert_not_called()
        args = service.plan_return.await_args.args
        self.assertEqual(args[0], "OBJ-1")
        self.assertEqual(args[1]["task_id"], "task-1")
        self.assertEqual(args[1]["resultado"], "no_aplica")
        stub.comment_on_ticket.assert_awaited_once()

    async def test_second_return_parks_gap(self) -> None:
        stub, service, ticket = self._stub()
        params = self._verdict(returns_from="task-0")
        await TaskService._trigger_objective_return(stub, ticket, params)
        service.park_return_gap.assert_awaited_once()
        service.plan_return.assert_not_called()
        stub.comment_on_ticket.assert_awaited_once()

    async def test_service_error_never_fails(self) -> None:
        stub, service, ticket = self._stub()
        service.plan_return = AsyncMock(side_effect=RuntimeError("boom"))
        await TaskService._trigger_objective_return(
            stub, ticket, self._verdict())
        # No raise: the failed ticket stands with its verdict.


class PolicyReturnTests(unittest.TestCase):
    def test_policy_names_single_return(self) -> None:
        text = planner_mod.load_policy_text()
        self.assertIn("returns once to this planner", text)
        self.assertIn("relaunch it with guidance", text)
        self.assertIn("never checked against the catalog", text)


if __name__ == "__main__":
    unittest.main()
