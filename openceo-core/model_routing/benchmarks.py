"""Benchmark sources: Artificial Analysis, LMArena, normalization, aliases (TASK-2026-00290).

Quality signals per canonical model, comparable across sources:

- Artificial Analysis: ``GET /api/v2/language/models/free`` with
  ``x-api-key``. The endpoint is free data, not a free-inference list.
  The intelligence-index version is recorded and major versions are
  never compared numerically.
- LMArena: the official Hugging Face datasets-server API over the
  ``lmarena-ai/leaderboard-dataset`` parquet exports (``/splits`` to
  verify config/split dynamically, ``/rows`` for slices). No page
  scraping. Agent-arena rows (intervals) stay distinct from text
  rankings (ratings).
- Normalization: percentiles per source and date, with coverage and
  degraded confidence when a metric is missing.
- Aliases: versioned maker-to-model maps across LMArena, Artificial
  Analysis and providers, exact match only, plus a manual exceptions
  list. Families resolve conservatively: ``unknown-family`` never
  counts as distinct. Models without enough benchmarks enter the
  ``experimental`` bucket instead of ranking first.

Error taxonomy is shared with ``sources`` (TASK-2026-00291 unifies
SPEC §9.2); canonical unification across providers belongs to
TASK-2026-00292, so this module resolves families without merging
catalog entries.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from model_routing.sources import GetJson, SourceError

__all__ = [
    "AA_FREE_URL",
    "ALIASES_SCHEMA_VERSION",
    "EXPERIMENTAL_MIN_OBSERVATIONS",
    "LMARENA_DATASET",
    "LMARENA_SPLITS_URL",
    "UNKNOWN_FAMILY",
    "WANTED_LMARENA_CONFIGS",
    "AAModel",
    "AAResult",
    "AliasEntry",
    "AliasError",
    "AliasFile",
    "AliasException",
    "LMArenaCatalog",
    "LMArenaObservation",
    "LMArenaResult",
    "LMArenaSplit",
    "ModelQuality",
    "NormalizedScore",
    "Resolution",
    "UnresolvedModel",
    "aa_major",
    "apply_families",
    "assert_same_aa_major",
    "build_quality",
    "default_aliases_path",
    "distinct_families",
    "fetch_aa_models",
    "fetch_lmarena_rows",
    "fetch_lmarena_splits",
    "find_unresolved",
    "load_aliases",
    "parse_aa_models",
    "parse_lmarena_rows",
    "parse_lmarena_splits",
    "percentile",
    "resolve_canonical",
    "resolve_family",
    "select_lmarena_target",
    "try_resolve",
]

AA_FREE_URL = "https://artificialanalysis.ai/api/v2/language/models/free"

LMARENA_DATASET = "lmarena-ai/leaderboard-dataset"
LMARENA_SPLITS_URL = f"https://datasets-server.huggingface.co/splits?dataset={LMARENA_DATASET}"
_LMARENA_ROWS_URL = "https://datasets-server.huggingface.co/rows"

WANTED_LMARENA_CONFIGS = (
    "agent",
    "webdev",
    "agent_tool_hallucination",
    "search",
    "text_style_control",
)

UNKNOWN_FAMILY = "unknown-family"

ALIASES_SCHEMA_VERSION = 1

EXPERIMENTAL_MIN_OBSERVATIONS = 2

_AA_EVALUATIONS = {
    "intelligence": "artificial_analysis_intelligence_index",
    "coding": "artificial_analysis_coding_index",
    "agentic": "artificial_analysis_agentic_index",
}

_MODEL_COLUMNS = ("model", "model_name", "name", "model_id")
_SCORE_COLUMNS = ("score", "rating", "elo", "win_rate", "ips", "value")
_INTERVAL_PAIRS = (
    ("score_ci_lower", "score_ci_upper"),
    ("ci_low", "ci_high"),
    ("lower", "upper"),
    ("lower_bound", "upper_bound"),
)

_USER_AGENT = "free-router-live/1"


class AliasError(RuntimeError):
    """Alias file invalid or no exact alias for an external id."""


class AAModel(BaseModel):
    """One Artificial Analysis row; missing indexes stay null."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str = ""
    creator: str = ""
    intelligence: float | None = None
    coding: float | None = None
    agentic: float | None = None


