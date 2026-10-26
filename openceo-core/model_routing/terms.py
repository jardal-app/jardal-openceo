"""Hand-verified free-tier records, never inferred (TASK-2026-00289).

Providers expose free status differently: OpenRouter publishes prices,
OpenCode publishes a docs pricing table with no machine API, and Groq,
Gemini and NVIDIA gate free usage at the account level. This module loads
the versioned manual attestation (``data/provider_terms.yaml``) that
fetchers consult before marking any deployment ``free_verified``.
A missing, stale or incomplete record fails closed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from model_routing.models import Confidentiality, DataPolicy

__all__ = [
    "TERMS_SCHEMA_VERSION",
    "OpenCodeFreeList",
    "ProviderTerms",
    "TermsError",
    "TermsFile",
    "default_terms_path",
    "is_fresh",
    "load_terms",
    "terms_for",
]

TERMS_SCHEMA_VERSION = 1


class TermsError(RuntimeError):
    """Terms file missing, stale or invalid; callers must fail closed."""


class ProviderTerms(BaseModel):
    """One manual free-tier attestation with date and source."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    free_tier: bool
    requires_payment_method: bool
    one_time_credit_only: bool
    data_policy: DataPolicy
    retention: str
    confidentiality_allowed: list[Confidentiality]
    paid_addons: list[str] = []
    source: str
    source_note: str = ""
    verified_at: str
    basis: str

    @field_validator("verified_at")
    @classmethod
    def _check_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"verified_at is not an ISO date: {value!r}") from exc
        return value


class OpenCodeFreeList(BaseModel):
    """Docs-table transcription of OpenCode's Free set (no pricing API)."""

    model_config = ConfigDict(extra="forbid")

    source: str
    verified_at: str
    basis: str = ""
    model_ids: list[str]

    @field_validator("verified_at")
    @classmethod
    def _check_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"verified_at is not an ISO date: {value!r}") from exc
        return value


class TermsFile(BaseModel):
    """Whole terms document; one entry per provider plus the OpenCode list."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = TERMS_SCHEMA_VERSION
    default_ttl_days: int = 7
    terms: list[ProviderTerms]
    opencode_free_models: OpenCodeFreeList


def default_terms_path() -> Path:
    """Versioned terms file inside the package."""
    return Path(__file__).resolve().parent / "data" / "provider_terms.yaml"


def load_terms(path: Path | None = None) -> TermsFile:
    """Load and validate the manual records; never silently defaults."""
    target = path or default_terms_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TermsError(f"terms file missing or unreadable: {target}") from exc
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TermsError(f"terms file {target} is not valid YAML") from exc
    if not isinstance(data, dict):
        raise TermsError(f"terms file {target} is empty or not a mapping")
    try:
        terms = TermsFile.model_validate(data)
    except ValidationError as exc:
        raise TermsError(f"terms file {target} invalid: {exc.errors()[0]['msg']}") from exc
    if terms.schema_version != TERMS_SCHEMA_VERSION:
        raise TermsError(f"unsupported terms schema_version: {terms.schema_version}")
    seen = [entry.provider for entry in terms.terms]
    if len(set(seen)) != len(seen):
        raise TermsError("duplicate provider in terms file")
    return terms


def terms_for(terms: TermsFile, provider: str) -> ProviderTerms:
    """Fetch one provider's record or fail closed."""
    for entry in terms.terms:
        if entry.provider == provider:
            return entry
    raise TermsError(f"no manual terms record for provider: {provider}")


def is_fresh(verified_at: str, ttl_days: int, today: date | None = None) -> bool:
    """A record older than its TTL needs human re-verification."""
    if ttl_days < 0:
        return False
    checked = date.fromisoformat(verified_at)
    now = today or date.today()
    return 0 <= (now - checked).days <= ttl_days
