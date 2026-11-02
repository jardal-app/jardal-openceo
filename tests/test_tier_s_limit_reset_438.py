"""Provider limit resets resume automatically (TASK-2026-00438).

Contract fixed here: a quota message naming its reset clock (e.g. a
5-hour limit) parks transient with resume at reset + 5 min instead of
parking config/blocked forever. Parsing is absolute-clock with next-day
rollover; the dispatcher backup fires the resume (no cron, no polling).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from orchestration.provider_recovery import provider_limit_reset  # noqa: E402

LIMIT_440 = "You've hit your 5-hour limit · resets 4:40am (UTC)"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 2, hour, minute, tzinfo=timezone.utc)


def _ctx(repo: Path, **over: Any) -> Any:
    base: dict[str, Any] = dict(
        ticket={"task_id": "t", "title": "T", "description": "D",
                "acceptance": ["a1"], "params": {}},
        repo=repo, ref="t", run_id="run", branch="b", base_branch="main",
        base_sha="s", budgets={}, timeouts={}, waiting_cfg={},
        auto_merge=True, budget_exclude=[], gate_specs=[], roles={})
    base.update(over)
    return cycle_mod._Ctx(**base)  # noqa: SLF001


class LimitResetParseTests(unittest.TestCase):
    def test_absolute_reset_same_day(self) -> None:
        self.assertEqual(
            provider_limit_reset(LIMIT_440, now=_at(1, 30)),
            _at(4, 40))

    def test_past_time_rolls_to_tomorrow(self) -> None:
        reset = provider_limit_reset(LIMIT_440, now=_at(5, 0))
        assert reset is not None
        self.assertEqual((reset.day, reset.hour, reset.minute), (3, 4, 40))

    def test_variants_parse(self) -> None:
        now = _at(1, 30)
        cases = {
            "Rate limit exceeded, resets at 11:05pm UTC": (23, 5),
            "usage limit hit. Reset 9:15 AM": (9, 15),
            "quota limit - resets 12:00am (UTC)": (0, 0),
        }
        for text, (hour, minute) in cases.items():
            with self.subTest(text=text):
                reset = provider_limit_reset(text, now=now)
                assert reset is not None
                self.assertEqual((reset.hour, reset.minute),
                                 (hour, minute))

    def test_unparseable_is_none(self) -> None:
        for text in [
                "Failed to authenticate. API Error: 401 OAuth expired",
                "429 Too Many Requests (no clock given)",
                "resets 4:40am (UTC)",
                "hour limit hit, try later",
                "limit resets 25:99am",
                "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(
                    provider_limit_reset(text, now=_at(1, 30)))


class LimitResetWiringTests(unittest.IsolatedAsyncioTestCase):
    def _cycle(self, output: str) -> Any:
        executor = MagicMock()
        executor.run = AsyncMock(return_value=ExecResult(
            1, 1.0, output_tail=output))
        cycle = cycle_mod.TierSCycle(
            MagicMock(), MagicMock(), MagicMock(), executor)
        cycle._record_session = AsyncMock()  # noqa: SLF001
        cycle._park = AsyncMock()  # noqa: SLF001
        return cycle

    async def test_planning_parks_transient_with_reset_resume(self) -> None:
        cycle = self._cycle(LIMIT_440)
        with tempfile.TemporaryDirectory() as tmp:
            result = await cycle._planning(_ctx(Path(tmp)))  # noqa: SLF001
        self.assertIsNone(result)
        cycle._park.assert_awaited_once()  # noqa: SLF001
        _, kwargs = cycle._park.call_args  # noqa: SLF001
        self.assertEqual(kwargs["reason"] if "reason" in kwargs
                         else cycle._park.call_args[0][1], "transient")
        resume_min = (kwargs["resume_in_min"] if "resume_in_min" in kwargs
                      else None)
        assert resume_min is not None
        self.assertGreaterEqual(resume_min, 1)
        self.assertLessEqual(resume_min, 24 * 60 + 5)

    async def test_planning_keeps_config_without_reset(self) -> None:
        cycle = self._cycle("Failed to authenticate. 401 OAuth expired")
        with tempfile.TemporaryDirectory() as tmp:
            await cycle._planning(_ctx(Path(tmp)))  # noqa: SLF001
        _, kwargs = cycle._park.call_args  # noqa: SLF001
        kind = (kwargs["reason"] if "reason" in kwargs
                else cycle._park.call_args[0][1])
        self.assertEqual(kind, "config")

    async def test_builder_bypasses_park_cap_on_reset(self) -> None:
        cycle = cycle_mod.TierSCycle(
            MagicMock(), MagicMock(), MagicMock(), MagicMock())
        cycle._park_build = AsyncMock()  # noqa: SLF001
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(Path(tmp), provider_parks=99)
            await cycle._park_builder_failure(  # noqa: SLF001
                ctx, ExecResult(1, 1.0, output_tail=LIMIT_440))
        cycle._park_build.assert_awaited_once()  # noqa: SLF001
        args, kwargs = cycle._park_build.call_args  # noqa: SLF001
        kind = kwargs.get("reason", args[1] if len(args) > 1 else None)
        self.assertEqual(kind, "transient")
        resume_at = kwargs.get("resume_at")
        assert resume_at is not None
        parsed = datetime.fromisoformat(resume_at)
        reset = provider_limit_reset(LIMIT_440)
        assert reset is not None
        margin = (parsed - reset).total_seconds()
        self.assertGreater(margin, 3.5 * 60)
        self.assertLess(margin, 6 * 60)


if __name__ == "__main__":
    unittest.main()
