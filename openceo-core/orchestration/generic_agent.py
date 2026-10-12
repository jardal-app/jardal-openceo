"""Generic CLI agent runner: any catalog agent, one headless run (TASK-2026-00205).

Two paths, no silent reclassification:

* exact skill: the suggested skill carries ``workflow.yaml`` (its exact
  part) and the ticket targets it via ``params.skill`` — the YAML engine
  runs it, the LLM is never invoked;
* LLM: one fenced Muse run with the role profile (card, skills, tool
  box badge), the ticket folder and the structured verdict schema.

Verdict semantics belong to TASK-2026-00202: hecho completes, bloqueado
and no_aplica fail with the verdict stored (objective children return
once to the planner through the existing funnel), and a missing verdict
is legacy behavior, never a failure. The orchestrator owns the outcome:
this runner returns ``result_comment`` and ``run_task`` posts it to
ERPNext; the agent never changes ticket states.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from catalog import (
    AgentType,
    Catalog,
    CatalogError,
    RoleProfile,
    catalog_root,
    load_catalog,
    load_role_profile,
    load_role_skills,
)
from objectives.agent_result import (
    AGENT_RESULT_PROMPT,
    AgentOutcome,
    AgentResult,
    parse_agent_result,
)
from orchestration.agent_sandbox import ensure_ticket_dir
from orchestration.live import ProcessExecutor
from orchestration.role_muse import TicketBadgeProvider, issuer_for_repo
from state import TaskStatus

logger = logging.getLogger(__name__)

__all__ = [
    "EXACT_SKILL_WORKFLOW_FILENAME",
    "GENERIC_AGENT_TIMEOUT_S",
    "GENERIC_MUSE_MODEL",
    "GenericAgentWiring",
    "build_agent_prompt",
    "default_executor_factory",
    "muse_role_spec",
    "resolve_exact_skill_path",
    "run_generic_agent",
]

#: The exact part of a skill: when present, the skill runs deterministically.
EXACT_SKILL_WORKFLOW_FILENAME = "workflow.yaml"

#: One headless run budget (same as the simple-tier one-shot runs).
GENERIC_AGENT_TIMEOUT_S = 1800

#: House Muse model until cards pin their own.
GENERIC_MUSE_MODEL = "muse-spark-1.3-contributor"

#: Engine with a role translator today (OpenCode/Claude land in 00200).
_MUSE_ENGINE = "muse-code"


def default_executor_factory(
    role: str, spec: Mapping[str, Any], badge_provider: Any,
) -> ProcessExecutor:
    """Production executor: one-shot fenced process over the role spec."""
    return ProcessExecutor(roles={role: dict(spec)},
                           badge_provider=badge_provider)


@dataclass(frozen=True)
class GenericAgentWiring:
    """What a generic run needs. Factories default to the real builders."""

    settings: Any = None
    repo: Any = None
    workflow_service: Any = None
    executor_factory: Callable[
        [str, Mapping[str, Any], Any], Any] | None = None
    badge_provider_factory: Callable[
        [str, str, str], Any] | None = None
    repo_root: str | Path | None = None
    work_base_dir: str | Path | None = None
    catalog: Catalog | None = None
    timeout_s: int = GENERIC_AGENT_TIMEOUT_S


def resolve_exact_skill_path(
    source: str, *, repo_root: str | Path | None = None,
) -> Path | None:
    """The skill's exact workflow, when the skill dir carries one."""
    root = Path(repo_root) if repo_root is not None \
        else catalog_root().parent
    base = Path(source)
    skill_dir = base if base.is_absolute() else root / base
    candidate = skill_dir / EXACT_SKILL_WORKFLOW_FILENAME
    return candidate if candidate.is_file() else None


