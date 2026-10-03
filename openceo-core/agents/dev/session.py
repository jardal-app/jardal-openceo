from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .consensus import CriticalConsensusOrchestrator
from .kimi_session_manager import KimiSessionManager
from .kimi_run import detect_kimi_retryable_failure, parse_kimi_output
from .opencode_models import apply_opencode_model_override
from .opencode_paths import normalize_opencode_command
from .opencode_run import (
    build_run_command,
    build_run_shell_command,
    extract_response_sentinel_exit_code,
    parse_run_output,
)
from .review_links import ReviewLinksExtraction, apply_review_links_to_params, extract_review_links
from .routing import ROUTING_MISSING_REASON, resolve_coding_tool
from .tmux_runtime import TmuxRuntime
from config_loader import ConfigLoader
from rate_monitor import RateMonitor
from settings import Settings
from state import CodingTier, CodingTool, TaskStatus

from .tier_selector import TierSelector

logger = logging.getLogger(__name__)

#: Typed wait reason when the project's config is invalid (Tier S design §15).
CONFIG_INVALID_REASON = "config"

#: Typed wait reason when another execution holds the repo lock (§11.1).
REPO_BUSY_REASON = "repo_busy"


def _resolve_execution_repo(project_config: dict[str, Any]) -> str | None:
    """Repo path this execution would work on, for the common lock."""
    repos = (project_config.get("project") or {}).get("repos") or []
    if not repos:
        return None
    local_path = repos[0].get("local_path") or repos[0].get("path")
    if not local_path:
        return None
    path = Path(str(local_path))
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return str(local_path)


@dataclass
class DevExecutionOutput:
    status: TaskStatus
    steps: list[dict[str, Any]] = field(default_factory=list)
    coding_tier: CodingTier | None = None
    coding_tool: CodingTool | None = None
    coding_session_id: str | None = None
    coding_session_title: str | None = None
    last_prompt_sent: str | None = None
    last_response_summary: str | None = None
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    error: str | None = None
    modified_files: list[str] = field(default_factory=list)
    params: dict[str, Any] | None = None

    def as_state_patch(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "steps": self.steps,
            "coding_tier": self.coding_tier.value if self.coding_tier else None,
            "coding_tool": self.coding_tool.value if self.coding_tool else None,
            "coding_session_id": self.coding_session_id,
            "coding_session_title": self.coding_session_title,
            "last_prompt_sent": self.last_prompt_sent,
            "last_response_summary": self.last_response_summary,
            "waiting_reason": self.waiting_reason,
            "resume_at": self.resume_at,
            "error": self.error,
            "modified_files": self.modified_files,
            "params": self.params,
        }


