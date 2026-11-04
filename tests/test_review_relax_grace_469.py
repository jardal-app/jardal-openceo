"""An available relaxed quorum stands after a short grace (TASK-2026-00469).

Contract fixed here:

- With the cascade on and ``relax_after_seconds`` set, once the valid
  reports meet quorum counting extra/builder-family slots (muse-review)
  and the strict quorum is still missing, the round waits only that
  grace: then it cuts the reviewers still running (their tasks are
  cancelled) and closes with the audited relaxed quorum, instead of
  waiting for the 12 h recovery deadline.
- A strict quorum inside the grace wins (not relaxed); without the key,
  or while the relaxed quorum is not met, the classic wait stands.
- The production default is 300 seconds.

Simulated reviewers on the real event loop; no network, no CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from config_loader import ConfigLoader  # noqa: E402
from orchestration import review  # noqa: E402
from settings import Settings  # noqa: E402

RECOVERY = {"attempts_per_slot": 3, "retry_delay_seconds": 300,
            "max_wait_seconds": 43200}


def _pool() -> list[review.ReviewSlot]:
    return [review.ReviewSlot("free-a", "deepseek", priority=1),
            review.ReviewSlot("free-b", "gemini", priority=2),
            review.ReviewSlot("muse-review", "muse", priority=99, extra=True)]


def _outcome(slot: review.ReviewSlot, attempt: int, valid: bool) -> dict[str, Any]:
    result = review.new_outcome(slot, attempt)
    result.update(valid=valid, kind="valid" if valid else "slot_error",
                  reason="" if valid else "503 unavailable")
    return result


class RelaxGraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cancelled: list[str] = []

    def _run_one(self, *, valid: set[str], hang: set[str],
                 late_valid: set[str] = frozenset()):  # type: ignore[assignment]
        async def run_one(slot: review.ReviewSlot, attempt: int,
                          previous: Any) -> dict[str, Any]:
            del previous
            if slot.label in hang:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.append(slot.label)
            if slot.label in late_valid:
                await asyncio.sleep(0.05)
                return _outcome(slot, attempt, True)
            return _outcome(slot, attempt, slot.label in valid)

        return run_one

    async def _round(self, run_one: Any, cascade: dict[str, Any] | None,
                     ) -> review.QuorumRound:
        pool = _pool()
        store = review.InMemorySlotStore()
        plan = await review.select_round(
            pool, store, 2, builder_family="muse",
            cascade_enabled=review.cascade_enabled(cascade))
        return await review.run_quorum_round(
            pool, store, plan, run_one, builder_family="muse", quorum=2,
            recovery=RECOVERY, cascade=cascade)

    async def test_hung_reviewer_is_cut_after_grace_and_muse_counts(self) -> None:
        run_one = self._run_one(valid={"free-a", "muse-review"}, hang={"free-b"})

        result = await asyncio.wait_for(
            self._round(run_one, {"enabled": True, "relax_after_seconds": 0.05}),
            timeout=5)

        self.assertTrue(result.met)
        self.assertTrue(result.relaxed)
        self.assertEqual(self.cancelled, ["free-b"])
        relaxed = [item.get("slot") for item in result.outcomes
                   if item.get("relaxed")]
        self.assertEqual(relaxed, ["muse-review"])
        self.assertIn("relaxed quorum", result.note)

    async def test_strict_quorum_inside_grace_wins(self) -> None:
        run_one = self._run_one(valid={"free-a", "muse-review"}, hang=set(),
                                late_valid={"free-b"})

        result = await asyncio.wait_for(
            self._round(run_one, {"enabled": True, "relax_after_seconds": 5}),
            timeout=5)

        self.assertTrue(result.met)
        self.assertFalse(result.relaxed)
        self.assertEqual(self.cancelled, [])

    async def test_without_the_key_the_classic_wait_stands(self) -> None:
        run_one = self._run_one(valid={"free-a", "muse-review"}, hang={"free-b"})

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._round(run_one, {"enabled": True}), timeout=0.3)
        self.assertEqual(self.cancelled, ["free-b"])  # outer cancel still cleans

    async def test_strict_quorum_path_is_untouched_by_the_grace(self) -> None:
        run_one = self._run_one(valid={"free-a", "free-b"}, hang={"muse-review"})

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._round(run_one, {"enabled": True, "relax_after_seconds": 0}),
                timeout=0.3)

    async def test_no_relaxed_quorum_means_no_cut(self) -> None:
        run_one = self._run_one(valid={"free-a"}, hang={"free-b"})

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._round(run_one, {"enabled": True, "relax_after_seconds": 0}),
                timeout=0.3)


class RelaxAfterParsingTests(unittest.TestCase):
    def test_parsing_is_fail_classic(self) -> None:
        on = {"enabled": True}
        self.assertIsNone(review.cascade_relax_after(None))
        self.assertIsNone(review.cascade_relax_after(
            {"enabled": False, "relax_after_seconds": 300}))
        self.assertIsNone(review.cascade_relax_after(on))
        for raw in (-1, True, "soon", [300]):
            self.assertIsNone(review.cascade_relax_after(
                {**on, "relax_after_seconds": raw}), raw)
        self.assertEqual(review.cascade_relax_after(
            {**on, "relax_after_seconds": "300"}), 300.0)
        self.assertEqual(review.cascade_relax_after(
            {**on, "relax_after_seconds": 0}), 0.0)

    def test_production_default_is_five_minutes(self) -> None:
        cfg = ConfigLoader(Settings()).load_project("jardal-openceo")
        cascade = cfg["review"]["review"]["cascade"]
        self.assertEqual(review.cascade_relax_after(cascade), 300.0)


if __name__ == "__main__":
    unittest.main()
