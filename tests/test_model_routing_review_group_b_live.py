"""One real group B reviewer under the hardened fence (TASK-2026-00306).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. Group A is exhausted first with the recorded FreeUsageLimitError
text, so the fed pair comes from group B; a single ``opencode run``
review of a tiny synthetic diff goes through the production path
(frozen fixtures, assembly, hardened executor with the audited group B
handoff, parse, verify, extended receipt). The fixture input is
synthetic but the reviewer call, the fence, the credential handoff and
the receipt are real, and OpenRouter credit usage is unchanged. Never
prints secrets.

``REVIEW_GROUP_B_PROVIDER`` picks the reviewed provider
(``openrouter`` by default; ``gemini`` or ``groq`` for a run without
OpenRouter generations); ``REVIEW_GROUP_B_MODEL`` pins the reviewed
model id and ``REVIEW_GROUP_B_MODEL2``/``REVIEW_GROUP_B_PROVIDER2``
the uncalled second pair member.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import live as router_live  # noqa: E402
from model_routing import review_wiring as wiring  # noqa: E402
from model_routing import secrets as router_secrets  # noqa: E402
from model_routing import sources as router_sources  # noqa: E402
from model_routing import terms as router_terms  # noqa: E402
from model_routing.availability import AvailabilityStore  # noqa: E402
from model_routing.availability import record_provider_error  # noqa: E402
from model_routing.errors import classify_text  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402
from test_model_routing_review_wiring import _static_pool  # noqa: E402

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

TINY_MODULE = '''\
"""Tiny synthetic addition under review."""


def add(first, second):
    """Return the sum of two numbers."""
    return first + second
'''

_PROVIDER = os.environ.get("REVIEW_GROUP_B_PROVIDER", "openrouter")
_PREFERRED_MODEL = os.environ.get(
    "REVIEW_GROUP_B_MODEL", "recursive/flint-v1.0:free")
_SECOND_MODEL = os.environ.get("REVIEW_GROUP_B_MODEL2", "")
_SECOND_PROVIDER = os.environ.get("REVIEW_GROUP_B_PROVIDER2", "")

_CREDENTIAL_ENV = {
    "openrouter": "FREE_ROUTER_OPENROUTER_API_KEY",
    "gemini": "FREE_ROUTER_GEMINI_API_KEY",
    "groq": "FREE_ROUTER_GROQ_API_KEY",
}

_SECOND_DEFAULT = {
    "openrouter": ("openrouter", ""),
    "gemini": ("openrouter", "recursive/flint-v1.0:free"),
    "groq": ("groq", "qwen/qwen3.6-27b"),
}


def _secret_values() -> list[str]:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError:
        return []
    return [value for value in vars(found).values()
            if isinstance(value, str) and value]


def _record(name: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(
        name, json.dumps(payload, indent=2), FIXTURES, secret_values)


@contextmanager
def _skip_on_429(provider: str):
    try:
        yield
    except router_live.LiveRateLimited:
        pytest.skip(f"real 429 for {provider}; resume tomorrow")
    except router_sources.SourceError as exc:
        if exc.kind == "rate_limited":
            pytest.skip(f"real 429 for {provider}; resume tomorrow")
        raise


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True).stdout.strip()


def _pick_two_families(verified: list[tuple[str, str]]) -> tuple[str, str]:
    first = (_PREFERRED_MODEL if any(model == _PREFERRED_MODEL
                                     for _family, model in verified)
             else verified[0][1])
    first_vendor = first.split("/", 1)[0]
    for _family, model in verified:
        if model != first and model.split("/", 1)[0] != first_vendor:
            return first, model
    for _family, model in verified:
        if model != first:
            return first, model
    raise AssertionError("live catalog holds a single free_verified model")


def test_one_real_group_b_reviewer_with_receipt(tmp_path):
    from agents.dev.session import DevSessionManager

    secret_values = _secret_values()
    try:
        secrets = router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")
    if _PROVIDER not in _CREDENTIAL_ENV:
        pytest.skip(f"unsupported REVIEW_GROUP_B_PROVIDER: {_PROVIDER}")
    if not getattr(secrets, _CREDENTIAL_ENV[_PROVIDER], ""):
        pytest.skip(f"{_CREDENTIAL_ENV[_PROVIDER]} is unset")
    key = secrets.FREE_ROUTER_OPENROUTER_API_KEY
    if not key:
        pytest.skip("FREE_ROUTER_OPENROUTER_API_KEY is unset")
    provider_b1 = _PROVIDER
    if _PROVIDER == "openrouter":
        terms = router_terms.load_terms()
        with _skip_on_429("openrouter"):
            raw = router_sources.fetch_openrouter_models()
            key_payload = router_sources.fetch_openrouter_key(key)
        entries = router_sources.parse_openrouter_models(
            raw, key_payload, terms)
        verified = sorted(
            (entry.family_id, entry.deployments[0].gateway_model_id)
            for entry in entries
            if entry.deployments and entry.deployments[0].free_verified)
        assert verified, "no free_verified :free model in the live catalog"
        model_b1, model_b2 = _pick_two_families(verified)
        provider_b2 = "openrouter"
        usage_before = router_live.parse_openrouter_key_info(
            key_payload).usage
    else:
        model_b1 = _PREFERRED_MODEL
        default_b2_provider, default_b2_model = _SECOND_DEFAULT[_PROVIDER]
        provider_b2 = _SECOND_PROVIDER or default_b2_provider
        model_b2 = _SECOND_MODEL or default_b2_model
        with _skip_on_429("openrouter"):
            key_payload = router_sources.fetch_openrouter_key(key)
        usage_before = router_live.parse_openrouter_key_info(
            key_payload).usage

    base = tmp_path / "xdg"
    (base / "cfg" / "free-router").mkdir(parents=True)
    (base / "st").mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    order = [
        {
            "rank": 1, "canonical_model_id": "org/alpha",
            "family_id": "alpha",
            "primary": {"deployment_id": "opencode/alpha-free",
                        "provider": "opencode"},
            "backups": [], "quality": 0.90, "coverage": 1.0,
            "dimensions": {"coding": 0.90},
            "quality_sources": ["synthetic-fixture"],
        },
        {
            "rank": 2, "canonical_model_id": f"live/{model_b1}",
            "family_id": "live-b1",
            "primary": {"deployment_id": f"{provider_b1}/{model_b1}",
                        "provider": provider_b1},
            "backups": [], "quality": 0.85, "coverage": 1.0,
            "dimensions": {"coding": 0.85},
            "quality_sources": ["synthetic-fixture"],
        },
        {
            "rank": 3, "canonical_model_id": f"live/{model_b2}",
            "family_id": "live-b2",
            "primary": {"deployment_id": f"{provider_b2}/{model_b2}",
                        "provider": provider_b2},
            "backups": [], "quality": 0.83, "coverage": 1.0,
            "dimensions": {"coding": 0.83},
            "quality_sources": ["synthetic-fixture"],
        },
    ]
    doc = {
        "schema_version": 1,
        "generated_at": now,
        "generator": "review-group-b-live-test",
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
    (base / "cfg" / "free-router" / "rankings.json").write_text(
        json.dumps(doc), encoding="utf-8")

    def _deployment(deployment_id, provider, route, provider_id, model_id):
        allowed = ("opencode_only" if route == "opencode_native_free"
                   else "configured_opencode_or_gateway")
        return {
            "deployment_id": deployment_id, "provider": provider,
            "route_type": route, "allowed_client": allowed,
            "provider_id": provider_id, "model_id": model_id,
            "gateway_model_id": model_id,
            "free_offered": True, "free_verified": True, "healthy": True,
            "input_price_usd": 0.0, "output_price_usd": 0.0,
            "approved_confidentiality": ["public"],
            "source": "review-group-b-live-test",
        }

    def _entry(family, canonical, display, deployment):
        return {
            "family_id": family, "canonical_model_id": canonical,
            "display_name": display,
            "capabilities": {"tools": True, "streaming": False,
                             "context_tokens": 131072},
            "deployments": [deployment],
        }

    catalog = {
        "schema_version": 2,
        "generated_at": now,
        "generator": "review-group-b-live-test",
        "entries": [
            _entry("alpha", "org/alpha", "Alpha Free", _deployment(
                "opencode/alpha-free", "opencode",
                "opencode_native_free", "opencode", "alpha-free")),
            _entry("live-b1", f"live/{model_b1}", f"Live {model_b1}",
                   _deployment(f"{provider_b1}/{model_b1}", provider_b1,
                               "external_free", provider_b1, model_b1)),
            _entry("live-b2", f"live/{model_b2}", f"Live {model_b2}",
                   _deployment(f"{provider_b2}/{model_b2}", provider_b2,
                               "external_free", provider_b2, model_b2)),
        ],
    }
    (base / "cfg" / "free-router" / "catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")

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
        quota = json.loads(
            (FIXTURES / "opencode-free-quota-429.json").read_text(
                encoding="utf-8"))
        quota_text = quota["event"]["error"]["response"]["body"]
        assert classify_text(quota_text).error_class == "daily_quota"
        blocker = AvailabilityStore()
        try:
            record_provider_error(
                blocker, provider="opencode",
                deployment_id="opencode/alpha-free",
                model="opencode/alpha-free",
                classified=classify_text(quota_text),
                run_id="live-306", excerpt=quota_text[:200])
        finally:
            blocker.close()
        assembly = wiring.assemble_review_pool(
            _static_pool(), review_cfg={}, run_id="live-306",
            round="1", builder_family="muse",
            receipts_dir=repo / ".openceo" / "review_receipts",
            auto_instance=False)
        routing = assembly.routing
        assert routing is not None
        assert assembly.prefer_families == ("live-b1", "live-b2")
        pool = review_mod.parse_pool(assembly.raw_pool)
        slot = next(item for item in pool
                    if item.label == "free-review-1")
        assert slot.model == f"{provider_b1}/{model_b1}"
        entry = next(item for item in assembly.raw_pool
                     if item.get("label") == "free-review-1")
        assert entry[wiring.REVIEW_TARGET_SPEC]["env_var"] == (
            _CREDENTIAL_ENV[provider_b1])
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
                acceptance=["tiny.py reviewed"], task_id="live-306",
                project="jardal-openceo", timeout_default=15,
                cost_runs=[], tag_prefix="live-306",
                slot=slot, attempt=1, previous=None, routing=routing))
            if not outcome.get("valid"):
                reason = str(outcome.get("reason") or "")
                print(f"live group-B reviewer outcome: {outcome!r}"[:2000])
                if "429" in reason or "quota" in reason.lower():
                    _record(
                        "review-group-b-blocked.json",
                        {
                            "recorded_at": datetime.now(
                                timezone.utc).isoformat(),
                            "model": f"{provider_b1}/{model_b1}",
                            "reason": reason[:500],
                            "note": f"real 429 for {provider_b1}; no retry",
                        },
                        secret_values,
                    )
                    pytest.skip(
                        f"real 429 for {provider_b1}; resume tomorrow")
            assert outcome.get("valid"), outcome.get("reason")
            receipt = outcome["receipt"]
            assert receipt["effective_model"] == f"{provider_b1}/{model_b1}"
            assert receipt["family_id"] == "live-b1"
            assert receipt["provider"] == provider_b1
            assert receipt["route_type"] == "external_free"
            assert receipt["provider_id"] == provider_b1
            assert receipt["model_id"] == model_b1
            assert receipt["fallback_count"] == 0
            assert receipt["reviewed_commit"] == review_sha
            assert receipt["rankings_snapshot"].startswith("sha256:")
            assert receipt["result"] == "validated"
            report_path = Path(receipt["report_path"])
            assert report_path.is_file()
            text = report_path.read_text(encoding="utf-8")
            assert "tiny.py" in text or "No findings" in text
        finally:
            routing.close()
    with _skip_on_429("openrouter"):
        after_payload = router_sources.fetch_openrouter_key(key)
    usage_after = router_live.parse_openrouter_key_info(after_payload).usage
    router_live.assert_usage_unchanged(usage_before, usage_after)
    _record(
        "review-group-b-live.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "model": f"{provider_b1}/{model_b1}",
            "provider": provider_b1,
            "route_type": "external_free",
            "credit_usage_before": usage_before,
            "credit_usage_after": usage_after,
            "receipt": {
                key: receipt[key]
                for key in ("slot", "requested_model",
                            "effective_model", "family_id",
                            "canonical_model_id", "provider",
                            "deployment_id", "route_type",
                            "provider_id", "model_id", "rank",
                            "quality", "rankings_snapshot",
                            "catalog_snapshot", "result",
                            "fallback_count")
            },
        },
        secret_values,
    )