class DevSessionManager:
    """Runs deterministic step-based Dev Agent sessions."""

    def __init__(self, settings: Settings, config_loader: ConfigLoader,
                 rate_monitor: RateMonitor | None = None, repo: Any | None = None) -> None:
        self._settings = settings
        self._config_loader = config_loader
        self._tier_selector = TierSelector(settings)
        self._rate_monitor = rate_monitor
        self._repo = repo
        self._kimi_sessions = KimiSessionManager(settings)
        self._consensus = CriticalConsensusOrchestrator(
            settings, TmuxRuntime(settings, rate_monitor=rate_monitor), rate_monitor=rate_monitor,
            repo=repo,
        )

    async def run(self, task: dict[str, Any], project_config: dict[str, Any]) -> DevExecutionOutput:
        dev_cfg = project_config.get("dev", {})
        qa_cfg = dev_cfg.get("qa", {})

        # Single routing reader (Tier S design §3): the top-level coding_tool,
        # with legacy fallbacks during the deprecation transition.
        decision = resolve_coding_tool(task, project_config)
        if decision.deprecated:
            logger.warning(
                "deprecated routing source=%s task_id=%s (%s)",
                decision.source, task.get("task_id"), decision.detail,
            )
        if decision.tool is None:
            # Not dispatched: no tier selection (it may call an LLM) and no
            # fallback. The ERP comment for the wait is posted by task_service.
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{ROUTING_MISSING_REASON}: {decision.detail}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Invalid project config parks every ticket, whatever the tier.
        config_errors = self._config_loader.validate_project(task.get("project"))
        if config_errors:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: {'; '.join(config_errors)}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Respect ticket-level tier overrides (e.g. manual classification)
        task_params = task.get("params") if isinstance(task.get("params"), dict) else {}
        task_tier = task.get("coding_tier") or task_params.get("coding_tier")

        if task_tier:
            try:
                tier = CodingTier(task_tier)
            except ValueError:
                logger.warning(
                    "invalid coding_tier=%r task_id=%s; falling back to selector",
                    task_tier, task.get("task_id"),
                )
                tier = await self._tier_selector.select(
                    str(dev_cfg.get("coding_tier", "auto")),
                    task.get("title", ""),
                    task.get("description", ""),
                )
        else:
            tier = await self._tier_selector.select(
                str(dev_cfg.get("coding_tier", "auto")),
                task.get("title", ""),
                task.get("description", ""),
            )

        # Common execution lock (§11.1): one repo, one execution, every tier.
        repo = _resolve_execution_repo(project_config) if self._repo is not None else None
        holder = str(task.get("task_id") or "")
        if repo is not None and holder:
            if await self._repo.acquire_repo_lock(repo, holder) is None:
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=f"{REPO_BUSY_REASON}: {repo}",
                    resume_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                    params=task.get("params") if isinstance(task.get("params"), dict) else None,
                )
        try:
            if decision.tool == CodingTool.TIER_S:
                output = await self._run_critical_consensus(task, project_config, tier, qa_cfg)
            else:
                output = await self._run_simple_tool(task, project_config, decision.tool, tier, qa_cfg)
        except Exception:
            if repo is not None and holder:
                await self._repo.release_repo_lock(repo, holder)
            raise
        if repo is not None and holder:
            if output.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED):
                await self._repo.release_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING:
                await self._repo.touch_repo_lock(repo, holder)
        return output

    async def _run_simple_tool(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        tier: CodingTier,
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        output = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            coding_tier=tier,
            coding_tool=tool,
        )

        code_result = await self._step_code(task, project_config, tool)
        output.steps.append(code_result.step)
        output.coding_session_id = code_result.session_id
        output.coding_session_title = code_result.session_title
        output.last_prompt_sent = code_result.prompt
        output.last_response_summary = code_result.summary
        output.params = apply_review_links_to_params(
            task.get("params"),
            code_result.review_links,
        )
        if code_result.waiting_reason:
            output.status = TaskStatus.WAITING
            output.waiting_reason = code_result.waiting_reason
            output.resume_at = code_result.resume_at or (datetime.now(timezone.utc) + timedelta(hours=2))
            return output
        if code_result.error:
            if "rate limit" in code_result.error.lower():
                output.status = TaskStatus.WAITING
                output.waiting_reason = code_result.error
                output.resume_at = datetime.now(timezone.utc) + timedelta(hours=2)
            else:
                output.status = TaskStatus.FAILED
                output.error = code_result.error
            return output

        qa_mode = str(qa_cfg.get("playwright", "optional"))
        max_retries = int(qa_cfg.get("max_retries", 2))

        step_3, qa_error = await self._step_qa(project_config, qa_mode, max_retries)
        output.steps.append(step_3)
        if qa_error:
            if qa_mode == "required":
                output.status = TaskStatus.ESCALATED
            else:
                output.status = TaskStatus.FAILED
            output.error = qa_error
            return output

        step_5 = await self._step_update_files(task, project_config)
        output.steps.append(step_5)
        if step_5["status"] == "failed":
            output.status = TaskStatus.FAILED
            output.error = step_5["detail"]
            return output

        output.status = TaskStatus.COMPLETED
        return output

    async def _record_tier_s_verification(self, task: dict[str, Any], step: dict[str, Any]) -> None:
        if self._repo is None:
            return
        task_id = str(task.get("task_id") or "")
        if not task_id:
            return
        stored = await self._repo.get_task(task_id)
        run_id = str((stored or {}).get("tier_s_run_id") or "")
        if run_id:
            verification = {"qa_status": step.get("status"), "detail": step.get("detail")}
            await self._repo.patch_tier_s_run(run_id, {"verification": verification})
            summary = {**(stored.get("tier_s_progress") or {}), "verification": verification}
            await self._repo.patch_task(task_id, {"tier_s_progress": summary})
            adapter = getattr(self._consensus, "_erpnext", None)
            mirrors = (stored.get("params") or {}).get("_mirrors") or {}
            mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
            name = str(mirror.get("name") or "") if isinstance(mirror, dict) else ""
            if adapter is not None and name:
                try:
                    await adapter.update_tier_s_progress(name, summary)
                except Exception:
                    logger.warning("Tier S QA projection failed for %s", task_id, exc_info=True)

    async def _run_critical_consensus(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tier: CodingTier,
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        output = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            coding_tier=tier,
            coding_tool=CodingTool.TIER_S,
        )

        consensus_result = await self._step_critical_consensus(task, project_config)
        output.steps.append(consensus_result.step)
        output.last_prompt_sent = consensus_result.prompt
        output.last_response_summary = consensus_result.summary
        output.params = apply_review_links_to_params(
            task.get("params"),
            consensus_result.review_links,
        )
        if consensus_result.waiting_reason:
            output.status = TaskStatus.WAITING
            output.waiting_reason = consensus_result.waiting_reason
            return output
        if consensus_result.error:
            output.status = TaskStatus.FAILED
            output.error = consensus_result.error
            return output

        qa_mode = str(qa_cfg.get("playwright", "optional"))
        max_retries = int(qa_cfg.get("max_retries", 2))

        step_3, qa_error = await self._step_qa(project_config, qa_mode, max_retries)
        output.steps.append(step_3)
        await self._record_tier_s_verification(task, step_3)
        if qa_error:
            if qa_mode == "required":
                output.status = TaskStatus.ESCALATED
            else:
                output.status = TaskStatus.FAILED
            output.error = qa_error
            return output

        step_5 = await self._step_update_files(task, project_config)
        output.steps.append(step_5)
        if step_5["status"] == "failed":
            output.status = TaskStatus.FAILED
            output.error = step_5["detail"]
            return output

        output.status = TaskStatus.COMPLETED
        return output

    async def _step_code(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
        tool: CodingTool,
    ) -> "CodingStepResult":
        prompt = self._build_coding_prompt(task, config)

        if self._settings.DEV_EXECUTION_MODE == "mock":
            summary = (
                f"MOCK {tool.value} execution completed for task {task.get('task_id')}. "
                "No external coding CLI was executed."
            )
            return CodingStepResult(
                step=self._step("code", "ok", summary),
                prompt=prompt,
                summary=summary,
            )

        if tool == CodingTool.OPENCODE:
            return await self._step_opencode(task, config, prompt)

        if tool == CodingTool.KIMI:
            return await self._step_kimi(task, config, prompt)

        command = self._resolve_cli_command(tool, config)
        ok, output = await self._run_command(command, stdin_payload=prompt, timeout=1800)

        if not ok:
            return CodingStepResult(
                step=self._step("code", "failed", output),
                prompt=prompt,
                summary="",
                error=output,
            )

        review_links = extract_review_links(output, self._review_base_url(config))
        summary = self._truncate(output, 500)
        return CodingStepResult(
            step=self._step("code", "ok", "Coding CLI finished"),
            prompt=prompt,
            summary=summary,
            review_links=review_links,
        )

    async def _step_critical_consensus(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
    ) -> "CodingStepResult":
        prompt = self._build_coding_prompt(task, config)

        if self._settings.DEV_EXECUTION_MODE == "mock":
            summary = (
                f"MOCK {CodingTool.TIER_S.value} execution completed for task {task.get('task_id')}. "
                "No external coding CLI was executed."
            )
            return CodingStepResult(
                step=self._step("tier_s", "ok", summary),
                prompt=prompt,
                summary=summary,
            )

        async def noop_progress(_: str) -> None:
            return None

        task_id = task.get("task_id") or task.get("_id")
        tags = task.get("tags") or []
        is_spillover = any(tag.startswith("spillover_from:") for tag in tags)

        result = await self._consensus.execute(
            project=str(task.get("project") or "default"),
            instruction=prompt,
            config=config,
            progress=noop_progress,
            task_id=task_id,
            is_spillover=is_spillover,
        )
        if not result.ok:
            detail = result.error or result.summary
            review_links = extract_review_links(
                result.last_output or result.summary,
                self._review_base_url(config),
            )
            return CodingStepResult(
                step=self._step("tier_s", "waiting" if result.waiting else "failed", detail),
                prompt=prompt,
                summary=result.summary,
                error=None if result.waiting else detail,
                waiting_reason=result.summary if result.waiting else None,
                review_links=review_links,
            )

        review_links = extract_review_links(
            result.last_output or result.summary,
            self._review_base_url(config),
        )
        summary = self._truncate(result.summary or result.last_output or "Tier S completed", 500)
        return CodingStepResult(
            step=self._step("tier_s", "ok", "Tier S completed"),
            prompt=prompt,
            summary=summary,
            review_links=review_links,
        )

    async def _step_opencode(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
        prompt: str,
    ) -> "CodingStepResult":
        command, cwd, env_vars = self._resolve_provider_runtime(CodingTool.OPENCODE, config)
        session_title = self._build_session_title(task)
        shell_command, sentinel = build_run_shell_command(
            base_command=command,
            prompt=prompt,
            cwd=cwd,
            title=session_title,
            agent_name="build",
        )
        ok, stdout, stderr = await self._run_process(
            ["/bin/sh", "-lc", shell_command],
            timeout=1800,
            cwd=cwd,
            env=env_vars,
        )
        combined_output = "\n".join(part for part in (stdout, stderr) if part).strip()
        parsed = parse_run_output(combined_output)
        review_links = extract_review_links(
            parsed.raw_text or parsed.final_text or combined_output,
            self._review_base_url(config),
        )

        if not ok:
            error = self._summarize_failure_output(
                stdout,
                stderr,
                fallback=parsed.error_text or "OpenCode build failed",
            )
            return CodingStepResult(
                step=self._step("code", "failed", error),
                prompt=prompt,
                summary="",
                error=error,
                review_links=review_links,
            )

        sentinel_exit = extract_response_sentinel_exit_code(combined_output, sentinel)
        if sentinel_exit is None:
            error = "OpenCode run did not emit completion sentinel"
            return CodingStepResult(
                step=self._step("code", "failed", error),
                prompt=prompt,
                summary="",
                error=error,
                review_links=review_links,
            )

        if sentinel_exit != 0:
            error = self._summarize_failure_output(
                stdout,
                stderr,
                fallback=parsed.error_text or f"OpenCode build failed with exit code {sentinel_exit}",
            )
            return CodingStepResult(
                step=self._step("code", "failed", error),
                prompt=prompt,
                summary="",
                error=error,
                review_links=review_links,
            )

        if parsed.error_text:
            error = self._truncate(parsed.error_text, 600)
            return CodingStepResult(
                step=self._step("code", "failed", error),
                prompt=prompt,
                summary="",
                error=error,
                review_links=review_links,
            )

        summary = self._truncate(
            parsed.final_text or parsed.raw_text or combined_output or "OpenCode build finished",
            500,
        )
        return CodingStepResult(
            step=self._step("code", "ok", "OpenCode build finished"),
            prompt=prompt,
            summary=summary,
            session_id=parsed.session_id,
            session_title=session_title,
            review_links=review_links,
        )

    async def _step_kimi(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
        prompt: str,
    ) -> "CodingStepResult":
        command, cwd, env_vars = self._resolve_provider_runtime(CodingTool.KIMI, config)
        run_command = self._build_kimi_command(command, prompt, cwd)
        process_result = await self._run_process_detailed(
            run_command,
            timeout=1800,
            cwd=cwd,
            env=env_vars,
        )
        combined_output = "\n".join(
            part for part in (process_result.stdout, process_result.stderr) if part
        ).strip()
        parsed = parse_kimi_output(combined_output)
        review_links = extract_review_links(
            parsed.raw_text or parsed.final_text or combined_output,
            self._review_base_url(config),
        )

        if not process_result.ok:
            retryable = detect_kimi_retryable_failure(process_result.returncode, combined_output)
            if retryable is not None:
                wait_seconds = retryable.wait_seconds or 7200
                detail = self._truncate(retryable.detail, 600)
                return CodingStepResult(
                    step=self._step("code", "waiting", detail),
                    prompt=prompt,
                    summary=detail,
                    session_id=parsed.session_id,
                    waiting_reason=retryable.waiting_reason,
                    resume_at=datetime.now(timezone.utc) + timedelta(seconds=wait_seconds),
                    review_links=review_links,
                )

            error = self._summarize_failure_output(
                process_result.stdout,
                process_result.stderr,
                fallback="Kimi CLI failed",
            )
            return CodingStepResult(
                step=self._step("code", "failed", error),
                prompt=prompt,
                summary="",
                error=error,
                session_id=parsed.session_id,
                review_links=review_links,
            )

        session_title: str | None = None
        desired_title = self._build_session_title(task)
        if parsed.session_id and cwd:
            try:
                if self._kimi_sessions.set_session_title(cwd, parsed.session_id, desired_title):
                    session_title = desired_title
            except Exception:
                session_title = None
        summary = self._truncate(
            parsed.final_text or parsed.raw_text or combined_output or "Kimi finished",
            500,
        )
        return CodingStepResult(
            step=self._step("code", "ok", "Kimi finished"),
            prompt=prompt,
            summary=summary,
            session_id=parsed.session_id,
            session_title=session_title,
            review_links=review_links,
        )

    async def _step_qa(
        self,
        config: dict[str, Any],
        mode: str,
        max_retries: int,
    ) -> tuple[dict[str, Any], str | None]:
        normalized = mode.strip().lower()

        if normalized == "skip":
            return self._step("qa", "skipped", "QA disabled by project config"), None

        tests_exist = self._detect_tests(config)

        if normalized == "optional" and not tests_exist:
            return self._step("qa", "skipped", "No tests detected"), None

        if self._settings.DEV_EXECUTION_MODE == "mock":
            return self._step("qa", "ok", "Mock QA passed"), None

        command = ["pytest", "-q"]
        retries = max(1, max_retries)

        for attempt in range(1, retries + 1):
            ok, output = await self._run_command(command, timeout=1200)
            if ok:
                return self._step("qa", "ok", f"QA passed on attempt {attempt}"), None

        return self._step("qa", "failed", "QA retries exhausted"), output

    async def _step_update_files(self, task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        try:
            project = task.get("project", "default")
            context_path = self._config_loader.context_path(project)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            context_line = (
                f"\n## {now} - {task.get('task_id')}\n"
                f"- Title: {task.get('title')}\n"
                f"- Summary: {self._truncate(task.get('description', ''), 300)}\n"
            )
            self._append_text(context_path, context_line)

            return self._step("update_files", "ok", "CONTEXT.md updated")
        except Exception as exc:
            return self._step("update_files", "skipped", f"CONTEXT.md unavailable: {exc}")

    def _resolve_cli_command(self, tool: CodingTool, config: dict[str, Any]) -> list[str]:
        command: list[str]
        cmd = config.get("dev", {}).get("cli_command")
        if isinstance(cmd, list) and cmd:
            command = [str(token) for token in cmd]
        elif isinstance(cmd, str) and cmd.strip():
            command = shlex.split(cmd)
        else:
            provider_cfg = (config.get("dev", {}).get("providers", {}) or {}).get(tool.value, {})
            provider_command = provider_cfg.get("command")
            if isinstance(provider_command, list) and provider_command:
                command = [str(token) for token in provider_command]
            elif isinstance(provider_command, str) and provider_command.strip():
                command = shlex.split(provider_command)
            elif tool == CodingTool.CODEX:
                command = [self._settings.CODEX_CLI_PATH, *shlex.split(self._settings.CODEX_DEFAULT_ARGS)]
            elif tool == CodingTool.GEMINI:
                command = [self._settings.GEMINI_CLI_PATH, *shlex.split(self._settings.GEMINI_DEFAULT_ARGS)]
            elif tool == CodingTool.OPENCODE:
                command = [self._settings.OPENCODE_CLI_PATH, *shlex.split(self._settings.OPENCODE_DEFAULT_ARGS)]
            elif tool == CodingTool.GLM:
                command = [self._settings.GLM_CLI_PATH, *shlex.split(self._settings.GLM_DEFAULT_ARGS)]
            elif tool == CodingTool.KIMI:
                command = [self._settings.KIMI_CLI_PATH, *shlex.split(self._settings.KIMI_DEFAULT_ARGS)]
            else:
                command = [self._settings.CLAUDE_CODE_PATH, *shlex.split(self._settings.CLAUDE_DEFAULT_ARGS)]

        if tool == CodingTool.OPENCODE:
            command = apply_opencode_model_override(command, self._settings.OPENCODE_MODEL_OVERRIDE)
            command = normalize_opencode_command(command)
        return command

    def _resolve_provider_runtime(
        self,
        tool: CodingTool,
        config: dict[str, Any],
    ) -> tuple[list[str], str | None, dict[str, str]]:
        provider_cfg = (config.get("dev", {}).get("providers", {}) or {}).get(tool.value, {})
        command = self._resolve_cli_command(tool, config)
        env_vars = {
            str(key): str(value)
            for key, value in dict(provider_cfg.get("env") or {}).items()
        }
        cwd = provider_cfg.get("cwd")
        if cwd is None:
            cwd = self._resolve_repo_path(config)
        if cwd is not None:
            cwd = str(cwd)
        return command, cwd, env_vars

    def _build_coding_prompt(self, task: dict[str, Any], config: dict[str, Any]) -> str:
        pm = (task.get("params") or {}).get("pm_ticket") or {}
        ticket_num = task.get("ticket_number")
        review_base_url = self._review_base_url(config)

        lines: list[str] = [
            "Lee AGENTS.md en la raíz del proyecto antes de implementar y cumple cada regla exactamente.",
            "",
            "TAREA",
        ]

        if ticket_num is not None:
            lines.append(f"Ticket: #{ticket_num}")
        lines.append(f"Título: {task.get('title')}")
        lines.append(f"Descripción: {task.get('description')}")

        module = pm.get("module")
        if module:
            lines.append(f"Módulo: {module}")

        ticket_type = pm.get("type")
        if ticket_type:
            lines.append(f"Tipo: {ticket_type}")

        acceptance = pm.get("acceptance_criteria")
        if acceptance:
            lines.append("")
            lines.append("Criterios de aceptación:")
            for idx, criterion in enumerate(acceptance, 1):
                lines.append(f"  {idx}. {criterion}")

        attachments = task.get("attachments")
        if attachments:
            lines.append("")
            lines.append(f"Adjuntos: {json.dumps(attachments)}")

        lines.extend(
            [
                "",
                "FORMATO FINAL OBLIGATORIO",
                "Al final de tu respuesta incluye exactamente un bloque REVIEW_LINKS con los links directos para QA/cliente de las vistas afectadas.",
                "Si hay varias vistas afectadas, escribe un link por línea dentro del bloque.",
                "Si el ticket toca vistas, router, menú, componentes visuales o renderizado, cumple también el smoke test/browser exigido por AGENTS.md.",
            ]
        )
        if review_base_url:
            lines.append(f"Usa URLs completas bajo: {review_base_url}")
        else:
            lines.append("Si no conoces el dominio, usa rutas directas tipo /dashboard/...")
        lines.extend(
            [
                "Si no hay vistas involucradas, escribe NONE dentro del bloque.",
                "",
                "REVIEW_LINKS_START",
                (
                    f"{review_base_url.rstrip('/')}/dashboard/ejemplo"
                    if review_base_url
                    else "/dashboard/ejemplo"
                ),
                "REVIEW_LINKS_END",
                "",
                "o",
                "",
                "REVIEW_LINKS_START",
                "NONE",
                "REVIEW_LINKS_END",
            ]
        )

        return "\n".join(lines)

    def _build_opencode_command(
        self,
        base_command: list[str],
        prompt: str,
        cwd: str | None,
        session_title: str,
    ) -> list[str]:
        return build_run_command(
            base_command=base_command,
            prompt=prompt,
            cwd=cwd,
            title=session_title,
            agent_name="build",
        )

    def _build_kimi_command(
        self,
        base_command: list[str],
        prompt: str,
        cwd: str | None,
    ) -> list[str]:
        if not base_command:
            raise ValueError("Kimi base command is empty")

        command = [base_command[0], "--print", *base_command[1:]]
        if cwd:
            command.extend(["--work-dir", cwd])
        command.extend(["--prompt", prompt])
        return command

    def _resolve_repo_path(self, config: dict[str, Any]) -> str | None:
        repos = config.get("project", {}).get("repos", [])
        if not repos:
            return None
        first = repos[0]
        local_path = first.get("local_path") or first.get("path")
        if not local_path:
            return None
        path = Path(str(local_path))
        return str(path) if path.exists() else None

    def _build_session_title(self, task: dict[str, Any]) -> str:
        parts: list[str] = []
        project = str(task.get("project") or "").strip()
        if project:
            parts.append(project)

        ticket_number = task.get("ticket_number")
        if ticket_number is not None:
            parts.append(f"#{ticket_number}")

        title = str(task.get("title") or "").strip() or "Task"
        parts.append(title)
        session_title = " | ".join(parts)
        return self._truncate(" ".join(session_title.split()), 180)

    @staticmethod
    def _review_base_url(config: dict[str, Any]) -> str | None:
        raw_value = str(config.get("dev", {}).get("review_base_url") or "").strip()
        return raw_value.rstrip("/") if raw_value else None

    def _detect_tests(self, config: dict[str, Any]) -> bool:
        repos = config.get("project", {}).get("repos", [])
        for repo in repos:
            local_path = repo.get("local_path") or repo.get("path")
            if not local_path:
                continue
            root = Path(local_path)
            if not root.exists():
                continue
            markers = ["tests", "pytest.ini", "playwright.config.ts", "package.json"]
            if any((root / marker).exists() for marker in markers):
                return True
        return False

    async def _run_command(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
    ) -> tuple[bool, str]:
        ok, output, err = await self._run_process(
            command,
            stdin_payload=stdin_payload,
            timeout=timeout,
        )
        if not ok:
            return False, self._truncate(err or output or "Unknown command error", 600)

        return True, self._truncate(output or "ok", 600)

    async def _run_process(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str, str]:
        result = await self._run_process_detailed(
            command,
            stdin_payload=stdin_payload,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return result.ok, result.stdout, result.stderr

    async def _run_process_detailed(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "ProcessRunResult":
        try:
            stdin_target = (
                asyncio.subprocess.PIPE
                if stdin_payload is not None
                else asyncio.subprocess.DEVNULL
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=stdin_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, **env} if env else None,
            )
            input_bytes = None if stdin_payload is None else stdin_payload.encode("utf-8")
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ProcessRunResult(False, "", f"Command timeout: {' '.join(command)}", None)
        except FileNotFoundError:
            return ProcessRunResult(False, "", f"Command not found: {command[0]}", None)
        except Exception as exc:
            return ProcessRunResult(False, "", f"Command execution error: {exc}", None)

        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            return ProcessRunResult(False, output, err, process.returncode)

        return ProcessRunResult(True, output or "ok", err, process.returncode)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."

    @classmethod
    def _summarize_failure_output(
        cls,
        stdout: str,
        stderr: str,
        *,
        fallback: str,
        max_len: int = 600,
    ) -> str:
        for candidate in (stderr, stdout, fallback):
            text = (candidate or "").strip()
            if not text:
                continue
            if len(text) <= max_len:
                return text
            return "..." + text[-(max_len - 3):]
        return fallback

    @staticmethod
    def _step(step: str, status: str, detail: str, result: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step": step,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc),
        }
        if result is not None:
            payload["result"] = result
        return payload


@dataclass
class CodingStepResult:
    step: dict[str, Any]
    prompt: str
    summary: str
    error: str | None = None
    session_id: str | None = None
    session_title: str | None = None
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    review_links: ReviewLinksExtraction = field(default_factory=lambda: ReviewLinksExtraction([], False))


@dataclass
class ProcessRunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
