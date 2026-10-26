"""Anti-cut review cascade: tiers, spend guard, relaxed quorum (00310).

Free → own → metered waves with a deterministic paid guard, then a
final relaxed quorum where extra/builder-family reports count, else a
block with reasons. Classic quorum stays intact when the switch is
off. No network, no CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from config_loader import ConfigLoader  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from settings import Settings  # noqa: E402

CascadeGuard = review_mod.CascadeGuard


def _slot(label: str, family: str, priority: int = 10, **overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "label": label, "provider": "opencode", "model": f"opencode/{label}",
        "family": family, "priority": priority, "active": True,
        "quota": "reactive", "prompt_via": "argv",
        "command": ["opencode", "run", "-m", f"opencode/{label}"],
    }
    spec.update(overrides)
    return spec


def _pool() -> list[review_mod.ReviewSlot]:
    return review_mod.parse_pool([
        _slot("dsh", "deepseek", 3, provider="dsh", model="deepseek-v4.1-flash",
              cost_tier="metered", prompt_via="stdin", command=["dsh"]),
        _slot("mimo", "mimo", 4, cost_tier="free"),
        _slot("nemotron", "nemotron", 5, cost_tier="free"),
        _slot("gemini", "gemini", 60, provider="gemini", model="gemini",
              cost_tier="own", command=["agyd", "--print"]),
        _slot("openrouter-paid", "openrouter", 70, cascade_only=True,
              cost_tier="metered", model="openrouter/openai/gpt-oss-20b",
              command=["opencode", "run", "-m", "openrouter/openai/gpt-oss-20b"]),
        _slot("muse-review", "muse", 99, provider="muse",
              model="muse-spark-1.3-contributor", extra=True, cost_tier="own",
              command=["muse", "exec"]),
    ])


def _cascade(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "enabled": True, "paid_cap_usd": "0.00",
        "paid_estimate_usd": "0.05", "dsh_estimate_usd": "0.00",
        "paid_whitelist": ["openai/gpt-oss-20b"],
    }
    cfg.update(overrides)
    return cfg


def _outcome(slot: review_mod.ReviewSlot, valid: bool, kind: str = "valid") -> dict[str, Any]:
    outcome = review_mod.new_outcome(slot, 1)
    outcome.update(valid=valid, kind=kind if valid else "slot_error",
                   reason="" if valid else "scripted")
    return outcome


class CascadeTierTests(unittest.TestCase):
    def test_explicit_tier_wins_and_unknown_provider_is_metered(self) -> None:
        slots = review_mod.parse_pool([
            _slot("a", "fa", 1, provider="opencode"),
            _slot("b", "fb", 2, provider="mystery"),
            _slot("c", "fc", 3, provider="opencode", cost_tier="own"),
        ])
        self.assertEqual([slot.cost_tier for slot in slots],
                         ["free", "metered", "own"])

    def test_unknown_tier_is_rejected(self) -> None:
        with self.assertRaises(review_mod.ReviewerConfigError):
            review_mod.parse_pool([_slot("a", "fa", 1, cost_tier="platinum")])

    def test_classic_select_skips_cascade_only(self) -> None:
        pool = _pool()
        plan = asyncio.run(review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 4, builder_family="muse"))
        labels = [slot.label for slot in plan.reviewers]
        self.assertNotIn("openrouter-paid", labels)
        self.assertEqual(labels, ["dsh", "mimo", "nemotron", "gemini"])

    def test_cascade_select_orders_free_own_metered(self) -> None:
        pool = _pool()
        plan = asyncio.run(review_mod.select_round(
            pool, review_mod.InMemorySlotStore(), 5, builder_family="muse",
            cascade_enabled=True))
        self.assertEqual([slot.label for slot in plan.reviewers],
                         ["mimo", "nemotron", "gemini", "dsh", "openrouter-paid"])

    def test_preflight_ignores_cascade_only(self) -> None:
        pool = _pool()
        families = asyncio.run(review_mod.available_families(pool, review_mod.InMemorySlotStore()))
        self.assertNotIn("openrouter", families)
        self.assertEqual(families, {"deepseek", "mimo", "nemotron", "gemini"})


class CascadeQuorumTests(unittest.TestCase):
    def test_classic_ignores_extra_and_builder_family(self) -> None:
        pool = _pool()
        valid = {"mimo": True, "muse-review": True}
        self.assertFalse(review_mod.quorum_met(valid, pool, "muse", 2))
        valid = {"mimo": True, "nemotron": True, "muse-review": True}
        self.assertTrue(review_mod.quorum_met(valid, pool, "muse", 2))

    def test_relaxed_counts_muse_last(self) -> None:
        pool = _pool()
        valid = {"mimo": True, "muse-review": True}
        self.assertTrue(review_mod.quorum_met(valid, pool, "muse", 2, relaxed=True))
        self.assertEqual(
            review_mod._counted_families(valid, pool, "muse", relaxed=True),
            {"mimo", "muse"})


class SpendGuardTests(unittest.TestCase):
    def test_free_and_own_pass_untouched(self) -> None:
        pool = _pool()
        guard = CascadeGuard(_cascade(), threshold_usd=Decimal("0.00"))
        state = review_mod.CascadeSpendState()
        for label in ("mimo", "gemini", "muse-review"):
            slot = next(item for item in pool if item.label == label)
            decision = guard.check(slot, state)
            self.assertTrue(decision.allowed)
        self.assertEqual(state.spent_usd, Decimal("0.00"))

    def test_zero_cap_denies_paid(self) -> None:
        pool = _pool()
        guard = CascadeGuard(_cascade(), threshold_usd=Decimal("0.00"))
        slot = next(item for item in pool if item.label == "openrouter-paid")
        decision = guard.check(slot, review_mod.CascadeSpendState())
        self.assertFalse(decision.allowed)
        self.assertIn("over cap", decision.reason)

    def test_cap_above_threshold_denies(self) -> None:
        pool = _pool()
        guard = CascadeGuard(_cascade(paid_cap_usd="1.00"),
                             threshold_usd=Decimal("0.00"))
        slot = next(item for item in pool if item.label == "openrouter-paid")
        decision = guard.check(slot, review_mod.CascadeSpendState())
        self.assertFalse(decision.allowed)
        self.assertIn("mandate threshold", decision.reason)

    def test_whitelist_rejects_unknown_model(self) -> None:
        pool = _pool()
        guard = CascadeGuard(
            _cascade(paid_cap_usd="1.00", paid_whitelist=["openai/gpt-oss-20b"]),
            threshold_usd=Decimal("1.00"))
        slot = next(item for item in pool if item.label == "openrouter-paid")
        other = review_mod.parse_pool([_slot(
            "or-x", "openrouter", 71, cascade_only=True, cost_tier="metered",
            model="openrouter/evil-model",
            command=["opencode", "run", "-m", "openrouter/evil-model"])])[0]
        self.assertFalse(guard.check(other, review_mod.CascadeSpendState()).allowed)
        self.assertTrue(guard.check(slot, review_mod.CascadeSpendState()).allowed)

    def test_spent_accrues_and_third_attempt_denies(self) -> None:
        pool = _pool()
        guard = CascadeGuard(
            _cascade(paid_cap_usd="0.10", paid_estimate_usd="0.05"),
            threshold_usd=Decimal("1.00"))
        slot = next(item for item in pool if item.label == "openrouter-paid")
        state = review_mod.CascadeSpendState()
        self.assertTrue(guard.check(slot, state).allowed)
        self.assertTrue(guard.check(slot, state).allowed)
        self.assertFalse(guard.check(slot, state).allowed)
        self.assertEqual(state.spent_usd, Decimal("0.10"))

    def test_dsh_runs_on_unmetered_estimate(self) -> None:
        pool = _pool()
        guard = CascadeGuard(_cascade(), threshold_usd=Decimal("0.00"))
        slot = next(item for item in pool if item.label == "dsh")
        decision = guard.check(slot, review_mod.CascadeSpendState())
        self.assertTrue(decision.allowed)

    def test_unreadable_money_fails_closed(self) -> None:
        pool = _pool()
        guard = CascadeGuard(_cascade(paid_cap_usd="much"),
                             threshold_usd=Decimal("1.00"))
        slot = next(item for item in pool if item.label == "dsh")
        self.assertFalse(guard.check(slot, review_mod.CascadeSpendState()).allowed)

    def test_mandate_threshold_matches_prepaid_credit(self) -> None:
        self.assertEqual(review_mod.cascade_mandate_threshold(), Decimal("10.00"))


class CascadeRoundTests(unittest.TestCase):
    def _run(self, script: dict[str, bool], cascade: dict[str, Any] | None,
             recovery: dict[str, Any] | None = None) -> review_mod.QuorumRound:
        pool = _pool()
        calls: list[str] = []

        async def run_one(slot: review_mod.ReviewSlot, attempt: int,
                          _previous: Any) -> dict[str, Any]:
            calls.append(slot.label)
            return _outcome(slot, script.get(slot.label, False))

        async def go() -> review_mod.QuorumRound:
            plan = await review_mod.select_round(
                pool, review_mod.InMemorySlotStore(), 3,
                builder_family="muse",
                cascade_enabled=review_mod.cascade_enabled(cascade))
            result = await review_mod.run_quorum_round(
                pool, review_mod.InMemorySlotStore(), plan, run_one,
                builder_family="muse", quorum=2, recovery=recovery,
                cascade=cascade)
            result.outcomes.append({"slot": "__calls__", "order": calls})
            return result

        return asyncio.run(go())

    def test_classic_round_ignores_cascade_slots_and_relaxation(self) -> None:
        result = self._run({"mimo": True, "muse-review": True}, None)
        self.assertFalse(result.met)
        self.assertFalse(result.relaxed)
        self.assertEqual(result.note, "")
        labels = [str(item.get("slot")) for item in result.outcomes
                  if not str(item.get("slot")).startswith("__")]
        self.assertNotIn("openrouter-paid", labels)

    def test_cascade_orders_tiers_and_denies_paid(self) -> None:
        result = self._run(
            {"mimo": False, "nemotron": False, "gemini": False,
             "dsh": False, "openrouter-paid": True}, _cascade())
        order = next(item["order"] for item in result.outcomes
                     if str(item.get("slot")) == "__calls__")
        counting = [label for label in order if label != "muse-review"]
        self.assertEqual(counting[:4], ["mimo", "nemotron", "gemini", "dsh"])
        denied = [item for item in result.outcomes if item.get("kind") == "denied"]
        self.assertEqual([item.get("slot") for item in denied], ["openrouter-paid"])
        self.assertIn("openrouter-paid", result.summary())
        self.assertIn("tiers:", result.note)

    def test_relaxed_quorum_counts_muse_audited(self) -> None:
        result = self._run(
            {"mimo": True, "nemotron": False, "gemini": False,
             "dsh": False, "muse-review": True}, _cascade())
        self.assertTrue(result.met)
        self.assertTrue(result.relaxed)
        relaxed = [item for item in result.outcomes if item.get("relaxed")]
        self.assertEqual([item.get("slot") for item in relaxed], ["muse-review"])
        self.assertIn("relaxed quorum", result.note)

    def test_block_lists_tiers_when_everything_fails(self) -> None:
        result = self._run({}, _cascade())
        self.assertFalse(result.met)
        self.assertFalse(result.relaxed)
        self.assertIn("blocked: no tier left", result.note)
        self.assertIn("tiers:free,own,metered", result.note.replace(" ", ""))

    def test_outcomes_carry_tier_receipts(self) -> None:
        result = self._run({"mimo": True, "nemotron": True}, _cascade())
        by_slot = {str(item.get("slot")): item for item in result.outcomes
                   if not str(item.get("slot")).startswith("__")}
        self.assertEqual(by_slot["mimo"]["tier"], "free")
        self.assertTrue(result.met)
        self.assertFalse(result.relaxed)


class CascadeConfigTests(unittest.TestCase):
    def test_production_pool_tags_and_paid_slot(self) -> None:
        self.assertFalse(review_mod.CASCADE_DEFAULTS["enabled"])
        self.assertFalse(review_mod.cascade_enabled(None))
        cfg = ConfigLoader(Settings()).load_project("jardal-openceo")
        review_cfg = cfg["review"]["review"]
        self.assertTrue(review_mod.cascade_enabled(review_cfg.get("cascade")))
        pool = review_mod.parse_pool(cfg["review"]["reviewers"])
        by_label = {slot.label: slot for slot in pool}
        self.assertEqual(by_label["mimo"].cost_tier, "free")
        self.assertEqual(by_label["gemini"].cost_tier, "own")
        self.assertEqual(by_label["dsh"].cost_tier, "metered")
        self.assertEqual(by_label["muse-review"].cost_tier, "own")
        paid = by_label["openrouter-paid"]
        self.assertTrue(paid.cascade_only)
        self.assertEqual(paid.cost_tier, "metered")
        cascade = review_cfg["cascade"]
        self.assertIn("openai/gpt-oss-20b", cascade["paid_whitelist"])
        self.assertEqual(cascade["paid_cap_usd"], "0.50")

    def test_paid_slot_handoff_shape(self) -> None:
        from model_routing import reviewer_isolation as iso_mod  # noqa: E402

        cfg = ConfigLoader(Settings()).load_project("jardal-openceo")
        raw = next(item for item in cfg["review"]["reviewers"]
                   if item["label"] == "openrouter-paid")
        self.assertTrue(raw["review_hardened"])
        target = raw["review_target"]
        iso_mod.check_group_b_handoff(
            target["providers"], {target["env_var"]: "sandbox-fake"})
        self.assertIn("openrouter/openai/gpt-oss-20b", raw["command"][-1])


if __name__ == "__main__":
    unittest.main()
