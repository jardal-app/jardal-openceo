"""Typed contracts for the free-model router (TASK-2026-00285).

F0 skeleton: only the status-report contract. Catalog, ranking and
availability models arrive with their own tickets (00287, 00291, 00292).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from model_routing.paths import catalog_path, config_dir, rankings_path, state_dir

__all__ = ["MODULE_PHASE", "STATUS_SCHEMA_VERSION", "StatusReport", "collect_status"]

STATUS_SCHEMA_VERSION = 1
MODULE_PHASE = "F0"


class StatusReport(BaseModel):
    """Result of ``free-router status``; validated on every read."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = STATUS_SCHEMA_VERSION
    phase: str = MODULE_PHASE
    config_dir: str
    state_dir: str
    config_present: bool
    state_present: bool
    catalog_present: bool
    rankings_present: bool
    # Reserved placeholder in F0; the snapshot series arrives with TASK-2026-00291.
    snapshot: str | None = None


def collect_status() -> StatusReport:
    """Read-only filesystem probe behind ``free-router status``."""
    config = config_dir()
    state = state_dir()
    return StatusReport(
        config_dir=str(config),
        state_dir=str(state),
        config_present=config.is_dir(),
        state_present=state.is_dir(),
        catalog_present=catalog_path().is_file(),
        rankings_present=rankings_path().is_file(),
    )
