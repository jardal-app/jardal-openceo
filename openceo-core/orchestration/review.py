"""Review rounds orchestration primitives (design §7, F3).

Pool selection by slot priority and family, quorum-2 of distinct families,
read-only detached worktrees with write detection, pointer review requests,
``FIX_<r>`` verdict parsing and the arbitration trigger. The deterministic
finding pipeline lives in :mod:`orchestration.findings`; the round flows
that call these helpers live in the Tier S single-ticket cycle and in
:mod:`objectives.engine` (final review).

Slot availability persists in ``agent_status``: ``available``,
``cooling_down(until)`` or ``disabled(reason)`` (§7.2). Reactive slots are
used until they fail and the orchestrator classifies the error against the
slot's ``limit_patterns`` / ``expired_patterns``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from catalog import ENGINE_ACCOUNTS
from orchestration.findings import (
    Finding,
    VerifiedFinding,
    _anchor_text,  # rendering only; anchors stay opaque otherwise
)
from orchestration.knowledge import with_preamble


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class ReviewerConfigError(ValueError):
    """A review slot entry is misconfigured; the message says why."""


@dataclass(frozen=True)
class ReviewSlot:
    """One ``(cli, model)`` review slot (§7.1)."""

    label: str
    family: str
    model: str = ""
    # Account-selected slots (00250): the logical Gemini slot carries
    # ``account`` (agyd/agyj/agy2, the account IS the command) instead
    # of a static command; resolved by :func:`with_resolved_command`.
    account: str = ""
    command: tuple[str, ...] = ()
    prompt_via: str = "argv"  # "argv" | "stdin"
    env: dict[str, str] = field(default_factory=dict)
    priority: int = 50
    quota: str = "reactive"  # "reactive" | "polled"
    expires_on: str = ""  # YYYY-MM-DD; disabled without waiting for an error
    limit_patterns: tuple[str, ...] = ()
    expired_patterns: tuple[str, ...] = ()
    extra: bool = False  # never counts toward quorum (§7.3)
    active: bool = True
    # Per-slot reviewer timeout; 0 = the round default
    # (``review.reviewer_timeout_minutes``). TASK-2026-00162.
    timeout_minutes: int = 0
    reserve_only: bool = False
    # Cascade cost tier (TASK-2026-00310): "free" | "own" | "metered".
    cost_tier: str = "metered"
    # Cascade-only slots (paid last resort) never join classic rounds.
    cascade_only: bool = False
    # Premium serves the proven paid list on critical tickets; the tier
    # order never changes (paid stays the last resort either way).
    premium: bool = False


#: Cascade wave order (TASK-2026-00310): free routes first, own flat-rate
#: plans next, metered routes last. Relaxed quorum and blocking follow.
TIER_ORDER = {"free": 0, "own": 1, "metered": 2}

#: Default tier per provider; explicit ``cost_tier`` on the slot wins.
#: Unknown providers land metered (guarded) rather than free.
TIER_BY_PROVIDER = {
    "opencode": "free",
    "gemini": "own",
    "agy": "own",
    "muse": "own",
    "dsh": "metered",
    "openrouter": "metered",
}


#: Providers whose review slots resolve the command from ``account``.
#: The agy account IS the command (00246: agyd/agyj/agy2); entries for
#: these providers carry no static command.
ACCOUNT_COMMAND_PROVIDERS = frozenset({"agy", "gemini"})


def with_resolved_command(
    spec: Mapping[str, Any], *, pick_pool: bool = False,
) -> dict[str, Any]:
    """Copy of a pool entry with the executable command. Fail-closed.

    Account-selected slots resolve ``[account]`` (validated against
    the engine accounts); a static command contradicting the account
    is an explicit error, never a silent pick. Entries without an
    account pass through untouched. Single origin for both the
    selection side (:func:`parse_pool`) and the executor (live
    ``review:`` roles), so both always agree on the account.

    ``account_pool`` (TASK-2026-00413) lists equivalent accounts the
    executor draws from per run (``pick_pool=True``); selection keeps
    the configured default. Pool members validate like the account.
    """
    out = dict(spec)
    account = str(out.get("account") or "").strip()
    if not account:
        return out
    provider = str(out.get("provider") or "").strip().lower()
    if provider not in ACCOUNT_COMMAND_PROVIDERS:
        return out
    known = tuple(ENGINE_ACCOUNTS.get("agy", ()))
    if account not in known:
        raise ReviewerConfigError(
            f"review slot {out.get('label')!r}: unknown agy account "
            f"{account!r} (known: {', '.join(known)})")
    pool = [str(name).strip()
            for name in out.get("account_pool") or []]
    pool = [name for name in pool if name]
    for candidate in pool:
        if candidate not in known:
            raise ReviewerConfigError(
                f"review slot {out.get('label')!r}: unknown pool account "
                f"{candidate!r} (known: {', '.join(known)})")
    if pick_pool and pool:
        account = random.choice(pool)
        out["account"] = account
    command = out.get("command") or []
    if isinstance(command, str):
        command = [command]
    command = [str(token) for token in command]
    if command and command[0] != account:
        raise ReviewerConfigError(
            f"review slot {out.get('label')!r}: command {command[0]!r} "
            f"contradicts account {account!r}")
    out["command"] = [account, *command[1:]] if command else [account]
    return out


def executed_account(slot: ReviewSlot, result_slot: str | None) -> str:
    """Per-attempt account identity (TASK-2026-00413).

    The executor reports the account it ran, else the slot label: a
    reported value differing from the label is a picked pool account
    and wins; otherwise the parsed account stands, so non-pool
    outcomes keep their exact values.
    """
    executed = str(result_slot or "").strip()
    if executed and executed != slot.label:
        return executed
    return slot.account


def parse_pool(raw: Sequence[Mapping[str, Any]] | None) -> list[ReviewSlot]:
    """Parse the global ``reviewers`` list (config_loader ``review``)."""
    slots: list[ReviewSlot] = []
    for entry in raw or []:
        if not isinstance(entry, Mapping):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        family = str(entry.get("family") or "").strip().lower()
        if not label or not family:
            continue
        resolved = with_resolved_command(entry)
        command = resolved.get("command") or []
        if isinstance(command, str):
            command = [command]
        try:
            priority = int(entry.get("priority", 50))
        except (TypeError, ValueError):
            priority = 50
        try:
            timeout_minutes = max(0, int(entry.get("timeout_minutes") or 0))
        except (TypeError, ValueError):
            timeout_minutes = 0
        provider = str(entry.get("provider") or "").strip().lower()
        tier = str(entry.get("cost_tier") or "").strip().lower()
        if not tier:
            tier = TIER_BY_PROVIDER.get(provider, "metered")
        if tier not in TIER_ORDER:
            raise ReviewerConfigError(
                f"review slot {label!r}: unknown cost_tier {tier!r} "
                f"(known: {', '.join(sorted(TIER_ORDER))})")
        slots.append(ReviewSlot(
            label=label, family=family,
            model=str(entry.get("model") or ""),
            account=str(resolved.get("account") or "").strip(),
            command=tuple(str(token) for token in command),
            prompt_via=str(entry.get("prompt_via") or "argv"),
            env={str(k): str(v)
                 for k, v in dict(entry.get("env") or {}).items()},
            priority=priority,
            quota=str(entry.get("quota") or "reactive"),
            expires_on=str(entry.get("expires_on") or ""),
            limit_patterns=tuple(str(p) for p in entry.get("limit_patterns") or []),
            expired_patterns=tuple(
                str(p) for p in entry.get("expired_patterns") or []),
            extra=bool(entry.get("extra", False)),
            active=entry.get("active", True) is not False,
            timeout_minutes=timeout_minutes,
            reserve_only=bool(entry.get("reserve_only", False)),
            cost_tier=tier,
            cascade_only=bool(entry.get("cascade_only", False)),
            premium=bool(entry.get("premium", False)),
        ))
    slots.sort(key=lambda slot: (slot.priority, slot.label))
    return slots


class SlotStore(Protocol):
    """Persistence for slot availability (§7.2)."""

    async def get_slot(self, label: str) -> dict[str, Any]: ...
    async def set_slot(self, label: str, state: dict[str, Any]) -> None: ...


class InMemorySlotStore:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}

    async def get_slot(self, label: str) -> dict[str, Any]:
        return dict(self.states.get(label, {"status": "available"}))

    async def set_slot(self, label: str, state: dict[str, Any]) -> None:
        self.states[label] = dict(state)


class MongoSlotStore:
    """Production slot states on ``agent_status`` (one doc per slot label)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_slot(self, label: str) -> dict[str, Any]:
        row = await self._repo.get_agent_status(f"review:{label}")
        if not row:
            return {"status": "available"}
        return {"status": str(row.get("status") or "available"),
                "until": row.get("until"), "reason": row.get("reason")}

    async def set_slot(self, label: str, state: dict[str, Any]) -> None:
        await self._repo.set_agent_status(f"review:{label}", dict(state))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slot_available(
    slot: ReviewSlot,
    state: Mapping[str, Any],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(eligible, reason). Pure; the caller persists ``expires_on`` cutoffs."""
    now = now or _utcnow()
    if not slot.active:
        return False, "inactive"
    if slot.expires_on:
        try:
            expiry = datetime.strptime(slot.expires_on[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            expiry = None
        if expiry is not None and now >= expiry:
            return False, f"expired {slot.expires_on[:10]}"
    status = str(state.get("status") or "available")
    if status == "disabled":
        return False, str(state.get("reason") or "disabled")
    if status == "cooling_down":
        until = state.get("until")
        try:
            resume = (datetime.fromisoformat(str(until))
                      if until else now + timedelta(hours=1))
            if resume.tzinfo is None:
                resume = resume.replace(tzinfo=timezone.utc)
        except ValueError:
            resume = now + timedelta(hours=1)
        if now < resume:
            return False, f"cooling_down until {resume.isoformat()}"
    return True, ""


def expiry_warning(slot: ReviewSlot, now: datetime | None = None) -> str:
    """Warn 3 days before ``expires_on`` (§7.2); "" when not due."""
    if not slot.expires_on:
        return ""
    now = now or _utcnow()
    try:
        expiry = datetime.strptime(slot.expires_on[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = expiry - now
    if timedelta(0) <= delta <= timedelta(days=3):
        return (f"review slot {slot.label} expires on {slot.expires_on[:10]} "
                f"({delta.days}d left)")
    return ""


def classify_slot_error(text: str, slot: ReviewSlot) -> tuple[str, dict[str, Any]]:
    """Classify a reactive slot failure (§7.2).

    Returns ``(action, patch)`` where action is ``cooling_down``,
    ``disabled`` or ``invalid_only`` (unclassified: bad report, availability
    untouched).
    """
    lowered = (text or "").lower()
    for pattern in slot.expired_patterns:
        if pattern.lower() in lowered:
            return "disabled", {"status": "disabled",
                                "reason": f"expired pattern: {pattern}"}
    if any(pattern in lowered for pattern in (
            "model not found", "model_not_found", "modelnotfound")):
        return "disabled", {"status": "disabled", "reason": "model not found"}
    if transient_slot_error(text):
        return "cooling_down", {
            "status": "cooling_down",
            "until": (_utcnow() + timedelta(seconds=300)).isoformat(),
            "reason": "transient provider error"}
    for pattern in slot.limit_patterns:
        if pattern.lower() in lowered:
            resume = _utcnow() + timedelta(minutes=60)
            return "cooling_down", {"status": "cooling_down",
                                    "until": resume.isoformat(),
                                    "reason": f"limit pattern: {pattern}"}
    return "invalid_only", {}


def transient_slot_error(text: str) -> bool:
    """Recognize provider outages and transport failures across reviewer CLIs."""
    from orchestration.provider_recovery import transient_provider_error

    return transient_provider_error(text)


# ---------------------------------------------------------------------------
# Selection + quorum (§7.2, §7.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoundPlan:
    reviewers: tuple[ReviewSlot, ...]
    extras: tuple[ReviewSlot, ...]


async def select_round(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    count: int,
    builder_family: str = "muse",
    prefer_families: Sequence[str] = (),
    include_extras: bool = True,
    exclude: Sequence[str] = (),
    cascade_enabled: bool = False,
) -> RoundPlan:
    """Pick ``count`` slots of distinct families by priority (§7.2).

    ``prefer_families`` (round 2: families behind dismissed CRITICALs) jump
    the queue but still respect one-slot-per-family. Slots of the builder's
    own family and ``extra`` slots never count: they ride along as
    information only (§7.3). ``exclude`` skips labels already tried (quorum
    replacement wave). Cascade rounds (00310) order free → own → metered
    and admit ``cascade_only`` slots; classic rounds skip those slots.
    """
    preferred = {name.lower() for name in prefer_families}
    excluded = set(exclude)
    eligible: list[ReviewSlot] = []
    extras: list[ReviewSlot] = []
    for slot in pool:
        if slot.label in excluded or slot.reserve_only:
            continue
        if slot.cascade_only and not cascade_enabled:
            continue
        ok, _reason = slot_available(slot, await store.get_slot(slot.label))
        if not ok:
            continue
        if slot.extra or slot.family == builder_family:
            extras.append(slot)
        else:
            eligible.append(slot)
    if cascade_enabled:
        eligible.sort(key=lambda slot: (
            TIER_ORDER.get(slot.cost_tier, 2),
            0 if slot.family in preferred else 1, slot.priority, slot.label))
    else:
        eligible.sort(key=lambda slot: (
            0 if slot.family in preferred else 1, slot.priority, slot.label))
    picked: list[ReviewSlot] = []
    seen: set[str] = set()
    for slot in eligible:
        if slot.family in seen:
            continue
        seen.add(slot.family)
        picked.append(slot)
        if len(picked) >= count:
            break
    return RoundPlan(reviewers=tuple(picked),
                     extras=tuple(extras) if include_extras else ())


async def available_families(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    builder_family: str = "muse",
) -> set[str]:
    """Eligible quorum-counting families (preflight needs ≥3, §6.1).

    Cascade-only last-resort slots never feed preflight capacity.
    """
    families: set[str] = set()
    for slot in pool:
        if slot.extra or slot.family == builder_family or slot.cascade_only:
            continue
        ok, _reason = slot_available(slot, await store.get_slot(slot.label))
        if ok:
            families.add(slot.family)
    return families


def quorum_met(
    valid: Mapping[str, bool],
    slots: Sequence[ReviewSlot],
    builder_family: str = "muse",
    quorum: int = 2,
    relaxed: bool = False,
) -> bool:
    """≥``quorum`` valid reports of distinct families (fail-closed, §7.2).

    Relaxed (cascade final degradation, 00310) counts ``extra`` and
    builder-family reports too; classic callers keep ``relaxed=False``.
    """
    seen: set[str] = set()
    by_label = {slot.label: slot for slot in slots}
    for label, ok in valid.items():
        if not ok:
            continue
        slot = by_label.get(label)
        if slot is None:
            continue
        if not relaxed and (slot.extra or slot.family == builder_family):
            continue
        if not slot.family:
            continue
        seen.add(slot.family)
        if len(seen) >= quorum:
            return True
    return False


# ---------------------------------------------------------------------------
# Cascade spend guard (TASK-2026-00310)
# ---------------------------------------------------------------------------

#: Cascade config keys under ``review.review.cascade``.
CASCADE_DEFAULTS = {
    "enabled": False,
    "paid_cap_usd": "0.00",
    "paid_estimate_usd": "0.05",
    "dsh_estimate_usd": "0.00",
    "paid_whitelist": [],
    "paid_dynamic": True,
    "premium": False,
    "premium_cap_usd": "1.00",
    "premium_estimate_usd": "0.50",
    "premium_whitelist": [],
}


def _money(value: Any) -> Decimal | None:
    """Decimal money or None; floats are rejected, never rounded."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return parsed if parsed >= 0 else None


def cascade_mandate_threshold() -> Decimal:
    """No-questions new-spend bar; unreadable mandate fails to zero."""
    try:
        from company_mandate import MandatoEmpresa, load_mandate_file
        path = (Path(__file__).resolve().parents[2]
                / "projects" / "company_mandate.yaml")
        mandate = load_mandate_file(path)
    except Exception:  # noqa: BLE001
        return Decimal("0.00")
    if not isinstance(mandate, MandatoEmpresa):
        return Decimal("0.00")
    threshold = _money(mandate.presupuesto.gasto_nuevo_sin_preguntar)
    return threshold if threshold is not None else Decimal("0.00")


@dataclass
class CascadeSpendState:
    """Worst-case paid spend assumed so far in one round."""

    spent_usd: Decimal = field(default_factory=lambda: Decimal("0.00"))


@dataclass(frozen=True)
class SpendDecision:
    """Guard verdict for one metered attempt (stored on the outcome)."""

    allowed: bool
    reason: str
    estimate_usd: str
    cap_usd: str
    threshold_usd: str
    spent_after_usd: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "allowed": self.allowed, "reason": self.reason,
            "estimate_usd": self.estimate_usd, "cap_usd": self.cap_usd,
            "threshold_usd": self.threshold_usd,
            "spent_after_usd": self.spent_after_usd,
        }