def muse_role_spec(profile: RoleProfile, label: str) -> dict[str, Any]:
    """Muse one-shot spec, mirroring the planner/ceo role entries.

    The verdict schema is a shipped product path, resolved against the
    real repo: a missing schema refuses the spec, never the run silently.
    """
    schema_rel = (profile.output_schema or "").strip()
    schema = catalog_root().parent / schema_rel if schema_rel else None
    if schema is None or not schema.is_file():
        raise CatalogError(
            f"role profile {profile.name!r}: output_schema "
            f"{schema_rel!r} is missing, refusing the run")
    return {
        "provider": "muse",
        "account": "muse",
        "label": label,
        "model": GENERIC_MUSE_MODEL,
        "effort": "max",
        "active": True,
        "command": ["musej", "exec", "--model", GENERIC_MUSE_MODEL],
        "headless_args": ["--output-schema", str(schema)],
        "prompt_via": "argv",
        "role_profile": profile.name,
    }


def build_agent_prompt(
    profile: RoleProfile, task: Mapping[str, Any],
    skill: str | None = None,
) -> str:
    """Ticket + suggested skill + mandatory verdict block."""
    lines = [
        f"You are the {profile.name} agent: {profile.card.who}",
        f"Never: {'; '.join(profile.card.never)}.",
        "",
        f"Ticket #{task.get('ticket_number')} ({task.get('task_id')}): "
        f"{task.get('title')}",
        "",
        str(task.get("description") or ""),
    ]
    params = task.get("params")
    if isinstance(params, dict):
        shown = {key: value for key, value in params.items()
                 if not str(key).startswith("_") and key != "skill"}
        if shown:
            lines.extend(["", f"Ticket data: {shown!r}"])
    if skill:
        lines.extend([
            "",
            f"Suggested skill: {skill} — read its SKILL.md and follow it; "
            "you decide how.",
        ])
    lines.extend(["", AGENT_RESULT_PROMPT])
    return "\n".join(lines)


def _step(name: str, status: str, detail: str) -> dict[str, Any]:
    return {"step": name, "status": status, "detail": detail,
            "timestamp": datetime.now(timezone.utc)}


def _waiting(agent: str, reason: str,
             params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "selected_agent": agent,
        "status": TaskStatus.WAITING.value,
        "steps": [_step("generic_agent", "waiting", reason)],
        "params": dict(params),
        "last_response_summary": None,
        "error": None,
        "waiting_reason": reason,
        "resume_at": None,
        "result_comment": None,
    }


async def run_generic_agent(
    agent_name: str, task: dict[str, Any], wiring: GenericAgentWiring,
) -> dict[str, Any]:
    """Run one catalog agent: exact skill first, else one LLM run."""
    catalog = wiring.catalog or load_catalog()
    params = dict(task.get("params") or {})
    entry = catalog.agents.get(agent_name)
    if entry is None or not entry.enabled or entry.type is not AgentType.AI:
        return _waiting(agent_name,
                        f"routing_missing: agent {agent_name!r} is not an "
                        "enabled AI executor", params)
    if entry.engine != _MUSE_ENGINE:
        return _waiting(agent_name,
                        f"routing_missing: agent {agent_name!r} engine "
                        f"{entry.engine!r} has no runner yet (translator "
                        "TASK-2026-00200)", params)
    if not (entry.profile or "").strip():
        return _waiting(agent_name,
                        f"routing_missing: agent {agent_name!r} is enabled "
                        "but has no runner yet", params)
    try:
        profile = load_role_profile(Path(str(entry.profile)).stem)
    except CatalogError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    skills_root = Path(wiring.repo_root) if wiring.repo_root is not None \
        else catalog_root().parent
    try:
        skills = {skill.name: skill for skill in load_role_skills(
            agent_name, entry.skills_dir, skills_root)}
    except CatalogError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    skill_name = str(params.get("skill") or "").strip() or None
    skill = skills.get(skill_name) if skill_name else None
    if skill_name is not None and skill is None:
        return _waiting(agent_name,
                        f"config: suggested skill {skill_name!r} is not a "
                        f"{agent_name} skill", params)
    exact = resolve_exact_skill_path(skill.source, repo_root=skills_root) \
        if skill is not None else None
    if exact is not None:
        return await _run_exact_skill(
            agent_name, task, params, profile, skill_name or "", exact,
            wiring)
    return await _run_llm(agent_name, task, params, profile, skill_name,
                          wiring)


