"""Transactional availability store and §4.2 contracts (TASK-2026-00291).

SQLite at ``availability.sqlite`` (SPEC §9.3): round-atomic reservations
with family uniqueness, deployment cooldowns, provider blocks, approximate
per-provider counters, the persistent error registry and audit events.
Event-driven: recording an error applies its cooldown or block at once;
there is no polling or sweeping (expiry is lazy, checked on access).
External consumers (TASK-2026-00295) share the file without importing
OpenCEO. No prompts, responses or credentials are ever stored.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from model_routing.errors import ClassifiedError, cooldown_seconds, sanitize_excerpt
from model_routing.models import (
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    RouterPolicy,
    is_usable,
)
from model_routing.paths import availability_db_path

__all__ = [
    "AVAILABILITY_SCHEMA_VERSION",
    "PROFILE_REQUIREMENTS",
    "AvailabilityStore",
    "Candidate",
    "EligibleDeployment",
    "ErrorRecord",
    "Exclusions",
    "NoEligibleCandidate",
    "ProfileRequirements",
    "Reservation",
    "ReservationConflict",
    "backups_of",
    "get_next_candidate",
    "list_eligible_models",
    "meets_profile",
    "record_provider_error",
    "release_reservation",
    "reserve_model",
    "write_audit_event",
]

AVAILABILITY_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    run_id TEXT NOT NULL,
    round TEXT NOT NULL,
    slot TEXT NOT NULL,
    canonical_model_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    exception TEXT,
    PRIMARY KEY (run_id, round, slot)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservation_family
    ON reservations (run_id, round, family_id) WHERE exception IS NULL;
CREATE TABLE IF NOT EXISTS cooldowns (
    deployment_id TEXT PRIMARY KEY,
    until_ts REAL NOT NULL,
    reason TEXT NOT NULL,
    retry_after_s INTEGER,
    failures INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS provider_blocks (
    provider TEXT PRIMARY KEY,
    until_ts REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disabled_routes (
    deployment_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    provider TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    model TEXT NOT NULL,
    error_class TEXT NOT NULL,
    retry_after_s INTEGER,
    action TEXT NOT NULL,
    run_id TEXT NOT NULL,
    excerpt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class ReservationConflict(RuntimeError):
    """A slot or family is already reserved for this run and round."""


class NoEligibleCandidate(RuntimeError):
    """No candidate survives exclusions, cooldowns, blocks and quality."""


@dataclass(frozen=True)
class Reservation:
    """One held ``run_id + round + slot`` triple (SPEC §9.3)."""

    run_id: str
    round: str
    slot: str
    canonical_model_id: str
    family_id: str
    deployment_id: str
    expires_at: float
    exception: str | None = None


@dataclass(frozen=True)
class ErrorRecord:
    """One registry row: codes and reasons, never content."""

    id: int
    ts: float
    provider: str
    deployment_id: str
    model: str
    error_class: str
    retry_after_s: int | None
    action: str
    run_id: str
    excerpt: str


@dataclass(frozen=True)
class Candidate:
    """One ordered fallback option; order comes from the ranking (00292)."""

    deployment_id: str
    family_id: str
    canonical_model_id: str
    provider: str
    quality: float | None = None


@dataclass(frozen=True)
class EligibleDeployment:
    """One catalog deployment clearing every availability gate."""

    family_id: str
    canonical_model_id: str
    deployment_id: str
    provider: str


@dataclass(frozen=True)
class Exclusions:
    """Already-tried identities to skip while walking candidates."""

    families: frozenset[str] = frozenset()
    canonicals: frozenset[str] = frozenset()
    deployments: frozenset[str] = frozenset()
    providers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProfileRequirements:
    """Hard gates BEFORE ordering (SPEC §8.3); unknown values fail closed."""

    needs_tools: bool = False
    min_context_tokens: int | None = None
    min_tpm: int | None = None
    min_rpd: int | None = None
    confidentiality: Confidentiality | None = None


PROFILE_REQUIREMENTS: dict[str, ProfileRequirements] = {
    "free-review": ProfileRequirements(needs_tools=True),
    "free-research": ProfileRequirements(needs_tools=True),
    "free-general": ProfileRequirements(),
}


def _utc_day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


class AvailabilityStore:
    """SQLite availability authority; one instance per thread, shared file across processes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or availability_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), timeout=30.0)
        self._db.execute("PRAGMA busy_timeout = 30000")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {AVAILABILITY_SCHEMA_VERSION}")

    def close(self) -> None:
        """Release the database handle."""
        self._db.close()

    def _purge_expired(self, now: float) -> int:
        cursor = self._db.execute(
            "DELETE FROM reservations WHERE expires_at <= ?", (now,))
        self._db.execute("DELETE FROM cooldowns WHERE until_ts <= ?", (now,))
        self._db.execute("DELETE FROM provider_blocks WHERE until_ts <= ?", (now,))
        return cursor.rowcount

    def reserve(
        self,
        run_id: str,
        round: str,
        slot: str,
        canonical_model_id: str,
        family_id: str,
        deployment_id: str,
        *,
        ttl_s: float = 3600.0,
        exception: str | None = None,
        now: float | None = None,
    ) -> Reservation:
        """Hold one slot; a taken family needs a recorded exception (T04)."""
        current = time.time() if now is None else now
        try:
            with self._db:
                self._db.execute("DELETE FROM reservations WHERE expires_at <= ?", (current,))
                self._db.execute(
                    "INSERT INTO reservations "
                    "(run_id, round, slot, canonical_model_id, family_id,"
                    " deployment_id, created_at, expires_at, exception)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, round, slot, canonical_model_id, family_id,
                     deployment_id, current, current + ttl_s, exception),
                )
        except sqlite3.IntegrityError as exc:
            raise ReservationConflict(
                f"slot {slot} or family {family_id} already reserved"
                f" for {run_id}/{round}") from exc
        return Reservation(run_id, round, slot, canonical_model_id,
                           family_id, deployment_id, current + ttl_s, exception)

    def reserve_round(
        self,
        run_id: str,
        round: str,
        items: list[tuple[str, str, str, str]],
        *,
        ttl_s: float = 3600.0,
        now: float | None = None,
    ) -> list[Reservation]:
        """Reserve whole rounds atomically; any conflict rolls everything back."""
        current = time.time() if now is None else now
        families = [family for _, _, family, _ in items]
        if len(set(families)) != len(families):
            raise ReservationConflict(f"duplicate family inside round {run_id}/{round}")
        try:
            with self._db:
                self._db.execute("DELETE FROM reservations WHERE expires_at <= ?", (current,))
                for slot, canonical, family, deployment in items:
                    self._db.execute(
                        "INSERT INTO reservations "
                        "(run_id, round, slot, canonical_model_id, family_id,"
                        " deployment_id, created_at, expires_at, exception)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                        (run_id, round, slot, canonical, family,
                         deployment, current, current + ttl_s),
                    )
        except sqlite3.IntegrityError as exc:
            raise ReservationConflict(
                f"round {run_id}/{round} conflicts with an existing reservation") from exc
        return [Reservation(run_id, round, slot, canonical, family, deployment, current + ttl_s)
                for slot, canonical, family, deployment in items]

    def release(self, run_id: str, round: str, slot: str) -> bool:
        """Free one slot; True when a reservation was actually held."""
        with self._db:
            cursor = self._db.execute(
                "DELETE FROM reservations WHERE run_id = ? AND round = ? AND slot = ?",
                (run_id, round, slot),
            )
        return cursor.rowcount > 0

    def reservations(self, run_id: str, round: str, *, now: float | None = None) -> list[Reservation]:
        """Active reservations for one round; expired ones read as absent."""
        current = time.time() if now is None else now
        rows = self._db.execute(
            "SELECT slot, canonical_model_id, family_id, deployment_id,"
            " expires_at, exception FROM reservations"
            " WHERE run_id = ? AND round = ? AND expires_at > ? ORDER BY slot",
            (run_id, round, current),
        ).fetchall()
        return [Reservation(run_id, round, slot, canonical, family, deployment, expires, exc)
                for slot, canonical, family, deployment, expires, exc in rows]

    def set_cooldown(
        self,
        deployment_id: str,
        reason: str,
        *,
        seconds: float,
        retry_after_s: int | None = None,
        now: float | None = None,
    ) -> None:
        """Cool one deployment; repeated failures stretch the window."""
        current = time.time() if now is None else now
        with self._db:
            row = self._db.execute(
                "SELECT failures FROM cooldowns WHERE deployment_id = ?",
                (deployment_id,)).fetchone()
            failures = (row[0] if row else 0) + 1
            window = cooldown_seconds(failures, retry_after_s)
            self._db.execute(
                "INSERT INTO cooldowns (deployment_id, until_ts, reason, retry_after_s, failures)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (deployment_id) DO UPDATE SET until_ts = excluded.until_ts,"
                " reason = excluded.reason, retry_after_s = excluded.retry_after_s,"
                " failures = excluded.failures",
                (deployment_id, current + max(seconds, window), reason, retry_after_s, failures),
            )

    def cooldown_for(self, deployment_id: str, *, now: float | None = None) -> dict | None:
        """Active cooldown row, if any; expired rows read as absent."""
        current = time.time() if now is None else now
        row = self._db.execute(
            "SELECT until_ts, reason, retry_after_s, failures FROM cooldowns"
            " WHERE deployment_id = ? AND until_ts > ?",
            (deployment_id, current)).fetchone()
        if row is None:
            return None
        return {"until_ts": row[0], "reason": row[1],
                "retry_after_s": row[2], "failures": row[3]}

    def block_provider(self, provider: str, reason: str, *, seconds: float,
                       now: float | None = None) -> None:
        """Disable a provider until its window resets (daily quota, bad credential)."""
        current = time.time() if now is None else now
        with self._db:
            self._db.execute(
                "INSERT INTO provider_blocks (provider, until_ts, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (provider) DO UPDATE SET until_ts = excluded.until_ts,"
                " reason = excluded.reason",
                (provider, current + seconds, reason),
            )

    def provider_blocked(self, provider: str, *, now: float | None = None) -> dict | None:
        """Active provider block, if any; expired rows read as absent."""
        current = time.time() if now is None else now
        row = self._db.execute(
            "SELECT until_ts, reason FROM provider_blocks WHERE provider = ? AND until_ts > ?",
            (provider, current)).fetchone()
        if row is None:
            return None
        return {"until_ts": row[0], "reason": row[1]}

    def disable_route(self, deployment_id: str, reason: str,
                      *, now: float | None = None) -> None:
        """Block one route now and keep it blocked (billing, not-found)."""
        current = time.time() if now is None else now
        with self._db:
            self._db.execute(
                "INSERT INTO disabled_routes (deployment_id, reason, recorded_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (deployment_id) DO UPDATE SET reason = excluded.reason,"
                " recorded_at = excluded.recorded_at",
                (deployment_id, reason, current),
            )

    def route_disabled(self, deployment_id: str) -> str | None:
        """Disable reason, if this route is blocked."""
        row = self._db.execute(
            "SELECT reason FROM disabled_routes WHERE deployment_id = ?",
            (deployment_id,)).fetchone()
        return row[0] if row else None

    def bump_counter(self, provider: str, *, now: float | None = None) -> int:
        """Approximate per-provider daily counter; the day rolls lazily."""
        current = time.time() if now is None else now
        day = _utc_day(current)
        with self._db:
            row = self._db.execute(
                "SELECT day, count FROM counters WHERE provider = ?", (provider,)).fetchone()
            count = row[1] + 1 if row and row[0] == day else 1
            self._db.execute(
                "INSERT INTO counters (provider, day, count) VALUES (?, ?, ?)"
                " ON CONFLICT (provider) DO UPDATE SET day = excluded.day,"
                " count = excluded.count",
                (provider, day, count),
            )
        return count

    def record_error(
        self,
        *,
        provider: str,
        deployment_id: str,
        model: str,
        classified: ClassifiedError,
        run_id: str,
        excerpt: str = "",
        now: float | None = None,
    ) -> ErrorRecord:
        """Persist one error and apply its action at once (event-driven)."""
        current = time.time() if now is None else now
        stored = sanitize_excerpt(excerpt or classified.excerpt)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO errors (ts, provider, deployment_id, model, error_class,"
                " retry_after_s, action, run_id, excerpt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (current, provider, deployment_id, model, classified.error_class,
                 classified.retry_after_s, classified.action, run_id, stored),
            )
            error_id = cursor.lastrowid or 0
            self._apply_action(provider, deployment_id, classified, current)
        return ErrorRecord(error_id, current, provider, deployment_id, model,
                           classified.error_class, classified.retry_after_s,
                           classified.action, run_id, stored)

    def _apply_action(self, provider: str, deployment_id: str,
                      classified: ClassifiedError, now: float) -> None:
        reason = f"{classified.error_class}: {classified.action}"
        # retry_next cools the failed deployment too, so the next
        # candidate is actually next; same-deployment retries stay
        # bounded by RetryBudget instead of the store.
        if classified.action in ("cooldown_deployment", "retry_next"):
            row = self._db.execute(
                "SELECT failures FROM cooldowns WHERE deployment_id = ?",
                (deployment_id,)).fetchone()
            failures = (row[0] if row else 0) + 1
            window = cooldown_seconds(failures, classified.retry_after_s)
            self._db.execute(
                "INSERT INTO cooldowns (deployment_id, until_ts, reason, retry_after_s, failures)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (deployment_id) DO UPDATE SET until_ts = excluded.until_ts,"
                " reason = excluded.reason, retry_after_s = excluded.retry_after_s,"
                " failures = excluded.failures",
                (deployment_id, now + window, reason, classified.retry_after_s, failures),
            )
        elif classified.action == "disable_provider":
            self._db.execute(
                "INSERT INTO provider_blocks (provider, until_ts, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (provider) DO UPDATE SET until_ts = excluded.until_ts,"
                " reason = excluded.reason",
                (provider, now + 86400.0, reason),
            )
        elif classified.action == "disable_credential":
            self._db.execute(
                "INSERT INTO provider_blocks (provider, until_ts, reason) VALUES (?, ?, ?)"
                " ON CONFLICT (provider) DO UPDATE SET until_ts = excluded.until_ts,"
                " reason = excluded.reason",
                (provider, now + 86400.0, reason),
            )
        elif classified.action == "block_route":
            self._db.execute(
                "INSERT INTO disabled_routes (deployment_id, reason, recorded_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (deployment_id) DO UPDATE SET reason = excluded.reason,"
                " recorded_at = excluded.recorded_at",
                (deployment_id, reason, now),
            )

    def errors_since(self, since_ts: float) -> list[ErrorRecord]:
        """Registry rows at or after ``since_ts``, oldest first."""
        rows = self._db.execute(
            "SELECT id, ts, provider, deployment_id, model, error_class,"
            " retry_after_s, action, run_id, excerpt FROM errors"
            " WHERE ts >= ? ORDER BY ts, id",
            (since_ts,)).fetchall()
        return [ErrorRecord(*row) for row in rows]

    def write_audit(self, run_id: str, event: str, detail: str = "",
                    *, now: float | None = None) -> None:
        """Append one audit event; callers pass codes, never content."""
        current = time.time() if now is None else now
        with self._db:
            self._db.execute(
                "INSERT INTO audit (ts, run_id, event, detail) VALUES (?, ?, ?, ?)",
                (current, run_id, event, sanitize_excerpt(detail)),
            )

    def audit_since(self, run_id: str, since_ts: float) -> list[tuple[float, str, str]]:
        """Audit events for one run at or after ``since_ts``."""
        return self._db.execute(
            "SELECT ts, event, detail FROM audit WHERE run_id = ? AND ts >= ? ORDER BY ts, id",
            (run_id, since_ts)).fetchall()

    def recover(self, *, now: float | None = None) -> tuple[int, list[Reservation]]:
        """Crash recovery: drop expired holds, report the live ones."""
        current = time.time() if now is None else now
        with self._db:
            purged = self._purge_expired(current)
            rows = self._db.execute(
                "SELECT run_id, round, slot, canonical_model_id, family_id,"
                " deployment_id, expires_at, exception FROM reservations"
                " ORDER BY run_id, round, slot").fetchall()
        live = [Reservation(*row) for row in rows]
        return purged, live


