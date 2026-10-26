"""Provider catalog fetchers with free-tier verification (TASK-2026-00289).

Five fetchers turn provider model lists into catalog deployments with
explicit ``free_offered`` / ``free_verified`` decisions. Prices decide,
never name suffixes:

- OpenRouter: prompt and completion price zero plus the ``:free`` suffix,
  no nonzero extra prices (image, audio, search, reasoning, per-request
  fee), and free quota remaining on ``GET /api/v1/key``.
- OpenCode: the model is both listed live and Free in the transcribed
  docs pricing table, plus fresh serve evidence at zero cost. Direct
  HTTPS is policy-blocked (403 FreeTierError), so the evidence comes
  from a CLI run, which is the only allowed free path; the CLI auth
  state itself is not checked.
- Groq, Gemini, NVIDIA: the account-level free tier from the manual
  terms record (no card, recurring, not a one-time credit) plus a valid
  key; the catalog stores the effective free-tier price (0.0), since
  list prices are paid-tier prices for another deployment.

Fetchers never set ``healthy`` or ``streaming``: probes do that
after serving. ``tools`` comes from provider-declared list metadata
where published (OpenRouter ``supported_parameters``; TASK-2026-00292),
declared support gating the ranking while probes verify reliability.
Families stay ``unknown-family`` until TASK-2026-00290 resolves them;
canonical ids are provider-scoped until TASK-2026-00292 unifies them.
The error taxonomy here is fetcher-local; TASK-2026-00291 unifies
SPEC §9.2.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import ValidationError

from model_routing import live as router_live
from model_routing.models import (
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Deployment,
)
from model_routing.terms import (
    OpenCodeFreeList,
    ProviderTerms,
    TermsError,
    TermsFile,
    is_fresh,
    terms_for,
)

__all__ = [
    "OPENROUTER_KEY_TTL",
    "PROVIDERS",
    "SERVE_EVIDENCE_TTL",
    "SOURCE_TTLS",
    "UNKNOWN_FAMILY",
    "FetchedCatalog",
    "GetJson",
    "SourceError",
    "SourceKind",
    "build_catalog",
    "fetch_gemini_models",
    "fetch_groq_models",
    "fetch_nvidia_models",
    "fetch_opencode_models",
    "fetch_openrouter_key",
    "fetch_openrouter_models",
    "is_terms_usable",
    "merge_with_previous",
    "parse_gemini_models",
    "parse_groq_models",
    "parse_nvidia_models",
    "parse_opencode_models",
    "parse_openrouter_models",
    "refresh_source",
]

PROVIDERS = ("openrouter", "opencode", "nvidia", "groq", "gemini")

UNKNOWN_FAMILY = "unknown-family"

SOURCE_TTLS: dict[str, timedelta] = {
    "openrouter": timedelta(hours=6),
    "opencode": timedelta(hours=6),
    "nvidia": timedelta(hours=6),
    "groq": timedelta(hours=6),
    "gemini": timedelta(hours=6),
}

OPENROUTER_KEY_TTL = timedelta(minutes=15)

SERVE_EVIDENCE_TTL = timedelta(days=7)

_USER_AGENT = "free-router-live/1"

SourceKind = Literal["auth", "forbidden", "rate_limited", "server", "network", "parse"]

GetJson = Callable[[str, Mapping[str, str]], dict]


class SourceError(RuntimeError):
    """A provider source failed; carries status, never credentials."""

    def __init__(
        self,
        provider: str,
        kind: SourceKind,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after: str | None = None,
        detail: str = "",
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
        self.detail = detail
        text = f"{provider} source {kind}: {message}"
        if detail:
            text += f" :: {detail}"
        super().__init__(text)

    @classmethod
    def from_http(
        cls,
        provider: str,
        status: int,
        safe_url: str,
        retry_after: str | None = None,
        detail: str = "",
    ) -> SourceError:
        """Map HTTP failures to kinds; auth/parse never retry."""
        if status == 401:
            return cls(provider, "auth", f"GET {safe_url} -> HTTP 401", http_status=401, detail=detail)
        if status == 403:
            return cls(provider, "forbidden", f"GET {safe_url} -> HTTP 403", http_status=403, detail=detail)
        if status == 429:
            return cls(
                provider,
                "rate_limited",
                f"GET {safe_url} -> HTTP 429",
                http_status=429,
                retryable=True,
                retry_after=retry_after,
                detail=detail,
            )
        if 500 <= status <= 599:
            return cls(
                provider,
                "server",
                f"GET {safe_url} -> HTTP {status}",
                http_status=status,
                retryable=True,
                retry_after=retry_after,
                detail=detail,
            )
        return cls(provider, "server", f"GET {safe_url} -> HTTP {status}", http_status=status, detail=detail)


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


@dataclass(frozen=True)
class FetchedCatalog:
    """One provider's parsed entries with fetch time and TTL."""

    provider: str
    entries: tuple[CatalogEntry, ...]
    fetched_at: datetime
    ttl: timedelta

    def is_fresh(self, now: datetime | None = None) -> bool:
        """Stale fetches must be refreshed, never silently reused."""
        current = now or datetime.now(timezone.utc)
        return timedelta(0) <= current - self.fetched_at <= self.ttl


