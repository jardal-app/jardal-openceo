"""Live short launcher session with a native model (TASK-2026-00308).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. The launcher prepares a real managed session on the best
native target, one minimal turn answers through it, and the
controller observes the healthy session back. The interactive
attach itself (``opencode --session <id> <dir>``) cannot run
headless; it was verified separately against the installed
opencode v2.0.22 on 2026-10-24 (the TUI attached to a prepared
session, preserved its valid controller-set model, and fell back
to its default only for an invalid one). Billed cost must stay
zero. Never prints secrets: the recorded fixture holds receipts
only, no prompts or answers.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import availability as router_availability  # noqa: E402
from model_routing import live as router_live  # noqa: E402
from model_routing import research_wiring as wiring  # noqa: E402
from model_routing import secrets as router_secrets  # noqa: E402
from model_routing import tui as launcher  # noqa: E402
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

_QUOTA_WORDS = ("429", "quota", "rate_limited", "rate limit",
                "freeusagelimit", "too many requests")


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
        source="tui-live-test",
        observed_at=datetime.now(timezone.utc),
    )


def _live_catalog() -> CatalogDocument:
    return CatalogDocument(
        generated_at=datetime.now(timezone.utc),
        generator="tui-live-test",
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
    ]
    return RankingsDocument(
        generated_at=datetime.now(timezone.utc),
        generator="tui-live-test",
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
    with tempfile.TemporaryDirectory(prefix="free-router-tui-") as workdir:
        directory = Path(workdir)
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True,
                       timeout=60)
        yield directory


def test_short_launcher_session_with_native_model_live(tmp_path):
    secret_values = _secret_values()
    rankings = _live_rankings()
    catalog = _live_catalog()
    snapshot = wiring.SessionSnapshot(
        document=rankings, profile=rankings.profiles["free-research"],
        file_sha256="sha256:live-test", catalog_snapshot="catalog-live-test",
        generated_at=datetime.now(timezone.utc).isoformat())
    store = router_availability.AvailabilityStore(
        tmp_path / "availability.sqlite")
    try:
        targets = wiring.native_research_targets(
            snapshot, catalog, store, credentials={},
            run_id="r-live-tui")
    except wiring.ResearchRoutingError as exc:
        store.close()
        pytest.skip(f"no live target today: {exc}")
    assert [item.model_arg() for item in targets] == [
        "opencode/big-pickle", "opencode/beam-free"]
    client = wiring.SessionClient(timeout=90.0)
    try:
        with _live_project() as project:
            plan = launcher.prepare_tui_session(
                client=client, project_dir=project, targets=targets,
                store=store, run_id="r-live-tui")
            try:
                assert plan.argv[:2] == ("opencode", "--session")
                assert plan.argv[2] == plan.session_id
                assert plan.argv[3] == str(project)
                turn = wiring.run_managed_turn(
                    session_id=plan.session_id, project_dir=project,
                    prompt="Reply with exactly: ok", timeout=240.0)
                if not turn.complete:
                    lowered = (turn.error_text or "").lower()
                    if any(word in lowered for word in _QUOTA_WORDS):
                        pytest.skip(
                            "real quota hit on the native group; "
                            "resume tomorrow")
                    pytest.fail(f"live turn failed: {turn.error_text}")
                assert turn.text.strip(), "turn carried no text"
                seen = launcher.observe_tui_session(
                    client=client, project_dir=project,
                    session_id=plan.session_id)
                assert seen.failure is None
                assert seen.model == plan.target.model_arg()
                info = client.get_session(project, plan.session_id)
                assert info.cost == 0
                _record("tui-launcher-session-live.json", {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "session_served": seen.model,
                    "plan_target": plan.target.model_arg(),
                    "attach_argv_head": list(plan.argv[:2]),
                    "cost": info.cost,
                    "input_tokens": info.input_tokens,
                    "output_tokens": info.output_tokens,
                    "watermark_seen": seen.watermark is not None,
                    "note": "receipts only: no prompts, answers or "
                            "tool payloads recorded",
                }, secret_values)
            finally:
                client.delete_session(project, plan.session_id)
    finally:
        store.close()
