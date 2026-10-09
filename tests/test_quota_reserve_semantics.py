"""Quota reserve semantics (TASK-2026-00178, policy §28).

The reserves (``RATE_*_RESERVE_*``) are Tier S family quota. Before this
ticket ``RateMonitorQuota.check`` -- the gate INSIDE the Tier S cycle, its
arbiter and the objective final-review arbiter -- also honored them, so an
S ticket the dispatcher admitted into the reserve parked itself a moment
later with ``quota: <account> exhausted``. Contract:

- ``can_launch`` honors the reserve by default and ignores it with
  ``honor_reserve=False``; real exhaustion blocks everyone.
- ``RateMonitorQuota.check`` (S family) consumes into the reserve.
- Tier A/B admission still waits ``quota_reserved`` inside the reserve.
- The Tier C primary link (muse) yields to the Muse reserve and the same
  C ticket runs on the next chain link (continuity, never an escalation).

No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import DevSessionManager  # noqa: E402
from orchestration.live import RateMonitorQuota  # noqa: E402
from rate_monitor import ProviderStatus, RateMonitor  # noqa: E402
from state import WAIT_QUOTA_RESERVED, CodingTool  # noqa: E402

_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

#: The live reserves after TASK-2026-00148 / 00178.
_LIVE_RESERVES = {
    "RATE_CODEX_RESERVE_5H": 0.20, "RATE_CODEX_RESERVE_WEEKLY": 0.05,
    "RATE_CLAUDE_RESERVE_5H": 0.20, "RATE_CLAUDE_RESERVE_WEEKLY": 0.05,
    "RATE_MUSE_RESERVE_5H": 0.10, "RATE_MUSE_RESERVE_WEEKLY": 0.05,
}


def _settings(**overrides: object) -> SimpleNamespace:
    cfg: dict[str, object] = {
        "RATE_MAX_CONCURRENT_TICKETS": 3,
        "RATE_TIER_S_WEIGHT": 3,
        "RATE_NORMAL_WEIGHT": 1,
        "RATE_CODEX_RESERVE_5H": 0.0,
        "RATE_CODEX_RESERVE_WEEKLY": 0.0,
        "RATE_CLAUDE_RESERVE_5H": 0.0,
        "RATE_CLAUDE_RESERVE_WEEKLY": 0.0,
        "RATE_MUSE_RESERVE_5H": 0.0,
        "RATE_MUSE_RESERVE_WEEKLY": 0.0,
        "RATE_STATUS_STALE_SECONDS": 300,
    }
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


def _status(provider: str, five_h: float, weekly: float) -> ProviderStatus:
    return ProviderStatus(
        provider=provider,
        remaining_5h=five_h,
        remaining_weekly=weekly,
        resets_5h_at=_NOW + timedelta(hours=1),
        resets_weekly_at=_NOW + timedelta(days=1),
        updated_at=_NOW,
    )


def _monitor(levels: dict[str, tuple[float, float]],
             **overrides: object) -> RateMonitor:
    monitor = RateMonitor(_settings(**{**_LIVE_RESERVES, **overrides}))

    async def fake_get_status(provider: str) -> ProviderStatus:
        five_h, weekly = levels.get(provider, (1.0, 1.0))
        return _status(provider, five_h, weekly)

    monitor.get_status = fake_get_status  # type: ignore[method-assign]
    return monitor


class CanLaunchReserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_is_honored_by_default(self) -> None:
        monitor = _monitor({"claude": (0.15, 0.9), "codex": (0.9, 0.04),
                            "muse": (0.08, 0.9)})
        self.assertFalse(await monitor.can_launch("claude"))   # 5h inside
        self.assertFalse(await monitor.can_launch("codex"))    # weekly inside
        self.assertFalse(await monitor.can_launch("muse"))     # 5h inside

    async def test_honor_reserve_false_consumes_the_reserve(self) -> None:
        monitor = _monitor({"claude": (0.15, 0.9), "codex": (0.9, 0.04),
                            "muse": (0.08, 0.9)})
        for provider in ("claude", "codex", "muse"):
            with self.subTest(provider=provider):
                self.assertTrue(
                    await monitor.can_launch(provider, honor_reserve=False))

    async def test_real_exhaustion_blocks_even_without_the_reserve(self) -> None:
        monitor = _monitor({"claude": (0.0, 0.9), "codex": (0.9, 0.0),
                            "muse": (0.0, 0.0)})
        for provider in ("claude", "codex", "muse"):
            with self.subTest(provider=provider):
                self.assertFalse(
                    await monitor.can_launch(provider, honor_reserve=False))

    async def test_untracked_providers_are_always_available(self) -> None:
        monitor = _monitor({})
        self.assertTrue(await monitor.can_launch("gemini"))
        self.assertTrue(await monitor.can_launch("gemini", honor_reserve=False))


class TierSFamilyGateTests(unittest.IsolatedAsyncioTestCase):
    """RateMonitorQuota: the gate inside the S cycle and both arbiters."""

    async def test_s_planner_and_reviewer_pass_inside_the_reserve(self) -> None:
        # Exactly the live failure: A/B would wait here, S must not.
        quota = RateMonitorQuota(_monitor(
            {"claude": (0.15, 0.9), "codex": (0.12, 0.9)}))
        verdicts = await quota.check(["claudej", "codexj"])
        self.assertTrue(verdicts["claudej"].ok)
        self.assertTrue(verdicts["codexj"].ok)

    async def test_weekly_reserve_does_not_park_s_either(self) -> None:
        quota = RateMonitorQuota(_monitor(
            {"claude": (0.9, 0.04), "codex": (0.9, 0.03)}))
        verdicts = await quota.check(["claudej", "codexj"])
        self.assertTrue(all(item.ok for item in verdicts.values()))

    async def test_s_gate_still_parks_on_real_exhaustion(self) -> None:
        quota = RateMonitorQuota(_monitor({"claude": (0.0, 0.9)}))
        verdicts = await quota.check(["claudej"])
        self.assertFalse(verdicts["claudej"].ok)
        self.assertIsNotNone(verdicts["claudej"].resume_at)
        self.assertIn("5h=0%", verdicts["claudej"].detail)

    async def test_arbiter_account_consumes_the_reserve(self) -> None:
        quota = RateMonitorQuota(_monitor({"claude": (0.01, 0.01)}))
        verdicts = await quota.check(["claudej"])
        self.assertTrue(verdicts["claudej"].ok)

    async def test_unknown_account_fails_open(self) -> None:
        quota = RateMonitorQuota(_monitor({}))
        verdicts = await quota.check(["mystery"])
        self.assertTrue(verdicts["mystery"].ok)


class AdmissionStillHonorsTheReserveTests(unittest.IsolatedAsyncioTestCase):
    """The same levels: A/B wait, S is admitted (admission unchanged)."""

    @staticmethod
    def _ticket(tool: str) -> dict:
        return {"task_id": "t", "coding_tool": tool, "status": "queued"}

    async def test_a_and_b_wait_quota_reserved_where_s_passes(self) -> None:
        monitor = _monitor({"claude": (0.15, 0.9), "codex": (0.12, 0.9),
                            "muse": (0.5, 0.9)})
        for tool in ("tier_a", "tier_b"):
            with self.subTest(tool=tool):
                can, _, reason = await monitor.admission_for_ticket(
                    self._ticket(tool), active_tickets=[])
                self.assertFalse(can)
                self.assertEqual(reason, WAIT_QUOTA_RESERVED)
        can, _, reason = await monitor.admission_for_ticket(
            self._ticket("tier_s"), active_tickets=[])
        self.assertTrue(can)
        self.assertIsNone(reason)
        # ...and the S gate inside the cycle agrees with the admission.
        verdicts = await RateMonitorQuota(monitor).check(["claudej", "codexj"])
        self.assertTrue(all(item.ok for item in verdicts.values()))


class TierCPrimaryLinkTests(unittest.IsolatedAsyncioTestCase):
    """C yields to the Muse reserve: the same ticket runs on the next link."""

    def _manager(self, monitor: RateMonitor) -> DevSessionManager:
        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock(), rate_monitor=monitor)

    def _config(self) -> dict:
        return {
            "project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]},
            "dev": {
                "tiers": {"tier_c": {
                    "chain": ["muse", "dsh", "claudep"],
                    "muse": {"command": ["musej", "exec"]},
                    "dsh": {"command": ["node", "/opt/dsh/bin.js", "-"],
                            "env": {"DSH_HOME": "/tmp/dsh"}},
                    "claudep": {"command": ["claudep"]},
                }},
                "providers": {"muse": {"command": ["musej"], "cwd": "/tmp/repo",
                                       "env": {}}},
            },
        }

    async def test_muse_inside_its_reserve_hands_c_to_the_next_link(self) -> None:
        manager = self._manager(_monitor({"muse": (0.08, 0.9)}))
        self.assertFalse(await manager._muse_available())
        with patch.object(DevSessionManager, "_command_available",
                          side_effect=lambda argv: argv[0] == "node"):
            executor = await manager._resolve_tier_executor(
                CodingTool.TIER_C, self._config())
        self.assertEqual(executor.label, "tier_c:dsh")

    async def test_muse_above_its_reserve_stays_the_primary_link(self) -> None:
        manager = self._manager(_monitor({"muse": (0.4, 0.9)}))
        self.assertTrue(await manager._muse_available())
        executor = await manager._resolve_tier_executor(
            CodingTool.TIER_C, self._config())
        self.assertEqual(executor.label, "tier_c:muse")


class LiveReserveConfigTests(unittest.TestCase):
    """The shipped example documents the values this ticket reasons about."""

    def test_env_example_sets_every_reserve(self) -> None:
        text = (CORE / ".env.example").read_text(encoding="utf-8")
        values = {}
        for line in text.splitlines():
            if line.startswith("RATE_") and "_RESERVE_" in line and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = float(value.strip())
        self.assertEqual(values, _LIVE_RESERVES)

    def test_env_example_no_longer_claims_c_ignores_quota(self) -> None:
        text = (CORE / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("Tier C never checks quota", text)
        self.assertNotIn("inert by construction", text)


if __name__ == "__main__":
    unittest.main()
