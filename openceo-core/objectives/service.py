"""Company-level objective wiring (F2 TASK-2026-00117).

:mod:`objectives.engine` is pure flow over protocols; this module binds it
to production: the ticket API (ERPNext first), the ERP group operations,
the live planner executor, the reviewer pool and the global config. Dev
flows keep their own env (see ``DevSessionManager._objective_env_for``);
every other entry — API, MCP, voice, and later the CEO (F4) and Horizon
promotion (F5) — comes through :class:`ObjectiveService`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from agents.dev.tier_s.cycle import AllowAllQuota
from agents.dev.tier_s.inbox import InMemoryInboxStore, MongoInboxStore
from agents.dev.tier_s.live import ProcessExecutor, RateMonitorQuota
from agents.dev.tier_s.review import (
    InMemoryReviewStore,
    InMemorySlotStore,
    MongoReviewStore,
    MongoSlotStore,
    parse_pool,
)
from objectives import engine as engine_mod
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    MongoObjectiveStore,
    MongoObjectiveTaskLister,
    ObjectiveEnv,
)
from state import AgentName, TaskCreateRequest, TaskSource

logger = logging.getLogger(__name__)


class ObjectiveErpAdapter:
    """Engine-facing wrapper over the ERPNext ticket adapter (group ops)."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        return await self._adapter.convert_to_group(identifier)


def erp_for_task_service(task_service: Any) -> ObjectiveErpAdapter | None:
    """ObjectiveErp view of a TaskService, or None when ERP is unavailable."""
    adapter = getattr(task_service, "_erpnext", None)
    if adapter is None or not getattr(adapter, "is_enabled", False):
        return None
    return ObjectiveErpAdapter(adapter)


