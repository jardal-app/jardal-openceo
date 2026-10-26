"""Dev reviewers fed by the free-review ranking (TASK-2026-00293).

Offline wiring units (pair pick, exclusions, reservations, R2
policies, §11.6 fallback, §11.7 receipts) plus engine integration
with scripted executors and injected failures for A/B/C (simple
round) and S (two rounds, T04–T09) incl. T17/T18. No network, no
CLI, no live model calls: the single real reviewer call lives in
test_model_routing_review_live.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import review_wiring as wiring  # noqa: E402
from model_routing.availability import (  # noqa: E402
    AvailabilityStore,
    Candidate,
    NoEligibleCandidate,
    ReservationConflict,
    get_next_candidate,
)
from model_routing.errors import classify_text  # noqa: E402
from orchestration import review as review_mod  # noqa: E402


def _entry(rank, canonical, family, deployment, provider="opencode",
           quality=0.8, backups=()):
    return {
        "rank": rank, "canonical_model_id": canonical,
        "family_id": family,
        "primary": {"deployment_id": deployment, "provider": provider},
        "backups": [{"deployment_id": dep, "provider": prov}
                    for dep, prov in backups],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"coding": quality},
        "quality_sources": ["artificial-analysis:index4.3@test"],
    }


def _rankings_doc(order, generated_at=None):
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "generator": "test",
        "catalog_snapshot": "catalog-test",
        "catalog_sha256": "0" * 64,
        "profiles": {
            "free-review": {
                "profile": "free-review",
                "order": order,
                "experimental": [],
                "pending_top3": None,
                "provenance": {
                    "catalog_snapshot": "catalog-test",
                    "catalog_sha256": "0" * 64,
                },
            }
        },
    }


def _order_abc():
    return [
        _entry(1, "org/model-a", "alpha", "opencode/model-a-free",
               quality=0.90),
        _entry(2, "org/model-b", "beta", "opencode/model-b-free",
               quality=0.85),
        _entry(3, "org/model-c", "gamma", "opencode/model-c-free",
               quality=0.80),
    ]


def _static_pool():
    return [
        {"label": "dsh", "provider": "dsh", "model": "deepseek-v4.1-flash",
         "family": "deepseek", "priority": 3, "active": True,
         "prompt_via": "stdin", "command": ["dsh"]},
        {"label": "mimo", "provider": "opencode",
         "model": "opencode/mimo-v2.6-flash-free", "family": "mimo",
         "priority": 4, "active": True, "prompt_via": "argv",
         "command": ["opencode", "run", "-m",
                     "opencode/mimo-v2.6-flash-free"]},
        {"label": "nemotron", "provider": "opencode",
         "model": "opencode/nemotron-3-ultra-free", "family": "nemotron",
         "priority": 5, "active": True, "prompt_via": "argv",
         "command": ["opencode", "run", "-m",
                     "opencode/nemotron-3-ultra-free"]},
        {"label": "claudep", "provider": "claude", "model": "deepseek-flash",
         "family": "deepseek", "priority": 9, "active": False,
         "command": ["claudep"]},
        {"label": "gemini", "provider": "gemini", "model": "gemini",
         "family": "gemini", "account": "agyd", "priority": 60,
         "active": True, "prompt_via": "argv",
         "headless_args": ["--print"]},
        {"label": "muse-review", "provider": "muse",
         "model": "muse-spark-1.3-contributor", "family": "muse",
         "priority": 99, "active": True, "extra": True,
         "command": ["muse"]},
        {"label": "reserve-minimax", "provider": "opencode",
         "model": "opencode/minimax-m3.1-flash-free", "family": "minimax",
         "priority": 100, "active": True, "reserve_only": True,
         "prompt_via": "argv",
         "command": ["opencode", "run", "-m",
                     "opencode/minimax-m3.1-flash-free"]},
    ]


@pytest.fixture()
def xdg(tmp_path, monkeypatch):
    base = tmp_path / "xdg"
    (base / "cfg").mkdir(parents=True)
    (base / "st").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "st"))
    return base


def _write_rankings(base, order):
    target = base / "cfg" / "free-router"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "rankings.json"
    path.write_text(json.dumps(_rankings_doc(order)), encoding="utf-8")
    return path


def _store(tmp_path):
    return AvailabilityStore(tmp_path / "avail.sqlite")


def _candidates_abc(store=None):
    from model_routing.models import ProfileRanking
    ranking = ProfileRanking.model_validate(
        _rankings_doc(_order_abc())["profiles"]["free-review"])
    return wiring.review_candidates(ranking)


class SettingsTests(unittest.TestCase):
    def test_defaults_active(self):
        settings = wiring.free_router_settings({})
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["round2_policy"], "reuse_best_plus_novel")
        self.assertEqual(settings["snapshot_max_age_hours"], 30)

    def test_absent_block_defaults_active(self):
        self.assertTrue(wiring.free_router_settings(None)["enabled"])
        self.assertTrue(wiring.free_router_settings({"quorum": 2})["enabled"])

    def test_switch_off(self):
        settings = wiring.free_router_settings({"free_router": {"enabled": False}})
        self.assertFalse(settings["enabled"])

    def test_non_mapping_rejected(self):
        with self.assertRaises(wiring.ReviewRoutingError):
            wiring.free_router_settings({"free_router": True})

    def test_bad_max_age_rejected(self):
        with self.assertRaises(wiring.ReviewRoutingError):
            wiring.free_router_settings(
                {"free_router": {"snapshot_max_age_hours": "soon"}})
        with self.assertRaises(wiring.ReviewRoutingError):
            wiring.free_router_settings(
                {"free_router": {"snapshot_max_age_hours": 0}})

    def test_bad_round2_policy_rejected_early(self):
        with self.assertRaises(wiring.ReviewRoutingError):
            wiring.free_router_settings(
                {"free_router": {"round2_policy": "nope"}})


class SnapshotTests(unittest.TestCase):
    def test_missing_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wiring.load_round_snapshot(
                Path(tmp) / "rankings.json"))

    def test_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rankings.json"
            payload = json.dumps(_rankings_doc(_order_abc()))
            path.write_text(payload, encoding="utf-8")
            snapshot = wiring.load_round_snapshot(path)
            assert snapshot is not None
            self.assertEqual(len(snapshot.profile.order), 3)
            self.assertTrue(snapshot.file_sha256.startswith("sha256:"))
            self.assertEqual(snapshot.catalog_snapshot, "catalog-test")

    def test_invalid_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rankings.json"
            path.write_text("{nope", encoding="utf-8")
            with self.assertRaises(wiring.ReviewRoutingError):
                wiring.load_round_snapshot(path)

    def test_expired_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rankings.json"
            old = (datetime.now(timezone.utc)
                   - timedelta(hours=31)).isoformat()
            path.write_text(json.dumps(_rankings_doc(_order_abc(), old)),
                            encoding="utf-8")
            with self.assertRaises(wiring.ReviewRoutingError):
                wiring.load_round_snapshot(path)

    def test_missing_profile_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rankings.json"
            doc = _rankings_doc(_order_abc())
            doc["profiles"] = {}
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(wiring.ReviewRoutingError):
                wiring.load_round_snapshot(path)


class CandidatesTests(unittest.TestCase):
    def test_order_and_opencode_routes(self):
        candidates = _candidates_abc()
        self.assertEqual(
            [item.deployment_id for item in candidates],
            ["opencode/model-a-free", "opencode/model-b-free",
             "opencode/model-c-free"])
        self.assertTrue(
            all(item.provider == "opencode" for item in candidates))

    def test_opencode_backup_serves(self):
        from model_routing.models import ProfileRanking
        order = [_entry(1, "org/model-x", "xray", "groq/model-x",
                        provider="groq", quality=0.9,
                        backups=[("opencode/model-x-free", "opencode")])]
        ranking = ProfileRanking.model_validate(
            _rankings_doc(order)["profiles"]["free-review"])
        candidates = wiring.review_candidates(ranking)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].deployment_id, "opencode/model-x-free")

    def test_no_opencode_route_skipped(self):
        from model_routing.models import ProfileRanking
        order = [_entry(1, "org/model-x", "xray", "groq/model-x",
                        provider="groq", quality=0.9)]
        ranking = ProfileRanking.model_validate(
            _rankings_doc(order)["profiles"]["free-review"])
        self.assertEqual(wiring.review_candidates(ranking), [])

    def test_unverified_families_skipped(self):
        from model_routing.models import ProfileRanking
        order = [
            _entry(1, "org/model-x", "anonymous", "opencode/model-x-free",
                   quality=0.9),
            _entry(2, "org/model-y", "unknown-family",
                   "opencode/model-y-free", quality=0.85),
            _entry(3, "org/model-z", "zed", "opencode/model-z-free",
                   quality=0.8),
        ]
        ranking = ProfileRanking.model_validate(
            _rankings_doc(order)["profiles"]["free-review"])
        candidates = wiring.review_candidates(ranking)
        self.assertEqual([item.family_id for item in candidates], ["zed"])

    def test_floor_and_drop_applied(self):
        from model_routing.models import ProfileRanking
        order = [
            _entry(1, "org/model-a", "alpha", "opencode/model-a-free",
                   quality=0.90),
            _entry(2, "org/model-b", "beta", "opencode/model-b-free",
                   quality=0.60),
        ]
        ranking = ProfileRanking.model_validate(
            _rankings_doc(order)["profiles"]["free-review"])
        candidates = wiring.review_candidates(ranking)
        self.assertEqual([item.family_id for item in candidates], ["alpha"])

    def test_same_canonical_collapses_to_first(self):
        from model_routing.models import ProfileRanking
        order = [
            _entry(1, "org/model-a", "alpha", "opencode/model-a-free",
                   quality=0.90),
            _entry(2, "org/model-a", "alpha", "opencode/model-a-alt",
                   quality=0.85),
        ]
        ranking = ProfileRanking.model_validate(
            _rankings_doc(order)["profiles"]["free-review"])
        candidates = wiring.review_candidates(ranking)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].deployment_id, "opencode/model-a-free")


class ExclusionsTests(unittest.TestCase):
    def test_builder_and_static_participants(self):
        excluded = wiring.static_exclusions(_static_pool(), builder_family="muse")
        self.assertEqual(excluded, frozenset({"muse", "deepseek", "gemini"}))

    def test_opencode_extras_and_inactive_skipped(self):
        pool = [
            {"label": "mimo", "provider": "opencode", "family": "mimo",
             "active": True},
            {"label": "off", "provider": "zzz", "family": "zed",
             "active": False},
            {"label": "extra", "provider": "yyy", "family": "yankee",
             "active": True, "extra": True},
        ]
        self.assertEqual(
            wiring.static_exclusions(pool, builder_family="muse"),
            frozenset({"muse"}))

    def test_codex_builder_excluded(self):
        excluded = wiring.static_exclusions(_static_pool(),
                                            builder_family="codex")
        self.assertIn("codex", excluded)
        self.assertIn("deepseek", excluded)


class PickPairTests(unittest.TestCase):
    def test_top_two_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                first, second = wiring.pick_pair(
                    _candidates_abc(), store, exclude_families=frozenset())
            finally:
                store.close()
        self.assertEqual(first.family_id, "alpha")
        self.assertEqual(second.family_id, "beta")

    def test_exclusions_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                first, second = wiring.pick_pair(
                    _candidates_abc(), store,
                    exclude_families=frozenset({"alpha", "muse"}))
            finally:
                store.close()
        self.assertEqual(first.family_id, "beta")
        self.assertEqual(second.family_id, "gamma")

    def test_single_family_is_no_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                with self.assertRaises(wiring.NoReviewPair):
                    wiring.pick_pair(
                        _candidates_abc()[:1], store,
                        exclude_families=frozenset())
            finally:
                store.close()

    def test_all_excluded_is_no_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                with self.assertRaises(wiring.NoReviewPair):
                    wiring.pick_pair(
                        _candidates_abc(), store,
                        exclude_families=frozenset({"alpha", "beta", "gamma"}))
            finally:
                store.close()

    def test_cooled_deployment_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                store.set_cooldown("opencode/model-a-free", "test", seconds=600)
                first, _ = wiring.pick_pair(
                    _candidates_abc(), store, exclude_families=frozenset())
            finally:
                store.close()
        self.assertEqual(first.family_id, "beta")

    def test_same_canonical_never_twice(self):
        first = Candidate("opencode/model-a-free", "alpha", "org/model-a",
                          "opencode", 0.9)
        backup = Candidate("groq/model-a", "alpha", "org/model-a",
                           "groq", 0.9)
        other = Candidate("opencode/model-b-free", "beta", "org/model-b",
                          "opencode", 0.8)
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(Path(tmp))
            try:
                picked_first, picked_second = wiring.pick_pair(
                    [first, backup, other], store,
                    exclude_families=frozenset())
            finally:
                store.close()
        self.assertEqual(picked_first.canonical_model_id, "org/model-a")
        self.assertEqual(picked_second.canonical_model_id, "org/model-b")


class AssemblyTests(unittest.TestCase):
    def _assemble(self, base, pool, review_cfg=None, **kwargs):
        kwargs.setdefault("run_id", "run-t04")
        kwargs.setdefault("round", "1")
        kwargs.setdefault("builder_family", "muse")
        kwargs.setdefault("receipts_dir", base / "receipts")
        return wiring.assemble_review_pool(
            pool, review_cfg=review_cfg or {}, **kwargs)

    def test_dynamic_pool_replaces_free_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                assembly = self._assemble(base, _static_pool())
                try:
                    self.assertIsNotNone(assembly.routing)
                    labels = [entry["label"] for entry in assembly.raw_pool]
                    self.assertIn("free-review-1", labels)
                    self.assertIn("free-review-2", labels)
                    self.assertNotIn("mimo", labels)
                    self.assertNotIn("nemotron", labels)
                    self.assertNotIn("reserve-minimax", labels)
                    for keep in ("dsh", "claudep", "gemini", "muse-review"):
                        self.assertIn(keep, labels)
                    first = next(entry for entry in assembly.raw_pool
                                 if entry["label"] == "free-review-1")
                    self.assertEqual(first["model"], "opencode/model-a-free")
                    self.assertEqual(first["family"], "alpha")
                    self.assertEqual(first["priority"], 4)
                    self.assertEqual(first["command"],
                                     ["opencode", "run", "-m",
                                      "opencode/model-a-free"])
                    self.assertTrue(first["review_hardened"])
                    self.assertEqual(assembly.prefer_families,
                                     ("alpha", "beta"))
                finally:
                    if assembly.routing is not None:
                        assembly.routing.close()

    def test_static_slots_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                assembly = self._assemble(base, _static_pool())
                try:
                    by_label = {entry["label"]: entry
                                for entry in assembly.raw_pool}
                    gemini = by_label["gemini"]
                    self.assertEqual(gemini["model"], "gemini")
                    self.assertEqual(gemini["account"], "agyd")
                    self.assertNotIn("review_hardened", gemini)
                    dsh = by_label["dsh"]
                    self.assertEqual(dsh["model"], "deepseek-v4.1-flash")
                finally:
                    if assembly.routing is not None:
                        assembly.routing.close()

    def test_reservations_atomic_and_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                assembly = self._assemble(base, _static_pool())
                routing = assembly.routing
                assert routing is not None
                try:
                    held = routing.store.reservations("run-t04", "1")
                    self.assertEqual(len(held), 2)
                    self.assertEqual(len({item.family_id for item in held}), 2)
                    audit = routing.store.audit_since("run-t04", 0.0)
                    self.assertTrue(
                        any(event == "review_round_assembled"
                            for _, event, _ in audit))
                finally:
                    routing.close()

    def test_switch_off_keeps_manual_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                assembly = self._assemble(
                    base, _static_pool(),
                    {"free_router": {"enabled": False}})
                self.assertIsNone(assembly.routing)
                labels = [entry["label"] for entry in assembly.raw_pool]
                self.assertIn("mimo", labels)
                self.assertNotIn("free-review-1", labels)
                self.assertTrue(all("review_hardened" not in entry
                                    for entry in assembly.raw_pool))

    def test_missing_snapshot_keeps_marked_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                assembly = self._assemble(base, _static_pool())
                self.assertIsNone(assembly.routing)
                by_label = {entry["label"]: entry
                            for entry in assembly.raw_pool}
                self.assertTrue(by_label["mimo"]["review_hardened"])
                self.assertNotIn("review_hardened", by_label["gemini"])
                self.assertFalse(
                    (base / "st" / "free-router").exists())

    def test_no_pair_falls_back_static_with_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc()[:1])
                assembly = self._assemble(base, _static_pool())
                self.assertIsNone(assembly.routing)
                labels = [entry["label"] for entry in assembly.raw_pool]
                self.assertIn("mimo", labels)
                probe = AvailabilityStore(
                    base / "st" / "free-router" / "availability.sqlite")
                try:
                    audit = probe.audit_since("run-t04", 0.0)
                finally:
                    probe.close()
                self.assertTrue(
                    any(event == "review_pair_unavailable"
                        for _, event, _ in audit))

    def test_reserve_conflict_falls_back_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                         "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                squatter = AvailabilityStore(
                    base / "st" / "free-router" / "availability.sqlite")
                try:
                    squatter.reserve(
                        "run-t04", "1", "other", "org/model-a", "alpha",
                        "opencode/model-a-free")
                finally:
                    squatter.close()
                assembly = self._assemble(base, _static_pool())
                self.assertIsNone(assembly.routing)
                labels = [entry["label"] for entry in assembly.raw_pool]
                self.assertIn("mimo", labels)

    def test_bad_shape_parks_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(wiring.ReviewRoutingError):
                self._assemble(base, _static_pool(),
                               {"free_router": {"enabled": "maybe",
                                                "snapshot_max_age_hours": -1}})

    def test_sync_executor_pool(self):
        class _Fake:
            pass

        wiring.sync_executor_pool(_Fake(), [{"label": "x"}])

        class _Recording:
            def __init__(self):
                self.seen = None

            def register_pool_entries(self, entries):
                self.seen = entries

        fake = _Recording()
        wiring.sync_executor_pool(fake, [{"label": "free-review-1"}])
        self.assertEqual(fake.seen, [{"label": "free-review-1"}])

    def test_bad_round2_policy_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(wiring.ReviewRoutingError):
                self._assemble(
                    base, _static_pool(),
                    {"free_router": {"round2_policy": "nope"}})

    def test_dynamic_entry_is_hardened_opencode(self):
        candidate = Candidate(
            deployment_id="opencode/model-c-free", family_id="gamma",
            canonical_model_id="org/model-c", provider="opencode",
            quality=0.8)
        entry = wiring.dynamic_entry("free-review-1", 4, candidate)
        self.assertEqual(entry["label"], "free-review-1")
        self.assertEqual(entry["model"], "opencode/model-c-free")
        self.assertEqual(entry["family"], "gamma")
        self.assertEqual(
            entry["command"],
            ["opencode", "run", "-m", "opencode/model-c-free"])
        self.assertTrue(entry["review_hardened"])

    def test_fallback_sync_updates_live_pool(self):
        from orchestration.live import ProcessExecutor

        candidate = Candidate(
            deployment_id="opencode/model-c-free", family_id="gamma",
            canonical_model_id="org/model-c", provider="opencode",
            quality=0.8)
        executor = ProcessExecutor(pool=[{
            "label": "free-review-1", "provider": "opencode",
            "model": "opencode/model-a-free", "family": "alpha",
            "priority": 4, "active": True, "prompt_via": "argv",
            "command": ["opencode", "run", "-m",
                        "opencode/model-a-free"],
            "review_hardened": True}])
        wiring.sync_executor_pool(
            executor, [wiring.dynamic_entry("free-review-1", 4, candidate)])
        spec, _, _ = executor._resolve("review:free-review-1")
        assert spec is not None
        self.assertEqual(spec["model"], "opencode/model-c-free")
        self.assertEqual(
            spec["command"],
            ["opencode", "run", "-m", "opencode/model-c-free"])


class ReceiptTests(unittest.TestCase):
    def _routing(self, base):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                     "XDG_STATE_HOME": str(base / "st")}):
            (base / "cfg").mkdir(exist_ok=True)
            (base / "st").mkdir(exist_ok=True)
            _write_rankings(base, _order_abc())
            assembly = wiring.assemble_review_pool(
                _static_pool(), review_cfg={}, run_id="run-r",
                round="1", builder_family="muse",
                receipts_dir=base / "receipts")
        assert assembly.routing is not None
        return assembly.routing

    def test_record_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routing = self._routing(base)
            try:
                receipt = wiring.record_receipt(
                    routing, "free-review-1", report_text="No findings.\n",
                    reviewed_commit="abc123",
                    started_at="2026-10-23T00:00:00+00:00",
                    finished_at="2026-10-23T00:01:00+00:00")
                self.assertEqual(receipt.slot, "free-review-1")
                self.assertEqual(receipt.requested_model,
                                 "opencode/model-a-free")
                self.assertEqual(receipt.effective_model,
                                 "opencode/model-a-free")
                self.assertEqual(receipt.family_id, "alpha")
                self.assertEqual(receipt.provider, "opencode")
                self.assertEqual(receipt.rank, 1)
                self.assertTrue(
                    receipt.rankings_snapshot.startswith("sha256:"))
                self.assertEqual(receipt.result, "validated")
                self.assertEqual(receipt.fallback_count, 0)
                json_path, md_path = wiring.receipt_filenames(
                    base / "receipts", "run-r", "1", "free-review-1")
                self.assertTrue(json_path.is_file())
                self.assertTrue(md_path.is_file())
                hit = wiring.load_valid_receipt(
                    base / "receipts", run_id="run-r", round="1",
                    slot="free-review-1", reviewed_commit="abc123")
                assert hit is not None
                back, text = hit
                self.assertEqual(back, receipt)
                self.assertEqual(text, "No findings.\n")
            finally:
                routing.close()

    def test_reload_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routing = self._routing(base)
            try:
                wiring.record_receipt(
                    routing, "free-review-1", report_text="No findings.\n",
                    reviewed_commit="abc123", started_at="", finished_at="")
                self.assertIsNone(wiring.load_valid_receipt(
                    base / "receipts", run_id="run-r", round="1",
                    slot="free-review-1", reviewed_commit="other"))
                self.assertIsNone(wiring.load_valid_receipt(
                    base / "receipts", run_id="run-r", round="2",
                    slot="free-review-1", reviewed_commit="abc123"))
                _, md_path = wiring.receipt_filenames(
                    base / "receipts", "run-r", "1", "free-review-1")
                md_path.write_text("tampered\n", encoding="utf-8")
                self.assertIsNone(wiring.load_valid_receipt(
                    base / "receipts", run_id="run-r", round="1",
                    slot="free-review-1", reviewed_commit="abc123"))
            finally:
                routing.close()

    def test_receipt_json_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routing = self._routing(base)
            try:
                receipt = wiring.record_receipt(
                    routing, "free-review-2", report_text="No findings.\n",
                    reviewed_commit="abc123", started_at="", finished_at="")
                back = wiring.ReviewReceipt.from_dict(receipt.to_dict())
                self.assertEqual(back, receipt)
            finally:
                routing.close()


class FallbackTests(unittest.TestCase):
    def _routing(self, base):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "cfg"),
                                     "XDG_STATE_HOME": str(base / "st")}):
            (base / "cfg").mkdir(exist_ok=True)
            (base / "st").mkdir(exist_ok=True)
            _write_rankings(base, _order_abc())
            assembly = wiring.assemble_review_pool(
                _static_pool(), review_cfg={}, run_id="run-f",
                round="1", builder_family="muse",
                receipts_dir=base / "receipts")
        assert assembly.routing is not None
        return assembly.routing

    def test_not_found_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found: opencode/model-a-free")
                assert nxt is not None
                self.assertEqual(nxt.family_id, "gamma")
                state = routing.states["free-review-1"]
                self.assertEqual(state.effective, nxt)
                self.assertEqual(state.fallback_count, 1)
                held = routing.store.reservations("run-f", "1")
                mine = [item for item in held
                        if item.slot == "free-review-1"]
                self.assertEqual(len(mine), 1)
                self.assertEqual(mine[0].family_id, "gamma")
                rows = routing.store.errors_since(0.0)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].error_class, "not_found")
                self.assertEqual(
                    routing.store.route_disabled("opencode/model-a-free") is not None,
                    True)
            finally:
                routing.close()

    def test_unclassified_slot_error_stays(self):
        self.assertEqual(
            classify_text("weird output without marks").error_class,
            "unclassified")
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                self.assertIsNone(wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="slot_error",
                    text="weird output without marks"))
                self.assertEqual(
                    routing.store.errors_since(0.0), [])
            finally:
                routing.close()

    def test_unclassified_cooling_down_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="cooling_down",
                    text="slot limit hit")
                assert nxt is not None
                rows = routing.store.errors_since(0.0)
                self.assertEqual(rows[0].error_class, "unclassified")
            finally:
                routing.close()

    def test_first_timeout_stays_repeat_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                self.assertIsNone(wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="timeout",
                    text="timeout after 900s", timeout_repeat=False))
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="timeout",
                    text="timeout after 900s", timeout_repeat=True)
                assert nxt is not None
                rows = routing.store.errors_since(0.0)
                self.assertEqual(rows[0].error_class, "server_error")
            finally:
                routing.close()

    def test_forbidden_text_never_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                self.assertIsNone(wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="slot_error",
                    text="provider.auth 403 forbidden"))
            finally:
                routing.close()

    def test_budget_caps_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            routing = self._routing(Path(tmp))
            try:
                routing.states["free-review-1"].fallback_count = (
                    wiring.REVIEW_FALLBACK_MAX)
                self.assertIsNone(wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found"))
            finally:
                routing.close()

    def test_exhausted_chain_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir(exist_ok=True)
                (base / "st").mkdir(exist_ok=True)
                _write_rankings(base, _order_abc()[:2])
                assembly = wiring.assemble_review_pool(
                    _static_pool(), review_cfg={}, run_id="run-e",
                    round="1", builder_family="muse",
                    receipts_dir=base / "receipts")
            routing = assembly.routing
            assert routing is not None
            try:
                self.assertIsNone(wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found"))
                audit = routing.store.audit_since("run-e", 0.0)
                self.assertTrue(
                    any(event == "review_fallback_exhausted"
                        for _, event, _ in audit))
            finally:
                routing.close()

    def test_conflict_skips_to_next_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir(exist_ok=True)
                (base / "st").mkdir(exist_ok=True)
                order = _order_abc() + [
                    _entry(4, "org/model-d", "delta",
                           "opencode/model-d-free", quality=0.78)]
                _write_rankings(base, order)
                assembly = wiring.assemble_review_pool(
                    _static_pool(), review_cfg={}, run_id="run-f",
                    round="1", builder_family="muse",
                    receipts_dir=base / "receipts")
            routing = assembly.routing
            assert routing is not None
            try:
                routing.store.reserve(
                    "run-f", "1", "squatter", "org/model-c", "gamma",
                    "opencode/model-c-free")
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found")
                assert nxt is not None
                self.assertEqual(nxt.family_id, "delta")
                audit = routing.store.audit_since("run-f", 0.0)
                self.assertTrue(
                    any(event == "review_fallback_conflict"
                        for _, event, _ in audit))
                self.assertTrue(
                    any(event == "review_fallback"
                        for _, event, _ in audit))
            finally:
                routing.close()


class Round2Tests(unittest.TestCase):
    def _setup(self, tmp):
        base = Path(tmp)
        store = AvailabilityStore(base / "avail.sqlite")
        from model_routing.models import ProfileRanking
        ranking = ProfileRanking.model_validate(
            _rankings_doc(_order_abc())["profiles"]["free-review"])
        return base, store, wiring.review_candidates(ranking)

    def _receipt(self, slot, canonical, family, deployment, rank):
        return wiring.ReviewReceipt(
            run_id="run-1", round="1", slot=slot,
            requested_model=deployment, effective_model=deployment,
            family_id=family, canonical_model_id=canonical,
            provider="opencode", deployment_id=deployment,
            rank=rank, quality=0.8, rankings_snapshot="sha256:x",
            catalog_snapshot="catalog-test", reviewed_commit="abc",
            report_path="r.md", sha256="0" * 64, result="validated",
            fallback_count=0, started_at="", finished_at="")

    def test_unknown_policy_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                with self.assertRaises(wiring.ReviewRoutingError):
                    wiring.select_round2_pair(
                        policy="nope", r1=wiring.R1Result(),
                        candidates=candidates, store=store)
            finally:
                store.close()

    def test_best_plus_novel(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-1", "org/model-a",
                                      "alpha", "opencode/model-a-free", 1),
                        self._receipt("free-review-2", "org/model-b",
                                      "beta", "opencode/model-b-free", 2),
                    ),
                    round_families=frozenset({"alpha", "beta", "deepseek"}),
                    tried_families=frozenset({"alpha", "beta"}))
                first, second = wiring.select_round2_pair(
                    policy="reuse_best_plus_novel", r1=r1,
                    candidates=candidates, store=store,
                    exclude_families=frozenset({"muse"}))
                self.assertEqual(first.deployment_id, "opencode/model-a-free")
                assert second is not None
                self.assertEqual(second.family_id, "gamma")
            finally:
                store.close()

    def test_novel_missing_reuses_runner_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-1", "org/model-a",
                                      "alpha", "opencode/model-a-free", 1),
                        self._receipt("free-review-2", "org/model-b",
                                      "beta", "opencode/model-b-free", 2),
                    ),
                    round_families=frozenset(
                        {"alpha", "beta", "gamma", "deepseek"}),
                    tried_families=frozenset({"alpha", "beta"}))
                first, second = wiring.select_round2_pair(
                    policy="reuse_best_plus_novel", r1=r1,
                    candidates=candidates, store=store,
                    exclude_families=frozenset({"muse"}))
                self.assertEqual(first.deployment_id, "opencode/model-a-free")
                assert second is not None
                self.assertEqual(
                    second.deployment_id, "opencode/model-b-free")
            finally:
                store.close()

    def test_demoted_best_falls_to_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-1", "org/gone",
                                      "gone", "opencode/gone-free", 1),
                        self._receipt("free-review-2", "org/model-b",
                                      "beta", "opencode/model-b-free", 2),
                    ),
                    round_families=frozenset({"gone", "beta"}),
                    tried_families=frozenset({"gone", "beta"}))
                first, _ = wiring.select_round2_pair(
                    policy="reuse_best_plus_novel", r1=r1,
                    candidates=candidates, store=store,
                    exclude_families=frozenset({"muse"}))
                self.assertEqual(
                    first.deployment_id, "opencode/model-b-free")
            finally:
                store.close()

    def test_four_distinct_plans_two_novels(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = AvailabilityStore(base / "avail.sqlite")
            from model_routing.models import ProfileRanking
            order = _order_abc() + [
                _entry(4, "org/model-d", "delta", "opencode/model-d-free",
                       quality=0.78)]
            ranking = ProfileRanking.model_validate(
                _rankings_doc(order)["profiles"]["free-review"])
            candidates = wiring.review_candidates(ranking)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-1", "org/model-a",
                                      "alpha", "opencode/model-a-free", 1),
                    ),
                    round_families=frozenset({"alpha", "deepseek"}),
                    tried_families=frozenset({"alpha"}))
                first, second = wiring.select_round2_pair(
                    policy="four_distinct", r1=r1,
                    candidates=candidates, store=store,
                    exclude_families=frozenset({"muse"}))
                self.assertEqual(first.family_id, "beta")
                assert second is not None
                self.assertEqual(second.family_id, "gamma")
            finally:
                store.close()

    def test_four_distinct_blocks_below_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-1", "org/model-a",
                                      "alpha", "opencode/model-a-free", 1),
                    ),
                    round_families=frozenset({"alpha"}),
                    tried_families=frozenset({"alpha", "gamma"}))
                with self.assertRaises(wiring.RoundBlocked):
                    wiring.select_round2_pair(
                        policy="four_distinct", r1=r1,
                        candidates=candidates, store=store,
                        exclude_families=frozenset({"muse"}))
            finally:
                store.close()

    def test_reuse_top_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, store, candidates = self._setup(tmp)
            try:
                r1 = wiring.R1Result(
                    receipts=(
                        self._receipt("free-review-2", "org/model-b",
                                      "beta", "opencode/model-b-free", 2),
                        self._receipt("free-review-1", "org/model-a",
                                      "alpha", "opencode/model-a-free", 1),
                    ),
                    round_families=frozenset({"alpha", "beta", "deepseek"}),
                    tried_families=frozenset({"alpha", "beta"}))
                first, second = wiring.select_round2_pair(
                    policy="reuse_top_two", r1=r1,
                    candidates=candidates, store=store,
                    exclude_families=frozenset({"muse"}))
                self.assertEqual(first.deployment_id, "opencode/model-a-free")
                assert second is not None
                self.assertEqual(
                    second.deployment_id, "opencode/model-b-free")
            finally:
                store.close()


BWRAP_AVAILABLE = shutil.which("bwrap") is not None

needs_bwrap = pytest.mark.skipif(
    not BWRAP_AVAILABLE, reason="bwrap required (host capability)")


def _fake_opencode(bindir: Path) -> Path:
    fake = bindir / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "echo \"ARGV:$@\"\n"
        "if [ -z \"$OPENCODE_CONFIG_CONTENT\" ]; then echo CONTENT-ABSENT; "
        "else echo CONTENT-PRESENT; fi\n"
        "if grep -q '\"review\"' \"$HOME/.config/opencode/opencode.json\" "
        "2>/dev/null; then echo HAS-REVIEW-AGENT; else echo NO-REVIEW-AGENT; fi\n",
        encoding="utf-8")
    fake.chmod(0o755)
    return fake


class HardenedExecutorTests(unittest.TestCase):
    def _spec(self, fake, **overrides):
        spec = {
            "label": "free-review-1", "provider": "opencode",
            "model": "opencode/model-a-free", "family": "alpha",
            "command": [str(fake)], "review_hardened": True,
            "extra_read_roots": [str(fake.parent)],
        }
        spec.update(overrides)
        return spec

    @needs_bwrap
    def test_marked_slot_runs_fenced_with_review_agent(self):
        import asyncio

        from orchestration.live import ProcessExecutor
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            fake = _fake_opencode(bindir)
            workdir = base / "ws"
            workdir.mkdir()
            executor = ProcessExecutor(pool=[self._spec(fake)])
            result = asyncio.run(executor.run(
                "review:free-review-1", "hello", workdir, 60))
            self.assertEqual(result.returncode, 0, result.output_tail)
            self.assertIn("--agent review", result.output_tail)
            self.assertIn("--standalone", result.output_tail)
            self.assertIn("CONTENT-PRESENT", result.output_tail)
            self.assertIn("HAS-REVIEW-AGENT", result.output_tail)

    @needs_bwrap
    def test_unmarked_slot_keeps_plain_fence(self):
        import asyncio

        from orchestration.live import ProcessExecutor
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            fake = _fake_opencode(bindir)
            workdir = base / "ws"
            workdir.mkdir()
            spec = self._spec(fake)
            del spec["review_hardened"]
            executor = ProcessExecutor(pool=[spec])
            result = asyncio.run(executor.run(
                "review:free-review-1", "hello", workdir, 60))
            self.assertEqual(result.returncode, 0, result.output_tail)
            self.assertNotIn("--agent", result.output_tail)
            self.assertIn("CONTENT-ABSENT", result.output_tail)

    @needs_bwrap
    def test_marked_slot_refuses_router_keys(self):
        import asyncio

        from orchestration.live import ProcessExecutor
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            fake = _fake_opencode(bindir)
            workdir = base / "ws"
            workdir.mkdir()
            spec = self._spec(fake, env={"FREE_ROUTER_PROBE": "x"})
            executor = ProcessExecutor(pool=[spec])
            result = asyncio.run(executor.run(
                "review:free-review-1", "hello", workdir, 60))
            self.assertEqual(result.returncode, 1)
            self.assertIn("sandbox refused", result.output_tail)

    def test_register_pool_entries_upserts(self):
        from orchestration.live import ProcessExecutor
        executor = ProcessExecutor(pool=[{"label": "a", "model": "old"}])
        executor.register_pool_entries(
            [{"label": "a", "model": "new"}, {"label": "b"}])
        self.assertEqual(executor._pool["a"]["model"], "new")
        self.assertIn("b", executor._pool)


CLEAN_SIMPLE = "No findings.\nREVIEW_DONE_simple_1\n"


class _SequencedReviewers:
    """Fake reviewer executor: per-label response queues."""

    def __init__(self, script=None, default=(0, CLEAN_SIMPLE, {})):
        self._script = {label: list(items)
                        for label, items in (script or {}).items()}
        self._default = default
        self.calls = []
        self.registered = []

    def register_pool_entries(self, entries):
        self.registered.append([dict(entry) for entry in entries])

    async def run(self, role, prompt, workdir, timeout_s):
        self.calls.append(role)
        label = role.split(":", 1)[1]
        queue = self._script.get(label) or []
        if queue:
            rc, text, usage = queue.pop(0)
        else:
            rc, text, usage = self._default
        return SimpleNamespace(returncode=rc, elapsed_s=0.1,
                               output_tail=text, usage=usage)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True).stdout.strip()


def _repo() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="review-wiring-")
    repo = Path(tmp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "tracked.txt").write_text("one\nbefore\nthree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return tmp


class SimpleRoundTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, pool, reviewers, task_extra=None,
                   review_cfg=None):
        from agents.dev.session import DevExecutionOutput, DevSessionManager
        from state import CodingTool, TaskStatus

        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = DevSessionManager(MagicMock(), MagicMock())
        task = {"task_id": "task-1", "title": "t", "description": "d"}
        task.update(task_extra or {})
        config = {
            "project": {"repos": [{"local_path": str(repo),
                                   "branch_base": "main"}]},
            "dev": {"gates": [{"name": "t", "cmd": "true"}]},
            "review": {"reviewers": pool, "review": review_cfg or {}},
        }
        tool = CodingTool.TIER_C
        branch = manager._open_simple_branch(task, config, tool, repo)
        assert branch is not None
        (repo / "tracked.txt").write_text("one\nafter\nthree\n")
        (repo / ".openceo").mkdir(parents=True, exist_ok=True)
        (repo / ".openceo" / "HANDOFF.md").write_text("did the thing")
        output = DevExecutionOutput(status=TaskStatus.IN_PROGRESS)
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            result = await manager._finish_simple_tool(
                task, config, tool, output, branch)
        return result, reviewers, repo

    async def test_dynamic_pair_feeds_free_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                reviewers = _SequencedReviewers()
                output, _, repo = await self._run(_static_pool(), reviewers)
                from state import TaskStatus
                self.assertEqual(output.status, TaskStatus.COMPLETED)
                self.assertEqual(
                    set(reviewers.calls),
                    {"review:free-review-1", "review:free-review-2",
                     "review:gemini"})
                checkpoint = None
                params = output.params
                if isinstance(params, dict):
                    from agents.dev.session import REVIEW_ROUND_PARAMS_KEY
                    checkpoint = params.get(REVIEW_ROUND_PARAMS_KEY)
                assert isinstance(checkpoint, dict)
                reports = {item["slot"]: item
                           for item in checkpoint["reports"]}
                self.assertEqual(
                    {item["family"] for item in reports.values()
                     if item["slot"] != "gemini"},
                    {"alpha", "beta"})
                first = reports["free-review-1"]
                self.assertTrue(first["valid"])
                self.assertIn("receipt", first)
                receipt = first["receipt"]
                self.assertEqual(receipt["requested_model"],
                                 "opencode/model-a-free")
                self.assertEqual(receipt["effective_model"],
                                 "opencode/model-a-free")
                self.assertEqual(receipt["family_id"], "alpha")
                self.assertEqual(receipt["fallback_count"], 0)
                self.assertTrue(
                    receipt["rankings_snapshot"].startswith("sha256:"))
                self.assertNotIn("receipt", reports["gemini"])
                receipts_dir = repo / ".openceo" / "review_receipts"
                self.assertTrue(any(receipts_dir.glob("*.json")))

    async def test_builder_and_dsh_families_excluded(self):
        order = [
            _entry(1, "org/deep-x", "deepseek", "opencode/deep-x-free",
                   quality=0.95),
            _entry(2, "org/muse-x", "muse", "opencode/muse-x-free",
                   quality=0.94),
            _entry(3, "org/model-a", "alpha", "opencode/model-a-free",
                   quality=0.90),
            _entry(4, "org/model-b", "beta", "opencode/model-b-free",
                   quality=0.85),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, order)
                reviewers = _SequencedReviewers()
                output, _, _ = await self._run(_static_pool(), reviewers)
                from agents.dev.session import REVIEW_ROUND_PARAMS_KEY
                params = output.params
                assert isinstance(params, dict)
                checkpoint = params.get(REVIEW_ROUND_PARAMS_KEY)
                families = {item["family"]
                            for item in checkpoint["reports"]
                            if item["slot"] != "gemini"}
                self.assertEqual(families, {"alpha", "beta"})

    async def test_no_snapshot_uses_manual_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                reviewers = _SequencedReviewers()
                await self._run(_static_pool(), reviewers)
                self.assertEqual(
                    set(reviewers.calls),
                    {"review:nemotron", "review:mimo", "review:gemini"})

    async def test_switch_off_uses_manual_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                reviewers = _SequencedReviewers()
                await self._run(
                    _static_pool(), reviewers,
                    review_cfg={"free_router": {"enabled": False}})
                self.assertEqual(
                    set(reviewers.calls),
                    {"review:nemotron", "review:mimo", "review:gemini"})

    async def test_fallback_restarts_only_failed_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                reviewers = _SequencedReviewers({
                    "free-review-1": [
                        (1, "model not found: opencode/model-a-free", {}),
                        (0, CLEAN_SIMPLE, {}),
                    ]})
                output, _, _ = await self._run(_static_pool(), reviewers)
                from state import TaskStatus
                self.assertEqual(output.status, TaskStatus.COMPLETED)
                self.assertEqual(
                    reviewers.calls.count("review:free-review-1"), 2)
                self.assertEqual(
                    reviewers.calls.count("review:free-review-2"), 1)
                self.assertEqual(reviewers.calls.count("review:gemini"), 1)
                from agents.dev.session import REVIEW_ROUND_PARAMS_KEY
                params = output.params
                assert isinstance(params, dict)
                reports = {item["slot"]: item
                           for item in
                           params[REVIEW_ROUND_PARAMS_KEY]["reports"]}
                receipt = reports["free-review-1"]["receipt"]
                self.assertEqual(receipt["requested_model"],
                                 "opencode/model-a-free")
                self.assertEqual(receipt["effective_model"],
                                 "opencode/model-c-free")
                self.assertEqual(receipt["fallback_count"], 1)
                self.assertEqual(reports["free-review-1"]["model"],
                                 "opencode/model-c-free")
                self.assertEqual(reports["free-review-1"]["family"], "gamma")
                synced = [batch for batch in reviewers.registered
                          if len(batch) == 1
                          and batch[0].get("label") == "free-review-1"
                          and batch[0].get("model") == "opencode/model-c-free"]
                self.assertTrue(synced)
                self.assertTrue(synced[0][0]["review_hardened"])
                probe = AvailabilityStore(
                    base / "st" / "free-router" / "availability.sqlite")
                try:
                    rows = probe.errors_since(0.0)
                finally:
                    probe.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].error_class, "not_found")

    async def test_t17_agy2_failure_needs_two_distinct(self):
        pool = _static_pool()
        for entry in pool:
            if entry["label"] == "gemini":
                entry["account"] = "agy2"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                reviewers = _SequencedReviewers({
                    "gemini": [(1, "boom", {})]})
                output, _, _ = await self._run(pool, reviewers)
                from state import TaskStatus
                self.assertEqual(output.status, TaskStatus.COMPLETED)
                from agents.dev.session import REVIEW_ROUND_PARAMS_KEY
                params = output.params
                assert isinstance(params, dict)
                reports = {item["slot"]: item
                           for item in
                           params[REVIEW_ROUND_PARAMS_KEY]["reports"]}
                self.assertEqual(reports["gemini"]["kind"], "slot_error")
                valid = [item for item in reports.values()
                         if item["valid"]]
                self.assertEqual(len(valid), 2)
                self.assertEqual(
                    {item["receipt"]["family_id"] for item in valid},
                    {"alpha", "beta"})
                probe = AvailabilityStore(
                    base / "st" / "free-router" / "availability.sqlite")
                try:
                    self.assertEqual(probe.errors_since(0.0), [])
                finally:
                    probe.close()

    async def test_off_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                reviewers = _SequencedReviewers()
                await self._run(_static_pool(), reviewers,
                                task_extra={"review": "off"})
                self.assertEqual(reviewers.calls, [])
                self.assertFalse((base / "st" / "free-router").exists())


CLEAN_TICKET = "No findings.\nREVIEW_DONE_ticket_1\n"
CLEAN_TICKET_2 = "No findings.\nREVIEW_DONE_ticket_2\n"
CRITICAL_TICKET = ("[CRITICAL] [bug] tracked.txt:2 — bad bound → check it\n"
                   "> after\n"
                   "REVIEW_DONE_ticket_1\n")


class _SScripted:
    """Scripted Tier S executor with per-role queues and pool sync record."""

    def __init__(self, script=None):
        self._script = {role: list(items)
                        for role, items in (script or {}).items()}
        self.calls = []
        self.prompts = []
        self.registered = []

    async def run(self, role, prompt, workdir, timeout_s):
        from orchestration.live import ExecResult
        self.calls.append(role)
        self.prompts.append(prompt)
        queue = self._script.get(role) or []
        if queue:
            item = queue.pop(0)
            if callable(item):
                return item(prompt, workdir)
            rc, text, usage = item
        else:
            rc, text, usage = (0, CLEAN_TICKET, {})
        return ExecResult(rc, 0.1, output_tail=text, usage=dict(usage),
                          model="", slot=role)

    async def cleanup(self, role):
        pass

    def register_pool_entries(self, entries):
        self.registered.append([dict(entry) for entry in entries])


class TierSRoundTests(unittest.IsolatedAsyncioTestCase):
    async def _rounds(self, script=None, review_extra=None, repo=None):
        from agents.dev.tier_s.cycle import TierSCycle, _Ctx
        from agents.dev.tier_s.gates import GateSpec
        from agents.dev.tier_s.models import (
            BUDGETS_DEFAULTS,
            TIMEOUTS_DEFAULTS,
            WAITING_DEFAULTS,
        )
        from agents.dev.tier_s.stores import (
            InMemoryLockStore,
            InMemoryRunStore,
            InMemoryTaskStore,
        )

        if repo is None:
            tmp = _repo()
            self.addCleanup(tmp.cleanup)
            repo = Path(tmp.name)
            (repo / "tracked.txt").write_text("one\nafter\nthree\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "build")
            (repo / ".openceo").mkdir(exist_ok=True)
        base_sha = _git(repo, "rev-parse", "HEAD~1")
        head_sha = _git(repo, "rev-parse", "HEAD")
        runs = InMemoryRunStore()
        await runs.create_run({"run_id": "run-s1", "status": "running"})
        fake = _SScripted(script)
        cycle = TierSCycle(runs, InMemoryLockStore(), InMemoryTaskStore(),
                           fake)
        review_cfg = {"round1_parallel": 3, "round2_parallel": 2,
                      "quorum": 2, "reviewer_timeout_minutes": 15}
        review_cfg.update(review_extra or {})
        ctx = _Ctx(
            ticket={"task_id": "task-s", "ref": "TASK-2026-x",
                    "title": "t", "acceptance": ["tracked.txt reviewed"]},
            repo=repo, ref="TASK-2026-x", run_id="run-s1",
            branch="tier-s/x", base_branch="main",
            budgets=dict(BUDGETS_DEFAULTS),
            timeouts=dict(TIMEOUTS_DEFAULTS),
            waiting_cfg=dict(WAITING_DEFAULTS), auto_merge=False,
            budget_exclude=[],
            gate_specs=[GateSpec(name="t", cmd="test -f tracked.txt",
                                 timeout_minutes=1, required=True)],
            roles={"builder": {"model": "muse-spark-1.3-contributor"}},
            pool=review_mod.parse_pool(_static_pool()),
            reviewers_raw=_static_pool(), review_cfg=review_cfg,
            base_sha=base_sha, build_sha=head_sha, head_sha=head_sha)
        outcome = await cycle._review_rounds(ctx)
        return outcome, ctx, fake, repo

    async def test_r1_priority_with_dynamic_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, fake, _ = await self._rounds()
                self.assertIsNone(outcome)
                review_calls = [call for call in fake.calls
                                if call.startswith("review:")]
                self.assertEqual(review_calls.count("review:dsh"), 1)
                self.assertEqual(
                    review_calls.count("review:free-review-1"), 2)
                self.assertEqual(
                    review_calls.count("review:free-review-2"), 2)
                r1 = ctx.rounds[0]
                self.assertEqual(
                    {item["slot"] for item in r1["attempts"]},
                    {"dsh", "free-review-1", "free-review-2",
                     "muse-review"})
                receipts = [item["receipt"] for item in r1["attempts"]
                            if "receipt" in item]
                self.assertEqual(len(receipts), 2)
                self.assertEqual(
                    {item["family_id"] for item in receipts},
                    {"alpha", "beta"})
                dyn = [entry for batch in fake.registered
                       for entry in batch
                       if entry["label"] == "free-review-1"]
                self.assertTrue(dyn)
                self.assertEqual(
                    dyn[0]["model"], "opencode/model-a-free")

    async def test_r2_repeats_best_and_novel(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, _, _ = await self._rounds()
                self.assertIsNone(outcome)
                r2 = ctx.rounds[1]
                receipts = [item["receipt"] for item in r2["attempts"]
                            if "receipt" in item]
                self.assertEqual(len(receipts), 2)
                self.assertEqual(
                    {item["effective_model"] for item in receipts},
                    {"opencode/model-a-free", "opencode/model-c-free"})

    async def test_r2_reuse_top_two_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, _, _ = await self._rounds(review_extra={
                    "free_router": {"round2_policy": "reuse_top_two"}})
                self.assertIsNone(outcome)
                r2 = ctx.rounds[1]
                receipts = [item["receipt"] for item in r2["attempts"]
                            if "receipt" in item]
                self.assertEqual(
                    {item["effective_model"] for item in receipts},
                    {"opencode/model-a-free", "opencode/model-b-free"})

    async def test_r2_four_distinct_blocks(self):
        script = {"review:dsh": [(1, "boom", {})]}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, _, _ = await self._rounds(
                    script=script, review_extra={
                        "free_router": {"round2_policy": "four_distinct"}})
                assert outcome is not None
                self.assertEqual(outcome.status, "waiting")
                self.assertIn("four_distinct", ctx.quorum_detail)

    async def test_t06_failed_reviewer_restarts_alone(self):
        import hashlib
        script = {"review:free-review-1": [
            (1, "model not found: opencode/model-a-free", {}),
            (0, CLEAN_TICKET, {}),
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, fake, _ = await self._rounds(script=script)
                self.assertIsNone(outcome)
                self.assertEqual(
                    fake.calls.count("review:free-review-1"), 3)
                self.assertEqual(
                    fake.calls.count("review:free-review-2"), 2)
                self.assertEqual(fake.calls.count("review:dsh"), 1)
                r1 = ctx.rounds[0]
                receipts = {item["slot"]: item["receipt"]
                            for item in r1["attempts"]
                            if "receipt" in item}
                kept = receipts["free-review-2"]
                self.assertEqual(
                    kept["sha256"],
                    hashlib.sha256(
                        CLEAN_TICKET.strip().encode()).hexdigest())
                self.assertEqual(kept["fallback_count"], 0)
                moved = receipts["free-review-1"]
                self.assertEqual(moved["requested_model"],
                                 "opencode/model-a-free")
                self.assertEqual(moved["effective_model"],
                                 "opencode/model-c-free")
                self.assertEqual(moved["fallback_count"], 1)
                synced = [batch for batch in fake.registered
                          if len(batch) == 1
                          and batch[0].get("label") == "free-review-1"
                          and batch[0].get("model") == "opencode/model-c-free"]
                self.assertTrue(synced)

    async def test_t04_racers_end_distinct(self):
        order = _order_abc() + [
            _entry(4, "org/model-d", "delta", "opencode/model-d-free",
                   quality=0.78)]
        script = {
            "review:free-review-1": [
                (1, "model not found: opencode/model-a-free", {}),
                (0, CLEAN_TICKET, {}),
            ],
            "review:free-review-2": [
                (1, "model not found: opencode/model-b-free", {}),
                (0, CLEAN_TICKET, {}),
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, order)
                outcome, ctx, _, _ = await self._rounds(script=script)
                self.assertIsNone(outcome)
                r1 = ctx.rounds[0]
                receipts = [item["receipt"] for item in r1["attempts"]
                            if "receipt" in item]
                self.assertEqual(len(receipts), 2)
                self.assertEqual(
                    len({item["family_id"] for item in receipts}), 2)
                self.assertTrue(
                    {item["family_id"] for item in receipts}
                    <= {"gamma", "delta"})

    async def test_t05_same_canonical_never_twice(self):
        order = [
            _entry(1, "org/model-a", "alpha", "opencode/model-a-free",
                   quality=0.90),
            _entry(2, "org/model-a", "alpha", "opencode/model-a-alt",
                   quality=0.85),
            _entry(3, "org/model-b", "beta", "opencode/model-b-free",
                   quality=0.80),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, order)
                outcome, ctx, _, _ = await self._rounds()
                self.assertIsNone(outcome)
                r1 = ctx.rounds[0]
                receipts = [item["receipt"] for item in r1["attempts"]
                            if "receipt" in item]
                self.assertEqual(
                    {item["canonical_model_id"] for item in receipts},
                    {"org/model-a", "org/model-b"})

    async def test_t07_no_pair_runs_manual_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc()[:1])
                outcome, ctx, fake, _ = await self._rounds()
                self.assertIsNone(outcome)
                r1_slots = {item["slot"]
                            for item in ctx.rounds[0]["attempts"]}
                self.assertIn("mimo", r1_slots)
                self.assertNotIn("free-review-1", r1_slots)
                probe = AvailabilityStore(
                    base / "st" / "free-router" / "availability.sqlite")
                try:
                    audit = probe.audit_since("run-s1", 0.0)
                finally:
                    probe.close()
                self.assertTrue(
                    any(event == "review_pair_unavailable"
                        for _, event, _ in audit))

    async def test_t08_r2_reviews_corrected_state(self):
        def _fix(prompt, workdir):
            from orchestration.live import ExecResult
            tier = Path(workdir) / ".openceo"
            tier.mkdir(exist_ok=True)
            if "round 2" in prompt:
                (tier / "FIX_2.md").write_text(
                    "F1: DESCARTADO — fuera de alcance\n",
                    encoding="utf-8")
            else:
                (tier / "FIX_1.md").write_text(
                    "F1: CORREGIDO — bounded the index\n",
                    encoding="utf-8")
                with open(Path(workdir) / "tracked.txt", "a",
                          encoding="utf-8") as handle:
                    handle.write("fixed\n")
            return ExecResult(0, 0.1, model="", slot="builder")

        script = {
            "review:free-review-1": [(0, CRITICAL_TICKET, {})],
            "builder": [_fix, _fix],
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, fake, repo = await self._rounds(script=script)
                self.assertIsNone(outcome)
                self.assertEqual(len(ctx.rounds), 2)
                fixed_head = _git(repo, "rev-parse", "HEAD")
                self.assertNotEqual(ctx.rounds[0]["slots"], [])
                r2 = ctx.rounds[1]
                receipts = [item["receipt"] for item in r2["attempts"]
                            if "receipt" in item]
                self.assertEqual(len(receipts), 2)
                self.assertTrue(
                    all(item["reviewed_commit"] == fixed_head
                        for item in receipts))

    async def test_t18_restart_reuses_verified_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome, ctx, fake, repo = await self._rounds()
                self.assertIsNone(outcome)
                first_calls = list(fake.calls)
                self.assertIn("review:free-review-1", first_calls)
                receipts_dir = repo / ".openceo" / "review_receipts"
                before = {path.name: path.read_bytes()
                          for path in receipts_dir.iterdir()}
                self.assertTrue(before)
                for path in receipts_dir.iterdir():
                    path.chmod(0o444)
                outcome2, _, fake2, _ = await self._rounds(repo=repo)
                self.assertIsNone(outcome2)
                dynamic = [call for call in fake2.calls
                           if call in ("review:free-review-1",
                                       "review:free-review-2")]
                self.assertEqual(dynamic, [])
                after = {path.name: path.read_bytes()
                         for path in receipts_dir.iterdir()}
                self.assertEqual(after, before)


PLAN_MD = """TICKET: TASK-2026-99999
OBJECTIVE: none
GOAL: Append one audited line to tracked.txt
ACCEPTANCE:
- tracked.txt gains exactly one line
- compile gate passes
FILES:
- tracked.txt (modify) — the line goes here
CONTRACTS:
- produces none
DECISIONS:
- D1 [durable: no] one line only — acceptance says so
STEPS:
1. Append the line
2. Write HANDOFF.md
ESTIMATE: files=1 lines=1
OUT_OF_SCOPE:
- anything else
"""

PLAN_REVIEW_MD = """O1 [WARN] PLAN.md:7 — acceptance counts lines strictly → keep the diff to one line
> ACCEPTANCE:
REVIEW_DONE_plan_0
"""


class TierSFullRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_t09_fix2_change_gets_final_verification(self):
        from agents.dev.tier_s.cycle import TierSCycle
        from agents.dev.tier_s.gates import GateSpec
        from agents.dev.tier_s.models import (
            BUDGETS_DEFAULTS,
            TIMEOUTS_DEFAULTS,
            WAITING_DEFAULTS,
        )
        from agents.dev.tier_s.stores import (
            InMemoryLockStore,
            InMemoryRunStore,
            InMemoryTaskStore,
        )
        from orchestration.live import ExecResult

        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        runs = InMemoryRunStore()
        locks = InMemoryLockStore()
        tasks = InMemoryTaskStore()

        def _builder(prompt, workdir):
            tier = Path(workdir) / ".openceo"
            tier.mkdir(exist_ok=True)
            if "round 2" in prompt:
                (tier / "FIX_2.md").write_text(
                    "F1: CORREGIDO — guarded the tail\n",
                    encoding="utf-8")
                with open(Path(workdir) / "tracked.txt", "a",
                          encoding="utf-8") as handle:
                    handle.write("fixed2\n")
            elif "round 1" in prompt:
                raise AssertionError("fix1 must not run without findings")
            else:
                with open(Path(workdir) / "tracked.txt", "w",
                          encoding="utf-8") as handle:
                    handle.write("one\nafter\nthree\nbuilt\n")
                (tier / "HANDOFF.md").write_text("did the thing")
            return ExecResult(0, 0.5, model="m", slot="builder")

        def _planner(prompt, workdir):
            tier = Path(workdir) / ".openceo"
            tier.mkdir(exist_ok=True)
            (tier / "PLAN.md").write_text(PLAN_MD, encoding="utf-8")
            return ExecResult(0, 0.5, model="m", slot="planner")

        def _plan_reviewer(prompt, workdir):
            tier = Path(workdir) / ".openceo"
            tier.mkdir(exist_ok=True)
            (tier / "PLAN_REVIEW.md").write_text(
                PLAN_REVIEW_MD, encoding="utf-8")
            return ExecResult(0, 0.5, model="m", slot="plan_reviewer")

        r2_finding = ("[CRITICAL] [bug] tracked.txt:4 — tail off → guard\n"
                      "> built\n"
                      "REVIEW_DONE_ticket_2\n")
        fake = _SScripted({
            "planner": [_planner],
            "plan_reviewer": [_plan_reviewer],
            "builder": [_builder, _builder, _builder],
            "review:free-review-1": [(0, CLEAN_TICKET, {}),
                                     (0, r2_finding, {})],
        })
        cycle = TierSCycle(runs, locks, tasks, fake)
        ticket = {"task_id": "task-t09", "ref": "TASK-2026-99999",
                  "title": "t", "description": "append lines",
                  "acceptance": ["tracked.txt reviewed"],
                  "coding_tool": "tier_s", "blocked_by": [],
                  "origin": "owner"}
        roles = {
            "planner": {"account": "claudej", "model": "opus",
                        "command": ["true"]},
            "plan_reviewer": {"account": "codexj", "model": "gpt-6.1-sol",
                              "command": ["true"]},
            "builder": {"account": "muse",
                        "model": "muse-spark-1.3-contributor",
                        "command": ["true"]},
        }
        cfg = {
            "roles": roles,
            "reviewers": _static_pool(),
            "review": {"round1_parallel": 3, "round2_parallel": 2,
                       "quorum": 2, "reviewer_timeout_minutes": 15},
            "budgets": dict(BUDGETS_DEFAULTS),
            "timeouts": dict(TIMEOUTS_DEFAULTS),
            "waiting": dict(WAITING_DEFAULTS),
            "auto_merge": True, "base_branch": "main",
            "budget_exclude": [],
        }
        specs = [GateSpec(name="t", cmd="test -f tracked.txt",
                        timeout_minutes=1, required=True)]
        with tempfile.TemporaryDirectory() as xtmp:
            base = Path(xtmp)
            with patch.dict(os.environ,
                            {"XDG_CONFIG_HOME": str(base / "cfg"),
                             "XDG_STATE_HOME": str(base / "st")}):
                (base / "cfg").mkdir()
                (base / "st").mkdir()
                _write_rankings(base, _order_abc())
                outcome = await cycle.run(
                    ticket, repo, cfg, gate_specs=specs)
        self.assertEqual(outcome.status, "completed")
        run = runs.runs[outcome.run_id]
        phases = [entry["phase"] for entry in run.get("gates", [])]
        self.assertIn("fix2", phases)
        self.assertIn("final", phases)
        self.assertLess(phases.index("fix2"), phases.index("final"))
        self.assertEqual(
            (repo / "tracked.txt").read_text(encoding="utf-8"),
            "one\nafter\nthree\nbuilt\nfixed2\n")
        rounds = run.get("rounds", [])
        self.assertEqual(len(rounds), 2)
        r2_receipts = [item["receipt"] for item in rounds[1]["attempts"]
                       if "receipt" in item]
        self.assertEqual(len(r2_receipts), 2)
