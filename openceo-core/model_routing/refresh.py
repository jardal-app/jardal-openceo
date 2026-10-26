"""Scheduled refresh: provider catalogs and benchmarks (TASK-2026-00292).

Cron B (6 h): fetch provider catalogs, keep the last good entries for
dead sources, resolve families, unify canonical models, apply catalog
verdicts, then publish the catalog and the rankings.
Cron A (24 h): fetch Artificial Analysis plus the LMArena snapshot,
rebuild quality, attach it to the catalog, then publish both.
Both run under a flock guard and append to updater.log; a failed
refresh leaves the previous snapshots byte-identical (T12).
"""

from __future__ import annotations

import fcntl
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from model_routing import alerts as router_alerts
from model_routing import benchmarks as router_benchmarks
from model_routing import live as router_live
from model_routing import ranking as router_ranking
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing.benchmarks import (
    AAResult,
    AliasFile,
    LMArenaResult,
    ModelQuality,
    load_aliases,
)
from model_routing.config import ConfigError, load_policy, load_providers
from model_routing.models import BenchmarkScore, CatalogDocument, RouterPolicy, is_usable
from model_routing.paths import (
    CATALOG_FILENAME,
    RANKINGS_FILENAME,
    UPDATER_LOCK_FILENAME,
    UPDATER_LOG_FILENAME,
)
from model_routing.publish import (
    PublishError,
    SnapshotRecord,
    load_catalog_doc,
    publish_catalog,
)
from model_routing.secrets import RouterSecrets, SecretsError, load_router_secrets
from model_routing.sources import SOURCE_TTLS, FetchedCatalog, GetJson, SourceError
from model_routing.terms import TermsFile, load_terms

__all__ = [
    "AA_MAX_PAGES",
    "LMARENA_ROWS_LENGTH",
    "RefreshError",
    "RefreshReport",
    "SourceStaleness",
    "apply_catalog_verdicts",
    "attach_benchmarks",
    "run_update",
    "source_staleness",
    "update_benchmarks",
    "update_providers",
]

AA_MAX_PAGES = 10
LMARENA_ROWS_LENGTH = 100


class RefreshError(RuntimeError):
    """Refresh cannot publish; the previous snapshots stay untouched."""


@dataclass(frozen=True)
class RefreshReport:
    """Outcome of one update run, safe to log (no secrets)."""

    kind: str
    catalog_snapshot: str
    rankings_snapshot: str
    stale: bool
    stale_reason: str | None
    fetched: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class SourceStaleness:
    """Freshness of one provider's catalog entries."""

    provider: str
    entries: int
    verified: int
    newest_observed_at: str | None
    ttl_hours: float | None
    fresh: bool


def apply_catalog_verdicts(document: CatalogDocument) -> CatalogDocument:
    """Catalog-layer health: presently listed plus verified free.

    Transient serving state lives in the availability store (00291)
    and is consulted at reservation time, never baked into the
    six-hour catalog; probes verify capabilities and reliability.
    """
    entries = []
    for entry in document.entries:
        deployments = [
            deployment.model_copy(update={"healthy": deployment.free_verified})
            for deployment in entry.deployments
        ]
        entries.append(entry.model_copy(update={"deployments": deployments}))
    return document.model_copy(update={"entries": entries})