class CascadeGuard:
    """Deterministic paid-tier gate; free/own tiers pass untouched."""

    def __init__(self, cascade: Mapping[str, Any] | None,
                 threshold_usd: Decimal | None = None,
                 paid_loader: Callable[[], list[str] | None] | None = None,
                 premium_loader: Callable[[], list[str] | None] | None = None,
                 ) -> None:
        raw = dict(cascade or {})
        self.static_whitelist = [str(item) for item in raw.get("paid_whitelist") or []]
        self.paid_dynamic = bool(raw.get(
            "paid_dynamic", CASCADE_DEFAULTS["paid_dynamic"]))
        self._paid_loader = paid_loader
        self.static_premium_whitelist = [
            str(item) for item in raw.get("premium_whitelist") or []]
        self._premium_loader = premium_loader
        self._ranking: Any = None
        self._ranking_loaded = False
        self.cap = _money(raw.get("paid_cap_usd",
                                  CASCADE_DEFAULTS["paid_cap_usd"]))
        self.paid_estimate = _money(raw.get(
            "paid_estimate_usd", CASCADE_DEFAULTS["paid_estimate_usd"]))
        self.dsh_estimate = _money(raw.get(
            "dsh_estimate_usd", CASCADE_DEFAULTS["dsh_estimate_usd"]))
        self.premium_cap = _money(raw.get(
            "premium_cap_usd", CASCADE_DEFAULTS["premium_cap_usd"]))
        self.premium_estimate = _money(raw.get(
            "premium_estimate_usd", CASCADE_DEFAULTS["premium_estimate_usd"]))
        self.threshold = (threshold_usd if threshold_usd is not None
                          else cascade_mandate_threshold())

    @property
    def whitelist(self) -> list[str]:
        """Auto-ranked paid ids when fresh, else the static config list."""
        return self.whitelist_for(premium=False)

    def whitelist_for(self, premium: bool) -> list[str]:
        """Effective paid ids; premium degrades to cheap without data."""
        whitelist, _cap, _estimate, _tier = self._paid_terms(premium)
        return whitelist

    def _paid_terms(self, premium: bool,
                    ) -> tuple[list[str], Decimal | None, Decimal | None, str]:
        """Whitelist, cap, estimate and tier; mirrors the executor rule."""
        if premium:
            dynamic = self._dynamic_paid_models(premium=True)
            if dynamic:
                return dynamic, self.premium_cap, self.premium_estimate, "premium"
            if self.static_premium_whitelist:
                return (self.static_premium_whitelist, self.premium_cap,
                        self.premium_estimate, "premium")
            premium = False
        dynamic = self._dynamic_paid_models(premium=False)
        if dynamic:
            return dynamic, self.cap, self.paid_estimate, "cheap"
        return self.static_whitelist, self.cap, self.paid_estimate, "cheap"

    def _dynamic_ranking(self) -> Any:
        if self._ranking_loaded:
            return self._ranking
        self._ranking_loaded = True
        try:
            from model_routing import paid as paid_mod
            from model_routing.paths import paid_path
            self._ranking = paid_mod.load_paid_ranking(paid_path())
        except Exception:  # noqa: BLE001
            self._ranking = None
        return self._ranking

    def _dynamic_paid_models(self, premium: bool = False) -> list[str] | None:
        loader = self._premium_loader if premium else self._paid_loader
        if loader is not None:
            try:
                return loader() or None
            except Exception:  # noqa: BLE001
                return None
        ranking = self._dynamic_ranking()
        if ranking is None:
            return None
        shortlist = ranking.premium if premium else ranking.models
        return [item.model_id for item in shortlist] or None

    def check(self, slot: ReviewSlot, state: CascadeSpendState,
              ) -> SpendDecision:
        """Allow or deny one metered attempt; allowed spend accrues."""
        if slot.cost_tier != "metered":
            return SpendDecision(True, f"{slot.cost_tier} tier needs no guard",
                                 "0.00", str(self.cap or 0),
                                 str(self.threshold), str(state.spent_usd))
        if self.threshold is None:
            return SpendDecision(False, "unreadable cap or mandate threshold",
                                 "?", "?", "?", str(state.spent_usd))
        estimate: Decimal | None = self.dsh_estimate
        cap: Decimal | None = self.cap
        tier_word = ""
        if slot.model.startswith("openrouter/") or slot.cascade_only:
            model = slot.model.split("/", 1)[1] if "/" in slot.model else slot.model
            whitelist, cap, estimate, tier = self._paid_terms(slot.premium)
            if tier == "premium":
                tier_word = "premium "
            if model not in whitelist:
                return SpendDecision(
                    False, f"{model} is outside the {tier_word}paid whitelist",
                    "?", str(cap), str(self.threshold),
                    str(state.spent_usd))
        if estimate is None or cap is None:
            return SpendDecision(False, "unreadable spend estimate",
                                 "?", str(cap), str(self.threshold),
                                 str(state.spent_usd))
        if cap > self.threshold:
            return SpendDecision(
                False, f"cap {cap} exceeds mandate threshold {self.threshold}",
                str(estimate), str(cap), str(self.threshold),
                str(state.spent_usd))
        if estimate > cap or state.spent_usd + estimate > cap:
            return SpendDecision(
                False, f"estimate {estimate} over cap {cap}",
                str(estimate), str(cap), str(self.threshold),
                str(state.spent_usd))
        state.spent_usd += estimate
        return SpendDecision(True, "within cap and mandate threshold",
                             str(estimate), str(cap), str(self.threshold),
                             str(state.spent_usd))