class AAResult(BaseModel):
    """One fetched AA page with its index version and pagination."""

    model_config = ConfigDict(extra="forbid")

    index_version: float
    models: list[AAModel] = []
    page: int = 1
    total_pages: int = 1
    has_more: bool = False
    skipped: int = 0


class LMArenaSplit(BaseModel):
    """One verified config/split pair from ``/splits``."""

    model_config = ConfigDict(extra="forbid")

    config: str
    split: str


class LMArenaCatalog(BaseModel):
    """Available, pending and failed splits for the dataset."""

    model_config = ConfigDict(extra="forbid")

    splits: list[LMArenaSplit] = []
    pending: list[str] = []
    failed: list[str] = []


class LMArenaObservation(BaseModel):
    """One per-model score; agent arenas keep their intervals."""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    score: float
    lower: float | None = None
    upper: float | None = None
    kind: str = "text_ranking"
    config: str = ""
    split: str = ""


class LMArenaResult(BaseModel):
    """Parsed ``/rows`` slice with its column names and skip count."""

    model_config = ConfigDict(extra="forbid")

    config: str
    split: str
    observations: list[LMArenaObservation] = []
    skipped_rows: int = 0
    features: list[str] = []


class NormalizedScore(BaseModel):
    """Percentile within one source/date cohort, with its coverage."""

    model_config = ConfigDict(extra="forbid")

    value: float
    coverage: float = 1.0
    confidence: str = "full"
    source: str = ""
    observed_at: str = ""


class ModelQuality(BaseModel):
    """Comparable quality for one canonical model, all sources merged."""

    model_config = ConfigDict(extra="forbid")

    canonical_model_id: str
    family_id: str
    dimensions: dict[str, NormalizedScore] = {}
    observations: int = 0
    experimental: bool = True
    experimental_reason: str = ""
    sources: list[str] = []


class AliasEntry(BaseModel):
    """One canonical model with its exact external ids per namespace."""

    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    family_id: str
    source: str = ""
    providers: dict[str, list[str]] = {}
    aa_slugs: list[str] = []
    lmarena_names: list[str] = []


class AliasException(BaseModel):
    """Manual override for one vendor id, checked before the maps."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    external_id: str
    canonical_id: str
    reason: str = ""


class AliasFile(BaseModel):
    """Whole versioned alias document with its verification date."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = ALIASES_SCHEMA_VERSION
    verified_at: str
    aliases: list[AliasEntry] = []
    exceptions: list[AliasException] = []

    @field_validator("verified_at")
    @classmethod
    def _check_date(cls, value: str) -> str:
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"verified_at is not an ISO date: {value!r}") from exc
        return value


class Resolution(NamedTuple):
    """Exact alias hit with the map that produced it."""

    canonical_id: str
    family_id: str
    via: str


class UnresolvedModel(NamedTuple):
    """External id without an alias entry, with the reason why."""

    vendor: str
    external_id: str
    reason: str


