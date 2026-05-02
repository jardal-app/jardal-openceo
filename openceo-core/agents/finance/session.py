from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agents.finance.elfec import run_elfec_house_payment_practice
from settings import Settings
from state import TaskStatus


@dataclass
class FinanceExecutionOutput:
    status: TaskStatus
    steps: list[dict[str, Any]] = field(default_factory=list)
    last_response_summary: str | None = None
    error: str | None = None


class FinanceSessionManager:
    """Minimal deterministic finance runtime for ELFEC practice payments."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any] | None = None,
    ) -> FinanceExecutionOutput:
        output = FinanceExecutionOutput(status=TaskStatus.IN_PROGRESS)
        workflow = self._resolve_workflow(task, project_config or {})
        if workflow is None:
            detail = (
                "No encontré un workflow de finanzas compatible. "
                "Para este repo el flujo soportado es 'Pago Elfec House' (alias 'Pago Luz Casa')."
            )
            output.steps.append(self._step("resolve_finance_workflow", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output

        output.steps.append(
            self._step(
                "resolve_finance_workflow",
                "ok",
                f"Workflow resuelto: {workflow.get('name') or workflow.get('id') or 'pago_elfec_house'}",
            )
        )

        handler = str(workflow.get("handler") or workflow.get("id") or "").strip().lower()
        if handler not in {"elfec_house_payment", "pago_elfec_house"}:
            detail = f"Handler de finanzas no soportado: {handler or 'desconocido'}"
            output.steps.append(self._step("execute_finance_workflow", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output

        try:
            result = await asyncio.to_thread(
                run_elfec_house_payment_practice,
                self._settings,
                workflow,
            )
        except Exception as exc:
            output.steps.append(self._step("execute_finance_workflow", "failed", str(exc)))
            output.status = TaskStatus.FAILED
            output.error = str(exc)
            return output

        summary = str(result.get("summary") or "").strip()
        output.steps.append(self._step("execute_finance_workflow", "ok", summary or "Flujo ELFEC ejecutado."))
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = summary or "Flujo ELFEC ejecutado."
        return output

    def _resolve_workflow(self, task: dict[str, Any], project_config: dict[str, Any]) -> dict[str, Any] | None:
        blob = " ".join(
            part.strip().lower()
            for part in [str(task.get("title") or ""), str(task.get("description") or "")]
            if part and str(part).strip()
        )

        workflows = (((project_config or {}).get("finance") or {}).get("workflows") or [])
        for workflow in workflows:
            candidates = [
                str(workflow.get("id") or "").strip().lower(),
                str(workflow.get("name") or "").strip().lower(),
                *[str(item).strip().lower() for item in workflow.get("aliases", [])],
                *[str(item).strip().lower() for item in workflow.get("triggers", [])],
            ]
            if any(candidate and candidate in blob for candidate in candidates):
                return workflow

        if "elfec" in blob and any(token in blob for token in {"house", "casa", "luz"}):
            return {
                "id": "pago_elfec_house",
                "name": "Pago Elfec House",
                "handler": "elfec_house_payment",
                "expected_row_label": "House",
            }
        return None

    @staticmethod
    def _step(step: str, status: str, detail: str) -> dict[str, Any]:
        return {
            "step": step,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc),
        }
