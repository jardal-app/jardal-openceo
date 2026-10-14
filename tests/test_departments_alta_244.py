"""Departments, employee onboarding and specialization (TASK-2026-00244).

Departments group responsibilities for the planner (no permissions, no
directors); a new employee dispatches from its card alone, with no enum
member, manual alias or route edit; the planner sees department +
summary inline and compact card detail on staged demand; skills transfer
by move with their history while each role learns only in its scope.

No network, no CLI, no LLM: tmp catalog trees, in-memory catalogs and
scripted executors.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import yaml  # noqa: E402

from catalog import (  # noqa: E402
    AgentCard,
    Catalog,
    CatalogError,
    IndexEntry,
    StaffRoster,
    agent_detail_text,
    check_skill_names_unique,
    discover_role_skills,
    find_consumer_mismatches,
    load_all_role_skills,
    load_catalog,
    load_role_profile,
    planner_registry_view,
    sync_skill_links,
)
from graph import OpenCEOGraph, resolve_agent_gate  # noqa: E402
from objectives.agent_result import AgentResult  # noqa: E402
from objectives.engine import (  # noqa: E402
    _agents_section,
    _stage_agent_details,
)
from orchestration.generic_agent import run_generic_agent  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from router import CEORouter, resolve_selected_agent  # noqa: E402
from state import AgentName, TaskStatus  # noqa: E402
from test_generic_agent_205 import (  # noqa: E402
    generic_task,
    hecho_stdout,
    scripted_executor,
    wiring_with,
)

DEPARTMENTS = [
    {"name": "engineering",
     "summary": "Builds and maintains company systems."},
    {"name": "finance",
     "summary": "Answers and records dated company money."},
    {"name": "research",
     "summary": "Runs grounded research on demand."},
    {"name": "operations",
     "summary": "Runs operations and coordination."},
]


def _card(name: str, *, department: str | None = None,
          skills: list[str] | None = None, profile: bool = False,
          card_type: str = "ai", enabled: bool = True,
          tools: list[str] | None = None) -> dict[str, Any]:
    card_skills = ["decidir", "aprobar", "responder", "hacer"] \
        if name == "owner" else list(skills or [])
    return {
        "name": name,
        "type": card_type,
        "engine": None if card_type == "human" else "muse-code",
        "profile": f"profiles/{name}.yaml" if profile else None,
        "department": department,
        "boundary_tools": list(tools or []),
        "skills_dir": f"skills/{name}",
        "skills": card_skills,
        "inputs": [f"{name} ticket"],
        "outputs": ["hecho / bloqueado / no_aplica result with reason"],
        "enabled": enabled,
        "disabled_reason": None if enabled else "off for tests",
        "decided_by_ticket": None if enabled else "TASK-2026-00244",
    }


def _profile(name: str, skills: list[str],
             who: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "agent": name,
        "engine": "muse-code",
        "card": {
            "who": who or f"{name} test agent.",
            "delivers": [f"{name} output"],
            "never": ["change states"],
        },
        "rules": "AGENTS.md",
        "skills": list(skills),
        "output_schema": "openceo-core/objectives/agent_result.schema.json",
    }


def _write_skill(root: Path, role: str, name: str,
                 ticket: str = "TASK-2026-00244",
                 body: str = "") -> Path:
    skill_dir = root / "skills" / role / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test skill {name} for {role}.\n"
        "status: activa\n"
        f"ticket: {ticket}\n"
        "date: 2026-10-14\n"
        "---\n\n# Test\n\n" + body, encoding="utf-8")
    return skill_dir


def write_tree(root: Path, cards: dict[str, dict[str, Any]],
               profiles: dict[str, dict[str, Any]] | None = None,
               departments: list[dict[str, str]] | None = DEPARTMENTS,
               summaries: dict[str, str] | None = None) -> Path:
    """Write a catalog tree under ``root`` and return its catalog path."""
    catalog = root / "catalog"
    agents = catalog / "agents"
    agents.mkdir(parents=True)
    (catalog / "profiles").mkdir(exist_ok=True)
    for name, profile in (profiles or {}).items():
        (catalog / "profiles" / f"{name}.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    entries = []
    for name, card in cards.items():
        (agents / f"{name}.yaml").write_text(
            yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
        summary = (summaries or {}).get(
            name, f"{name} does things, not others.")
        entries.append({"name": name, "summary": summary,
                        "card": f"agents/{name}.yaml"})
    index: dict[str, Any] = {"version": 1, "agents": entries}
    if departments is not None:
        index["departments"] = departments
    (catalog / "index.yaml").write_text(
        yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    (catalog / "staff.yaml").write_text(
        yaml.safe_dump({"roster": []}, sort_keys=False), encoding="utf-8")
    return catalog


def tiny_catalog(specs: dict[str, dict[str, Any]]) -> Catalog:
    """In-memory catalog: name -> {enabled, department?, type?}."""
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
            engine=None if spec.get("type") == "human" else "muse-code",
            profile=spec.get("profile", f"profiles/{name}.yaml"),
            department=spec.get("department"),
            boundary_tools=[], skills_dir=f"skills/{name}",
            skills=(["decidir", "aprobar", "responder", "hacer"]
                    if name == "owner" else []),
            inputs=[], outputs=[], enabled=enabled,
            disabled_reason=None if enabled else spec.get("reason", "off"),
            decided_by_ticket=(None if enabled
                               else spec.get("ticket", "TASK-2026-00244")))
    return Catalog(version=1, entries=entries, agents=agents,
                   staff=StaffRoster())


class DepartmentTreeTest(unittest.TestCase):
    def test_ai_card_with_declared_department_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root, {"accountant": _card("accountant",
                                           department="finance")})
            catalog = load_catalog(tree)
        self.assertEqual("finance",
                         catalog.agents["accountant"].department)
        self.assertEqual({"engineering", "finance", "research",
                          "operations"},
                         {item.name for item in catalog.departments})

    def test_undeclared_department_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root, {"accountant": _card("accountant",
                                           department="treasury")})
            with self.assertRaisesRegex(CatalogError, "not declared"):
                load_catalog(tree)

    def test_card_without_department_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(root, {"dev": _card("dev")})
            catalog = load_catalog(tree)
        self.assertIsNone(catalog.agents["dev"].department)

    def test_index_without_departments_section_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(root, {"dev": _card("dev")},
                              departments=None)
            catalog = load_catalog(tree)
        self.assertEqual([], catalog.departments)

    def test_duplicate_department_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root, {"dev": _card("dev")},
                departments=[{"name": "finance", "summary": "Money."},
                             {"name": "finance", "summary": "Money twice."}])
            with self.assertRaisesRegex(CatalogError, "duplicate"):
                load_catalog(tree)

    def test_departments_must_be_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(root, {"dev": _card("dev")},
                              departments={"finance": "Money."})  # type: ignore[arg-type]
            with self.assertRaisesRegex(CatalogError, "must be a list"):
                load_catalog(tree)

    def test_human_card_may_carry_a_department(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root, {"owner": _card("owner", card_type="human",
                                      profile=False, department=None),
                       "auditor": _card("auditor", card_type="human",
                                        profile=False,
                                        department="finance")})
            catalog = load_catalog(tree)
        self.assertIsNone(catalog.agents["owner"].department)
        self.assertEqual("finance", catalog.agents["auditor"].department)

    def test_blank_department_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root, {"dev": _card("dev", department="  ")})
            with self.assertRaises(CatalogError):
                load_catalog(tree)


class ResolveSelectedAgentTest(unittest.TestCase):
    def test_manual_aliases_preserved(self) -> None:
        catalog = tiny_catalog({"dev": {}, "operations": {}})
        for hint, want in (("developer", "dev"), ("devops", "dev"),
                           ("operations", "operations"), ("dev", "dev")):
            with self.subTest(hint=hint):
                self.assertEqual(
                    want, resolve_selected_agent(hint, None, catalog))
                self.assertEqual(want, resolve_selected_agent(hint, None))

    def test_catalog_only_name_resolves_to_itself(self) -> None:
        catalog = tiny_catalog({"dev": {}, "accountant": {}})
        self.assertEqual(
            "accountant",
            resolve_selected_agent("accountant", None, catalog))

    def test_unknown_and_empty_resolve_to_none(self) -> None:
        catalog = tiny_catalog({"dev": {}})
        for hint in ("nope", "", "  ", None, "ceo"):
            with self.subTest(hint=hint):
                self.assertIsNone(
                    resolve_selected_agent(hint, None, catalog))
        self.assertIsNone(resolve_selected_agent("accountant", None))

    def test_coding_tool_forces_dev(self) -> None:
        catalog = tiny_catalog({"dev": {}})
        self.assertEqual(
            "dev", resolve_selected_agent("nope", "tier_s", catalog))
        self.assertEqual("dev", resolve_selected_agent(None, "tier_c"))

    def test_normalizes_case_and_spaces(self) -> None:
        catalog = tiny_catalog({"dev": {}, "accountant": {}})
        self.assertEqual(
            "accountant",
            resolve_selected_agent("  Accountant ", None, catalog))

    def test_alias_wins_over_catalog_self_name(self) -> None:
        catalog = tiny_catalog({"dev": {}, "devops": {}})
        self.assertEqual(
            "dev", resolve_selected_agent("devops", None, catalog))


class RouteByCatalogTest(unittest.TestCase):
    def test_new_ai_agent_routes_generic(self) -> None:
        catalog = tiny_catalog({"dev": {}, "accountant": {}})
        graph = OpenCEOGraph(None, None, None, catalog=catalog)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "accountant"}))

    def test_dev_wired_rest_unsupported(self) -> None:
        catalog = tiny_catalog(
            {"dev": {}, "accountant": {},
             "old": {"enabled": False, "reason": "retired"},
             "owner": {"type": "human"}})
        graph = OpenCEOGraph(None, None, None, catalog=catalog)
        cases = {"dev": "dev", "accountant": "generic_agent",
                 "ceo": "unsupported", "": "unsupported",
                 "unknown": "unsupported", "old": "unsupported",
                 "owner": "unsupported"}
        for name, want in cases.items():
            with self.subTest(agent=name):
                self.assertEqual(
                    want,
                    graph._after_ceo_route({"selected_agent": name}))

    def test_new_name_without_catalog_entry_stays_unsupported(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        self.assertEqual(
            "unsupported",
            graph._after_ceo_route({"selected_agent": "accountant"}))


class AgentsSectionTest(unittest.TestCase):
    def test_catalog_lines_carry_department_summary_and_detail(self) -> None:
        catalog = tiny_catalog(
            {"accountant": {"department": "finance"}, "dev": {}})
        section = _agents_section(
            {"accountant": {"enabled": True}, "dev": {"enabled": True}},
            skills_md="- accountant: reconcile — matches ledger lines.",
            catalog=catalog,
            detail_paths={"accountant": "/scratch/agents/accountant.md",
                          "dev": "/scratch/agents/dev.md"})
        self.assertIn("- accountant [finance]: accountant does things, "
                      "not others. (enabled)", section)
        self.assertIn("- dev: dev does things, not others. (enabled)",
                      section)
        self.assertIn("reconcile", section)
        self.assertIn("AGENT DETAIL", section)
        self.assertIn("- accountant: /scratch/agents/accountant.md",
                      section)
        self.assertIn("recipes stay unloaded", section)

    def test_staff_names_render_bare(self) -> None:
        catalog = tiny_catalog({"dev": {}})
        section = _agents_section(
            {"dev": {"enabled": True}, "alice": {"enabled": True}},
            catalog=catalog)
        self.assertIn("- alice: enabled", section)
        self.assertNotIn("AGENT DETAIL", section)

    def test_disabled_line_keeps_reason(self) -> None:
        catalog = tiny_catalog(
            {"old": {"enabled": False, "reason": "retired",
                     "department": "operations"}})
        section = _agents_section(
            {"old": {"enabled": False, "reason": "retired"}},
            catalog=catalog)
        self.assertIn("— DISABLED (retired) — never route here", section)
        self.assertIn("[operations]", section)

    def test_without_catalog_keeps_bare_shape(self) -> None:
        section = _agents_section({"dev": {"enabled": True},
                                   "old": {"enabled": False,
                                           "reason": "retired"}})
        self.assertIn("- dev: enabled", section)
        self.assertIn("- old: DISABLED (retired) — never route here",
                      section)


class StageAgentDetailsTest(unittest.TestCase):
    def test_stages_compact_detail_without_recipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root,
                {"accountant": _card(
                    "accountant", department="finance",
                    skills=["reconcile"], profile=True,
                    tools=["openceo_search_tickets"])},
                {"accountant": _profile(
                    "accountant", ["reconcile", "create-skill"],
                    who="Counts company money.")})
            _write_skill(root, "accountant", "reconcile",
                         body="SECRET-RECIPE-BODY-MARKER\n")
            sync_skill_links(discover_role_skills(root), root)
            catalog = load_catalog(tree)
            staged = _stage_agent_details(root / "scratch", catalog, tree)
            self.assertEqual(["accountant"], [name for name, _ in staged])
            text = Path(staged[0][1]).read_text(encoding="utf-8")
        self.assertIn("# accountant [finance]", text)
        self.assertIn("Counts company money.", text)
        self.assertIn("Delivers:", text)
        self.assertIn("Never:", text)
        self.assertIn("reconcile", text)
        self.assertIn("openceo_search_tickets", text)
        self.assertNotIn("SECRET-RECIPE-BODY-MARKER", text)

    def test_purges_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(root, {"dev": _card("dev")})
            stale = root / "scratch" / "agents" / "stale.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            catalog = load_catalog(tree)
            _stage_agent_details(root / "scratch", catalog, tree)
            self.assertFalse(stale.exists())
            self.assertTrue((root / "scratch" / "agents" / "dev.md")
                            .is_file())

    def test_missing_profile_degrades_to_card_only(self) -> None:
        catalog = tiny_catalog({"accountant": {"department": "finance"}})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertLogs("objectives.engine",
                                 level="WARNING") as logs:
                staged = _stage_agent_details(root, catalog, root)
            text = Path(staged[0][1]).read_text(encoding="utf-8")
        self.assertTrue(any("without profile" in line
                            for line in logs.output))
        self.assertIn("(role profile unreadable: card detail only)", text)
        self.assertIn("# accountant [finance]", text)

    def test_unknown_name_renders_absence(self) -> None:
        catalog = tiny_catalog({"dev": {}})
        self.assertIn("not in the catalog",
                      agent_detail_text(catalog, "ghost"))


class TransferOwnershipTest(unittest.TestCase):
    def _world(self, root: Path) -> None:
        write_tree(
            root,
            {"finance": _card(
                "finance", department="finance",
                skills=["registrar-gasto", "regla-10"], profile=True,
                tools=["openceo_register_expense"]),
             "accountant": _card(
                 "accountant", department="finance",
                 skills=[], profile=True,
                 tools=["openceo_search_tickets"])},
            {"finance": _profile(
                "finance",
                ["registrar-gasto", "regla-10", "create-skill"]),
             "accountant": _profile("accountant", ["create-skill"])})
        _write_skill(root, "finance", "registrar-gasto",
                     ticket="TASK-2026-00221")
        _write_skill(root, "finance", "regla-10",
                     ticket="TASK-2026-00221")
        sync_skill_links(discover_role_skills(root), root)

    def test_transfer_keeps_history_and_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._world(root)
            tree = root / "catalog"
            load_catalog(tree)
            (root / "skills" / "accountant").mkdir(parents=True)
            shutil.move(str(root / "skills" / "finance" / "regla-10"),
                        str(root / "skills" / "accountant" / "regla-10"))
            (tree / "agents" / "finance.yaml").write_text(
                yaml.safe_dump(_card(
                    "finance", department="finance",
                    skills=["registrar-gasto"], profile=True,
                    tools=["openceo_register_expense"]),
                    sort_keys=False),
                encoding="utf-8")
            (tree / "agents" / "accountant.yaml").write_text(
                yaml.safe_dump(_card(
                    "accountant", department="finance",
                    skills=["regla-10"], profile=True,
                    tools=["openceo_search_tickets"]),
                    sort_keys=False),
                encoding="utf-8")
            (tree / "profiles" / "finance.yaml").write_text(
                yaml.safe_dump(_profile(
                    "finance", ["registrar-gasto", "create-skill"]),
                    sort_keys=False),
                encoding="utf-8")
            (tree / "profiles" / "accountant.yaml").write_text(
                yaml.safe_dump(_profile(
                    "accountant", ["regla-10", "create-skill"]),
                    sort_keys=False),
                encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "regla-10"):
                load_catalog(tree)
            (root / ".codex" / "skills" / "regla-10").unlink()
            sync_skill_links(discover_role_skills(root), root)
            catalog = load_catalog(tree)
            moved = (root / "skills" / "accountant" / "regla-10"
                     / "SKILL.md").read_text(encoding="utf-8")
            by_agent = load_all_role_skills(catalog, root)
        self.assertIn("ticket: TASK-2026-00221", moved)
        self.assertEqual(["openceo_register_expense"],
                         catalog.agents["finance"].boundary_tools)
        self.assertEqual(["openceo_search_tickets"],
                         catalog.agents["accountant"].boundary_tools)
        self.assertNotEqual(catalog.agents["finance"].boundary_tools,
                            catalog.agents["accountant"].boundary_tools)
        self.assertEqual([], check_skill_names_unique(by_agent))
        self.assertEqual({"registrar-gasto"},
                         {skill.name for skill in by_agent["finance"]})
        self.assertEqual({"regla-10"},
                         {skill.name for skill in by_agent["accountant"]})

    def test_original_scope_narrows_on_specialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._world(root)
            tree = root / "catalog"
            (tree / "profiles" / "finance.yaml").write_text(
                yaml.safe_dump(_profile(
                    "finance",
                    ["registrar-gasto", "regla-10", "create-skill"],
                    who="Records expenses only; the accountant owns "
                        "summaries and rules."),
                    sort_keys=False),
                encoding="utf-8")
            catalog = load_catalog(tree)
            profile = load_role_profile("finance", tree)
            text = agent_detail_text(catalog, "finance", profile)
        self.assertIn("Records expenses only", text)


class DispatchAcceptanceTest(unittest.TestCase):
    def _tree(self, root: Path) -> Path:
        tree = write_tree(
            root,
            {"accountant": _card(
                "accountant", department="finance",
                skills=["reconcile", "report"], profile=True,
                tools=["openceo_search_tickets",
                       "openceo_get_ticket"])},
            {"accountant": _profile(
                "accountant",
                ["reconcile", "report", "create-skill"],
                who="Reconciles the company ledger.")})
        _write_skill(root, "accountant", "reconcile")
        _write_skill(root, "accountant", "report")
        sync_skill_links(discover_role_skills(root), root)
        return tree

    def test_new_employee_dispatches_with_scripted_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = self._tree(root)
            catalog = load_catalog(tree)
            gate = resolve_agent_gate(
                None, {"target_agent": "accountant"}, None, catalog)
            self.assertTrue(gate.proceed)
            self.assertEqual("accountant", gate.hint)
            self.assertEqual(
                "accountant",
                resolve_selected_agent("accountant", None, catalog))
            graph = OpenCEOGraph(None, None, None, catalog=catalog)
            self.assertEqual(
                "generic_agent",
                graph._after_ceo_route({"selected_agent": "accountant"}))
            factory, recorded = scripted_executor(
                ExecResult(returncode=0, elapsed_s=1.0,
                           output_tail=hecho_stdout("ledger balanced"),
                           usage=None))
            wiring = wiring_with(root, catalog, factory)
            with mock.patch("orchestration.generic_agent.load_role_profile",
                            side_effect=lambda name: load_role_profile(
                                name, tree)):
                out = asyncio.run(run_generic_agent(
                    "accountant", generic_task(), wiring))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        verdict = AgentResult.model_validate(
            out["params"]["agent_result"])
        self.assertEqual("hecho", verdict.resultado.value)
        self.assertEqual("accountant", out["selected_agent"])
        prompts = [call["prompt"] for call in recorded
                   if "prompt" in call]
        self.assertTrue(prompts)
        self.assertIn("Reconciles the company ledger.", prompts[0])
        self.assertTrue(any(call.get("factory_role") == "accountant"
                            for call in recorded))

    def test_disabled_new_employee_waits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(
                root,
                {"accountant": _card("accountant", department="finance",
                                     enabled=False)})
            catalog = load_catalog(tree)
            gate = resolve_agent_gate(
                None, {"target_agent": "accountant"}, None, catalog)
            graph = OpenCEOGraph(None, None, None, catalog=catalog)
            route = graph._after_ceo_route(
                {"selected_agent": "accountant"})
        self.assertFalse(gate.proceed)
        self.assertIn("routing_missing", gate.reason)
        self.assertEqual("unsupported", route)


NEW_NAMES = ("accountant", "auditor", "treasurer", "analyst",
             "archivist", "bookkeeper", "buyer", "cashier", "clerk",
             "collector", "controller", "estimator", "inspector",
             "liaison")


class TwentyFixtureTest(unittest.TestCase):
    def test_twenty_cards_load_resolve_and_match(self) -> None:
        cards: dict[str, dict[str, Any]] = {}
        profiles: dict[str, dict[str, Any]] = {}
        depts = ["engineering", "finance", "research", "operations"]
        for name in ("dev", "owner", "operations", "secretary",
                     "finance", "research"):
            if name == "owner":
                cards[name] = _card(name, card_type="human",
                                    profile=False)
            else:
                cards[name] = _card(
                    name, department={
                        "dev": "engineering", "finance": "finance",
                        "research": "research"}.get(name, "operations"))
        for position, name in enumerate(NEW_NAMES):
            cards[name] = _card(name, department=depts[position % 4])
        profiles["accountant"] = _profile("accountant", [])
        profiles["auditor"] = _profile("auditor", [])
        cards["accountant"]["profile"] = "profiles/accountant.yaml"
        cards["auditor"]["profile"] = "profiles/auditor.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_tree(root, cards, profiles)
            catalog = load_catalog(tree)
        self.assertEqual(20, len(catalog.agents))
        for name in catalog.agents:
            with self.subTest(agent=name):
                self.assertEqual(
                    name, resolve_selected_agent(name, None, catalog))
        mismatches = find_consumer_mismatches(
            [member.value for member in AgentName],
            [member.value for member in CEORouter.AGENT_ALIASES.values()],
            planner_registry_view(catalog),
            ("dev",),
            catalog)
        self.assertEqual([], mismatches)
        gate = resolve_agent_gate(
            None, {"target_agent": "accountant"}, None, catalog)
        self.assertTrue(gate.proceed)
        graph = OpenCEOGraph(None, None, None, catalog=catalog)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "liaison"}))


class RealTreeTest(unittest.TestCase):
    def test_real_departments_declared_and_ai_cards_assigned(self) -> None:
        catalog = load_catalog()
        declared = {item.name for item in catalog.departments}
        self.assertEqual({"engineering", "finance", "research",
                          "operations"}, declared)
        for name, card in catalog.agents.items():
            with self.subTest(agent=name):
                if card.type.value == "ai":
                    self.assertIn(card.department, declared)
                else:
                    self.assertIsNone(card.department)

    def test_real_planner_index_and_detail(self) -> None:
        catalog = load_catalog()
        registry = planner_registry_view(catalog)
        section = _agents_section(registry, catalog=catalog)
        self.assertIn("- dev [engineering]:", section)
        self.assertIn("- owner: Decides, approves, answers and does.",
                      section)
        self.assertIn("- finance [finance]:", section)
        with tempfile.TemporaryDirectory() as tmp:
            staged = _stage_agent_details(Path(tmp), catalog)
            names = [name for name, _ in staged]
            self.assertEqual(sorted(catalog.agents), names)
            dev_text = Path(dict(staged)["dev"]).read_text(encoding="utf-8")
        self.assertIn("[engineering]", dev_text)
        self.assertIn("Delivers:", dev_text)
        self.assertNotIn("SECRET", dev_text)

    def test_real_resolve(self) -> None:
        catalog = load_catalog()
        self.assertEqual(
            "research", resolve_selected_agent("research", None, catalog))
        self.assertIsNone(
            resolve_selected_agent("nope", None, catalog))