async def _run_exact_skill(
    agent_name: str, task: dict[str, Any], params: dict[str, Any],
    profile: RoleProfile, skill_name: str, workflow_path: Path,
    wiring: GenericAgentWiring,
) -> dict[str, Any]:
    """Deterministic path: the skill's YAML runs, the LLM stays out."""
    del profile
    service = wiring.workflow_service
    if service is None:
        return _waiting(agent_name,
                        "config: exact skills need a workflow service",
                        params)
    try:
        raw = workflow_path.read_text(encoding="utf-8")
        workflow = service.parse_workflow_text(raw, source=str(workflow_path))
    except Exception as exc:
        logger.warning("exact skill %s has an invalid workflow %s: %s",
                       skill_name, workflow_path, exc)
        return _waiting(agent_name,
                        f"config: skill {skill_name!r} carries an invalid "
                        f"workflow ({type(exc).__name__})", params)
    workflow_id = str(workflow["workflow"])
    try:
        outcome = await service.execute_task_workflow(task, workflow)
    except Exception as exc:
        logger.warning("exact skill %s engine run failed: %s",
                       skill_name, exc, exc_info=True)
        return _failed(agent_name, params, f"skill {skill_name}",
                       error=f"workflow {workflow_id} raised "
                             f"{type(exc).__name__}: {exc}",
                       verdict=AgentResult(
                           resultado=AgentOutcome.BLOQUEADO,
                           motivo=f"workflow {workflow_id} raised "
                                  f"{type(exc).__name__}"))
    status = str(outcome.status or "")
    if status == TaskStatus.COMPLETED.value:
        summary = str(outcome.last_response_summary or "").strip() \
            or f"workflow {workflow_id} completed"
        verdict = AgentResult(resultado=AgentOutcome.HECHO, motivo=summary)
        out_params = {**params, "agent_result": verdict.model_dump(mode="json")}
        if isinstance(outcome.params, dict):
            out_params.update({key: value
                               for key, value in outcome.params.items()
                               if key != "agent_result"})
        return {
            "selected_agent": agent_name,
            "status": TaskStatus.COMPLETED.value,
            "steps": [_step("generic_agent", "ok",
                            f"exact skill {skill_name} via workflow "
                            f"{workflow_id}"),
                      *list(outcome.steps or [])],
            "params": out_params,
            "last_response_summary": summary,
            "error": None,
            "result_comment": (f"{agent_name}: hecho ({workflow_id}): "
                               f"{summary}"),
        }
    if status == TaskStatus.FAILED.value:
        detail = str(outcome.error or outcome.last_response_summary or ""
                     ).strip() or f"workflow {workflow_id} failed"
        failed = _failed(agent_name, params, f"skill {skill_name}",
                         steps=list(outcome.steps or []),
                         error=detail,
                         verdict=AgentResult(
                             resultado=AgentOutcome.BLOQUEADO, motivo=detail))
        failed["result_comment"] = (
            f"{agent_name}: bloqueado ({workflow_id}): {detail}")
        return failed
    return {
        "selected_agent": agent_name,
        "status": TaskStatus.WAITING.value,
        "steps": [_step("generic_agent", "waiting",
                        f"exact skill {skill_name} via workflow "
                        f"{workflow_id}: {outcome.waiting_reason or status}"),
                  *list(outcome.steps or [])],
        "params": dict(params) if outcome.params is None
        else {**params, **outcome.params},
        "last_response_summary": outcome.last_response_summary,
        "error": outcome.error,
        "waiting_reason": str(outcome.waiting_reason or status),
        "resume_at": None,
        "result_comment": None,
    }