# ---------------------------------------------------------------------------
# Quorum round: relief + one retry per slot (§7.2, TASK-2026-00162)
# ---------------------------------------------------------------------------

#: Outcome kinds worth one more attempt in the same round: a slow or sloppy
#: run of a slot that is still available. Classified slot errors
#: (``cooling_down`` / ``disabled``) leave the pool and a ``write_attempt``
#: breached the read-only rule: neither is retried.
RETRIABLE_KINDS = frozenset({"timeout", "malformed", "slot_error", "worktree"})

#: A slot that timed out gets this much more time on its one retry.
RETRY_TIMEOUT_FACTOR = 1.5

#: ``run_one(slot, attempt, previous)`` → outcome (see :func:`new_outcome`);
#: ``previous`` is the failed first attempt being retried, else None.
RunOne = Callable[
    [ReviewSlot, int, "Mapping[str, Any] | None"], Awaitable[dict[str, Any]]]


def new_outcome(slot: ReviewSlot, attempt: int = 1) -> dict[str, Any]:
    """Blank outcome of one reviewer attempt; both engines fill it.

    ``kind``: ``valid``, ``timeout``, ``malformed``, ``slot_error``,
    ``cooling_down``, ``disabled``, ``write_attempt``, ``worktree`` or
    ``denied`` (cascade spend guard, 00310). ``account``/``model`` keep
    the per-attempt identity (00250); ``tier``/``spend`` its receipt.
    """
    return {"slot": slot.label, "family": slot.family,
            "account": slot.account, "model": slot.model,
            "attempt": attempt,
            "valid": False, "kind": "", "reason": "", "verdicts": [],
            "elapsed_s": 0.0, "exit_code": None,
            "tier": slot.cost_tier, "spend": {}, "relaxed": False}


