"""Live short research session with an injected fault (TASK-2026-00294).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. Five turns run against a real verified OpenRouter ``:free``
model, the first attempt of turn 1 fails with a proxy-injected 429
(spending nothing) and the session rotates to the real route,
keeping affinity for the rest. OpenRouter credit usage must not
move. Never prints secrets: the recorded fixture holds receipts
only, no prompts or answers.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import availability as router_availability  # noqa: E402
from model_routing import fault_proxy as router_proxy  # noqa: E402
from model_routing import live as router_live  # noqa: E402
from model_routing import probes as router_probes  # noqa: E402
from model_routing import research_wiring as wiring  # noqa: E402
from model_routing import secrets as router_secrets  # noqa: E402
from model_routing import sources as router_sources  # noqa: E402
from model_routing.availability import Candidate  # noqa: E402

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"


def _openrouter_candidates() -> list[str]:
    recorded = json.loads(
        (FIXTURES / "sources-openrouter-catalog.json").read_text(encoding="utf-8"))
    ids = [item["id"] for item in recorded.get("verified_free", [])]
    preferred = "recursive/flint-v1.0:free"
    ordered = [preferred] if preferred in ids else []
    ordered += [model for model in ids if model != preferred]
    assert ordered, "no verified :free model recorded today"
    return ordered


def _secrets():
    try:
        return router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")


def _key(name: str) -> tuple[str, list[str]]:
    found = _secrets()
    value = getattr(found, name)
    if not value:
        pytest.skip(f"{name} is unset")
    return value, [v for v in vars(found).values() if isinstance(v, str) and v]


def _record(name: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(name, json.dumps(payload, indent=2), FIXTURES, secret_values)


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


def _key_usage(key: str) -> float:
    with _skip_on_429("openrouter"):
        payload = router_sources.fetch_openrouter_key(key)
    return router_live.parse_openrouter_key_info(payload).usage


def _real_chat(model: str, headers: dict, candidates: list[str]) -> tuple[str, dict]:
    """One minimal real chat, failing over across verified ``:free`` ids."""
    last_error: Exception | None = None
    tried = [model, *[item for item in candidates if item != model]]
    for candidate in tried:
        try:
            result, raw = router_probes.run_probe(
                "openrouter", "chat", candidate, headers, enabled=True)
        except router_live.LiveRateLimited:
            pytest.skip("real 429 for openrouter; resume tomorrow")
        except router_probes.ProbeError as exc:
            if exc.http_status in (400, 404, 503) or exc.http_status is None:
                last_error = exc
                continue
            raise
        content = raw["choices"][0]["message"]["content"]
        assert "ok" in content.lower()
        return candidate, {"in": result.input_tokens, "out": result.output_tokens}
    assert last_error is not None
    raise last_error


def test_short_session_with_injected_429_live(tmp_path):
    or_key, or_secrets = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    headers = {"Authorization": f"Bearer {or_key}"}
    verified = _openrouter_candidates()
    store = router_availability.AvailabilityStore(tmp_path / "availability.sqlite")
    session = wiring.ResearchSession(
        research_id="r-live",
        candidates=[Candidate("openrouter/dead-route", "fam-dead",
                              "canon-dead", "openrouter", 0.9),
                    Candidate(f"openrouter/{verified[0]}", "fam-real",
                              "canon-real", "openrouter", 0.8)],
        store=store, checkpoints=tmp_path / "ckpt",
        objective="live smoke", plan=["five turns"])
    receipts: list[dict] = []
    try:
        usage_before = _key_usage(or_key)
        with router_proxy.FaultProxy(
                {"openrouter": router_proxy.FaultSpec(
                    "rate_limited", retry_after_s=60)}) as proxy:
            def invoke(request):
                if request.model.endswith("dead-route"):
                    try:
                        proxy.post("openrouter", {"model": "dead-route"})
                    except router_live.LiveRateLimited as exc:
                        return wiring.ProviderTurn(
                            text="", complete=False,
                            error_text=f"injected fault: {exc}",
                            failure_stage="before_response")
                    raise AssertionError("dead route unexpectedly served")
                served, tokens = _real_chat(
                    request.model.removeprefix("openrouter/"), headers, verified)
                receipts.append({"model": served, **tokens})
                return wiring.ProviderTurn(
                    text=f"live answer ({served})", complete=True,
                    executed_tools=(wiring.ToolCall(
                        id=f"live-t{len(receipts)}", name="research_read"),),
                    tool_results=((f"live-t{len(receipts)}",
                                   "live page (receipt only)"),))

            outcomes = [
                session.run_turn(
                    f"live turn {turn}", invoke,
                    queries_and_sources=[(f"lq{turn}", "https://example.org/live")],
                    validated=[f"lv{turn}"])
                for turn in range(1, 6)]
            assert proxy.served == [("openrouter", "rate_limited")]
        assert [item.turn for item in outcomes] == [1, 2, 3, 4, 5]
        assert outcomes[0].rotations == 1
        assert all(item.rotations == 0 for item in outcomes[1:])
        effective = [item.model_effective for item in outcomes]
        assert effective[0] == f"openrouter/{receipts[0]['model']}"
        assert len(set(effective)) == 1
        assert len(receipts) == 5
        saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-live")
        assert saved is not None and saved.turn == 5
        assert len(saved.validated_results) == 5
        assert len(saved.applied_tools) == 5
        rows = store.errors_since(0)
        assert [(item.error_class, item.deployment_id) for item in rows] == [
            ("rate_limited", "openrouter/dead-route")]
        _record("research-session-live.json", {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "turns": 5, "rotations_first_turn": 1,
            "effective_model": effective[0],
            "input_tokens": [item["in"] for item in receipts],
            "output_tokens": [item["out"] for item in receipts],
            "errors": [{"class": item.error_class,
                        "deployment": item.deployment_id} for item in rows],
            "note": "receipts only: no prompts, answers or tool payloads recorded",
        }, or_secrets)
        usage_after = _key_usage(or_key)
        router_live.assert_usage_unchanged(usage_before, usage_after)
    finally:
        session.close()
        store.close()
