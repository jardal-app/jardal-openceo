"""Blocking-window resume + S quota chain (TASK-2026-00166).

TASK-2026-00178 gave the Tier S family its reserve semantics (the S
cycle, its arbiter and the final-review arbiter consume into the
reserve down to zero; A/B wait ``quota_reserved``; the C primary link
yields). This ticket fixes what it left:

- ``RateMonitorQuota.check`` always resumed at the 5h reset, even when
  the weekly window was the one blocking. It now resumes at the reset
  of the blocking window (one window: its own; both: the later, the
  earliest instant both may have replenished; race: the earlier, a
  fail-safe early wake).
- No test drove the real entry → preflight → arbitration chain. The
  regression below does: a real ``RateMonitor`` with scripted levels,
  the real ``RateMonitorQuota`` gate, and a simulated ``TierSCycle``
  (real preflight gate, real arbitration, spy executor).

No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s.cycle import TierSCycle, _Ctx  # noqa: E402
from orchestration.findings import Anchor, Finding  # noqa: E402
from orchestration.live import ExecResult, RateMonitorQuota  # noqa: E402
from rate_monitor import ProviderStatus, RateMonitor  # noqa: E402
from state import WAIT_QUOTA, WAIT_QUOTA_RESERVED  # noqa: E402

_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
_RESET_5H = timedelta(hours=1)
_RESET_WEEKLY = timedelta(days=1)
_MARGIN = timedelta(minutes=5)  # RateMonitorQuota default


def _settings(**overrides: object) -> SimpleNamespace:
    cfg: dict[str, object] = {
        "RATE_MAX_CONCURRENT_TICKETS": 3,
        "RATE_TIER_S_WEIGHT": 3,
        "RATE_NORMAL_WEIGHT": 1,
        "RATE_CODEX_RESERVE_5H": 0.20,
        "RATE_CODEX_RESERVE_WEEKLY": 0.05,
        "RATE_CLAUDE_RESERVE_5H": 0.20,
        "RATE_CLAUDE_RESERVE_WEEKLY": 0.05,
        "RATE_MUSE_RESERVE_5H": 0.10,
        "RATE_MUSE_RESERVE_WEEKLY": 0.05,
        "RATE_STATUS_STALE_SECONDS": 300,
    }
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


def _status(provider: str, five_h: float, weekly: float,
            reset_5h: timedelta = _RESET_5H,
            reset_weekly: timedelta = _RESET_WEEKLY) -> ProviderStatus:
    return ProviderStatus(
        provider=provider,
        remaining_5h=five_h,
        remaining_weekly=weekly,
        resets_5h_at=_NOW + reset_5h,
        resets_weekly_at=_NOW + reset_weekly,
        updated_at=_NOW,
    )


def _monitor(levels: dict[str, tuple[float, float]],
             reset_5h: timedelta = _RESET_5H,
             reset_weekly: timedelta = _RESET_WEEKLY) -> RateMonitor:
    monitor = RateMonitor(_settings())

    async def fake_get_status(provider: str) -> ProviderStatus:
        five_h, weekly = levels.get(provider, (1.0, 1.0))
        return _status(provider, five_h, weekly, reset_5h, reset_weekly)

    monitor.get_status = fake_get_status  # type: ignore[method-assign]
    return monitor


def _expect(reset: timedelta) -> str:
    return (_NOW + reset + _MARGIN).isoformat()


class BlockingResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_h_exhaustion_resumes_at_the_5h_reset(self) -> None:
        quota = RateMonitorQuota(_monitor({"claude": (0.0, 0.9)}))
        verdicts = await quota.check(["claudej"])
        self.assertFalse(verdicts["claudej"].ok)
        self.assertEqual(verdicts["claudej"].resume_at,
                         _expect(_RESET_5H))
        self.assertIn("5h=0%", verdicts["claudej"].detail)

    async def test_weekly_exhaustion_resumes_at_the_weekly_reset(self) -> None:
        quota = RateMonitorQuota(_monitor({"claude": (0.9, 0.0)}))
        verdicts = await quota.check(["claudej"])
        self.assertFalse(verdicts["claudej"].ok)
        self.assertEqual(verdicts["claudej"].resume_at,
                         _expect(_RESET_WEEKLY))
        self.assertIn("weekly=0%", verdicts["claudej"].detail)

    async def test_both_exhausted_resumes_at_the_later_reset(self) -> None:
        quota = RateMonitorQuota(_monitor({"claude": (0.0, 0.0)}))
        verdicts = await quota.check(["claudej"])
        # Weekly resets later here: the earliest instant both may be back.
        self.assertEqual(verdicts["claudej"].resume_at,
                         _expect(_RESET_WEEKLY))

    async def test_both_exhausted_weekly_sooner_resumes_at_5h(self) -> None:
        quota = RateMonitorQuota(
            _monitor({"claude": (0.0, 0.0)},
                     reset_5h=timedelta(days=2),
                     reset_weekly=timedelta(hours=3)))
        verdicts = await quota.check(["claudej"])
        self.assertEqual(verdicts["claudej"].resume_at,
                         _expect(timedelta(days=2)))

    async def test_race_falls_back_to_the_earlier_reset(self) -> None:
        monitor = _monitor({"claude": (0.9, 0.9)})
        quota = RateMonitorQuota(monitor)
        with patch.object(monitor, "can_launch",
                          new=AsyncMock(return_value=False)):
            verdicts = await quota.check(["claudej"])
        self.assertFalse(verdicts["claudej"].ok)
        self.assertEqual(verdicts["claudej"].resume_at,
                         _expect(_RESET_5H))


class _SpyExecutor:
    def __init__(self, verdict_tail: str = "FALSO — not a real bug") -> None:
        self.calls: list[str] = []
        self.verdict_tail = verdict_tail

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del prompt, workdir, timeout_s
        self.calls.append(role)
        return ExecResult(0, 1.0, output_tail=self.verdict_tail)

    async def cleanup(self, role: str) -> None:
        del role


def _cycle(monitor: RateMonitor, spy: _SpyExecutor) -> TierSCycle:
    return TierSCycle(MagicMock(), MagicMock(), MagicMock(), spy,
                      quota=RateMonitorQuota(monitor), inbox=None)


def _ctx(roles: dict[str, dict[str, Any]]) -> _Ctx:
    return _Ctx(
        ticket={"task_id": "t", "project": "acme"}, repo=Path("/tmp/166"),
        ref="s", run_id="r", branch="b", base_branch="main",
        budgets={}, timeouts={"arbiter": 10}, waiting_cfg={},
        auto_merge=False, budget_exclude=[], gate_specs=[], roles=roles)


def _finding() -> Finding:
    return Finding(
        id=7, severity="CRITICAL", category="correctness",
        anchors=[Anchor(raw="x.py:1", path="x.py", line=1,
                        base=False, criterion=None)],
        problem="wrong total", fix="recompute", strength="strong",
        families=["fa"], quotes=["total = 0", "return total"])


class QuotaChainTests(unittest.IsolatedAsyncioTestCase):
    """Entry → preflight → arbitration with the real gate (acceptance)."""

    @staticmethod
    def _ticket(tool: str) -> dict[str, Any]:
        return {"task_id": "t", "coding_tool": tool, "status": "queued"}

    async def test_chain_agrees_inside_the_reserve(self) -> None:
        monitor = _monitor({"claude": (0.15, 0.9), "codex": (0.12, 0.9),
                            "muse": (0.5, 0.9)})
        spy = _SpyExecutor()
        cycle = _cycle(monitor, spy)
        # Entry: S admitted, A/B wait on the reserve.
        admitted, _, reason = await monitor.admission_for_ticket(
            self._ticket("tier_s"), active_tickets=[])
        self.assertTrue(admitted)
        self.assertIsNone(reason)
        for tool in ("tier_a", "tier_b"):
            can, _, why = await monitor.admission_for_ticket(
                self._ticket(tool), active_tickets=[])
            self.assertFalse(can)
            self.assertEqual(why, WAIT_QUOTA_RESERVED)
        # Preflight: the real gate lets the S roles through.
        ctx = _ctx({"planner": {"account": "claudej"},
                    "plan_reviewer": {"account": "codexj"}})
        self.assertIsNone(await cycle._quota_gate(ctx))
        # Arbitration: the arbiter runs, no dispute recorded.
        arb = _ctx({"arbiter": {"account": "claudej"}})
        await cycle._arbitrate(arb, [_finding()], {})
        self.assertEqual(spy.calls, ["arbiter"])
        self.assertEqual(arb.arbitrations,
                         [{"finding": 7, "verdict": "false",
                           "reason": "not a real bug"}])

    async def test_preflight_parks_with_the_blocking_window_resume(
            self) -> None:
        cases = {
            "weekly": ({"claude": (0.9, 0.0), "codex": (0.9, 0.9)},
                       _expect(_RESET_WEEKLY), "weekly=0%"),
            "five_h": ({"claude": (0.0, 0.9), "codex": (0.9, 0.9)},
                       _expect(_RESET_5H), "5h=0%"),
        }
        for name, (levels, resume, detail) in cases.items():
            with self.subTest(window=name):
                monitor = _monitor(levels)
                cycle = _cycle(monitor, _SpyExecutor())
                # Entry agrees: real exhaustion, not the reserve.
                admitted, _, reason = await monitor.admission_for_ticket(
                    self._ticket("tier_s"), active_tickets=[])
                self.assertFalse(admitted)
                self.assertEqual(reason, WAIT_QUOTA)
                # Preflight: the real gate parks on the blocking window.
                ctx = _ctx({"planner": {"account": "claudej"},
                            "plan_reviewer": {"account": "codexj"}})
                hit = await cycle._quota_gate(ctx)
                assert hit is not None
                self.assertEqual(hit[0], resume)
                self.assertIn("quota: claudej exhausted", hit[1])
                self.assertIn(detail, hit[1])

    async def test_arbitration_defers_without_quota(self) -> None:
        monitor = _monitor({"claude": (0.0, 0.9)})
        spy = _SpyExecutor()
        cycle = _cycle(monitor, spy)
        ctx = _ctx({"arbiter": {"account": "claudej"}})
        await cycle._arbitrate(ctx, [_finding()], {})
        self.assertEqual(spy.calls, [])
        self.assertEqual(ctx.arbitrations,
                         [{"finding": 7, "verdict": "disputed",
                           "reason": "no arbiter quota (→ inbox, promotes)"}])


if __name__ == "__main__":
    unittest.main()