def slot_timeout_s(
    slot: ReviewSlot,
    default_minutes: Any,
    previous: Mapping[str, Any] | None = None,
) -> int:
    """Reviewer timeout: the slot's own, else the round default; a retry
    after a timeout gets :data:`RETRY_TIMEOUT_FACTOR` more."""
    try:
        minutes = slot.timeout_minutes or int(default_minutes or 15)
    except (TypeError, ValueError):
        minutes = 15
    seconds = max(60, int(minutes) * 60)
    if previous is not None and previous.get("kind") == "timeout":
        seconds = int(seconds * RETRY_TIMEOUT_FACTOR)
    return seconds


def retry_note(previous: Mapping[str, Any] | None) -> str:
    """Prompt lead for a retry (empty on the first attempt)."""
    if not previous:
        return ""
    kind = str(previous.get("kind") or "")
    reason = str(previous.get("reason") or "")[:300]
    if kind == "timeout":
        return ("RETRY: your previous attempt in this round timed out. Stay "
                "on the listed files and print the report as soon as it is "
                "ready.")
    if kind == "malformed":
        return (f"RETRY: your previous report in this round was rejected "
                f"({reason}). Print ONLY the report, in the exact format "
                "below.")
    return (f"RETRY: your previous attempt in this round failed ({reason}). "
            "Print ONLY the report, in the exact format below.")


def quorum_summary(outcomes: Sequence[Mapping[str, Any]], limit: int = 900) -> str:
    """``label#attempt kind (reason)`` per attempt: why a round fell short."""
    parts: list[str] = []
    for outcome in outcomes:
        valid = bool(outcome.get("valid"))
        kind = "valid" if valid else str(outcome.get("kind") or "invalid")
        text = f"{outcome.get('slot')}#{outcome.get('attempt', 1)} {kind}"
        reason = "" if valid else str(outcome.get("reason") or "")
        if reason and reason != kind:
            text += f" ({reason[:120]})"
        parts.append(text)
    return "; ".join(parts)[:limit]


