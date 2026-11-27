"""Attempt deadline covers the whole A/B/C session (ticket #1034).

The simple-tier attempt spans the entire ``_run_simple_tool`` session
(build + continuation + review + fixes + gates + merge), so arming it
with one builder turn (1800 s + grace = 2160 s) fires ``deadline_missed``
on healthy sessions. The budget below follows the Tier S sum pattern.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    SIMPLE_MERGE_SLACK_SECONDS,
    SIMPLE_REVIEW_COUNT,
    SIMPLE_REVIEW_MAX_FIX_TURNS,
    DevSessionManager,
    simple_attempt_timeout_s,
)
from reliability import deadlines as deadline_mod  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402

_NOW = datetime(2026, 11, 27, 16, 43, 0, tzinfo=timezone.utc)
_DEV_TURN = 1800


def _tier_c_config(
    *,
    reviewer_minutes: int = 15,
    gate_retries: int = 2,
    gates: list[dict[str, Any]] | None = None,
    oracle_minutes: int | None = None,
) -> dict[str, Any]:
    dev: dict[str, Any] = {
        "tier_s": {"budgets": {"gate_retries": gate_retries}},
        "gates": gates if gates is not None else [],
    }
    if oracle_minutes is not None:
        dev["oracle_timeout_minutes"] = oracle_minutes
    return {
        "dev": dev,
        "review": {"review": {"reviewer_timeout_minutes": reviewer_minutes}},
    }


def _phase_sum(
    *,
    dev_turn: int = _DEV_TURN,
    reviewer_minutes: int = 15,
    gates_minutes: int = 0,
    oracle_minutes: int = 10,
) -> int:
    """Nominal configured phase budgets, once each (acceptance floor)."""
    builder = (2 + SIMPLE_REVIEW_MAX_FIX_TURNS) * dev_turn
    review = SIMPLE_REVIEW_COUNT * reviewer_minutes * 60
    return (builder + review + gates_minutes * 60 + oracle_minutes * 60
            + SIMPLE_MERGE_SLACK_SECONDS)


class SimpleBudgetTests(unittest.TestCase):
    def test_default_budget_covers_phase_sum(self) -> None:
        budget = simple_attempt_timeout_s(_DEV_TURN, _tier_c_config())
        self.assertGreaterEqual(budget, _phase_sum())
        # Old math armed 1800 s; the session budget must exceed one turn.
        self.assertGreater(budget, _DEV_TURN)

    def test_configured_timeouts_raise_the_budget(self) -> None:
        base = simple_attempt_timeout_s(_DEV_TURN, _tier_c_config())
        wider = simple_attempt_timeout_s(
            _DEV_TURN,
            _tier_c_config(
                reviewer_minutes=30,
                gates=[
                    {"name": "unit", "cmd": "pytest -q",
                     "timeout_minutes": 20},
                ],
                oracle_minutes=12,
            ),
        )
        self.assertGreater(wider, base)
        self.assertGreaterEqual(
            wider,
            _phase_sum(reviewer_minutes=30, gates_minutes=20,
                       oracle_minutes=12))

    def test_unreadable_config_falls_back_to_one_turn(self) -> None:
        self.assertGreaterEqual(
            simple_attempt_timeout_s(_DEV_TURN, None), _DEV_TURN)
        self.assertGreaterEqual(
            simple_attempt_timeout_s(_DEV_TURN, {"dev": {"gates": "nope"}}),
            _DEV_TURN)
        self.assertGreaterEqual(
            simple_attempt_timeout_s("nope", None), 1800)


class HealthySessionTests(unittest.IsolatedAsyncioTestCase):
    def _repo(self, deadline: datetime) -> Any:
        row = {
            "task_id": "tier-c-healthy",
            "status": TaskStatus.IN_PROGRESS.value,
            "attempt_id": "tier-c-healthy-g0-w0",
            "attempt_deadline_at": deadline,
        }

        class _Repo:
            async def list_tasks_by_status(
                self, status: TaskStatus, limit: int = 200,
            ) -> list[dict]:
                if status == TaskStatus.IN_PROGRESS:
                    return [dict(row)]
                return []

            async def get_task(self, task_id: str) -> dict | None:
                return dict(row) if task_id == row["task_id"] else None

        return _Repo()

    async def test_build_review_fix_beyond_36min_does_not_miss(self) -> None:
        """A healthy 2 h tier_c session outlives the old 36 min math."""
        old_deadline = deadline_mod.attempt_deadline_for(_NOW, _DEV_TURN)
        self.assertEqual(old_deadline, _NOW + timedelta(seconds=2160))
        budget = simple_attempt_timeout_s(_DEV_TURN, _tier_c_config())
        fresh = deadline_mod.attempt_deadline_for(_NOW, budget)
        # The session advances through build + review + fix for 2 h.
        two_hours = _NOW + timedelta(hours=2)
        self.assertLess(old_deadline, two_hours)
        self.assertGreater(fresh, two_hours)
        recorded: list[dict] = []

        async def _record(**fields: object) -> str:
            recorded.append(dict(fields))
            return f"incident-{len(recorded)}"

        watch = deadline_mod.DeadlineWatch(
            self._repo(fresh), record=_record, clock=lambda: two_hours)
        try:
            self.assertEqual(await watch.check_due(two_hours), [])
            self.assertEqual(recorded, [])
        finally:
            await watch.close()

    async def test_stalled_attempt_past_budget_still_misses(self) -> None:
        """No milestones, no commits, past the new budget: critical miss."""
        budget = simple_attempt_timeout_s(_DEV_TURN, _tier_c_config())
        fresh = deadline_mod.attempt_deadline_for(_NOW, budget)
        late = fresh + timedelta(seconds=1)
        recorded: list[dict] = []

        async def _record(**fields: object) -> str:
            recorded.append(dict(fields))
            return f"incident-{len(recorded)}"

        watch = deadline_mod.DeadlineWatch(
            self._repo(fresh), record=_record, clock=lambda: late)
        try:
            found = await watch.check_due(late)
            self.assertEqual(len(found), 1)
            self.assertEqual(recorded[0]["deadline_type"], "attempt")
            self.assertEqual(recorded[0]["severity"], "critical")
        finally:
            await watch.close()


class ExtendPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_extend_rewrites_only_the_deadline(self) -> None:
        patches: list[tuple[str, dict]] = []

        async def patch(task_id: str, updates: dict) -> None:
            patches.append((task_id, dict(updates)))

        first = await deadline_mod.mark_attempt_start(
            patch, task_id="t1", attempt_id="t1-g0-w0",
            timeout_seconds=600, started_at=_NOW)
        assert first is not None
        later = await deadline_mod.extend_attempt_deadline(
            patch, task_id="t1", timeout_seconds=3600,
            started_at=_NOW + timedelta(minutes=30))
        assert later is not None
        self.assertGreater(later, first)
        # The rearm keeps the live attempt_id untouched.
        self.assertEqual(patches[-1], ("t1", {"attempt_deadline_at": later}))

    async def test_extend_without_patcher_is_fail_open(self) -> None:
        self.assertIsNone(await deadline_mod.extend_attempt_deadline(
            None, task_id="t1", timeout_seconds=60))


class MarkStartTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> tuple[DevSessionManager, AsyncMock]:
        settings = SimpleNamespace(
            DEV_EXECUTION_MODE="real", DEV_TURN_TIMEOUT_SECONDS=_DEV_TURN)
        manager = DevSessionManager(settings, SimpleNamespace(), repo=None)
        patch = AsyncMock()
        service = SimpleNamespace(patch_task=patch, wake_dispatcher=None)
        manager.set_task_service(service)
        return manager, patch

    async def test_mark_start_arms_total_budget_for_tier_c(self) -> None:
        manager, patch = self._manager()
        task = {"task_id": "t-tier-c", "params": {}}
        armed = await manager._deadline_mark_start(
            task, CodingTool.TIER_C, _tier_c_config())
        self.assertTrue(armed)
        _, updates = patch.await_args.args
        deadline = updates["attempt_deadline_at"]
        # Total budget + grace must clear a healthy 2 h session.
        now = datetime.now(timezone.utc)
        self.assertGreater(
            deadline, now + timedelta(hours=2) - timedelta(seconds=60))

    async def test_mark_start_skips_tier_s(self) -> None:
        manager, patch = self._manager()
        armed = await manager._deadline_mark_start(
            {"task_id": "t-tier-s", "params": {}}, CodingTool.TIER_S,
            _tier_c_config())
        self.assertFalse(armed)
        patch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