def _safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.split("@")[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _http_get_json(provider: str, url: str, headers: Mapping[str, str]) -> dict:
    """Real GET with live-harness hygiene; errors name codes, not secrets."""
    safe = _safe_url(url)
    merged = dict(headers)
    merged.setdefault("User-Agent", _USER_AGENT)
    request = urllib.request.Request(url, headers=merged, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SourceError.from_http(
            provider, exc.code, safe, exc.headers.get("Retry-After"), _error_body(exc)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(
            provider, "network", f"GET {safe} failed: {type(exc).__name__}", retryable=True
        ) from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise SourceError(provider, "parse", f"GET {safe} returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise SourceError(provider, "parse", f"GET {safe} returned a non-object payload")
    return payload


def _fetch(
    provider: str, url: str, headers: Mapping[str, str], get: GetJson | None
) -> dict:
    """Run a GET through the real client or an injected test double."""
    if get is None:
        return _http_get_json(provider, url, headers)
    try:
        payload = get(url, headers)
    except urllib.error.HTTPError as exc:
        raise SourceError.from_http(
            provider, exc.code, _safe_url(url), exc.headers.get("Retry-After"), _error_body(exc)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(
            provider, "network", f"GET {_safe_url(url)} failed: {type(exc).__name__}",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise SourceError(provider, "parse", f"GET {_safe_url(url)} returned non-object")
    return payload


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _price(value: object) -> float | None:
    """Provider prices arrive as strings; anything unclear stays unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _context_tokens(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def is_terms_usable(terms: TermsFile, provider: str) -> bool:
    """Manual record present, fresh, free, recurring and card-free."""
    try:
        record = terms_for(terms, provider)
    except TermsError:
        return False
    return (
        record.free_tier
        and not record.requires_payment_method
        and not record.one_time_credit_only
        and is_fresh(record.verified_at, terms.default_ttl_days)
    )


def _terms_gate(terms: TermsFile, provider: str) -> ProviderTerms | None:
    try:
        record = terms_for(terms, provider)
    except TermsError:
        return None
    if not is_terms_usable(terms, provider):
        return None
    return record


def _base_entry(
    provider: str,
    model_id: str,
    record: ProviderTerms | None,
    *,
    offered: bool,
    verified: bool,
    input_price: float | None,
    output_price: float | None,
    source: str,
    context: int | None = None,
    tools: bool = False,
) -> CatalogEntry:
    deployment_id = f"{provider}/{model_id}"
    native = provider == "opencode"
    return CatalogEntry(
        family_id=UNKNOWN_FAMILY,
        canonical_model_id=deployment_id,
        display_name=model_id,
        capabilities=Capabilities(context_tokens=context, tools=tools),
        deployments=[
            Deployment(
                deployment_id=deployment_id,
                provider=provider,
                route_type="opencode_native_free" if native else "external_free",
                allowed_client="opencode_only" if native else "configured_opencode_or_gateway",
                provider_id=provider,
                model_id=model_id,
                gateway_model_id=model_id,
                free_offered=offered,
                free_verified=verified,
                input_price_usd=input_price,
                output_price_usd=output_price,
                data_policy=record.data_policy if record else "unknown",
                retention_policy=record.retention if record else "unknown",
                approved_confidentiality=list(record.confidentiality_allowed) if record else [],
                requires_payment_method=record.requires_payment_method if record else True,
                source=f"{source} @{_utc_today()}",
                observed_at=datetime.now(timezone.utc),
            )
        ],
    )


def fetch_openrouter_models(get: GetJson | None = None) -> dict:
    """Public models catalog; no credential needed or sent."""
    return _fetch("openrouter", "https://openrouter.ai/api/v1/models", {}, get)


def fetch_openrouter_key(api_key: str, get: GetJson | None = None) -> dict:
    """Account quota and credit usage for the free-tier check."""
    return _fetch(
        "openrouter", "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {api_key}"}, get
    )


_OPENROUTER_EXTRA_PRICES = ("image", "audio", "web_search", "internal_reasoning", "request")


def parse_openrouter_models(
    raw: dict,
    key_payload: dict | None,
    terms: TermsFile,
) -> list[CatalogEntry]:
    """Zero text prices plus :free plus quota; extras block verification."""
    record = _terms_gate(terms, "openrouter")
    key_info = None
    if key_payload is not None:
        try:
            key_info = router_live.parse_openrouter_key_info(key_payload)
        except router_live.LiveError:
            key_info = None
    quota_ok = (
        key_info is not None
        and key_info.free_limit is not None
        and (key_info.free_remaining or 0) > 0
    )
    entries: list[CatalogEntry] = []
    items = raw.get("data")
    if not isinstance(items, list):
        raise SourceError("openrouter", "parse", "models payload has no data list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id: str = item["id"]
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        prompt = _price(pricing.get("prompt"))
        completion = _price(pricing.get("completion"))
        offered = prompt == 0 and completion == 0
        extras_paid = any(
            (extra := _price(pricing.get(name))) is not None and extra != 0
            for name in _OPENROUTER_EXTRA_PRICES
        )
        verified = (
            record is not None
            and offered
            and model_id.endswith(":free")
            and not extras_paid
            and quota_ok
        )
        declared = item.get("supported_parameters")
        tools = isinstance(declared, list) and "tools" in declared
        entries.append(
            _base_entry(
                "openrouter",
                model_id,
                record,
                offered=offered,
                verified=verified,
                input_price=prompt,
                output_price=completion,
                source="openrouter-models+key" if key_info is not None else "openrouter-models",
                context=_context_tokens(item.get("context_length")),
                tools=tools,
            )
        )
    return entries


def fetch_opencode_models(get: GetJson | None = None) -> dict:
    """Public model list; free chat models serve without a credential."""
    headers: dict[str, str] = {}
    router_live.assert_no_auth(headers)
    return _fetch("opencode", "https://opencode.ai/inference/v1/models", headers, get)


def parse_opencode_models(
    raw: dict,
    free_list: OpenCodeFreeList,
    terms: TermsFile,
    serve_evidence: Mapping[str, str] | None = None,
) -> list[CatalogEntry]:
    """Free means listed live plus Free in the transcribed pricing table."""
    record = _terms_gate(terms, "opencode")
    list_fresh = is_fresh(free_list.verified_at, terms.default_ttl_days)
    free_ids = set(free_list.model_ids) if list_fresh else set()
    evidence = dict(serve_evidence or {})
    today = datetime.now(timezone.utc)
    entries: list[CatalogEntry] = []
    items = raw.get("data")
    if not isinstance(items, list):
        raise SourceError("opencode", "parse", "models payload has no data list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id: str = item["id"]
        offered = record is not None and model_id in free_ids
        served_at = evidence.get(model_id)
        served_fresh = False
        if served_at is not None:
            try:
                observed = datetime.fromisoformat(served_at)
                served_fresh = timedelta(0) <= today - observed <= SERVE_EVIDENCE_TTL
            except (TypeError, ValueError):
                served_fresh = False
        verified = offered and served_fresh
        entries.append(
            _base_entry(
                "opencode",
                model_id,
                record,
                offered=offered,
                verified=verified,
                input_price=0.0 if offered else None,
                output_price=0.0 if offered else None,
                source="opencode-docs-pricing+probe",
            )
        )
    return entries


def fetch_groq_models(api_key: str, get: GetJson | None = None) -> dict:
    """Account model list; a 200 also proves the key is valid."""
    return _fetch(
        "groq", "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {api_key}"}, get
    )


def parse_groq_models(
    raw: dict, terms: TermsFile, *, key_valid: bool = True
) -> list[CatalogEntry]:
    """Account free tier decides; list prices are paid-tier prices."""
    record = _terms_gate(terms, "groq") if key_valid else None
    entries: list[CatalogEntry] = []
    items = raw.get("data")
    if not isinstance(items, list):
        raise SourceError("groq", "parse", "models payload has no data list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id: str = item["id"]
        offered = record is not None
        context = _context_tokens(item.get("context_window"))
        if context is None:
            context = _context_tokens(item.get("context_length"))
        entries.append(
            _base_entry(
                "groq",
                model_id,
                record,
                offered=offered,
                verified=offered,
                input_price=0.0 if offered else None,
                output_price=0.0 if offered else None,
                source="groq-free-tier-terms",
                context=context,
            )
        )
    return entries


def fetch_nvidia_models(api_key: str, get: GetJson | None = None) -> dict:
    """Build model list; a 200 also proves the key is valid."""
    return _fetch(
        "nvidia",
        "https://integrate.api.nvidia.com/v1/models",
        {"Authorization": f"Bearer {api_key}"},
        get,
    )


def parse_nvidia_models(
    raw: dict, terms: TermsFile, *, key_valid: bool = True
) -> list[CatalogEntry]:
    """Account trial tier decides; the list carries no prices."""
    record = _terms_gate(terms, "nvidia") if key_valid else None
    entries: list[CatalogEntry] = []
    items = raw.get("data")
    if not isinstance(items, list):
        raise SourceError("nvidia", "parse", "models payload has no data list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id: str = item["id"]
        offered = record is not None
        entries.append(
            _base_entry(
                "nvidia",
                model_id,
                record,
                offered=offered,
                verified=offered,
                input_price=0.0 if offered else None,
                output_price=0.0 if offered else None,
                source="nvidia-trial-terms",
            )
        )
    return entries


def fetch_gemini_models(api_key: str, get: GetJson | None = None) -> dict:
    """AI Studio model list (free endpoint, never Vertex)."""
    return _fetch(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/models",
        {"x-goog-api-key": api_key},
        get,
    )


def parse_gemini_models(
    raw: dict, terms: TermsFile, *, key_valid: bool = True
) -> list[CatalogEntry]:
    """AI Studio free tier decides; context comes from metadata."""
    record = _terms_gate(terms, "gemini") if key_valid else None
    entries: list[CatalogEntry] = []
    items = raw.get("models")
    if not isinstance(items, list):
        raise SourceError("gemini", "parse", "models payload has no models list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        model_id: str = item["name"].split("/")[-1]
        offered = record is not None
        entries.append(
            _base_entry(
                "gemini",
                model_id,
                record,
                offered=offered,
                verified=offered,
                input_price=0.0 if offered else None,
                output_price=0.0 if offered else None,
                source="gemini-free-tier-terms",
                context=_context_tokens(item.get("inputTokenLimit")),
            )
        )
    return entries


def refresh_source(
    provider: str,
    terms: TermsFile,
    *,
    api_key: str = "",
    serve_evidence: Mapping[str, str] | None = None,
    get: GetJson | None = None,
    fetched_at: datetime | None = None,
) -> FetchedCatalog:
    """Fetch and parse one provider; raises SourceError when it fails."""
    if provider == "openrouter":
        raw = fetch_openrouter_models(get)
        key_payload = fetch_openrouter_key(api_key, get) if api_key else None
        entries = parse_openrouter_models(raw, key_payload, terms)
    elif provider == "opencode":
        raw = fetch_opencode_models(get)
        entries = parse_opencode_models(raw, terms.opencode_free_models, terms, serve_evidence)
    elif provider == "groq":
        raw = fetch_groq_models(api_key, get)
        entries = parse_groq_models(raw, terms)
    elif provider == "nvidia":
        raw = fetch_nvidia_models(api_key, get)
        entries = parse_nvidia_models(raw, terms)
    elif provider == "gemini":
        raw = fetch_gemini_models(api_key, get)
        entries = parse_gemini_models(raw, terms)
    else:
        raise SourceError(provider, "parse", f"unknown provider: {provider}")
    return FetchedCatalog(
        provider=provider,
        entries=tuple(entries),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        ttl=SOURCE_TTLS[provider],
    )


def build_catalog(fetched: list[FetchedCatalog], *, generator: str = "model_routing.sources") -> CatalogDocument:
    """Concatenate parsed entries; families wait for TASK-2026-00290."""
    entries: list[CatalogEntry] = []
    for catalog in fetched:
        entries.extend(catalog.entries)
    return CatalogDocument(
        generated_at=datetime.now(timezone.utc),
        generator=generator,
        entries=entries,
    )


def merge_with_previous(
    previous: CatalogDocument | None,
    fetched: list[FetchedCatalog],
    failed: Mapping[str, str],
) -> tuple[CatalogDocument, bool, str | None]:
    """Keep the last good deployments for dead sources instead of emptying.

    Rescue runs per deployment: a unified previous entry may mix
    providers, so carrying it whole would either drop live routes or
    collide with the fresh ones.
    """
    doc = build_catalog(fetched)
    if not failed:
        return doc, False, None
    reason = "sources failed: " + ", ".join(f"{name} ({failed[name]})" for name in sorted(failed))
    if previous is None:
        return doc.model_copy(update={"stale": True, "stale_reason": reason}), True, reason
    failed_providers = set(failed)
    carried = []
    for entry in previous.entries:
        rescued = [deployment for deployment in entry.deployments
                   if deployment.provider in failed_providers]
        if rescued:
            carried.append(entry.model_copy(update={"deployments": rescued}))
    merged = doc.model_copy(update={"entries": [*doc.entries, *carried]})
    try:
        merged = CatalogDocument.model_validate(merged.model_dump())
    except ValidationError as exc:
        raise SourceError("merge", "parse", "carried entries collide with fresh ones") from exc
    return merged.model_copy(update={"stale": True, "stale_reason": reason}), True, reason
