"""Incident capture: idempotent, never-throwing record_incident (design v2 5.4).

Raw incident in, classified/fingerprinted/sanitized doc out. Mongo insert
with a 2 s timeout; on failure the doc goes to a local JSONL spool that is
replayed at startup and after the next good write. Avalanche cap drops the
excess past 20 incidents per fingerprint and hour (the worker in TASK-2026-00378
bumps Problem.suppressed; until then the drop is counted in memory).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .classification import REASON_CODE_MAP_VERSION, classify
from .config import RELIABILITY_DEFAULTS
from .fingerprint import (
    FINGERPRINT_VERSION,
    fingerprint_deadline,
    fingerprint_escape,
    fingerprint_exception,
    fingerprint_invariant,
    fingerprint_stall,
    fingerprint_terminal,
    fingerprint_unclean,
    problem_key,
)
from .models import SCHEMA_VERSION, Attribution, Component, IncidentKind, Severity, default_severity
from .sanitize import sanitize_incident

logger = logging.getLogger(__name__)

COLLECTION = "reliability_incidents"
SPOOL_FILENAME = "reliability-spool.jsonl"
LAST_EXIT_FILENAME = "last-exit.json"
INSERT_TIMEOUT_SECONDS = 2.0

_NOTIFY_HOOK: Callable[[dict[str, Any]], None] | None = None
_SPOOL_LOCK = threading.Lock()
_SPOOL_DROPPED = 0
_SUPPRESSED: dict[str, int] = {}


def set_notify_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    """Worker hook (TASK-2026-00378 owns the worker); None disables."""
    global _NOTIFY_HOOK
    _NOTIFY_HOOK = hook


def suppressed_counts() -> dict[str, int]:
    with _SPOOL_LOCK:
        return dict(_SUPPRESSED)


def spool_dropped() -> int:
    with _SPOOL_LOCK:
        return _SPOOL_DROPPED


def reset_counters() -> None:
    with _SPOOL_LOCK:
        global _SPOOL_DROPPED
        _SPOOL_DROPPED = 0
        _SUPPRESSED.clear()


def repo_root_for_here() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_dir_for(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return repo_root_for_here() / ".runtime"


def spool_path(runtime_dir: str | Path | None = None) -> Path:
    return runtime_dir_for(runtime_dir) / SPOOL_FILENAME


def spool_status(runtime_dir: str | Path | None = None) -> dict[str, Any]:
    try:
        path = spool_path(runtime_dir)
        size = path.stat().st_size if path.exists() else 0
        pending = 0
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        pending += 1
        return {"pending": pending, "bytes": size, "dropped": spool_dropped()}
    except OSError:
        return {"pending": -1, "bytes": -1, "dropped": spool_dropped()}


def _key(value: object) -> str:
    from enum import Enum

    return value.value if isinstance(value, Enum) else str(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime | None) -> datetime:
    if moment is None:
        return _utcnow()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def build_incident_id(
    kind: str | IncidentKind,
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
    discriminator: str | None = None,
) -> str:
    """Deterministic id with natural identity, else a generated UUID hex."""
    attempt = (attempt_id or run_id or "").strip()
    task = (task_id or "").strip()
    disc = (discriminator or "").strip()
    if _key(kind) and task and attempt and disc:
        raw = "|".join((_key(kind), task, attempt, disc))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return uuid.uuid4().hex


def _discriminator(
    *,
    invariant_id: str | None,
    reason_code: str | None,
    brake: str | None,
    fingerprint: str,
) -> str:
    for candidate in (invariant_id, reason_code, brake):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return fingerprint


def _fingerprint_for(
    *,
    kind: IncidentKind,
    component: str,
    phase: str | None,
    exc_type: str | None,
    frames: list[dict[str, str]] | None,
    reason_code: str | None,
    brake: str | None,
    invariant_id: str | None,
    result_signature: str | None,
    guard_rule: str | None,
    service_result: str | None,
    deadline_type: str | None,
    escape_type: str | None,
    tier: str | None,
    review_policy: str | None,
) -> str:
    if kind is IncidentKind.EXCEPTION:
        return fingerprint_exception(exc_type or "?", frames or [], component)
    if kind in (IncidentKind.TERMINAL_FAILURE, IncidentKind.BRAKE_EXHAUSTED):
        return fingerprint_terminal(
            _key(kind), component, phase, reason_code, brake,
            map_version=REASON_CODE_MAP_VERSION)
    if kind is IncidentKind.STALL:
        return fingerprint_stall(
            component, phase, guard_rule, result_signature)
    if kind is IncidentKind.INVARIANT_VIOLATION:
        return fingerprint_invariant(invariant_id or reason_code or "?")
    if kind is IncidentKind.DEADLINE_MISSED:
        return fingerprint_deadline(component, deadline_type or phase or "?")
    if kind is IncidentKind.UNCLEAN_EXIT:
        return fingerprint_unclean(service_result or "?")
    return fingerprint_escape(
        escape_type or reason_code or "?", tier, review_policy)


def _scope_for(attribution: Attribution, project_key: str | None) -> str:
    if attribution is Attribution.ENGINE:
        return "engine"
    if attribution is Attribution.ENVIRONMENT:
        return "environment"
    return f"project:{(project_key or 'unknown').strip() or 'unknown'}"


def build_incident_doc(
    *,
    kind: str | IncidentKind,
    component: str | Component,
    now: datetime | None = None,
    severity: str | Severity | None = None,
    release: dict[str, Any] | None = None,
    retention_days: int | None = None,
    incident_id: str | None = None,
    shadow: bool = False,
    late: bool = False,
    from_reliability_ticket: bool = False,
    glitchtip_event_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
    phase: str | None = None,
    exc_type: str | None = None,
    exc_text: str | None = None,
    frames: list[dict[str, str]] | None = None,
    reason_code: str | None = None,
    brake: str | None = None,
    invariant_id: str | None = None,
    result_signature: str | None = None,
    guard_rule: str | None = None,
    service_result: str | None = None,
    deadline_type: str | None = None,
    escape_type: str | None = None,
    tier: str | None = None,
    review_policy: str | None = None,
    project_key: str | None = None,
    business_ref: str | None = None,
    message: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Classify, fingerprint and sanitize one raw incident into its doc."""
    kind_enum = kind if isinstance(kind, IncidentKind) else IncidentKind(str(kind))
    created = _as_utc(now)
    attribution, rule = classify(
        kind_enum, exc_type=exc_type, exc_text=exc_text, frames=frames,
        reason_code=reason_code, brake=brake, result_signature=result_signature,
        service_result=service_result)
    motor_frame = any(
        "openceo-core/" in str(frame.get("file") or "")
        or str(frame.get("file") or "") == "automatico.py"
        for frame in (frames or []))
    resolved = default_severity(kind_enum, motor_frame=motor_frame, guard_rule=guard_rule)
    if severity is not None:
        try:
            forced = severity if isinstance(severity, Severity) \
                else Severity(str(severity).strip().lower())
        except ValueError:
            forced = None
        if forced is not None:
            resolved = forced
    severity = resolved
    fingerprint = _fingerprint_for(
        kind=kind_enum, component=_key(component), phase=phase, exc_type=exc_type,
        frames=frames, reason_code=reason_code, brake=brake,
        invariant_id=invariant_id, result_signature=result_signature,
        guard_rule=guard_rule, service_result=service_result,
        deadline_type=deadline_type, escape_type=escape_type, tier=tier,
        review_policy=review_policy)
    doc_id = incident_id or build_incident_id(
        kind_enum, task_id=task_id, attempt_id=attempt_id, run_id=run_id,
        discriminator=_discriminator(
            invariant_id=invariant_id, reason_code=reason_code,
            brake=brake, fingerprint=fingerprint))
    engine_raw: dict[str, Any] = {}
    for field, value in (
            ("exc_type", exc_type), ("frames", frames), ("reason_code", reason_code),
            ("guard_rule", guard_rule), ("brake", brake), ("invariant_id", invariant_id),
            ("project_key", project_key), ("task_id", task_id),
            ("run_id", run_id), ("attempt_id", attempt_id)):
        if value is not None:
            engine_raw[field] = value
    context_raw: dict[str, Any] = {
        "message": message or "",
        "evidence_refs": list(evidence_refs or []),
    }
    if business_ref is not None:
        context_raw["business_ref"] = business_ref
    sanitized = sanitize_incident({"engine": engine_raw, "context": context_raw})
    days = RELIABILITY_DEFAULTS["retention_days"]["incidents"] \
        if retention_days is None else retention_days
    rel = dict(release) if isinstance(release, dict) else {}
    return {
        "_id": doc_id,
        "schema_version": SCHEMA_VERSION,
        "kind": _key(kind_enum),
        "severity": severity.value
        if isinstance(severity, Severity) else str(severity),
        "attribution": attribution.value
        if isinstance(attribution, Attribution) else str(attribution),
        "attribution_rule": rule,
        "component": _key(component),
        "phase": phase,
        "release": {"sha": rel.get("sha", "unknown"), "dirty": rel.get("dirty")},
        "fingerprint": fingerprint,
        "fingerprint_version": FINGERPRINT_VERSION,
        "problem_key": problem_key(
            _scope_for(attribution, project_key), fingerprint),
        "engine": sanitized["engine"],
        "context": sanitized["context"],
        "shadow": bool(shadow),
        "late": bool(late),
        "from_reliability_ticket": bool(from_reliability_ticket),
        "glitchtip_event_id": glitchtip_event_id,
        "processed": False,
        "created_at": created,
        "expire_at": created + timedelta(days=int(days)),
    }


