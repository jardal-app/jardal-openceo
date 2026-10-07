"""Contract: numeric tiers are retired, letters are the only routing (TASK-2026-00138).

``CodingTier`` (tier1-4), the LLM ``TierSelector``, the MCP ``coding_tier``
parameter, the ``dev.coding_tier`` default and every deprecated routing
source (``params``/``project``/``tier`` plus pre-v2 provider values) are
gone. ``coding_tool`` (tier_s/a/b/c) is the single routing field, written
at creation and read only at the top level.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import state  # noqa: E402
from agents.dev.routing import resolve_coding_tool, RoutingDecision  # noqa: E402
from agents.dev.session import DevExecutionOutput, DevSessionManager  # noqa: E402
from config_loader import ConfigLoader  # noqa: E402
from dev_session_service import DevSessionService  # noqa: E402
from ops.create_project import _project_yaml  # noqa: E402
from settings import Settings  # noqa: E402
from state import (  # noqa: E402
    CodingTool,
    DevProvider,
    GraphState,
    TaskCreateRequest,
    TaskDocument,
    TaskPatchRequest,
)

#: Tokens of the retired routing. No live source under openceo-core may
#: carry them, except the documented exclusion below.
_FORBIDDEN = re.compile(
    r"CodingTier|coding_tier|TierSelector|tier_selector"
    r"|params\.coding_tool|params\.coding_tier"
    r"|\btier[1-4]\b"
)

#: (file, line-substring) pairs allowed to carry a forbidden token.
#: ``validate_project`` must name the obsolete key to tell the owner what
#: to remove (docstring, check, message). ``workflow_service`` labels how
#: a deterministic workflow was picked (explicit ``tier0`` vs scored
#: ``tier1``): a workflow-engine tag, not the retired dev routing. The
#: sweep asserts each exclusion is still there so none can go stale
#: silently.
_ALLOWED = (
    ("config_loader.py", "``dev.coding_tier`` (numeric tiers retired"),
    ("config_loader.py", 'if "coding_tier" in dev:'),
    ("config_loader.py", "dev.coding_tier is obsolete"),
    ("workflow_service.py", 'tier="tier1"'),
)


def _sweep_hits(root: Path, suffix: str) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob(f"*{suffix}")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    return hits


def _loader_for(tmp: Path) -> ConfigLoader:
    return ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(tmp)))


def _provider_service() -> DevSessionService:
    return DevSessionService(MagicMock(), AsyncMock(), MagicMock(), MagicMock(), MagicMock())


class NoNumericTiersTests(unittest.TestCase):
    def test_core_sources_carry_no_retired_tokens(self) -> None:
        hits = _sweep_hits(CORE, ".py")
        allowed = [hit for hit in hits
                   if any(name in hit and snippet in hit for name, snippet in _ALLOWED)]
        self.assertEqual(len(allowed), len(_ALLOWED),
                         f"documented exclusion missing from sweep: {hits}")
        self.assertEqual([], [hit for hit in hits if hit not in allowed])

    def test_state_models_carry_no_numeric_tier(self) -> None:
        self.assertFalse(hasattr(state, "CodingTier"))
        for model in (TaskCreateRequest, TaskDocument, TaskPatchRequest):
            self.assertNotIn("coding_tier", model.model_fields, model.__name__)
        self.assertNotIn("coding_tier", GraphState.__annotations__)

    def test_selector_is_gone_from_dispatch(self) -> None:
        self.assertFalse((CORE / "agents" / "dev" / "tier_selector.py").exists())
        manager = DevSessionManager(MagicMock(), MagicMock())
        self.assertFalse(hasattr(manager, "_tier_selector"))
        field_names = {field.name for field in dataclasses.fields(DevExecutionOutput)}
        self.assertNotIn("coding_tier", field_names)

    def test_routing_helpers_are_gone(self) -> None:
        import agents.dev.routing as routing_mod

        for name in ("tool_for_tier", "normalize_task_dict",
                     "normalize_document_routing", "plan_routing_migration",
                     "RoutingMigrationPlan"):
            self.assertFalse(hasattr(routing_mod, name), name)
        self.assertNotIn("project_config",
                         inspect.signature(resolve_coding_tool).parameters)
        field_names = [field.name for field in dataclasses.fields(RoutingDecision)]
        self.assertEqual(["tool", "source", "detail"], field_names)

    def test_routing_reads_root_letters_only(self) -> None:
        for value in ("tier_s", "tier_a", "tier_b", "tier_c"):
            decision = resolve_coding_tool({"task_id": "t", "coding_tool": value})
            self.assertEqual(decision.tool, CodingTool(value))
            self.assertEqual(decision.source, "root")
        # Pre-v2 provider values no longer resolve.
        for value in ("codex", "codexj", "claude", "claudej", "muse",
                      "opencode", "kimi", "glm", "gemini"):
            decision = resolve_coding_tool({"task_id": "t", "coding_tool": value})
            self.assertIsNone(decision.tool, value)
            self.assertEqual(decision.source, "none", value)
        # Deprecated sources no longer resolve either.
        stale = {"task_id": "t", "coding_tier": "tier1",
                 "params": {"coding_tool": "tier_a", "coding_tier": "tier2"}}
        decision = resolve_coding_tool(stale)
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")

    def test_global_config_carries_no_numeric_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            dev = _loader_for(Path(raw)).load_project("anything")["dev"]
        self.assertNotIn("coding_tier", dev)

    def test_validate_project_rejects_numeric_tier_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proj = root / "proj"
            proj.mkdir()
            (proj / "project.yaml").write_text(
                "dev:\n  coding_tier: auto\n  default_provider: muse\n",
                encoding="utf-8")
            errors = _loader_for(root).validate_project("proj")
            self.assertTrue(any("dev.coding_tier is obsolete" in err for err in errors),
                            errors)
            (proj / "project.yaml").write_text(
                "dev:\n  default_provider: muse\n", encoding="utf-8")
            self.assertEqual([], _loader_for(root).validate_project("proj"))

    def test_provider_fallback_ignores_numeric_tier(self) -> None:
        service = _provider_service()
        auto = {"provider": DevProvider.AUTO.value}
        self.assertEqual(
            service._resolve_provider(auto, {"dev": {"coding_tier": "tier1"}}),
            DevProvider.CLAUDE)
        self.assertEqual(
            service._resolve_provider(auto, {"dev": {"coding_tier": "tier3"}}),
            DevProvider.CLAUDE)
        self.assertEqual(
            service._resolve_provider(auto, {"dev": {"default_provider": "codex"}}),
            DevProvider.CODEX)

    def test_project_yamls_carry_no_numeric_tier(self) -> None:
        yamls = sorted((REPO / "projects").glob("*/project.yaml"))
        self.assertTrue(yamls, "no project.yaml found under projects/")
        hits = [str(path) for path in yamls
                if _FORBIDDEN.search(path.read_text(encoding="utf-8"))]
        self.assertEqual([], hits)

    def test_new_project_template_carries_no_numeric_tier(self) -> None:
        body = _project_yaml(project_key="demo", number=1,
                             remote_url="git@example:demo.git",
                             repo_path="/tmp/demo",
                             vault_root="vault", vault_project="projects/jardal/demo")
        self.assertIsNone(_FORBIDDEN.search(body), body)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proj = root / "demo"
            proj.mkdir()
            (proj / "project.yaml").write_text(body, encoding="utf-8")
            self.assertEqual([], _loader_for(root).validate_project("demo"))

    def test_dashboard_shows_letters_only(self) -> None:
        pages = [
            REPO / "openceo-react/src/pages/TicketList.jsx",
            REPO / "openceo-react/src/pages/TicketDetail.jsx",
            REPO / "openceo-react/src/components/KanbanBoard/index.jsx",
        ]
        for path in pages:
            body = path.read_text(encoding="utf-8")
            self.assertIsNone(_FORBIDDEN.search(body), str(path))
        for path in pages[:2]:
            body = path.read_text(encoding="utf-8")
            for letter in ("tier_s", "tier_a", "tier_b", "tier_c"):
                self.assertIn(letter, body, f"{path.name} lacks {letter}")

    def test_migration_script_is_gone(self) -> None:
        self.assertFalse((REPO / "scripts" / "migrate_coding_tool_routing.py").exists())


if __name__ == "__main__":
    unittest.main()
