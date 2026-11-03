from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from config_loader import ConfigLoader
from orchestration import review
from orchestration.agent_sandbox import _gemini_state_ops
from orchestration.live import _reviewer_output
from settings import Settings


RECOVERY = {"attempts_per_slot": 3, "retry_delay_seconds": 300,
            "max_wait_seconds": 43200}


def outcome(slot, attempt, valid=False, kind="slot_error", reason="503 unavailable"):
    result = review.new_outcome(slot, attempt)
    result.update(valid=valid, kind="valid" if valid else kind, reason=reason)
    return result


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_round(self, pool, run_one, *, budget=43200, store=None):
        store = store or review.InMemorySlotStore()
        plan = await review.select_round(pool, store, 3, include_extras=False)
        self.elapsed = 0
        self.delays = []

        async def sleep(seconds):
            self.delays.append(seconds)
            self.elapsed += seconds

        with patch.object(review.time, "monotonic", side_effect=lambda: self.elapsed), \
                patch.object(review.asyncio, "sleep", side_effect=sleep):
            return await review.run_quorum_round(
                pool, store, plan, run_one,
                recovery={**RECOVERY, "max_wait_seconds": budget})

    async def test_ranked_reserves_precede_retries_and_keep_successful_report(self):
        pool = [review.ReviewSlot("deep", "deepseek", model="deep"),
                review.ReviewSlot("agy2", "gemini"),
                review.ReviewSlot("duplicate", "deepseek", model="deep-free", reserve_only=True),
                review.ReviewSlot("minimax", "minimax", priority=101, reserve_only=True),
                review.ReviewSlot("mimo", "mimo", priority=102, reserve_only=True)]
        calls = []

        async def run_one(slot, attempt, previous):
            calls.append((slot.label, attempt))
            return outcome(slot, attempt, valid=slot.label in {"deep", "minimax", "mimo"})

        result = await self.run_round(pool, run_one)
        self.assertTrue(result.met)
        self.assertEqual([call for call in calls if call[0] == "agy2"],
                         [("agy2", 1)])
        self.assertEqual(calls.count(("deep", 1)), 1)
        self.assertNotIn(("deep", 2), calls)
        self.assertNotIn(("duplicate", 1), calls)
        self.assertLess(calls.index(("minimax", 1)), calls.index(("mimo", 1)))
        self.assertEqual(self.delays, [])

    async def test_exhausted_router_uses_reserve_without_repeating_failed_model(self):
        for kind in ("cooling_down", "timeout", "malformed"):
            pool = [review.ReviewSlot("free", "alpha"),
                    review.ReviewSlot("kept", "beta"),
                    review.ReviewSlot("reserve", "gamma", reserve_only=True)]
            calls = []

            async def run_one(slot, attempt, previous):
                calls.append((slot.label, attempt))
                result = outcome(slot, attempt, valid=slot.label != "free", kind=kind)
                if slot.label == "free":
                    result["routing_exhausted"] = True
                return result

            result = await self.run_round(pool, run_one)
            self.assertTrue(result.met)
            self.assertEqual(calls.count(("free", 1)), 1)
            self.assertNotIn(("free", 2), calls)
            self.assertEqual(self.delays, [])

    async def test_recovers_after_hour_without_repeating_valid_reviewer(self):
        pool = [review.ReviewSlot("deep", "deepseek"), review.ReviewSlot("agyj", "gemini")]
        calls = []

        async def run_one(slot, attempt, previous):
            calls.append((slot.label, attempt))
            return outcome(slot, attempt, valid=slot.label == "deep" or self.elapsed >= 3600)

        result = await self.run_round(pool, run_one)
        self.assertTrue(result.met)
        self.assertGreaterEqual(self.elapsed, 3600)
        self.assertGreater(len([call for call in calls if call[0] == "agyj"]), 3)
        self.assertEqual([call for call in calls if call[0] == "deep"], [("deep", 1)])
        self.assertTrue(all(delay == 300 for delay in self.delays))

    async def test_deadline_bounds_an_outage(self):
        pool = [review.ReviewSlot("a", "deepseek"), review.ReviewSlot("b", "gemini")]

        async def run_one(slot, attempt, previous):
            return outcome(slot, attempt)

        result = await self.run_round(pool, run_one, budget=900)
        self.assertFalse(result.met)
        self.assertEqual(self.elapsed, 900)
        self.assertTrue(result.outcomes)

    async def test_persisted_cooldown_recovers_automatically(self):
        pool = [review.ReviewSlot("deep", "deepseek"), review.ReviewSlot("agy2", "gemini")]
        store = review.InMemorySlotStore()
        start = datetime(2026, 10, 19, tzinfo=timezone.utc)
        await store.set_slot("agy2", {
            "status": "cooling_down", "until": (start + timedelta(seconds=600)).isoformat()})
        calls = []

        async def run_one(slot, attempt, previous):
            calls.append((slot.label, self.elapsed))
            return outcome(slot, attempt, valid=True)

        with patch.object(review, "_utcnow", side_effect=lambda: start + timedelta(
                seconds=getattr(self, "elapsed", 0))):
            result = await self.run_round(pool, run_one, store=store)
        self.assertTrue(result.met)
        self.assertEqual(calls, [("deep", 0), ("agy2", 600)])
        self.assertEqual((await store.get_slot("agy2"))["status"], "available")

    async def test_missing_model_disables_slot_and_uses_reserve_without_delay(self):
        pool = [review.ReviewSlot("deep", "deepseek"), review.ReviewSlot("old", "gemini"),
                review.ReviewSlot("reserve", "minimax", reserve_only=True)]
        store = review.InMemorySlotStore()

        async def run_one(slot, attempt, previous):
            if slot.label == "old":
                kind, state = review.classify_slot_error("ProviderModelNotFoundError", slot)
                await store.set_slot(slot.label, state)
                return outcome(slot, attempt, kind=kind, reason="ProviderModelNotFoundError")
            return outcome(slot, attempt, valid=True)

        result = await self.run_round(pool, run_one, store=store)
        self.assertTrue(result.met)
        self.assertEqual((await store.get_slot("old"))["status"], "disabled")
        self.assertEqual(self.delays, [])

    async def test_permanent_error_and_write_attempt_do_not_loop(self):
        for kind, reason in (("slot_error", "authentication required"),
                             ("write_attempt", "write_attempt"),
                             ("disabled", "model not found")):
            pool = [review.ReviewSlot("a", "deepseek"), review.ReviewSlot("b", "gemini")]
            calls = []

            async def run_one(slot, attempt, previous):
                calls.append((slot.label, attempt))
                return outcome(slot, attempt, valid=slot.label == "a", kind=kind, reason=reason)

            result = await self.run_round(pool, run_one)
            self.assertFalse(result.met)
            self.assertEqual(calls, [("a", 1), ("b", 1)])
            self.assertEqual(self.delays, [])

    async def test_malformed_report_gets_only_one_retry(self):
        pool = [review.ReviewSlot("a", "deepseek"), review.ReviewSlot("b", "gemini")]

        async def run_one(slot, attempt, previous):
            return outcome(slot, attempt, valid=slot.label == "a", kind="malformed", reason="header")

        result = await self.run_round(pool, run_one)
        self.assertFalse(result.met)
        self.assertEqual([item["attempt"] for item in result.outcomes if item["slot"] == "b"], [1, 2])

    async def test_exception_does_not_cancel_sibling(self):
        pool = [review.ReviewSlot("a", "deepseek"), review.ReviewSlot("b", "gemini"),
                review.ReviewSlot("fallback", "minimax", reserve_only=True)]
        finished = []

        async def run_one(slot, attempt, previous):
            if slot.label == "b":
                raise RuntimeError("503 unavailable")
            await asyncio.sleep(0)
            finished.append(slot.label)
            return outcome(slot, attempt, valid=True)

        result = await self.run_round(pool, run_one)
        self.assertTrue(result.met)
        self.assertIn("a", finished)
        self.assertIn("fallback", finished)

    async def test_reserves_and_same_family_never_inflate_quorum(self):
        pool = [review.ReviewSlot("n1", "nemotron"),
                review.ReviewSlot("n2", "nemotron", reserve_only=True),
                review.ReviewSlot("builder", "muse", extra=True)]

        async def run_one(slot, attempt, previous):
            return outcome(slot, attempt, valid=True)

        result = await self.run_round(pool, run_one)
        self.assertFalse(result.met)
        self.assertEqual([item["slot"] for item in result.outcomes], ["n1"])

    async def test_cancellation_propagates_to_active_reviewers(self):
        started = asyncio.Event()
        cleaned = []
        pool = [review.ReviewSlot("a", "deepseek")]

        async def run_one(slot, attempt, previous):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(slot.label)

        store = review.InMemorySlotStore()
        plan = await review.select_round(pool, store, 1)
        task = asyncio.create_task(review.run_quorum_round(pool, store, plan, run_one, recovery=RECOVERY))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cleaned, ["a"])