def meets_profile(entry: CatalogEntry, deployment: Deployment,
                  requirements: ProfileRequirements) -> tuple[bool, str]:
    """Hard gates BEFORE ordering (SPEC §8.3); unknown values fail closed."""
    capabilities: Capabilities = entry.capabilities
    if requirements.needs_tools and not capabilities.tools:
        return False, "profile needs tools; deployment is not tools-capable"
    if requirements.min_context_tokens is not None and (
            capabilities.context_tokens is None
            or capabilities.context_tokens < requirements.min_context_tokens):
        return False, "context below the profile floor"
    if requirements.min_tpm is not None and (
            deployment.limits.tpm is None or deployment.limits.tpm < requirements.min_tpm):
        return False, "TPM below the profile floor"
    if requirements.min_rpd is not None and (
            deployment.limits.rpd is None or deployment.limits.rpd < requirements.min_rpd):
        return False, "RPD below the profile floor"
    if requirements.confidentiality is not None and (
            requirements.confidentiality not in deployment.approved_confidentiality):
        return False, "confidentiality not approved for this deployment"
    return True, ""


def list_eligible_models(
    profile: str,
    catalog: CatalogDocument,
    store: AvailabilityStore,
    *,
    policy: RouterPolicy | None = None,
    requirements: ProfileRequirements | None = None,
    now: float | None = None,
) -> list[EligibleDeployment]:
    """Usable, healthy, profile-fitting deployments minus cooldowns and blocks.

    Catalog order is preserved; per-profile ordering is the ranking
    (TASK-2026-00292), which reuses these same hard gates.
    """
    active = policy or RouterPolicy()
    if requirements is None:
        if profile not in PROFILE_REQUIREMENTS:
            raise ValueError(f"unknown profile: {profile!r} (known: {sorted(PROFILE_REQUIREMENTS)})")
        gates = PROFILE_REQUIREMENTS[profile]
    else:
        gates = requirements
    eligible: list[EligibleDeployment] = []
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if not is_usable(deployment, active) or not deployment.healthy:
                continue
            ok, _reason = meets_profile(entry, deployment, gates)
            if not ok:
                continue
            if store.cooldown_for(deployment.deployment_id, now=now) is not None:
                continue
            if store.route_disabled(deployment.deployment_id) is not None:
                continue
            if store.provider_blocked(deployment.provider, now=now) is not None:
                continue
            eligible.append(EligibleDeployment(
                entry.family_id, entry.canonical_model_id,
                deployment.deployment_id, deployment.provider))
    return eligible


