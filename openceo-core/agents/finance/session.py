from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agents.finance import contribution as contribution_flow
from agents.finance import scope_summary as scope_flow
from agents.finance import voice_expense as expense_flow
from agents.finance import voice_income as income_flow
from agents.finance.elfec import run_elfec_house_payment_practice
from agents.finance.intent import BALANCE, CONTRIBUTION, EXPENSE, INCOME, classify
from agents.finance.money_text import MissingExchangeRateError
from settings import Settings
from state import TaskStatus


@dataclass
class FinanceExecutionOutput:
    status: TaskStatus
    steps: list[dict[str, Any]] = field(default_factory=list)
    last_response_summary: str | None = None
    error: str | None = None


class FinanceSessionManager:
    """Runtime determinista de finanzas: ingresos, gastos, lecturas y ELFEC.

    Los cuatro flujos de ERPNext se resuelven sin tickets: el registro (o la
    consulta) es el efecto. El dispatch lo decide `intent.classify`, un solo
    lugar para la precedencia entre ingreso, aporte, saldo y gasto.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any] | None = None,
    ) -> FinanceExecutionOutput:
        output = FinanceExecutionOutput(status=TaskStatus.IN_PROGRESS)
        title = str(task.get("title") or "").strip()
        desc = str(task.get("description") or "").strip()
        if title and title in desc:
            text = desc
        elif desc and desc in title:
            text = title
        else:
            text = f"{title} {desc}".strip()

        intent = classify(text)
        if intent == INCOME:
            return await self._run_income(output, text)
        if intent == CONTRIBUTION:
            return await self._run_contribution(output)
        if intent == BALANCE:
            return await self._run_balance(output, text)
        if intent == EXPENSE:
            return await self._run_expense(output, text)

        workflow = self._resolve_workflow(task, project_config or {})
        if workflow is None:
            detail = (
                "No encontré un workflow de finanzas compatible. "
                "Puedo registrar gastos ('registra gasto personal carne 50 bolivianos'), "
                "registrar ingresos ('registra ingreso sueldo 6200 bolivianos'), "
                "consultar el saldo ('cuánto gastamos este mes'), "
                "consultar la regla del 10% ('cómo vamos con el diezmo') "
                "o practicar el 'Pago Elfec House'."
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

    async def _run_expense(self, output: FinanceExecutionOutput, text: str) -> FinanceExecutionOutput:
        """Registra un gasto ocasional en ERPNext. Sin tickets: el registro es el efecto."""
        output.steps.append(self._step("resolve_finance_workflow", "ok", "Workflow resuelto: gasto ocasional por voz"))
        parsed = expense_flow.parse_expense(text)
        if parsed is None:
            detail = (
                "No encontré un monto válido para registrar. "
                "Dime por ejemplo: 'registra gasto personal carne 50 bolivianos'."
            )
            output.steps.append(self._step("register_expense", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        try:
            result = await self._with_client(
                lambda client: expense_flow.register_expense(
                    client, parsed, ref=expense_flow.new_openceo_ref()
                )
            )
        except MissingExchangeRateError as exc:
            detail = str(exc)
            output.steps.append(self._step("register_expense", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        except Exception as exc:
            detail = f"No pude registrar el gasto en ERPNext: {exc}"
            output.steps.append(self._step("register_expense", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        summary = expense_flow.build_spoken_summary(parsed, result["doc"], result["duplicate"])
        output.steps.append(self._step("register_expense", "ok", summary))
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = summary
        return output

    async def _run_income(self, output: FinanceExecutionOutput, text: str) -> FinanceExecutionOutput:
        """Registra un ingreso ya cobrado en ERPNext (TASK-2026-00053).

        Espejo del gasto con el asiento invertido: debito a efectivo, credito a
        la cuenta de ingreso real. Sin tickets: el registro es el efecto.
        """
        output.steps.append(self._step("resolve_finance_workflow", "ok", "Workflow resuelto: ingreso ocasional por voz"))
        parsed = income_flow.parse_income(text)
        if parsed is None:
            detail = (
                "No encontré un monto válido para el ingreso. "
                "Dime por ejemplo: 'registra ingreso sueldo 6200 bolivianos'."
            )
            output.steps.append(self._step("register_income", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        try:
            result = await self._with_client(
                lambda client: income_flow.register_income(
                    client, parsed, ref=income_flow.new_openceo_ref()
                )
            )
        except MissingExchangeRateError as exc:
            detail = str(exc)
            output.steps.append(self._step("register_income", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        except Exception as exc:
            detail = f"No pude registrar el ingreso en ERPNext: {exc}"
            output.steps.append(self._step("register_income", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output
        summary = income_flow.build_spoken_summary(parsed, result["doc"], result["duplicate"])
        output.steps.append(self._step("register_income", "ok", summary))
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = summary
        return output

    async def _run_balance(self, output: FinanceExecutionOutput, text: str) -> FinanceExecutionOutput:
        """Consulta de saldo/gasto (TASK-2026-00044). Solo lectura.

        El texto importa: "cuánto gastamos este mes" acota el período, y sin
        pasar las cotas la lectura devolvía el total histórico sin avisar.
        """
        output.steps.append(self._step("resolve_finance_workflow", "ok", "Workflow resuelto: resumen de saldos por Scope"))
        date_from, date_to = scope_flow.period_bounds(text)
        try:
            summary = await self._with_client(
                lambda client: scope_flow.get_scope_summary(
                    client, date_from=date_from, date_to=date_to
                )
            )
        except Exception as exc:
            detail = f"No pude consultar el saldo en ERPNext: {exc}"
            output.steps.append(self._step("get_scope_summary", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = "No pude consultar el saldo ahora mismo. Lo intentamos de nuevo."
            return output
        spoken = scope_flow.build_spoken_summary(summary)
        output.steps.append(self._step("get_scope_summary", "ok", spoken))
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = spoken
        return output

    async def _run_contribution(self, output: FinanceExecutionOutput) -> FinanceExecutionOutput:
        """Estado de la regla del 10% (TASK-2026-00054). Solo lectura."""
        output.steps.append(self._step("resolve_finance_workflow", "ok", "Workflow resuelto: regla del 10%"))
        try:
            status = await self._with_client(contribution_flow.get_contribution_10_status)
        except Exception as exc:
            detail = f"No pude calcular la regla del 10% desde ERPNext: {exc}"
            output.steps.append(self._step("get_contribution_10_status", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = "No pude consultar el estado del aporte ahora mismo. Lo intentamos de nuevo."
            return output
        spoken = contribution_flow.build_spoken_summary(status)
        output.steps.append(
            self._step("get_contribution_10_status", status.state, spoken)
        )
        # `needs_input` no es un fallo: falta una decision del dueno (la cuenta
        # que recibe el aporte). La respuesta es util y se entrega igual.
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = spoken
        return output

    async def _with_client(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        from tools.erpnext_client import ERPNextClient

        client = ERPNextClient(self._settings)
        try:
            return await fn(client)
        finally:
            await client.aclose()

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
