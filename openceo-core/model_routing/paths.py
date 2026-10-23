"""Runtime paths for the free-model router (TASK-2026-00285).

Honours ``XDG_CONFIG_HOME``/``XDG_STATE_HOME`` per SPEC §6.1; relative
or empty XDG values fall back to ``~``. Secrets stay in
``openceo-core/.env.free-router.local`` (epic TASK-2026-00283 decision,
deviating from SPEC §6.1 ``secrets.env``), never under these
directories. F0 subset: ``gateway.generated.yaml``, ``updater.log``,
``gateway.log`` and ``reviews/`` arrive with their own tickets. Pure
path joins; creates nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "AVAILABILITY_DB_FILENAME",
    "CATALOG_FILENAME",
    "CONFIG_DIRNAME",
    "HISTORY_DIRNAME",
    "POLICY_FILENAME",
    "PROVIDERS_FILENAME",
    "RANKINGS_FILENAME",
    "STATE_DIRNAME",
    "UPDATER_LOCK_FILENAME",
    "UPDATER_LOG_FILENAME",
    "availability_db_path",
    "catalog_path",
    "config_dir",
    "history_dir",
    "policy_path",
    "providers_path",
    "rankings_path",
    "state_dir",
]

CONFIG_DIRNAME = "free-router"
STATE_DIRNAME = "free-router"
POLICY_FILENAME = "policy.yaml"
PROVIDERS_FILENAME = "providers.yaml"
CATALOG_FILENAME = "catalog.json"
RANKINGS_FILENAME = "rankings.json"
HISTORY_DIRNAME = "history"
AVAILABILITY_DB_FILENAME = "availability.sqlite"
UPDATER_LOCK_FILENAME = "update.lock"
UPDATER_LOG_FILENAME = "updater.log"


def _xdg_base(var: str, fallback: Path) -> Path:
    """XDG base or ``fallback``; relative values are ignored per convention."""
    raw = os.environ.get(var)
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return fallback


def config_dir() -> Path:
    """``~/.config/free-router`` (SPEC §6.1)."""
    return _xdg_base("XDG_CONFIG_HOME", Path.home() / ".config") / CONFIG_DIRNAME


def state_dir() -> Path:
    """``~/.local/state/free-router`` (SPEC §6.1)."""
    return _xdg_base("XDG_STATE_HOME", Path.home() / ".local" / "state") / STATE_DIRNAME


def policy_path() -> Path:
    """Approved persistent rules."""
    return config_dir() / POLICY_FILENAME


def providers_path() -> Path:
    """Providers and account types, without keys."""
    return config_dir() / PROVIDERS_FILENAME


def catalog_path() -> Path:
    """Enriched inventory published by the catalog pipeline."""
    return config_dir() / CATALOG_FILENAME


def rankings_path() -> Path:
    """Per-profile order with provenance."""
    return config_dir() / RANKINGS_FILENAME


def history_dir() -> Path:
    """Immutable published snapshots."""
    return config_dir() / HISTORY_DIRNAME


def availability_db_path() -> Path:
    """Health, cooldowns, usage, locks and reservations store."""
    return state_dir() / AVAILABILITY_DB_FILENAME