def _score_moment(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def attach_benchmarks(
    catalog: CatalogDocument, qualities: list[ModelQuality]
) -> CatalogDocument:
    """Replace benchmark dimensions with one fresh quality rebuild."""
    by_canonical = {quality.canonical_model_id: quality for quality in qualities}
    entries = []
    for entry in catalog.entries:
        quality = by_canonical.get(entry.canonical_model_id)
        if quality is None:
            entries.append(entry)
            continue
        benchmarks = {
            name: BenchmarkScore(
                score=score.value, source=score.source,
                observed_at=_score_moment(score.observed_at),
            )
            for name, score in quality.dimensions.items()
        }
        entries.append(entry.model_copy(update={"benchmarks": benchmarks}))
    return catalog.model_copy(update={"entries": entries})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_policy(path: Path | None = None) -> RouterPolicy:
    try:
        return load_policy(path) if path else load_policy()
    except ConfigError:
        return RouterPolicy()


def _load_providers(config_dir: Path) -> tuple[list[str], RouterPolicy]:
    policy = _load_policy(config_dir / "policy.yaml")
    try:
        providers = load_providers(config_dir / "providers.yaml")
    except ConfigError:
        return sorted(SOURCE_TTLS), policy
    return [entry.name for entry in providers.providers if entry.enabled], policy


def _load_terms() -> TermsFile:
    return load_terms()


def _load_aliases() -> AliasFile:
    return load_aliases()


def _load_secrets() -> RouterSecrets | None:
    try:
        return load_router_secrets()
    except SecretsError:
        return None


def _provider_key(secrets: RouterSecrets | None, provider: str) -> str:
    if secrets is None:
        return ""
    return {
        "openrouter": secrets.FREE_ROUTER_OPENROUTER_API_KEY,
        "nvidia": secrets.FREE_ROUTER_NVIDIA_NIM_API_KEY,
        "groq": secrets.FREE_ROUTER_GROQ_API_KEY,
        "gemini": secrets.FREE_ROUTER_GEMINI_API_KEY,
    }.get(provider, "")


def _fetch_providers(
    providers: list[str],
    terms: TermsFile,
    secrets: RouterSecrets | None,
    get: GetJson | None,
) -> tuple[list[FetchedCatalog], dict[str, str]]:
    fetched: list[FetchedCatalog] = []
    failed: dict[str, str] = {}
    for provider in providers:
        if provider not in SOURCE_TTLS:
            failed[provider] = f"unsupported provider: {provider}"
            continue
        try:
            catalog = router_sources.refresh_source(
                provider, terms, api_key=_provider_key(secrets, provider), get=get)
        except (SourceError, router_live.LiveError) as exc:
            failed[provider] = str(exc)
            continue
        fetched.append(catalog)
    return fetched, failed


def _previous_catalog(config_dir: Path) -> CatalogDocument | None:
    catalog_file = config_dir / CATALOG_FILENAME
    if not catalog_file.is_file():
        return None
    return load_catalog_doc(catalog_file)


def _previous_rankings_provenance(config_dir: Path) -> tuple[float | None, str | None]:
    rankings_file = config_dir / RANKINGS_FILENAME
    if not rankings_file.is_file():
        return None, None
    try:
        previous = router_ranking.load_rankings(rankings_file)
    except PublishError:
        return None, None
    for ranking in previous.profiles.values():
        return ranking.provenance.aa_index_version, ranking.provenance.lmarena_target
    return None, None


def _carry_benchmarks(
    previous: CatalogDocument | None, unified: CatalogDocument
) -> CatalogDocument:
    """Keep benchmark scores for canonical models the fetch cannot score.

    Provider catalogs carry deployment metadata only; quality comes from
    Cron A. Without this carry, every 6 h refresh would empty the
    rankings (all entries back to experimental).
    """
    if previous is None:
        return unified
    known = {(entry.family_id, entry.canonical_model_id): entry.benchmarks
             for entry in previous.entries if entry.benchmarks}
    if not known:
        return unified
    entries = []
    for entry in unified.entries:
        if not entry.benchmarks:
            carried = known.get((entry.family_id, entry.canonical_model_id))
            if carried:
                entries.append(entry.model_copy(update={"benchmarks": carried}))
                continue
        entries.append(entry)
    return unified.model_copy(update={"entries": entries})


def update_providers(
    config_dir: Path,
    *,
    get: GetJson | None = None,
    secrets: RouterSecrets | None = None,
    policy: RouterPolicy | None = None,
    providers: list[str] | None = None,
    terms: TermsFile | None = None,
    aliases: AliasFile | None = None,
) -> RefreshReport:
    """Cron B: refresh provider catalogs and republish catalog + rankings."""
    config_dir = Path(config_dir)
    wanted, file_policy = _load_providers(config_dir) if providers is None else (providers, policy)
    active = policy or file_policy or RouterPolicy()
    terms = terms if terms is not None else _load_terms()
    aliases = aliases if aliases is not None else _load_aliases()
    secrets = _load_secrets() if secrets is None and get is None else secrets
    fetched, failed = _fetch_providers(wanted, terms, secrets, get)
    if not fetched:
        detail = "; ".join(f"{name}: {failed[name]}" for name in sorted(failed)) or "no providers"
        raise RefreshError(f"all provider sources failed ({detail}); previous snapshot kept")
    from model_routing.sources import merge_with_previous

    previous = _previous_catalog(config_dir)
    try:
        merged, stale, reason = merge_with_previous(previous, fetched, failed)
    except SourceError as exc:
        raise RefreshError(f"provider merge failed: {exc}") from exc
    familied = router_benchmarks.apply_families(merged, aliases)
    assert isinstance(familied, CatalogDocument)
    unified = router_ranking.unify_canonical(familied, aliases)
    scored = _carry_benchmarks(previous, unified)
    decided = apply_catalog_verdicts(scored)
    try:
        record = publish_catalog(decided, config_dir, stale=stale, stale_reason=reason)
    except PublishError as exc:
        raise RefreshError(f"catalog publication failed: {exc}") from exc
    return _republish_rankings(
        config_dir, record, active, benchmark_sources=None,
        aa_index_version=None, lmarena_target=None,
        generator="model_routing.refresh",
        kind="providers",
        fetched=tuple(catalog.provider for catalog in fetched),
        failed=tuple(f"{name}: {failed[name]}" for name in sorted(failed)),
        stale=stale, stale_reason=reason,
    )


def _republish_rankings(
    config_dir: Path,
    record: SnapshotRecord,
    policy: RouterPolicy,
    *,
    benchmark_sources: list[str] | None,
    aa_index_version: float | None,
    lmarena_target: str | None,
    generator: str,
    kind: str,
    fetched: tuple[str, ...],
    failed: tuple[str, ...],
    stale: bool,
    stale_reason: str | None,
) -> RefreshReport:
    catalog = load_catalog_doc(config_dir / CATALOG_FILENAME)
    rankings_file = config_dir / RANKINGS_FILENAME
    previous = None
    if rankings_file.is_file():
        try:
            previous = router_ranking.load_rankings(rankings_file)
        except PublishError:
            previous = None
    carried_version, carried_target = _previous_rankings_provenance(config_dir)
    aa_version = aa_index_version if aa_index_version is not None else carried_version
    arena_target = lmarena_target if lmarena_target is not None else carried_target
    if benchmark_sources is None:
        benchmark_sources = sorted({
            score.source for entry in catalog.entries
            for score in entry.benchmarks.values() if score.source
        })
    try:
        document = router_ranking.build_rankings(
            catalog, policy, previous=previous,
            catalog_snapshot=record.snapshot_id, catalog_sha256=record.sha256,
            benchmark_sources=benchmark_sources,
            aa_index_version=aa_version, lmarena_target=arena_target,
            generator=generator,
        )
        snapshot = router_ranking.publish_rankings(document, config_dir, generator=generator)
    except (router_ranking.RankingError, PublishError) as exc:
        raise RefreshError(f"rankings publication failed: {exc}") from exc
    return RefreshReport(
        kind=kind,
        catalog_snapshot=record.snapshot_id,
        rankings_snapshot=snapshot.snapshot_id,
        stale=stale,
        stale_reason=stale_reason,
        fetched=fetched,
        failed=failed,
    )


def _fetch_aa_pages(api_key: str, get: GetJson | None) -> AAResult:
    page = 1
    combined: AAResult | None = None
    while True:
        try:
            raw = router_benchmarks.fetch_aa_models(api_key, get, page=page)
        except SourceError as exc:
            if page == 1:
                raise
            raise RefreshError(f"AA page {page} failed; cohort incomplete, keeping previous") from exc
        parsed = router_benchmarks.parse_aa_models(raw)
        if combined is None:
            combined = parsed
        else:
            if parsed.index_version != combined.index_version:
                raise RefreshError("AA index version moved mid-pagination; keeping previous")
            combined.models.extend(parsed.models)
        if not parsed.has_more or page >= parsed.total_pages or page >= AA_MAX_PAGES:
            break
        page += 1
    assert combined is not None
    return combined


def _fetch_arena(token: str, get: GetJson | None) -> tuple[LMArenaResult, str]:
    raw_splits = router_benchmarks.fetch_lmarena_splits(token, get)
    catalog = router_benchmarks.parse_lmarena_splits(raw_splits)
    target = router_benchmarks.select_lmarena_target(catalog)
    raw_rows = router_benchmarks.fetch_lmarena_rows(
        token, target.config, target.split, get, length=LMARENA_ROWS_LENGTH)
    result = router_benchmarks.parse_lmarena_rows(raw_rows, target.config, target.split)
    return result, f"{target.config}/{target.split}"


def update_benchmarks(
    config_dir: Path,
    *,
    get: GetJson | None = None,
    secrets: RouterSecrets | None = None,
    policy: RouterPolicy | None = None,
    aliases: AliasFile | None = None,
) -> RefreshReport:
    """Cron A: rebuild quality from AA + LMArena and republish both docs."""
    config_dir = Path(config_dir)
    catalog_file = config_dir / CATALOG_FILENAME
    if not catalog_file.is_file():
        raise RefreshError(
            f"no catalog at {catalog_file}; run update --providers first")
    try:
        catalog = load_catalog_doc(catalog_file)
    except PublishError as exc:
        raise RefreshError(f"catalog unreadable: {exc}") from exc
    active = policy or _load_policy(config_dir / "policy.yaml")
    aliases = aliases if aliases is not None else _load_aliases()
    secrets = _load_secrets() if secrets is None and get is None else secrets
    aa_key = secrets.FREE_ROUTER_AA_API_KEY if secrets else ""
    hf_token = secrets.FREE_ROUTER_HF_TOKEN if secrets else ""
    aa: AAResult | None = None
    arena: LMArenaResult | None = None
    arena_target: str | None = None
    failed: list[str] = []
    if not aa_key:
        failed.append("artificial_analysis: key unset")
    else:
        try:
            aa = _fetch_aa_pages(aa_key, get)
        except (SourceError, router_live.LiveError, RefreshError) as exc:
            failed.append(f"artificial_analysis: {exc}")
    if not hf_token:
        failed.append("huggingface: token unset")
    else:
        try:
            arena, arena_target = _fetch_arena(hf_token, get)
        except (SourceError, router_live.LiveError) as exc:
            failed.append(f"huggingface: {exc}")
    if aa is None and arena is None:
        detail = "; ".join(failed) or "no benchmark sources"
        raise RefreshError(f"all benchmark sources failed ({detail}); previous snapshot kept")
    previous_version, _previous_target = _previous_rankings_provenance(config_dir)
    if aa is not None and previous_version is not None:
        try:
            router_benchmarks.assert_same_aa_major(previous_version, aa.index_version)
        except ValueError as exc:
            raise RefreshError(f"AA major moved; keeping previous: {exc}") from exc
    observed = _utc_now().isoformat()
    qualities, _unresolved = router_benchmarks.build_quality(
        aliases, observed, aa=aa, arena=arena)
    enriched = attach_benchmarks(catalog, qualities)
    try:
        record = publish_catalog(
            enriched, config_dir, stale=catalog.stale, stale_reason=catalog.stale_reason)
    except PublishError as exc:
        raise RefreshError(f"catalog publication failed: {exc}") from exc
    sources: list[str] = []
    if aa is not None:
        sources.append(f"artificial-analysis:index{aa.index_version}@{observed}")
    if arena is not None and arena_target is not None:
        sources.append(f"lmarena:{arena_target}@{observed}")
    return _republish_rankings(
        config_dir, record, active, benchmark_sources=sources,
        aa_index_version=aa.index_version if aa is not None else None,
        lmarena_target=arena_target,
        generator="model_routing.refresh", kind="benchmarks",
        fetched=tuple(source.split("@")[0] for source in sources),
        failed=tuple(failed),
        stale=catalog.stale, stale_reason=catalog.stale_reason,
    )


def source_staleness(
    catalog: CatalogDocument, *, now: datetime | None = None
) -> list[SourceStaleness]:
    """Freshness per provider from persisted observations and TTLs."""
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seen: dict[str, list] = {}
    for entry in catalog.entries:
        for deployment in entry.deployments:
            seen.setdefault(deployment.provider, []).append(deployment)
    report: list[SourceStaleness] = []
    for provider in sorted(set(seen) | set(SOURCE_TTLS)):
        deployments = seen.get(provider, [])
        verified = sum(1 for deployment in deployments if deployment.free_verified)
        observed = [deployment.observed_at for deployment in deployments
                    if deployment.observed_at is not None]
        newest = max(observed) if observed else None
        if newest is not None and newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        ttl = SOURCE_TTLS.get(provider)
        fresh = newest is not None and ttl is not None and current - newest <= ttl
        report.append(SourceStaleness(
            provider=provider,
            entries=len(deployments),
            verified=verified,
            newest_observed_at=newest.isoformat() if newest else None,
            ttl_hours=ttl.total_seconds() / 3600 if ttl else None,
            fresh=fresh,
        ))
    return report


def run_update(
    kind: str,
    config_dir: Path,
    state_dir: Path,
    update: Callable[[], RefreshReport] | None = None,
) -> tuple[int, str]:
    """Run one refresh under the flock guard, appending to updater.log."""
    if kind not in ("providers", "benchmarks"):
        return 2, f"unknown update kind: {kind}"
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / UPDATER_LOCK_FILENAME
    log_path = state_dir / UPDATER_LOG_FILENAME
    runner = update or (lambda: update_providers(config_dir)
                        if kind == "providers" else update_benchmarks(config_dir))
    with open(lock_path, "a+", encoding="utf-8") as guard:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _log_outcome(
                log_path, kind, False, f"another update holds {lock_path}; skipping this run")
        try:
            try:
                report = runner()
            except (RefreshError, PublishError, SecretsError,
                    router_live.LiveError, SourceError,
                    router_benchmarks.AliasError,
                    router_terms.TermsError) as exc:
                router_alerts.emit_alert(
                    state_dir, "refresh_failed", f"update --{kind}", str(exc))
                return _log_outcome(log_path, kind, False, str(exc))
            except Exception:  # noqa: BLE001 - the log must capture any crash
                router_alerts.emit_alert(
                    state_dir, "refresh_failed", f"update --{kind}",
                    f"unexpected error: {traceback.format_exc(limit=3)}")
                return _log_outcome(log_path, kind, False,
                                    f"unexpected error: {traceback.format_exc(limit=3)}")
            detail = (f"catalog {report.catalog_snapshot} "
                      f"rankings {report.rankings_snapshot}")
            if report.stale:
                detail += f" STALE ({report.stale_reason})"
            if report.failed:
                detail += f"; failed: {'; '.join(report.failed)}"
            _alert_when_no_free_models(config_dir, state_dir)
            return _log_outcome(log_path, kind, True, detail)
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _alert_when_no_free_models(config: Path, state_dir: Path) -> None:
    """Alert when a successful refresh leaves zero usable deployments.

    A missing or unreadable catalog means the runner did not publish
    (injected test runners): nothing to check, no alert.
    """
    catalog_file = Path(config) / CATALOG_FILENAME
    if not catalog_file.is_file():
        return
    try:
        catalog = load_catalog_doc(catalog_file)
    except PublishError:
        return
    active = _load_policy(Path(config) / "policy.yaml")
    usable = sum(1 for entry in catalog.entries for route in entry.deployments
                 if is_usable(route, active))
    if usable == 0:
        router_alerts.emit_alert(
            state_dir, "no_free_models", "update",
            f"{catalog_file.name} publishes {len(catalog.entries)} entries"
            " with zero usable deployments")


def _log_outcome(log_path: Path, kind: str, ok: bool, detail: str) -> tuple[int, str]:
    line = f"{_utc_now().isoformat()} update --{kind} {'OK' if ok else 'FAIL'}: {detail}\n"
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(line)
    return (0 if ok else 1), detail
