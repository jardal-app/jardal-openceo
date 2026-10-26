"""F6 acceptance battery T01–T20, §16 metrics and F6 operations (TASK-2026-00297).

The battery drives the real router paths (store, classifiers,
loopback fault proxy, rankings, review/research wiring, refresh,
external) with simulated providers, injected faults and the live
fixtures the epic already recorded. Nothing leaves loopback and no
model is called; the single live check of this ticket (OpenRouter
credit usage unchanged) lives in
``tests/test_model_routing_acceptance_live.py``.

Addendum v1.2 §11.1–§11.12 runs alongside as scenarios A01–A12
(TASK-2026-00309): native_first selection, group B reserve,
managed research sessions and the TUI launcher over a simulated
OpenCode server, plus 9 recorded verdicts (00289/00291/00294 reused,
00304–00308 added). No new live calls; the credit-unchanged check
covers the extended battery too.

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
from types import SimpleNamespace
from typing import Any, Callable

from model_routing import alerts as router_alerts
from model_routing import external as router_external
from model_routing import refresh as router_refresh
from model_routing import research_wiring as research_mod
from model_routing import review_wiring as review_mod
from model_routing import selector as router_selector
from model_routing import tui as router_tui
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
#: generation). v1.1 rows are dated 2026-10-23; the v1.2 rows below
#: are dated 2026-10-24/25. Local CLI listings (instance catalog,
#: config checks) count no provider call; the 00306 short $0 chats
#: that never moved the credit counter are excluded and noted in
#: the ticket comment.
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
    ("TASK-2026-00304", 3, "1 native probe + 2x 403 pin, no quota spent"),
    ("TASK-2026-00305", 3, "1 external chat + 2 key reads, credits unchanged"),
    ("TASK-2026-00306", 7, "1 review + 4 diagnosis reviews + 2 key reads"),
    ("TASK-2026-00307", 3, "3 managed turns with one injected 429"),
    ("TASK-2026-00308", 3, "1 unplanned + 1 quota-hit + 1 rerun turn"),
    ("TASK-2026-00309", 2, "key before/after the battery, no generation"),
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


# Addendum v1.2 battery (§11.1–§11.12, TASK-2026-00309) ---------------------------


#: Fake group B credential values for the §11 scenarios; names are
#: real, values never leave this module and are asserted absent from
#: every audit trail the battery reads back.
_ADDENDUM_FAKE_SECRETS = {
    "FREE_ROUTER_OPENROUTER_API_KEY": "TEST-ONLY-ADDENDUM-OPENROUTER",
    "FREE_ROUTER_GROQ_API_KEY": "TEST-ONLY-ADDENDUM-GROQ",
}

#: Synthetic prompt text that must never land in an audit trail (A12).
_ADDENDUM_CANARY = "addendum-canary-prompt-7f3a9c"


def _addendum_deployment(deployment_id: str, provider: str, route: str,
                         provider_id: str, model_id: str, *,
                         context: int = 131072, tools: bool = True,
                         approved: tuple[str, ...] = ("public",)) -> dict:
    allowed = ("opencode_only" if route == "opencode_native_free"
               else "configured_opencode_or_gateway")
    return {
        "deployment_id": deployment_id, "provider": provider,
        "route_type": route, "allowed_client": allowed,
        "provider_id": provider_id, "model_id": model_id,
        "gateway_model_id": model_id,
        "free_offered": True, "free_verified": True, "healthy": True,
        "input_price_usd": 0.0, "output_price_usd": 0.0,
        "approved_confidentiality": list(approved),
        "source": "addendum-battery",
        "_context": context, "_tools": tools,
    }


def _addendum_entry(family: str, canonical: str, deployment: dict) -> dict:
    context = deployment.pop("_context")
    tools = deployment.pop("_tools")
    return {
        "family_id": family, "canonical_model_id": canonical,
        "display_name": canonical,
        "capabilities": {"tools": tools, "streaming": False,
                         "context_tokens": context},
        "deployments": [deployment],
    }


def _addendum_catalog() -> CatalogDocument:
    """Two native plus two external routes, all profile-eligible."""
    entries = [
        _addendum_entry("alpha", "org/alpha", _addendum_deployment(
            "opencode/alpha-free", "opencode", "opencode_native_free",
            "opencode", "alpha-free")),
        _addendum_entry("beta", "org/beta", _addendum_deployment(
            "opencode/beta-free", "opencode", "opencode_native_free",
            "opencode", "beta-free")),
        _addendum_entry("gamma", "acme/gamma", _addendum_deployment(
            "openrouter/acme/gamma:free", "openrouter", "external_free",
            "openrouter", "acme/gamma:free")),
        _addendum_entry("delta", "acme/delta", _addendum_deployment(
            "openrouter/acme/delta:free", "openrouter", "external_free",
            "openrouter", "acme/delta:free")),
    ]
    return CatalogDocument.model_validate({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR, "entries": entries})


def _addendum_order() -> list[dict]:
    return [
        _ranked(1, "org/alpha", "alpha", "opencode/alpha-free", 0.90),
        _ranked(2, "org/beta", "beta", "opencode/beta-free", 0.88),
        _ranked(3, "acme/gamma", "gamma",
                "openrouter/acme/gamma:free", 0.85),
        _ranked(4, "acme/delta", "delta",
                "openrouter/acme/delta:free", 0.83),
    ]


def _addendum_rankings() -> RankingsDocument:
    """Same native-first order for every profile the battery drives."""
    profiles = {}
    for profile in ("free-review", "free-research", "free-general"):
        profiles[profile] = {
            "profile": profile, "order": _addendum_order(),
            "experimental": [], "pending_top3": None,
            "provenance": {
                "catalog_snapshot": "catalog-addendum",
                "catalog_sha256": "0" * 64,
            },
        }
    return RankingsDocument.model_validate({
        "schema_version": 1,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR,
        "catalog_snapshot": "catalog-addendum",
        "catalog_sha256": "0" * 64,
        "profiles": profiles,
    })


def _addendum_review_round(directory: Path, store: AvailabilityStore,
                           run_id: str) -> review_mod.RoundRouting:
    """Live review round over the addendum order, both labels held."""
    catalog = _addendum_catalog()
    rankings = _addendum_rankings()
    snapshot = review_mod.RoundSnapshot(
        document=rankings, profile=rankings.profiles["free-review"],
        file_sha256="sha256:addendum-battery",
        catalog_snapshot="catalog-addendum",
        generated_at="2026-10-25T00:00:00+00:00")
    targets = review_mod.native_review_targets(
        snapshot, catalog, store,
        credentials=dict(_ADDENDUM_FAKE_SECRETS), run_id=run_id)
    first, second = review_mod.pick_pair(targets, store)
    reserve_model(store, run_id, "1", "free-review-1",
                  first.canonical_model_id, first.family_id,
                  first.deployment_id)
    reserve_model(store, run_id, "1", "free-review-2",
                  second.canonical_model_id, second.family_id,
                  second.deployment_id)
    states = {
        label: review_mod.LabelState(requested=candidate,
                                     effective=candidate)
        for label, candidate in (("free-review-1", first),
                                 ("free-review-2", second))}
    return review_mod.RoundRouting(
        run_id=run_id, round="1", snapshot=snapshot, store=store,
        candidates=list(targets), labels=("free-review-1", "free-review-2"),
        states=states,
        round_families=frozenset({first.family_id, second.family_id}),
        receipts_dir=directory / "receipts")


def _addendum_research_pair(store: AvailabilityStore, run_id: str):
    """Ordered research targets plus the catalog they were picked from."""
    catalog = _addendum_catalog()
    rankings = _addendum_rankings()
    snapshot = research_mod.SessionSnapshot(
        document=rankings, profile=rankings.profiles["free-research"],
        file_sha256="sha256:addendum-battery",
        catalog_snapshot="catalog-addendum",
        generated_at="2026-10-25T00:00:00+00:00")
    targets = research_mod.native_research_targets(
        snapshot, catalog, store,
        credentials=dict(_ADDENDUM_FAKE_SECRETS), run_id=run_id)
    return targets, catalog


def _addendum_managed(directory: Path, store: AvailabilityStore,
                      api: "_NativeApi", targets, catalog: CatalogDocument,
                      research_id: str) -> research_mod.ManagedResearchSession:
    project = directory / "proj"
    project.mkdir(exist_ok=True)
    return research_mod.ManagedResearchSession(
        research_id=research_id, candidates=list(targets), store=store,
        session_client=research_mod.SessionClient(runner=api),
        project_dir=project, catalog=catalog,
        sleeper=lambda _seconds: None, checkpoints=directory / "ckpt")


class _NativeApi:
    """In-memory OpenCode session server for the §11 battery.

    Speaks the switchModel contract the managed-session and TUI
    tests verified: create, switch, read back, message history and
    delete, standalone only. Mirrors their FakeApi doubles.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sessions: dict[str, dict] = {}
        self.counter = 0

    def __call__(self, argv: list[str], cwd: str, timeout: float):
        self.calls.append(list(argv))
        assert argv[1:3] == ["api", "--standalone"], argv
        assert "--server" not in argv, argv
        method, path = argv[3], argv[4]
        body = json.loads(argv[argv.index("-d") + 1]) if "-d" in argv else None
        if method == "POST" and path == "/api/session":
            return self._created(argv)
        if path.startswith("/api/session/") and path.endswith("/model"):
            return self._switched(argv, path, body)
        if path.startswith("/api/session/") and path.endswith("/message"):
            session_id = path.removeprefix("/api/session/").removesuffix(
                "/message")
            return self._messaged(argv, session_id)
        if path.startswith("/api/session/"):
            session_id = path.removeprefix("/api/session/")
            if method == "GET":
                return self._got(argv, session_id)
            if method == "DELETE":
                return self._deleted(argv, session_id)
        raise AssertionError(f"unexpected call: {argv}")

    def inject_messages(self, session_id: str, messages: list[dict]) -> None:
        """Newest-first history the observer reads back (A09)."""
        self.sessions[session_id]["messages"] = list(messages)

    def _created(self, argv):
        self.counter += 1
        session_id = f"ses_fake{self.counter:04d}"
        self.sessions[session_id] = {"model": None, "messages": []}
        return research_mod.ApiRun(
            args=tuple(argv), returncode=0, stderr="",
            stdout=json.dumps({"data": {"id": session_id, "cost": 0,
                                        "tokens": {"input": 0, "output": 0,
                                                   "reasoning": 0}}}))

    def _switched(self, argv, path, body):
        session_id = path.removeprefix("/api/session/").removesuffix("/model")
        model = (body or {}).get("model") or {}
        if not model.get("providerID") or not model.get("id"):
            return self._failed(argv, 400, "InvalidRequestError",
                                "Missing key")
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        self.sessions[session_id]["model"] = {
            "providerID": model["providerID"], "id": model["id"]}
        self.sessions[session_id]["messages"].insert(0, _addendum_message(
            f"msg_switch{len(self.calls):04d}", "model-switched",
            model={"providerID": model["providerID"],
                   "id": model["id"]}))
        return research_mod.ApiRun(args=tuple(argv), returncode=0,
                                   stdout="", stderr="")

    def _got(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        model = self.sessions[session_id]["model"]
        data: dict = {"id": session_id, "cost": 0,
                      "tokens": {"input": 3, "output": 7, "reasoning": 0,
                                 "cache": {"read": 0, "write": 0}}}
        if model is not None:
            data["model"] = dict(model)
        return research_mod.ApiRun(args=tuple(argv), returncode=0, stderr="",
                                   stdout=json.dumps({"data": data}))

    def _messaged(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        return research_mod.ApiRun(
            args=tuple(argv), returncode=0, stderr="",
            stdout=json.dumps(
                {"data": list(self.sessions[session_id]["messages"])}))

    def _deleted(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        del self.sessions[session_id]
        return research_mod.ApiRun(args=tuple(argv), returncode=0,
                                   stdout="", stderr="")

    @staticmethod
    def _failed(argv, status, tag, message):
        reason = {400: "Bad Request", 404: "Not Found"}.get(status, "Error")
        return research_mod.ApiRun(
            args=tuple(argv), returncode=1,
            stdout=json.dumps({"_tag": tag, "message": message}),
            stderr=f"HTTP {status} {reason}\n")


def _addendum_message(message_id: str, msg_type: str, *, outcome: str = "",
                      model: dict | None = None, text: str = "") -> dict:
    payload: dict = {"id": message_id, "time": {"created": 1792865860299},
                     "type": msg_type}
    if outcome:
        payload["outcome"] = outcome
    if model is not None:
        payload["model"] = model
    if text:
        payload["text"] = text
    return payload


def _gateway_served_models(plan: router_external.GatewayPlan) -> list[str]:
    """LiteLLM model strings the gateway would serve (A01)."""
    return [str(entry["litellm_params"]["model"])
            for entry in plan.model_list]


def _scenario_a01(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    plan = router_external.build_gateway_plan(catalog, battery_rankings())
    served = _gateway_served_models(plan)
    assert served, "the gateway plan must serve the ranked externals"
    assert "opencode/" not in json.dumps(plan.model_list), plan.model_list
    native = [model for model in served if "opencode/" in model]
    assert not native, native
    assert plan.native_routes, "native routes stay listed for the CLI"
    listed = {route.deployment_id for route in plan.native_routes}
    set_aside = {skipped.deployment_id for skipped in plan.skipped
                 if "native-only" in skipped.reason}
    assert set_aside and set_aside <= listed, (set_aside, listed)
    blocked = classify_http(403, detail="synthetic external call",
                            provider="opencode", client="external")
    assert blocked.error_class == "route_not_authorized", blocked
    assert blocked.retryable is False
    assert blocked.action == "block_route"
    bare = classify_http(403, detail="synthetic bare call")
    assert bare.error_class == "forbidden", bare
    return ScenarioResult(
        "A01", "forbidden route stays out of the gateway", True,
        f"gateway serves {len(served)} external rows, 0 native; "
        "403→route_not_authorized no-retry")


def _scenario_a02(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    selection = router_selector.select_targets(
        "free-research", _addendum_catalog(), _addendum_rankings())
    first = selection.targets[0]
    assert (first.group, first.deployment_id) == (
        "A", "opencode/alpha-free"), first
    ledger.served.append(first.deployment_id)
    return ScenarioResult(
        "A02", "research opens on the best native model", True,
        f"free-research serves {first.deployment_id} (group A rank 1)")


def _scenario_a03(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        catalog = _addendum_catalog()
        rankings = _addendum_rankings()
        first = router_selector.select_targets(
            "free-research", catalog, rankings).targets[0]
        classified = classify_http(429, retry_after="30",
                                   detail="injected 429 before any response")
        assert classified.error_class == "rate_limited", classified
        ledger.quota_errors += 1
        ledger.quota_classified += 1
        record_provider_error(
            store, provider="opencode",
            deployment_id=first.deployment_id, model=first.deployment_id,
            classified=classified, run_id="acceptance-a03")
        ledger.pre_response_attempts += 1
        served = router_selector.select_targets(
            "free-research", catalog, rankings, store=store).targets[0]
        assert served.deployment_id == "opencode/beta-free", served
        ledger.pre_response_recovered += 1
        ledger.served.append(served.deployment_id)
        return ScenarioResult(
            "A03", "spent native falls to the next native alone", True,
            f"429 on {first.deployment_id} cooled; "
            f"served {served.deployment_id}")
    finally:
        store.close()


def _scenario_a04(directory: Path, ledger: _Ledger, catalog: CatalogDocument,
                  fixtures: Path) -> ScenarioResult:
    store = _store(directory)
    try:
        catalog = _addendum_catalog()
        rankings = _addendum_rankings()
        payload = json.loads(
            (fixtures / "opencode-free-quota-429.json").read_text(
                encoding="utf-8"))
        body = payload["event"]["error"]["response"]["body"]
        classified = classify_text(body)
        assert classified.error_class == "daily_quota", classified
        ledger.quota_errors += 1
        ledger.quota_classified += 1
        record_provider_error(
            store, provider="opencode",
            deployment_id="opencode/alpha-free",
            model="opencode/alpha-free", classified=classified,
            run_id="acceptance-a04")
        assert store.provider_blocked("opencode") is not None
        ledger.pre_response_attempts += 1
        selection = router_selector.select_targets(
            "free-research", catalog, rankings, store=store)
        assert selection.targets, "group B must catch the native outage"
        assert all(item.group == "B" for item in selection.targets)
        target = selection.targets[0]
        assert target.deployment_id == "openrouter/acme/gamma:free", target
        assert target.opencode_model == "openrouter/acme/gamma:free"
        assert target.provider_id and target.model_id
        ledger.pre_response_recovered += 1
        config = router_external.build_external_providers_config(
            "v2", catalog)
        text = json.dumps(config)
        assert "{env:FREE_ROUTER_OPENROUTER_API_KEY}" in text, text
        assert "TEST-ONLY" not in text
        # The crossing joins ledger.served: _metrics counts the
        # addendum ids too, so §16 paid invocations stay global.
        ledger.served.append(target.deployment_id)
        return ScenarioResult(
            "A04", "no native left crosses to a configured external", True,
            "opencode blocked by the recorded quota; crossed to "
            f"{target.deployment_id} via its configured provider")
    finally:
        store.close()


def _scenario_a05(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    entries = [
        _addendum_entry("good", "org/good", _addendum_deployment(
            "opencode/good-free", "opencode", "opencode_native_free",
            "opencode", "good-free")),
        _addendum_entry("small", "org/small", _addendum_deployment(
            "opencode/small-free", "opencode", "opencode_native_free",
            "opencode", "small-free", context=32768)),
        _addendum_entry("notools", "org/notools", _addendum_deployment(
            "opencode/notools-free", "opencode", "opencode_native_free",
            "opencode", "notools-free", tools=False)),
        _addendum_entry("private", "acme/private", _addendum_deployment(
            "openrouter/acme/private:free", "openrouter", "external_free",
            "openrouter", "acme/private:free",
            approved=("internal",))),
    ]
    catalog = CatalogDocument.model_validate({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR, "entries": entries})
    order = [
        _ranked(1, "org/good", "good", "opencode/good-free", 0.90),
        _ranked(2, "org/small", "small", "opencode/small-free", 0.89),
        _ranked(3, "org/notools", "notools", "opencode/notools-free", 0.88),
        _ranked(4, "acme/private", "private",
                "openrouter/acme/private:free", 0.87),
    ]
    rankings = RankingsDocument.model_validate({
        "schema_version": 1,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR,
        "catalog_snapshot": "catalog-addendum",
        "catalog_sha256": "0" * 64,
        "profiles": {"free-research": {
            "profile": "free-research", "order": order, "experimental": [],
            "pending_top3": None,
            "provenance": {"catalog_snapshot": "catalog-addendum",
                           "catalog_sha256": "0" * 64}}},
    })
    selection = router_selector.select_targets(
        "free-research", catalog, rankings)
    assert [item.deployment_id for item in selection.targets] == [
        "opencode/good-free"]
    reasons = {item.deployment_id: item.reason
               for item in selection.dropped}
    assert "context" in reasons["opencode/small-free"], reasons
    assert "tools" in reasons["opencode/notools-free"], reasons
    assert "confidentiality" in reasons[
        "openrouter/acme/private:free"], reasons
    return ScenarioResult(
        "A05", "incompatible context, tools or privacy never serve", True,
        "good served; small context, no-tools and private dropped "
        "with reasons")


def _scenario_a06(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        routing = _addendum_review_round(directory, store, "acceptance-a06")
        models = [item.model_arg() for item in routing.candidates[:2]]
        assert models == ["opencode/alpha-free", "opencode/beta-free"], models
        first = review_mod.plan_fallback(
            routing, "free-review-1", outcome_kind="disabled",
            text="model not found: opencode/alpha-free")
        second = review_mod.plan_fallback(
            routing, "free-review-2", outcome_kind="disabled",
            text="model not found: opencode/beta-free")
        assert first is not None and second is not None
        assert first.family_id != second.family_id
        assert {first.family_id, second.family_id} == {"gamma", "delta"}
        try:
            reserve_model(store, "acceptance-a06", "1", "free-review-9",
                          "acme/gamma", "gamma",
                          "openrouter/acme/gamma:free")
        except ReservationConflict:
            pass
        else:
            raise AssertionError("a held family must refuse a third label")
        ledger.round_families.append((first.family_id, second.family_id))
        return ScenarioResult(
            "A06", "simultaneous failure keeps distinct families", True,
            "both labels failed together; gamma+delta serve, "
            "a third label refused the held family")
    finally:
        store.close()


def _scenario_a07(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        routing = _addendum_review_round(directory, store, "acceptance-a07")
        frozen = routing.snapshot
        kept = review_mod.record_receipt(
            routing, "free-review-1",
            report_text="# Report\n\nNo findings.\n",
            reviewed_commit="abc123",
            started_at="2026-10-25T00:00:00+00:00",
            finished_at="2026-10-25T00:01:00+00:00")
        nxt = review_mod.plan_fallback(
            routing, "free-review-2", outcome_kind="disabled",
            text="model not found: opencode/beta-free")
        assert nxt is not None and nxt.family_id == "gamma", nxt
        assert routing.snapshot is frozen
        assert routing.receipts["free-review-1"] == kept
        reloaded = review_mod.load_valid_receipt(
            directory / "receipts", run_id="acceptance-a07", round="1",
            slot="free-review-1", reviewed_commit="abc123")
        assert reloaded is not None and reloaded[0].sha256 == kept.sha256
        assert "free-review-2" not in routing.receipts
        assert routing.states["free-review-2"].fallback_count == 1
        ledger.receipts_effective.append(kept.effective_model)
        return ScenarioResult(
            "A07", "failed reviewer restarts whole on one snapshot", True,
            "label2 rotates to gamma on the same snapshot; "
            "label1 receipt sha intact")
    finally:
        store.close()


def _scenario_a08(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    api = _NativeApi()
    targets, catalog = _addendum_research_pair(store, "acceptance-a08")
    session = _addendum_managed(
        directory, store, api, targets, catalog, "acceptance-a08")
    try:
        calls: list[str] = []

        def invoke(request) -> research_mod.ProviderTurn:
            calls.append(request.model)
            if len(calls) == 1:
                return research_mod.ProviderTurn(
                    text="", complete=False,
                    error_text="429 too many requests (injected)",
                    failure_stage="before_response")
            return research_mod.ProviderTurn(
                text="beta takes over", complete=True)

        first = session.run_turn("synthetic objective", invoke,
                                 validated=["v1"])
        assert first.model_effective == "opencode/beta-free", first
        assert first.rotations == 1
        session.run_turn(
            "synthetic follow-up",
            lambda _request: research_mod.ProviderTurn(
                text="sky is blue", complete=True,
                executed_tools=(research_mod.ToolCall(
                    id="call_sky", name="read"),),
                tool_results=(("call_sky", "blue"),)),
            validated=["v2"])
        tries: list[str] = []
        reexecutions = {"call_sky": 0}

        def flaky(request) -> research_mod.ProviderTurn:
            tries.append(request.model)
            if "call_sky" not in {item.id for item in request.applied_tools}:
                reexecutions["call_sky"] += 1
            if len(tries) == 1:
                return research_mod.ProviderTurn(
                    text="partial ", complete=False,
                    error_text="provider exploded mid-stream",
                    failure_stage="partial_text")
            return research_mod.ProviderTurn(
                text="the answer", complete=True,
                executed_tools=(research_mod.ToolCall(
                    id="call_sky", name="read"),),
                tool_results=(("call_sky", "blue"),))

        third = session.run_turn("synthetic resume", flaky,
                                 validated=["v3"])
        assert third.turn == 3 and third.resumed is True, third
        assert tries == ["opencode/beta-free"] * 2, tries
        assert reexecutions == {"call_sky": 0}, reexecutions
        saved = research_mod.load_checkpoint(
            directory / "ckpt", "acceptance-a08")
        assert saved is not None and saved.turn == 3, saved
        assert saved.validated_results == ("v1", "v2", "v3"), saved
        assert [tool.id for tool in saved.applied_tools] == ["call_sky"]
        switches = [detail for _ts, event, detail
                    in store.audit_since("acceptance-a08", 0.0)
                    if event == "research_model_switch"]
        assert len(switches) >= 2, switches
        session.close()
        assert api.sessions == {}
        return ScenarioResult(
            "A08", "history survives switches; tools run once", True,
            "3 turns, 1 rotation, checkpoint turn=3, "
            "cut stream re-claims call_sky without re-execution")
    finally:
        session.close()
        store.close()


def _scenario_a09(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    try:
        api = _NativeApi()
        targets, _catalog = _addendum_research_pair(
            store, "acceptance-a09")
        project = directory / "proj"
        project.mkdir(exist_ok=True)
        client = research_mod.SessionClient(runner=api)
        plan = router_tui.prepare_tui_session(
            client=client, project_dir=project, targets=targets,
            store=store, run_id="acceptance-a09")
        assert plan.target.model_arg() == "opencode/alpha-free", plan
        assert plan.argv[:2] == ("opencode", "--session"), plan
        api.inject_messages(plan.session_id, [
            _addendum_message("msg_idle_failed", "idle", outcome="failed"),
            _addendum_message("msg_user", "user", text=_ADDENDUM_CANARY),
            _addendum_message(
                "msg_switched", "model-switched",
                model={"providerID": "opencode", "id": "alpha-free"}),
        ])
        seen = router_tui.observe_tui_session(
            client=client, project_dir=project, session_id=plan.session_id)
        assert seen.failure is not None, seen
        assert seen.failure.message_id == "msg_idle_failed", seen
        assert _ADDENDUM_CANARY not in repr(seen)
        moved = router_tui.apply_next_turn_fallback(
            client=client, project_dir=project, observation=seen,
            targets=targets, store=store, run_id="acceptance-a09")
        assert moved.applied is True, moved
        assert moved.new_model == "opencode/beta-free", moved
        served = client.get_session(project, plan.session_id)
        assert served.opencode_model == "opencode/beta-free", served
        events = [event for _ts, event, _detail
                  in store.audit_since("acceptance-a09", 0.0)]
        assert "tui_session_prepared" in events, events
        assert "tui_next_turn_fallback" in events, events
        return ScenarioResult(
            "A09", "launcher failure observed and routed next turn", True,
            "launcher session failure observed; next turn routed "
            "alpha→beta")
    finally:
        store.close()


def _scenario_a10(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    paid = _addendum_entry("paid", "org/paid", {
        "deployment_id": "openrouter/org/paid", "provider": "openrouter",
        "route_type": "external_free",
        "allowed_client": "configured_opencode_or_gateway",
        "provider_id": "openrouter", "model_id": "org/paid",
        "gateway_model_id": "org/paid",
        "free_offered": True, "free_verified": True, "healthy": True,
        "input_price_usd": 0.01, "output_price_usd": 0.01,
        "approved_confidentiality": ["public"],
        "source": "addendum-battery", "_context": 32768, "_tools": True})
    unverified = _addendum_entry("unver", "org/unver", {
        "deployment_id": "openrouter/org/unver", "provider": "openrouter",
        "route_type": "external_free",
        "allowed_client": "configured_opencode_or_gateway",
        "provider_id": "openrouter", "model_id": "org/unver",
        "gateway_model_id": "org/unver",
        "free_offered": True, "free_verified": False, "healthy": True,
        "input_price_usd": 0.0, "output_price_usd": 0.0,
        "approved_confidentiality": ["public"],
        "source": "addendum-battery", "_context": 32768, "_tools": True})
    good = _addendum_entry("good", "org/good", _addendum_deployment(
        "opencode/good-free", "opencode", "opencode_native_free",
        "opencode", "good-free", context=32768))
    extended = CatalogDocument.model_validate({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR,
        "entries": [paid, unverified, good]})
    order = [
        _ranked(1, "org/paid", "paid", "openrouter/org/paid", 0.95),
        _ranked(2, "org/unver", "unver", "openrouter/org/unver", 0.94),
        _ranked(3, "org/good", "good", "opencode/good-free", 0.80),
    ]
    rankings = RankingsDocument.model_validate({
        "schema_version": 1,
        "generated_at": "2026-10-25T00:00:00+00:00",
        "generator": _BATTERY_GENERATOR,
        "catalog_snapshot": "catalog-acceptance",
        "catalog_sha256": "0" * 64,
        "profiles": {"free-general": {
            "profile": "free-general", "order": order, "experimental": [],
            "pending_top3": None,
            "provenance": {"catalog_snapshot": "catalog-acceptance",
                           "catalog_sha256": "0" * 64}}},
    })
    selection = router_selector.select_targets(
        "free-general", extended, rankings)
    assert [item.deployment_id for item in selection.targets] == [
        "opencode/good-free"]
    reasons = " ".join(item.reason for item in selection.dropped)
    assert "zero-cost" in reasons, reasons
    usable = {route.deployment_id
              for entry in extended.entries for route in entry.deployments
              if is_usable(route, RouterPolicy())}
    assert all(item.deployment_id in usable
               for item in selection.targets)
    return ScenarioResult(
        "A10", "paid or unverified routes never serve", True,
        "paid + unverified dropped; served opencode/good-free")


def _scenario_a11(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    review_off = review_mod.free_router_settings(
        {"free_router": {"enabled": False}})
    assert review_off["enabled"] is False
    assert review_mod.free_router_settings({})["enabled"] is True
    research_off = research_mod.research_router_settings(
        SimpleNamespace(RESEARCH_FREE_ROUTER_ENABLED=False))
    assert research_off["enabled"] is False
    assembly = review_mod.assemble_review_pool(
        [{"label": "manual", "provider": "opencode",
          "model": "opencode/alpha-free", "family": "alpha",
          "active": True, "command": ["opencode", "run", "-m",
                                      "opencode/alpha-free"]}],
        review_cfg={"free_router": {"enabled": False}},
        run_id="acceptance-a11", round="1", builder_family="addendum",
        receipts_dir=directory / "receipts")
    assert assembly.routing is None, assembly
    assert [entry["label"] for entry in assembly.raw_pool] == ["manual"]
    research_static = research_mod.assemble_research_model(
        SimpleNamespace(RESEARCH_FREE_ROUTER_ENABLED=False,
                        RESEARCH_OPENCODE_TEST_MODEL="opencode/alpha-free"),
        research_id="acceptance-a11")
    assert research_static.active is False, research_static
    assert research_static.model == "opencode/alpha-free", research_static
    project = directory / "project"
    (project / "ckpt").mkdir(parents=True)
    kept = {
        project / "opencode.json": b'{"providers": {"openrouter": {}}}\n',
        project / "prompt.md": b"# Synthetic prompt\n",
        project / "ckpt" / "rses.json": b'{"turn": 2}\n',
    }
    for path, content in kept.items():
        path.write_bytes(content)
    merged = router_external.merge_gateway_provider(
        {"provider": {"other": {"npm": "x"}}},
        router_external.build_opencode_config("classic"))
    stripped = router_external.strip_gateway_provider(merged)
    assert stripped.get("provider") == {"other": {"npm": "x"}}, stripped
    assert "free-gateway" not in json.dumps(stripped), stripped
    router_external.freeze_gateway(directory, "acceptance-a11")
    try:
        router_external.build_gateway_plan(
            catalog, battery_rankings(), freeze_dir=directory)
    except router_external.GatewayFrozenError:
        pass
    else:
        raise AssertionError("generation must refuse while frozen")
    assert router_external.unfreeze_gateway(directory) is True
    router_external.build_gateway_plan(
        catalog, battery_rankings(), freeze_dir=directory)
    for path, content in kept.items():
        assert path.read_bytes() == content, path
    native = router_selector.select_targets(
        "free-research", _addendum_catalog(), _addendum_rankings(),
        enabled_providers={"opencode"}).targets[0]
    assert native.deployment_id == "opencode/alpha-free", native
    return ScenarioResult(
        "A11", "router off returns to native with files intact", True,
        "router off in both pools; gateway stripped and frozen; "
        "3 files intact; native alpha servable")


def _scenario_a12(directory: Path, ledger: _Ledger,
                  catalog: CatalogDocument) -> ScenarioResult:
    store = _store(directory)
    review_store = AvailabilityStore(directory / "review.sqlite")
    api = _NativeApi()
    targets, catalog = _addendum_research_pair(store, "acceptance-a12")
    session = _addendum_managed(
        directory, store, api, targets, catalog, "acceptance-a12")
    try:
        calls: list[str] = []
        key_like = "sk-" + "z" * 12

        def invoke(request) -> research_mod.ProviderTurn:
            calls.append(request.model)
            if len(calls) == 1:
                return research_mod.ProviderTurn(
                    text="", complete=False,
                    error_text="429 too many requests (injected) "
                    f"token {key_like}",
                    failure_stage="before_response")
            return research_mod.ProviderTurn(text="done", complete=True)

        outcome = session.run_turn(_ADDENDUM_CANARY, invoke)
        assert outcome.rotations == 1, outcome
        # The review round keeps its own store: the research rotation
        # cools alpha down, and the receipt must pin the undisturbed
        # first pick.
        routing = _addendum_review_round(
            directory, review_store, "acceptance-a12r")
        receipt = review_mod.record_receipt(
            routing, "free-review-1",
            report_text="# Report\n\nNo findings.\n",
            reviewed_commit="abc123",
            started_at="2026-10-25T00:00:00+00:00",
            finished_at="2026-10-25T00:01:00+00:00")
        nxt = review_mod.plan_fallback(
            routing, "free-review-2", outcome_kind="disabled",
            text="model not found: opencode/beta-free")
        assert nxt is not None, "the audit needs a real rotation"
        details = [detail for _ts, _event, detail
                   in store.audit_since("acceptance-a12", 0.0)]
        details += [detail for _ts, _event, detail
                    in review_store.audit_since("acceptance-a12r", 0.0)]
        joined = "\n".join([
            *details, json.dumps(receipt.to_dict()),
            *[row.excerpt for row in store.errors_since(0.0)],
            *[row.excerpt for row in review_store.errors_since(0.0)],
        ])
        for needle in ("opencode/", "reason=", "result=",
                       '"family_id": "alpha"', '"provider": "opencode"'):
            assert needle in joined, joined
        assert _ADDENDUM_CANARY not in joined
        for value in _ADDENDUM_FAKE_SECRETS.values():
            assert value not in joined
        assert key_like not in joined, "injected key-like token must redact"
        leaks = scan_for_secrets(joined, ())
        key_leaks = [leak for leak in leaks if "key-like" in leak]
        assert not key_leaks, key_leaks
        session.close()
        return ScenarioResult(
            "A12", "audit keeps model data; prompts stay out", True,
            "switch+receipt audit keeps model/family/provider/"
            "reason/result; canary, secrets and key-like absent")
    finally:
        session.close()
        review_store.close()
        store.close()


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
    quota = json.loads(
        (fixtures / "opencode-free-quota-429.json").read_text(encoding="utf-8"))
    quota_verdict = classify_text(quota["event"]["error"]["response"]["body"])
    assert quota_verdict.error_class == "daily_quota", quota_verdict
    notes.append("opencode-free-quota-429 replays as daily_quota")
    instance = json.loads(
        (fixtures / "native-instance-catalog.json").read_text(encoding="utf-8"))
    assert (instance["instance_models"], instance["router_models"],
            instance["overlap"]) == (82, 85, 81), instance
    notes.append("native-instance-catalog replays 81/82 models overlapping")
    chat = json.loads(
        (fixtures / "selector-openrouter-chat.json").read_text(encoding="utf-8"))
    assert chat["credit_usage_before"] == chat["credit_usage_after"] == 0.0
    notes.append("selector-openrouter-chat replays with credits 0.0 → 0.0")
    fed = json.loads(
        (fixtures / "review-group-b-live.json").read_text(encoding="utf-8"))
    assert fed["receipt"]["effective_model"] == fed["model"], fed
    assert fed["receipt"]["route_type"] == "external_free", fed
    assert fed["credit_usage_before"] == fed["credit_usage_after"], fed
    notes.append("review-group-b-live replays an external receipt, "
                 "credits unchanged")
    managed = json.loads(
        (fixtures / "research-managed-session-live.json").read_text(
            encoding="utf-8"))
    assert managed["turns"] == 3 and managed["cost"] == 0.0, managed
    assert managed["effective_model"] == managed["session_model"], managed
    notes.append("research-managed-session-live replays 3 turns, "
                 "effective == session model, cost 0")
    launcher = json.loads(
        (fixtures / "tui-launcher-session-live.json").read_text(
            encoding="utf-8"))
    assert launcher["cost"] == 0.0 and launcher["watermark_seen"] is True
    notes.append("tui-launcher-session-live replays cost 0 with a watermark")
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
    "A01": "forbidden route stays out of the gateway",
    "A02": "research opens on the best native model",
    "A03": "spent native falls to the next native alone",
    "A04": "no native left crosses to a configured external",
    "A05": "incompatible context, tools or privacy never serve",
    "A06": "simultaneous failure keeps distinct families",
    "A07": "failed reviewer restarts whole on one snapshot",
    "A08": "history survives switches; tools run once",
    "A09": "launcher failure observed and routed next turn",
    "A10": "paid or unverified routes never serve",
    "A11": "router off returns to native with files intact",
    "A12": "audit keeps model data; prompts stay out",
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
    ("A01", lambda d, l, c, f: _scenario_a01(d, l, c)),
    ("A02", lambda d, l, c, f: _scenario_a02(d, l, c)),
    ("A03", lambda d, l, c, f: _scenario_a03(d, l, c)),
    ("A04", _scenario_a04),
    ("A05", lambda d, l, c, f: _scenario_a05(d, l, c)),
    ("A06", lambda d, l, c, f: _scenario_a06(d, l, c)),
    ("A07", lambda d, l, c, f: _scenario_a07(d, l, c)),
    ("A08", lambda d, l, c, f: _scenario_a08(d, l, c)),
    ("A09", lambda d, l, c, f: _scenario_a09(d, l, c)),
    ("A10", lambda d, l, c, f: _scenario_a10(d, l, c)),
    ("A11", lambda d, l, c, f: _scenario_a11(d, l, c)),
    ("A12", lambda d, l, c, f: _scenario_a12(d, l, c)),
)


def _metrics(ledger: _Ledger, catalog: CatalogDocument) -> AcceptanceMetrics:
    verified = {route.deployment_id
                for entry in catalog.entries for route in entry.deployments
                if route.free_verified}
    verified |= {route.deployment_id
                 for entry in _addendum_catalog().entries
                 for route in entry.deployments
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
    """Run every T- and A-scenario with a fresh store each; failures are reported."""
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
        "# Router acceptance T01–T20 + addendum §11 (TASK-2026-00297, TASK-2026-00309)",
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
        "## Scenarios T01–T20",
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
        "A01": "no native route in the gateway; external 403 never retries",
        "A02": "free-research opens on the best native model",
        "A03": "spent native falls to the next native alone",
        "A04": "no native left crosses to a configured external",
        "A05": "incompatible context, tools or privacy never serve",
        "A06": "simultaneous failure keeps distinct families",
        "A07": "failed reviewer restarts whole on the same snapshot",
        "A08": "history survives switches; tools run once",
        "A09": "launcher failure observed and routed next turn",
        "A10": "paid or unverified routes never serve",
        "A11": "router off returns to native with files intact",
        "A12": "audit keeps model data; prompts stay out",
    }
    for item in report.scenarios:
        if item.id.startswith("A"):
            continue
        mark = "pass" if item.passed else "FAIL"
        lines.append(f"| {item.id} | {expected[item.id]} | {item.evidence} | {mark} |")
    lines += [
        "",
        "## Addendum §11 scenarios",
        "",
        "| ID | Expected | Observed | Result |",
        "|---|---|---|---|",
    ]
    for item in report.scenarios:
        if not item.id.startswith("A"):
            continue
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
        "## Real provider calls spent by the epic (2026-10-23–2026-10-25)",
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
