"""Identidad de release (TASK-2026-00367, diseno v2 ap. 16.5).

El script de despliegue escribe ``.runtime/RELEASE`` con el SHA exacto que
se ejecuta (``{"sha", "dirty", "at"}``). El core y el runner lo leen al
arrancar y lo exponen en health y logs, para medir regresiones tras la
release R. Sin fichero (desarrollo local), la identidad es desconocida.
"""

from __future__ import annotations

import json
from pathlib import Path

RELEASE_FILENAME = "RELEASE"

__all__ = ["RELEASE_FILENAME", "read_release", "release_file"]


def release_file(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".runtime" / RELEASE_FILENAME


def read_release(repo_root: str | Path) -> dict:
    try:
        data = json.loads(release_file(repo_root).read_text())
    except (OSError, ValueError):
        return {"sha": "unknown", "dirty": None, "at": None}
    sha = data.get("sha") or "unknown"
    dirty = data.get("dirty")
    dirty = bool(dirty) if isinstance(dirty, bool) else None
    at = data.get("at") if isinstance(data.get("at"), str) else None
    return {"sha": str(sha), "dirty": dirty, "at": at}
