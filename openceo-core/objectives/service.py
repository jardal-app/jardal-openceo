"""Company-level objective wiring (F2 TASK-2026-00117).

:mod:`objectives.engine` is pure flow over protocols; this module binds it
to production: the ticket API (Jardal ERP first), the ERP group operations,
the live planner executor, the reviewer pool and the global config. Dev
flows keep their own env (see ``DevSessionManager._objective_env_for``);
every other entry — API, MCP, voice, and later the CEO (F4) and Horizon
promotion (F5) — comes through :class:`ObjectiveService`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from catalog import (
    load_all_role_skills,
    load_catalog,
    suggestable_skills,
)
from objectives import missions as missions_mod
from objectives import playbooks as playbooks_mod
from objectives import recovery_deploy as deploy_mod
from objectives import recovery_policy as recovery_mod
from objectives.delivery_verification import generic_agent_verifiers
from objectives.money_guide import project_money_routing
from objectives.missions import (
    InMemoryMissionStore,
    MongoMissionStore,
)
from orchestration.inbox import InMemoryInboxStore, MongoInboxStore
from orchestration.live import AllowAllQuota, ProcessExecutor, RateMonitorQuota
from orchestration.role_muse import ObjectiveBadgeProvider, issuer_for_repo
from orchestration.review import (
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


def _generic_verifiers_or_empty() -> dict[str, Any]:
    """Generic map, or {} when the catalog cannot load (legacy parks)."""
    try:
        return generic_agent_verifiers()
    except Exception as exc:  # noqa: BLE001 - boot gate owns fail-closed
        logger.error("generic delivery verifiers unavailable: %s", exc)
        return {}


class ObjectiveErpAdapter:
    """Engine-facing wrapper over the business work port."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        return await self._adapter.convert_to_group(identifier)

    async def set_dependencies(
        self, identifier: str, depends_on: list[str]) -> dict[str, Any]:
        return await self._adapter.set_dependencies(identifier, depends_on)


def erp_for_task_service(task_service: Any) -> ObjectiveErpAdapter | None:
    """ObjectiveErp view of a TaskService, or None when ERP is unavailable."""
    adapter = getattr(task_service, "_business", None) or getattr(task_service, "_business", None)
    if adapter is None or not getattr(adapter, "is_enabled", False):
        return None
    return ObjectiveErpAdapter(adapter)