def resolve_collection(source: Any) -> Any | None:
    """Return the incidents collection behind a repo, db or collection."""
    return _resolve_collection(source)


def _resolve_collection(source: Any) -> Any | None:
    try:
        if source is None:
            return None
        if hasattr(source, "insert_one") and hasattr(source, "count_documents"):
            return source
        db = getattr(source, "db", None)
        if db is not None:
            try:
                return db[COLLECTION]
            except Exception:
                return None
        if hasattr(source, "__getitem__"):
            try:
                return source[COLLECTION]
            except Exception:
                return None
    except Exception:
        return None
    return None


def _spool_append(doc: dict[str, Any],
                  runtime_dir: str | Path | None,
                  max_bytes: int) -> bool:
    global _SPOOL_DROPPED
    try:
        path = spool_path(runtime_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(doc, default=str) + "\n"
        encoded = line.encode("utf-8")
        with _SPOOL_LOCK:
            size = path.stat().st_size if path.exists() else 0
            if size + len(encoded) > max_bytes:
                _SPOOL_DROPPED += 1
                logger.warning("Reliability spool full; incident discarded")
                return False
            with open(path, "ab") as fh:
                fh.write(encoded)
        return True
    except OSError:
        logger.warning("Reliability spool write failed", exc_info=True)
        with _SPOOL_LOCK:
            _SPOOL_DROPPED += 1
        return False


def _revive_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Parse ISO date strings back to datetime (BSON TTL needs Date)."""
    for field in ("created_at", "expire_at"):
        value = doc.get(field)
        if isinstance(value, str):
            try:
                moment = datetime.fromisoformat(
                    value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            doc[field] = moment
    return doc


def _read_lines(path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                docs.append(_revive_doc(doc))
    return docs


def _read_spool(runtime_dir: str | Path | None) -> list[dict[str, Any]]:
    try:
        path = spool_path(runtime_dir)
        if not path.exists():
            return []
        with _SPOOL_LOCK:
            return _read_lines(path)
    except OSError:
        logger.warning("Reliability spool read failed", exc_info=True)
        return []


def _rewrite_spool(docs: list[dict[str, Any]],
                   runtime_dir: str | Path | None) -> None:
    try:
        path = spool_path(runtime_dir)
        with _SPOOL_LOCK:
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for doc in docs:
                    fh.write(json.dumps(doc, default=str) + "\n")
            tmp.replace(path)
    except OSError:
        logger.warning("Reliability spool rewrite failed", exc_info=True)


def _drop_replayed(replayed_ids: list[Any],
                   runtime_dir: str | Path | None) -> int:
    """Remove replayed ids from the spool, keeping concurrent appends."""
    try:
        done = set(replayed_ids)
        path = spool_path(runtime_dir)
        with _SPOOL_LOCK:
            if not path.exists():
                return 0
            remaining = [doc for doc in _read_lines(path)
                         if doc.get("_id") not in done]
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for doc in remaining:
                    fh.write(json.dumps(doc, default=str) + "\n")
            tmp.replace(path)
            return len(remaining)
    except OSError:
        logger.warning("Reliability spool rewrite failed", exc_info=True)
        return -1


def _is_duplicate_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "uplicate" in name:
        return True
    return getattr(exc, "code", None) == 11000


async def _insert_async(collection: Any, doc: dict[str, Any]) -> str:
    try:
        result = await asyncio.wait_for(
            collection.insert_one(doc), timeout=INSERT_TIMEOUT_SECONDS)
        return str(getattr(result, "inserted_id", doc["_id"]))
    except Exception as exc:
        if _is_duplicate_error(exc):
            return str(doc["_id"])
        raise


def _insert_sync(collection: Any, doc: dict[str, Any]) -> str:
    box: dict[str, Any] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["result"] = collection.insert_one(doc)
        except BaseException as exc:  # noqa: BLE001 - captured, never raised
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    if not done.wait(timeout=INSERT_TIMEOUT_SECONDS):
        raise TimeoutError("reliability insert timed out")
    if "error" in box:
        exc = box["error"]
        if isinstance(exc, Exception) and _is_duplicate_error(exc):
            return str(doc["_id"])
        raise exc  # type: ignore[misc]
    return str(getattr(box.get("result"), "inserted_id", doc["_id"]))


async def _count_recent_async(collection: Any, fingerprint: str,
                              since: datetime) -> int:
    return await asyncio.wait_for(
        collection.count_documents(
            {"fingerprint": fingerprint, "created_at": {"$gte": since}}),
        timeout=INSERT_TIMEOUT_SECONDS)


def _count_recent_sync(collection: Any, fingerprint: str, since: datetime) -> int:
    return int(collection.count_documents(
        {"fingerprint": fingerprint, "created_at": {"$gte": since}}))


def _notify(doc: dict[str, Any],
            notify: Callable[[dict[str, Any]], None]
            | Callable[[dict[str, Any]], Awaitable[None]] | None) -> None:
    hook = notify if notify is not None else _NOTIFY_HOOK
    if hook is None:
        return
    try:
        result = hook(doc)
        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)
    except Exception:
        logger.warning("Reliability notify hook failed", exc_info=True)


async def replay_spool(collection: Any,
                       *,
                       runtime_dir: str | Path | None = None) -> dict[str, int]:
    """Reinsert spooled docs; duplicates count as replayed, never double."""
    outcome = {"replayed": 0, "pending": 0, "failed": 0}
    try:
        pending = _read_spool(runtime_dir)
        if not pending:
            return outcome
        coll = _resolve_collection(collection)
        if coll is None:
            outcome["pending"] = len(pending)
            return outcome
        replayed_ids: list[Any] = []
        for doc in pending:
            try:
                await _insert_async(coll, doc)
                replayed_ids.append(doc.get("_id"))
                outcome["replayed"] += 1
            except Exception:
                outcome["failed"] += 1
                break
        outcome["pending"] = _drop_replayed(replayed_ids, runtime_dir)
        return outcome
    except Exception:
        logger.warning("Reliability spool replay failed", exc_info=True)
        return outcome


def replay_spool_sync(collection: Any,
                      *,
                      runtime_dir: str | Path | None = None) -> dict[str, int]:
    """Blocking replay for the sync path; merges with concurrent appends."""
    outcome = {"replayed": 0, "pending": 0, "failed": 0}
    try:
        pending = _read_spool(runtime_dir)
        if not pending:
            return outcome
        coll = _resolve_collection(collection)
        if coll is None:
            outcome["pending"] = len(pending)
            return outcome
        replayed_ids: list[Any] = []
        for doc in pending:
            try:
                if asyncio.iscoroutinefunction(
                        getattr(coll, "insert_one", None)):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            asyncio.run, _insert_async(coll, doc))
                        future.result(
                            timeout=INSERT_TIMEOUT_SECONDS + 1)
                else:
                    _insert_sync(coll, doc)
                replayed_ids.append(doc.get("_id"))
                outcome["replayed"] += 1
            except Exception:
                outcome["failed"] += 1
                break
        outcome["pending"] = _drop_replayed(replayed_ids, runtime_dir)
        return outcome
    except Exception:
        logger.warning("Reliability spool replay failed", exc_info=True)
        return outcome


async def record_incident(
    source: Any,
    *,
    kind: str | IncidentKind,
    component: str | Component,
    max_per_fingerprint_hour: int | None = None,
    spool_max_bytes: int | None = None,
    runtime_dir: str | Path | None = None,
    notify: Callable[[dict[str, Any]], None]
    | Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    now: datetime | None = None,
    **fields: Any,
) -> str | None:
    """Persist one incident; never raises (spools when Mongo is down)."""
    try:
        return await _record_async(
            source, kind=kind, component=component,
            max_per_fingerprint_hour=max_per_fingerprint_hour,
            spool_max_bytes=spool_max_bytes, runtime_dir=runtime_dir,
            notify=notify, now=now, **fields)
    except Exception:
        logger.warning("record_incident failed", exc_info=True)
        return None


async def _record_async(
    source: Any,
    *,
    kind: str | IncidentKind,
    component: str | Component,
    max_per_fingerprint_hour: int | None,
    spool_max_bytes: int | None,
    runtime_dir: str | Path | None,
    notify: Callable[[dict[str, Any]], None]
    | Callable[[dict[str, Any]], Awaitable[None]] | None,
    now: datetime | None,
    **fields: Any,
) -> str | None:
    limit = RELIABILITY_DEFAULTS["triage"]["max_incidents_per_fingerprint_hour"] \
        if max_per_fingerprint_hour is None else max_per_fingerprint_hour
    cap_bytes = RELIABILITY_DEFAULTS["spool_max_bytes"] \
        if spool_max_bytes is None else spool_max_bytes
    doc = build_incident_doc(kind=kind, component=component, now=now, **fields)
    coll = _resolve_collection(source)
    if coll is None:
        _spool_append(doc, runtime_dir, int(cap_bytes))
        return str(doc["_id"])
    try:
        recent = await _count_recent_async(
            coll, doc["fingerprint"], doc["created_at"] - timedelta(hours=1))
    except Exception:
        _spool_append(doc, runtime_dir, int(cap_bytes))
        return str(doc["_id"])
    if recent >= int(limit):
        with _SPOOL_LOCK:
            _SUPPRESSED[doc["fingerprint"]] = \
                _SUPPRESSED.get(doc["fingerprint"], 0) + 1
        return None
    try:
        stored_id = await _insert_async(coll, doc)
    except Exception:
        _spool_append(doc, runtime_dir, int(cap_bytes))
        return str(doc["_id"])
    _notify(doc, notify)
    try:
        await replay_spool(coll, runtime_dir=runtime_dir)
    except Exception:
        logger.warning("Reliability post-write replay failed", exc_info=True)
    return stored_id


def record_incident_sync(
    source: Any,
    *,
    kind: str | IncidentKind,
    component: str | Component,
    max_per_fingerprint_hour: int | None = None,
    spool_max_bytes: int | None = None,
    runtime_dir: str | Path | None = None,
    notify: Callable[[dict[str, Any]], None] | None = None,
    now: datetime | None = None,
    **fields: Any,
) -> str | None:
    """Thread-safe blocking variant; never raises."""
    try:
        limit = RELIABILITY_DEFAULTS["triage"][
            "max_incidents_per_fingerprint_hour"] \
            if max_per_fingerprint_hour is None else max_per_fingerprint_hour
        cap_bytes = RELIABILITY_DEFAULTS["spool_max_bytes"] \
            if spool_max_bytes is None else spool_max_bytes
        doc = build_incident_doc(kind=kind, component=component, now=now, **fields)
        coll = _resolve_collection(source)
        if coll is None:
            _spool_append(doc, runtime_dir, int(cap_bytes))
            return str(doc["_id"])
        try:
            recent = _count_recent_sync(
                coll, doc["fingerprint"],
                doc["created_at"] - timedelta(hours=1))
        except Exception:
            _spool_append(doc, runtime_dir, int(cap_bytes))
            return str(doc["_id"])
        if recent >= int(limit):
            with _SPOOL_LOCK:
                _SUPPRESSED[doc["fingerprint"]] = \
                    _SUPPRESSED.get(doc["fingerprint"], 0) + 1
            return None
        try:
            if asyncio.iscoroutinefunction(getattr(coll, "insert_one", None)):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        asyncio.run, _insert_async(coll, doc))
                    stored_id = future.result(timeout=INSERT_TIMEOUT_SECONDS + 1)
            else:
                stored_id = _insert_sync(coll, doc)
        except Exception:
            _spool_append(doc, runtime_dir, int(cap_bytes))
            return str(doc["_id"])
        if notify is not None:
            try:
                notify(doc)
            except Exception:
                logger.warning("Reliability notify hook failed", exc_info=True)
        else:
            _notify(doc, None)
        try:
            replay_spool_sync(coll, runtime_dir=runtime_dir)
        except Exception:
            logger.warning("Reliability post-write replay failed",
                           exc_info=True)
        return stored_id
    except Exception:
        logger.warning("record_incident_sync failed", exc_info=True)
        return None


def read_last_exit(runtime_dir: str | Path | None = None) -> dict[str, Any] | None:
    try:
        path = runtime_dir_for(runtime_dir) / LAST_EXIT_FILENAME
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


async def record_unclean_exit(
    source: Any,
    *,
    runtime_dir: str | Path | None = None,
    release: dict[str, Any] | None = None,
    now: datetime | None = None,
    notify: Callable[[dict[str, Any]], None]
    | Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str | None:
    """Register unclean_exit from last-exit.json; idempotent by its at."""
    try:
        last = read_last_exit(runtime_dir)
        if not last or str(last.get("service_result") or "") == "success":
            return None
        at = str(last.get("at") or "unknown")
        service_result = " ".join(
            str(last.get(part) or "").strip()
            for part in ("service_result", "exit_code", "exit_status")).strip()
        incident_id = hashlib.sha256(
            f"unclean_exit|{at}".encode("utf-8")).hexdigest()
        return await record_incident(
            source, kind=IncidentKind.UNCLEAN_EXIT,
            component=Component.CORE_PROCESS, service_result=service_result,
            reason_code=service_result, incident_id=incident_id,
            release=release, runtime_dir=runtime_dir, now=now, notify=notify)
    except Exception:
        logger.warning("record_unclean_exit failed", exc_info=True)
        return None


async def ensure_reliability_indexes(collection: Any) -> None:
    """Create the §13.1 indexes; never raises (boot must not fail)."""
    try:
        coll = _resolve_collection(collection)
        if coll is None:
            return
        from pymongo import ASCENDING, DESCENDING

        await asyncio.wait_for(
            coll.create_index(
                [("problem_key", ASCENDING), ("created_at", DESCENDING)]),
            timeout=INSERT_TIMEOUT_SECONDS)
        await asyncio.wait_for(
            coll.create_index(
                [("processed", ASCENDING)],
                partialFilterExpression={"processed": False}),
            timeout=INSERT_TIMEOUT_SECONDS)
        await asyncio.wait_for(
            coll.create_index([("expire_at", ASCENDING)],
                              expireAfterSeconds=0),
            timeout=INSERT_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Reliability index setup failed", exc_info=True)


def transient_recovered(probe: str | BaseException | None) -> bool:
    """True when the engine recovered a transient error: WARNING, no incident."""
    try:
        from orchestration.provider_recovery import transient_provider_error

        text = probe if isinstance(probe, str) else str(probe or "")
        return bool(transient_provider_error(text))
    except Exception:
        return False