class OutputTests(unittest.TestCase):
    def test_agy_exit_zero_error_is_not_a_valid_report(self):
        code, text = _reviewer_output(json.dumps({"status": "ERROR", "error": "UNAVAILABLE (503)",
                                                "response": "No findings."}), "", "gemini", 0)
        self.assertEqual(code, 1)
        self.assertIn("503", text)
        self.assertNotIn("No findings", text)

    def test_agy_success_unwraps_response(self):
        self.assertEqual(_reviewer_output(json.dumps({"status": "SUCCESS", "response": "No findings."}),
                                          "", "agy", 0), (0, "No findings."))

    def test_opencode_text_then_error_is_not_success(self):
        stdout = '\n'.join(json.dumps(event) for event in (
            {"type": "text", "part": {"id": "1", "text": "No findings."}},
            {"type": "error", "error": {"message": "socket connection was closed"}}))
        code, text = _reviewer_output(stdout, "", "opencode", 0)
        self.assertEqual(code, 1)
        self.assertIn("socket connection", text)

    def test_opencode_deduplicates_text_parts(self):
        stdout = '\n'.join(json.dumps({"type": "text", "part": {"id": "1", "text": text}})
                            for text in ("No findings", "No findings."))
        self.assertEqual(_reviewer_output(stdout, "", "opencode", 0), (0, "No findings."))

    def test_nonzero_exit_preserves_stderr_diagnostic(self):
        code, text = _reviewer_output("partial report", "503 unavailable", "qwen", 1)
        self.assertEqual(code, 1)
        self.assertTrue(review.transient_slot_error(text))

    def test_all_cli_transport_errors_get_temporary_cooldown(self):
        for text in ("UNAVAILABLE (code 503)", "The socket connection was closed unexpectedly",
                     "429 Too Many Requests", "ECONNRESET", "502 Bad Gateway"):
            action, state = review.classify_slot_error(text, review.ReviewSlot("r", "f"))
            self.assertEqual(action, "cooling_down")
            self.assertIn("until", state)