def _valid_by_label(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """Label → any attempt valid (a retry's success clears its failure)."""
    valid: dict[str, bool] = {}
    for outcome in outcomes:
        label = str(outcome.get("slot"))
        valid[label] = valid.get(label, False) or bool(outcome.get("valid"))
    return valid


def _counted_families(
    valid: Mapping[str, bool],
    slots: Sequence[ReviewSlot],
    builder_family: str,
    relaxed: bool = False,
) -> set[str]:
    by_label = {slot.label: slot for slot in slots}
    families: set[str] = set()
    for label, ok in valid.items():
        slot = by_label.get(label)
        if not ok or slot is None or not slot.family:
            continue
        if not relaxed and (slot.extra or slot.family == builder_family):
            continue
        families.add(slot.family)
    return families


@dataclass
class QuorumRound:
    """One round across its waves; every attempt is kept for telemetry."""

    met: bool
    outcomes: list[dict[str, Any]]  # every attempt, in run order
    reviewers: list[ReviewSlot]  # counting slots that ran
    valid: dict[str, bool]  # label → any attempt valid
    relaxed: bool = False  # cascade final degradation counted extras (00310)
    note: str = ""  # cascade tiers/denials, appended to the summary

    @property
    def verdicts(self) -> list[Any]:
        return [item for outcome in self.outcomes
                for item in outcome.get("verdicts") or []]

    def summary(self) -> str:
        text = quorum_summary(self.outcomes)
        return f"{text} [{self.note}]" if self.note else text


def cascade_enabled(cascade: Mapping[str, Any] | None) -> bool:
    """Rollback switch: anything but an enabled mapping is classic."""
    return bool(isinstance(cascade, Mapping) and cascade.get("enabled"))


def cascade_relax_after(cascade: Mapping[str, Any] | None) -> float | None:
    """Grace before the relaxed quorum may stand (TASK-2026-00469).

    ``None`` keeps the classic wait for the recovery deadline: cascade
    off, or ``relax_after_seconds`` absent, negative or unreadable.
    """
    if not cascade_enabled(cascade):
        return None
    raw = cascade.get("relax_after_seconds")  # type: ignore[union-attr]
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def cascade_for_priority(cascade: Mapping[str, Any] | None,
                         priority: str | None) -> dict[str, Any]:
    """Round cascade copy; critical tickets arm the premium paid list."""
    out = dict(cascade or {})
    out["premium"] = str(priority or "").lower() == "critical"
    return out


def premium_slots(pool: Sequence[ReviewSlot], enabled: bool,
                  ) -> list[ReviewSlot]:
    """Flag cascade-only slots premium; tier order never changes."""
    if not enabled:
        return list(pool)
    return [replace(slot, premium=True) if slot.cascade_only else slot
            for slot in pool]


def arm_premium_slots(executor: Any, pool: Sequence[ReviewSlot],
                      cascade: Mapping[str, Any] | None,
                      critical: bool) -> dict[str, Any]:
    """Arm the paid slot premium on executor plus mapping, in one call.

    Scripted executors without ``arm_premium`` keep the static slot; the
    guard still decides from the slot flag, so nothing spends blind.
    """
    out = dict(cascade or {})
    out["premium"] = bool(critical)
    if critical:
        arm = getattr(executor, "arm_premium", None)
        if callable(arm):
            for slot in pool or []:
                if slot.cascade_only:
                    arm(slot.label)
    return out


def disarm_premium_slots(executor: Any, pool: Sequence[ReviewSlot]) -> None:
    """Drop round-scoped premium flags; call in a finally after the round."""
    disarm = getattr(executor, "disarm_premium", None)
    if not callable(disarm):
        return
    for slot in pool or []:
        if slot.cascade_only:
            disarm(slot.label)


def _cascade_note(result: QuorumRound) -> str:
    """Tiers tried, guard denials and the final verdict for the ticket."""
    tiers = sorted({str(outcome.get("tier") or "?") for outcome in result.outcomes},
                   key=lambda name: TIER_ORDER.get(name, 9))
    parts = [f"tiers: {','.join(tiers) or 'none'}"]
    denials = [f"{outcome.get('slot')} ({outcome.get('reason')})"
               for outcome in result.outcomes
               if outcome.get("kind") == "denied"]
    if denials:
        parts.append("denied: " + "; ".join(denials))
    if result.relaxed:
        parts.append("relaxed quorum: extra/builder-family reports count")
    elif result.met:
        parts.append("quorum")
    else:
        parts.append("blocked: no tier left")
    return "; ".join(parts)


async def run_quorum_round(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    plan: RoundPlan,
    run_one: RunOne,
    builder_family: str = "muse",
    quorum: int = 2,
    recovery: Mapping[str, Any] | None = None,
    cascade: Mapping[str, Any] | None = None,
) -> QuorumRound:
    """Run independent reviewers, preserving valid reports across failures.

    Recovery adds delayed retries and ranked reserves until its deadline;
    without recovery, use two bounded waves and one retry per slot.
    A cascade mapping (00310) guards metered attempts, orders recovery
    free → own → metered and, only as final degradation, lets extra
    and builder-family reports count toward quorum, audited. With
    ``relax_after_seconds`` it comes early: the grace starts at the
    first counted report, an untried extra joins when it ends, and once
    the relaxed quorum holds the reviewers still running are cut
    (TASK-2026-00469).
    """
    enabled = cascade_enabled(cascade)
    pool = premium_slots(
        pool, enabled and bool((cascade or {}).get("premium")))
    if enabled:
        guard = CascadeGuard(cascade)
        spend = CascadeSpendState()
        inner = run_one

        async def guarded(slot: ReviewSlot, attempt: int,
                          previous: Mapping[str, Any] | None) -> dict[str, Any]:
            decision = guard.check(slot, spend)
            if not decision.allowed:
                denied = new_outcome(slot, attempt)
                denied.update(kind="denied", reason=decision.reason,
                              spend=decision.as_dict())
                return denied
            outcome = await inner(slot, attempt, previous)
            if slot.cost_tier == "metered":
                outcome["spend"] = decision.as_dict()
            return outcome

        run_one = guarded
    if recovery:
        result = await _recover_quorum(
            pool, store, plan, run_one, builder_family, quorum, recovery,
            cascade_tiers=enabled, relax_after=cascade_relax_after(cascade))
    else:
        result = await _plain_quorum_waves(
            pool, store, plan, run_one, builder_family, quorum, enabled)
    if enabled:
        result = await _cascade_finish(
            pool, store, result, run_one, builder_family, quorum)
    return result


async def _cascade_finish(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    result: QuorumRound,
    run_one: RunOne,
    builder_family: str,
    quorum: int,
) -> QuorumRound:
    """Final degradation: run untried extras, then count them if needed."""
    if not result.met:
        tried = {str(outcome.get("slot")) for outcome in result.outcomes}
        pool_by_label = {slot.label: slot for slot in pool}
        for slot in pool:
            if slot.label in tried:
                continue
            if not (slot.extra or slot.family == builder_family):
                continue
            if slot.reserve_only:
                continue  # reserves belong to recovery, not to relaxation
            ok, _reason = slot_available(slot, await store.get_slot(slot.label))
            if not ok:
                continue
            result.outcomes.append(await run_one(slot, 1, None))
        ran = {str(outcome.get("slot")) for outcome in result.outcomes}
        listed = {slot.label for slot in result.reviewers}
        for label in sorted(ran - listed):
            slot = pool_by_label.get(label)
            if slot is not None:
                result.reviewers.append(slot)
        by_label = {slot.label: slot for slot in result.reviewers}
        result.valid = _valid_by_label(result.outcomes)
        if quorum_met(result.valid, result.reviewers, builder_family,
                      quorum, relaxed=True):
            for outcome in result.outcomes:
                slot = by_label.get(str(outcome.get("slot")))
                if (outcome.get("valid") and slot is not None
                        and (slot.extra or slot.family == builder_family)):
                    outcome["relaxed"] = True
            result.met = True
            result.relaxed = True
    result.note = _cascade_note(result)
    return result


async def _plain_quorum_waves(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    plan: RoundPlan,
    run_one: RunOne,
    builder_family: str,
    quorum: int,
    cascade_enabled: bool,
) -> QuorumRound:
    """Two bounded waves with one retry per slot (no recovery config)."""
    first = [*plan.reviewers, *plan.extras]
    outcomes: list[dict[str, Any]] = list(await asyncio.gather(
        *[run_one(slot, 1, None) for slot in first]))
    reviewers = list(plan.reviewers)
    valid = _valid_by_label(outcomes)
    if quorum_met(valid, reviewers, builder_family, quorum):
        return QuorumRound(True, outcomes, reviewers, valid)
    counted = _counted_families(valid, reviewers, builder_family)
    target = max(1, quorum - len(counted)) + 1
    relief = await select_round(
        pool, store, target, builder_family=builder_family,
        include_extras=False,
        exclude=[str(outcome.get("slot")) for outcome in outcomes],
        cascade_enabled=cascade_enabled)
    wave: list[tuple[ReviewSlot, int, Mapping[str, Any] | None]] = [
        (slot, 1, None) for slot in relief.reviewers]
    covered = counted | {slot.family for slot in relief.reviewers}
    by_label = {slot.label: slot for slot in reviewers}
    for outcome in outcomes:
        if len(wave) >= target:
            break
        slot = by_label.get(str(outcome.get("slot")))
        if slot is None or outcome.get("valid") or slot.family in covered:
            continue
        if outcome.get("routing_exhausted"):
            continue
        if str(outcome.get("kind") or "") not in RETRIABLE_KINDS:
            continue
        ok, _reason = slot_available(slot, await store.get_slot(slot.label))
        if not ok:
            continue
        wave.append((slot, 2, outcome))
        covered.add(slot.family)
    if not wave:
        return QuorumRound(False, outcomes, reviewers, valid)
    outcomes.extend(await asyncio.gather(
        *[run_one(slot, attempt, previous)
          for slot, attempt, previous in wave]))
    labels = {slot.label for slot in reviewers}
    reviewers.extend(slot for slot, _attempt, _previous in wave
                     if slot.label not in labels)
    valid = _valid_by_label(outcomes)
    return QuorumRound(
        quorum_met(valid, reviewers, builder_family, quorum),
        outcomes, reviewers, valid)


async def _recover_quorum(
    pool: Sequence[ReviewSlot], store: SlotStore, plan: RoundPlan,
    run_one: RunOne, builder_family: str, quorum: int,
    recovery: Mapping[str, Any], cascade_tiers: bool = False,
    relax_after: float | None = None,
) -> QuorumRound:
    def _tier_key(slot: ReviewSlot) -> tuple:
        base = (not slot.reserve_only, slot.priority, slot.label)
        if not cascade_tiers:
            return base
        return (TIER_ORDER.get(slot.cost_tier, 2), *base)

    attempts_per_slot = max(1, int(recovery.get("attempts_per_slot", 3)))
    delay = max(1, float(recovery.get("retry_delay_seconds", 300)))
    deadline = time.monotonic() + max(1, float(recovery.get("max_wait_seconds", 43200)))
    outcomes: list[dict[str, Any]] = []
    reviewers = list(plan.reviewers)
    attempts: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    tried_models: set[str] = set()
    armed_at: float | None = None
    degraded = False

    def relax_left() -> float | None:
        """Grace left since the first counted report; None while unarmed."""
        nonlocal armed_at
        if relax_after is None:
            return None
        valid = _valid_by_label(outcomes)
        if quorum_met(valid, reviewers, builder_family, quorum):
            return None
        if not _counted_families(valid, reviewers, builder_family):
            return None
        if armed_at is None:
            armed_at = time.monotonic()
        return armed_at + relax_after - time.monotonic()

    def relaxed_met() -> bool:
        listed = {slot.label for slot in reviewers}
        ran = [slot for slot in pool
               if slot.label in latest and slot.label not in listed]
        return quorum_met(_valid_by_label(outcomes), [*reviewers, *ran],
                          builder_family, quorum, relaxed=True)

    def relax_due() -> bool:
        left = relax_left()
        return left is not None and left <= 0 and relaxed_met()

    async def degradation_slots() -> list[ReviewSlot]:
        """Untried extra/builder-family slots, launched once the grace ends."""
        found: list[ReviewSlot] = []
        for slot in pool:
            if (slot.label in attempts or slot.reserve_only
                    or not (slot.extra or slot.family == builder_family)):
                continue
            ok, _reason = slot_available(slot, await store.get_slot(slot.label))
            if ok:
                found.append(slot)
        return found

    def recoverable(outcome: Mapping[str, Any]) -> bool:
        if outcome.get("routing_exhausted"):
            return False
        kind = outcome.get("kind")
        return (kind in {"timeout", "cooling_down"}
                or (kind == "slot_error"
                    and transient_slot_error(str(outcome.get("reason") or ""))))

    async def pause(seconds: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(seconds, remaining))
        return time.monotonic() < deadline

    async def run_slot(slot: ReviewSlot) -> None:
        for local_attempt in range(attempts_per_slot):
            if local_attempt and quorum_met(
                    _valid_by_label(outcomes), reviewers, builder_family, quorum):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            previous = latest.get(slot.label)
            attempt = attempts.get(slot.label, 0) + 1
            attempts[slot.label] = attempt
            if slot.model:
                tried_models.add(slot.model)
            try:
                outcome = await asyncio.wait_for(
                    run_one(slot, attempt, previous), timeout=remaining)
            except asyncio.TimeoutError:
                outcome = new_outcome(slot, attempt)
                outcome.update(kind="timeout", reason="review recovery deadline reached")
            except Exception as exc:  # noqa: BLE001
                outcome = new_outcome(slot, attempt)
                outcome.update(kind="slot_error", reason=f"{type(exc).__name__}: {exc}"[:300])
            outcomes.append(outcome)
            latest[slot.label] = outcome
            if outcome.get("valid"):
                await store.set_slot(slot.label, {"status": "available"})
                return
            if quorum_met(_valid_by_label(outcomes), reviewers, builder_family, quorum):
                return
            if slot.extra or slot.family == builder_family:
                return
            retryable = recoverable(outcome)
            if (not outcome.get("routing_exhausted")
                    and outcome.get("kind") in {"malformed", "worktree"}):
                retryable = local_attempt == 0
            if not retryable or local_attempt + 1 >= attempts_per_slot:
                return
            counted = _counted_families(
                _valid_by_label(outcomes), reviewers, builder_family)
            for reserve in pool:
                if (reserve.label in scheduled or reserve.label in latest
                        or reserve.extra or reserve.family == builder_family
                        or reserve.family in counted
                        or (reserve.model and reserve.model in tried_models)):
                    continue
                ok, _reason = slot_available(
                    reserve, await store.get_slot(reserve.label))
                if ok:
                    return
            state = await store.get_slot(slot.label)
            if state.get("status") == "disabled":
                return
            wait = delay
            if state.get("until"):
                try:
                    until = datetime.fromisoformat(str(state["until"]))
                    until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until
                    wait = max(wait, (until - _utcnow()).total_seconds())
                except ValueError:
                    return
            logger.warning("Reviewer %s attempt %s failed (%s); retry in %.0fs",
                           slot.label, attempt, outcome.get("kind"), wait)
            if not await pause(wait):
                return

    async def run_batch(slots: Sequence[ReviewSlot]) -> bool:
        """Run one batch; True when the relax grace cut it short."""
        nonlocal degraded
        pending = {asyncio.ensure_future(run_slot(slot)) for slot in slots}
        try:
            while True:
                left = relax_left()
                if left is not None and left <= 0:
                    if relaxed_met():
                        return True
                    if not degraded:
                        degraded = True
                        pending |= {asyncio.ensure_future(run_slot(slot))
                                    for slot in await degradation_slots()}
                    left = None
                if not pending:
                    return False
                done, pending = await asyncio.wait(
                    pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    batch = [*plan.reviewers, *plan.extras]
    while time.monotonic() < deadline:
        scheduled = {slot.label for slot in batch}
        cut = await run_batch(batch)
        valid = _valid_by_label(outcomes)
        if quorum_met(valid, reviewers, builder_family, quorum):
            return QuorumRound(True, outcomes, reviewers, valid)
        if cut or relax_due():
            logger.warning("Review quorum relaxed after %.0fs without the "
                           "strict quorum", relax_after)
            break
        counted = _counted_families(valid, reviewers, builder_family)
        candidates: list[ReviewSlot] = []
        for slot in sorted(pool, key=_tier_key):
            if (slot.extra or slot.family == builder_family or slot.family in counted
                    or slot.label in latest or (slot.model and slot.model in tried_models)):
                continue
            ok, _reason = slot_available(slot, await store.get_slot(slot.label))
            if ok:
                candidates.append(slot)
        if not candidates:
            waiting: list[ReviewSlot] = []
            for slot in pool:
                eligible, _reason = slot_available(slot, {"status": "available"})
                if (slot.extra or slot.family == builder_family or slot.family in counted
                        or not eligible):
                    continue
                state = await store.get_slot(slot.label)
                if state.get("status") == "disabled":
                    continue
                outcome = latest.get(slot.label)
                if ((outcome and recoverable(outcome))
                        or (outcome is None and state.get("status") == "cooling_down")):
                    waiting.append(slot)
            if len(counted | {slot.family for slot in waiting}) < quorum:
                break
            left = relax_left()
            wait = min(delay, left) if left is not None and left > 0 else delay
            logger.warning("Review quorum unavailable; keeping valid reports and waiting %.0fs", wait)
            if not await pause(wait):
                break
            for slot in sorted(waiting, key=_tier_key):
                ok, _reason = slot_available(slot, await store.get_slot(slot.label))
                if ok:
                    candidates.append(slot)
            if not candidates:
                batch = []
                continue
        batch = []
        covered = set(counted)
        models: set[str] = set()
        for slot in candidates:
            if slot.family in covered or (slot.model and slot.model in models):
                continue
            batch.append(slot)
            covered.add(slot.family)
            if slot.model:
                models.add(slot.model)
            if slot.label not in {item.label for item in reviewers}:
                reviewers.append(slot)
            if len(batch) >= max(1, quorum - len(counted)) + 1:
                break
        logger.info("Review quorum recovery using %s",
                    ", ".join(f"{slot.label} ({slot.model})" for slot in batch))
    valid = _valid_by_label(outcomes)
    return QuorumRound(False, outcomes, reviewers, valid)


# ---------------------------------------------------------------------------
# Worktree (read-only review environment, §7.5)
# ---------------------------------------------------------------------------

def create_worktree(repo: Path, head_sha: str, tag: str) -> Path:
    """Detached worktree at ``head_sha`` in a scratch dir (outside the repo)."""
    target = Path(tempfile.mkdtemp(prefix=f"tier-s-{tag}-"))
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), head_sha],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"worktree failed: {proc.stderr.strip()[:300]}")
    return target


