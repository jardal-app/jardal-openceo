"""Free-router activation: the live ranking serves review slots (TASK-2026-00317).

Offline pilot under the rank-no-discard design (TASK-2026-00319): the
shipped defaults enable the router, a thin-evidence low-quality chain
still serves a distinct-family pair whose receipts carry route_type,
and the manual switch keeps the static pool untouched. No network, no
model calls, no host-state writes (receipts use the pure
``build_receipt``; the availability store lives under ``tmp_path``).
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from config_loader import ConfigLoader  # noqa: E402
from model_routing import review_wiring as wiring  # noqa: E402
from model_routing.availability import AvailabilityStore, Candidate  # noqa: E402
from model_routing.models import ProfileRanking  # noqa: E402


def _entry(rank, canonical, family, deployment, provider="openrouter", quality=0.3):
    return {
        "rank": rank, "canonical_model_id": canonical,
        "family_id": family,
        "primary": {"deployment_id": deployment, "provider": provider},
        "backups": [],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"coding": quality},
        "quality_sources": ["artificial-analysis:index4.3@test"],
    }


def _ranking(order):
    return ProfileRanking.model_validate({
        "profile": "free-review",
        "order": order,
        "experimental": [],
        "pending_top3": None,
        "provenance": {
            "catalog_snapshot": "catalog-test",
            "catalog_sha256": "0" * 64,
        },
    })


def _thin_chain():
    return _ranking([
        _entry(1, "org/thin-a", "alpha", "openrouter/org/thin-a:free", quality=0.30),
        _entry(2, "org/thin-b", "beta", "openrouter/org/thin-b:free", quality=0.25),
        _entry(3, "org/thin-c", "gamma", "openrouter/org/thin-c:free", quality=0.20),
    ])


class ActivationDefaultsTests(unittest.TestCase):
    def test_shipped_defaults_enable_free_router(self):
        cfg = ConfigLoader._merge_with_defaults("", {})
        block = cfg["review"]["review"]["free_router"]
        self.assertIs(block["enabled"], True)
        settings = wiring.free_router_settings(cfg["review"]["review"])
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["round2_policy"], "reuse_best_plus_novel")
        self.assertEqual(settings["snapshot_max_age_hours"], 30)


class ThinChainPilotTests(unittest.TestCase):
    def test_thin_evidence_chain_serves_distinct_pair(self):
        candidates = wiring.review_candidates(_thin_chain())
        self.assertEqual(len(candidates), 3)
        store = AvailabilityStore(Path(self._tmp()) / "avail.sqlite")
        try:
            first, second = wiring.pick_pair(candidates, store)
        finally:
            store.close()
        self.assertEqual(first.family_id, "alpha")
        self.assertEqual(second.family_id, "beta")
        self.assertNotEqual(first.family_id, second.family_id)

    def test_dynamic_entries_free_with_route_receipts(self):
        ranking = _thin_chain()
        candidates = wiring.review_candidates(ranking)
        store = AvailabilityStore(Path(self._tmp()) / "avail.sqlite")
        try:
            pair = wiring.pick_pair(candidates, store)
        finally:
            store.close()
        entries = [wiring.dynamic_entry(f"free-review-{i}", i, cand)
                   for i, cand in enumerate(pair, start=1)]
        self.assertTrue(all(entry["cost_tier"] == "free" for entry in entries))
        self.assertTrue(all(entry["command"][:3] == ["opencode", "run", "-m"]
                            for entry in entries))
        self.assertEqual(len({entry["family"] for entry in entries}), 2)
        snapshot = wiring.RoundSnapshot(
            document=None, profile=ranking,
            file_sha256="sha256:test", catalog_snapshot="catalog-test",
            generated_at=datetime.now(timezone.utc).isoformat())
        routing = wiring.RoundRouting(
            run_id="pilot-317", round="1", snapshot=snapshot, store=store,
            candidates=list(candidates), labels=("free-review-1", "free-review-2"),
            states={label: wiring.LabelState(requested=cand, effective=cand)
                    for label, cand in zip(("free-review-1", "free-review-2"), pair)},
            round_families=frozenset({cand.family_id for cand in pair}),
            receipts_dir=Path(self._tmp()))
        receipts = [wiring.build_receipt(
            routing, label, report_text="pilot report",
            reviewed_commit="c0ffee", started_at="2026-10-27T00:00:00+00:00",
            finished_at="2026-10-27T00:01:00+00:00")
            for label in routing.labels]
        for receipt in receipts:
            self.assertTrue(receipt.route_type)
            self.assertIsNotNone(receipt.rank)
            self.assertIsNotNone(receipt.quality)
        self.assertEqual(len({receipt.family_id for receipt in receipts}), 2)

    def _tmp(self):
        import tempfile
        tmp = getattr(self, "_scratch", None)
        if tmp is None:
            tmp = tempfile.mkdtemp(prefix="pilot-317-")
            self._scratch = tmp
        return tmp


class ManualSwitchTests(unittest.TestCase):
    def test_switch_off_keeps_static_pool(self):
        static = [{"label": "dsh", "provider": "dsh", "model": "deepseek-v4.1-flash",
                   "family": "deepseek", "priority": 3, "active": True}]
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            assembly = wiring.assemble_review_pool(
                static, review_cfg={"free_router": {"enabled": False}},
                run_id="pilot-317", round="1", builder_family="muse",
                receipts_dir=Path(tmp))
        self.assertIsNone(assembly.routing)
        self.assertEqual([entry["label"] for entry in assembly.raw_pool], ["dsh"])


if __name__ == "__main__":
    unittest.main()
