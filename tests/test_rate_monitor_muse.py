"""Unit tests for the muse quota monitor (offline fixtures, no tmux)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.routing import resolved_tool_name  # noqa: E402
from rate_monitor import RateMonitor  # noqa: E402


MUSE_USAGE = """\
┌──────────────────────────────────────────────────────┐
│  Session usage                                       │
│                                                      │
│    Input      0                                      │
│    Cached     0                                      │
│    Output     0                                      │
│    Total      0                                      │
│                                                      │
│    Turns         0                                   │
│    Subagents  none                                   │
│                                                      │
│  Subscription · Muse Code High Usage                 │
│    Current        0% used · Resets at 4:54 PM        │
│    Weekly         4% used · Resets Oct 5 at 12:00 AM │
│    as of 12:05 PM                                    │
└──────────────────────────────────────────────────────┘
"""


class ParseMuseUsageTests(unittest.TestCase):
    def test_parses_current_and_weekly(self) -> None:
        status = RateMonitor._parse_muse_usage(MUSE_USAGE)
        self.assertEqual(status.provider, "muse")
        self.assertAlmostEqual(status.remaining_5h, 1.0)
        self.assertAlmostEqual(status.remaining_weekly, 0.96)
        self.assertEqual(
            (status.resets_5h_at.hour, status.resets_5h_at.minute), (16, 54)
        )
        self.assertEqual(
            (status.resets_weekly_at.month, status.resets_weekly_at.day,
             status.resets_weekly_at.hour, status.resets_weekly_at.minute),
            (10, 5, 0, 0),
        )

    def test_last_output_wins_in_scrollback(self) -> None:
        older = MUSE_USAGE.replace("0% used · Resets at 4:54 PM",
                                   "50% used · Resets at 4:54 PM")
        status = RateMonitor._parse_muse_usage(older + "\n" + MUSE_USAGE)
        self.assertAlmostEqual(status.remaining_5h, 1.0)

    def test_empty_content_keeps_full_availability_defaults(self) -> None:
        status = RateMonitor._parse_muse_usage("❯\n")
        self.assertAlmostEqual(status.remaining_5h, 1.0)
        self.assertAlmostEqual(status.remaining_weekly, 1.0)

    def test_current_reset_rolls_to_next_day_when_past(self) -> None:
        now = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        reset = RateMonitor._muse_current_to_utc("4:54 PM", now)
        self.assertEqual((reset.day, reset.hour, reset.minute), (5, 16, 54))

    def test_retired_muse_value_no_longer_resolves(self) -> None:
        # TASK-2026-00138: pre-v2 provider values are not routing.
        self.assertEqual(resolved_tool_name({"coding_tool": "muse"}), "")
        self.assertEqual(resolved_tool_name({"coding_tool": "tier_c"}), "tier_c")


if __name__ == "__main__":
    unittest.main()
