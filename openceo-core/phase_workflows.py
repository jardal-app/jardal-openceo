from __future__ import annotations

from copy import deepcopy
from typing import Any

from state import AgentName, TaskStatus, WorkflowPhaseType

HUMAN_PHASE_PERMISSION_ALIASES = {
    "client_signoff": "client_review",
}


def default_workflow_templates() -> dict[str, dict[str, Any]]:
    """System defaults. Projects can override these in Mongo."""
    return {
        "dev.default": {
            "name": "Dev con revision QA",
            "description": "Trabajo dev, revision QA interna y cierre.",
            "default": True,
            "agent": AgentName.DEV.value,
            "phases": [
                {
                    "key": "dev_work",
                    "label": "Trabajo DEV",
                    "type": WorkflowPhaseType.AGENT.value,
                    "agent": AgentName.DEV.value,
                },
                {
                    "key": "qa_review",
                    "label": "Revision QA",
                    "type": WorkflowPhaseType.HUMAN.value,
                    "permission": "qa_review",
                    "on_reject": "restart",
                },
                {
                    "key": "completed",
                    "label": "Completado",
                    "type": WorkflowPhaseType.TERMINAL.value,
                    "status": TaskStatus.COMPLETED.value,
                },
            ],
        },
        "dev.client_visible": {
            "name": "Dev con QA y aprobacion cliente",
            "description": "Trabajo dev, QA interna, aprobacion del cliente y cierre.",
            "default": False,
            "agent": AgentName.DEV.value,
            "phases": [
                {
                    "key": "dev_work",
                    "label": "Trabajo DEV",
                    "type": WorkflowPhaseType.AGENT.value,
                    "agent": AgentName.DEV.value,
                },
                {
                    "key": "qa_review",
                    "label": "Revision QA",
                    "type": WorkflowPhaseType.HUMAN.value,
                    "permission": "qa_review",
                    "on_reject": "restart",
                },
                {
                    "key": "client_signoff",
                    "label": "Aprobacion cliente",
                    "type": WorkflowPhaseType.HUMAN.value,
                    "permission": "client_review",
                    "client_visible": True,
                    "on_reject": "restart",
                },
                {
                    "key": "completed",
                    "label": "Completado",
                    "type": WorkflowPhaseType.TERMINAL.value,
                    "status": TaskStatus.COMPLETED.value,
                },
            ],
        },
    }


def merge_workflow_templates(project_templates: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    merged = default_workflow_templates()
    if isinstance(project_templates, dict):
        for key, raw_template in project_templates.items():
            if isinstance(raw_template, dict):
                merged[str(key)] = normalize_workflow_template(str(key), raw_template)
    return merged


def normalize_workflow_template(key: str, template: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(template)
    normalized["key"] = str(normalized.get("key") or key)
    normalized.setdefault("name", normalized["key"])
    normalized.setdefault("description", "")
    normalized.setdefault("default", False)
    normalized["phases"] = normalize_phases(normalized.get("phases") or [])
    return normalized


def normalize_phases(phases: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw_phase in enumerate(phases):
        if isinstance(raw_phase, str):
            phase = {"key": raw_phase}
        elif isinstance(raw_phase, dict):
            phase = dict(raw_phase)
        else:
            continue
        key = str(phase.get("key") or phase.get("id") or "").strip()
        if not key:
            continue
        phase["key"] = key
        phase.setdefault("label", key.replace("_", " ").title())
        phase_type = str(phase.get("type") or "").strip().lower()
        if not phase_type:
            if index == len(phases) - 1 and key == "completed":
                phase_type = WorkflowPhaseType.TERMINAL.value
            elif key.endswith("_work"):
                phase_type = WorkflowPhaseType.AGENT.value
                phase.setdefault("agent", key.removesuffix("_work"))
            else:
                phase_type = WorkflowPhaseType.HUMAN.value
        phase["type"] = phase_type
        if phase_type == WorkflowPhaseType.AGENT.value:
            phase.setdefault("agent", AgentName.CEO.value)
        if phase_type == WorkflowPhaseType.HUMAN.value:
            phase.setdefault("permission", HUMAN_PHASE_PERMISSION_ALIASES.get(key, key))
            if key == "client_signoff":
                phase.setdefault("client_visible", True)
        if phase_type == WorkflowPhaseType.TERMINAL.value:
            phase.setdefault("status", TaskStatus.COMPLETED.value)
        normalized.append(phase)
    return normalized


def phase_key(phase: dict[str, Any] | None) -> str | None:
    if not isinstance(phase, dict):
        return None
    key = str(phase.get("key") or "").strip()
    return key or None


def phase_label(phase: dict[str, Any] | None) -> str:
    if not isinstance(phase, dict):
        return ""
    return str(phase.get("label") or phase.get("key") or "").strip()


def find_phase_index(phases: list[dict[str, Any]], key: str | None) -> int:
    if not key:
        return 0
    for index, phase in enumerate(phases):
        if phase_key(phase) == key:
            return index
    return 0


def first_executable_phase_index(phases: list[dict[str, Any]]) -> int:
    for index, phase in enumerate(phases):
        if str(phase.get("type") or "") != WorkflowPhaseType.TERMINAL.value:
            return index
    return 0


def resolve_template_key(
    templates: dict[str, dict[str, Any]],
    *,
    requested_key: str | None,
    agent: str | None,
    ticket_type: str | None,
) -> str | None:
    if requested_key and requested_key in templates:
        return requested_key

    clean_agent = str(agent or "").strip().lower()
    clean_type = str(ticket_type or "default").strip().lower()
    for candidate in (f"{clean_agent}.{clean_type}", f"{clean_agent}.default"):
        if clean_agent and candidate in templates:
            return candidate

    for key, template in templates.items():
        if clean_agent and str(template.get("agent") or "").strip().lower() != clean_agent:
            continue
        if template.get("default"):
            return key

    return None
