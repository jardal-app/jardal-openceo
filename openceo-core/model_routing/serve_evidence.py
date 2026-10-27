"""Native serve evidence for group A free verification (TASK-2026-00315).

OpenCode Free only serves from the native CLI (addendum v1.2 §1), so
``parse_opencode_models`` marks a deployment ``free_verified`` only
with fresh serve evidence: proof the model served from the native
CLI at zero cost within the last 7 days. No production path
recorded it, so every group A deployment stayed unverified.

This module is the producer: every native zero-cost serve records
one entry here, keyed by bare OpenCode model id, and the refresh
pipeline feeds the file back into ``refresh_source``. Recording is
best-effort and never raises: evidence must never break serving.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_routing.paths import serve_evidence_path

__all__ = [
    "NATIVE_PROVIDER",
    "SERVE_EVIDENCE_SCHEMA_VERSION",
    "evidence_path",
    "is_fresh",
    "load_serve_evidence",
    "record_native_serve",
]

NATIVE_PROVIDER = "opencode"

SERVE_EVIDENCE_SCHEMA_VERSION = 1

SERVE_EVIDENCE_TTL = timedelta(days=7)


def evidence_path(path: Path | str | None = None) -> Path:
    """State file holding the evidence map; SPEC §6.1 state dir."""
    if path is not None:
        return Path(path)
    return serve_evidence_path()


def _moment(value: str) -> datetime | None:
    """Aware parsed timestamp; naive or malformed values fail closed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def is_fresh(observed_at: str, ttl: timedelta = SERVE_EVIDENCE_TTL) -> bool:
    """Fresh means an aware timestamp within ``ttl`` of now."""
    moment = _moment(observed_at)
    if moment is None:
        return False
    return timedelta(0) <= datetime.now(timezone.utc) - moment <= ttl


def _normalize_model_id(provider: str, model_id: str) -> str | None:
    """Bare OpenCode model id; other providers never count as evidence."""
    if provider != NATIVE_PROVIDER:
        return None
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    bare = model_id.strip()
    if bare.startswith(f"{NATIVE_PROVIDER}/"):
        bare = bare[len(NATIVE_PROVIDER) + 1:]
    if not bare or "/" in bare:
        return None
    return bare


def load_serve_evidence(path: Path | str | None = None) -> dict[str, str]:
    """Evidence map ``{model_id: iso_timestamp}``; fail-open to empty.

    A missing or corrupt file means no evidence, never an error: the
    catalog then marks group A unverified until a native serve lands.
    Malformed entries are skipped, never trusted.
    """
    target = evidence_path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    evidence: dict[str, str] = {}
    for model_id, observed_at in entries.items():
        if _normalize_model_id(NATIVE_PROVIDER, model_id) != model_id:
            continue
        if _moment(observed_at) is None:
            continue
        evidence[model_id] = observed_at
    return evidence


def record_native_serve(
    provider: str,
    model_id: str,
    *,
    observed_at: str | None = None,
    path: Path | str | None = None,
    ttl: timedelta = SERVE_EVIDENCE_TTL,
) -> str | None:
    """Record one native zero-cost serve; never raises.

    Returns the stored timestamp, or None when the input is not
    native evidence (wrong provider, bad id) or the write failed.
    Stale entries are pruned on write so the file stays bounded.
    """
    bare = _normalize_model_id(provider, model_id)
    if bare is None:
        return None
    if observed_at is None:
        stamp = datetime.now(timezone.utc).isoformat()
    else:
        if _moment(observed_at) is None:
            return None
        stamp = observed_at
    target = evidence_path(path)
    try:
        current = load_serve_evidence(target)
        current[bare] = stamp
        fresh = {
            model: seen for model, seen in current.items()
            if is_fresh(seen, ttl)
        }
        payload = {
            "schema_version": SERVE_EVIDENCE_SCHEMA_VERSION,
            "entries": fresh,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except (OSError, ValueError):
        return None
    return stamp