def _safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.split("@")[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _error_body(exc: urllib.error.HTTPError, limit: int = 300) -> str:
    """Truncated server explanation; responses never carry our credentials."""
    if exc.fp is None:
        return ""
    try:
        raw = exc.read(4096)
    except (OSError, ValueError):
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else ""
    return " ".join(text.split())[:limit]


def _get_json(
    provider: str, url: str, headers: Mapping[str, str], get: GetJson | None
) -> dict:
    """Run a metadata GET through the real client or a test double."""
    if get is None:
        merged = dict(headers)
        merged.setdefault("User-Agent", _USER_AGENT)
        request = urllib.request.Request(url, headers=merged, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise SourceError.from_http(
                provider, exc.code, _safe_url(url), exc.headers.get("Retry-After"),
                _error_body(exc),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError(
                provider, "network", f"GET {_safe_url(url)} failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise SourceError(provider, "parse", f"GET {_safe_url(url)} returned non-JSON") from exc
    else:
        try:
            payload = get(url, headers)
        except urllib.error.HTTPError as exc:
            raise SourceError.from_http(
                provider, exc.code, _safe_url(url), exc.headers.get("Retry-After"),
                _error_body(exc),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError(
                provider, "network", f"GET {_safe_url(url)} failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
    if not isinstance(payload, dict):
        raise SourceError(provider, "parse", f"GET {_safe_url(url)} returned non-object")
    return payload


def fetch_aa_models(api_key: str, get: GetJson | None = None, page: int = 1) -> dict:
    """Free data endpoint (100 calls/24 h); one page per call."""
    if page < 1:
        raise ValueError(f"AA page must be positive: {page}")
    url = AA_FREE_URL if page == 1 else f"{AA_FREE_URL}?page={page}"
    return _get_json("artificial_analysis", url, {"x-api-key": api_key}, get)


def _aa_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_aa_models(raw: dict) -> AAResult:
    """Parse one AA page; the index version is mandatory provenance."""
    version = _aa_number(raw.get("intelligence_index_version"))
    if version is None:
        raise SourceError("artificial_analysis", "parse", "AA payload misses intelligence_index_version")
    items = raw.get("data")
    if not isinstance(items, list):
        raise SourceError("artificial_analysis", "parse", "AA payload has no data list")
    models: list[AAModel] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            skipped += 1
            continue
        evaluations = item.get("evaluations") if isinstance(item.get("evaluations"), dict) else {}
        creator = item.get("model_creator")
        creator_name = creator.get("name") if isinstance(creator, dict) else ""
        models.append(
            AAModel(
                slug=item["slug"],
                name=item.get("name") if isinstance(item.get("name"), str) else "",
                creator=creator_name if isinstance(creator_name, str) else "",
                intelligence=_aa_number(evaluations.get(_AA_EVALUATIONS["intelligence"])),
                coding=_aa_number(evaluations.get(_AA_EVALUATIONS["coding"])),
                agentic=_aa_number(evaluations.get(_AA_EVALUATIONS["agentic"])),
            )
        )
    pagination = raw.get("pagination") if isinstance(raw.get("pagination"), dict) else {}
    return AAResult(
        index_version=version,
        models=models,
        page=pagination.get("page") if isinstance(pagination.get("page"), int) else 1,
        total_pages=pagination.get("total_pages") if isinstance(pagination.get("total_pages"), int) else 1,
        has_more=pagination.get("has_more") is True,
        skipped=skipped,
    )


def aa_major(version: float) -> int:
    """Major component of an AA index version (4.3 -> 4)."""
    return int(version)


def assert_same_aa_major(previous: float, current: float) -> None:
    """Refuse numeric comparison across AA index major versions."""
    if aa_major(previous) != aa_major(current):
        raise ValueError(f"AA index major changed: {previous} vs {current}; do not compare")


def fetch_lmarena_splits(token: str, get: GetJson | None = None) -> dict:
    """List every config/split the viewer serves for the dataset."""
    return _get_json(
        "lmarena", LMARENA_SPLITS_URL, {"Authorization": f"Bearer {token}"}, get
    )


def _split_label(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    config, split = entry.get("config"), entry.get("split")
    if not isinstance(config, str) or not isinstance(split, str):
        return None
    return f"{config}/{split}"


def parse_lmarena_splits(raw: dict) -> LMArenaCatalog:
    """Strict on splits (the download target), lenient on diagnostics."""
    items = raw.get("splits")
    if not isinstance(items, list):
        raise SourceError("lmarena", "parse", "/splits payload has no splits list")
    splits: list[LMArenaSplit] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("config"), str)
            or not isinstance(item.get("split"), str)
        ):
            raise SourceError("lmarena", "parse", "/splits entry misses config/split strings")
        splits.append(LMArenaSplit(config=item["config"], split=item["split"]))
    pending = [
        label
        for entry in (raw.get("pending") if isinstance(raw.get("pending"), list) else [])
        if (label := _split_label(entry)) is not None
    ]
    failed = [
        label
        for entry in (raw.get("failed") if isinstance(raw.get("failed"), list) else [])
        if (label := _split_label(entry)) is not None
    ]
    return LMArenaCatalog(splits=splits, pending=pending, failed=failed)


def select_lmarena_target(
    catalog: LMArenaCatalog, wanted: tuple[str, ...] = WANTED_LMARENA_CONFIGS
) -> LMArenaSplit:
    """First wanted config available, preferring its train split."""
    by_config: dict[str, list[str]] = {}
    for entry in catalog.splits:
        by_config.setdefault(entry.config, []).append(entry.split)
    for name in wanted:
        splits = by_config.get(name)
        if not splits:
            continue
        return LMArenaSplit(config=name, split="train" if "train" in splits else splits[0])
    available = ", ".join(sorted(by_config)) or "none"
    raise SourceError(
        "lmarena", "parse",
        f"none of the wanted configs {', '.join(wanted)} is available ({available})",
    )


def fetch_lmarena_rows(
    token: str,
    config: str,
    split: str,
    get: GetJson | None = None,
    *,
    length: int = 100,
    offset: int = 0,
) -> dict:
    """Download one slice of rows (the viewer caps length at 100)."""
    if not 1 <= length <= 100:
        raise ValueError(f"rows length must be within 1..100: {length}")
    if offset < 0:
        raise ValueError(f"rows offset must be non-negative: {offset}")
    query = urllib.parse.urlencode(
        {"dataset": LMARENA_DATASET, "config": config, "split": split,
         "offset": offset, "length": length}
    )
    return _get_json(
        "lmarena", f"{_LMARENA_ROWS_URL}?{query}", {"Authorization": f"Bearer {token}"}, get
    )


def _arena_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first_present(columns: tuple[str, ...], available: set[str]) -> str | None:
    for name in columns:
        if name in available:
            return name
    return None


def parse_lmarena_rows(raw: dict, config: str, split: str) -> LMArenaResult:
    """Per-model scores from a rows slice; unknown schemas fail closed."""
    features = raw.get("features")
    if not isinstance(features, list):
        raise SourceError("lmarena", "parse", "/rows payload has no features list")
    names = [entry.get("name") for entry in features if isinstance(entry, dict)]
    columns = {name for name in names if isinstance(name, str)}
    model_column = _first_present(_MODEL_COLUMNS, columns)
    score_column = _first_present(_SCORE_COLUMNS, columns)
    if model_column is None or score_column is None:
        available = ", ".join(sorted(columns)) or "none"
        raise SourceError(
            "lmarena", "parse",
            f"/rows slice has no usable model/score columns ({available})",
        )
    interval = next(
        (pair for pair in _INTERVAL_PAIRS if pair[0] in columns and pair[1] in columns),
        None,
    )
    items = raw.get("rows")
    if not isinstance(items, list):
        raise SourceError("lmarena", "parse", "/rows payload has no rows list")
    kind = "agent_arena" if config.startswith("agent") else "text_ranking"
    observations: list[LMArenaObservation] = []
    skipped = 0
    for item in items:
        row = item.get("row") if isinstance(item, dict) else None
        if not isinstance(row, dict):
            skipped += 1
            continue
        model_name = row.get(model_column)
        score = _arena_number(row.get(score_column))
        if not isinstance(model_name, str) or not model_name or score is None:
            skipped += 1
            continue
        lower = upper = None
        if interval is not None:
            lower = _arena_number(row.get(interval[0]))
            upper = _arena_number(row.get(interval[1]))
        observations.append(
            LMArenaObservation(
                model_name=model_name,
                score=score,
                lower=lower,
                upper=upper,
                kind=kind,
                config=config,
                split=split,
            )
        )
    return LMArenaResult(
        config=config,
        split=split,
        observations=observations,
        skipped_rows=skipped,
        features=sorted(columns),
    )


def percentile(values: list[float], point: float) -> float:
    """Mean-rank percentile of one point within its cohort (0..1)."""
    if not values:
        raise ValueError("percentile needs a non-empty cohort")
    below = sum(1 for value in values if value < point)
    equal = sum(1 for value in values if value == point)
    return (below + 0.5 * equal) / len(values)


def _confidence(present: int, total: int) -> str:
    if present >= total:
        return "full"
    if present > 0:
        return "degraded"
    return "insufficient"


def default_aliases_path() -> Path:
    """Versioned maker-to-model alias file inside the package."""
    return Path(__file__).resolve().parent / "data" / "model_aliases.yaml"


def load_aliases(path: Path | None = None) -> AliasFile:
    """Load the versioned aliases; duplicates or dangling refs fail closed."""
    target = path or default_aliases_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AliasError(f"alias file missing or unreadable: {target}") from exc
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AliasError(f"alias file {target} is not valid YAML") from exc
    if not isinstance(data, dict):
        raise AliasError(f"alias file {target} is empty or not a mapping")
    try:
        aliases = AliasFile.model_validate(data)
    except ValidationError as exc:
        raise AliasError(f"alias file {target} invalid: {exc.errors()[0]['msg']}") from exc
    if aliases.schema_version != ALIASES_SCHEMA_VERSION:
        raise AliasError(f"unsupported alias schema_version: {aliases.schema_version}")
    canonicals = [entry.canonical_id for entry in aliases.aliases]
    if len(set(canonicals)) != len(canonicals):
        raise AliasError("duplicate canonical_id in alias file")
    seen: dict[tuple[str, str], str] = {}
    for entry in aliases.aliases:
        if not entry.family_id:
            raise AliasError(f"alias entry {entry.canonical_id} misses family_id")
        for vendor, ids in entry.providers.items():
            for external_id in ids:
                key = (vendor, external_id)
                if key in seen:
                    raise AliasError(f"external id {vendor}:{external_id} maps twice")
                seen[key] = entry.canonical_id
        for slug in entry.aa_slugs:
            key = ("aa", slug)
            if key in seen:
                raise AliasError(f"external id aa:{slug} maps twice")
            seen[key] = entry.canonical_id
        for name in entry.lmarena_names:
            key = ("lmarena", name)
            if key in seen:
                raise AliasError(f"external id lmarena:{name} maps twice")
            seen[key] = entry.canonical_id
    known = set(canonicals)
    seen_exceptions: set[tuple[str, str]] = set()
    for override in aliases.exceptions:
        key = (override.vendor, override.external_id)
        if key in seen_exceptions:
            raise AliasError(f"exception {override.vendor}:{override.external_id} repeats")
        seen_exceptions.add(key)
        if override.canonical_id not in known:
            raise AliasError(f"exception {override.vendor}:{override.external_id} dangles")
    return aliases


def _build_index(aliases: AliasFile) -> tuple[dict[tuple[str, str], Resolution], dict[str, str]]:
    index: dict[tuple[str, str], Resolution] = {}
    for entry in aliases.aliases:
        for vendor, ids in entry.providers.items():
            for external_id in ids:
                index[(vendor, external_id)] = Resolution(entry.canonical_id, entry.family_id, vendor)
        for slug in entry.aa_slugs:
            index[("aa", slug)] = Resolution(entry.canonical_id, entry.family_id, "aa")
        for name in entry.lmarena_names:
            index[("lmarena", name)] = Resolution(entry.canonical_id, entry.family_id, "lmarena")
    families = {entry.canonical_id: entry.family_id for entry in aliases.aliases}
    for override in aliases.exceptions:
        index[(override.vendor, override.external_id)] = Resolution(
            override.canonical_id, families[override.canonical_id], "exception"
        )
    return index, families


def try_resolve(vendor: str, external_id: str, aliases: AliasFile) -> Resolution | None:
    """Exact alias hit or None; near-misses never resolve."""
    index, _ = _build_index(aliases)
    return index.get((vendor, external_id))


def resolve_canonical(vendor: str, external_id: str, aliases: AliasFile) -> Resolution:
    """Exact alias hit or an AliasError naming why it missed."""
    hit = try_resolve(vendor, external_id, aliases)
    if hit is None:
        raise AliasError(f"no alias entry for {vendor}:{external_id}")
    return hit


def resolve_family(canonical_id: str, aliases: AliasFile) -> str:
    """Family for a canonical id, unknown when the alias file lacks it."""
    _, families = _build_index(aliases)
    return families.get(canonical_id, UNKNOWN_FAMILY)


def distinct_families(first: str, second: str) -> bool:
    """Unknown families never count as distinct (SPEC §6.3)."""
    if first == UNKNOWN_FAMILY or second == UNKNOWN_FAMILY:
        return False
    return first != second


def _normalize_aa(
    result: AAResult, aliases: AliasFile, observed_at: str
) -> tuple[dict[str, dict[str, NormalizedScore]], list[UnresolvedModel]]:
    cohorts: dict[str, list[float]] = {dimension: [] for dimension in _AA_EVALUATIONS}
    for model in result.models:
        for dimension in cohorts:
            value = getattr(model, dimension)
            if value is not None:
                cohorts[dimension].append(value)
    source = f"artificial-analysis:index{result.index_version}@{observed_at}"
    scores: dict[str, dict[str, NormalizedScore]] = {}
    unresolved: list[UnresolvedModel] = []
    for model in result.models:
        hit = try_resolve("aa", model.slug, aliases)
        if hit is None:
            unresolved.append(UnresolvedModel("aa", model.slug, "no alias entry for this slug"))
            continue
        present = sum(1 for dimension in cohorts if getattr(model, dimension) is not None)
        dimensions: dict[str, NormalizedScore] = {}
        for dimension, cohort in cohorts.items():
            value = getattr(model, dimension)
            if value is None or not cohort:
                continue
            dimensions[dimension] = NormalizedScore(
                value=percentile(cohort, value),
                coverage=present / len(cohorts),
                confidence=_confidence(present, len(cohorts)),
                source=source,
                observed_at=observed_at,
            )
        scores.setdefault(hit.canonical_id, {}).update(dimensions)
    return scores, unresolved


def _normalize_arena(
    result: LMArenaResult, aliases: AliasFile, observed_at: str
) -> tuple[dict[str, NormalizedScore], list[UnresolvedModel]]:
    cohort = [observation.score for observation in result.observations]
    source = f"lmarena:{result.config}/{result.split}@{observed_at}"
    scores: dict[str, NormalizedScore] = {}
    unresolved: list[UnresolvedModel] = []
    for observation in result.observations:
        hit = try_resolve("lmarena", observation.model_name, aliases)
        if hit is None:
            unresolved.append(
                UnresolvedModel("lmarena", observation.model_name, "no alias entry for this name")
            )
            continue
        if hit.canonical_id in scores:
            continue
        scores[hit.canonical_id] = NormalizedScore(
            value=percentile(cohort, observation.score),
            coverage=1.0,
            confidence="full",
            source=source,
            observed_at=observed_at,
        )
    return scores, unresolved


def build_quality(
    aliases: AliasFile,
    observed_at: str,
    *,
    aa: AAResult | None = None,
    arena: LMArenaResult | None = None,
) -> tuple[list[ModelQuality], list[UnresolvedModel]]:
    """Merge every source into per-canonical quality; thin models stay experimental."""
    merged: dict[str, dict[str, NormalizedScore]] = {
        entry.canonical_id: {} for entry in aliases.aliases
    }
    unresolved: list[UnresolvedModel] = []
    if aa is not None:
        aa_scores, aa_missing = _normalize_aa(aa, aliases, observed_at)
        unresolved.extend(aa_missing)
        for canonical_id, dimensions in aa_scores.items():
            merged.setdefault(canonical_id, {}).update(dimensions)
    if arena is not None:
        arena_scores, arena_missing = _normalize_arena(arena, aliases, observed_at)
        unresolved.extend(arena_missing)
        for canonical_id, score in arena_scores.items():
            merged.setdefault(canonical_id, {}).setdefault("arena", score)
    qualities: list[ModelQuality] = []
    for entry in aliases.aliases:
        dimensions = merged.get(entry.canonical_id, {})
        count = len(dimensions)
        experimental = count < EXPERIMENTAL_MIN_OBSERVATIONS
        if count == 0:
            reason = "no benchmark observations from any source"
        elif experimental:
            missing = EXPERIMENTAL_MIN_OBSERVATIONS - count
            reason = f"only {count} benchmark observation(s); {missing} more needed out of experimental"
        else:
            reason = ""
        qualities.append(
            ModelQuality(
                canonical_model_id=entry.canonical_id,
                family_id=entry.family_id,
                dimensions=dimensions,
                observations=count,
                experimental=experimental,
                experimental_reason=reason,
                sources=sorted({score.source for score in dimensions.values()}),
            )
        )
    return qualities, unresolved


def find_unresolved(
    aliases: AliasFile,
    *,
    aa: AAResult | None = None,
    arena: LMArenaResult | None = None,
    provider_ids: Mapping[str, list[str]] | None = None,
) -> list[UnresolvedModel]:
    """External ids without an alias entry, each with its reason."""
    missing: list[UnresolvedModel] = []
    if aa is not None:
        for model in aa.models:
            if try_resolve("aa", model.slug, aliases) is None:
                missing.append(UnresolvedModel("aa", model.slug, "no alias entry for this slug"))
    if arena is not None:
        for observation in arena.observations:
            if try_resolve("lmarena", observation.model_name, aliases) is None:
                missing.append(
                    UnresolvedModel("lmarena", observation.model_name, "no alias entry for this name")
                )
    for vendor, ids in (provider_ids or {}).items():
        for external_id in ids:
            if try_resolve(vendor, external_id, aliases) is None:
                missing.append(
                    UnresolvedModel(vendor, external_id, "no alias entry for this provider id")
                )
    return missing


def apply_families(document: object, aliases: AliasFile) -> object:
    """Copy a catalog with alias families; canonical ids stay provider-scoped.

    Canonical unification across providers (duplicate ``(family,
    canonical)`` pairs) belongs to TASK-2026-00292; this step only
    fills ``family_id`` so unknown families shrink before ranking.
    """
    from model_routing.models import CatalogDocument

    if not isinstance(document, CatalogDocument):
        raise AliasError(f"apply_families needs a CatalogDocument, got {type(document).__name__}")
    entries = []
    for entry in document.entries:
        family_id = entry.family_id
        for deployment in entry.deployments:
            gateway_id = deployment.gateway_model_id
            if gateway_id is None:
                continue
            hit = try_resolve(deployment.provider, gateway_id, aliases)
            if hit is not None:
                family_id = hit.family_id
                break
        entries.append(entry.model_copy(update={"family_id": family_id}))
    return document.model_copy(update={"entries": entries})
