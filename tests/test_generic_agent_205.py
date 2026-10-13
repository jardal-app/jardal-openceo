"""Generic CLI agent executor (TASK-2026-00205).

Any catalog agent runs the same: one headless process per ticket with its
profile (LLM path), or the YAML engine when its suggested skill is exact
(skill dir carries ``workflow.yaml``) and the ticket targets it. The
legacy dev node is untouched: the generic rule only admits enabled,
unwired AI agents.

No network, no CLI, no LLM: scripted executors, a dict-backed workflow
store, and the real YAML parser/engine for the exact-skill path.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    AgentCard,
    Catalog,
    CatalogError,
    IndexEntry,
    RoleProfile,
    StaffRoster,
    load_catalog,
)
from graph import OpenCEOGraph, resolve_agent_gate  # noqa: E402
from objectives.agent_result import AgentResult  # noqa: E402
from orchestration.generic_agent import (  # noqa: E402
    GENERIC_AGENT_TIMEOUT_S,
    GenericAgentWiring,
    build_agent_prompt,
    muse_role_spec,
    resolve_exact_skill_path,
    run_generic_agent,
)
from orchestration.live import ExecResult  # noqa: E402
from router import CEORouter  # noqa: E402
from settings import Settings  # noqa: E402
from state import AgentName, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402
from workflow_service import WorkflowService  # noqa: E402


def tiny_catalog(specs: dict[str, dict[str, Any]]) -> Catalog:
    entries = [
        IndexEntry(name=name, summary=f"{name} does things, not others.",
                   card=f"agents/{name}.yaml")
        for name in specs
    ]
    agents = {}
    for name, spec in specs.items():
        enabled = bool(spec.get("enabled", True))
        agents[name] = AgentCard(
            name=name, type=spec.get("type", "ai"),
            engine=None if spec.get("type") == "human"
            else spec.get("engine", "muse-code"),
            profile=spec.get("profile", f"profiles/{name}.yaml"),
            boundary_tools=[], skills_dir=f"skills/{name}",
            skills=(["decidir", "aprobar", "responder", "hacer"]
                    if name == "owner" else []),
            inputs=[], outputs=[], enabled=enabled,
            disabled_reason=None if enabled else spec.get("reason", "off"),
            decided_by_ticket=(None if enabled
                               else spec.get("ticket", "TASK-2026-00209")))
    return Catalog(version=1, entries=entries, agents=agents,
                   staff=StaffRoster())


def tiny_profile(name: str = "operations") -> RoleProfile:
    return RoleProfile(
        name=name, agent=name, engine="muse-code",
        card={"who": f"{name} test agent.",
              "delivers": ["test output"], "never": ["change states"]},
        rules="AGENTS.md", skills=["create-skill"],
        output_schema="openceo-core/objectives/agent_result.schema.json")


def write_skill(root: Path, role: str, name: str,
                workflow: str | None = None) -> Path:
    skill_dir = root / "skills" / role / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test skill {name}.\n"
        "status: activa\n"
        "ticket: TASK-2026-00205\n"
        "date: 2026-10-12\n"
        "---\n\n# Test\n", encoding="utf-8")
    if workflow is not None:
        (skill_dir / "workflow.yaml").write_text(workflow, encoding="utf-8")
    return skill_dir


ECHO_SKILL_WORKFLOW = """\
workflow: operations.probe_exact
name: exact skill probe
agent: operations
steps:
  - id: echo_it
    tool: core.echo
    params:
      text: "{{task.title}}"
