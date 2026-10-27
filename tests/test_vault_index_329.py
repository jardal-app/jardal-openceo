"""Dev prompt carries a bounded hub + practices index (TASK-2026-00329, D6).

Synthetic vaults under tmp dirs only: never the real vault, never other
projects, never shared/. No tokens.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration.vault_index import (
    VAULT_INDEX_MAX_CHARS,
    build_vault_index,
)


def _vault(root: Path, *, with_practices: bool = True) -> Path:
    active = root / "vault" / "projects" / "org" / "active"
    active.mkdir(parents=True)
    (active / "active.md").write_text(
        "# Active hub\n\nShort hub line.\n\nSECRET-HUB-BODY-MARKER deep down.\n")
    if with_practices:
        notes = active / "practices"
        notes.mkdir()
        (notes / "notes.md").write_text(
            "---\ntype: practice\n---\n\n# Notes\n\nFirst rule line.\n\n"
            "Second paragraph that must stay out.\n"
            "SECRET-PRACTICE-BODY-MARKER deep down.\n")
        (notes / "second.md").write_text("# Second\n\nAnother line.\n")
    return root / "vault"


def _cfg(vault: Path, project: str = "projects/org/active") -> dict:
    return {"dev": {"vault_project": {"dir": str(vault), "project": project}}}


class BuildVaultIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = _vault(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_lists_hub_and_practices_one_line_each(self) -> None:
        index = build_vault_index(_cfg(self.vault))
        self.assertIn("KNOWLEDGE INDEX", index)
        self.assertIn("Active hub (active.md)", index)
        self.assertIn("Notes (practices/notes.md)", index)
        self.assertIn("Second (practices/second.md)", index)

    def test_index_never_contains_note_bodies(self) -> None:
        index = build_vault_index(_cfg(self.vault))
        self.assertIn("First rule line.", index)
        self.assertNotIn("Second paragraph that must stay out.", index)
        self.assertNotIn("SECRET-HUB-BODY-MARKER", index)
        self.assertNotIn("SECRET-PRACTICE-BODY-MARKER", index)

    def test_missing_vault_is_noop(self) -> None:
        for cfg in (None, {}, {"dev": {}}, {"dev": {"vault_project": None}},
                    {"dev": {"vault_project": "nope"}},
                    "not-a-mapping"):
            self.assertEqual(build_vault_index(cfg), "", msg=repr(cfg))

    def test_project_without_hub_or_practices_is_noop(self) -> None:
        empty = Path(self._tmp.name) / "vault" / "projects" / "org" / "ghost"
        empty.mkdir(parents=True)
        cfg = {"dev": {"vault_project": {
            "dir": str(self.vault), "project": "projects/org/ghost"}}}
        self.assertEqual(build_vault_index(cfg), "")

    def test_project_without_practices_lists_hub_only(self) -> None:
        vault = _vault(Path(self._tmp.name) / "other", with_practices=False)
        index = build_vault_index(_cfg(vault))
        self.assertIn("Active hub (active.md)", index)
        self.assertNotIn("practices/", index)

    def test_symlink_note_is_skipped(self) -> None:
        notes = self.vault / "projects/org/active/practices"
        (notes / "real.md").write_text("# Real\n\nLine.\n")
        link = notes / "link.md"
        link.symlink_to(notes / "real.md")
        index = build_vault_index(_cfg(self.vault))
        self.assertNotIn("link.md", index)
        self.assertIn("real.md", index)

    def test_cap_truncates_with_notice_and_no_detail(self) -> None:
        notes = self.vault / "projects/org/active/practices"
        for num in range(30):
            (notes / f"note-{num:02d}.md").write_text(
                f"# Note {num}\n\n" + ("filler line of text. " * 20) + "\n")
        index = build_vault_index(_cfg(self.vault), max_chars=500)
        self.assertLessEqual(len(index), 500 + 200)
        self.assertIn("truncated", index)
        self.assertNotIn("note-29", index)
        self.assertNotIn("filler line of text. " * 20, index)

    def test_default_cap_holds_realistic_vault(self) -> None:
        index = build_vault_index(_cfg(self.vault))
        self.assertLessEqual(len(index), VAULT_INDEX_MAX_CHARS)


class CodingPromptIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = _vault(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self):
        from agents.dev.session import DevSessionManager
        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def test_coding_prompt_contains_index_not_bodies(self) -> None:
        manager = self._manager()
        task = {"task_id": "t", "title": "t", "description": "d"}
        prompt = manager._build_coding_prompt(task, _cfg(self.vault))
        self.assertIn("KNOWLEDGE INDEX", prompt)
        self.assertIn("practices/notes.md", prompt)
        self.assertNotIn("SECRET-PRACTICE-BODY-MARKER", prompt)

    def test_coding_prompt_without_vault_unchanged(self) -> None:
        manager = self._manager()
        task = {"task_id": "t", "title": "t", "description": "d"}
        prompt = manager._build_coding_prompt(task, {"dev": {}})
        self.assertNotIn("KNOWLEDGE INDEX", prompt)


class TierSCyclePromptIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = _vault(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cycle_and_ctx(self, vault_index: str):
        from types import SimpleNamespace
        from agents.dev.tier_s import cycle as cycle_mod
        from agents.dev.tier_s.plan import Plan, PlannedFile
        cycle = cycle_mod.TierSCycle.__new__(cycle_mod.TierSCycle)
        ctx = SimpleNamespace(
            ticket={"title": "t", "description": "d", "acceptance": ["a1"]},
            repo=Path(self._tmp.name), ref="T-1",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
            vault_index=vault_index)
        plan = Plan(
            ticket="t", objective="", goal="g", acceptance=(),
            files=(PlannedFile(path="a.py", operation="modify",
                               reason="r"),),
            contracts=(), decisions=(), steps=("s1",),
            estimate_files=1, estimate_lines=10, out_of_scope=())
        return cycle, ctx, plan

    def test_planner_and_builder_carry_index(self) -> None:
        cycle, ctx, plan = self._cycle_and_ctx(
            build_vault_index(_cfg(self.vault)))
        planner = cycle._planner_prompt(ctx)
        builder = cycle._builder_prompt(ctx, plan)
        for prompt in (planner, builder):
            self.assertIn("KNOWLEDGE INDEX", prompt)
            self.assertIn("practices/notes.md", prompt)
            self.assertNotIn("SECRET-PRACTICE-BODY-MARKER", prompt)

    def test_prompts_without_vault_unchanged(self) -> None:
        cycle, ctx, plan = self._cycle_and_ctx("")
        self.assertNotIn("KNOWLEDGE INDEX", cycle._planner_prompt(ctx))
        self.assertNotIn("KNOWLEDGE INDEX",
                         cycle._builder_prompt(ctx, plan))


class DevProfileIndexInstructionTests(unittest.TestCase):
    def test_dev_profile_orders_on_demand_reading(self) -> None:
        from catalog import load_role_profile
        profile = load_role_profile("dev")
        blob = "; ".join(profile.card.delivers)
        self.assertIn("on demand", blob)


if __name__ == "__main__":
    unittest.main()
