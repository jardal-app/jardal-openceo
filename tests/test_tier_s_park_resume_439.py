"""Tier S parks store native datetimes on the task (TASK-2026-00439).

Contract fixed here: the dispatcher due query only matches native
datetimes, so ``_park`` converts the ISO resume string before the task
patch. The run doc and the outcome keep the ISO string (no contract
change); unparseable input stays verbatim so the dispatcher holds it
with a warning instead of launching blind.
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


def _ctx(repo: Path, **over: Any) -> Any:
    base: dict[str, Any] = dict(
        ticket={"task_id": "t", "title": "T", "description": "D",
                "acceptance": ["a1"], "params": {}},
        repo=repo, ref="t", run_id="run", branch="b", base_branch="main",
        base_sha="s", budgets={}, timeouts={}, waiting_cfg={},
        auto_merge=True, budget_exclude=[], gate_specs=[], roles={})
    base.update(over)
    return cycle_mod._Ctx(**base)  # noqa: SLF001


def _cycle() -> Any:
    tasks = MagicMock()
    tasks.task_status = AsyncMock(return_value="Open")
    tasks.patch_task = AsyncMock()
    runs = MagicMock()
    runs.patch_run = AsyncMock()
    return cycle_mod.TierSCycle(runs, MagicMock(), tasks, MagicMock())


class ParkResumeTypeTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_patch_gets_datetime_run_keeps_string(self) -> None:
        cycle = _cycle()
        iso = "2026-11-02T04:45:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            outcome = await cycle._park(  # noqa: SLF001
                _ctx(Path(tmp)), "transient", "limit resets 4:40am",
                resume_at=iso, patch_run=True, skip_boundary=True)
        task_args, _ = cycle._tasks.patch_task.call_args  # noqa: SLF001
        stored = task_args[1]["resume_at"]
        self.assertIsInstance(stored, datetime)
        self.assertEqual(stored,
                         datetime(2026, 11, 2, 4, 45, tzinfo=timezone.utc))
        run_args, _ = cycle._runs.patch_run.call_args  # noqa: SLF001
        self.assertEqual(run_args[1]["waiting"]["resume_at"], iso)
        self.assertEqual(outcome.resume_at, iso)

    async def test_resume_in_min_stores_datetime(self) -> None:
        cycle = _cycle()
        before = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            await cycle._park(  # noqa: SLF001
                _ctx(Path(tmp)), "transient", "backoff",
                resume_in_min=30, patch_run=False, skip_boundary=True)
        task_args, _ = cycle._tasks.patch_task.call_args  # noqa: SLF001
        stored = task_args[1]["resume_at"]
        self.assertIsInstance(stored, datetime)
        assert isinstance(stored, datetime)
        delta = (stored - before).total_seconds()
        self.assertGreater(delta, 29 * 60)
        self.assertLess(delta, 31 * 60)

    async def test_garbage_stays_verbatim_for_dispatcher_hold(self) -> None:
        cycle = _cycle()
        with tempfile.TemporaryDirectory() as tmp:
            await cycle._park(  # noqa: SLF001
                _ctx(Path(tmp)), "transient", "bad clock",
                resume_at="not-a-datetime", patch_run=False,
                skip_boundary=True)
        task_args, _ = cycle._tasks.patch_task.call_args  # noqa: SLF001
        self.assertEqual(task_args[1]["resume_at"], "not-a-datetime")


if __name__ == "__main__":
    unittest.main()