"""


def scripted_executor(result: ExecResult,
                      calls: list[dict[str, Any]] | None = None):
    recorded = calls if calls is not None else []

    class _Scripted:
        async def run(self, role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            recorded.append({"role": role, "prompt": prompt,
                             "workdir": workdir, "timeout_s": timeout_s})
            return result

        async def cleanup(self, role: str) -> None:
            del role

    def factory(role: str, spec: dict[str, Any],
                badge_provider: Any) -> _Scripted:
        recorded.append({"factory_role": role, "spec": dict(spec),
                         "has_badge_provider": badge_provider is not None})
        return _Scripted()

    return factory, recorded


def hecho_stdout(motivo: str = "listo") -> str:
    return ("did the work\n```agent-result\n"
            f'{{"resultado": "hecho", "motivo": "{motivo}"}}\n```\n')


def verdict_stdout(resultado: str, motivo: str) -> str:
    return ("note\n```agent-result\n"
            f'{{"resultado": "{resultado}", "motivo": "{motivo}"}}\n```\n')


class _StubWorkflowStore:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    async def latest_workflow_run_for_task(
            self, task_id: str) -> dict[str, Any] | None:
        del task_id
        return None

    async def create_workflow_run(
            self, run_doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(run_doc)
        self.runs[stored["run_id"]] = stored
        return dict(stored)

    async def patch_workflow_run(
            self, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.runs[run_id].update(updates)
        return dict(self.runs[run_id])

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        del task_id
        return None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        del task_id, updates
        return None


class _StubHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict) -> None:
        del payload
        self.events.append(event)


def real_workflow_service() -> WorkflowService:
    settings = Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                        DEFAULT_PROJECT="jardal-openceo")
    return WorkflowService(settings=settings, repo=_StubWorkflowStore(),  # type: ignore[arg-type]
                           realtime_hub=_StubHub())  # type: ignore[arg-type]


def generic_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-205",
        "ticket_number": 244,
        "project": "jardal-openceo",
        "title": "Probe the generic agent",
        "description": "Do the thing.",
        "source": "api",
        "params": {},
    }
    task.update(overrides)
    return task


class GenericGateTest(unittest.TestCase):
    def test_enabled_unwired_ai_agent_proceeds(self) -> None:
        catalog = tiny_catalog({"operations": {"enabled": True}})
        decision = resolve_agent_gate("operations", {}, None, catalog)
        self.assertTrue(decision.proceed)
        self.assertEqual("operations", decision.hint)

    def test_planner_target_routes_by_name(self) -> None:
        catalog = tiny_catalog({"operations": {"enabled": True}})
        decision = resolve_agent_gate(
            None, {"target_agent": "operations"}, None, catalog)
        self.assertTrue(decision.proceed)
        self.assertEqual("operations", decision.hint)

    def test_disabled_agent_still_waits(self) -> None:
        catalog = tiny_catalog(
            {"operations": {"enabled": False, "reason": "no skill yet"}})
        decision = resolve_agent_gate("operations", {}, None, catalog)
        self.assertFalse(decision.proceed)
        self.assertIn("disabled", decision.reason)

    def test_real_operations_proceeds_with_its_skill(self) -> None:
        decision = resolve_agent_gate(
            "operations", {}, None, load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("operations", decision.hint)

    def test_human_agent_never_runs(self) -> None:
        catalog = tiny_catalog({"owner": {"enabled": True, "type": "human"}})
        decision = resolve_agent_gate("owner", {}, None, catalog)
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)

    def test_enabled_agent_without_profile_waits(self) -> None:
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "profile": None}})
        decision = resolve_agent_gate("operations", {}, None, catalog)
        self.assertFalse(decision.proceed)
        self.assertIn("no runner", decision.reason)

    def test_non_muse_engine_waits_for_translator(self) -> None:
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "engine": "opencode"}})
        decision = resolve_agent_gate("operations", {}, None, catalog)
        self.assertFalse(decision.proceed)
        self.assertIn("opencode", decision.reason)

    def test_wired_names_keep_legacy_routing(self) -> None:
        # TASK-2026-00220 moved secretary and TASK-2026-00221 finance to
        # the generic node (their gate assertions live in their files);
        # TASK-2026-00209 removed the pm node. Only dev stays wired.
        catalog = load_catalog()
        for name in ("dev",):
            with self.subTest(agent=name):
                decision = resolve_agent_gate(name, {}, None, catalog)
                self.assertTrue(decision.proceed)
                self.assertEqual(name, decision.hint)


class GenericRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo")
        self.router = CEORouter(settings)

    def test_hinted_operations_selects_operations(self) -> None:
        selected = self.router.resolve_explicit("operations", None)
        self.assertEqual(AgentName.OPERATIONS, selected)

    def test_after_route_maps_operations_to_generic(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "operations"}))

    def test_after_route_sends_rest_to_unsupported(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        for name in ("marketing", "sales", "media", "ceo", "unknown"):
            with self.subTest(agent=name):
                self.assertEqual(
                    "unsupported",
                    graph._after_ceo_route({"selected_agent": name}))


class FakeBadgeProvider:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def mint(self, role: str) -> str:
        del role
        self._calls.append("mint")
        return "badge-test"

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        del badge
        return {}

    async def revoke(self, badge: str) -> None:
        del badge
        self._calls.append("revoke")


def wiring_with(tmp: Path, catalog: Catalog, factory: Any,
                **overrides: Any) -> GenericAgentWiring:
    badge_calls: list[str] = []

    def _badge_factory(agent: str, ticket: str,
                       project: str) -> FakeBadgeProvider:
        del agent, ticket, project
        return FakeBadgeProvider(badge_calls)

    params: dict[str, Any] = {
        "settings": Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                             DEFAULT_PROJECT="jardal-openceo"),
        "repo": MagicMock(db=None),
        "workflow_service": real_workflow_service(),
        "executor_factory": factory,
        "badge_provider_factory": _badge_factory,
        "repo_root": tmp,
        "work_base_dir": tmp / "agent-work",
        "catalog": catalog,
    }
    params.update(overrides)
    return GenericAgentWiring(**params)


class GenericAgentRunTest(unittest.TestCase):
    def _world(self, stdout: str, returncode: int = 0,
               params: dict[str, Any] | None = None,
               usage: dict[str, Any] | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=returncode, elapsed_s=1.0,
                       output_tail=stdout, usage=usage))
        wiring = wiring_with(root, catalog, factory)
        return root, wiring, recorded, generic_task(
            params=dict(params or {}))

    def test_hecho_completes_with_verdict_and_comment(self) -> None:
        _, wiring, recorded, task = self._world(hecho_stdout("repo ready"))
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("hecho", verdict.resultado.value)
        self.assertIn("repo ready", verdict.motivo)
        self.assertIn("hecho", out["result_comment"])
        self.assertIn("repo ready", out["result_comment"])
        self.assertEqual("operations", out["selected_agent"])
        self.assertTrue(any("generic_agent" in str(step.get("step"))
                            for step in out["steps"]))

    def test_run_uses_profile_spec_schema_and_badge(self) -> None:
        _, wiring, recorded, task = self._world(hecho_stdout())
        asyncio.run(run_generic_agent("operations", task, wiring))
        built = [call for call in recorded if "factory_role" in call]
        self.assertEqual(1, len(built))
        spec = built[0]["spec"]
        self.assertEqual("operations", built[0]["factory_role"])
        self.assertEqual("operations", spec["role_profile"])
        self.assertIn("--output-schema", spec["headless_args"])
        schema = Path(spec["headless_args"][
            spec["headless_args"].index("--output-schema") + 1])
        self.assertTrue(schema.is_absolute())
        self.assertTrue(schema.exists())
        self.assertTrue(built[0]["has_badge_provider"])
        runs = [call for call in recorded if "prompt" in call]
        self.assertEqual(1, len(runs))
        self.assertEqual(GENERIC_AGENT_TIMEOUT_S, runs[0]["timeout_s"])
        self.assertIn("Probe the generic agent", runs[0]["prompt"])
        self.assertIn("agent-result", runs[0]["prompt"])

    def test_bloqueado_fails_and_keeps_verdict(self) -> None:
        _, wiring, _, task = self._world(
            verdict_stdout("bloqueado", "ERPNext down"))
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertIn("ERPNext down", out["error"])
        self.assertIn("bloqueado", out["result_comment"])

    def test_no_aplica_fails_and_keeps_verdict(self) -> None:
        _, wiring, _, task = self._world(
            verdict_stdout("no_aplica", "dev owns repos"))
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("no_aplica", verdict.resultado.value)
        self.assertIn("no_aplica", out["result_comment"])

    def test_missing_verdict_is_legacy_never_a_failure(self) -> None:
        _, wiring, _, task = self._world("all done, no verdict block")
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        self.assertNotIn("agent_result", out["params"])
        self.assertIn("all done", out["result_comment"])

    def test_crash_invalidates_hecho_claim(self) -> None:
        # Review 00006/agyd-H1: a stored hecho on a FAILED run skips
        # _trigger_objective_return (only bloqueado/no_aplica return),
        # so the crash rewrites the claim instead of preserving it.
        _, wiring, _, task = self._world(hecho_stdout(), returncode=1)
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertIn("returncode 1", verdict.motivo)
        self.assertIn("hecho", verdict.motivo)
        self.assertIn("returncode 1", out["error"])
        self.assertIn("bloqueado", out["result_comment"])

    def test_crash_keeps_bloqueado_verdict(self) -> None:
        _, wiring, _, task = self._world(
            verdict_stdout("bloqueado", "ERPNext down"), returncode=1)
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertEqual("ERPNext down", verdict.motivo)

    def test_truncated_output_fails_closed_without_verdict(self) -> None:
        # Review 00006/mimo-W1: output_tail keeps the head, so a
        # truncated run lost its trailing verdict block (if any).
        _, wiring, _, task = self._world(
            "long log without any verdict block",
            usage={"stdout_chars": 50000, "stderr_chars": 0,
                   "truncated": True})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertIn("truncated", verdict.motivo)
        self.assertIn("bloqueado", out["result_comment"])

    def test_truncated_output_distrusts_parsed_claim(self) -> None:
        _, wiring, _, task = self._world(
            hecho_stdout("all green"),
            usage={"stdout_chars": 60000, "stderr_chars": 0,
                   "truncated": True})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertIn("truncated", verdict.motivo)
        self.assertIn("all green", verdict.motivo)

    def test_crash_without_verdict_fails(self) -> None:
        _, wiring, _, task = self._world("traceback...", returncode=124)
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        self.assertNotIn("agent_result", out["params"])
        self.assertIn("124", out["error"])

    def test_unknown_suggested_skill_waits(self) -> None:
        _, wiring, recorded, task = self._world(
            hecho_stdout(), params={"skill": "missing-skill"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])
        self.assertIn("missing-skill", out["waiting_reason"])
        self.assertFalse([call for call in recorded if "prompt" in call])

    def test_unbound_identity_waits_before_running(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=0.0, output_tail="x"))
        wiring = wiring_with(Path(tmp.name), catalog, factory, repo=None,
                                badge_provider_factory=None)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(), wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])
        self.assertFalse([call for call in recorded if "prompt" in call])

    def test_missing_profile_waits(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = tiny_catalog(
            {"operations": {"enabled": True,
                            "profile": "profiles/nope.yaml"}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=0.0, output_tail="x"))
        wiring = wiring_with(Path(tmp.name), catalog, factory)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(), wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])
        self.assertFalse([call for call in recorded if "prompt" in call])

    def test_suggested_skill_names_it_in_prompt(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_skill(root, "operations", "triage")
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=1.0,
                       output_tail=hecho_stdout()))
        wiring = wiring_with(root, catalog, factory)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(params={"skill": "triage"}), wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        runs = [call for call in recorded if "prompt" in call]
        self.assertIn("triage", runs[0]["prompt"])


class ExactSkillTest(unittest.TestCase):
    def _skill_world(self, workflow: str, params: dict[str, Any]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_skill(root, "operations", "probe", workflow)

        def _forbidden_factory(role: str, spec: dict[str, Any],
                               provider: Any) -> Any:
            raise AssertionError("exact skills never invoke the LLM")

        catalog = tiny_catalog({"operations": {"enabled": True}})
        wiring = wiring_with(root, catalog, _forbidden_factory, repo=None)
        return wiring, generic_task(params=params)

    def test_exact_skill_runs_yaml_without_llm(self) -> None:
        wiring, task = self._skill_world(
            ECHO_SKILL_WORKFLOW, {"skill": "probe"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("hecho", verdict.resultado.value)
        self.assertIn("operations.probe_exact", out["result_comment"])
        self.assertTrue(any("probe_exact" in str(step.get("detail"))
                            for step in out["steps"]))

    def test_exact_skill_failure_maps_to_bloqueado(self) -> None:
        bad_step = ECHO_SKILL_WORKFLOW.replace(
            "    tool: core.echo\n", "")
        wiring, task = self._skill_world(bad_step, {"skill": "probe"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("bloqueado", verdict.resultado.value)
        self.assertIn("operations.probe_exact", out["result_comment"])

    def test_unknown_tool_wait_propagates(self) -> None:
        bad_step = ECHO_SKILL_WORKFLOW.replace(
            "tool: core.echo", "tool: core.no_such_tool")
        wiring, task = self._skill_world(bad_step, {"skill": "probe"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertEqual("workflow_blocked_tool", out["waiting_reason"])
        self.assertIn("core.no_such_tool", out["error"])
        self.assertNotIn("agent_result", out["params"])

    def test_invalid_skill_workflow_waits_for_skill_fix(self) -> None:
        wiring, task = self._skill_world(
            "workflow: [not, a, mapping]\n", {"skill": "probe"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])

    def test_skill_without_workflow_takes_llm_path(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_skill(root, "operations", "advice")
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=1.0,
                       output_tail=hecho_stdout("advised")))
        wiring = wiring_with(root, catalog, factory)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(params={"skill": "advice"}), wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        self.assertTrue([call for call in recorded if "prompt" in call])

    def test_resolve_exact_skill_path(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        skill_dir = write_skill(root, "operations", "probe",
                                ECHO_SKILL_WORKFLOW)
        found = resolve_exact_skill_path(
            "skills/operations/probe", repo_root=root)
        self.assertEqual(skill_dir / "workflow.yaml", found)
        plain = write_skill(root, "operations", "advice")
        self.assertIsNone(resolve_exact_skill_path(
            str(plain.relative_to(root)), repo_root=root))
        self.assertEqual(skill_dir / "workflow.yaml",
                         resolve_exact_skill_path(
                             str(skill_dir), repo_root=root))


class SpecAndPromptTest(unittest.TestCase):
    def test_muse_role_spec_pins_engine_and_schema(self) -> None:
        spec = muse_role_spec(tiny_profile("operations"), "operations")
        self.assertEqual(
            ["musej", "exec", "--model",
             "muse-spark-1.3-contributor"], spec["command"])
        self.assertEqual("argv", spec["prompt_via"])
        self.assertEqual("operations", spec["role_profile"])
        self.assertIn("--output-schema", spec["headless_args"])
        schema = Path(spec["headless_args"][
            spec["headless_args"].index("--output-schema") + 1])
        self.assertEqual(
            REPO / "openceo-core" / "objectives" / "agent_result.schema.json",
            schema)
        self.assertTrue(schema.exists())

    def test_missing_schema_refuses_spec(self) -> None:
        profile = tiny_profile("operations")
        profile.output_schema = "profiles/no-schema.json"
        with self.assertRaises(CatalogError):
            muse_role_spec(profile, "operations")

    def test_build_agent_prompt_carries_ticket_and_verdict(self) -> None:
        prompt = build_agent_prompt(
            tiny_profile("operations"), generic_task(), skill="triage")
        self.assertIn("Probe the generic agent", prompt)
        self.assertIn("Do the thing.", prompt)
        self.assertIn("triage", prompt)
        self.assertIn("agent-result", prompt)


class LegacyNodesUnchangedTest(unittest.TestCase):
    def _legacy_graph(self, dev_status: str = TaskStatus.COMPLETED.value):
        from types import SimpleNamespace
        settings = Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo")
        dev_out = SimpleNamespace(
            status=TaskStatus(dev_status), steps=[{"step": "dev_did"}],
            coding_tool=None, coding_session_id=None,
            coding_session_title=None, last_prompt_sent=None,
            last_response_summary="dev done", waiting_reason=None,
            resume_at=None, error=None, modified_files=[], params={})
        dev_session = MagicMock()
        dev_session.run = AsyncMock(return_value=dev_out)
        loader = MagicMock()
        loader.load_project.return_value = {}
        return OpenCEOGraph(
            router=CEORouter(settings), config_loader=loader,
            dev_session=dev_session), dev_session

    def test_dev_hint_still_runs_legacy_dev_node(self) -> None:
        graph, dev_session = self._legacy_graph()
        out = asyncio.run(graph.execute(
            generic_task(), agent_hint="dev"))
        dev_session.run.assert_called_once()
        self.assertEqual("dev", out["selected_agent"])
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])

    def test_enabled_operations_without_wiring_waits_through_execute(
            self) -> None:
        graph, dev_session = self._legacy_graph()
        out = asyncio.run(graph.execute(
            generic_task(), agent_hint="operations"))
        dev_session.run.assert_not_called()
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])

    def test_generic_node_without_wiring_waits(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        out = asyncio.run(graph._generic_node(
            {"task_id": "t", "title": "T", "description": "D",
             "selected_agent": "operations", "params": {},
             "steps": []}))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])

    def test_generic_node_runs_enabled_agent(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, _ = scripted_executor(
            ExecResult(returncode=0, elapsed_s=1.0,
                       output_tail=hecho_stdout("node ran")))
        wiring = wiring_with(Path(tmp.name), catalog, factory)
        graph = OpenCEOGraph(None, None, None,
                             generic_wiring=wiring)
        out = asyncio.run(graph._generic_node(
            {"task_id": "task-205", "ticket_number": 244,
             "project": "jardal-openceo", "title": "T", "description": "D",
             "selected_agent": "operations", "params": {}, "steps": []}))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        self.assertEqual("operations", out["selected_agent"])
        self.assertIn("node ran", out["result_comment"])


@dataclass
class _RecordingErp:
    is_enabled: bool = True
    comments: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.comments = []

    async def add_comment(self, name: str, text: str) -> None:
        assert self.comments is not None
        self.comments.append((name, text))


class _DictRepo:
    def __init__(self, tasks: dict[str, dict[str, Any]]) -> None:
        self.tasks = {key: dict(row) for key, row in tasks.items()}

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)

    async def list_tasks_blocked_by(self, task_id: str) -> list[dict]:
        del task_id
        return []


class _FakeGraph:
    def __init__(self, out: dict[str, Any]) -> None:
        self._out = out

    async def execute(self, task: dict[str, Any],
                      agent_hint: Any = None) -> dict[str, Any]:
        del task, agent_hint
        return dict(self._out)


class _FakeHub:
    async def broadcast(self, event: str, payload: dict) -> None:
        del event, payload


class ResultCommentTest(unittest.TestCase):
    def _service(self, graph_out: dict[str, Any], hint: str = "operations"):
        task = generic_task(status=TaskStatus.QUEUED.value)
        # Explicit agent: these tests cover result comments, and since
        # TASK-2026-00204 an undecided ticket on an unwired service
        # fails fast instead of running.
        task["agent_hint"] = hint
        task["params"] = {"_mirrors": {"erpnext": {"name": "TASK-2026-09999"}}}
        repo = _DictRepo({"task-205": task})
        service = TaskService(
            settings=Settings(), repo=repo,  # type: ignore[arg-type]
            graph=_FakeGraph(graph_out),  # type: ignore[arg-type]
            realtime_hub=_FakeHub(),  # type: ignore[arg-type]
            notion=None, rate_monitor=None)
        erp = _RecordingErp()
        service._erpnext = erp  # noqa: SLF001
        return service, erp

    def test_run_task_posts_result_comment(self) -> None:
        service, erp = self._service({
            "selected_agent": "operations",
            "status": TaskStatus.COMPLETED.value,
            "steps": [],
            "params": {"_mirrors": {"erpnext": {
                "name": "TASK-2026-09999"}}},
            "result_comment": "operations: hecho — repo ready",
        })
        updated = asyncio.run(service.run_task("task-205"))
        assert updated is not None
        self.assertEqual(TaskStatus.COMPLETED.value, updated["status"])
        assert erp.comments is not None
        self.assertEqual(
            [("TASK-2026-09999", "operations: hecho — repo ready")],
            erp.comments)

    def test_run_task_skips_blank_result_comment(self) -> None:
        service, erp = self._service({
            "selected_agent": "dev",
            "status": TaskStatus.COMPLETED.value,
            "steps": [],
            "params": {"_mirrors": {"erpnext": {
                "name": "TASK-2026-09999"}}},
        })
        asyncio.run(service.run_task("task-205"))
        assert erp.comments is not None
        self.assertEqual([], erp.comments)

    def test_run_task_comments_blocked_tool_wait(self) -> None:
        # Review 00006/mimo-I1: a blocked-tool wait needs a human, so
        # it gets the one-shot ERP comment like routing/config waits.
        service, erp = self._service({
            "selected_agent": "operations",
            "status": TaskStatus.WAITING.value,
            "waiting_reason": "workflow_blocked_tool",
            "error": "Tool not registered: core.no_such_tool",
            "steps": [],
            "params": {"_mirrors": {"erpnext": {
                "name": "TASK-2026-09999"}}},
        })

        async def _run():
            updated = await service.run_task("task-205")
            for _ in range(200):
                if erp.comments:
                    break
                await asyncio.sleep(0.01)
            return updated

        updated = asyncio.run(_run())
        assert updated is not None
        self.assertEqual(TaskStatus.WAITING.value, updated["status"])
        assert erp.comments is not None
        self.assertEqual(1, len(erp.comments))
        self.assertEqual("TASK-2026-09999", erp.comments[0][0])
        self.assertIn("core.no_such_tool", erp.comments[0][1])


if __name__ == "__main__":
    unittest.main()

