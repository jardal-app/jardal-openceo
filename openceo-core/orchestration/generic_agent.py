"""Generic CLI agent runner: any catalog agent, one headless run (TASK-2026-00205).

Two paths, no silent reclassification:

* exact skill: the suggested skill carries ``workflow.yaml`` (its exact
  part) and the ticket targets it via ``params.skill`` — the YAML engine
  runs it, the LLM is never invoked;
* LLM: one fenced run on the resolved engine (muse default) with the
  role profile (card, skills, tool box badge), the ticket folder, the
  crystallization draft instruction and the structured verdict schema.
  Skilled runs stage their skill links into the ticket folder and mount
  the repo read-only; creation agents also mount the creation roots
  writable (TASK-2026-00214).

Upstream outputs (TASK-2026-00211) resolve from the ``blocked_by``
predecessors before either path: the LLM prompt carries them as a
section and the exact workflow receives them in its params, with
verified files staged under ``upstream/<task_id>/`` in both.

Verdict semantics belong to TASK-2026-00202: hecho completes, bloqueado
and no_aplica fail with the verdict stored (objective children return
once to the planner through the existing funnel), and a missing verdict
is legacy behavior, never a failure. Two harness exceptions, fail-closed:
a crash invalidates a hecho claim (rewritten bloqueado, so the planner
loop still runs), and a truncated output makes any parse untrustworthy
(bloqueado, review 00006). The orchestrator owns the outcome:
this runner returns ``result_comment`` and ``run_task`` posts it to
ERPNext; the agent never changes ticket states.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from catalog import (
    AgentType,
    Catalog,
    CatalogError,
    RoleProfile,
    catalog_root,
    default_engine_account,
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
from objectives import ticket_cost as cost_mod
from orchestration.agent_sandbox import ensure_ticket_dir
from orchestration.crystallization import CRYSTALLIZATION_PROMPT
from orchestration.engines import (
    ATTEMPT_PARAMS_KEY,
    ENGINE_ACCOUNT_PARAMS_KEY,
    ENGINE_PARAMS_KEY,
    ENGINE_SPECS,
    EngineResolutionError,
    EngineSelection,
    check_engine_available,
    command_for,
    resolve_engine,
)
from orchestration.live import ProcessExecutor
from orchestration.ticket_handoff import (
    UPSTREAM_OUTPUTS_KEY,
    render_upstream_section,
    resolve_upstream_outputs,
    stage_upstream_files,
)
from orchestration.role_muse import (
    TicketBadgeProvider,
    badge_ticket_for_task,
    issuer_for_repo,
)
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
    "role_extra_mounts",
    "role_spec_for_engine",
    "run_generic_agent",
    "stage_skill_links",
]

#: The exact part of a skill: when present, the skill runs deterministically.
EXACT_SKILL_WORKFLOW_FILENAME = "workflow.yaml"

#: One headless run budget (same as the simple-tier one-shot runs).
GENERIC_AGENT_TIMEOUT_S = 1800

#: House Muse model until cards pin their own.
GENERIC_MUSE_MODEL = "muse-spark-1.3-contributor"


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


#: Agents allowed writable creation roots (operations creates projects;
#: later migrations extend the map in their own ticket).
_WRITABLE_ROOT_AGENTS = frozenset({"operations"})


def stage_skill_links(ticket_dir: str | Path,
                      skills: Mapping[str, Any]) -> list[str]:
    """Expose the agent's skills where the run discovers them.

    Muse discovers project skills under ``.codex/skills/`` from the cwd,
    so each skill source links there (host side, before the fence; the
    repo mounts read-only and the links resolve inside). A stale link is
    replaced; a real file or dir blocking the name is left alone, never
    deleted. Returns the staged skill names.
    """
    base = Path(ticket_dir) / ".codex" / "skills"
    staged: list[str] = []
    for name, skill in skills.items():
        source = Path(str(getattr(skill, "source", "")))
        if not name or not source.is_absolute() or not source.is_dir():
            continue
        link = base / name
        try:
            if link.is_symlink():
                if os.path.realpath(link) == os.path.realpath(source):
                    staged.append(name)
                    continue
                link.unlink()
            elif link.exists():
                logger.warning("skill link %s blocked by a real path",
                               link)
                continue
            base.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source)
        except OSError as exc:
            logger.warning("skill link %s failed: %s", link, exc)
            continue
        staged.append(name)
    return staged


def role_extra_mounts(agent_name: str, *, has_skills: bool,
                      settings: Any) -> tuple[list[str], list[str]]:
    """Fence mounts for one generic run: ``(read_roots, write_roots)``.

    Skilled runs read the repo (skill sources plus the service modules
    the skill scripts import); only creation agents write the creation
    roots. Same absolute paths inside and outside, so skill defaults
    just work — both sides resolve from the same Settings, and the
    service .env must not override these three keys (review 00007).
    """
    read: list[str] = []
    write: list[str] = []
    if has_skills:
        read.append(str(catalog_root().parent))
    if agent_name in _WRITABLE_ROOT_AGENTS:
        from config_loader import ConfigLoader
        repos = Path(str(settings.PROJECT_REPOS_ROOT)).expanduser()
        vault = Path(str(settings.VAULT_ROOT)).expanduser()
        projects = ConfigLoader(settings).projects_root()
        write.extend([str(repos), str(projects), str(vault)])
    return read, write


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
    engine = ENGINE_SPECS["muse-code"]
    return {
        "provider": engine.provider,
        "account": default_engine_account("muse-code"),
        "label": label,
        "model": engine.model,
        "effort": engine.effort,
        "active": True,
        "command": list(engine.command),
        "headless_args": ["--output-schema", str(schema)],
        "prompt_via": engine.prompt_via,
        "role_profile": profile.name,
    }


def role_spec_for_engine(
    selection: EngineSelection,
    profile: RoleProfile,
    label: str,
) -> dict[str, Any]:
    """One-shot spec for a resolved engine (TASK-2026-00246).

    Muse keeps its schema-pinned builder; other engines render from the
    registry (their CLIs take the verdict contract from the prompt, and
    their translators add the MCP extras at launch).
    """
    if selection.engine == "muse-code":
        return muse_role_spec(profile, label)
    spec = ENGINE_SPECS[selection.engine]
    return {
        "provider": spec.provider,
        "account": selection.account,
        "label": label,
        "model": spec.model,
        "effort": spec.effort,
        "active": True,
        "command": command_for(selection.engine, selection.account),
        "headless_args": list(spec.headless_args),
        "prompt_via": spec.prompt_via,
        "role_profile": profile.name,
    }


def build_agent_prompt(
    profile: RoleProfile, task: Mapping[str, Any],
    skill: str | None = None,
    upstream_section: str = "",
    skill_outputs: Sequence[str] = (),
    skill_evidence: Sequence[str] = (),
    skill_source: str | None = None,
) -> str:
    """Ticket + upstream outputs + suggested skill + verdict block."""
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
    if upstream_section.strip():
        lines.extend(["", upstream_section.strip()])
    if skill:
        where = f" at {skill_source}" if (skill_source or "").strip() \
            else ""
        lines.extend([
            "",
            f"Suggested skill: {skill} — read its SKILL.md{where} and "
            "follow it; you decide how.",
        ])
        declared: list[str] = []
        if list(skill_outputs):
            declared.append(
                f"It declares outputs: {', '.join(skill_outputs)} — "
                "name each one in your entregables.")
        if list(skill_evidence):
            declared.append(
                f"Required evidence: {', '.join(skill_evidence)} — "
                "include it in your evidencia.")
        lines.extend(declared)
    lines.extend(["", CRYSTALLIZATION_PROMPT, "", AGENT_RESULT_PROMPT])
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


async def _resolve_upstream(
    task: Mapping[str, Any], wiring: GenericAgentWiring,
) -> list[dict[str, Any]]:
    """Predecessor outputs for this run, [] without repo or blockers."""
    blocked = task.get("blocked_by") or []
    repo = wiring.repo
    task_id = str(task.get("task_id") or "")
    if not blocked and repo is not None and task_id:
        try:
            row = await repo.get_task(task_id)
        except Exception:  # noqa: BLE001 - no upstream, the run proceeds
            row = None
        if isinstance(row, dict):
            blocked = row.get("blocked_by") or []
    if not blocked or repo is None:
        return []
    try:
        return await resolve_upstream_outputs(
            repo, [str(dep) for dep in blocked], wiring.work_base_dir)
    except Exception:  # noqa: BLE001 - no upstream, the run proceeds
        logger.warning("upstream outputs unresolved for %s", task_id,
                       exc_info=True)
        return []


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
    try:
        selection = resolve_engine(entry.engine, entry.account, params)
    except EngineResolutionError as exc:
        if str(params.get(ENGINE_PARAMS_KEY)
               or params.get(ENGINE_ACCOUNT_PARAMS_KEY) or "").strip():
            reason = (f"config: invalid engine override for "
                      f"{agent_name!r}: {exc}")
        else:
            reason = (f"routing_missing: agent {agent_name!r} engine "
                      f"{entry.engine!r} has no runner yet ({exc})")
        return _waiting(agent_name, reason, params)
    unavailable = check_engine_available(selection.engine,
                                         selection.account)
    if unavailable is not None:
        return _waiting(agent_name, f"config: {unavailable}", params)
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
    upstream = await _resolve_upstream(task, wiring)
    if exact is not None:
        return await _run_exact_skill(
            agent_name, task, params, profile, skill_name or "", exact,
            wiring, upstream=upstream)
    return await _run_llm(agent_name, task, params, profile, selection,
                          skill_name, wiring, skills, upstream=upstream)


#: Exact verdict citations stay bounded: lines, then chars per value.
_EXACT_CITE_LINES = 20
_EXACT_CITE_VALUE_CHARS = 200


def _cite_workflow_outputs(outputs: Mapping[str, Any]) -> list[str]:
    """One citation line per scalar step output, dotted and bounded."""
    lines: list[str] = []

    def visit(prefix: str, value: Any, depth: int) -> None:
        if len(lines) >= _EXACT_CITE_LINES or depth > 2:
            return
        if isinstance(value, Mapping):
            for key in sorted(str(item) for item in value):
                visit(f"{prefix}.{key}" if prefix else str(key),
                      value[key], depth + 1)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value)
            if len(text) > _EXACT_CITE_VALUE_CHARS:
                text = text[:_EXACT_CITE_VALUE_CHARS] + "…"
            lines.append(f"{prefix}: {text}")

    if isinstance(outputs, Mapping):
        for step in sorted(str(item) for item in outputs):
            visit(str(step), outputs[step], 0)
    return lines


async def _read_workflow_cites(service: Any, task_id: str) -> list[str]:
    """Latest run outputs as verdict citations, [] when unreadable."""
    latest = getattr(service, "latest_workflow_outputs", None)
    if latest is None or not task_id:
        return []
    try:
        outputs = await latest(task_id)
    except Exception:  # noqa: BLE001 - citations never fail a run
        return []
    if not isinstance(outputs, Mapping):
        return []
    return _cite_workflow_outputs(outputs)


async def _run_exact_skill(
    agent_name: str, task: dict[str, Any], params: dict[str, Any],
    profile: RoleProfile, skill_name: str, workflow_path: Path,
    wiring: GenericAgentWiring,
    upstream: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic path: the skill's YAML runs, the LLM stays out."""
    del profile
    service = wiring.workflow_service
    if service is None:
        return _waiting(agent_name,
                        "config: exact skills need a workflow service",
                        params)
    flow_task = task
    flow_params = params
    if upstream:
        try:
            workdir = ensure_ticket_dir(
                str(task.get("task_id") or ""),
                base_dir=(Path(wiring.work_base_dir)
                          if wiring.work_base_dir is not None else None))
            staged = stage_upstream_files(upstream, workdir)
        except ValueError:
            staged = []
        payload = {"tickets": upstream, "dir": "upstream",
                   "staged": [[pair[0], pair[1]] for pair in staged]}
        flow_params = {**params, UPSTREAM_OUTPUTS_KEY: payload}
        flow_task = {**task, "params": flow_params}
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
    started = time.monotonic()
    try:
        outcome = await service.execute_task_workflow(flow_task, workflow)
    except Exception as exc:
        logger.warning("exact skill %s engine run failed: %s",
                       skill_name, exc, exc_info=True)
        return _failed(agent_name, flow_params, f"skill {skill_name}",
                       error=f"workflow {workflow_id} raised "
                             f"{type(exc).__name__}: {exc}",
                       verdict=AgentResult(
                           resultado=AgentOutcome.BLOQUEADO,
                           motivo=f"workflow {workflow_id} raised "
                                  f"{type(exc).__name__}"),
                       cost_runs=[_exact_skill_run(
                           time.monotonic() - started)])
    status = str(outcome.status or "")
    runs = [_exact_skill_run(time.monotonic() - started)]
    if status == TaskStatus.COMPLETED.value:
        summary = str(outcome.last_response_summary or "").strip() \
            or f"workflow {workflow_id} completed"
        cites = await _read_workflow_cites(
            service, str(task.get("task_id") or ""))
        evidence = summary if not cites \
            else f"{summary} | {' | '.join(cites)}"
        verdict = AgentResult(resultado=AgentOutcome.HECHO, motivo=summary,
                              entregables=cites,
                              evidencia=evidence[:2000])
        out_params = {**flow_params,
                      "agent_result": verdict.model_dump(mode="json")}
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
            cost_mod.COST_RUNS_KEY: runs,
        }
    if status == TaskStatus.FAILED.value:
        detail = str(outcome.error or outcome.last_response_summary or ""
                     ).strip() or f"workflow {workflow_id} failed"
        failed = _failed(agent_name, flow_params, f"skill {skill_name}",
                         steps=list(outcome.steps or []),
                         error=detail,
                         verdict=AgentResult(
                             resultado=AgentOutcome.BLOQUEADO, motivo=detail),
                         cost_runs=runs)
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
        "params": dict(flow_params) if outcome.params is None
        else {**flow_params, **outcome.params},
        "last_response_summary": outcome.last_response_summary,
        "error": outcome.error,
        "waiting_reason": str(outcome.waiting_reason or status),
        "resume_at": None,
        "result_comment": None,
        cost_mod.COST_RUNS_KEY: runs,
    }


