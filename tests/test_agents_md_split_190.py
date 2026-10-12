"""Contract: AGENTS.md is the standalone executor file (TASK-2026-00190).

CLAUDE.md §10 used to be served as AGENTS.md through a symlink, so every
motor auto-loaded the whole Owner manual (~33 KB, past Codex's 32 KiB cut).
Since 00190 the executor contract lives in a real AGENTS.md file and
CLAUDE.md only imports it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration.knowledge import _rules_files, repo_preamble  # noqa: E402

EXECUTOR_SECTIONS = (
    "### Layout",
    "### Python",
    "### Validate before finishing",
    "### Service (host mode)",
    "### Tier S executor contract",
    "### Standing rules",
)

OWNER_MARKS = (
    "## 11",
    "sigue",
    "audita",
    "cockpit",
    "LiveKit",
    "first_layer",
    "preset",
    "APK",
)


def _section_10(text: str) -> str:
    return text[text.index("## 10."):text.index("## 11.")]


class AgentsSplitTests(unittest.TestCase):
    def test_agents_md_is_a_real_file(self) -> None:
        agents = REPO / "AGENTS.md"
        self.assertTrue(agents.is_file())
        self.assertFalse(agents.is_symlink(), "AGENTS.md must not be a symlink")

    def test_agents_md_fits_motor_budgets(self) -> None:
        size = (REPO / "AGENTS.md").stat().st_size
        self.assertLessEqual(size, 5 * 1024, f"AGENTS.md is {size} bytes")

    def test_agents_md_holds_the_executor_contract(self) -> None:
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        for section in EXECUTOR_SECTIONS:
            self.assertIn(section, text)
        self.assertIn("Sesiones del Owner: lee CLAUDE.md.", text)

    def test_agents_md_has_no_owner_sections(self) -> None:
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        for mark in OWNER_MARKS:
            self.assertNotIn(mark, text, f"owner mark leaked: {mark}")

    def test_claude_md_imports_agents_md(self) -> None:
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("## 10.", text)
        section = _section_10(text)
        self.assertIn("@AGENTS.md", section)
        self.assertNotIn("### Tier S executor contract", section)
        self.assertNotIn("### Standing rules", section)
        self.assertNotIn("symlink", section)

    def test_prompts_read_agents_md_as_its_own_file(self) -> None:
        self.assertEqual(["CLAUDE.md", "AGENTS.md"], _rules_files(REPO))
        self.assertIn("AGENTS.md", repo_preamble(REPO, "en"))
        self.assertIn("AGENTS.md", repo_preamble(REPO, "es"))


if __name__ == "__main__":
    unittest.main()
