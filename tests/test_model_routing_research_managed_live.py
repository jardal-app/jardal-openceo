"""Live short managed research session with a real switch (TASK-2026-00307).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. Three turns run on one owned OpenCode session: the first
attempt fails with a proxy-injected fault (spending nothing) and the
session really switches models through ``session.switchModel``,
keeping affinity for the rest. Native ids come from the recorded
instance catalog (switches re-verified without generation on
opencode v2.0.22); the group B reserve is Groq, which served the
00306 live review. Billed cost must stay zero. Never prints
secrets: the recorded fixture holds receipts only, no prompts or
answers.
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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import availability as router_availability  # noqa: E402
from model_routing import fault_proxy as router_proxy  # noqa: E402
from model_routing import live as router_live  # noqa: E402
from model_routing import research_wiring as wiring  # noqa: E402
from model_routing import secrets as router_secrets  # noqa: E402
from model_routing.models import (  # noqa: E402
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    ProfileRanking,
    RankedDeployment,
    RankedEntry,
    RankingsDocument,
)

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

TURNS = 3


def _secrets():
    try:
        return router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")


def _secret_values() -> list[str]:
    found = _secrets()
    return [v for v in vars(found).values() if isinstance(v, str) and v]


def _record(name: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(
        name, json.dumps(payload, indent=2), FIXTURES, secret_values)


def _native_dep(model_id: str) -> Deployment:
    return Deployment(
        deployment_id=f"opencode/{model_id}", provider="opencode",
        route_type="opencode_native_free", allowed_client="opencode_only",
        provider_id="opencode", model_id=model_id,
        gateway_model_id=model_id,
        free_offered=True, free_verified=True, healthy=True,
        input_price_usd=0.0, output_price_usd=0.0,
        approved_confidentiality=[Confidentiality.PUBLIC],
        source="managed-live-test",
        observed_at=datetime.now(timezone.utc),
    )


def _external_dep(provider: str, model_id: str) -> Deployment:
    return Deployment(
        deployment_id=f"{provider}/{model_id}", provider=provider,
        route_type="external_free",
        allowed_client="configured_opencode_or_gateway",
        provider_id=provider, model_id=model_id,
        gateway_model_id=model_id,
        free_offered=True, free_verified=True, healthy=True,
        input_price_usd=0.0, output_price_usd=0.0,
        approved_confidentiality=[Confidentiality.PUBLIC],
        source="managed-live-test",
        observed_at=datetime.now(timezone.utc),
    )


def _live_catalog() -> CatalogDocument:
    return CatalogDocument(
        generated_at=datetime.now(timezone.utc),
        generator="managed-live-test",
        entries=[
            CatalogEntry(
                family_id="big-pickle", canonical_model_id="org/big-pickle",
                display_name="Big Pickle",
                capabilities=Capabilities(tools=True, context_tokens=131072),
                deployments=[_native_dep("big-pickle")]),
            CatalogEntry(
                family_id="beam", canonical_model_id="org/beam-free",
                display_name="Beam Free",
                capabilities=Capabilities(tools=True, context_tokens=131072),
                deployments=[_native_dep("beam-free")]),
            CatalogEntry(
                family_id="gpt-oss", canonical_model_id="openai/gpt-oss-120b",
                display_name="GPT OSS 120B",
                capabilities=Capabilities(tools=True, context_tokens=131072),
                deployments=[_external_dep(
                    "groq", "openai/gpt-oss-120b")]),
        ],
    )


def _live_rankings() -> RankingsDocument:
    order = [
        RankedEntry(
            rank=1, canonical_model_id="org/big-pickle",
            family_id="big-pickle",
            primary=RankedDeployment(
                deployment_id="opencode/big-pickle", provider="opencode"),
            backups=[], quality=0.90, coverage=1.0,
            dimensions={"agentic": 0.90},
            quality_sources=["synthetic-fixture"]),
        RankedEntry(
            rank=2, canonical_model_id="org/beam-free",
            family_id="beam",
            primary=RankedDeployment(
                deployment_id="opencode/beam-free", provider="opencode"),
            backups=[], quality=0.88, coverage=1.0,
            dimensions={"agentic": 0.88},
            quality_sources=["synthetic-fixture"]),
        RankedEntry(
            rank=3, canonical_model_id="openai/gpt-oss-120b",
            family_id="gpt-oss",
            primary=RankedDeployment(
                deployment_id="groq/openai/gpt-oss-120b", provider="groq"),
            backups=[], quality=0.85, coverage=1.0,
            dimensions={"agentic": 0.85},
            quality_sources=["synthetic-fixture"]),
    ]
    return RankingsDocument(
        generated_at=datetime.now(timezone.utc),
        generator="managed-live-test",
        catalog_snapshot="catalog-live-test",
        catalog_sha256="0" * 64,
        profiles={"free-research": ProfileRanking(
            profile="free-research", order=order, experimental=[],
            pending_top3=None,
            provenance={"catalog_snapshot": "catalog-live-test",
                        "catalog_sha256": "0" * 64})},
    )


@contextmanager
def _live_project():
    with tempfile.TemporaryDirectory(prefix="free-router-managed-") as workdir:
        directory = Path(workdir)
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True,
                       timeout=60)
        yield directory


def test_short_managed_session_with_real_switch_live(tmp_path):
    secret_values = _secret_values()
    secrets = router_secrets.load_router_secrets()
    groq_key = secrets.FREE_ROUTER_GROQ_API_KEY
    if not groq_key:
        pytest.skip("FREE_ROUTER_GROQ_API_KEY is unset")
    rankings = _live_rankings()
    catalog = _live_catalog()
    snapshot = wiring.SessionSnapshot(
        document=rankings, profile=rankings.profiles["free-research"],
        file_sha256="sha256:live-test", catalog_snapshot="catalog-live-test",
        generated_at=datetime.now(timezone.utc).isoformat())
    store = router_availability.AvailabilityStore(
        tmp_path / "availability.sqlite")
    targets = wiring.native_research_targets(
        snapshot, catalog, store,
        credentials={"FREE_ROUTER_GROQ_API_KEY": groq_key},
        run_id="r-live-managed")
    assert [item.model_arg() for item in targets] == [
        "opencode/big-pickle", "opencode/beam-free",
        "groq/openai/gpt-oss-120b"]
    client = wiring.SessionClient(timeout=90.0)
    previous = os.environ.get("FREE_ROUTER_GROQ_API_KEY")
    os.environ["FREE_ROUTER_GROQ_API_KEY"] = groq_key
    try:
        with _live_project() as project:
            session = wiring.ManagedResearchSession(
                research_id="r-live-managed", candidates=targets,
                store=store, checkpoints=tmp_path / "ckpt",
                session_client=client, project_dir=project,
                catalog=catalog, objective="live smoke",
                plan=["three turns"])
            try:
                with router_proxy.FaultProxy(
                        {"opencode": router_proxy.FaultSpec(
                            "rate_limited", retry_after_s=60)}) as proxy:
                    injected = {"done": False}

                    def invoke(request):
                        if not injected["done"]:
                            injected["done"] = True
                            try:
                                proxy.post(
                                    "opencode", {"model": "inject"})
                            except router_live.LiveRateLimited as exc:
                                return wiring.ProviderTurn(
                                    text="", complete=False,
                                    error_text=f"injected fault: {exc}",
                                    failure_stage="before_response")
                            raise AssertionError(
                                "injected fault unexpectedly served")
                        owned = session.managed_session_id
                        assert owned, "turn ran before the session started"
                        return wiring.run_managed_turn(
                            session_id=owned, project_dir=project,
                            prompt=request.prompt)

                    try:
                        outcomes = [
                            session.run_turn(
                                f"Reply with exactly this word: sky{turn}",
                                invoke, validated=[f"lv{turn}"])
                            for turn in range(1, TURNS + 1)]
                    except wiring.SessionBlocked as blocked:
                        rows = store.errors_since(0)
                        quota_hit = {item.deployment_id for item in rows
                                     if item.error_class in (
                                         "rate_limited", "daily_quota")}
                        if (blocked.code
                                == wiring.BLOCKED_NO_ELIGIBLE_MODEL
                                and len(quota_hit) > 1):
                            pytest.skip(
                                "real quota hit on every live route; "
                                "resume tomorrow")
                        raise
                    assert proxy.served == [("opencode", "rate_limited")]
                assert [item.turn for item in outcomes] == [1, 2, 3]
                assert outcomes[0].rotations in (1, 2)
                assert all(item.rotations == 0 for item in outcomes[1:])
                effective = [item.model_effective for item in outcomes]
                assert len(set(effective)) == 1
                for item in outcomes:
                    assert item.text.strip(), "turn carried no text"
                owned = session.managed_session_id
                assert owned
                info = client.get_session(project, owned)
                assert info.cost == 0
                assert info.opencode_model == session.managed_model
                saved = wiring.load_checkpoint(tmp_path / "ckpt",
                                               "r-live-managed")
                assert saved is not None and saved.turn == TURNS
                switches = [
                    detail
                    for _ts, event, detail
                    in store.audit_since("r-live-managed", 0.0)
                    if event == "research_model_switch"]
                assert len(switches) == outcomes[0].rotations + 1
                assert "reason=initial" in switches[0]
                assert "reason=rotation after rate_limited" in switches[1]
                rows = store.errors_since(0)
                _record("research-managed-session-live.json", {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "turns": TURNS,
                    "rotations_first_turn": outcomes[0].rotations,
                    "effective_model": effective[0],
                    "session_model": info.opencode_model,
                    "cost": info.cost,
                    "input_tokens": info.input_tokens,
                    "output_tokens": info.output_tokens,
                    "switches": len(switches),
                    "errors": [{"class": item.error_class,
                                "deployment": item.deployment_id}
                               for item in rows],
                    "note": "receipts only: no prompts, answers or "
                            "tool payloads recorded",
                }, secret_values)
            finally:
                session.close()
    finally:
        if previous is None:
            os.environ.pop("FREE_ROUTER_GROQ_API_KEY", None)
        else:
            os.environ["FREE_ROUTER_GROQ_API_KEY"] = previous
        store.close()
