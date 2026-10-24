"""F6 acceptance battery T01–T20, §16 metrics and F6 operations (TASK-2026-00297).

The battery drives the real router paths (store, classifiers,
loopback fault proxy, rankings, review/research wiring, refresh,
external) with simulated providers, injected faults and the live
fixtures the epic already recorded. Nothing leaves loopback and no
model is called; the single live check of this ticket (OpenRouter
credit usage unchanged) lives in
``tests/test_model_routing_acceptance_live.py``.

The committed ``docs/model_routing/ACCEPTANCE.md`` is the rendered
output of :func:`generate_report_text`: deterministic, no
timestamps, reproducible by re-running the battery.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from model_routing import alerts as router_alerts
from model_routing import external as router_external
from model_routing import refresh as router_refresh
from model_routing import research_wiring as research_mod
from model_routing import review_wiring as review_mod
from model_routing.availability import (
    AvailabilityStore,
    Candidate,
    Exclusions,
    ProfileRequirements,
    ReservationConflict,
    backups_of,
    get_next_candidate,
    list_eligible_models,
    meets_profile,
    record_provider_error,
    reserve_model,
)
from model_routing.errors import (
    classify_http,
    classify_text,
)
from model_routing.fault_proxy import FaultProxy, FaultSpec
from model_routing.live import LiveRateLimited, scan_for_secrets
from model_routing.models import (
    CATALOG_SCHEMA_VERSION,
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Deployment,
    DeploymentLimits,
    RankingsDocument,
    RouterPolicy,
    is_usable,
    usability,
)
from model_routing.publish import (
    load_catalog_doc,
    publish_catalog,
)
from model_routing.ranking import (
    RANKINGS_SCHEMA_VERSION,
    build_rankings,
    fallback_chain,
    load_rankings,
    publish_rankings,
)

__all__ = [
    "ACCEPTANCE_REPORT_PATH",
    "REAL_CALLS_BY_TICKET",
    "AcceptanceMetrics",
    "BatteryReport",
    "CostAudit",
    "ScenarioResult",
    "SessionRow",
    "active_sessions",
    "battery_catalog",
    "battery_rankings",
    "cost_audit",
    "generate_report_text",
    "render_report",
    "run_battery",
    "total_real_calls",
]

ACCEPTANCE_REPORT_FILENAME = "ACCEPTANCE.md"

_REPO_ROOT = Path(__file__).resolve().parents[2]

ACCEPTANCE_REPORT_PATH = _REPO_ROOT / "docs" / "model_routing" / ACCEPTANCE_REPORT_FILENAME

_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "model_routing_live"

_BATTERY_GENERATOR = "model_routing.acceptance"

#: Real provider calls spent by the epic, per code ticket, as recorded
#: in each ticket's builder comment (ERPNext): ledger deltas before
#: the Owner removed the live budgets (00283), plus the metadata-only
#: updates of 00292 and the single fed calls of 00293–00295. 00297
#: contributes only the OpenRouter usage-unchanged check (no
#: generation). Figures are dated 2026-10-23 with the ticket sources.
REAL_CALLS_BY_TICKET: tuple[tuple[str, int, str], ...] = (
    ("TASK-2026-00285", 0, "F0 without tokens"),
    ("TASK-2026-00287", 16, "8 live probes + 8 diagnosed metadata calls"),
    ("TASK-2026-00288", 0, "no live tests"),
    ("TASK-2026-00289", 34, "catalogs + chat/tool probes per provider"),
    ("TASK-2026-00290", 3, "1 Artificial Analysis + 2 Hugging Face"),
    ("TASK-2026-00291", 9, "8 key reads + 1 failover chat"),
    ("TASK-2026-00292", 17, "metadata-only provider + benchmark updates"),
    ("TASK-2026-00293", 1, "single fed review under the fence"),
    ("TASK-2026-00294", 7, "5 session turns + 2 key reads"),
    ("TASK-2026-00295", 6, "minimal native OpenCode calls"),
    ("TASK-2026-00297", 2, "key before/after the battery, no generation"),
)


def total_real_calls() -> int:
    """Epic-wide provider calls; every one free and minimal."""
    return sum(count for _ticket, count, _note in REAL_CALLS_BY_TICKET)


@dataclass(frozen=True)
class ScenarioResult:
    """One T-scenario outcome with deterministic one-line evidence."""

    id: str
    title: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class AcceptanceMetrics:
    """SPEC §16 measurable criteria over the battery artifacts."""

    paid_invocations: int
    receipts_without_effective: int
    family_collisions: int
    snapshots_published: int
    snapshots_valid: int
    quota_errors: int
    quota_classified: int
    pre_response_attempts: int
    pre_response_recovered: int
    tool_repeats_without_check: int

    @property
    def snapshots_valid_rate(self) -> float:
        if not self.snapshots_published:
            return 0.0
        return self.snapshots_valid / self.snapshots_published

    @property
    def quota_classification_rate(self) -> float:
        if not self.quota_errors:
            return 1.0
        return self.quota_classified / self.quota_errors

    @property
    def pre_response_recovery_rate(self) -> float:
        if not self.pre_response_attempts:
            return 1.0
        return self.pre_response_recovered / self.pre_response_attempts

    def failures(self) -> list[str]:
        """Criteria lines that miss their §16 threshold."""
        problems: list[str] = []
        if self.paid_invocations:
            problems.append(f"{self.paid_invocations} paid invocations")
        if self.receipts_without_effective:
            problems.append(f"{self.receipts_without_effective} receipts without effective model")
        if self.family_collisions:
            problems.append(f"{self.family_collisions} family collisions")
        if self.snapshots_valid_rate < 1.0:
            problems.append("snapshots below 100% valid")
        if self.quota_classification_rate < 1.0:
            problems.append("quota errors below 100% classified")
        if self.pre_response_recovery_rate < 0.95:
            problems.append("pre-response recovery below 95%")
        if self.tool_repeats_without_check:
            problems.append(f"{self.tool_repeats_without_check} tool repeats without check")
        return problems


@dataclass(frozen=True)
class BatteryReport:
    """Full battery outcome: scenarios, metrics and replay notes."""

    scenarios: tuple[ScenarioResult, ...]
    metrics: AcceptanceMetrics
    replay_notes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.scenarios) and not self.metrics.failures()


@dataclass
class _Ledger:
    """Battery-wide artifacts the §16 metrics are computed from."""

    served: list[str] = field(default_factory=list)
    receipts_effective: list[str] = field(default_factory=list)
    round_families: list[tuple[str, ...]] = field(default_factory=list)
    snapshots_published: int = 0
    snapshots_valid: int = 0
    quota_errors: int = 0
    quota_classified: int = 0
    pre_response_attempts: int = 0
    pre_response_recovered: int = 0
    tool_repeats_without_check: int = 0


def _deployment(deployment_id: str, provider: str, gateway: str, *,
                tpm: int | None = None, free_verified: bool = True,
                price: float | None = 0.0) -> Deployment:
    native = provider == "opencode"
    return Deployment(
        deployment_id=deployment_id, provider=provider,
        route_type="opencode_native_free" if native else "external_free",
        allowed_client="opencode_only" if native else "configured_opencode_or_gateway",
        provider_id=provider, model_id=deployment_id.split("/", 1)[1],
        gateway_model_id=gateway,
        free_offered=True, free_verified=free_verified, healthy=True,
        input_price_usd=price, output_price_usd=price,
        limits=DeploymentLimits(tpm=tpm))


def battery_catalog() -> CatalogDocument:
    """Synthetic free-only catalog; every route is verified and healthy."""
    tools_128k = Capabilities(tools=True, streaming=True, context_tokens=131072)
    tools_32k = Capabilities(tools=True, streaming=True, context_tokens=32768)
    no_tools = Capabilities(tools=False, streaming=True, context_tokens=131072)
    entries = [
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="fam-alpha", canonical_model_id="example/alpha",
            capabilities=tools_128k,
            deployments=[
                _deployment("opencode/alpha-free", "opencode", "alpha-free", tpm=60000),
                _deployment("openrouter/example/alpha:free", "openrouter",
                            "example/alpha:free", tpm=20000),
            ]),
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="fam-beta", canonical_model_id="example/beta",
            capabilities=tools_128k,
            deployments=[_deployment("opencode/beta-free", "opencode", "beta-free")]),
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="fam-gamma", canonical_model_id="example/gamma",
            capabilities=tools_32k,
            deployments=[_deployment("opencode/gamma-free", "opencode", "gamma-free")]),
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="fam-delta", canonical_model_id="example/delta",
            capabilities=tools_32k,
            deployments=[
                _deployment("groq/delta-20b", "groq", "delta-20b", tpm=30000),
                _deployment("nvidia/delta-20b", "nvidia", "delta-20b"),
            ]),
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="fam-epsilon", canonical_model_id="example/epsilon",
            capabilities=no_tools,
            deployments=[_deployment("opencode/epsilon-free", "opencode", "epsilon-free")]),
        CatalogEntry(
            schema_version=CATALOG_SCHEMA_VERSION,
            family_id="unknown-family", canonical_model_id="example/zeta",
            capabilities=tools_128k,
            deployments=[_deployment("opencode/zeta-free", "opencode", "zeta-free")]),
    ]
    return CatalogDocument(
        schema_version=CATALOG_SCHEMA_VERSION, generator=_BATTERY_GENERATOR,
        entries=entries)


def _ranked(rank: int, canonical: str, family: str, deployment: str,
            quality: float, backups: tuple[tuple[str, str], ...] = ()) -> dict:
    return {
        "rank": rank, "canonical_model_id": canonical, "family_id": family,
        "primary": {"deployment_id": deployment, "provider": deployment.split("/")[0]},
        "backups": [{"deployment_id": dep, "provider": prov} for dep, prov in backups],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"coding": quality, "agentic": quality},
        "quality_sources": ["acceptance-battery"],
    }


def battery_rankings() -> RankingsDocument:
    """Synthetic orders with provenance; unknown-family never ranks strict."""
    order = [
        _ranked(1, "example/alpha", "fam-alpha", "opencode/alpha-free", 0.90,
                (("openrouter/example/alpha:free", "openrouter"),)),
        _ranked(2, "example/beta", "fam-beta", "opencode/beta-free", 0.85),
        _ranked(3, "example/gamma", "fam-gamma", "opencode/gamma-free", 0.80),
    ]
    profiles = {}
    for profile in ("free-review", "free-research", "free-general"):
        profiles[profile] = {
            "profile": profile, "order": order,
            "experimental": [{"canonical_model_id": "example/zeta",
                              "family_id": "unknown-family",
                              "reason": "unknown family never counts strict",
                              "observations": 1}],
            "pending_top3": None,
            "provenance": {
                "catalog_snapshot": "catalog-acceptance",
                "catalog_sha256": "0" * 64,
                "sources": ["openrouter", "groq", "nvidia", "opencode",
                            "artificial-analysis", "lmarena"],
                "observed_at": "2026-10-23T00:00:00+00:00",
                "weights": {"coding": 0.5, "agentic": 0.5},
                "quality_floor": 0.45,
                "aa_index_version": 4.3,
                "lmarena_target": "agent/latest",
            },
        }
    return RankingsDocument.model_validate({
        "schema_version": RANKINGS_SCHEMA_VERSION,
        "generated_at": "2026-10-23T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR,
        "catalog_snapshot": "catalog-acceptance",
        "catalog_sha256": "0" * 64,
        "profiles": profiles,
    })


def _battery_candidates() -> list[Candidate]:
    return [
        Candidate(deployment_id="opencode/alpha-free", family_id="fam-alpha",
                  canonical_model_id="example/alpha", provider="opencode", quality=0.90),
        Candidate(deployment_id="opencode/beta-free", family_id="fam-beta",
                  canonical_model_id="example/beta", provider="opencode", quality=0.85),
        Candidate(deployment_id="opencode/gamma-free", family_id="fam-gamma",
                  canonical_model_id="example/gamma", provider="opencode", quality=0.80),
    ]


def _store(directory: Path) -> AvailabilityStore:
    return AvailabilityStore(directory / "availability.sqlite")


def _scenario_t01(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        candidates = _battery_candidates()
        first, second = candidates[0], candidates[1]
        with FaultProxy({"opencode": FaultSpec("rate_limited", retry_after_s=30)}) as proxy:
            try:
                proxy.post("opencode", {"model": first.deployment_id})
            except LiveRateLimited as exc:
                retry_after = exc.retry_after
            else:
                raise AssertionError("injected 429 unexpectedly served")
        assert proxy.served == [("opencode", "rate_limited")]
        classified = classify_http(429, retry_after=retry_after,
                                   detail="injected 429 before any response")
        assert classified.error_class == "rate_limited"
        ledger.quota_errors += 1
        if classified.error_class != "unclassified":
            ledger.quota_classified += 1
        record_provider_error(
            store, provider="opencode", deployment_id=first.deployment_id,
            model=first.deployment_id, classified=classified, run_id="acceptance-t01")
        ledger.pre_response_attempts += 1
        served = get_next_candidate(candidates, store)
        assert served.deployment_id == second.deployment_id, served
        ledger.pre_response_recovered += 1
        ledger.served.append(served.deployment_id)
        assert store.cooldown_for(first.deployment_id) is not None
        return ScenarioResult("T01", "429 before response", True,
                              f"429 on {first.deployment_id} cooled; served {served.deployment_id}")
    finally:
        store.close()


def _scenario_t02(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        classified = classify_text("daily free quota exhausted for this account")
        assert classified.error_class == "daily_quota", classified
        ledger.quota_errors += 1
        ledger.quota_classified += 1
        record_provider_error(
            store, provider="openrouter", deployment_id="openrouter/example/alpha:free",
            model="openrouter/example/alpha:free", classified=classified,
            run_id="acceptance-t02")
        assert store.provider_blocked("openrouter") is not None
        ledger.pre_response_attempts += 1
        eligible = list_eligible_models("free-general", catalog, store)
        providers = {item.provider for item in eligible}
        assert "openrouter" not in providers, providers
        assert {"groq", "opencode"} <= providers, providers
        ledger.pre_response_recovered += 1
        ledger.served.append("groq/delta-20b")
        return ScenarioResult("T02", "provider quota blocks one provider", True,
                              f"openrouter blocked; {len(eligible)} deployments eligible")
    finally:
        store.close()


def _scenario_t03(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    gates = ProfileRequirements(needs_tools=True, min_tpm=10000)
    by_provider = {route.provider: (entry, route)
                   for entry in catalog.entries for route in entry.deployments}
    groq_entry, groq_route = by_provider["groq"]
    nvidia_entry, nvidia_route = by_provider["nvidia"]
    ok, _reason = meets_profile(groq_entry, groq_route, gates)
    assert ok, "known TPM must clear the gate"
    blocked, why = meets_profile(nvidia_entry, nvidia_route, gates)
    assert not blocked and why, "unknown TPM must fail closed"
    return ScenarioResult("T03", "capacity gates before failover", True,
                          "groq tpm=30000 serves; nvidia tpm unknown blocks")


def _scenario_t04(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        held = store.reserve_round("acceptance-t04", "r1", [
            ("slot-a", "example/alpha", "fam-alpha", "opencode/alpha-free"),
            ("slot-b", "example/beta", "fam-beta", "opencode/beta-free"),
        ])
        ledger.round_families.append(tuple(item.family_id for item in held))
        try:
            reserve_model(store, "acceptance-t04", "r1", "slot-c",
                          "example/alpha", "fam-alpha", "openrouter/example/alpha:free")
        except ReservationConflict:
            pass
        else:
            raise AssertionError("duplicate family reserved without exception")
        excused = reserve_model(
            store, "acceptance-t04", "r1", "slot-c", "example/alpha", "fam-alpha",
            "openrouter/example/alpha:free", exception="quorum fallback, owner rule T07")
        assert excused.exception == "quorum fallback, owner rule T07"
        return ScenarioResult("T04", "family race needs an exception", True,
                              "duplicate family refused; recorded exception holds slot-c")
    finally:
        store.close()


def _scenario_t05(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    candidates = [
        Candidate(deployment_id="opencode/alpha-free", family_id="fam-alpha",
                  canonical_model_id="example/alpha", provider="opencode"),
        Candidate(deployment_id="openrouter/example/alpha:free", family_id="fam-alpha",
                  canonical_model_id="example/alpha", provider="openrouter"),
        Candidate(deployment_id="opencode/beta-free", family_id="fam-beta",
                  canonical_model_id="example/beta", provider="opencode"),
    ]
    store = _store(directory)
    try:
        assert len(backups_of(candidates[0], candidates)) == 1
        nxt = get_next_candidate(
            candidates, store,
            exclusions=Exclusions(canonicals=frozenset({"example/alpha"})))
        assert nxt.deployment_id == "opencode/beta-free", nxt
        return ScenarioResult("T05", "same base is a backup, not an opinion", True,
                              "excluding example/alpha skips both its routes")
    finally:
        store.close()


def _round_routing(directory: Path, run_id: str, round_name: str,
                   candidates: list[Candidate]) -> review_mod.RoundRouting:
    store = _store(directory)
    document = battery_rankings()
    snapshot = review_mod.RoundSnapshot(
        document=document, profile=document.profiles["free-review"],
        file_sha256="sha256:acceptance", catalog_snapshot="catalog-acceptance",
        generated_at="2026-10-23T00:00:00+00:00")
    labels = ("free-review-1", "free-review-2")
    states = {label: review_mod.LabelState(
        requested=candidate, effective=candidate,
        tried_deployments={candidate.deployment_id},
        tried_canonicals={candidate.canonical_model_id},
        tried_families={candidate.family_id})
        for label, candidate in zip(labels, candidates)}
    for label, candidate in zip(labels, candidates):
        reserve_model(store, run_id, round_name, label, candidate.canonical_model_id,
                      candidate.family_id, candidate.deployment_id)
    return review_mod.RoundRouting(
        run_id=run_id, round=round_name, snapshot=snapshot, store=store,
        candidates=candidates, labels=labels, states=states,
        round_families=frozenset(item.family_id for item in candidates[:2]),
        receipts_dir=directory / "receipts")


def _scenario_t06(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    candidates = _battery_candidates()
    routing = _round_routing(directory, "acceptance-t06", "r1", candidates)
    try:
        report_b = "## valid report from beta\n\nNo findings.\n"
        receipt_b = review_mod.record_receipt(
            routing, "free-review-2", report_text=report_b, reviewed_commit="c0ffee",
            started_at="2026-10-23T00:00:00+00:00", finished_at="2026-10-23T00:01:00+00:00")
        ledger.receipts_effective.append(receipt_b.effective_model)
        ledger.pre_response_attempts += 1
        nxt = review_mod.plan_fallback(
            routing, "free-review-1", outcome_kind="error",
            text="connection was closed")
        assert nxt is not None and nxt.family_id == "fam-gamma", nxt
        ledger.pre_response_recovered += 1
        ledger.served.append(nxt.deployment_id)
        assert routing.states["free-review-2"].effective.deployment_id == \
            "opencode/beta-free"
        loaded = review_mod.load_valid_receipt(
            routing.receipts_dir, run_id="acceptance-t06", round="r1",
            slot="free-review-2", reviewed_commit="c0ffee")
        assert loaded is not None and loaded[0].sha256 == receipt_b.sha256
        ledger.round_families.append(("fam-alpha", "fam-beta"))
        return ScenarioResult("T06", "only the failed reviewer restarts", True,
                              f"free-review-1 rotates to {nxt.deployment_id}; beta sha intact")
    finally:
        routing.close()


def _scenario_t07(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        lone = [Candidate(deployment_id="opencode/alpha-free", family_id="fam-alpha",
                           canonical_model_id="example/alpha", provider="opencode")]
        try:
            review_mod.pick_pair(lone, store)
        except review_mod.NoReviewPair:
            pass
        else:
            raise AssertionError("a lone family must never form a pair")
        return ScenarioResult("T07", "no two families, no fake quorum", True,
                              "NoReviewPair raised; nothing fabricated")
    finally:
        store.close()


def _scenario_t08(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    candidates = _battery_candidates()
    routing = _round_routing(directory, "acceptance-t08", "r2", candidates)
    try:
        r1 = review_mod.R1Result(
            receipts=(), round_families=frozenset({"fam-alpha", "fam-beta"}),
            tried_families=frozenset())
        first, second = review_mod.select_round2_pair(
            policy="reuse_best_plus_novel", r1=r1, candidates=candidates,
            store=routing.store)
        assert second is None or second.family_id != first.family_id
        routing.states["free-review-1"].effective = first
        report = "## round 2 on the corrected tree\n\nNo regressions.\n"
        receipt = review_mod.record_receipt(
            routing, "free-review-1", report_text=report, reviewed_commit="c0ffee2",
            started_at="2026-10-23T00:02:00+00:00", finished_at="2026-10-23T00:03:00+00:00")
        ledger.receipts_effective.append(receipt.effective_model)
        assert review_mod.load_valid_receipt(
            routing.receipts_dir, run_id="acceptance-t08", round="r2",
            slot="free-review-1", reviewed_commit="c0ffee2") is not None
        assert review_mod.load_valid_receipt(
            routing.receipts_dir, run_id="acceptance-t08", round="r2",
            slot="free-review-1", reviewed_commit="c0ffee") is None
        return ScenarioResult("T08", "round 2 reviews the corrected state", True,
                              f"r2 serves {first.deployment_id} on c0ffee2, not c0ffee")
    finally:
        routing.close()


def _scenario_t09(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    candidates = _battery_candidates()
    routing = _round_routing(directory, "acceptance-t09", "r1", candidates)
    try:
        first = review_mod.plan_fallback(
            routing, "free-review-1", outcome_kind="error", text="connection was closed")
        assert first is not None, "first correction must rotate"
        second = review_mod.plan_fallback(
            routing, "free-review-1", outcome_kind="error", text="connection was closed")
        assert second is None, "chain exhausted after two corrections"
        audit = routing.store.audit_since("acceptance-t09", 0.0)
        events = [event for _ts, event, _detail in audit]
        assert "review_fallback_exhausted" in events, events
        return ScenarioResult("T09", "corrections stay bounded and audited", True,
                              "second failure exhausts the chain with an audit event")
    finally:
        routing.close()


def _scenario_t10(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    ranking = battery_rankings().profiles["free-review"]
    first = fallback_chain(ranking, down={"example/alpha"})
    second = fallback_chain(ranking, down={"example/alpha"})
    assert first and first[0].canonical_model_id == "example/beta", first
    assert [item.canonical_model_id for item in first] == [
        item.canonical_model_id for item in second]
    ledger.pre_response_attempts += 1
    ledger.pre_response_recovered += 1
    ledger.served.append(first[0].primary.deployment_id)
    return ScenarioResult("T10", "number one down follows live priorities", True,
                          f"down example/alpha serves {first[0].canonical_model_id} twice alike")


def _scenario_t11(directory: Path, ledger: _Ledger, catalog: CatalogDocument,
                  fixtures: Path) -> ScenarioResult:
    active = RouterPolicy()
    unknown = _deployment("openrouter/mystery:free", "openrouter", "mystery:free", price=None)
    assert usability(unknown, active) == "unknown_price"
    assert not is_usable(unknown, active)
    paid = _deployment("openrouter/rich:free", "openrouter", "rich:free", price=0.001)
    assert usability(paid, active) == "paid"
    raw = json.loads((fixtures / "sources-openrouter-catalog.json").read_text(encoding="utf-8"))
    verified = raw.get("verified_free") or []
    assert verified, "recorded catalog carries no verified_free models"
    for item in verified:
        assert str(item.get("id", "")).endswith(":free"), item.get("id")
        pricing = item.get("pricing") or {}
        assert pricing.get("prompt") in ("0", 0), item.get("id")
        assert pricing.get("completion") in ("0", 0), item.get("id")
    return ScenarioResult("T11", "free status lost means unusable", True,
                          f"unknown/paid refused; {len(verified)} recorded :free replay clean")


def _scenario_t12(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    config = directory / "config"
    state = directory / "state"
    config.mkdir()
    publish_catalog(catalog, config)
    before = (config / "catalog.json").read_bytes()
    frozen_rankings = battery_rankings()
    frozen_path = directory / "round-rankings.json"
    frozen_path.write_text(frozen_rankings.model_dump_json(indent=2), encoding="utf-8")
    frozen_bytes = frozen_path.read_bytes()
    ledger.snapshots_published += 1
    loaded = load_catalog_doc(config / "catalog.json")
    assert len(loaded.entries) == len(catalog.entries)
    ledger.snapshots_valid += 1
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=_failing_update)
    assert code != 0, "the injected failure must fail the refresh"
    assert (config / "catalog.json").read_bytes() == before
    assert frozen_path.read_bytes() == frozen_bytes
    reloaded = load_rankings(frozen_path)
    assert reloaded.catalog_snapshot == frozen_rankings.catalog_snapshot
    return ScenarioResult("T12", "failed refresh keeps the round intact", True,
                          "catalog byte-identical; frozen round untouched")


def _failing_update() -> Any:
    raise router_refresh.RefreshError("injected source outage")


def _scenario_t13(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    checkpoints = directory / "ckpt"
    store = _store(directory)
    session = research_mod.ResearchSession(
        research_id="acceptance-t13", candidates=_battery_candidates(),
        store=store, checkpoints=checkpoints, sleeper=lambda _s: None)
    executions: dict[str, int] = {}
    calls = {"count": 0}

    def invoke(request: Any) -> Any:
        calls["count"] += 1
        tool = research_mod.ToolCall(id="t1", name="research_read")
        if "t1" not in {item.id for item in request.applied_tools}:
            executions["t1"] = executions.get("t1", 0) + 1
        if calls["count"] == 1:
            return research_mod.ProviderTurn(
                text="prefix-", complete=False, executed_tools=(tool,),
                tool_results=(("t1", "sky page"),),
                error_text="connection was closed", failure_stage="partial_text")
        return research_mod.ProviderTurn(
            text="prefix-done", complete=True, executed_tools=(tool,),
            tool_results=(("t1", "sky page"),))

    try:
        outcome = session.run_turn("synthetic prompt", invoke, validated=["sky"])
    finally:
        session.close()
        store.close()
    assert outcome.turn == 1, outcome
    assert executions.get("t1") == 1, executions
    saved = research_mod.load_checkpoint(checkpoints, "acceptance-t13")
    assert saved is not None and saved.validated_results == ("sky",)
    ledger.tool_repeats_without_check += max(0, executions.get("t1", 0) - 1)
    return ScenarioResult("T13", "cut stream never reruns the tool", True,
                          "t1 executed once; checkpoint rebuilds the turn")


def _scenario_t14(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    plans = router_external.plan_consumer_routes(
        catalog, battery_rankings(), gateway_available=False)
    review = plans["free-review"]
    assert review.mode == "native", review
    assert review.native, review
    assert "gateway" in review.notice.lower(), review.notice
    first_native = review.native[0].deployment_id
    return ScenarioResult("T14", "absent gateway falls back to native", True,
                          f"free-review native via {first_native} with notice")


def _scenario_t15(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        key_like = "sk-" + "x" * 12
        bearer_like = "Bearer " + "y" * 12
        injected = f"token {key_like} and {bearer_like} in one line"
        classified = classify_text("upstream error with an injected token")
        record = record_provider_error(
            store, provider="groq", deployment_id="groq/delta-20b",
            model="groq/delta-20b", classified=classified,
            run_id="acceptance-t15", excerpt=injected)
        assert key_like not in record.excerpt, record.excerpt
        assert bearer_like not in record.excerpt, record.excerpt
        leaks = scan_for_secrets(
            " ".join(row.excerpt for row in store.errors_since(0.0)), ())
        key_leaks = [leak for leak in leaks if "key-like" in leak]
        assert not key_leaks, key_leaks
        return ScenarioResult("T15", "injected secrets never persist", True,
                              "excerpt redacted; registry scan finds no key-like pattern")
    finally:
        store.close()


def _scenario_t16(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        gates = ProfileRequirements(needs_tools=True)
        by_canonical = {entry.canonical_model_id: entry for entry in catalog.entries}
        broken = by_canonical["example/epsilon"]
        ok, _reason = meets_profile(broken, broken.deployments[0], gates)
        assert not ok, "a tools-less model must fail the review gate"
        candidates = [
            Candidate(deployment_id="opencode/epsilon-free", family_id="fam-epsilon",
                      canonical_model_id="example/epsilon", provider="opencode"),
            Candidate(deployment_id="opencode/alpha-free", family_id="fam-alpha",
                      canonical_model_id="example/alpha", provider="opencode"),
        ]
        compatible = [item for item in candidates
                      if meets_profile(by_canonical[item.canonical_model_id],
                                       by_canonical[item.canonical_model_id].deployments[0],
                                       gates)[0]]
        assert [item.deployment_id for item in compatible] == ["opencode/alpha-free"]
        ledger.pre_response_attempts += 1
        served = get_next_candidate(compatible, store)
        ledger.pre_response_recovered += 1
        ledger.served.append(served.deployment_id)
        return ScenarioResult("T16", "broken tools leave the profile", True,
                              f"epsilon excluded; served {served.deployment_id}")
    finally:
        store.close()


def _scenario_t17(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    ranking = battery_rankings().profiles["free-review"]
    candidates = review_mod.review_candidates(ranking)
    families = [item.family_id for item in candidates]
    assert "unknown-family" not in families, families
    assert families[:2] == ["fam-alpha", "fam-beta"], families
    store = _store(directory)
    try:
        first, second = review_mod.pick_pair(candidates, store)
        assert first.family_id != second.family_id
        ledger.round_families.append((first.family_id, second.family_id))
        ledger.served.extend([first.deployment_id, second.deployment_id])
        return ScenarioResult("T17", "two valid reviews need distinct families", True,
                              f"pair {first.family_id}+{second.family_id}; unknown dropped")
    finally:
        store.close()


def _scenario_t18(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    candidates = _battery_candidates()
    routing = _round_routing(directory, "acceptance-t18", "r1", candidates)
    try:
        report = "## verified report\n\nNo findings.\n"
        receipt = review_mod.record_receipt(
            routing, "free-review-1", report_text=report, reviewed_commit="deadbeef",
            started_at="2026-10-23T00:04:00+00:00", finished_at="2026-10-23T00:05:00+00:00")
        ledger.receipts_effective.append(receipt.effective_model)
        assert receipt.effective_model and receipt.sha256
        loaded = review_mod.load_valid_receipt(
            routing.receipts_dir, run_id="acceptance-t18", round="r1",
            slot="free-review-1", reviewed_commit="deadbeef")
        assert loaded is not None and loaded[1] == report
        _json_path, md_path = review_mod.receipt_filenames(
            routing.receipts_dir, "acceptance-t18", "r1", "free-review-1")
        md_path.write_text(report + "tampered\n", encoding="utf-8")
        assert review_mod.load_valid_receipt(
            routing.receipts_dir, run_id="acceptance-t18", round="r1",
            slot="free-review-1", reviewed_commit="deadbeef") is None
        return ScenarioResult("T18", "restarts reuse verified reports only", True,
                              "valid receipt reuses; tampered bytes refuse")
    finally:
        routing.close()


def _scenario_t19(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    config = directory / "config"
    config.mkdir()
    document = build_rankings(
        catalog, catalog_snapshot="catalog-acceptance", catalog_sha256="1" * 64,
        benchmark_sources=["artificial-analysis", "lmarena"],
        aa_index_version=4.3, lmarena_target="agent/latest",
        generator=_BATTERY_GENERATOR,
        now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    for profile in ("free-review", "free-research", "free-general"):
        provenance = document.profiles[profile].provenance
        assert provenance.catalog_snapshot == "catalog-acceptance"
        assert provenance.aa_index_version == 4.3
        assert provenance.lmarena_target == "agent/latest"
        assert "artificial-analysis" in provenance.sources
    publish_rankings(document, config, generator=_BATTERY_GENERATOR)
    ledger.snapshots_published += 1
    reloaded = load_rankings(config / "rankings.json")
    assert reloaded.catalog_snapshot == "catalog-acceptance"
    ledger.snapshots_valid += 1
    return ScenarioResult("T19", "new orders carry sources and motive", True,
                          "snapshot valid with aa 4.3 + agent/latest")


def _scenario_t20(directory: Path, ledger: _Ledger, catalog: CatalogDocument) -> ScenarioResult:
    merged = router_external.merge_gateway_provider(
        {"provider": {"other": {"npm": "x"}}},
        router_external.build_opencode_config("classic"))
    result = router_external.apply_security_rollback(
        merged, directory, "upstream advisory acceptance-t20", alerts_dir=directory)
    assert result.native_config["provider"] == {"other": {"npm": "x"}}
    assert router_external.gateway_freeze(directory) == result.freeze
    try:
        router_external.build_gateway_plan(catalog, battery_rankings(), freeze_dir=directory)
    except router_external.GatewayFrozenError:
        pass
    else:
        raise AssertionError("generation must refuse while frozen")
    alerts = router_alerts.list_alerts(directory, kind="gateway_security")
    assert len(alerts) == 1 and "advisory" in alerts[0].detail, alerts
    return ScenarioResult("T20", "security alert freezes and rolls back", True,
                          "freeze holds; generation refuses; alert recorded")


def _replay_recorded_errors(fixtures: Path) -> list[str]:
    """Current classifiers must reproduce the recorded live verdicts."""
    notes: list[str] = []
    auth = json.loads((fixtures / "sources-401-groq.json").read_text(encoding="utf-8"))
    classified = classify_http(int(auth["http_status"]), detail="synthetic invalid credential")
    assert classified.error_class == auth["kind"], (classified, auth)
    notes.append(f"sources-401-groq replays as {auth['kind']}")
    missing = json.loads(
        (fixtures / "availability-not-found-openrouter.json").read_text(encoding="utf-8"))
    text_verdict = classify_text("not a valid model ID")
    assert text_verdict.error_class == missing["kind"], (text_verdict, missing)
    notes.append(f"availability-not-found-openrouter replays as {missing['kind']}")
    session = json.loads((fixtures / "research-session-live.json").read_text(encoding="utf-8"))
    assert session["turns"] == 5 and session["errors"][0]["class"] == "rate_limited"
    notes.append("research-session-live replays 5 turns with one rotation")
    return notes


_TITLES = {
    "T01": "429 before response",
    "T02": "provider quota blocks one provider",
    "T03": "capacity gates before failover",
    "T04": "family race needs an exception",
    "T05": "same base is a backup, not an opinion",
    "T06": "only the failed reviewer restarts",
    "T07": "no two families, no fake quorum",
    "T08": "round 2 reviews the corrected state",
    "T09": "corrections stay bounded and audited",
    "T10": "number one down follows live priorities",
    "T11": "free status lost means unusable",
    "T12": "failed refresh keeps the round intact",
    "T13": "cut stream never reruns the tool",
    "T14": "absent gateway falls back to native",
    "T15": "injected secrets never persist",
    "T16": "broken tools leave the profile",
    "T17": "two valid reviews need distinct families",
    "T18": "restarts reuse verified reports only",
    "T19": "new orders carry sources and motive",
    "T20": "security alert freezes and rolls back",
}

_SCENARIOS: tuple[tuple[str, Callable[..., ScenarioResult]], ...] = (
    ("T01", lambda d, l, c, f: _scenario_t01(d, l, c)),
    ("T02", lambda d, l, c, f: _scenario_t02(d, l, c)),
    ("T03", lambda d, l, c, f: _scenario_t03(d, l, c)),
    ("T04", lambda d, l, c, f: _scenario_t04(d, l, c)),
    ("T05", lambda d, l, c, f: _scenario_t05(d, l, c)),
    ("T06", lambda d, l, c, f: _scenario_t06(d, l, c)),
    ("T07", lambda d, l, c, f: _scenario_t07(d, l, c)),
    ("T08", lambda d, l, c, f: _scenario_t08(d, l, c)),
    ("T09", lambda d, l, c, f: _scenario_t09(d, l, c)),
    ("T10", lambda d, l, c, f: _scenario_t10(d, l, c)),
    ("T11", _scenario_t11),
    ("T12", lambda d, l, c, f: _scenario_t12(d, l, c)),
    ("T13", lambda d, l, c, f: _scenario_t13(d, l, c)),
    ("T14", lambda d, l, c, f: _scenario_t14(d, l, c)),
    ("T15", lambda d, l, c, f: _scenario_t15(d, l, c)),
    ("T16", lambda d, l, c, f: _scenario_t16(d, l, c)),
    ("T17", lambda d, l, c, f: _scenario_t17(d, l, c)),
    ("T18", lambda d, l, c, f: _scenario_t18(d, l, c)),
    ("T19", lambda d, l, c, f: _scenario_t19(d, l, c)),
    ("T20", lambda d, l, c, f: _scenario_t20(d, l, c)),
)


def _metrics(ledger: _Ledger, catalog: CatalogDocument) -> AcceptanceMetrics:
    verified = {route.deployment_id
                for entry in catalog.entries for route in entry.deployments
                if route.free_verified}
    paid = sum(1 for deployment in ledger.served if deployment not in verified)
    missing = sum(1 for effective in ledger.receipts_effective if not effective)
    collisions = sum(1 for families in ledger.round_families
                     if len(set(families)) != len(families))
    return AcceptanceMetrics(
        paid_invocations=paid,
        receipts_without_effective=missing,
        family_collisions=collisions,
        snapshots_published=ledger.snapshots_published,
        snapshots_valid=ledger.snapshots_valid,
        quota_errors=ledger.quota_errors,
        quota_classified=ledger.quota_classified,
        pre_response_attempts=ledger.pre_response_attempts,
        pre_response_recovered=ledger.pre_response_recovered,
        tool_repeats_without_check=ledger.tool_repeats_without_check)


def run_battery(workdir: Path, *, fixtures_dir: Path | None = None) -> BatteryReport:
    """Run every T-scenario with a fresh store each; failures are reported."""
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    fixtures = Path(fixtures_dir) if fixtures_dir is not None else _FIXTURES_DIR
    catalog = battery_catalog()
    ledger = _Ledger()
    results: list[ScenarioResult] = []
    for scenario_id, func in _SCENARIOS:
        target = root / scenario_id.lower()
        target.mkdir(parents=True, exist_ok=True)
        try:
            result = func(target, ledger, catalog, fixtures)
        except Exception as exc:  # noqa: BLE001 - the battery reports, never hides
            result = ScenarioResult(scenario_id, _TITLES[scenario_id], False,
                                    f"{type(exc).__name__}: {exc}")
        results.append(result)
    try:
        replay = tuple(_replay_recorded_errors(fixtures))
    except Exception as exc:  # noqa: BLE001 - replay failures join the report
        replay = (f"replay FAILED: {type(exc).__name__}: {exc}",)
    return BatteryReport(scenarios=tuple(results), metrics=_metrics(ledger, catalog),
                         replay_notes=replay)


@dataclass(frozen=True)
class CostAudit:
    """Zero-cost audit over one window: registry plus optional receipts."""

    since_ts: float
    errors_total: int
    by_class: dict[str, int]
    by_provider: dict[str, int]
    billing_rows: int
    receipts_checked: int
    receipts_missing_model: int
    receipts_invalid: int

    @property
    def clean(self) -> bool:
        """No billing evidence and no incomplete accepted receipt."""
        return (self.billing_rows == 0 and self.receipts_missing_model == 0
                and self.receipts_invalid == 0)


def cost_audit(store: AvailabilityStore, since_ts: float,
               receipts_dirs: tuple[Path, ...] | list[Path] = ()) -> CostAudit:
    """Audit one window; billing rows or incomplete receipts fail the audit."""
    rows = store.errors_since(since_ts)
    by_class: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for row in rows:
        by_class[row.error_class] = by_class.get(row.error_class, 0) + 1
        by_provider[row.provider] = by_provider.get(row.provider, 0) + 1
    checked = missing = invalid = 0
    for receipts_dir in receipts_dirs:
        try:
            candidates = sorted(Path(receipts_dir).glob("*.json"))
        except OSError:
            continue
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                receipt = review_mod.ReviewReceipt.from_dict(payload)
            except (OSError, ValueError, KeyError, TypeError):
                invalid += 1
                continue
            checked += 1
            if not receipt.effective_model or not receipt.deployment_id \
                    or not receipt.sha256:
                missing += 1
    return CostAudit(
        since_ts=since_ts, errors_total=len(rows), by_class=by_class,
        by_provider=by_provider,
        billing_rows=by_class.get("billing", 0),
        receipts_checked=checked, receipts_missing_model=missing,
        receipts_invalid=invalid)


@dataclass(frozen=True)
class SessionRow:
    """One research checkpoint as seen by ``sessions --active``."""

    research_id: str
    session_id: str
    turn: int
    effective_deployment: str
    updated_at: str
    status: str  # active | stale | corrupt


def active_sessions(checkpoints_dir: Path, *, max_age_hours: float = 24.0,
                    now: float | None = None) -> list[SessionRow]:
    """Research sessions by freshness; corrupt checkpoints stay visible.

    ``active`` means a valid checkpoint updated within the window;
    older valid ones read ``stale``; unparseable files read
    ``corrupt`` instead of vanishing silently.
    """
    current = time.time() if now is None else now
    try:
        candidates = sorted(Path(checkpoints_dir).glob("*.json"))
    except OSError:
        return []
    rows: list[SessionRow] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved = research_mod.ResearchCheckpoint.from_dict(payload)
        except (OSError, ValueError, KeyError, TypeError):
            rows.append(SessionRow(
                research_id=path.stem, session_id="", turn=-1,
                effective_deployment="", updated_at="", status="corrupt"))
            continue
        try:
            moment = datetime.fromisoformat(saved.updated_at)
        except ValueError:
            moment = None
        if moment is None:
            status = "stale"
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            now_moment = datetime.fromtimestamp(current, timezone.utc)
            age_hours = (now_moment - moment).total_seconds() / 3600.0
            status = "active" if 0 <= age_hours <= max_age_hours else "stale"
        rows.append(SessionRow(
            research_id=saved.research_id, session_id=saved.session_id, turn=saved.turn,
            effective_deployment=saved.effective_deployment,
            updated_at=saved.updated_at, status=status))
    return rows


def _fixture_digest(fixtures: Path) -> str:
    digest = hashlib.sha256()
    try:
        names = sorted(path.name for path in fixtures.glob("*.json"))
    except OSError:
        return "missing"
    for name in names:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((fixtures / name).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:16]


def render_report(report: BatteryReport, *, fixtures_dir: Path | None = None) -> str:
    """Deterministic markdown for the committed acceptance report."""
    fixtures = Path(fixtures_dir) if fixtures_dir is not None else _FIXTURES_DIR
    metrics = report.metrics
    lines = [
        "# Router acceptance T01–T20 (TASK-2026-00297)",
        "",
        "Battery over simulated providers, injected faults and the live",
        "fixtures the epic recorded. No network leaves loopback; no model",
        "is called. Regenerate with the battery:",
        "",
        "```",
        "PYTHONPATH=openceo-core ./.venv/bin/python -c \"from pathlib import Path; "
        "from model_routing.acceptance import "
        "ACCEPTANCE_REPORT_PATH, generate_report_text; "
        "import tempfile; "
        "text = generate_report_text(Path(tempfile.mkdtemp())); "
        "ACCEPTANCE_REPORT_PATH.write_text(text)\"",
        "```",
        "",
        "## Scenarios",
        "",
        "| ID | Expected | Observed | Result |",
        "|---|---|---|---|",
    ]
    expected = {
        "T01": "429 skips to the next eligible deployment, no paid model",
        "T02": "global quota blocks one provider, others continue",
        "T03": "failover only past TPM/context/privacy gates",
        "T04": "no shared family without a recorded exception",
        "T05": "same base model counts once",
        "T06": "only the failed reviewer restarts",
        "T07": "block or explicit quorum, never faked independence",
        "T08": "round 2 reviews the corrected state",
        "T09": "final verification before next",
        "T10": "fallback follows live priorities",
        "T11": "unfree or unknown-price models leave the usable set",
        "T12": "failed refresh keeps the previous snapshot and the round",
        "T13": "no duplicated tool call, no false success",
        "T14": "clear error, native mechanisms still usable",
        "T15": "content logging off, secrets never persist",
        "T16": "broken tool-calling leaves MCP/review profiles",
        "T17": "two valid reviews continue on distinct families",
        "T18": "verified reports survive restarts byte-identical",
        "T19": "sources, date, snapshot and motive recorded",
        "T20": "freeze new deployments, roll back to native",
    }
    for item in report.scenarios:
        mark = "pass" if item.passed else "FAIL"
        lines.append(f"| {item.id} | {expected[item.id]} | {item.evidence} | {mark} |")
    lines += [
        "",
        "## SPEC §16 criteria",
        "",
        "| Criterion | Observed | Threshold | Result |",
        "|---|---|---|---|",
        f"| paid invocations | {metrics.paid_invocations} | 0 "
        f"| {'pass' if not metrics.paid_invocations else 'FAIL'} |",
        f"| receipts without effective model | {metrics.receipts_without_effective} "
        f"| 0 | {'pass' if not metrics.receipts_without_effective else 'FAIL'} |",
        f"| family collisions in strict rounds | {metrics.family_collisions} "
        f"| 0 | {'pass' if not metrics.family_collisions else 'FAIL'} |",
        f"| valid published snapshots | {metrics.snapshots_valid}/"
        f"{metrics.snapshots_published} | 100% "
        f"| {'pass' if metrics.snapshots_valid_rate >= 1.0 else 'FAIL'} |",
        f"| quota errors classified | {metrics.quota_classified}/"
        f"{metrics.quota_errors} | 100% "
        f"| {'pass' if metrics.quota_classification_rate >= 1.0 else 'FAIL'} |",
        f"| pre-response recovery with a healthy alternative | "
        f"{metrics.pre_response_recovered}/{metrics.pre_response_attempts} "
        f"({metrics.pre_response_recovery_rate:.0%}) | ≥95% "
        f"| {'pass' if metrics.pre_response_recovery_rate >= 0.95 else 'FAIL'} |",
        f"| tool repeats without a check | {metrics.tool_repeats_without_check} "
        f"| 0 | {'pass' if not metrics.tool_repeats_without_check else 'FAIL'} |",
        "",
        "## Replay of recorded live responses",
        "",
    ]
    for note in report.replay_notes:
        lines.append(f"- {note}")
    lines += [
        "",
        "## Real provider calls spent by the epic (2026-10-23)",
        "",
        "Every call free, minimal and synthetic; generation only where the",
        "ticket acceptance required it. Sources: the builder comments on",
        "each ticket.",
        "",
        "| Ticket | Calls | Notes |",
        "|---|---|---|",
    ]
    for ticket, count, note in REAL_CALLS_BY_TICKET:
        lines.append(f"| {ticket} | {count} | {note} |")
    lines += [
        f"| **total** | **{total_real_calls()}** | |",
        "",
        "## Provenance",
        "",
        f"- catalog schema {CATALOG_SCHEMA_VERSION}, "
        f"rankings schema {RANKINGS_SCHEMA_VERSION}",
        f"- recorded fixtures digest {_fixture_digest(fixtures)}",
        f"- battery {'GREEN' if report.passed else 'RED'}",
        "",
    ]
    return "\n".join(lines)


def generate_report_text(workdir: Path, *,
                         fixtures_dir: Path | None = None) -> str:
    """Run the battery once and render the deterministic report."""
    report = run_battery(workdir, fixtures_dir=fixtures_dir)
    return render_report(report, fixtures_dir=fixtures_dir)
