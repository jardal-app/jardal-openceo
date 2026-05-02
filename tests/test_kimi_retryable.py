from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.kimi_run import detect_kimi_retryable_failure


class KimiRetryableFailureTests(unittest.TestCase):
    def test_detects_rate_limit_from_retryable_exit_code(self) -> None:
        result = detect_kimi_retryable_failure(
            75,
            "Error: provider returned HTTP 429 Too Many Requests",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.waiting_reason, "rate_limit:kimi")

    def test_parses_try_again_wait_window(self) -> None:
        result = detect_kimi_retryable_failure(
            75,
            "Rate limit exceeded. Try again in 1h 15m",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreaterEqual(result.wait_seconds or 0, 4500)

    def test_marks_retryable_non_rate_limit_failures(self) -> None:
        result = detect_kimi_retryable_failure(
            75,
            "Connection timeout while contacting provider",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.waiting_reason, "retryable_error:kimi")

    def test_returns_none_for_permanent_failures(self) -> None:
        result = detect_kimi_retryable_failure(
            1,
            "Invalid API key",
        )

        self.assertIsNone(result)
