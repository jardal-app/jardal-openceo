"""One real fed reviewer under the hardened fence (TASK-2026-00293).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. A single ``opencode run`` review of a tiny synthetic diff goes
through the production path (frozen ranking fixture, assembly,
hardened executor, parse, verify, §11.7 receipt). No credential is
configured, so a paid route would fail instead of billing; the
fixture input is synthetic but the reviewer call, the fence and the
receipt are real. Never prints secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import review_wiring as wiring  # noqa: E402
from model_routing.availability import AvailabilityStore  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402

pytestmark = pytest.mark.live


TINY_MODULE = '''\
"""Tiny synthetic addition under review."""


def add(first, second):
    """Return the sum of two numbers."""
    return first + second
'''

# Serving evidence for the called model: the loop coordinator probed
# opencode/nemotron-3-ultra-free healthy on 2026-10-23 ~12:11 UTC and
# used it for two real §16.4 reviews minutes later. The second pair
# member is picked but never called in this test.
LIVE_ORDER = [
    {
        "rank": 1, "canonical_model_id": "nvidia/nemotron-3-ultra",
        "family_id": "nemotron",
        "primary": {"deployment_id": "opencode/nemotron-3-ultra-free",
                    "provider": "opencode"},
        "backups": [], "quality": 0.90, "coverage": 1.0,
        "dimensions": {"coding": 0.90},
        "quality_sources": ["synthetic-fixture"],
    },
    {
        "rank": 2, "canonical_model_id": "xiaomi/mimo-v2-flash",
        "family_id": "mimo",
        "primary": {"deployment_id": "opencode/mimo-v2.6-flash-free",
                    "provider": "opencode"},
        "backups": [], "quality": 0.85, "coverage": 1.0,
        "dimensions": {"coding": 0.85},
        "quality_sources": ["synthetic-fixture"],
    },
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True).stdout.strip()


def test_one_real_fed_reviewer_with_receipt(tmp_path):
    from datetime import datetime, timezone

    from agents.dev.session import DevSessionManager
    from test_model_routing_review_wiring import _static_pool

    base = tmp_path / "xdg"
    (base / "cfg" / "free-router").mkdir(parents=True)
    (base / "st").mkdir(parents=True)
    doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "review-live-test",
        "catalog_snapshot": "catalog-test",
        "catalog_sha256": "0" * 64,
        "profiles": {
            "free-review": {
                "profile": "free-review",
                "order": LIVE_ORDER,
                "experimental": [],
                "pending_top3": None,
                "provenance": {
                    "catalog_snapshot": "catalog-test",
                    "catalog_sha256": "0" * 64,
                },
            }
        },
    }
    rankings_path = base / "cfg" / "free-router" / "rankings.json"
    rankings_path.write_text(json.dumps(doc), encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "tracked.txt").write_text("one\ntwo\nthree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "tiny.py").write_text(TINY_MODULE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tiny synthetic addition")
    review_sha = _git(repo, "rev-parse", "HEAD")

    with patch.dict(os.environ,
                    {"XDG_CONFIG_HOME": str(base / "cfg"),
                     "XDG_STATE_HOME": str(base / "st")}):
        assembly = wiring.assemble_review_pool(
            _static_pool(), review_cfg={}, run_id="live-293",
            round="1", builder_family="muse",
            receipts_dir=repo / ".openceo" / "review_receipts")
        routing = assembly.routing
        assert routing is not None
        assert assembly.prefer_families == ("nemotron", "mimo")
        pool = review_mod.parse_pool(assembly.raw_pool)
        slot = next(item for item in pool
                    if item.label == "free-review-1")
        assert slot.model == "opencode/nemotron-3-ultra-free"
        assert slot.command == ("opencode", "run", "-m",
                                "opencode/nemotron-3-ultra-free")
        try:
            import asyncio

            manager = DevSessionManager(MagicMock(), MagicMock())
            executor = ProcessExecutor(pool=assembly.raw_pool)
            slots = review_mod.InMemorySlotStore()
            reviews = review_mod.InMemoryReviewStore()
            request = review_mod.build_review_request(
                1, "tiny synthetic addition", ["tiny.py reviewed"],
                base_sha, review_sha, " tiny.py | 8 ++++++++",
                ["tiny.py"], [], "", ".openceo/HANDOFF.md")
            outcome = asyncio.run(manager._run_simple_reviewer(
                executor=executor, slots=slots, reviews=reviews,
                repo=repo, review_sha=review_sha, request=request,
                request_name="REVIEW REQUEST (simple, round 1)",
                handoff="HANDOFF.md", base_sha=base_sha,
                acceptance=["tiny.py reviewed"], task_id="live-293",
                project="jardal-openceo", timeout_default=15,
                cost_runs=[], tag_prefix="live-293",
                slot=slot, attempt=1, previous=None, routing=routing))
            if not outcome.get("valid"):
                print(f"live reviewer outcome: {outcome!r}"[:2000])
            assert outcome.get("valid"), outcome.get("reason")
            receipt = outcome["receipt"]
            assert receipt["requested_model"] == (
                "opencode/nemotron-3-ultra-free")
            assert receipt["effective_model"] == (
                "opencode/nemotron-3-ultra-free")
            assert receipt["family_id"] == "nemotron"
            assert receipt["provider"] == "opencode"
            assert receipt["fallback_count"] == 0
            assert receipt["reviewed_commit"] == review_sha
            assert receipt["rankings_snapshot"].startswith("sha256:")
            assert receipt["result"] == "validated"
            report_path = Path(receipt["report_path"])
            assert report_path.is_file()
            text = report_path.read_text(encoding="utf-8")
            assert "tiny.py" in text or "No findings" in text
            probe = AvailabilityStore(
                base / "st" / "free-router" / "availability.sqlite")
            try:
                assert probe.errors_since(0.0) == []
            finally:
                probe.close()
        finally:
            routing.close()