def remove_worktree(repo: Path, worktree: Path) -> None:
    """Best-effort removal; a leftover worktree never fails the cycle."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        shutil.rmtree(worktree, ignore_errors=True)
    except OSError:
        pass


def worktree_snapshot(worktree: Path) -> str:
    """Digest of the worktree state for write detection (§7.5, §20-5)."""
    from agents.dev.tier_s_contract import git_snapshot
    try:
        return git_snapshot(worktree).digest
    except (OSError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------------------
# Request + prompt (§7.4, §7.5)
# ---------------------------------------------------------------------------

def build_review_request(
    round_no: int,
    goal: str,
    acceptance: Sequence[str],
    base_sha: str,
    head_sha: str,
    diff_stat: str,
    files: Sequence[str],
    out_of_plan: Sequence[str],
    gates_section: str,
    handoff_path: str,
    extra_context: str = "",
) -> str:
    """``REVIEW_REQUEST_<r>.md``: pointers, never pasted diffs (§7.4)."""
    out = set(out_of_plan)
    criteria = [f"- {criterion}" for criterion in acceptance] or ["- (none listed)"]
    lines = [
        f"REVIEW REQUEST R{round_no}",
        f"Goal: {goal}",
        "Acceptance:",
        *criteria,
        f"Base: {base_sha}",
        f"Head: {head_sha}",
        "Diff stat:",
        (diff_stat.strip() or "(empty)")[:4000],
        "Files under review (out-of-plan flagged):",
        *[f"- {name}{'  FUERA DE PLAN' if name in out else ''}" for name in files],
        "Gates (already run by the orchestrator):",
        (gates_section.strip() or "(none)")[:2000],
        f"Handoff: {handoff_path}",
    ]
    if extra_context:
        lines.extend(["", extra_context.strip()[:6000]])
    return "\n".join(lines) + "\n"


def reviewer_prompt(
    slot_label: str,
    request_name: str,
    scope: str,
    round_no: int,
    repo: Path | str | None = None,
    retry: str = "",
) -> str:
    """Pointed review prompt (§7.5): diff-only, no tests, no-wide review.

    ``retry`` (see :func:`retry_note`) leads the body on a second attempt.
    The output contract is explicit because the report is machine-parsed
    (TASK-2026-00162: preambles voided 4 of 5 valid pilot reports).
    """
    body = (
        f"You are review slot {slot_label}. Read {request_name} in this "
        "worktree (read-only checkout at the ticket head).\n"
        "Review ONLY the listed files, plus directly related ones when a "
        "contract needs it (e.g. a caller). Do NOT run tests or builds: gate "
        "results are in the request. Do NOT do a general system review. A "
        "grave bug outside the diff may be reported, but do not go hunting.\n"
        "Focus on what breaks for users and callers: logic errors, broken "
        "events or handlers, wrong data flow or state, navigation and "
        "history, error paths, contract mismatches. Report a hand-written "
        "substitute for a standard maintained library, or a "
        "non-conventional name, as maintainability. Skip style nits.\n"
        "OUTPUT CONTRACT: your stdout IS the report and a program parses "
        "it. The first line is a finding header or `No findings.`: no "
        "preamble, no summary, no markdown, no bullets, no backticks.\n"
        "Exactly one finding per header line:\n"
        "[CRITICAL|WARN|INFO] [bug|security|contract|performance|test|"
        "maintainability] <anchor> — <problem> → <fix>  (§16.4)\n"
        "then one `> <source text copied verbatim>` line per line anchor, "
        "then an optional explanation of at most 5 lines. "
        "Anchors: path:line, path:line@base (deleted/changed code), "
        "path:line + path:line (cross-location, quote each), path "
        "(whole file), CRITERIO:<id>. If clean, print `No findings.`.\n"
        f"End with REVIEW_DONE_{scope}_{round_no} on its own last line."
    )
    if retry:
        body = f"{retry}\n{body}"
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# FIX verdicts (§7.9, §16.8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FixVerdict:
    finding_id: int
    verdict: str  # CORREGIDO | DESCARTADO
    reason: str
    out_of_scope: bool


_FIX_RE = re.compile(
    r"^F(\d+)\s*:\s*(CORREGIDO|DESCARTADO)\b\s*(?:[—–-]\s*(.*))?$",
    re.IGNORECASE,
)


def parse_fix(text: str) -> dict[int, list[FixVerdict]]:
    """Parse ``FIX_<r>.md``: finding id → verdicts (repeats kept, §7.9)."""
    verdicts: dict[int, list[FixVerdict]] = {}
    for raw_line in (text or "").splitlines():
        match = _FIX_RE.match(raw_line.strip())
        if not match:
            continue
        reason = (match.group(3) or "").strip()
        lowered = reason.lower()
        verdicts.setdefault(int(match.group(1)), []).append(FixVerdict(
            finding_id=int(match.group(1)),
            verdict=match.group(2).upper(), reason=reason,
            out_of_scope="fuera de alcance" in lowered
            or "out of scope" in lowered))
    return verdicts


def fix_gaps(
    findings: Sequence[Finding],
    verdicts: Mapping[int, list[FixVerdict]],
) -> tuple[list[int], list[int]]:
    """(missing, duplicated) finding ids — mirrors plan_response_gaps."""
    wanted = {item.id for item in findings}
    missing = sorted(wanted - set(verdicts))
    duplicated = sorted(index for index in wanted
                        if len(verdicts.get(index, ())) > 1)
    return missing, duplicated


def muse_fix_prompt(
    round_no: int,
    findings_name: str,
    count: int,
    repo: Path | str | None = None,
) -> str:
    """First-instance judge prompt (§7.9): fix or discard with reasons."""
    body = (
        f"Decide every finding in {findings_name} ({count} findings, round "
        f"{round_no}). They are verified: they point at real code. Judge the "
        "reasoning: is the problem real, is the proposed fix correct, is it "
        "in this ticket's scope?\n"
        f"Write FIX_{round_no}.md with one line per finding: "
        "`F<n>: CORREGIDO — <what you changed>` (and change the code, no "
        "commits) or `F<n>: DESCARTADO — <verifiable technical reason>`. "
        "`I disagree` is not a reason. A multi-family CRITICAL needs an "
        "especially solid reason to discard. Real but out-of-scope problems: "
        "`DESCARTADO — fuera de alcance` (the orchestrator routes them to "
        "the inbox). Do not rewrite uninvolved code.\n"
        f"Then emit FIX_DONE_{round_no} in .openceo/status."
    )
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# Arbitration trigger (§7.10, §6.9)
# ---------------------------------------------------------------------------

def _same_issue(left: VerifiedFinding, right: VerifiedFinding) -> bool:
    if left.raw.category != right.raw.category:
        return False
    for left_anchor in left.raw.anchors:
        for right_anchor in right.raw.anchors:
            if left_anchor.criterion is not None or right_anchor.criterion is not None:
                if left_anchor.criterion == right_anchor.criterion:
                    return True
                continue
            if left_anchor.path != right_anchor.path:
                continue
            if left_anchor.line is None or right_anchor.line is None:
                return True
            if abs(left_anchor.line - right_anchor.line) <= 5:
                return True
    return False


def arbitration_candidates(
    round2: Sequence[VerifiedFinding],
    fix2: Mapping[int, list[FixVerdict]],
    findings2: Sequence[Finding],
    round1_dismissed: Sequence[VerifiedFinding],
) -> list[Finding]:
    """CRITICALs reconfirmed by ≥2 families that ``muse`` dismissed again.

    A candidate needs: CRITICAL in ``FINDINGS_2`` with ≥2 counting families,
    ``DESCARTADO`` in ``FIX_2`` (not out-of-scope), and the same issue
    dismissed in round 1 (reconfirmation, §6.9).
    """
    by_id = {item.id: item for item in findings2}
    candidates: list[Finding] = []
    for finding in findings2:
        if finding.severity != "CRITICAL":
            continue
        if len([name for name in finding.families]) < 2:
            continue
        verdicts = fix2.get(finding.id, [])
        if not verdicts or verdicts[0].verdict != "DESCARTADO":
            continue
        if verdicts[0].out_of_scope:
            continue
        probe = next(
            (item for item in round2
             if item.destination == "findings"
             and any(_anchor_text(a) in {
                 _anchor_text(b) for b in finding.anchors}
                     for a in item.raw.anchors)),
            None)
        if probe is None:
            continue
        if not any(_same_issue(probe, old) for old in round1_dismissed):
            continue
        candidates.append(by_id[finding.id])
    return candidates


def arbiter_prompt(
    finding: Finding,
    muse_reason: str,
    quote: str,
    repo: Path | str | None = None,
) -> str:
    """Short arbitration session (§7.10): the verdict is final."""
    anchors = " + ".join(_anchor_text(a) for a in finding.anchors)
    body = (
        "Arbitrate ONE disputed CRITICAL finding (final verdict).\n"
        f"Finding: [{finding.category}] {anchors} — {finding.problem} → "
        f"{finding.fix}\nFamilies reporting: {', '.join(finding.families)}\n"
        f"Code:\n{quote[:2000]}\n"
        f"muse discarded it: {muse_reason[:1000]}\n"
        "Reply exactly one line: `REAL — <why>` or `FALSO — <verifiable "
        "technical reason>`. Then emit ARBITRATED in .openceo/status."
    )
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# task_reviews persistence (§15.5, append-only)
# ---------------------------------------------------------------------------

class ReviewStore(Protocol):
    """Append-only reviewer reports."""

    async def create_review(self, doc: dict[str, Any]) -> None: ...


class InMemoryReviewStore:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def create_review(self, doc: dict[str, Any]) -> None:
        self.docs.append(dict(doc))


class MongoReviewStore:
    """Production reports on ``task_reviews`` (existing collection)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def create_review(self, doc: dict[str, Any]) -> None:
        await self._repo.create_task_review(dict(doc))


