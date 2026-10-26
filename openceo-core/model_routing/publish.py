"""Atomic catalog publication with immutable history snapshots (TASK-2026-00287).

Pipeline (SPEC §12.2): validate → temp file → fsync → atomic rename,
with the history snapshot written first so every published catalog has
one. A failure before the final rename leaves the previous catalog
byte-identical (T12 catalog part); a failed source keeps serving the
last good content marked stale instead of emptying the catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from model_routing.models import (
    CATALOG_SCHEMA_VERSION,
    CatalogDocument,
    migrate_catalog_v1_to_v2,
)
from model_routing.paths import CATALOG_FILENAME, HISTORY_DIRNAME

__all__ = [
    "PublishError",
    "SnapshotRecord",
    "atomic_write_json",
    "load_catalog_doc",
    "mark_catalog_stale",
    "publish_catalog",
]


class PublishError(RuntimeError):
    """Publication failed; the previous catalog is untouched."""


@dataclass(frozen=True)
class SnapshotRecord:
    """Provenance of one publication."""

    snapshot_id: str
    sha256: str
    created_at: str
    stale: bool
    stale_reason: str | None
    price_changes: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write_json(path: Path, payload: str) -> None:
    """Write through temp + fsync + atomic rename; cleans up on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_catalog_doc(path: Path) -> CatalogDocument:
    """Read and validate a published catalog; v1 snapshots migrate to v2."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PublishError(f"catalog missing or unreadable: {path}") from exc
    except ValueError as exc:
        raise PublishError(f"catalog {path} is not valid JSON") from exc
    if isinstance(data, dict) and data.get("schema_version") == 1:
        try:
            data = migrate_catalog_v1_to_v2(data)
        except ValueError as exc:
            raise PublishError(f"catalog {path} failed v1 migration") from exc
    elif not isinstance(data, dict) or data.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise PublishError(f"catalog {path} has an unsupported schema_version")
    try:
        return CatalogDocument.model_validate(data)
    except ValidationError as exc:
        raise PublishError(f"catalog {path} failed schema validation") from exc


def _free_conditions(doc: CatalogDocument) -> dict[str, tuple[float | None, float | None, bool]]:
    return {
        deployment.deployment_id: (
            deployment.input_price_usd,
            deployment.output_price_usd,
            deployment.free_offered,
        )
        for entry in doc.entries
        for deployment in entry.deployments
    }


def _demote_changed_prices(doc: CatalogDocument, previous: CatalogDocument | None) -> tuple[str, ...]:
    """Force re-verification where price or free_offered changed (T11)."""
    if previous is None:
        return ()
    old = _free_conditions(previous)
    changed: list[str] = []
    for entry in doc.entries:
        for deployment in entry.deployments:
            current = (
                deployment.input_price_usd,
                deployment.output_price_usd,
                deployment.free_offered,
            )
            if deployment.deployment_id in old and old[deployment.deployment_id] != current:
                deployment.free_verified = False
                changed.append(deployment.deployment_id)
    return tuple(changed)


def publish_catalog(
    doc: CatalogDocument,
    config_dir: Path,
    *,
    stale: bool = False,
    stale_reason: str | None = None,
) -> SnapshotRecord:
    """Validate, demote changed prices, and publish catalog + snapshot."""
    if doc.schema_version != CatalogDocument.model_fields["schema_version"].default:
        raise PublishError(f"unsupported catalog schema_version: {doc.schema_version}")
    catalog_file = config_dir / CATALOG_FILENAME
    previous = load_catalog_doc(catalog_file) if catalog_file.is_file() else None
    doc = doc.model_copy(deep=True)
    changed = _demote_changed_prices(doc, previous)
    doc = doc.model_copy(update={"stale": stale, "stale_reason": stale_reason})
    payload = doc.model_dump_json(indent=2)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    created = _utc_now()
    snapshot_id = f"catalog-{created.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"
    try:
        atomic_write_json(config_dir / HISTORY_DIRNAME / f"{snapshot_id}.json", payload)
        atomic_write_json(catalog_file, payload)
    except OSError as exc:
        raise PublishError(f"publication failed for {catalog_file}") from exc
    return SnapshotRecord(
        snapshot_id=snapshot_id,
        sha256=digest,
        created_at=created.isoformat(),
        stale=stale,
        stale_reason=stale_reason,
        price_changes=changed,
    )


def mark_catalog_stale(config_dir: Path, reason: str) -> SnapshotRecord:
    """Keep serving the last good catalog, flagged stale (SPEC §12.2)."""
    doc = load_catalog_doc(config_dir / CATALOG_FILENAME)
    return publish_catalog(doc, config_dir, stale=True, stale_reason=reason)
