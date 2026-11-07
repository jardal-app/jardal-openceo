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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from orchestration.provider_recovery import (  # noqa: E402
    provider_limit_reset, transient_provider_error)

LIMIT_440 = "You've hit your 5-hour limit · resets 4:40am (UTC)"
LIMIT_8PM = "You've hit your 5-hour limit · resets 8pm (UTC)"


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

    def test_hour_only_variants_parse(self) -> None:
        now = _at(1, 30)
        cases = {
            LIMIT_8PM: (20, 0),
            "Rate limit exceeded, resets at 8pm UTC": (20, 0),
            "usage limit hit. Reset 9 AM": (9, 0),
            "quota limit - resets 12am (UTC)": (0, 0),
            "quota limit - resets 12pm (UTC)": (12, 0),
        }
        for text, (hour, minute) in cases.items():
            with self.subTest(text=text):
                reset = provider_limit_reset(text, now=now)
                assert reset is not None
                self.assertEqual((reset.hour, reset.minute),
                                 (hour, minute))

    def test_hour_only_past_time_rolls_to_tomorrow(self) -> None:
        reset = provider_limit_reset(LIMIT_8PM, now=_at(21, 0))
        assert reset is not None
        self.assertEqual((reset.day, reset.hour, reset.minute), (3, 20, 0))

    def test_unparseable_is_none(self) -> None:
        for text in [
                "Failed to authenticate. API Error: 401 OAuth expired",
                "429 Too Many Requests (no clock given)",
                "resets 4:40am (UTC)",
                "resets 8pm (UTC)",
                "hour limit hit, try later",
                "limit resets 25:99am",
                "limit resets 8:99pm",
                "limit resets 13pm",
                "limit resets 0am",
                "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(
                    provider_limit_reset(text, now=_at(1, 30)))


class WindowQuotaTransientTests(unittest.TestCase):
    def test_window_quota_without_clock_is_transient(self) -> None:
        for text in [
                "You've hit your 5-hour limit, try again later",
                "hour limit hit, try later",
        ]:
            with self.subTest(text=text):
                self.assertTrue(transient_provider_error(text))

    def test_auth_and_context_limits_stay_non_transient(self) -> None:
        for text in [
                "Failed to authenticate. API Error: 401 OAuth expired",
                "subscription expired: you've hit your 5-hour limit",
                "You've hit your context token limit",
        ]:
            with self.subTest(text=text):
                self.assertFalse(transient_provider_error(text))


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

    async def test_planning_parks_hour_only_reset_with_margin(self) -> None:
        cycle = self._cycle(LIMIT_8PM)
        with tempfile.TemporaryDirectory() as tmp:
            before = datetime.now(timezone.utc)
            result = await cycle._planning(_ctx(Path(tmp)))  # noqa: SLF001
            after = datetime.now(timezone.utc)
        self.assertIsNone(result)
        cycle._park.assert_awaited_once()  # noqa: SLF001
        _, kwargs = cycle._park.call_args  # noqa: SLF001
        self.assertEqual(kwargs["reason"] if "reason" in kwargs
                         else cycle._park.call_args[0][1], "transient")
        resume_min = (kwargs["resume_in_min"] if "resume_in_min" in kwargs
                      else None)
        assert resume_min is not None
        lo = provider_limit_reset(LIMIT_8PM, now=after)
        hi = provider_limit_reset(LIMIT_8PM, now=before)
        assert lo is not None and hi is not None
        expected_lo = max(1, int((lo - after).total_seconds() / 60) + 5)
        expected_hi = max(1, int((hi - before).total_seconds() / 60) + 5)
        self.assertGreaterEqual(resume_min, expected_lo - 1)
        self.assertLessEqual(resume_min, expected_hi + 1)
        self.assertLessEqual(timedelta(minutes=resume_min),
                             timedelta(hours=24, minutes=6))

    async def test_planning_parks_window_quota_without_clock_transient(
            self) -> None:
        cycle = self._cycle("You've hit your 5-hour limit, try again later")
        with tempfile.TemporaryDirectory() as tmp:
            await cycle._planning(_ctx(Path(tmp)))  # noqa: SLF001
        _, kwargs = cycle._park.call_args  # noqa: SLF001
        kind = (kwargs["reason"] if "reason" in kwargs
                else cycle._park.call_args[0][1])
        self.assertEqual(kind, "transient")
        self.assertEqual(kwargs.get("resume_in_min"), 15)

    async def test_planning_keeps_config_without_reset(self) -> None:
        cycle = self._cycle("Failed to authenticate. 401 OAuth expired")
        with tempfile.TemporaryDirectory() as tmp:
            await cycle._planning(_ctx(Path(tmp)))  # noqa: SLF001
        _, kwargs = cycle._park.call_args  # noqa: SLF001
        kind = (kwargs["reason"] if "reason" in kwargs
                else cycle._park.call_args[0][1])
        self.assertEqual(kind, "config")

    async def test_builder_bypasses_park_cap_on_reset(self) -> None:
        for output in (LIMIT_440, LIMIT_8PM):
            with self.subTest(output=output):
                cycle = cycle_mod.TierSCycle(
                    MagicMock(), MagicMock(), MagicMock(), MagicMock())
                cycle._park_build = AsyncMock()  # noqa: SLF001
                with tempfile.TemporaryDirectory() as tmp:
                    ctx = _ctx(Path(tmp), provider_parks=99)
                    await cycle._park_builder_failure(  # noqa: SLF001
                        ctx, ExecResult(1, 1.0, output_tail=output))
                cycle._park_build.assert_awaited_once()  # noqa: SLF001
                args, kwargs = cycle._park_build.call_args  # noqa: SLF001
                kind = kwargs.get("reason", args[1] if len(args) > 1 else None)
                self.assertEqual(kind, "transient")
                resume_at = kwargs.get("resume_at")
                assert resume_at is not None
                parsed = datetime.fromisoformat(resume_at)
                reset = provider_limit_reset(output)
                assert reset is not None
                margin = (parsed - reset).total_seconds()
                self.assertGreater(margin, 3.5 * 60)
                self.assertLess(margin, 6 * 60)


if __name__ == "__main__":
    unittest.main()
