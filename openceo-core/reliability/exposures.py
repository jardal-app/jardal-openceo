"""Per-day exposure counters: the verification denominator (design v2 11.1, 13.3).

One document per ``(component, project, day)`` in
``reliability_exposures`` with a ``$inc`` counter and a 180-day TTL.
Every counting point calls :func:`record_exposure`, which never raises:
without a real store the exposure is dropped (a lost count only widens
the verification margin; incidents keep the spool because a lost
incident hides a failure). The runner reports through the Unix socket
(TASK-2026-00379); every other point calls here in-process.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RELIABILITY_DEFAULTS
from .models import SCHEMA_VERSION, Component

logger = logging.getLogger(__name__)

EXPOSURES_COLLECTION = "reliability_exposures"


def _retention_days() -> int:
    """TTL in days; the package import already fail-closes on bad config."""
    return int(RELIABILITY_DEFAULTS["retention_days"]["exposures"])


def _as_utc(moment: datetime | None = None) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def normalize_project(project_key: Any) -> str:
    """Blank or missing project keys count under ``unknown``."""
    return str(project_key or "").strip() or "unknown"


def exposure_day_id(component: str, project_key: Any, now: datetime) -> str:
    """``<component>|<project>|<AAAA-MM-DD>`` (§13.3)."""
    return f"{component}|{normalize_project(project_key)}|{now.strftime('%Y-%m-%d')}"


def build_exposure_update(
    component: str, project_key: Any, now: datetime
) -> tuple[str, dict[str, Any]]:
    """Filter plus upsert update for one exposure (also used by the socket)."""
    project = normalize_project(project_key)
    day = now.strftime("%Y-%m-%d")
    day_id = f"{component}|{project}|{day}"
    return day_id, {
        "$inc": {"count": 1},
        "$set": {
            "updated_at": now,
            "component": component,
            "project": project,
            "day": day,
            "schema_version": SCHEMA_VERSION,
            "expire_at": now + timedelta(days=_retention_days()),
        },
        "$setOnInsert": {"created_at": now},
    }


def _is_mock(obj: Any) -> bool:
    return type(obj).__module__.split(".")[0] == "unittest" \
        and "mock" in type(obj).__module__


def resolve_exposures(source: Any) -> Any | None:
    """Exposures collection behind a repo, db or collection; else None (skip).

    A collection with ``insert_one``/``count_documents`` is the incidents
    collection, not the exposures one: it only resolves through its
    ``database`` when one is available. Mocks and unknown shapes skip.
    """
    try:
        if source is None or _is_mock(source):
            return None
        name = str(getattr(source, "name", "") or "")
        if callable(getattr(source, "update_one", None)):
            if name == EXPOSURES_COLLECTION:
                return source
            if callable(getattr(source, "insert_one", None)) or callable(
                getattr(source, "count_documents", None)
            ):
                database = getattr(source, "database", None)
                if database is None:
                    return None
                try:
                    return database[EXPOSURES_COLLECTION]
                except (AttributeError, KeyError, TypeError):
                    logger.debug("exposures database unreadable (skip)",
                                 exc_info=True)
                    return None
            return source
        db = getattr(source, "db", None)
        if db is not None and not _is_mock(db):
            try:
                if hasattr(db, "__getitem__"):
                    return db[EXPOSURES_COLLECTION]
            except (AttributeError, KeyError, TypeError):
                logger.debug("exposures db unreadable (skip)", exc_info=True)
                return None
            return None
        if hasattr(source, "__getitem__"):
            try:
                return source[EXPOSURES_COLLECTION]
            except (AttributeError, KeyError, TypeError):
                logger.debug("exposures source unreadable (skip)",
                             exc_info=True)
                return None
    except Exception:
        logger.debug("exposures source unreadable (skip)", exc_info=True)
        return None
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _component_value(component: str | Component) -> str | None:
    try:
        if isinstance(component, Component):
            return component.value
        return Component(str(component)).value
    except ValueError:
        return None


async def record_exposure(
    source: Any,
    component: str | Component,
    *,
    project_key: Any = None,
    task_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Count one pass through ``component``; True when counted, never raises."""
    try:
        value = _component_value(component)
        if value is None:
            logger.warning("exposure skipped: unknown component %r", component)
            return False
        moment = _as_utc(now)
        coll = resolve_exposures(source)
        if coll is None:
            return False
        day_id, update = build_exposure_update(value, project_key, moment)
        await _maybe_await(coll.update_one({"_id": day_id}, update, upsert=True))
        return True
    except Exception:
        logger.warning("exposure record failed (fail-open)", exc_info=True)
        return False


async def ensure_exposure_indexes(collection: Any) -> int:
    """Create the §13.3 TTL plus the verification read index; never raises."""
    if collection is None:
        return 0
    created = 0
    try:
        from pymongo import ASCENDING

        specs = [
            ([("expire_at", ASCENDING)], {"expireAfterSeconds": 0}),
            ([("component", ASCENDING), ("day", ASCENDING)], {}),
        ]
        for keys, options in specs:
            try:
                await _maybe_await(
                    collection.create_index(keys, **options))
                created += 1
            except Exception:
                logger.warning("exposure index %s failed (fail-open)",
                               keys, exc_info=True)
    except Exception:
        logger.warning("exposure index setup failed (fail-open)", exc_info=True)
    return created
