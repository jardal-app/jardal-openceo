"""Research as a catalog agent: ticket bridge to the research service (TASK-2026-00222).

Research leaves the disabled list: the catalog card is enabled with its
ticket + research box, the investigar recipe guides the run, and two
boundary tools (submit/get) reach the in-process research service under
a per-ticket consumer the server mints. The agent only holds its ticket
badge; the consumer token never leaves the server. No tokens.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    load_catalog,
    load_role_profile,
    load_role_skills,
)
from config_loader import _planner_agents_from_catalog  # noqa: E402
from graph import (  # noqa: E402
    OpenCEOGraph,
    resolve_agent_gate,
)
from mcp_identity import (  # noqa: E402
    McpDenied,
    TicketGrant,
    TokenIssuer,
    TokenKind,
    box_for_agent,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _agents_section, _planner_skills  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)
from orchestration.generic_agent import (  # noqa: E402
    GenericAgentWiring,
    run_generic_agent,
)
from orchestration.live import ExecResult  # noqa: E402
from research.consumers import ConsumerRegistry  # noqa: E402
from research.ticket_bridge import TicketResearchBridge  # noqa: E402
from settings import Settings  # noqa: E402
from state import AgentName, TaskStatus  # noqa: E402
from workflow_service import (  # noqa: E402
    MissingWorkflowToolError,
    WorkflowToolRegistry,
)

SKILL_DIR = REPO / "skills" / "research" / "investigar"

RESEARCH_BOX = frozenset({
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_add_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_research_submit",
    "openceo_research_get",
})

TICKET = "TASK-2026-00222"
PROJECT = "jardal-openceo"
MIRROR = {"_mirrors": {"erpnext": {"name": TICKET}}}


def _validate_skill_live(target: str, skill: str | None,
                         skills: dict[str, list[str]] | None) -> list[str]:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Research the vendor options",
        "target_agent": target, "work_type": "research", "goal": "g",
        "why_now": "now", "acceptance_criteria": ["brief"],
        "expected_artifacts": ["grounded brief"],
    }
    if skill is not None:
        fields["skill"] = skill
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[PlannedTicket(**fields)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents={target: {"enabled": True}}, open_titles=[],
            task_exists=_exists, skills=skills)

    return asyncio.run(_run())


class ResearchCardTest(unittest.TestCase):
    def test_research_enabled_with_box_and_skill(self) -> None:
        card = load_catalog().agents["research"]
        self.assertTrue(card.enabled)
        self.assertIsNone(card.disabled_reason)
        self.assertIsNone(card.decided_by_ticket)
        self.assertEqual(RESEARCH_BOX, set(card.boundary_tools))
        self.assertEqual(["investigar"], card.skills)

    def test_profile_loads_card_skill(self) -> None:
        profile = load_role_profile("research")
        self.assertEqual({"create-skill", "investigar"},
                         set(profile.skills))

    def test_index_summary_dropped_disabled(self) -> None:
        import yaml
        entries = yaml.safe_load(
            (REPO / "catalog" / "index.yaml").read_text(encoding="utf-8"))
        summary = next(item["summary"] for item in entries["agents"]
                       if item["name"] == "research")
        self.assertNotIn("Disabled", summary)

    def test_enum_and_alias_resolve(self) -> None:
        from router import resolve_explicit_name  # noqa: E402
        self.assertEqual(AgentName.RESEARCH, AgentName("research"))
        self.assertEqual(AgentName.RESEARCH,
                         resolve_explicit_name("research", None))
        self.assertEqual(AgentName.RESEARCH,
                         resolve_explicit_name("Research", None))


class SkillOnDiskTest(unittest.TestCase):
    def test_skill_parses_activa(self) -> None:
        found = load_role_skills("research", "skills/research")
        self.assertEqual(["investigar"],
                         [skill.name for skill in found])
        skill = found[0]
        self.assertEqual("activa", skill.status)
        self.assertEqual("TASK-2026-00222", skill.ticket)

    def test_exposure_symlink_resolves_to_source(self) -> None:
        link = REPO / ".codex" / "skills" / "investigar"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(SKILL_DIR),
                         os.path.realpath(link))

    def test_recipe_uses_the_bridge_and_attaches(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertIn("openceo_research_submit", flat)
        self.assertIn("openceo_research_get", flat)
        self.assertIn("openceo_attach_ticket_file", flat)
        self.assertIn("hecho", flat)
        self.assertIn("bloqueado", flat)

    def test_recipe_stays_small(self) -> None:
        size = (SKILL_DIR / "SKILL.md").stat().st_size
        self.assertLess(size, 4096, "the role prompt budget is shared")


class PlannerResearchLiveTest(unittest.TestCase):
    def test_planner_sees_research_skill(self) -> None:
        block, mapping = _planner_skills()
        self.assertIn("investigar", block)
        self.assertEqual(["investigar"], mapping["research"])

    def test_suggested_skill_validated_live(self) -> None:
        _block, mapping = _planner_skills()
        live = {name: list(names) for name, names in mapping.items()}
        self.assertEqual(
            [], _validate_skill_live("research", "investigar", live))
        errors = _validate_skill_live("research", "frobnicate", live)
        self.assertTrue(any("frobnicate" in line for line in errors),
                        errors)

    def test_registry_enables_research(self) -> None:
        registry = _planner_agents_from_catalog(load_catalog())
        self.assertEqual({"enabled": True}, registry["research"])

    def test_prompt_lists_research_enabled(self) -> None:
        registry = _planner_agents_from_catalog(load_catalog())
        section = _agents_section(registry)
        self.assertIn("- research: enabled", section)
        self.assertNotIn("research: DISABLED", section)


class GateLiveTest(unittest.TestCase):
    def test_hint_proceeds(self) -> None:
        decision = resolve_agent_gate("research", {}, None,
                                      load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("research", decision.hint)

    def test_planner_target_proceeds(self) -> None:
        decision = resolve_agent_gate(
            None, {"target_agent": "research"}, None, load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("research", decision.hint)


class ResearchBoxTest(unittest.TestCase):
    def _grant(self, tools: frozenset[str]) -> TicketGrant:
        return TicketGrant(kind=TokenKind.AGENT, agent="research",
                           ticket=TICKET, project=PROJECT, tools=tools)

    def test_box_comes_from_the_live_card(self) -> None:
        card = load_catalog().agents["research"]
        self.assertEqual(RESEARCH_BOX, box_for_agent(card))

    def test_submit_and_get_bind_own_ticket_and_project(self) -> None:
        grant = self._grant(RESEARCH_BOX)
        grant.check("openceo_research_submit", {
            "purpose": "p", "question": "q",
            "ticket": TICKET, "project": PROJECT})
        grant.check("openceo_research_get", {
            "research_id": "r1", "ticket": TICKET,
            "project": PROJECT})
        with self.assertRaises(McpDenied):
            grant.check("openceo_research_submit", {
                "purpose": "p", "question": "q",
                "ticket": "TASK-2026-00001", "project": PROJECT})
        with self.assertRaises(McpDenied):
            grant.check("openceo_research_get", {
                "research_id": "r1", "ticket": TICKET,
                "project": "other-project"})

    def test_missing_tools_still_refused(self) -> None:
        grant = self._grant(frozenset({"openceo_get_ticket"}))
        with self.assertRaises(McpDenied):
            grant.check("openceo_research_submit", {
                "purpose": "p", "question": "q",
                "ticket": TICKET, "project": PROJECT})

    def test_research_badge_mints(self) -> None:
        class _Store:
            async def save(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _mint() -> str:
            issuer = TokenIssuer(_Store(), load_catalog().agents)  # type: ignore[arg-type]
            return await issuer.issue_agent("research", TICKET, PROJECT)

        badge = asyncio.run(_mint())
        self.assertTrue(badge)


class _FakeResearchService:
    """Simulated service: stores docs, enforces the project flag."""

    def __init__(self, *, enabled: bool = True) -> None:
        from research.contracts import ResearchIdempotencyConflict  # noqa: E402
        from research.policies import ResearchPolicyError  # noqa: E402
        self._conflict = ResearchIdempotencyConflict
        self._policy_error = ResearchPolicyError
        self.enabled = enabled
        self.jobs: dict[str, dict[str, Any]] = {}
        self.principals: list[Any] = []
        self._seq = 0

    def available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "research deshabilitado"
        return True, "ok"

    async def submit(self, request: Any, *, principal: Any,
                     project_cfg: dict[str, Any],
                     effective_limits: Any = None) -> dict[str, Any]:
        if request.idempotency_key == "clash":
            raise self._conflict("same key, other request")
        if not project_cfg.get("research", {}).get("enabled", False):
            raise self._policy_error(
                "research deshabilitado para este proyecto")
        self._seq += 1
        self.principals.append(principal)
        doc = {
            "research_id": f"r{self._seq}",
            "project_key": request.project_key,
            "owner_id": principal.user_id,
            "purpose": request.purpose,
            "mode": request.mode.value,
            "question": request.question,
            "status": "queued",
            "_created": True,
        }
        self.jobs[doc["research_id"]] = doc
        return dict(doc)

    async def status(self, research_id: str) -> dict[str, Any] | None:
        doc = self.jobs.get(research_id)
        return dict(doc) if doc is not None else None

    async def result_view(self, document: dict[str, Any]) -> dict[str, Any]:
        return document


def _bridge(service: Any = None, clock: Any = None,
            project_cfg: Any = None) -> tuple[TicketResearchBridge,
                                              ConsumerRegistry, Any,
                                              list[str]]:
    consumers = ConsumerRegistry() if clock is None else ConsumerRegistry(
        clock=clock)
    live = _FakeResearchService() if service is None else service
    woken: list[str] = []
    if project_cfg is None:
        project_cfg = lambda project: {"research": {"enabled": True}}
    bridge = TicketResearchBridge(
        consumers=consumers, get_service=lambda: live,
        project_cfg_for=project_cfg,
        on_admitted=woken.append)
    return bridge, consumers, live, woken


class TicketBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_submit_admits_and_wakes(self) -> None:
        bridge, _consumers, live, woken = _bridge()
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="decide X", question="which?")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["research_id"], "r1")
        self.assertEqual(out["status"], "queued")
        self.assertTrue(out["created"])
        self.assertEqual(out["poll_with"], "openceo_research_get")
        self.assertEqual(woken, ["r1"])
        self.assertEqual(live.principals[0].user_id, f"ticket:{TICKET}")
        self.assertIn("run_research",
                      live.principals[0].permissions)

    async def test_one_consumer_per_ticket(self) -> None:
        bridge, consumers, _live, _woken = _bridge()
        await bridge.submit(ticket=TICKET, project=PROJECT,
                            purpose="a", question="qa")
        await bridge.submit(ticket=TICKET, project=PROJECT,
                            purpose="b", question="qb")
        self.assertEqual(consumers.count(), 1)
        await bridge.submit(ticket="TASK-2026-00001", project=PROJECT,
                            purpose="c", question="qc")
        self.assertEqual(consumers.count(), 2)

    async def test_get_returns_own_job(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        admitted = await bridge.submit(
            ticket=TICKET, project=PROJECT, purpose="decide X",
            question="which?")
        out = await bridge.get(ticket=TICKET, project=PROJECT,
                               research_id=admitted["research_id"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["job"]["research_id"], "r1")
        self.assertEqual(out["job"]["question"], "which?")

    async def test_cross_ticket_get_is_not_found(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        admitted = await bridge.submit(
            ticket=TICKET, project=PROJECT, purpose="decide X",
            question="which?")
        out = await bridge.get(ticket="TASK-2026-00001", project=PROJECT,
                               research_id=admitted["research_id"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "not_found")

    async def test_get_job_from_other_project_is_not_found(self) -> None:
        # Second branch of get: same owner, job in a project the grant
        # does not cover (review 00017/H6).
        bridge, _consumers, live, _woken = _bridge()
        live.jobs["r9"] = {
            "research_id": "r9", "project_key": "other-project",
            "owner_id": f"ticket:{TICKET}", "status": "queued",
        }
        out = await bridge.get(ticket=TICKET, project=PROJECT,
                               research_id="r9")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "not_found")

    async def test_second_project_on_same_ticket_is_forbidden(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        first = await bridge.submit(ticket=TICKET, project=PROJECT,
                                    purpose="a", question="qa")
        self.assertTrue(first["ok"], first)
        out = await bridge.submit(ticket=TICKET, project="other-project",
                                  purpose="b", question="qb")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "forbidden")

    async def test_blank_question_is_validation_error(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="decide X", question="  ")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "validation_error")

    async def test_missing_service_is_unavailable(self) -> None:
        bridge = TicketResearchBridge(
            consumers=ConsumerRegistry(), get_service=lambda: None,
            project_cfg_for=lambda project: {"research": {"enabled": True}})
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="a", question="qa")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "unavailable")

    async def test_disabled_service_is_unavailable(self) -> None:
        bridge, _consumers, _live, _woken = _bridge(
            service=_FakeResearchService(enabled=False))
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="a", question="qa")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "unavailable")

    async def test_project_without_research_is_forbidden(self) -> None:
        bridge, _consumers, _live, _woken = _bridge(
            project_cfg=lambda project: {"research": {"enabled": False}})
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="a", question="qa")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "forbidden")

    async def test_idempotency_conflict_maps(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        out = await bridge.submit(ticket=TICKET, project=PROJECT,
                                  purpose="a", question="qa",
                                  idempotency_key="clash")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "conflict")

    async def test_expired_credential_remints(self) -> None:
        from datetime import datetime, timedelta, timezone
        now = [datetime.now(timezone.utc)]
        bridge, consumers, _live, _woken = _bridge(
            clock=lambda: now[0])
        first = await bridge.submit(ticket=TICKET, project=PROJECT,
                                    purpose="a", question="qa")
        self.assertTrue(first["ok"], first)
        now[0] = now[0] + timedelta(days=60)
        second = await bridge.submit(ticket=TICKET, project=PROJECT,
                                     purpose="b", question="qb")
        self.assertTrue(second["ok"], second)
        # The re-mint purges expired credentials first (review 00017/H2).
        self.assertEqual(consumers.count(), 1)

    async def test_agent_never_sees_the_consumer_token(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        admitted = await bridge.submit(
            ticket=TICKET, project=PROJECT, purpose="decide X",
            question="which?")
        seen = await bridge.get(ticket=TICKET, project=PROJECT,
                                research_id=admitted["research_id"])
        token = bridge._tokens[TICKET]
        self.assertTrue(token)
        self.assertNotIn(token, repr(admitted))
        self.assertNotIn(token, repr(seen))


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


class RegistryBridgeTest(unittest.TestCase):
    def test_bridge_tools_are_conditional(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        self.assertNotIn("research.submit_job", registry.names())
        self.assertNotIn("research.get_job", registry.names())

    def test_set_bridge_registers_documented_tools(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        registry.set_research_bridge(object())
        self.assertIn("research.submit_job", registry.names())
        self.assertIn("research.get_job", registry.names())
        described = registry.describe()
        self.assertTrue(described["research.submit_job"]["doc"].strip())
        self.assertTrue(described["research.get_job"]["doc"].strip())

    def test_call_without_bridge_raises(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        with self.assertRaises(MissingWorkflowToolError):
            asyncio.run(registry.call("research.submit_job", {}, {}))

    def test_submit_call_delegates_params(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        bridge = AsyncMock()
        bridge.submit.return_value = {"ok": True}
        registry.set_research_bridge(bridge)
        out = asyncio.run(registry.call("research.submit_job", {
            "ticket": TICKET, "project": PROJECT, "purpose": "a",
            "question": "qa", "mode": "quick", "seed_urls": ["u"],
            "idempotency_key": "k"}, {}))
        self.assertTrue(out["ok"])
        bridge.submit.assert_awaited_once_with(
            ticket=TICKET, project=PROJECT, purpose="a", question="qa",
            mode="quick", seed_urls=["u"], idempotency_key="k")

    def test_get_call_delegates_params(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        bridge = AsyncMock()
        bridge.get.return_value = {"ok": True}
        registry.set_research_bridge(bridge)
        out = asyncio.run(registry.call("research.get_job", {
            "ticket": TICKET, "project": PROJECT,
            "research_id": "r1"}, {}))
        self.assertTrue(out["ok"])
        bridge.get.assert_awaited_once_with(
            ticket=TICKET, project=PROJECT, research_id="r1")


class MCPDelegationTest(unittest.IsolatedAsyncioTestCase):
    def _server(self, **kwargs: Any) -> Any:
        from fastmcp import FastMCP  # noqa: PLC0415
        from mcp_server import _register_all_tools  # noqa: PLC0415

        class _FakeTaskService:
            async def get_task(self, task_id: str) -> None:
                return None

        class _FakeJardalERP:
            is_enabled = True

        server: Any = FastMCP("research-delegation-test")
        _register_all_tools(server, _settings(), _FakeTaskService(),
                            _FakeJardalERP(), **kwargs)
        return server

    async def test_submit_wrapper_delegates_to_registry(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        calls: list[dict[str, Any]] = []

        async def fake_submit(
            ticket: str = "",
            project: str = "",
            purpose: str = "",
            question: str = "",
            mode: str = "quick",
            seed_urls: Any = None,
            idempotency_key: Any = None,
            context: dict[str, Any] | None = None,
        ) -> dict[str, bool]:
            calls.append({
                "ticket": ticket, "project": project,
                "purpose": purpose, "question": question,
                "mode": mode, "seed_urls": seed_urls,
                "idempotency_key": idempotency_key,
                "context": context,
            })
            return {"ok": True}

        with patch.object(WorkflowToolRegistry, "_research_submit_job",
                          new=staticmethod(fake_submit)):
            server = self._server(research_bridge=object())
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_research_submit",
                    {"ticket": TICKET, "project": PROJECT,
                     "purpose": "a", "question": "qa"})
        self.assertTrue(result.data["ok"])
        self.assertEqual(calls, [{
            "ticket": TICKET, "project": PROJECT, "purpose": "a",
            "question": "qa", "mode": "quick", "seed_urls": None,
            "idempotency_key": None, "context": {},
        }])

    async def test_get_wrapper_delegates_to_registry(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        calls: list[dict[str, Any]] = []

        async def fake_get(
            ticket: str = "",
            project: str = "",
            research_id: str = "",
            context: dict[str, Any] | None = None,
        ) -> dict[str, bool]:
            calls.append({
                "ticket": ticket, "project": project,
                "research_id": research_id, "context": context,
            })
            return {"ok": True}

        with patch.object(WorkflowToolRegistry, "_research_get_job",
                          new=staticmethod(fake_get)):
            server = self._server(research_bridge=object())
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_research_get",
                    {"ticket": TICKET, "project": PROJECT,
                     "research_id": "r1"})
        self.assertTrue(result.data["ok"])
        self.assertEqual(calls, [{
            "ticket": TICKET, "project": PROJECT, "research_id": "r1",
            "context": {},
        }])

    async def test_missing_bridge_maps_to_not_ok(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        server = self._server()
        async with Client(server) as client:
            result = await client.call_tool(
                "openceo_research_submit",
                {"ticket": TICKET, "project": PROJECT,
                 "purpose": "a", "question": "qa"})
        self.assertFalse(result.data["ok"])


def _scripted_factory(recorded: list[dict[str, Any]],
                      stdout: str):
    result = ExecResult(returncode=0, elapsed_s=1.0,
                        output_tail=stdout, usage={})

    class _Scripted:
        async def run(self, role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            recorded.append({"role": role, "prompt": prompt,
                             "workdir": workdir})
            return result

        async def cleanup(self, role: str) -> None:
            recorded.append({"cleanup": role})

    def _factory(role: str, spec: dict[str, Any],
                 provider: Any) -> _Scripted:
        recorded.append({"factory_role": role, "spec": dict(spec)})
        return _Scripted()

    return _factory


def _badge_recorder(calls: list[dict[str, str]]) -> Any:
    class _Provider:
        async def mint(self, role: str) -> str:
            return "badge-scripted"

        def mcp_servers(self, badge: str) -> dict[str, Any]:
            return {}

        async def revoke(self, badge: str) -> None:
            return None

    def _factory(agent: str, ticket: str, project: str) -> _Provider:
        calls.append({"agent": agent, "ticket": ticket,
                      "project": project})
        return _Provider()

    return _factory


_HECHO = ("researched and attached\n```agent-result\n"
          '{"resultado": "hecho", "motivo": "brief attached"}\n```\n')


def _live_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "8d5cc2b8-8f1e-4a2a-9c3e-7f6a5b4c3d22",
        "ticket_number": 9003, "project": PROJECT,
        "title": "Research the vendor options", "description": "spec",
        "source": "test",
        "params": dict(MIRROR),
    }
    task.update(overrides)
    return task


class GenericRunLiveTest(unittest.TestCase):
    def test_research_run_stages_mounts_and_badges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict[str, Any]] = []
            badge_calls: list[dict[str, str]] = []
            wiring = GenericAgentWiring(
                settings=Settings(),
                repo=None,
                executor_factory=_scripted_factory(recorded, _HECHO),
                badge_provider_factory=_badge_recorder(badge_calls),
                work_base_dir=root / "agent-work",
                catalog=load_catalog())
            task = _live_task(params={**MIRROR, "skill": "investigar"})
            out = asyncio.run(run_generic_agent("research", task,
                                                wiring))
            self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
            self.assertIn("hecho", out["result_comment"])
            link = (root / "agent-work"
                    / "8d5cc2b8-8f1e-4a2a-9c3e-7f6a5b4c3d22"
                    / ".codex" / "skills" / "investigar")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(SKILL_DIR),
                             os.path.realpath(link))
            built = [call for call in recorded if "factory_role" in call]
            self.assertEqual(1, len(built))
            spec = built[0]["spec"]
            self.assertEqual([str(REPO)], spec["extra_read_roots"])
            self.assertNotIn("extra_write_roots", spec)
            runs = [call for call in recorded if "prompt" in call]
            self.assertIn("investigar", runs[0]["prompt"])
            self.assertEqual(TICKET, badge_calls[0]["ticket"])


class GraphRoutingTest(unittest.TestCase):
    def test_research_routes_to_the_generic_node(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "research"}))

    def test_decided_research_without_wiring_waits(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415
        loader = MagicMock()
        loader.load_project.return_value = {}
        graph = OpenCEOGraph(MagicMock(), loader, None)
        out = asyncio.run(graph._generic_node({
            "task_id": "task-pedido-1", "title": "t", "description": "d",
            "selected_agent": "research",
            "params": {"target_agent": "research",
                       "decided_by": "planner"}}))
        self.assertEqual(out["status"], TaskStatus.WAITING.value)
        self.assertIn("config:", out["waiting_reason"])


class PolicyTextTest(unittest.TestCase):
    def test_section_10_no_longer_disables_research(self) -> None:
        text = (CORE / "objectives" / "routing_policy.md").read_text(
            encoding="utf-8")
        section = text.split("## 10.")[1].split("## 11.")[0]
        self.assertNotIn("DISABLED", section)
        self.assertNotIn("must not route", section)
        self.assertIn("openceo_research_submit", section)

    def test_section_6_names_the_investigar_skill(self) -> None:
        text = (CORE / "objectives" / "routing_policy.md").read_text(
            encoding="utf-8")
        section = text.split("## 6.")[1].split("## 7.")[0]
        self.assertIn("investigar", section)
