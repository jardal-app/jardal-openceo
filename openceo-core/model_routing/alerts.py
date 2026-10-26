"""Event-driven router alerts (TASK-2026-00297, SPEC §15 F6).

Three events append one JSON line to ``alerts.jsonl`` next to the
component that owns them (refresh failures and empty catalogs under
the state dir, gateway freezes next to the freeze marker): no
polling, no sweeping, expiry never needed. Alerts carry codes and
counts, never prompts, responses or credentials. Emission never
breaks the main path: failures to write are logged and reported
as ``None``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from model_routing.errors import sanitize_excerpt
from model_routing.paths import ALERTS_FILENAME

__all__ = [
    "Alert",
    "AlertKind",
    "emit_alert",
    "list_alerts",
]

AlertKind = Literal["no_free_models", "refresh_failed", "gateway_security"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    """One persisted alert: kind, source and a sanitized detail."""

    ts: str
    kind: str
    source: str
    detail: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_alert(
    directory: Path,
    kind: AlertKind,
    source: str,
    detail: str = "",
    *,
    now: str | None = None,
) -> Alert | None:
    """Append one alert; ``None`` when the file cannot be written."""
    alert = Alert(
        ts=now or _utc_now_iso(), kind=kind,
        source=source, detail=sanitize_excerpt(detail, limit=500))
    target = Path(directory) / ALERTS_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": alert.ts, "kind": alert.kind,
                "source": alert.source, "detail": alert.detail,
            }) + "\n")
    except OSError:
        logger.warning("router alert unwritable: %s %s", kind, source)
        return None
    return alert


def list_alerts(
    directory: Path,
    *,
    kind: str | None = None,
) -> list[Alert]:
    """Every alert in this directory, oldest first; corrupt lines are skipped."""
    target = Path(directory) / ALERTS_FILENAME
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    alerts: list[Alert] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        entry = Alert(
            ts=str(payload.get("ts") or ""), kind=str(payload.get("kind") or ""),
            source=str(payload.get("source") or ""),
            detail=str(payload.get("detail") or ""))
        if kind is not None and entry.kind != kind:
            continue
        alerts.append(entry)
    return alerts