async def _run_llm(
    agent_name: str, task: dict[str, Any], params: dict[str, Any],
    profile: RoleProfile, skill_name: str | None,
    wiring: GenericAgentWiring,
) -> dict[str, Any]:
    """One fenced Muse run with profile, badge, folder and verdict schema."""
    if wiring.settings is None:
        return _waiting(agent_name,
                        "config: generic runs need service settings", params)
    badge_factory = wiring.badge_provider_factory
    if badge_factory is None:
        issuer = issuer_for_repo(wiring.repo)
        if issuer is None:
            return _waiting(agent_name,
                            "config: MCP identity is not bound, refusing "
                            "an unbadged agent run", params)
        settings = wiring.settings

        def badge_factory(agent: str, ticket: str,
                          project: str) -> TicketBadgeProvider:
            return TicketBadgeProvider(issuer, settings, agent, ticket,
                                       project)
    try:
        spec = muse_role_spec(profile, agent_name)
    except CatalogError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    task_id = str(task.get("task_id") or "")
    try:
        workdir = ensure_ticket_dir(task_id,
                                    base_dir=(Path(wiring.work_base_dir)
                                              if wiring.work_base_dir
                                              is not None else None))
    except ValueError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    factory = wiring.executor_factory or default_executor_factory
    provider = badge_factory(agent_name, task_id,
                             str(task.get("project") or ""))
    executor = factory(agent_name, spec, provider)
    prompt = build_agent_prompt(profile, task, skill=skill_name)
    try:
        result = await executor.run(agent_name, prompt, workdir,
                                    wiring.timeout_s)
    finally:
        try:
            await executor.cleanup(agent_name)
        except Exception:
            logger.warning("generic executor cleanup failed for %s",
                           task_id, exc_info=True)
    verdict = parse_agent_result(result.output_tail)
    if result.returncode != 0:
        detail = (f"agent run failed (returncode {result.returncode})"
                  + (f": {result.output_tail.strip()[:500]}"
                     if result.output_tail and result.output_tail.strip()
                     else ""))
        return _failed(agent_name, params, "LLM run", error=detail,
                       verdict=verdict)
    if verdict is None:
        summary = (result.output_tail or "").strip() or "ok"
        return {
            "selected_agent": agent_name,
            "status": TaskStatus.COMPLETED.value,
            "steps": [_step("generic_agent", "ok",
                            "LLM run completed without a verdict (legacy)")],
            "params": dict(params),
            "last_response_summary": summary[:500],
            "error": None,
            "result_comment": f"{agent_name}: {summary[:500]}",
        }
    if verdict.resultado is AgentOutcome.HECHO:
        out_params = {**params,
                      "agent_result": verdict.model_dump(mode="json")}
        return {
            "selected_agent": agent_name,
            "status": TaskStatus.COMPLETED.value,
            "steps": [_step("generic_agent", "ok",
                            f"LLM run verdict: hecho ({verdict.motivo})")],
            "params": out_params,
            "last_response_summary": verdict.motivo,
            "error": None,
            "result_comment": f"{agent_name}: hecho: {verdict.motivo}",
        }
    return _failed(agent_name, params, "LLM run",
                   error=verdict.motivo, verdict=verdict)


def _failed(agent_name: str, params: dict[str, Any], where: str, *,
            error: str, verdict: AgentResult | None,
            steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out_params = dict(params)
    if verdict is not None:
        out_params["agent_result"] = verdict.model_dump(mode="json")
    comment = (f"{agent_name}: {verdict.resultado.value}: {verdict.motivo}"
               if verdict is not None else f"{agent_name}: failed: {error}")
    return {
        "selected_agent": agent_name,
        "status": TaskStatus.FAILED.value,
        "steps": [_step("generic_agent", "failed", f"{where}: {error}"),
                  *(steps or [])],
        "params": out_params,
        "last_response_summary": None,
        "error": error,
        "result_comment": comment,
    }
