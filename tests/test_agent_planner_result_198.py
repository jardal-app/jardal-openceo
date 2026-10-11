"""PlannerResult v2: any catalog agent, suggested skill, dev-only tier.

TASK-2026-00198. The planner targets any enabled catalog agent (owner and
staff names included); ``skill`` is an optional suggestion for any agent;
``coding_tool`` stays the dev-only execution tier.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import load_catalog, planner_registry_view  # noqa: E402
from config_loader import _planner_agents_from_catalog  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _planned_params, _planner_prompt  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    DevTier,
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)


def write_catalog(root: Path, staff: list[dict[str, Any]]) -> Path:
    """Minimal catalog tree: dev + owner + staff roster."""
    catalog = root / "catalog"
    agents = catalog / "agents"
    agents.mkdir(parents=True)
    cards = {
        "dev": {"type": "ai", "engine": "muse-code", "enabled": True},
        "owner": {"type": "human", "engine": None, "enabled": True,
                  "skills": ["decidir", "aprobar", "responder", "hacer"]},
    }
    entries = []
    for name, spec in cards.items():
        card = {"name": name, "profile": None, "boundary_tools": [],
                "skills_dir": f"skills/{name}",
                "skills": spec.get("skills", []),
                "inputs": [], "outputs": [], "enabled": spec["enabled"],
                "disabled_reason": None, "decided_by_ticket": None,
                "type": spec["type"], "engine": spec["engine"]}
        (agents / f"{name}.yaml").write_text(
            yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
        entries.append({"name": name, "summary": f"{name} summary.",
                        "card": f"agents/{name}.yaml"})
    (catalog / "index.yaml").write_text(
        yaml.safe_dump({"version": 1, "agents": entries}, sort_keys=False),
        encoding="utf-8")
    (catalog / "staff.yaml").write_text(
        yaml.safe_dump({"roster": staff}, sort_keys=False), encoding="utf-8")
    return catalog


def make_ticket(target: str, **overrides: Any) -> PlannedTicket:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Do work", "target_agent": target,
        "work_type": "general", "goal": "g", "why_now": "now",
        "acceptance_criteria": ["done"], "expected_artifacts": ["report"],
    }
    if target == "dev":
        fields.update(coding_tool=DevTier.TIER_C, tier_reason="simple")
    fields.update(overrides)
    return PlannedTicket(**fields)


def validate_target(target: str, view: dict[str, Any], **overrides: Any) -> list[str]:
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[make_ticket(target, **overrides)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents=view, open_titles=[], task_exists=_exists)

    return asyncio.run(_run())


class RegistryViewTest(unittest.TestCase):
    def test_owner_and_active_staff_included(self):
        staff = [{"name": "Ana", "role": "qa",
                  "projects": ["jardal-openceo"],
                  "active_from": "2026-01-01", "active_until": None}]
        with tempfile.TemporaryDirectory() as tmp:
            view = planner_registry_view(
                load_catalog(write_catalog(Path(tmp), staff)))
        self.assertEqual({"enabled": True}, view["owner"])
        self.assertEqual({"enabled": True}, view["Ana"])
        self.assertEqual({"enabled": True}, view["dev"])

    def test_inactive_staff_disabled_with_reason(self):
        staff = [{"name": "Ana", "role": "qa",
                  "projects": ["jardal-openceo"],
                  "active_from": "2026-01-01", "active_until": "2026-02-01"}]
        with tempfile.TemporaryDirectory() as tmp:
            view = planner_registry_view(
                load_catalog(write_catalog(Path(tmp), staff)),
                today=date(2026, 10, 11))
        self.assertFalse(view["Ana"]["enabled"])
        self.assertIn("reason", view["Ana"])

    def test_staff_addressed_by_name_not_role(self):
        staff = [{"name": "Ana", "role": "qa",
                  "projects": ["jardal-openceo"],
                  "active_from": "2026-01-01", "active_until": None}]
        with tempfile.TemporaryDirectory() as tmp:
            view = planner_registry_view(
                load_catalog(write_catalog(Path(tmp), staff)))
        self.assertIn("Ana", view)
        self.assertNotIn("qa", view)

    def test_derivation_uses_view(self):
        staff = [{"name": "Ana", "role": "qa",
                  "projects": ["jardal-openceo"],
                  "active_from": "2026-01-01", "active_until": None}]
        with tempfile.TemporaryDirectory() as tmp:
            catalog = load_catalog(write_catalog(Path(tmp), staff))
            derived = _planner_agents_from_catalog(catalog)
            expected = planner_registry_view(catalog)
        self.assertEqual(expected, derived)


class TargetValidationTest(unittest.TestCase):
    def setUp(self):
        staff = [{"name": "Ana", "role": "qa",
                  "projects": ["jardal-openceo"],
                  "active_from": "2026-01-01", "active_until": None}]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        catalog = load_catalog(
            write_catalog(Path(self._tmp.name), staff))
        self.view = planner_registry_view(catalog)

    def test_enabled_dev_owner_staff_accepted(self):
        for target in ("dev", "owner", "Ana"):
            with self.subTest(target=target):
                self.assertEqual([], validate_target(target, self.view))

    def test_unknown_target_rejected(self):
        errors = validate_target("frobnicate", self.view)
        self.assertTrue(any("unknown target_agent" in line for line in errors))

    def test_tier_outside_dev_rejected(self):
        with self.assertRaises(Exception) as ctx:
            make_ticket("owner", coding_tool=DevTier.TIER_C,
                        tier_reason="x")
        self.assertIn("coding_tool is only valid", str(ctx.exception))


class SkillFieldTest(unittest.TestCase):
    def test_skill_optional_for_any_agent(self):
        for target in ("dev", "owner"):
            with self.subTest(target=target):
                ticket = make_ticket(target, skill="triage")
                self.assertEqual("triage", ticket.skill)
                self.assertIsNone(make_ticket(target).skill)

    def test_blank_skill_rejected(self):
        with self.assertRaises(Exception):
            make_ticket("dev", skill="  ")

    def test_params_carry_skill(self):
        self.assertEqual("triage",
                         _planned_params(make_ticket("dev", skill="triage"))
                         ["skill"])
        self.assertNotIn("skill", _planned_params(make_ticket("dev")))

    def test_prompt_documents_skill(self):
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], blocked_tickets=[], agents_md="", policy_text="p",
            handoffs=[], roadmap=[])
        self.assertIn('"skill?"', prompt)
        self.assertIn("suggestion", prompt)


if __name__ == "__main__":
    unittest.main()