async def create_internal_ticket(
    task_service: Any, payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Create engine tickets through the internal API (Jardal ERP first).

    Idempotent per ``source_id``. ``agent_hint``/``params`` ride through
    from the payload (planner targets; no default: undecided tickets
    are pedidos and the single planner decides them,
    TASK-2026-00204); hierarchy (``parent_task``/``depends_on``/
    ``is_group``) rides through to business registration. None when
    unbound or on failure: callers degrade to telemetry instead of
    forking Mongo-only tickets.
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
        agent_hint = AgentName(str(hint)) if hint else None
    except ValueError:
        # Unknown to the router enum: leave it unset. The graph agent gate
        # resolves the planner target from params (TASK-2026-00195): wired
        # names route by name, the rest wait with routing_missing.
        # Nothing reclassifies silently.
        agent_hint = None
    request = TaskCreateRequest(
        source=source,
        source_id=data.get("source_id"),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        project=data.get("project"),
        priority=data.get("priority", "medium"),  # type: ignore[arg-type]
        params=data.get("params"),
        tags=data.get("tags"),
        acceptance=data.get("acceptance"),
        objective_id=data.get("objective_id"),
        blocked_by=data.get("blocked_by"),
        origin=data.get("origin"),
        finding_fingerprint=data.get("finding_fingerprint"),
        coding_tool=data.get("coding_tool"),  # type: ignore[arg-type]
        review=data.get("review"),  # type: ignore[arg-type]
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


def objectives_wiring_config(
    project_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Review/executor wiring the objectives engine needs (TASK-2026-00141).

    Reads only the global ``objective_planner``, ``objectives`` and shared
    ``review`` blocks: the engine never looks at ``dev.tier_s`` (the
    ``dev.tier_s.objectives`` legacy fallback is retired). Shared by the
    company-level :class:`ObjectiveService` and the dev flows
    (``DevSessionManager._objective_env_for``), so both resolve the planner
    role and the review machinery from one place.
    """
    planner_cfg = dict(project_config.get("objective_planner", {}) or {})
    objectives_cfg = dict(project_config.get("objectives", {}) or {})
    review = project_config.get("review", {}) or {}
    review_roles = review.get("roles", {}) or {}
    roles: dict[str, Any] = {}
    planner_role = planner_cfg.get("role")
    if isinstance(planner_role, dict) \
            and planner_role.get("active", True):
        roles["objective_planner"] = planner_role
    for key in ("closer", "arbiter"):
        role = review_roles.get(key)
        if isinstance(role, dict):
            roles[key] = role
    return {
        "roles": roles,
        "pool": list(review.get("reviewers", []) or []),
        "review_cfg": dict(review.get("review", {}) or {}),
        "objectives_cfg": objectives_cfg,
        "planner_cfg": planner_cfg,
        "timeouts": dict(review.get("timeouts_minutes", {}) or {}),
        "fix_tiers": dict(review.get("fix_ticket_tiers", {}) or {}),
        "inbox_rule": dict((review.get("inbox", {}) or {}).get(
            "promotion", {}) or {}),
    }


def objectives_process_executor(
    project_config: Mapping[str, Any],
    badge_provider: Any | None = None,
) -> ProcessExecutor:
    """One-shot executor for objective flows over the global wiring."""
    wiring = objectives_wiring_config(project_config)
    return ProcessExecutor(roles=wiring["roles"], pool=wiring["pool"],
                           badge_provider=badge_provider)


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
        self._ceo_wake: Any | None = None
        self._recovery_wake: Any | None = None
        # This process's code is what a deploy park waits for: a park
        # requested before this instant is live now (TASK-2026-00483).
        self._booted_at = datetime.now(timezone.utc)
        # One deploy restart per process life (ticket #570): a spawned
        # restart kills this process in seconds, so the flag dies with it.
        self._deploy_spawned = False

    def set_recovery_wake(self, wake: Any | None) -> None:
        """Inject the recovery scheduler wake (TASK-2026-00481)."""
        self._recovery_wake = wake
        # Dev-flow envs are built outside this service; the single timer
        # registers process-wide so their parks also wake it.
        engine_mod.RECOVERY_WAKE = wake

    def wake_recovery(self) -> None:
        """Nudge the recovery timer (a recovery ticket ended)."""
        if self._recovery_wake is not None:
            self._recovery_wake()

    def set_ceo_wake(self, wake: Any | None) -> None:
        """Inject the CEO loop wake (objective parked/closed events).

        The engine calls it with a reason; without it the company
        events simply wake nobody. Mirrors ``set_dispatcher_wake``.
        """
        self._ceo_wake = wake

    # -- env ------------------------------------------------------------

    def _quota(self) -> Any:
        return (RateMonitorQuota(self._rate_monitor)
                if self._rate_monitor is not None else AllowAllQuota())

    def env_for(self, project: str) -> ObjectiveEnv:
        """Bind the engine to production for one project (no I/O)."""
        project_config = self._config.load_project(project)
        dev_cfg = project_config.get("dev", {}) or {}
        repos = project_config.get("project", {}).get("repos", []) or []
        base = str((repos[0].get("branch_base") if repos else None) or "main")
        repo_path = self._workdir(repos)
        wiring = objectives_wiring_config(project_config)
        objectives_cfg = dict(wiring["objectives_cfg"])
        objectives_cfg["child_autorun"] = bool(
            getattr(self._settings, "TIER_S_OBJECTIVE_CHILD_AUTORUN", True))
        task_service = self._task_service
        repo = self._repo

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            if repo is not None:
                await repo.patch_task(task_id, updates)
            revoke = getattr(task_service, "revoke_badges_for_patch", None)
            if revoke is not None:
                await revoke(task_id, str((updates or {}).get("status") or "") or None)

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

        try:
            mandate = self._config.load_company_mandate()
        except Exception:  # noqa: BLE001
            mandate = None

        issuer = issuer_for_repo(repo)
        provider = (ObjectiveBadgeProvider(
            issuer, self._settings, project)
            if issuer is not None else None)
        return ObjectiveEnv(
            objectives=(MongoObjectiveStore(repo) if repo is not None
                        else InMemoryObjectiveStore()),
            tasks=(MongoObjectiveTaskLister(repo) if repo is not None
                   else InMemoryTaskLister()),
            create_ticket=create_ticket,
            patch_task=patch_task,
            executor=objectives_process_executor(
                project_config, badge_provider=provider),
            quota=self._quota(),
            slots=(MongoSlotStore(repo) if repo is not None
                   else InMemorySlotStore()),
            reviews=(MongoReviewStore(repo) if repo is not None
                     else InMemoryReviewStore()),
            inbox=(MongoInboxStore(repo) if repo is not None
                   else InMemoryInboxStore()),
            notify=notify,
            repo=repo_path,
            roles=wiring["roles"],
            pool=list(parse_pool(wiring["pool"])),
            pool_raw=list(wiring["pool"]),
            review_cfg=wiring["review_cfg"],
            objectives_cfg=objectives_cfg,
            planner_cfg=wiring["planner_cfg"],
            erp=(erp_for_task_service(task_service)
                 if task_service is not None else None),
            timeouts=wiring["timeouts"],
            fix_tiers=wiring["fix_tiers"],
            inbox_rule=wiring["inbox_rule"],
            auto_merge=bool(dev_cfg.get("auto_merge", True)),
            base_branch=base,
            vault=dict(dev_cfg.get("vault_project") or {})
            if dev_cfg.get("vault_project") else None,
            ceo_wake=self._ceo_wake,
            recovery_wake=self._recovery_wake,
            project_routing=project_money_routing(project_config),
            # Generic delivery check per enabled AI agent
            # (TASK-2026-00216): declared skill outputs plus required
            # evidence, with optional stricter per-skill verify.py.
            # Domains outside the catalog still park as
            # capability_gap (TASK-2026-00175).
            delivery_verifiers=_generic_verifiers_or_empty(),
            playbook_proposals=True,
            dispatcher_wake=(getattr(task_service, "wake_dispatcher", None)
                             if task_service is not None else None),
            mandate=mandate,
            missions=(MongoMissionStore(repo) if repo is not None
                      else InMemoryMissionStore()),
        )

    def mission_env_for(self, project: str) -> missions_mod.MissionEnv:
        """Bind the mission flows to production for one project."""
        base = self.env_for(project)
        return missions_mod.MissionEnv(
            missions=base.missions,
            objectives=base.objectives,
            tasks=base.tasks,
            create_ticket=base.create_ticket,
            patch_task=base.patch_task,
            notify=base.notify,
            ceo_wake=self._ceo_wake,
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

    async def create_mission(
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
        """Top-down entry: validate the project, then run the flow."""
        name = str(project or "").strip()
        if not name or not self._config.has_project(name):
            raise missions_mod.MissionRejected(
                f"unknown project {name or '<empty>'}: a mission "
                "needs exactly one known project")
        return await missions_mod.create_mission(
            self.mission_env_for(name),
            project=name,
            goal=goal,
            acceptance=acceptance,
            entry=entry,
            source=source,
            source_id=source_id,
            requester=requester,
        )

    async def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        """Read one mission document (API/MCP status surface)."""
        if self._repo is None:
            return None
        return await self._repo.get_mission(mission_id)

    async def close_mission(
        self, mission_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Close a mission the CEO declared complete (00218)."""
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        mission = await self._repo.get_mission(mission_id)
        if mission is None:
            return {"ok": False, "reason": "no-mission"}
        project = str(mission.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        try:
            return await missions_mod.close_mission(
                self.mission_env_for(project), mission_id, reason=reason)
        except missions_mod.MissionRejected as exc:
            return {"ok": False, "reason": str(exc)[:300]}

    async def request_playbook(self, objective_id: str) -> dict[str, Any]:
        """Owner-asked playbook proposal for a closed objective (00217)."""
        if self._repo is None:
            return {"ok": False, "action": "no-objective"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "action": "no-objective"}
        env = self.env_for(str(objective.get("project") or ""))
        try:
            catalog = load_catalog()
            skills = suggestable_skills(
                catalog, load_all_role_skills(catalog))
        except Exception:  # noqa: BLE001
            skills = {}
        return await playbooks_mod.propose_playbook(
            env, objective_id, "owner_request",
            agents=env.planner_cfg.get("agents", {}) or {},
            skills=skills)

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
        result = await engine_mod.reflect_group_decision(
            self.env_for(project), objective_id,
            target=target, business_status=business_status)
        if (result.get("action") == "cancelled"
                and result.get("cancelled_children")):
            # Dependency wakeup (§9.5, TASK-2026-00180): dependents of
            # cascade-cancelled children requeue like any other cancel
            # landing. Children of the dead objective are already
            # cancelled, so the waiting/queued filter (plus the 00177
            # atomic net) excludes them: no resurrection. Optional
            # wiring, like dispatcher_wake above.
            requeue = getattr(
                self._task_service, "_requeue_dependents", None)
            if callable(requeue):
                try:
                    for child_id in result["cancelled_children"]:
                        await requeue(str(child_id))
                except Exception:
                    logger.warning(
                        "Dependent requeue error for objective %s",
                        objective_id, exc_info=True,
                    )
        return result

    async def plan_objective(self, objective_id: str) -> dict[str, Any]:
        """Run one planning cycle now (external triggers, TASK-2026-00174).

        Used when promotion (or any external producer) leaves work the
        planner must consider — e.g. a tierless linked child awaiting
        its contract. Returns the engine result, or ok False when the
        objective cannot be planned.
        """
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "no-objective"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        return await engine_mod.plan_objective(
            self.env_for(project), objective_id)

    async def boot_resume_objectives(
        self, limit: int = 100,
    ) -> dict[str, Any]:
        """Run one bounded boot-resume pass (TASK-2026-00396).

        The lifespan schedules this once per boot, in background: each
        orphaned or parked objective is reconsidered through
        :func:`engine.boot_resume_one` and resumes only when the
        deterministic check proves its wedge cleared. One objective
        never breaks the pass for the rest.
        """
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        summary: dict[str, Any] = {
            "resumed": [], "replanned": [],
            "left": 0, "skipped": [], "errors": [],
        }
        rows = await self._repo.list_boot_resumable_objectives(limit)
        for row in rows:
            objective_id = str((row or {}).get("objective_id") or "")
            if not objective_id:
                continue
            try:
                project = str((row or {}).get("project") or "")
                if not project or not self._config.has_project(project):
                    summary["skipped"].append(objective_id)
                    continue
                result = await engine_mod.boot_resume_one(
                    self.env_for(project), objective_id)
            except Exception:  # noqa: BLE001 - isolated per objective
                logger.warning("Boot resume failed for %s",
                               objective_id, exc_info=True)
                summary["errors"].append(objective_id)
                continue
            action = str(result.get("action") or "leave")
            if action == "resume_fresh":
                summary["resumed"].append(objective_id)
            elif action == "replan":
                summary["replanned"].append(objective_id)
            elif result.get("ok"):
                summary["left"] += 1
            else:
                summary["errors"].append(objective_id)
        summary["ok"] = True
        return summary

    # -- automatic recovery (TASK-2026-00481) ---------------------------

    def _objectives_cfg(self, project: str) -> dict[str, Any]:
        try:
            return dict(self._config.load_project(project).get(
                "objectives") or {})
        except Exception:  # noqa: BLE001 - missing config keeps defaults
            return {}

    async def seed_recovery_parks(self, limit: int = 100) -> dict[str, Any]:
        """One bounded boot pass: give legacy parks their first due time."""
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        seeded = 0
        rows = await self._repo.list_boot_resumable_objectives(limit)
        for row in rows or []:
            if str((row or {}).get("status") or "") != "waiting":
                continue
            objective_id = str((row or {}).get("objective_id") or "")
            if not objective_id:
                continue
            project = str((row or {}).get("project") or "")
            if not project or not self._config.has_project(project):
                continue
            patch = recovery_mod.recovery_initial_patch(
                row, self._objectives_cfg(project))
            if patch is None:
                continue
            try:
                await self._repo.patch_objective(objective_id, patch)
                seeded += 1
            except Exception:  # noqa: BLE001 - one row never breaks the pass
                logger.warning("recovery seed failed for %s",
                               objective_id, exc_info=True)
        if seeded and self._recovery_wake is not None:
            self._recovery_wake()
        return {"ok": True, "seeded": seeded}

    async def recovery_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Waiting objectives with their live recovery decision inputs."""
        if self._repo is None:
            return []
        out: list[dict[str, Any]] = []
        rows = await self._repo.list_boot_resumable_objectives(limit)
        for row in rows or []:
            if str((row or {}).get("status") or "") != "waiting":
                continue
            objective_id = str((row or {}).get("objective_id") or "")
            project = str((row or {}).get("project") or "")
            if not objective_id or not project \
                    or not self._config.has_project(project):
                continue
            try:
                children = await self._repo.list_tasks_by_objective(
                    objective_id)
            except Exception:  # noqa: BLE001
                children = []
            active = engine_mod._brake_active_children(children)
            out.append({
                "objective": row,
                "objective_id": objective_id,
                "project": project,
                "objectives_cfg": self._objectives_cfg(project),
                "active_children": len(active),
                "children": children,
                "ticket_open": await self._recovery_ticket_open(objective_id),
            })
        return out

    async def recovery_next_delay(self) -> int:
        if self._repo is None:
            return recovery_mod.MAX_SLEEP_SECONDS
        rows = await self._repo.list_boot_resumable_objectives(100)
        return recovery_mod.recovery_seconds_to_next(rows or [])

    async def _pace_unactionable_recovery(
        self, objective_id: str, objective: Mapping[str, Any],
        cfg: Mapping[str, Any], reason: str,
        *, now: datetime | None = None,
    ) -> None:
        """Push a stale due forward after a none-decision (TASK-2026-00534).

        The kernel re-drives past-dues right after every lane end, so a
        park that keeps deciding none (children active, ticket open, ...)
        seals, decides and releases in a tight loop (live: a core at 86%).
        Rewriting a missing-or-past due to a bounded recheck delay makes
        the lane sleep; a future due is left alone, and a park that lands
        meanwhile still wakes the lane sooner. Never raises.
        """
        if self._repo is None or reason == "not due":
            return
        if str(objective.get("status") or "") != "waiting":
            return
        moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        recovery = objective.get("recovery")
        recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
        due = recovery_mod.parse_due(recovery.get("due_at"))
        if due is not None and due > moment:
            return
        try:
            fresh = await self._repo.get_objective(objective_id)
        except Exception:  # noqa: BLE001 - pacing is best-effort
            return
        if fresh is None or str(fresh.get("status") or "") != "waiting":
            return
        fresh_recovery = fresh.get("recovery")
        fresh_recovery = dict(fresh_recovery) \
            if isinstance(fresh_recovery, Mapping) else {}
        if fresh_recovery.get("due_at") != recovery.get("due_at"):
            return  # A park moved the due under us; leave its schedule.
        try:
            minutes = int(cfg.get("none_recheck_minutes", 10))
        except (TypeError, ValueError):
            minutes = 10
        try:
            await self._repo.patch_objective(objective_id, {"recovery": {
                **fresh_recovery,
                "due_at": (moment + timedelta(minutes=minutes)).isoformat(),
            }})
        except Exception:  # noqa: BLE001 - pacing is best-effort
            logger.warning("recovery pacing failed for %s", objective_id,
                           exc_info=True)

    async def recover_waiting_objective(
        self, objective_id: str, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Decide and act for one waiting objective (ticket #570).

        One body shared by the legacy ``recovery_tick`` and the kernel
        agenda ``run``: fresh decision inputs, the policy decision, then
        the branch the tick always ran (replan a retry/resume, open the
        ticket when the budget is spent, restart-or-defer a deploy park).
        ``due_after`` feeds the tick's sleep; the agenda ignores it and
        re-reads its index instead.
        """
        outcome: dict[str, Any] = {"ok": True, "action": "none",
                                   "reason": "", "result": {},
                                   "due_after": None, "deployed": False}
        if self._repo is None:
            return {**outcome, "ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {**outcome, "ok": False, "reason": "not-found"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            # #586: an unpaced no-due spent park with an unknown project
            # would re-drive every lane end on the now-wait.
            await self._pace_unactionable_recovery(
                objective_id, objective, {}, "unknown-project", now=now)
            return {**outcome, "ok": False, "reason": "unknown-project"}
        cfg = self._objectives_cfg(project)
        try:
            children = await self._repo.list_tasks_by_objective(objective_id)
        except Exception:  # noqa: BLE001
            children = []
        idle = False
        if recovery_mod.park_reason(objective) \
                == recovery_mod.DEPLOY_REASON:
            idle = await self.deploy_idle()
        decision = recovery_mod.recovery_decision(
            objective,
            len(engine_mod._brake_active_children(children)),
            cfg,
            ticket_open=await self._recovery_ticket_open(objective_id),
            now=now,
            booted_at=self._booted_at,
            idle=idle,
        )
        action = str(decision.get("action") or "none")
        reason = str(decision.get("reason") or "")
        if action == "none":
            await self._pace_unactionable_recovery(
                objective_id, objective, cfg, reason, now=now)
        if action == "deploy" and self._deploy_spawned:
            # One restart per process life: a spawned restart kills this
            # process in seconds, so a second deploy would only race it;
            # the park keeps checking (and resumes on the running code past
            # its deadline).
            action = "deploy-deferred"
        if action in ("retry", "resume"):
            result = await self.recover_objective(objective_id)
            return {**outcome, "ok": bool(result.get("ok")),
                    "action": action, "reason": reason, "result": result}
        if action == "ticket":
            result = await self.open_recovery_ticket(objective_id)
            if not result.get("ok"):
                # #586: a failed ticket round leaves the park no-due
                # exhausted; without pacing the now-wait re-drives it
                # every lane end (same shape as TASK-2026-00534).
                await self._pace_unactionable_recovery(
                    objective_id, objective, cfg,
                    str(result.get("reason") or "ticket-failed"), now=now)
            return {**outcome, "ok": bool(result.get("ok")),
                    "action": action, "reason": reason, "result": result}
        if action in ("deploy", "deploy-deferred", "defer"):
            spawned = action == "deploy" and await asyncio.to_thread(
                deploy_mod.spawn_service_restart)
            if spawned:
                self._deploy_spawned = True
            # A restart that never came, or a busy service, checks
            # again later; the boot after a real restart resumes it.
            due_after = recovery_mod.deploy_retry_due(cfg)
            await self._repo.patch_objective(objective_id, {"recovery": {
                **dict(objective.get("recovery") or {}),
                "due_at": due_after}})
            if spawned:
                logger.warning(
                    "Recovery deploy: restarting the idle service so "
                    "objective %s resumes on its merged fix",
                    objective_id)
            return {**outcome, "action": "deploy" if spawned else "defer",
                    "reason": reason, "result": {"spawned": bool(spawned)},
                    "due_after": due_after, "deployed": bool(spawned)}
        due_after = None
        if reason == "not due":
            due_after = (objective.get("recovery") or {}).get("due_at") \
                if isinstance(objective.get("recovery"), Mapping) else None
        return {**outcome, "action": action, "reason": reason,
                "due_after": due_after}

    async def recovery_tick(self) -> dict[str, Any]:
        """One scheduler pass: retry due parks, open tickets when spent.

        Returns the sleep until the nearest still-pending due time, so the
        timer never re-reads every waiting objective just to find it.
        """
        retried: list[str] = []
        ticketed: list[str] = []
        deployed: list[str] = []
        next_due: Any = None
        candidates = await self.recovery_candidates()
        for candidate in candidates:
            objective_id = candidate["objective_id"]
            acted = await self.recover_waiting_objective(objective_id)
            action = str(acted.get("action") or "none")
            if action in ("retry", "resume") and acted.get("ok"):
                retried.append(objective_id)
            elif action == "ticket" and acted.get("ok"):
                ticketed.append(objective_id)
            if acted.get("deployed"):
                deployed.append(objective_id)
            parsed = recovery_mod.parse_due(acted.get("due_after"))
            if parsed is not None and (next_due is None or parsed < next_due):
                next_due = parsed
            if deployed:
                # This process ends in seconds: starting a planner or a
                # second restart now would only be killed mid-flight.
                break
        now = datetime.now(timezone.utc)
        if next_due is None:
            delay = recovery_mod.MAX_SLEEP_SECONDS
        else:
            delay = int((next_due - now).total_seconds())
            delay = max(recovery_mod.MIN_SLEEP_SECONDS,
                        min(recovery_mod.MAX_SLEEP_SECONDS, delay))
        return {"ok": True, "retried": retried, "ticketed": ticketed,
                "deployed": deployed, "next_seconds": delay}

    async def deploy_idle(self) -> bool:
        """True when a restart kills no run and deploys a clean ``main``."""
        if self._repo is None or engine_mod.planning_in_flight():
            return False
        try:
            busy = await self._repo.tasks.count_documents({
                "status": "in_progress", "autorun": {"$ne": False}})
        except Exception:  # noqa: BLE001 - an unreadable count never restarts
            logger.warning("recovery deploy idle check failed", exc_info=True)
            return False
        if busy:
            return False
        return await asyncio.to_thread(deploy_mod.live_checkout_ready)

    async def recover_objective(self, objective_id: str) -> dict[str, Any]:
        """Claim one due park and replan it (single winner, never twice).

        A retry spends an attempt; a deploy park resuming on its live fix
        (or on the running code after the wait) does not.
        """
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "not-found"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        cfg = self._objectives_cfg(project)
        try:
            children = await self._repo.list_tasks_by_objective(objective_id)
        except Exception:  # noqa: BLE001
            children = []
        decision = recovery_mod.recovery_decision(
            objective, len(engine_mod._brake_active_children(children)), cfg,
            ticket_open=await self._recovery_ticket_open(objective_id),
            booted_at=self._booted_at)
        action = decision.get("action")
        if action not in ("retry", "resume"):
            return {"ok": False, "reason": str(decision.get("reason") or "")}
        recovery = objective.get("recovery")
        due_at = (recovery or {}).get("due_at") \
            if isinstance(recovery, Mapping) else None
        store = (MongoObjectiveStore(self._repo) if self._repo is not None
                 else InMemoryObjectiveStore())
        claimed = await store.claim_recovery(
            objective_id, due_at, count_attempt=action == "retry")
        if claimed is None:
            return {"ok": False, "reason": "claim-lost"}
        env = self.env_for(project)
        try:
            plan = await engine_mod.plan_objective(env, objective_id)
        except Exception as exc:  # noqa: BLE001 - park it again, bounded
            logger.exception("automatic recovery planning failed")
            await engine_mod._objective_park(
                env, claimed, "planner_error",
                f"automatic recovery planning failed: {exc}")
            return {"ok": False, "reason": "plan-error",
                    "detail": str(exc)[:300]}
        logger.info("Objective %s recovered automatically (%s: %s)",
                    objective_id, action, decision.get("reason"))
        return {"ok": True, "action": "retried" if action == "retry"
                else "resumed", "plan": plan}

    async def open_recovery_ticket(self, objective_id: str) -> dict[str, Any]:
        """Create the musej recovery ticket once the retry budget is spent."""
        if self._repo is None or self._task_service is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "not-found"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        cfg = self._objectives_cfg(project)
        decision = recovery_mod.recovery_decision(
            objective, 0, cfg, ticket_open=False)
        if decision.get("action") != "ticket":
            return {"ok": False, "reason": str(decision.get("reason") or "")}
        if await self._recovery_ticket_open(objective_id):
            return {"ok": False, "reason": "ticket-open"}
        recovery = objective.get("recovery")
        recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
        rec_cfg = recovery_mod.recovery_config(cfg)
        target = str(rec_cfg.get("ticket_project") or "jardal-openceo")
        if not self._config.has_project(target):
            logger.warning("recovery ticket project %s missing; using %s",
                           target, project)
            target = project
        try:
            children = await self._repo.list_tasks_by_objective(objective_id)
        except Exception:  # noqa: BLE001
            children = []
        round_no = int(recovery.get("ticket_round") or 0) + 1
        if round_no > int(rec_cfg.get("max_tickets") or 2):
            logger.warning("Recovery ticket budget spent for objective %s",
                           objective_id)
            return {"ok": False, "reason": "ticket-budget"}
        previous: Mapping[str, Any] | None = None
        previous_id = str(recovery.get("ticket_id") or "")
        if previous_id and round_no > 1:
            try:
                previous = await self._repo.get_task(previous_id)
            except Exception:  # noqa: BLE001 - the evidence is optional
                previous = None
        description = self._recovery_ticket_description(
            objective, project, children, recovery, previous)
        payload = {
            "source": "spillover",
            "source_id": f"objective-recovery:{objective_id}:{round_no}",
            "title": f"[recovery] Objetivo {objective_id}: causa raíz del park",
            "description": description,
            "project": target,
            "priority": "high",
            "tags": ["recovery", "objective-recovery", project],
            "coding_tool": str(rec_cfg.get("ticket_tier") or "tier_c"),
            "agent_hint": "dev",
            "autorun": True,
            # Not an objective child on purpose: a failed recovery must not
            # replan the mission from the wrong project. The close hook
            # resumes the objective through params.recovery_for.
            "params": {
                "recovery_for": objective_id,
                "recovery_project": project,
                "recovery_reason": str(
                    objective.get("waiting_reason") or "")[:500],
            },
            "acceptance": [
                "Diagnóstico de la causa raíz del park en el ticket",
                "Corrección verificada con tests dirigidos",
                "HANDOFF con evidencia y siguiente acción",
            ],
        }
        created = await create_internal_ticket(self._task_service, payload)
        if created is None:
            return {"ok": False, "reason": "create-failed"}
        recovery.update({
            "ticket_id": str(created.get("task_id") or ""),
            "ticket_round": round_no,
            "ticket_at": engine_mod._utcnow_iso(),
            "exhausted": True,
            "due_at": None,
        })
        await self._repo.patch_objective(objective_id, {"recovery": recovery})
        logger.warning("Recovery ticket opened for objective %s: %s",
                       objective_id, created.get("task_id"))
        return {"ok": True, "ticket_id": created.get("task_id"),
                "project": target}

    async def _recovery_ticket_open(self, objective_id: str) -> bool:
        if self._repo is None:
            return False
        terminal = list(engine_mod.TERMINAL_TICKET_STATUS
                        | {engine_mod.FAILED_HISTORY_STATUS})
        try:
            row = await self._repo.tasks.find_one({
                "params.recovery_for": objective_id,
                "status": {"$nin": terminal},
            })
            return row is not None
        except Exception:  # noqa: BLE001 - a read failure never blocks recovery
            return False

    def _recovery_ticket_description(
        self, objective: Mapping[str, Any], project: str,
        children: list[Mapping[str, Any]], recovery: Mapping[str, Any],
        previous: Mapping[str, Any] | None = None,
    ) -> str:
        gaps = objective.get("capability_gap")
        gap_text = (str(gaps.get("summary") or "")[:900]
                    if isinstance(gaps, Mapping) else "")
        previous_text = ""
        if isinstance(previous, Mapping) and previous:
            outcome = (previous.get("error") or previous.get("waiting_reason")
                       or previous.get("last_response_summary") or "")
            previous_text = (
                f"RONDA DE RECUPERACIÓN ANTERIOR: {previous.get('task_id')} "
                f"[{previous.get('status')}] {str(outcome)[:400]}\n"
                "No repitas ese enfoque: parte de su HANDOFF y de su fallo.")
        failed = [item for item in children
                  if str(item.get("status")) in ("failed", "waiting")]
        evidence: list[str] = []
        for item in failed[-6:]:
            kept = item.get("params") if isinstance(
                item.get("params"), Mapping) else {}
            kept = kept.get("kept_work") if isinstance(
                kept.get("kept_work"), Mapping) else {}
            line = (f"- {item.get('task_id')} [{item.get('status')}] "
                    f"{(str(item.get('title') or ''))[:120]}")
            if kept.get("branch"):
                line += f" | work kept on {kept['branch']}"
            evidence.append(line)
        returns = objective.get("planner_returns") or []
        last_return = returns[-1] if returns else {}
        return_text = ""
        if isinstance(last_return, Mapping) and last_return:
            return_text = (f"Último retorno: {last_return.get('title')} "
                           f"[{last_return.get('agent')}/"
                           f"{last_return.get('resultado')}] "
                           f"{str(last_return.get('necesidad') or '')[:300]}")
        return f"""MANDATO DE RECUPERACIÓN (TASK-2026-00481, automático)

OpenCEO detectó un objetivo parado tras agotar sus reintentos. Este ticket
existe para encontrar la causa raíz y corregirla, no para repetir el trabajo
de la misión.

OBJETIVO: {objective.get('objective_id')} (proyecto {project})
ESTADO: {objective.get('status')} — {str(objective.get('waiting_reason') or '')[:400]}
INTENTOS AUTOMÁTICOS: {recovery.get('attempts')} (agotados)
{('GAP DECLARADO: ' + gap_text) if gap_text else ''}
{return_text}
{previous_text}

TICKETS FALLIDOS O EN ESPERA:
{chr(10).join(evidence) if evidence else '- (sin hijos fallidos)'}

QUÉ HACER
1. Diagnostica la causa raíz del park (motor, configuración, credencial,
   presupuesto, prompt o contrato), leyendo el repo de OpenCEO y los
   artefactos de la misión. No asumas que el plan anterior era correcto.
2. Si la causa es código/configuración de OpenCEO, corrígela en este repo
   con el cambio mínimo y tests dirigidos. Si la causa está en el repo de
   la misión ({project}), no lo edites desde aquí: deja la corrección
   propuesta y el punto exacto en el HANDOFF.
3. Verifica con tests dirigidos, sin modelos en vivo. Los gates del
   orquestador se ejecutan después; no ejecutes la suite global si pide
   contenedores.
4. NO reinicies el servicio, NO hagas commit ni merge: el orquestador
   integra este ticket con sus gates y merge normales.
5. Deja .openceo/HANDOFF.md con: causa raíz, cambio, evidencia, cómo
   verificar y qué debe hacer el siguiente ciclo de la misión.

ACEPTACIÓN
- Diagnóstico explícito y corrección (o propuesta exacta si el fix vive en
  el repo de la misión).
- Tests dirigidos en verde y HANDOFF completo."""

    async def plan_return(
        self,
        objective_id: str,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Replan after an agent verdict returned once (TASK-2026-00202).

        Records the return in the ledger, then plans with ``return_from``
        so created tickets carry the tag. A non-active objective keeps
        the ledger entry but is not replanned (first park wins).
        """
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "no-objective"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        env = self.env_for(project)
        await engine_mod.record_planner_return(
            env.objectives, objective_id,
            task_id=str(entry.get("task_id") or ""),
            title=str(entry.get("title") or ""),
            agent=str(entry.get("agent") or ""),
            resultado=str(entry.get("resultado") or ""),
            motivo=str(entry.get("motivo") or ""),
            necesidad=str(entry.get("necesidad") or ""))
        if str(objective.get("status") or "") != "active":
            return {"ok": False, "reason": "objective-not-active"}
        return await engine_mod.plan_objective(
            env, objective_id,
            return_from=str(entry.get("task_id") or "") or None)

    async def apply_spend_approval(
        self,
        objective_id: str,
        question_task_id: str,
    ) -> dict[str, Any]:
        """Launch a spend-held plan after the Owner approved it.

        Fail-closed: without the store, the objective, its project or
        an approved question, nothing launches. The engine re-validates
        the stored plan before applying it.
        """
        from owner_channel import owner_question_block, question_answered

        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "no-objective"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        question = await self._repo.get_task(question_task_id)
        if question is None or not question_answered(question):
            return {"ok": False, "reason": "question-not-answered"}
        decision = (owner_question_block(question).get("decision") or {}
                    ).get("decision")
        if decision != "approved":
            return {"ok": False, "reason": "question-not-approved"}
        pending = objective.get("pending_plan")
        held_for = str((pending or {}).get("question_task_id") or "")
        if held_for != str(question_task_id or ""):
            # The approval belongs to another plan (an orphaned older
            # question): never launch what the Owner did not see.
            return {"ok": False, "reason": "approval-plan-mismatch"}
        return await engine_mod.apply_approved_plan(
            self.env_for(project), objective_id)

    async def park_return_gap(
        self,
        objective_id: str,
        entry: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Park capability_gap: the return replan stayed unresolved."""
        if self._repo is None:
            return {"ok": False, "reason": "no-store"}
        objective = await self._repo.get_objective(objective_id)
        if objective is None:
            return {"ok": False, "reason": "no-objective"}
        project = str(objective.get("project") or "")
        if not project or not self._config.has_project(project):
            return {"ok": False, "reason": "unknown-project"}
        env = self.env_for(project)
        await engine_mod.record_planner_return(
            env.objectives, objective_id,
            task_id=str(entry.get("task_id") or ""),
            title=str(entry.get("title") or ""),
            agent=str(entry.get("agent") or ""),
            resultado=str(entry.get("resultado") or ""),
            motivo=str(entry.get("motivo") or ""),
            necesidad=str(entry.get("necesidad") or ""))
        return await engine_mod.park_return_gap(env, objective_id, entry)
