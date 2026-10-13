from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.tmux_runtime import TmuxRuntime


LONG_PROMPT = (
    "Read CLAUDE.md and AGENTS.md in the repo root and follow all applicable\n"
    "rules before doing anything else.\n"
    "\n"
    "Implement ONLY sub-plan 1 from .openceo/CURRENT_PLAN.md."
)


class NeedsExtraEnterTests(unittest.TestCase):
    def test_short_prompt_idle_in_box_needs_enter(self) -> None:
        content = "header\n❯ /usage\nfooter"
        self.assertTrue(TmuxRuntime._needs_extra_enter(content, "/usage"))

    def test_working_indicator_needs_no_enter(self) -> None:
        content = "❯ /usage\n· Generating… (esc to interrupt)"
        self.assertFalse(TmuxRuntime._needs_extra_enter(content, "/usage"))

    def test_long_wrapped_prompt_idle_needs_enter(self) -> None:
        """Long prompts wrap in the 80-col pane, so the full text never
        matches verbatim: the first line after ❯ is the signal. Without it
        the retry loop never fires and the turn stalls forever."""
        content = (
            "❯ Read CLAUDE.md and AGENTS.md in the repo root and follow all applicab\n"
            "le\n"
            "  rules before doing anything else.\n"
            "  Implement ONLY sub-plan 1 from .openceo/CURRENT_PLAN.md.\n"
            "  muse-spark-1.3-contributor · max · YOLO"
        )
        self.assertTrue(TmuxRuntime._needs_extra_enter(content, LONG_PROMPT))

    def test_long_prompt_working_needs_no_enter(self) -> None:
        content = (
            "❯ Read CLAUDE.md and AGENTS.md in the repo root and follow all applicab\n"
            "◈ Thinking (44s · esc to interrupt)"
        )
        self.assertFalse(TmuxRuntime._needs_extra_enter(content, LONG_PROMPT))

    def test_no_prompt_marker_needs_no_enter(self) -> None:
        self.assertFalse(TmuxRuntime._needs_extra_enter("plain shell output\n$", "/usage"))

    def test_empty_text_needs_no_enter(self) -> None:
        self.assertFalse(TmuxRuntime._needs_extra_enter("❯ ", "   "))
