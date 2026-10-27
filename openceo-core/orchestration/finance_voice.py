"""Sync exact finance skills for voice/chat (TASK-2026-00221).

The chat layer names the skill; this validates it against the live
catalog, runs its exact workflow on the YAML engine and returns the
spoken summary. The record (or the read) in Jardal ERP is the effect:
no tickets, no planner. The engine summarizes runs generically, so
the spoken line is read back from the persisted run steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from catalog import Catalog, load_catalog
from orchestration.generic_agent import resolve_exact_skill_path

logger = logging.getLogger(__name__)

__all__ = ["FinanceVoiceWiring", "run_finance_skill"]


@dataclass(frozen=True)
class FinanceVoiceWiring:
    """What a sync skill run needs (main wires the globals)."""

    settings: Any
    workflow_service: Any
    repo: Any
    catalog: Catalog | None = None


async def run_finance_skill(
    skill: str, text: str, wiring: FinanceVoiceWiring,
) -> str | None:
    """Run one exact finance skill; the spoken summary or None."""
    skill_name = str(skill or "").strip()
    clean = str(text or "").strip()
    try:
        catalog = wiring.catalog or load_catalog()
        card = catalog.agents.get("finance")
        if card is None or not card.enabled \
                or skill_name not in list(card.skills or []):
            return None
        workflow_path = resolve_exact_skill_path(
            f"skills/finance/{skill_name}")
    except Exception:
        logger.warning("Finance skill resolve failed", exc_info=True)
        return None
    if skill_name == "pago-elfec":
        # The practice parks for Owner approval and resumes on the
        # ticket; a sync voice turn has no real origin to resume, so
        # it never runs here.
        return ("La práctica de Elfec pide mi aprobación y se reanuda "
                "desde el ticket. Lánzala con @finance paga la luz de "
                "la casa.")
    if workflow_path is None:
        logger.warning("Finance skill %s carries no exact workflow",
                       skill_name)
        return None
    task = {
        "task_id": f"finance-voice-{uuid4().hex}",
        "project": wiring.settings.DEFAULT_PROJECT,
        "title": clean.splitlines()[0][:120] if clean else "Finanzas por voz",
        "description": clean,
        "attachments": [],
        "params": {},
        "priority": "medium",
    }
    try:
        workflow = wiring.workflow_service.parse_workflow_text(
            workflow_path.read_text(encoding="utf-8"),
            source=str(workflow_path))
        outcome = await wiring.workflow_service.execute_task_workflow(
            task, workflow)
    except Exception as exc:
        logger.warning("Finance skill run failed: %s", exc)
        return ("No pude resolver eso con finanzas ahora mismo. "
                "Lo intentamos de nuevo.")
    summary = await _spoken_summary(task["task_id"], wiring)
    return summary or outcome.last_response_summary or outcome.error


async def _spoken_summary(task_id: str,
                          wiring: FinanceVoiceWiring) -> str | None:
    """First spoken line of the skill run.

    The finance tools already answer one, persisted on the run steps.
    A missing line falls back to the engine summary, never to silence.
    """
    try:
        run = await wiring.repo.latest_workflow_run_for_task(str(task_id))
    except Exception:
        logger.warning("Finance skill run readback failed", exc_info=True)
        return None
    for item in (run or {}).get("steps") or []:
        output = (item or {}).get("output") or {}
        if not isinstance(output, dict):
            continue
        line = str(output.get("summary") or output.get("reason")
                   or output.get("message") or "").strip()
        if line:
            return line
    return None
