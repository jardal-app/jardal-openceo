"""Planner policy integrity (TASK-2026-00184; split by TASK-2026-00201).

The planner receives the policy core
(``openceo-core/objectives/routing_policy.md``) plus an index of staged
per-agent detail files in every planning cycle. Two cheap,
deterministic safeguards (plain Python: no model call, nothing added
to the prompt):

- a boot-time check says early if a policy file is missing, empty, or
  the core is longer than the prompt budget (it would be cut off,
  tail first, and the tail holds the compact runtime kernel);
- every planning run stores a short fingerprint of the composed core +
  details, so a decision can be traced to the policy version it saw.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives.engine import _planner_prompt  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    POLICY_PROMPT_MAX_CHARS,
    PolicyStatus,
    check_policy,
    load_policy_text,
    log_policy_status,
    policy_file,
    policy_fingerprint,
    policy_prompt_text,
)

_LIMIT = POLICY_PROMPT_MAX_CHARS


def _tmp_policy(test: unittest.TestCase, text: str) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    path = Path(temp.name) / "policy.md"
    path.write_text(text, encoding="utf-8")
    return path


class FingerprintTests(unittest.TestCase):
    def test_is_twelve_hex_characters_and_stable(self) -> None:
        first = policy_fingerprint("# Policy\nroute by nature, not quota\n")
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(
            first, policy_fingerprint("# Policy\nroute by nature, not quota\n"))

    def test_follows_the_content(self) -> None:
        self.assertNotEqual(policy_fingerprint("rule A"),
                            policy_fingerprint("rule B"))

    def test_ignores_outer_whitespace_like_the_prompt_does(self) -> None:
        self.assertEqual(policy_fingerprint("rule\n"),
                         policy_fingerprint("  \n rule  \n\n"))

    def test_empty_policy_has_no_fingerprint(self) -> None:
        for text in ("", "   \n\t"):
            with self.subTest(text=repr(text)):
                self.assertEqual(policy_fingerprint(text), "")

    def test_hashes_exactly_what_the_prompt_carries(self) -> None:
        long = "a" * _LIMIT + "tail that the prompt cuts"
        carried = long[:_LIMIT]
        self.assertEqual(
            policy_fingerprint(long),
            hashlib.sha256(carried.encode("utf-8")).hexdigest()[:12])
        # Text past the budget is not part of what the planner saw.
        self.assertEqual(policy_fingerprint(long),
                         policy_fingerprint("a" * _LIMIT + "another tail"))

    def test_prompt_text_is_stripped_and_capped(self) -> None:
        self.assertEqual(policy_prompt_text("  x  \n"), "x")
        self.assertEqual(len(policy_prompt_text("z" * (_LIMIT + 50))), _LIMIT)
        self.assertEqual(policy_prompt_text(""), "")


class PromptCarriesExactlyTheFingerprintedTextTests(unittest.TestCase):
    def test_the_prompt_uses_the_shared_cut(self) -> None:
        kept = "☃" * _LIMIT           # inside the budget
        cut = "☄" * 40               # past the budget
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "", [],
            blocked_tickets=[], agents_md="REGISTERED AGENTS",
            policy_text="  " + kept + cut + "  ", handoffs=[], roadmap=[])
        self.assertIn(kept, prompt)
        self.assertNotIn("☄", prompt)


class CheckPolicyTests(unittest.TestCase):
    def test_the_shipped_policy_is_healthy(self) -> None:
        status = check_policy()
        self.assertTrue(status.ok, status.problem)
        self.assertEqual(status.problem, "")
        self.assertGreater(status.chars, 1000)
        self.assertLess(status.chars, _LIMIT)
        self.assertEqual(status.sha, policy_fingerprint(load_policy_text()))
        self.assertTrue(Path(status.path).is_file())
        self.assertEqual(Path(status.path), policy_file(None))

    def test_the_default_file_ships_next_to_the_planner_module(self) -> None:
        from objectives import planner_models

        self.assertEqual(policy_file(None).parent,
                         Path(planner_models.__file__).resolve().parent)
        self.assertEqual(policy_file(None).name, "routing_policy.md")

    def test_an_override_path_wins(self) -> None:
        path = _tmp_policy(self, "# my policy\n")
        status = check_policy(path)
        self.assertTrue(status.ok)
        self.assertEqual(status.path, str(path))
        self.assertEqual(status.sha, policy_fingerprint("# my policy\n"))

    def test_a_missing_file_is_a_problem(self) -> None:
        status = check_policy("/nonexistent/policy.md")
        self.assertFalse(status.ok)
        self.assertEqual((status.chars, status.sha), (0, ""))
        self.assertIn("missing, unreadable or empty", status.problem)

    def test_an_empty_file_is_a_problem(self) -> None:
        status = check_policy(_tmp_policy(self, "  \n\n"))
        self.assertFalse(status.ok)
        self.assertIn("missing, unreadable or empty", status.problem)

    def test_a_directory_is_unreadable_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            status = check_policy(folder)
        self.assertFalse(status.ok)

    def test_over_budget_is_a_problem_that_names_the_cost(self) -> None:
        status = check_policy(_tmp_policy(self, "p" * (_LIMIT + 1)))
        self.assertFalse(status.ok)
        self.assertEqual(status.chars, _LIMIT + 1)
        self.assertIn("exceed the prompt budget", status.problem)
        self.assertIn("compact runtime kernel", status.problem)
        self.assertEqual(status.sha, policy_fingerprint("p" * (_LIMIT + 1)))

    def test_exactly_at_the_budget_is_healthy(self) -> None:
        self.assertTrue(check_policy(_tmp_policy(self, "p" * _LIMIT)).ok)


class BootLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = logging.getLogger("test.planner.policy")

    def test_a_healthy_policy_logs_info_with_its_fingerprint(self) -> None:
        path = _tmp_policy(self, "# ok\n")
        with self.assertLogs(self.log, level="INFO") as captured:
            status = log_policy_status(path, self.log)
        self.assertTrue(status.ok)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.INFO)
        self.assertIn(status.sha, record.getMessage())
        self.assertIn(str(path), record.getMessage())

    def test_a_missing_policy_logs_an_error_that_says_what_happens(self) -> None:
        with self.assertLogs(self.log, level="INFO") as captured:
            status = log_policy_status("/nonexistent/policy.md", self.log)
        self.assertFalse(status.ok)
        (record,) = captured.records
        self.assertEqual(record.levelno, logging.ERROR)
        message = record.getMessage()
        self.assertIn("/nonexistent/policy.md", message)
        self.assertIn("parks with 'config'", message)
        self.assertIn("refusing to plan blind", message)

    def test_an_over_budget_policy_logs_an_error_about_truncation(self) -> None:
        path = _tmp_policy(self, "p" * (_LIMIT + 10))
        with self.assertLogs(self.log, level="INFO") as captured:
            status = log_policy_status(path, self.log)
        self.assertFalse(status.ok)
        (record,) = captured.records
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIn("truncated", record.getMessage())
        self.assertIn(status.sha, record.getMessage())

    def test_it_returns_the_status_it_reported(self) -> None:
        self.assertIsInstance(
            log_policy_status(_tmp_policy(self, "x"), self.log), PolicyStatus)


class StartupWiringTests(unittest.TestCase):
    def test_lifespan_runs_the_check_before_the_reconcile_steps(self) -> None:
        source = (CORE / "main.py").read_text(encoding="utf-8")
        lifespan = source[source.index("async def lifespan("):]
        self.assertIn("log_policy_status(", lifespan)
        # The app logger has no INFO handler in host mode: use uvicorn's.
        self.assertIn('logging.getLogger("uvicorn.error")',
                      lifespan[lifespan.index("log_policy_status("):][:600])
        self.assertLess(lifespan.index("log_policy_status("),
                        lifespan.index("mark_interrupted_pm_states"))
        # A failing check must never stop the service from starting.
        before = lifespan[:lifespan.index("log_policy_status(")]
        self.assertTrue(before.rstrip().endswith("with suppress(Exception):")
                        or "with suppress(Exception):" in before[-200:])


if __name__ == "__main__":
    unittest.main()
