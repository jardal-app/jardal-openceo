"""Live-test harness: zero-cost guards and cassettes (TASK-2026-00287).

Live tests run only with ``FREE_ROUTER_LIVE=1`` (see the ``live`` pytest
marker) and record sanitized fixtures. Errors never carry credentials:
URLs are stripped of query strings and headers are dropped.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from model_routing.models import CatalogDocument
from model_routing.publish import atomic_write_json

__all__ = [
    "LIVE_ENV_VAR",
    "LiveError",
    "LiveGuardError",
    "LiveRateLimited",
    "OpenRouterKeyInfo",
    "assert_free_verified",
    "assert_no_auth",
    "assert_usage_unchanged",
    "http_get_json",
    "live_enabled",
    "load_fixture",
    "parse_openrouter_key_info",
    "record_fixture",
    "redact_text",
    "scan_for_secrets",
    "today_utc",
]

LIVE_ENV_VAR = "FREE_ROUTER_LIVE"

_AUTH_HEADERS = ("authorization", "x-api-key", "api-key", "x-goog-api-key")

_KEY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("openai-like", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}"),
    ("nvidia", r"(?<![A-Za-z0-9])nvapi-[A-Za-z0-9_\-]{8,}"),
    ("groq", r"(?<![A-Za-z0-9])gsk_[A-Za-z0-9]{8,}"),
    ("google", r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_\-]{8,}"),
    ("huggingface", r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{8,}"),
    ("bearer", r"(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9_\-\.~+/=]{8,}"),
)

_REDACTED = "[REDACTED]"


class LiveError(RuntimeError):
    """Live harness failure; carries no credentials."""


class LiveGuardError(LiveError):
    """A zero-cost guard tripped."""


class LiveRateLimited(LiveError):
    """A real 429: stop live calls for this provider until tomorrow."""

    def __init__(self, provider: str, safe_url: str, retry_after: str | None) -> None:
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"live 429 for {provider} at {safe_url} (retry-after: {retry_after})")


@dataclass(frozen=True)
class OpenRouterKeyInfo:
    """Credit usage and free-quota counters from ``GET /api/v1/key``."""

    usage: float
    free_limit: int | None
    free_used: int | None
    free_remaining: int | None


def live_enabled() -> bool:
    """Live tests run only with ``FREE_ROUTER_LIVE=1``."""
    return os.environ.get(LIVE_ENV_VAR) == "1"


def today_utc() -> str:
    """Current UTC calendar day."""
    return datetime.now(timezone.utc).date().isoformat()


def assert_no_auth(headers: Mapping[str, str]) -> None:
    """OpenCode free calls carry no credential; anything else fails loudly."""
    present = [name for name in headers if name.lower() in _AUTH_HEADERS]
    if present:
        raise LiveGuardError(f"credentialed header on a credential-free call: {', '.join(sorted(present))}")


def assert_free_verified(deployment_id: str, catalog: CatalogDocument) -> None:
    """Only catalog deployments with ``free_verified`` may be exercised live."""
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if deployment.deployment_id == deployment_id:
                if deployment.free_verified:
                    return
                raise LiveGuardError(f"deployment is not free_verified: {deployment_id}")
    raise LiveGuardError(f"deployment not in catalog: {deployment_id}")


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """Replace configured values and key-like patterns before recording."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, _REDACTED)
    for _name, pattern in _KEY_PATTERNS:
        redacted = re.sub(pattern, _REDACTED, redacted)
    return redacted


def scan_for_secrets(text: str, secrets: Iterable[str]) -> list[str]:
    """Describe secret leaks without repeating values."""
    findings: list[str] = []
    for secret in secrets:
        if secret and secret in text:
            findings.append(f"embedded configured secret ({len(secret)} chars)")
    for name, pattern in _KEY_PATTERNS:
        for match in re.finditer(pattern, text):
            findings.append(f"key-like pattern {name} at offset {match.start()}")
    return findings


def _fixture_name(name: str) -> str:
    if "/" in name or "\\" in name or name != Path(name).name or not name.endswith(".json"):
        raise LiveError(f"unsafe fixture name: {name!r}")
    return name


def record_fixture(
    name: str, data: str | bytes, fixtures_dir: Path, secrets: Iterable[str] = ()
) -> Path:
    """Persist a sanitized response; auth headers are never recorded."""
    payload = data.decode("utf-8") if isinstance(data, bytes) else data
    path = fixtures_dir / _fixture_name(name)
    atomic_write_json(path, redact_text(payload, secrets))
    return path


def load_fixture(name: str, fixtures_dir: Path) -> str:
    """Replay a recorded response."""
    try:
        return (fixtures_dir / _fixture_name(name)).read_text(encoding="utf-8")
    except OSError as exc:
        raise LiveError(f"fixture missing or unreadable: {name}") from exc


def _safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.split("@")[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def http_get_json(
    url: str, headers: Mapping[str, str] | None = None, timeout: float = 30.0
) -> dict:
    """Minimal metadata GET; errors name the endpoint, never credentials."""
    safe = _safe_url(url)
    merged = dict(headers or {})
    # Cloudflare rejects the default Python UA (1010) on documented API
    # endpoints (verified 2026-10-23); identify the harness instead.
    merged.setdefault("User-Agent", "free-router-live/1")
    request = urllib.request.Request(url, headers=merged, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LiveRateLimited("unknown", safe, exc.headers.get("Retry-After")) from exc
        raise LiveError(f"GET {safe} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveError(f"GET {safe} failed: {type(exc).__name__}") from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise LiveError(f"GET {safe} returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise LiveError(f"GET {safe} returned a non-object payload")
    return payload


def parse_openrouter_key_info(payload: dict) -> OpenRouterKeyInfo:
    """Extract usage and free-quota counters from ``GET /api/v1/key``."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LiveError("OpenRouter /api/v1/key returned no data object")
    try:
        usage = float(data.get("usage", 0))
    except (TypeError, ValueError) as exc:
        raise LiveError("OpenRouter /api/v1/key returned non-numeric usage") from exc
    free = data.get("free_model_daily_requests")
    if not isinstance(free, dict):
        return OpenRouterKeyInfo(usage=usage, free_limit=None, free_used=None, free_remaining=None)
    return OpenRouterKeyInfo(
        usage=usage,
        free_limit=free.get("limit"),
        free_used=free.get("used"),
        free_remaining=free.get("remaining"),
    )


def assert_usage_unchanged(before: float, after: float) -> None:
    """Credit usage must not move during free-only live tests."""
    if before != after:
        raise LiveGuardError(f"OpenRouter credit usage changed: {before} -> {after}")