async def create_internal_ticket(
    task_service: Any, payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Create engine tickets through the internal API (ERPNext first).

    Idempotent per ``source_id``. ``agent_hint``/``params`` ride through
    from the payload (planner targets; default dev); hierarchy
    (``parent_task``/``depends_on``/``is_group``) rides through to business
    registration. None when unbound or on failure: callers degrade to
    telemetry instead of forking Mongo-only tickets.
    """
    if task_service is None:
        return None
    data = dict(payload)
    try:
        source = TaskSource(str(data.pop("source", "spillover")))
    except ValueError:
        source = TaskSource.SPILLOVER
    hint = data.get("agent_hint")
    try:
        agent_hint = AgentName(str(hint)) if hint else AgentName.DEV
    except ValueError:
        # Unknown to the router enum: leave it unset so the router
        # classifies; the planner target stays in params.
        agent_hint = None
    request = TaskCreateRequest(
        source=source,
        source_id=data.get("source_id"),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        project=data.get("project"),
        priority=data.get("priority", "medium"),  # type: ignore[arg-type]
        params=data.get("params"),
        acceptance=data.get("acceptance"),
        objective_id=data.get("objective_id"),
        blocked_by=data.get("blocked_by"),
        origin=data.get("origin"),
        finding_fingerprint=data.get("finding_fingerprint"),
        coding_tool=data.get("coding_tool"),  # type: ignore[arg-type]
        autorun=bool(data.get("autorun", True)),
        agent_hint=agent_hint,
        parent_task=data.get("parent_task"),
        depends_on=data.get("depends_on"),
        is_group=bool(data.get("is_group", False)),
    )
    try:
        return await task_service.create_task(request)
    except Exception:
        logger.warning("internal ticket creation failed: %s",
                       data.get("title"), exc_info=True)
        return None


class ObjectiveService:
    """Company-level objective entries over the shared engine."""

    def __init__(
        self,
        repo: Any,
        task_service: Any,
        config_loader: Any,
        settings: Any,
        rate_monitor: Any | None = None,
    ) -> None:
        self._repo = repo
        self._task_service = task_service
        self._config = config_loader
        self._settings = settings
        self._rate_monitor = rate_monitor

    # -- env ------------------------------------------------------------

    def _quota(self) -> Any:
        return (RateMonitorQuota(self._rate_monitor)
                if self._rate_monitor is not None else AllowAllQuota())

    def env_for(self, project: str) -> ObjectiveEnv:
        """Bind the engine to production for one project (no I/O)."""
        project_config = self._config.load_project(project)
        dev_cfg = project_config.get("dev", {}) or {}
        tier_s = dev_cfg.get("tier_s", {}) or {}
        repos = project_config.get("project", {}).get("repos", []) or []
        base = str((repos[0].get("branch_base") if repos else None) or "main")
        repo_path = self._workdir(repos)
        roles = dict(tier_s.get("roles", {}) or {})
        planner_role = (project_config.get("objective_planner", {}) or {}
                        ).get("role")
        if isinstance(planner_role, dict) \
                and planner_role.get("active", True):
            roles["objective_planner"] = planner_role
        # Global block wins; dev.tier_s.objectives stays as legacy fallback.
        objectives_cfg = {**(tier_s.get("objectives", {}) or {}),
                          **(project_config.get("objectives", {}) or {})}
        objectives_cfg["child_autorun"] = bool(
            getattr(self._settings, "TIER_S_OBJECTIVE_CHILD_AUTORUN", True))
        task_service = self._task_service
        repo = self._repo

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            if repo is not None:
                await repo.patch_task(task_id, updates)

        async def create_ticket(
            payload: dict[str, Any],
        ) -> dict[str, Any] | None:
            return await create_internal_ticket(task_service, payload)

        async def notify(ref: str, text: str) -> None:
            if task_service is not None and ref:
                try:
                    await task_service.comment_on_ticket(ref, text)
                except Exception:
                    logger.warning("objective comment failed for %s",
                                   ref, exc_info=True)

        return ObjectiveEnv(
            objectives=(MongoObjectiveStore(repo) if repo is not None
                        else InMemoryObjectiveStore()),
            tasks=(MongoObjectiveTaskLister(repo) if repo is not None
                   else InMemoryTaskLister()),
            create_ticket=create_ticket,
            patch_task=patch_task,
            executor=ProcessExecutor(
                roles=roles,
                pool=tier_s.get("reviewers", []) or []),
            quota=self._quota(),
            slots=(MongoSlotStore(repo) if repo is not None
                   else InMemorySlotStore()),
            reviews=(MongoReviewStore(repo) if repo is not None
                     else InMemoryReviewStore()),
            inbox=(MongoInboxStore(repo) if repo is not None
                   else InMemoryInboxStore()),
            notify=notify,
            repo=repo_path,
            roles=roles,
            pool=list(parse_pool(tier_s.get("reviewers", []) or [])),
            review_cfg=dict(tier_s.get("review", {}) or {}),
            objectives_cfg=objectives_cfg,
            planner_cfg=dict(
                project_config.get("objective_planner", {}) or {}),
            erp=(erp_for_task_service(task_service)
                 if task_service is not None else None),
            timeouts=dict(tier_s.get("timeouts_minutes", {}) or {}),
            fix_tiers=dict(tier_s.get("fix_ticket_tiers", {}) or {}),
            inbox_rule=dict((tier_s.get("inbox", {}) or {}).get(
                "promotion", {}) or {}),
            auto_merge=bool(dev_cfg.get("auto_merge", True)),
            base_branch=base,
            vault=dict(dev_cfg.get("vault_project") or {})
            if dev_cfg.get("vault_project") else None,
        )

    @staticmethod
    def _workdir(repos: list[dict[str, Any]]) -> Path:
        """Planner workdir: the project primary repo, else the OpenCEO root."""
        if repos:
            candidate = str(repos[0].get("local_path") or "").strip()
            if candidate:
                path = Path(candidate).expanduser()
                if path.is_dir():
                    return path
        return Path(__file__).resolve().parents[2]

    # -- entries --------------------------------------------------------

    async def create_objective(
        self,
        *,
        project: str,
        goal: str,
        acceptance: list[str] | None = None,
        entry: str = "top_down",
        source: str = "api",
        source_id: str | None = None,
        requester: str | None = None,
    ) -> dict[str, Any]:
        """Top-down entry: validate the project, then run the engine."""
        name = str(project or "").strip()
        if not name or not self._config.has_project(name):
            raise engine_mod.ObjectiveRejected(
                f"unknown project {name or '<empty>'}: an objective "
                "needs exactly one known project")
        return await engine_mod.create_objective(
            self.env_for(name),
            project=name,
            goal=goal,
            acceptance=acceptance,
            entry=entry,
            source=source,
            source_id=source_id,
            requester=requester,
        )

    async def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        """Read one objective document (API/MCP status surface)."""
        if self._repo is None:
            return None
        return await self._repo.get_objective(objective_id)

    async def propagate_group_decision(
        self,
        ticket: Mapping[str, Any],
        *,
        target: str,
        business_status: str,
    ) -> dict[str, Any]:
        """Reflect a parent-ticket business decision on its objective.

        ``ticket`` is the pre-patch parent (status ``converted``). Non-parent
        tickets (children sharing the objective) are left alone: their own
        status patch is the whole inverse flow.
        """
        objective_id = str(ticket.get("objective_id") or "").strip()
        if not objective_id:
            return {"ok": False, "reason": "no-objective"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "no-objective"}
        if str(ticket.get("task_id") or "") != str(
                objective.get("conversion_source_task_id") or ""):
            return {"ok": True, "action": "not-parent"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        return await engine_mod.reflect_group_decision(
            self.env_for(project), objective_id,
            target=target, business_status=business_status)