def _exact_skill_run(duration_s: float) -> dict[str, Any]:
    """Exact-skill cost: the LLM never runs, so 0 tokens is measured."""
    return cost_mod.normalize_run({
        "engine": "exact-skill",
        "duration_s": duration_s,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
    })


async def _run_llm(
    agent_name: str, task: dict[str, Any], params: dict[str, Any],
    profile: RoleProfile, selection: EngineSelection,
    skill_name: str | None,
    wiring: GenericAgentWiring,
    skills: Mapping[str, Any] | None = None,
    upstream: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One fenced run on the resolved engine, with profile, badge, folder
    and verdict schema. The spec is built once and the selection recorded
    in the result params, so a config change mid-run cannot hot-swap the
    engine; the next invocation re-resolves."""
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
        spec = role_spec_for_engine(selection, profile, agent_name)
    except CatalogError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    run_params = {**params, ATTEMPT_PARAMS_KEY: {
        "engine": selection.engine,
        "account": selection.account,
        "model": str(spec.get("model") or ""),
        "via": selection.via,
    }}
    task_id = str(task.get("task_id") or "")
    try:
        workdir = ensure_ticket_dir(task_id,
                                    base_dir=(Path(wiring.work_base_dir)
                                              if wiring.work_base_dir
                                              is not None else None))
    except ValueError as exc:
        return _waiting(agent_name, f"config: {exc}", params)
    stage_skill_links(workdir, skills or {})
    staged = stage_upstream_files(upstream or [], workdir)
    section = render_upstream_section(upstream or [], staged)
    read_roots, write_roots = role_extra_mounts(
        agent_name, has_skills=bool(skills), settings=wiring.settings)
    if read_roots:
        spec["extra_read_roots"] = read_roots
    if write_roots:
        spec["extra_write_roots"] = write_roots
    badge_ticket = badge_ticket_for_task(task)
    if badge_ticket is None and wiring.badge_provider_factory is None:
        return _waiting(agent_name,
                        "config: agent badges bind to the ERPNext mirror "
                        "ticket and this task has none", params)
    factory = wiring.executor_factory or default_executor_factory
    # Injected factories are test-only (production wires none and waited
    # above); the fallback keeps scripted runs working without a mirror.
    provider = badge_factory(agent_name, badge_ticket or task_id,
                             str(task.get("project") or ""))
    executor = factory(agent_name, spec, provider)
    declared_outputs: list[str] = []
    declared_evidence: list[str] = []
    skill_source: str | None = None
    if skill_name and skills:
        found = skills.get(skill_name)
        if found is not None:
            declared_outputs = [str(item)
                                for item in
                                (getattr(found, "outputs", []) or [])]
            declared_evidence = [str(item)
                                 for item in
                                 (getattr(found, "evidence", []) or [])]
            skill_source = str(getattr(found, "source", "") or "") or None
    prompt = build_agent_prompt(
        profile, task, skill=skill_name, upstream_section=section,
        skill_outputs=declared_outputs, skill_evidence=declared_evidence,
        skill_source=skill_source)
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
    usage = result.usage if isinstance(result.usage, dict) else {}
    truncated = bool(usage.get("truncated"))
    runs = [cost_mod.run_from_exec_result(
        usage=usage, elapsed_s=result.elapsed_s,
        engine=selection.engine, model=result.model)]
    if result.returncode != 0:
        detail = (f"agent run failed (returncode {result.returncode})"
                  + (f": {result.output_tail.strip()[:500]}"
                     if result.output_tail and result.output_tail.strip()
                     else ""))
        if verdict is not None \
                and verdict.resultado is AgentOutcome.HECHO:
            verdict = AgentResult(
                resultado=AgentOutcome.BLOQUEADO,
                motivo=(f"run crashed (returncode {result.returncode}); "
                        f"the agent claimed hecho: {verdict.motivo}"))
        return _failed(agent_name, run_params, "LLM run", error=detail,
                       verdict=verdict, cost_runs=runs)
    if truncated:
        motivo = (f"agent output truncated "
                  f"({usage.get('stdout_chars', '?')} chars), verdict "
                  "unreadable")
        if verdict is not None:
            motivo += f"; last parsed claim: {verdict.motivo}"
        return _failed(agent_name, run_params, "LLM run",
                       error=motivo,
                       verdict=AgentResult(
                           resultado=AgentOutcome.BLOQUEADO, motivo=motivo),
                       cost_runs=runs)
    if verdict is None:
        summary = (result.output_tail or "").strip() or "ok"
        return {
            "selected_agent": agent_name,
            "status": TaskStatus.COMPLETED.value,
            "steps": [_step("generic_agent", "ok",
                            "LLM run completed without a verdict (legacy)")],
            "params": dict(run_params),
            "last_response_summary": summary[:500],
            "error": None,
            "result_comment": f"{agent_name}: {summary[:500]}",
            cost_mod.COST_RUNS_KEY: runs,
        }
    if verdict.resultado is AgentOutcome.HECHO:
        out_params = {**run_params,
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
            cost_mod.COST_RUNS_KEY: runs,
        }
    return _failed(agent_name, run_params, "LLM run",
                   error=verdict.motivo, verdict=verdict, cost_runs=runs)


def _failed(agent_name: str, params: dict[str, Any], where: str, *,
            error: str, verdict: AgentResult | None,
            steps: list[dict[str, Any]] | None = None,
            cost_runs: list[dict[str, Any]] | None = None,
            ) -> dict[str, Any]:
    out_params = dict(params)
    if verdict is not None:
        out_params["agent_result"] = verdict.model_dump(mode="json")
    comment = (f"{agent_name}: {verdict.resultado.value}: {verdict.motivo}"
               if verdict is not None else f"{agent_name}: failed: {error}")
    out: dict[str, Any] = {
        "selected_agent": agent_name,
        "status": TaskStatus.FAILED.value,
        "steps": [_step("generic_agent", "failed", f"{where}: {error}"),
                  *(steps or [])],
        "params": out_params,
        "last_response_summary": None,
        "error": error,
        "result_comment": comment,
    }
    if cost_runs:
        out[cost_mod.COST_RUNS_KEY] = cost_runs
    return out