class RankingTests(unittest.TestCase):
    def test_no_static_reserves_free_is_dynamic_only(self):
        config = ConfigLoader(Settings(_env_file=None)).load_project(None)["review"]
        reserves = [entry for entry in config["reviewers"] if entry.get("reserve_only")]
        self.assertEqual(reserves, [])
        self.assertEqual(config["review"]["recovery"], RECOVERY)


class AccountSandboxTests(unittest.TestCase):
    def test_missing_history_mounts_without_changing_host_account(self):
        if not shutil.which("bwrap"):
            self.skipTest("bwrap unavailable")
        with tempfile.TemporaryDirectory(prefix="agy-state-test-") as directory:
            home = Path(directory)
            account = home / ".gemini-j"
            (account / "config").mkdir(parents=True)
            (account / "config" / "test.json").write_text("{}")
            scratch = home / "scratch"
            scratch.mkdir()
            ops = _gemini_state_ops(home, scratch, ["agyj"])
            program = (
                "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
                "assert (p/'config/test.json').read_text() == '{}'; "
                "(p/'history/probe').write_text('ok'); "
                "(p/'new-state.json').write_text('{}')")
            result = subprocess.run(
                ["bwrap", "--ro-bind", "/", "/", *[part for op in ops for part in op],
                 "--", "/usr/bin/python3", "-c", program, str(account)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((account / "history").exists())
            self.assertFalse((account / "new-state.json").exists())