def reserve_model(
    store: AvailabilityStore,
    run_id: str,
    round: str,
    slot: str,
    canonical_model_id: str,
    family_id: str,
    deployment_id: str,
    *,
    ttl_s: float = 3600.0,
    exception: str | None = None,
    now: float | None = None,
) -> Reservation:
    """Hold one slot for a run and round (SPEC §4.2)."""
    return store.reserve(run_id, round, slot, canonical_model_id, family_id,
                         deployment_id, ttl_s=ttl_s, exception=exception, now=now)


def release_reservation(store: AvailabilityStore, run_id: str, round: str, slot: str) -> bool:
    """Free one held slot (SPEC §4.2)."""
    return store.release(run_id, round, slot)


def record_provider_error(
    store: AvailabilityStore,
    *,
    provider: str,
    deployment_id: str,
    model: str,
    classified: ClassifiedError,
    run_id: str,
    excerpt: str = "",
    now: float | None = None,
) -> ErrorRecord:
    """Register one provider failure and apply its action (SPEC §4.2)."""
    return store.record_error(
        provider=provider, deployment_id=deployment_id, model=model,
        classified=classified, run_id=run_id, excerpt=excerpt, now=now)


def get_next_candidate(
    candidates: list[Candidate],
    store: AvailabilityStore,
    *,
    exclusions: Exclusions | None = None,
    minimum_quality: float | None = None,
    now: float | None = None,
) -> Candidate:
    """First candidate in ranking order surviving every availability gate.

    Same-canonical candidates across providers are backups of one model,
    never two independent opinions (T05): exclude the canonical to skip
    the whole backup chain.
    """
    skipped = exclusions or Exclusions()
    for candidate in candidates:
        if candidate.family_id in skipped.families:
            continue
        if candidate.canonical_model_id in skipped.canonicals:
            continue
        if candidate.deployment_id in skipped.deployments:
            continue
        if candidate.provider in skipped.providers:
            continue
        if minimum_quality is not None and (
                candidate.quality is None or candidate.quality < minimum_quality):
            continue
        if store.cooldown_for(candidate.deployment_id, now=now) is not None:
            continue
        if store.route_disabled(candidate.deployment_id) is not None:
            continue
        if store.provider_blocked(candidate.provider, now=now) is not None:
            continue
        return candidate
    raise NoEligibleCandidate("no candidate survives exclusions, cooldowns and blocks")


def backups_of(candidate: Candidate, candidates: list[Candidate]) -> list[Candidate]:
    """Same base model via other providers: backups, not opinions (T05)."""
    return [other for other in candidates
            if other.canonical_model_id == candidate.canonical_model_id
            and other.deployment_id != candidate.deployment_id]


def write_audit_event(store: AvailabilityStore, run_id: str, event: str,
                      detail: str = "", *, now: float | None = None) -> None:
    """Append one audit event without prompt or response content (SPEC §4.2)."""
    store.write_audit(run_id, event, detail, now=now)
