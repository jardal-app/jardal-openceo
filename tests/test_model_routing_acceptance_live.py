"""Acceptance live check: OpenRouter credit usage is unchanged (TASK-2026-00297).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. The battery itself makes no provider calls, so the only live
traffic of this ticket is the ``GET /api/v1/key`` pair around it:
metadata, no generation. Never prints secrets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import acceptance as router_acceptance  # noqa: E402
from model_routing import live as router_live  # noqa: E402
from model_routing import secrets as router_secrets  # noqa: E402
from model_routing import sources as router_sources  # noqa: E402

pytestmark = pytest.mark.live


def _openrouter_key() -> str:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")
    if not found.FREE_ROUTER_OPENROUTER_API_KEY:
        pytest.skip("FREE_ROUTER_OPENROUTER_API_KEY is unset")
    return found.FREE_ROUTER_OPENROUTER_API_KEY


def _key_usage(key: str) -> float:
    try:
        payload = router_sources.fetch_openrouter_key(key)
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for openrouter; resume tomorrow")
    return router_live.parse_openrouter_key_info(payload).usage


def test_battery_keeps_openrouter_usage_unchanged(tmp_path):
    key = _openrouter_key()
    before = _key_usage(key)
    report = router_acceptance.run_battery(tmp_path / "battery")
    assert report.passed
    after = _key_usage(key)
    router_live.assert_usage_unchanged(before, after)
