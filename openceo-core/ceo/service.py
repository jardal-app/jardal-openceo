"""Company-level CEO wiring (F4 TASK-2026-00119).

:mod:`ceo.review` is pure flow over protocols; this module binds it to
production: the mandate file, the ticket API (ERPNext first), the ERP
group-aware objective service, the live one-shot executor and the
global config. The CEO is company-wide, so the ``ceo`` block is read
from the global defaults — projects must not set it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from company_mandate import MandatoEmpresa
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    MongoObjectiveStore,
    MongoObjectiveTaskLister,
)
from objectives.service import create_internal_ticket
from orchestration.live import ProcessExecutor
from orchestration.role_muse import ReadonlyBadgeProvider, issuer_for_repo
from ceo.models import CEOKpiReading
from ceo.review import (
    CEO_BUSINESS_ROWS,
    CEO_EXECUTOR_ROLE,
    CEOEnv,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CEOService",
    "SUPPORTED_KPI_SOURCES",
    "ceo_wiring_config",
]

#: Closed KPI-source vocabulary the CEO can measure today (scope §2).
#: ``erpnext.finance.*`` stays unknown until the Finance frontier
#: (HZ-005) lands a dated reader; unknown never blocks the review,
#: it only narrows which axes may start new work.
SUPPORTED_KPI_SOURCES = frozenset({
    "objectives.active",
    "erpnext.open",
    "erpnext.working",
    "erpnext.pending_review",
})

_ESCALATION_REASON_PREFIX = "owner_escalation"


def ceo_wiring_config(global_config: Mapping[str, Any]) -> dict[str, Any]:
    """CEO executor wiring from the global ``ceo`` block.

    Only the active ``ceo_reviewer`` role and the timeout travel: the
    CEO runs no reviewer pool. Empty when unconfigured — the review
    then fails its run step instead of planning blind.
    """
    block = global_config.get("ceo", {}) or {}
    if not isinstance(block, dict):
        return {"roles": {}, "timeout_minutes": 15}
    roles: dict[str, Any] = {}
    role = block.get("role")
    if isinstance(role, dict) and role.get("active", True):
        roles[CEO_EXECUTOR_ROLE] = role
    try:
        timeout = int(block.get("timeout_minutes", 15))
    except (TypeError, ValueError):
        timeout = 15
    return {"roles": roles, "timeout_minutes": max(1, timeout)}


class CEOService:
    """Production binding for the autonomous CEO loop."""

    def __init__(
        self,
        repo: Any,
        task_service: Any,
        config_loader: Any,
        settings: Any,
        company_reader: Any | None = None,
    ) -> None:
        self._repo = repo
        self._task_service = task_service
        self._config = config_loader
        self._settings = settings
        self._company_reader = company_reader

    # -- mandate ------------------------------------------------------

    def current_mandate(self) -> MandatoEmpresa | None:
        """Valid mandate, or None (fail-closed, never raises)."""
        try:
            return self._config.load_company_mandate()
        except Exception:
            logger.warning("CEO: mandate unreadable", exc_info=True)
            return None

    # -- env ----------------------------------------------------------

    def build_env(self, mandate: MandatoEmpresa | None) -> CEOEnv:
        """Bind one review to production (no I/O)."""
        wiring = ceo_wiring_config(self._config.load_project(None))
        repo = self._repo
        issuer = issuer_for_repo(repo)
        provider = (ReadonlyBadgeProvider(
            issuer, self._settings, "ceo")
            if issuer is not None else None)
        return CEOEnv(
            mandate=mandate,
            objectives=(
                MongoObjectiveStore(repo) if repo is not None
                else InMemoryObjectiveStore()
            ),
            tasks=(
                MongoObjectiveTaskLister(repo) if repo is not None
                else InMemoryTaskLister()
            ),
            open_titles=self._open_titles,
            open_escalations=self._open_escalations,
            recently_closed=self._recently_closed,
            blocked_tasks=self._blocked_tasks,
            recent_research=self._recent_research,
            business_open=self._business_open,
            read_kpi=self._read_kpi,
            known_projects=list(self._config.available_projects()),
            ops_project=str(
                getattr(self._settings, "DEFAULT_PROJECT", "")
                or "jardal-openceo"
            ),
            create_objective=self._create_objective,
            create_ticket=self._create_ticket,
            escalate=self._escalate,
            executor=ProcessExecutor(roles=wiring["roles"], pool=[],
                                   badge_provider=provider),
            repo_root=Path(__file__).resolve().parents[2],
            publish=self._publish,
            ceo_cfg={"timeout_minutes": wiring["timeout_minutes"]},
        )

    # -- readers ------------------------------------------------------

    async def _open_titles(self, limit: int) -> list[str]:
        return await self._repo.list_open_company_titles(limit)

    async def _open_escalations(self) -> list[dict[str, Any]]:
        rows = await self._repo.list_tasks_needing_human(limit=50)
        escalations = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("waiting_reason") or "")
            if not reason.startswith(_ESCALATION_REASON_PREFIX):
                continue
            params = item.get("params")
            spec = (
                params.get("escalation")
                if isinstance(params, dict) else None
            )
            if not isinstance(spec, dict):
                continue
            escalations.append({
                "category": str(spec.get("category") or ""),
                "human_action": str(spec.get("human_action") or ""),
            })
        return escalations

    async def _recently_closed(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._repo.list_recently_closed_tasks(limit=limit)
        return [
            {
                "task_id": str(item.get("task_id") or ""),
                "erp_name": self._erp_name(item),
                "title": str(item.get("title") or ""),
                "project": item.get("project"),
            }
            for item in rows or []
        ]

    async def _blocked_tasks(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._repo.list_blocked_tasks(limit=limit)
        out = []
        for item in rows or []:
            params = item.get("params")
            marker = (
                params.get("secret_block")
                if isinstance(params, dict) else None
            )
            reason = ""
            if isinstance(marker, dict):
                reason = f"missing secret: {marker.get('secret')}"
            else:
                reason = str(
                    item.get("error")
                    or item.get("waiting_reason")
                    or item.get("last_response_summary")
                    or ""
                )
            out.append({
                "task_id": str(item.get("task_id") or ""),
                "erp_name": self._erp_name(item),
                "title": str(item.get("title") or ""),
                "project": item.get("project"),
                "status": item.get("status"),
                "reason": reason,
            })
        return out

    async def _recent_research(self, limit: int) -> list[dict[str, Any]]:
        return await self._repo.list_recent_research_results(limit)

    async def _business_open(self) -> dict[str, Any]:
        reader = self._company_reader
        if reader is None:
            return {"rows": [], "unknown": True}
        try:
            rows = await reader.search_tickets(
                status=["Open", "Working", "Pending Review"],
                limit=CEO_BUSINESS_ROWS,
            )
        except Exception:
            logger.warning("CEO: ERPNext unreadable", exc_info=True)
            return {"rows": [], "unknown": True}
        return {
            "rows": [
                {
                    "erp_name": str(row.get("name") or ""),
                    "subject": str(row.get("subject") or ""),
                    "status": str(row.get("status") or ""),
                    "project": row.get("project"),
                }
                for row in (rows or [])
            ],
            "unknown": False,
        }

    async def _read_kpi(self, fuente: str) -> CEOKpiReading:
        source = str(fuente or "").strip()
        if source not in SUPPORTED_KPI_SOURCES:
            return CEOKpiReading(value=None, unknown=True)
        try:
            if source == "objectives.active":
                rows = await self._repo.list_active_objectives(limit=100)
                return CEOKpiReading(value=len(rows or []), unknown=False)
            reader = self._company_reader
            if reader is None:
                return CEOKpiReading(value=None, unknown=True)
            status = {
                "erpnext.open": ["Open"],
                "erpnext.working": ["Working"],
                "erpnext.pending_review": ["Pending Review"],
            }[source]
            rows = await reader.search_tickets(
                status=status, limit=CEO_BUSINESS_ROWS)
            return CEOKpiReading(value=len(rows or []), unknown=False)
        except Exception:
            logger.warning(
                "CEO: KPI read failed for %s", source, exc_info=True)
            return CEOKpiReading(value=None, unknown=True)

    @staticmethod
    def _erp_name(item: Mapping[str, Any]) -> str:
        params = item.get("params")
        if not isinstance(params, dict):
            return ""
        mirrors = params.get("_mirrors")
        if not isinstance(mirrors, dict):
            return ""
        mirror = mirrors.get("erpnext")
        if not isinstance(mirror, dict):
            return ""
        return str(mirror.get("name") or "")

    # -- writers ------------------------------------------------------

    async def _create_objective(self, **kwargs: Any) -> dict[str, Any]:
        return await self._task_service.create_objective(**kwargs)

    async def _create_ticket(
        self, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        return await create_internal_ticket(self._task_service, payload)

    async def _escalate(self, **kwargs: Any) -> dict[str, Any]:
        return await self._task_service.create_owner_escalation(**kwargs)

    async def _publish(self, record: dict[str, Any]) -> None:
        await self._repo.save_ceo_review(record)
