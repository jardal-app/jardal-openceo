"""Common review pool: Gemini/AGY slot + Nemotron/MiMo (00250).

One logical Gemini slot (family gemini, account selectable by config
among agyd/agyj/agy2) plus two OpenCode slots join the shared pool;
S keeps its rounds, top-3 and routing. Covers account→command
resolution, real reviewer argv with a faked fence, the loaded pool,
family eligibility/quorum, per-attempt identity and failure
classification. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from config_loader import ConfigLoader  # noqa: E402
from orchestration import findings as findings_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402
from research.runtimes.base import IsolationUnavailable  # noqa: E402
from settings import Settings  # noqa: E402


def _gemini_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "label": "gemini", "provider": "gemini", "model": "gemini",
        "family": "gemini", "account": "agyd", "priority": 60,
        "active": True, "quota": "reactive", "prompt_via": "argv",
        "headless_args": ["--print"],
    }
    spec.update(overrides)
    return spec


def _pool(*specs: tuple[str, str, int],
          extras: tuple[str, ...] = ()) -> list[review_mod.ReviewSlot]:
    return review_mod.parse_pool([
        {"label": label, "family": family, "priority": priority,
         "extra": label in extras}
        for label, family, priority in specs
    ])


class AccountResolutionTests(unittest.TestCase):
    def test_each_agy_account_resolves_to_itself(self) -> None:
        for account in ("agyd", "agyj", "agy2"):
            resolved = review_mod.with_resolved_command(
                _gemini_spec(account=account))
            self.assertEqual(resolved["command"], [account])
            self.assertEqual(resolved["account"], account)
            self.assertEqual(resolved["headless_args"], ["--print"])

    def test_resolution_is_pure(self) -> None:
        spec = _gemini_spec(account="agyj")
        review_mod.with_resolved_command(spec)
        self.assertNotIn("command", spec)

    def test_unknown_account_fails_closed(self) -> None:
        with self.assertRaises(review_mod.ReviewerConfigError) as raised:
            review_mod.with_resolved_command(
                _gemini_spec(account="agy9"))
        message = str(raised.exception)
        self.assertIn("agy9", message)
        self.assertIn("agyd", message)

    def test_command_contradicting_account_fails_closed(self) -> None:
        with self.assertRaises(review_mod.ReviewerConfigError) as raised:
            review_mod.with_resolved_command(_gemini_spec(
                account="agyj", command=["agyd", "--print"]))
        self.assertIn("contradicts", str(raised.exception))

    def test_static_entries_pass_through(self) -> None:
        spec = {"label": "dsh", "provider": "dsh", "family": "deepseek",
                "command": ["dsh", "-"], "prompt_via": "stdin"}
        self.assertEqual(review_mod.with_resolved_command(spec), spec)

    def test_account_ignored_outside_agy_providers(self) -> None:
        spec = {"label": "q", "provider": "qwen", "family": "qwen",
                "account": "agyd", "command": ["qwen"]}
        self.assertEqual(review_mod.with_resolved_command(spec), spec)

    def test_parse_pool_carries_account(self) -> None:
        (slot,) = review_mod.parse_pool([_gemini_spec(account="agy2")])
        self.assertEqual(slot.account, "agy2")
        self.assertEqual(slot.family, "gemini")
        self.assertEqual(slot.command, ("agy2",))

    def test_pool_pick_distributes_over_accounts(self) -> None:
        spec = _gemini_spec(
            account_pool=["agyd", "agyj", "agy2"])
        seen = {review_mod.with_resolved_command(
            spec, pick_pool=True)["account"] for _ in range(300)}
        self.assertEqual(seen, {"agyd", "agyj", "agy2"})

    def test_pool_pick_sets_command_account(self) -> None:
        resolved = review_mod.with_resolved_command(
            _gemini_spec(account_pool=["agyj"]), pick_pool=True)
        self.assertEqual(resolved["account"], "agyj")
        self.assertEqual(resolved["command"], ["agyj"])

    def test_no_pick_keeps_configured_account(self) -> None:
        resolved = review_mod.with_resolved_command(
            _gemini_spec(account_pool=["agyd", "agyj", "agy2"]))
        self.assertEqual(resolved["account"], "agyd")
        self.assertEqual(resolved["command"], ["agyd"])

    def test_unknown_pool_account_fails_closed(self) -> None:
        with self.assertRaises(review_mod.ReviewerConfigError) as raised:
            review_mod.with_resolved_command(
                _gemini_spec(account_pool=["agyd", "agy9"]),
                pick_pool=True)
        self.assertIn("agy9", str(raised.exception))

    def test_executed_account_prefers_pool_pick(self) -> None:
        (slot,) = review_mod.parse_pool([_gemini_spec()])
        self.assertEqual(
            review_mod.executed_account(slot, "agy2"), "agy2")
        self.assertEqual(
            review_mod.executed_account(slot, "gemini"), "agyd")
        self.assertEqual(
            review_mod.executed_account(slot, ""), "agyd")

    def test_parse_pool_rejects_bad_account(self) -> None:
        with self.assertRaises(review_mod.ReviewerConfigError):
            review_mod.parse_pool([_gemini_spec(account="nope")])


class LiveArgvTests(unittest.IsolatedAsyncioTestCase):
    async def _argv(self, spec: dict[str, Any],
                    prompt: str = "review this") -> tuple[list[str],
                                                         list[str], Any]:
        seen: dict[str, Any] = {}

        def fake_plan(*, argv, workspace, base, overlay, config_args,
                      home_file_overlays=None, extra_read_roots=(),
                      extra_write_roots=()):
            seen["argv"] = list(argv)
            seen["config_args"] = list(config_args)
            raise IsolationUnavailable("no bwrap here")

        executor = ProcessExecutor(pool=[spec])
        with mock.patch("orchestration.live.plan_agent",
                        side_effect=fake_plan), \
                tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run(
                f"review:{spec['label']}", prompt, Path(tmp), 5)
        return seen["config_args"], seen["argv"], result

    async def test_gemini_argv_runs_account_print_prompt(self) -> None:
        config_args, argv, result = await self._argv(
            _gemini_spec(account="agyj"))
        self.assertEqual(config_args, ["agyj", "--output-format", "json", "--print"])
        self.assertEqual(argv[-2:], ["--print", "review this"])
        self.assertEqual(argv[0], "agyj")
        self.assertEqual(result.slot, "agyj")
        self.assertEqual(result.model, "gemini")

    async def test_account_switch_changes_only_argv0(self) -> None:
        first = await self._argv(_gemini_spec(account="agyd"))
        second = await self._argv(_gemini_spec(account="agy2"))
        self.assertEqual(first[0][1:], second[0][1:])
        self.assertEqual((first[0][0], second[0][0]), ("agyd", "agy2"))
        self.assertEqual(first[1][1:], second[1][1:])

    async def test_opencode_argv_carries_model(self) -> None:
        config_args, argv, _result = await self._argv({
            "label": "nemotron", "provider": "opencode",
            "model": "opencode/nemotron-3-ultra-free", "family": "nemotron",
            "prompt_via": "argv",
            "command": ["opencode", "run", "-m",
                        "opencode/nemotron-3-ultra-free"]})
        self.assertEqual(config_args[:4],
                         ["opencode", "run", "-m",
                          "opencode/nemotron-3-ultra-free"])
        self.assertEqual(argv[-1], "review this")

    async def test_misconfigured_reviewer_never_spawns(self) -> None:
        seen: dict[str, Any] = {}

        def fake_plan(*, argv, workspace, base, overlay, config_args,
                      home_file_overlays=None, extra_read_roots=(),
                      extra_write_roots=()):
            seen["called"] = True
            raise IsolationUnavailable("no bwrap here")

        executor = ProcessExecutor(
            pool=[_gemini_spec(account="agy9")])
        with mock.patch("orchestration.live.plan_agent",
                        side_effect=fake_plan), \
                tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("review:gemini", "hi", Path(tmp), 5)
        self.assertNotIn("called", seen)
        self.assertIn("reviewer misconfigured", result.output_tail)
        self.assertIn("agy9", result.output_tail)


def _real_reviewers() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repo = Path(__file__).resolve().parents[1]
    loader = ConfigLoader(
        Settings(_env_file=None, PROJECTS_DIR=str(repo / "projects")))
    review = loader.load_project("jardal-openceo")["review"]
    return list(review["reviewers"]), dict(review["review"])


class RealPoolTests(unittest.TestCase):
    def test_single_active_gemini_slot(self) -> None:
        reviewers, _cfg = _real_reviewers()
        gemini = [item for item in reviewers
                  if item["family"] == "gemini"]
        self.assertEqual(len(gemini), 1)
        slot = gemini[0]
        self.assertTrue(slot["active"])
        self.assertEqual(slot["label"], "gemini")
        self.assertEqual(slot["account"], "agyd")
        self.assertNotIn("command", slot)
        self.assertEqual(slot["headless_args"],
                         ["--model", "gemini-3.8-flash-high", "--print"])
        self.assertEqual(slot["model"], "gemini-3.8-flash-high")
        self.assertEqual(slot["account_pool"], ["agyd", "agyj", "agy2"])

    def test_opencode_candidates_active_with_script_ids(self) -> None:
        reviewers, _cfg = _real_reviewers()
        by_label = {item["label"]: item for item in reviewers}
        self.assertTrue(by_label["mimo"]["active"])
        self.assertEqual(by_label["mimo"]["model"],
                         "opencode/mimo-v2.6-flash-free")
        self.assertEqual(by_label["mimo"]["command"],
                         ["opencode", "run", "-m",
                          "opencode/mimo-v2.6-flash-free"])
        self.assertTrue(by_label["nemotron"]["active"])
        self.assertEqual(by_label["nemotron"]["model"],
                         "opencode/nemotron-3.5-super-vl-free")
        families = {by_label["mimo"]["family"],
                    by_label["nemotron"]["family"]}
        self.assertEqual(families, {"mimo", "nemotron"})

    def test_s_active_top3_rounds_and_quorum(self) -> None:
        reviewers, cfg = _real_reviewers()
        active = sorted(
            (item for item in reviewers
             if item["active"] and not item.get("extra")),
            key=lambda item: (item["priority"], item["label"]))
        self.assertEqual([item["label"] for item in active[:3]],
                         ["mimo", "nemotron", "gemini"])
        self.assertEqual(cfg["quorum"], 2)
        self.assertEqual(cfg["distinct_by"], "family")
        self.assertEqual(cfg["round1_parallel"], 3)
        self.assertEqual(cfg["round2_parallel"], 2)
        self.assertEqual(cfg["max_rounds"], 2)
        self.assertEqual(cfg["min_available_families"], 3)


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_builder_family_excluded(self) -> None:
        pool = _pool(("dsh", "deepseek", 1), ("claudep", "deepseek", 2),
                     ("qwen-max", "qwen", 3))
        plan = await review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 2,
            builder_family="deepseek")
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["qwen-max"])

    async def test_one_slot_per_family(self) -> None:
        pool = _pool(("gemini", "gemini", 1), ("gemini2", "gemini", 2),
                     ("qwen-max", "qwen", 3))
        plan = await review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 3,
            builder_family="muse")
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["gemini", "qwen-max"])

    async def test_extras_ride_along_and_never_count(self) -> None:
        pool = _pool(("qwen-max", "qwen", 1), ("muse-review", "muse", 2),
                     extras=("muse-review",))
        plan = await review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 2,
            builder_family="muse")
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["qwen-max"])
        self.assertEqual([slot.label for slot in plan.extras],
                         ["muse-review"])
        self.assertFalse(review_mod.quorum_met(
            {"muse-review": True}, pool, builder_family="other"))

    async def test_preferred_families_jump_the_queue(self) -> None:
        pool = _pool(("qwen-max", "qwen", 1), ("qwen-glm", "glm", 2),
                     ("dsh", "deepseek", 3), ("nemotron", "nemotron", 4),
                     ("mimo", "mimo", 5), ("gemini", "gemini", 60))
        plan = await review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 3,
            builder_family="muse",
            prefer_families=("nemotron", "mimo", "gemini"))
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["nemotron", "mimo", "gemini"])

    async def test_unavailable_preferred_fall_back_to_alternatives(
            self) -> None:
        pool = _pool(("qwen-max", "qwen", 1), ("nemotron", "nemotron", 4),
                     ("mimo", "mimo", 5))
        store = review_mod.InMemorySlotStore()
        await store.set_slot("nemotron", {"status": "cooling_down",
                                          "until": "2999-01-01T00:00:00+00:00"})
        plan = await review_mod.select_round(
            pool, store, 2, builder_family="muse",
            prefer_families=("nemotron", "mimo"))
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["mimo", "qwen-max"])

    async def test_no_candidates_plans_empty(self) -> None:
        pool = _pool(("qwen-max", "qwen", 1))
        store = review_mod.InMemorySlotStore()
        await store.set_slot("qwen-max", {"status": "disabled",
                                         "reason": "test"})
        plan = await review_mod.select_round(
            pool, store, 3, builder_family="muse")
        self.assertEqual(plan.reviewers, ())

    async def test_gemini_selectable_in_current_s_pool(self) -> None:
        reviewers, _cfg = _real_reviewers()
        pool = review_mod.parse_pool(reviewers)
        store = review_mod.InMemorySlotStore()
        for label in ("qwen-max", "qwen-glm", "dsh"):
            await store.set_slot(label, {"status": "cooling_down",
                                        "until": "2999-01-01T00:00:00+00:00"})
        plan = await review_mod.select_round(
            pool, store, 3, builder_family="muse")
        labels = [slot.label for slot in plan.reviewers]
        self.assertIn("gemini", labels)
        self.assertEqual(len({slot.family for slot in plan.reviewers}), 3)

    def test_quorum_counts_valid_independent_families_only(self) -> None:
        pool = _pool(("a", "fa", 1), ("b", "fb", 2), ("a2", "fa", 3),
                     ("x", "muse", 4))
        self.assertTrue(review_mod.quorum_met(
            {"a": True, "b": True}, pool, builder_family="muse"))
        self.assertFalse(review_mod.quorum_met(
            {"a": True, "a2": True}, pool, builder_family="muse"))
        self.assertFalse(review_mod.quorum_met(
            {"a": True, "b": False}, pool, builder_family="muse"))
        self.assertFalse(review_mod.quorum_met(
            {"a": True, "x": True}, pool, builder_family="muse"))
        self.assertFalse(review_mod.quorum_met(
            {"unknown": True}, pool, builder_family="muse"))


class AttemptIdentityTests(unittest.TestCase):
    def test_outcome_carries_account_model_family(self) -> None:
        (slot,) = review_mod.parse_pool([_gemini_spec(account="agyj")])
        outcome = review_mod.new_outcome(slot)
        self.assertEqual(
            (outcome["slot"], outcome["family"], outcome["account"],
             outcome["model"]),
            ("gemini", "gemini", "agyj", "gemini"))
        self.assertFalse(outcome["valid"])

    def test_static_slots_default_to_blank_identity(self) -> None:
        (slot,) = _pool(("dsh", "deepseek", 1))
        outcome = review_mod.new_outcome(slot)
        self.assertEqual(outcome["account"], "")
        self.assertEqual(outcome["model"], "")


class ClassificationTests(unittest.TestCase):
    def test_model_not_found_disables(self) -> None:
        (slot,) = review_mod.parse_pool([{
            "label": "nemotron", "family": "nemotron",
            "command": ["opencode", "run"]}])
        action, patch = review_mod.classify_slot_error(
            "Error: model not found: opencode/typo-free", slot)
        self.assertEqual(action, "disabled")
        self.assertEqual(patch["status"], "disabled")

    def test_unclassified_error_keeps_availability(self) -> None:
        (slot,) = review_mod.parse_pool([_gemini_spec()])
        action, patch = review_mod.classify_slot_error(
            "weird transient wobble", slot)
        self.assertEqual((action, patch), ("invalid_only", {}))

    def test_limit_pattern_cools_down(self) -> None:
        (slot,) = review_mod.parse_pool([_gemini_spec(
            limit_patterns=["quota exhausted"])])
        action, patch = review_mod.classify_slot_error(
            "denied: quota exhausted for today", slot)
        self.assertEqual(action, "cooling_down")
        self.assertEqual(patch["status"], "cooling_down")


class ReportContractTests(unittest.TestCase):
    def test_clean_report_parses_to_no_findings(self) -> None:
        self.assertEqual(
            findings_mod.parse_report("No findings.\nREVIEW_DONE_x\n"), [])

    def test_empty_or_garbage_report_is_malformed(self) -> None:
        for bad in ("", "\n  \n", "looks good to me, ship it\n",
                    "some prose without a marker\n"):
            with self.assertRaises(findings_mod.ReportMalformed, msg=bad):
                findings_mod.parse_report(bad)