# ---------------------------------------------------------------------------
# Round telemetry (§15.3 rounds[])
# ---------------------------------------------------------------------------

def round_telemetry(
    round_no: int,
    slots: Sequence[str],
    valid_reports: int,
    verdicts: Sequence[VerifiedFinding],
    fix: Mapping[int, list[FixVerdict]],
    attempts: Sequence[Mapping[str, Any]] = (),
    met: bool = True,
) -> dict[str, Any]:
    """One ``rounds[]`` entry for the run document.

    ``attempts`` (every reviewer attempt of the round) adds ``quorum`` and
    ``attempts`` (slot, attempt, kind, reason, seconds): a failed round
    keeps why each slot fell short (TASK-2026-00162).
    """
    counts = {"verified": 0, "unverified": 0, "pre_existing": 0, "in_scope": 0}
    for item in verdicts:
        if not item.verified:
            counts["unverified"] += 1
        else:
            counts["verified"] += 1
        if item.destination == "inbox":
            counts["pre_existing"] += 1
        elif item.destination == "findings":
            counts["in_scope"] += 1
    fixed = sum(1 for verdicts_list in fix.values()
                for verdict in verdicts_list[:1]
                if verdict.verdict == "CORREGIDO")
    discarded = sum(1 for verdicts_list in fix.values()
                    for verdict in verdicts_list[:1]
                    if verdict.verdict == "DESCARTADO")
    entry: dict[str, Any] = {
        "r": round_no,
        "slots": list(slots),
        "valid_reports": valid_reports,
        "findings": counts,
        "muse": {"fixed": fixed, "discarded": discarded, "no_response": 0},
    }
    if attempts:
        entry["quorum"] = met
        rows = []
        for item in attempts:
            row = {
                "slot": str(item.get("slot")),
                "attempt": int(item.get("attempt") or 1),
                "kind": "valid" if item.get("valid") else str(item.get("kind") or ""),
                "reason": "" if item.get("valid") else str(item.get("reason") or "")[:200],
                "elapsed_s": round(float(item.get("elapsed_s") or 0.0), 1),
            }
            if isinstance(item.get("receipt"), dict):
                row["receipt"] = dict(item["receipt"])
            rows.append(row)
        entry["attempts"] = rows
    return entry
