from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .opencode_run import (
    parse_run_output,
    read_session_id,
    write_session_id,
)
from .opencode_models import apply_opencode_model_override
from .opencode_paths import normalize_opencode_command
from mcp_server import build_mcp_provider_env
from rate_monitor import RateMonitor
from settings import Settings
from state import DevProvider

from .tmux_runtime import TmuxRuntime, TmuxTurnResult

_STATUS_FILENAME = ".consensus-status"
_PLAN_FILENAME = "CURRENT_PLAN.md"
_BRIEF_FILENAME = "BRIEF.md"
_CONTEXT_FILENAME = "CONTEXT.md"

_REVIEWER_SYSTEM_PROMPT = """You are one of the most experienced senior software engineers in the world.
You have mass-scale production experience, decades of debugging the hardest systems,
and an obsessive attention to detail. Your reputation is built on finding the problems
that everyone else misses. You do not let anything slide. You do not give superficial reviews.
You treat every review as if the code will run unsupervised in a high-stakes production
environment where failures have real consequences.

Read BRIEF.md for the full context: the project objective, the implementation plan,
and the path to the script under review. Open the script and review it with absolute rigor.

If it is perfect, say it is perfect. If it has problems, tear it apart. Be specific:
identify the exact location, explain the problem, and suggest the fix.
Do not hold back. Do not be polite about bad code.
"""

_OPENCODE_TEAM_ARTIFACT_DIR = ".opencode-team-review"
_OPENCODE_BASE_AGENT = "build"
_OPENCODE_REVIEWERS: dict[str, dict[str, str]] = {
    "reviewer-glm51": {
        "label": "GLM-5.1",
        "model_env": "OPENCODE_GO_MODEL_GLM51",
        "model": "opencode-go/glm-5.1",
        "lens": "Architecture, interface contracts, invariants, data flow, and cross-file consistency.",
        "context_focus": "understand intent, callers, callees, and invariants",
        "specialty": "find mismatched assumptions between modules, weak contracts, broken abstractions, missing invariants, or architectural drift.",
    },
    "reviewer-kimi-k26": {
        "label": "Kimi K2.6",
        "model_env": "OPENCODE_GO_MODEL_KIMI_K26",
        "model": "opencode-go/kimi-k2.6",
        "lens": "Maintainability, readability, developer ergonomics, product behavior, and surprising edge cases.",
        "context_focus": "understand intent, user-visible behavior, and maintenance impact",
        "specialty": "identify confusing flows, brittle APIs, unclear naming, poor UX or developer experience, hidden coupling, and cases future maintainers are likely to break.",
    },
    "reviewer-mimo-v25-pro": {
        "label": "MiMo-V2.5-Pro",
        "model_env": "OPENCODE_GO_MODEL_MIMO_V25_PRO",
        "model": "opencode-go/mimo-v2.5-pro",
        "lens": "Practical implementation quality, regression risk, test gaps, incomplete behavior, and likely failures.",
        "context_focus": "understand intent, execution flow, and likely regression surfaces",
        "specialty": "identify missing tests, incomplete branching, broken integrations, weak validation, upgrade hazards, and code that is likely to regress after small future edits.",
    },
    "reviewer-m27": {
        "label": "MiniMax M2.7",
        "model_env": "OPENCODE_GO_MODEL_MINIMAX_M27",
        "model": "opencode-go/minimax-m2.7",
        "lens": "Production robustness, failure modes, state handling, concurrency or async risks, and performance cliffs.",
        "context_focus": "understand runtime behavior, state transitions, and operational risk",
        "specialty": "identify crash paths, retry or timeout issues, race conditions, resource leaks, observability gaps, unsafe state mutations, and scale-related weaknesses.",
    },
    "reviewer-qwen36-plus": {
        "label": "Qwen3.6 Plus",
        "model_env": "OPENCODE_GO_MODEL_QWEN36_PLUS",
        "model": "opencode-go/qwen3.6-plus",
        "lens": "System integration, API contract fidelity, backward compatibility, and cross-module assumptions.",
        "context_focus": "understand integration boundaries, API contracts, and compatibility constraints",
        "specialty": "find API mismatches, breaking changes, incomplete integrations, protocol violations, and assumptions that break when modules evolve independently.",
    },
    "reviewer-deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "model_env": "OPENCODE_GO_MODEL_DEEPSEEK_V4_PRO",
        "model": "opencode-go/deepseek-v4-pro",
        "lens": "Deep logic, hidden assumptions, subtle edge cases, algorithmic mistakes, and reasoning errors.",
        "context_focus": "understand intent, invariants, and non-obvious assumptions",
        "specialty": "stress the logic mentally, look for contradictory assumptions, invalid invariants, off-by-one behavior, flawed branching, and cases where the code seems plausible but is actually wrong.",
    },
}
_OPENCODE_REVIEW_BATCHES: tuple[tuple[str, ...], ...] = (
    ("reviewer-glm51", "reviewer-kimi-k26"),
    ("reviewer-mimo-v25-pro", "reviewer-m27"),
    ("reviewer-qwen36-plus", "reviewer-deepseek-v4-pro"),
)
_OPENCODE_PARALLEL_REVIEWERS: tuple[str, ...] = ()
_OPENCODE_REVIEWER_PROCESS_TIMEOUT_SECONDS = 600
_OPENCODE_COMPILER_PROCESS_TIMEOUT_SECONDS = 600

_LEAD_STEP_SIGNAL_RE = re.compile(r"^STEP_COMPLETE_(\d+)$")
_LEAD_CORRECTION_SIGNAL_RE = re.compile(r"^CORRECTION_DONE_(\d+)_(\d+)$")
_REVIEW_DONE_SIGNAL_RE = re.compile(r"REVIEW_DONE_(\d+)_(\d+)")


@dataclass
class ConsensusParticipant:
    role: str
    provider: DevProvider
    session_key: str
    tmux_session_name: str


@dataclass
class ConsensusRunResult:
    ok: bool
    summary: str
    last_output: str = ""
    steps_completed: int = 0
    reviews_received: int = 0
    error: str | None = None


class CriticalConsensusOrchestrator:
    """Deterministic tmux orchestration for multi-AI peer review."""

    def __init__(self, settings: Settings, tmux: TmuxRuntime, rate_monitor: RateMonitor | None = None) -> None:
        self._settings = settings
        self._tmux = tmux
        self._rate_monitor = rate_monitor
        self._primed_reviewers: set[str] = set()

    def _opencode_reviewer_model(self, reviewer_name: str) -> str:
        spec = _OPENCODE_REVIEWERS[reviewer_name]
        model_env = spec.get("model_env", "").strip()
        if model_env:
            configured = str(getattr(self._settings, model_env, "") or "").strip()
            if configured:
                return configured
        return spec["model"]

    def _opencode_compiler_model(self) -> str:
        configured = str(self._settings.OPENCODE_GO_MODEL_KIMI_K26 or "").strip()
        return configured or "opencode-go/kimi-k2.6"

    @staticmethod
    def is_enabled(config: dict[str, Any]) -> bool:
        dev_cfg = config.get("dev", {}) or {}
        coding_tool = str(dev_cfg.get("coding_tool", "")).strip().lower()
        tier_s_cfg = CriticalConsensusOrchestrator._consensus_config(dev_cfg)
        return coding_tool in {"tier_s", "critical_consensus"} or bool(tier_s_cfg.get("enabled"))

    async def execute(
        self,
        project: str,
        instruction: str,
        config: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
    ) -> ConsensusRunResult:
        cfg = self._normalized_config(config)
        repo_path = self._resolve_repo_path(config)
        if repo_path is None:
            return ConsensusRunResult(
                ok=False,
                summary="No pude iniciar Tier S porque no encontré el repo local del proyecto.",
                error="Repository path not found",
            )

        status_path = repo_path / _STATUS_FILENAME
        plan_path = repo_path / _PLAN_FILENAME
        brief_path = repo_path / _BRIEF_FILENAME
        feedback_paths = self._feedback_paths(repo_path, cfg["reviewers"])

        # Pre-launch rate limit check for all providers used by Tier S
        if self._rate_monitor is not None:
            for role in [cfg["lead"], *cfg["reviewers"]]:
                provider_name = role.strip().lower()
                if provider_name in ("codex", "claude"):
                    can = await self._rate_monitor.can_launch(provider_name)
                    if not can:
                        status = await self._rate_monitor.get_status(provider_name)
                        return ConsensusRunResult(
                            ok=False,
                            summary=(
                                f"Tier S bloqueado: {provider_name} está en rate limit. "
                                f"Reintenta a las {status.resets_5h_at.strftime('%H:%M UTC')}."
                            ),
                            error=f"rate_limit:{provider_name}",
                        )

        lead, reviewers = await self.ensure_project_sessions(project=project, config=config)

        await progress("Tier S activo. Claude está preparando el plan y el primer script.")
        self._reset_signal_file(status_path)
        lead_turn = await self._send_participant_instruction(
            participant=lead,
            config=config,
            repo_path=repo_path,
            prompt=self._initial_lead_prompt(instruction=instruction, repo_path=repo_path),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: self._read_signal(status_path) == "STEP_COMPLETE_1",
        )
        step_number = self._expect_lead_step_signal(status_path=status_path, turn=lead_turn, expected_step=1)
        if step_number is None:
            return self._lead_failure(
                turn=lead_turn,
                signal_path=status_path,
                summary="Claude no confirmó el primer paso del consenso.",
            )

        total_reviews = 0
        steps_completed = 0

        while True:
            for iteration in range(1, cfg["iterations"] + 1):
                self._clear_feedback_files(feedback_paths)
                await progress(
                    f"Paso {step_number}: reviewers en paralelo, iteración {iteration} "
                    f"({', '.join(p.role for p in reviewers)})."
                )
                review_results = await asyncio.gather(
                    *[
                        self._send_participant_instruction(
                            participant=reviewer,
                            config=config,
                            repo_path=repo_path,
                            prompt=self._reviewer_prompt_for_participant(
                                participant=reviewer,
                                step_number=step_number,
                                iteration=iteration,
                                repo_path=repo_path,
                            ),
                            timeout=cfg["timeout_seconds"],
                            completion_check=lambda reviewer=reviewer.role: self._feedback_file_ready(
                                path=feedback_paths[reviewer],
                                expected_signal=f"REVIEW_DONE_{step_number}_{iteration}",
                            ),
                        )
                        for reviewer in reviewers
                    ],
                    return_exceptions=True,
                )

                feedback_count = self._count_ready_feedbacks(
                    feedback_paths=feedback_paths,
                    step_number=step_number,
                    iteration=iteration,
                )
                total_reviews += feedback_count

                await progress(
                    f"Paso {step_number}, iteración {iteration}: recibí feedback de "
                    f"{feedback_count}/{len(reviewers)} reviewers."
                )

                self._reset_signal_file(status_path)
                correction_turn = await self._send_participant_instruction(
                    participant=lead,
                    config=config,
                    repo_path=repo_path,
                    prompt=self._correction_prompt(
                        reviewers=[p.role for p in reviewers],
                        step_number=step_number,
                        iteration=iteration,
                        repo_path=repo_path,
                    ),
                    timeout=cfg["timeout_seconds"],
                    completion_check=lambda: self._read_signal(status_path)
                    == f"CORRECTION_DONE_{step_number}_{iteration}",
                )
                if not self._expect_correction_signal(
                    status_path=status_path,
                    turn=correction_turn,
                    step_number=step_number,
                    iteration=iteration,
                ):
                    return self._lead_failure(
                        turn=correction_turn,
                        signal_path=status_path,
                        summary=(
                            f"Claude no confirmó la corrección del paso {step_number} "
                            f"en la iteración {iteration}."
                        ),
                    )

            steps_completed = step_number
            await progress(f"Paso {step_number} validado. Claude decidirá si sigue otro script o cierra el ticket.")

            self._reset_signal_file(status_path)
            next_turn = await self._send_participant_instruction(
                participant=lead,
                config=config,
                repo_path=repo_path,
                prompt=self._next_step_prompt(step_number=step_number, repo_path=repo_path),
                timeout=cfg["timeout_seconds"],
                completion_check=lambda: self._is_next_step_resolved(
                    status_path=status_path,
                    step_number=step_number,
                ),
            )
            signal = self._read_signal(status_path)
            if signal == "TICKET_COMPLETE":
                summary = self._build_success_summary(
                    plan_path=plan_path,
                    brief_path=brief_path,
                    steps_completed=steps_completed,
                    total_reviews=total_reviews,
                )
                return ConsensusRunResult(
                    ok=True,
                    summary=summary,
                    last_output=next_turn.output.strip(),
                    steps_completed=steps_completed,
                    reviews_received=total_reviews,
                )

            next_step = self._extract_step_signal(signal)
            if next_step is None:
                return self._lead_failure(
                    turn=next_turn,
                    signal_path=status_path,
                    summary=(
                        f"Claude no indicó si había un siguiente paso después del paso {step_number}."
                    ),
                )

            if next_step <= step_number:
                return ConsensusRunResult(
                    ok=False,
                    summary="Tier S quedó en un estado inconsistente: el siguiente paso no avanzó.",
                    last_output=next_turn.output.strip(),
                    steps_completed=steps_completed,
                    reviews_received=total_reviews,
                    error=f"Non-incremental step signal: {signal}",
                )

            if next_step > cfg["max_steps"]:
                return ConsensusRunResult(
                    ok=False,
                    summary="Tier S excedió el máximo de scripts permitido para un ticket.",
                    last_output=next_turn.output.strip(),
                    steps_completed=steps_completed,
                    reviews_received=total_reviews,
                    error=f"Step limit exceeded: {next_step}>{cfg['max_steps']}",
                )

            step_number = next_step

    async def ensure_project_sessions(
        self,
        project: str,
        config: dict[str, Any],
        wait_ready: bool = True,
        prime_reviewers: bool = True,
    ) -> tuple[ConsensusParticipant, list[ConsensusParticipant]]:
        cfg = self._normalized_config(config)
        participants = [
            self._participant(project=project, role=cfg["lead"]),
            *[self._participant(project=project, role=role) for role in cfg["reviewers"]],
        ]

        for participant in participants:
            await self._ensure_participant_session(
                participant=participant,
                config=config,
                wait_ready=wait_ready,
            )

        reviewers = [participant for participant in participants if participant.role != cfg["lead"]]
        if prime_reviewers:
            repo_path = self._resolve_repo_path(config)
            for reviewer in reviewers:
                if self._is_opencode_team_reviewer(reviewer.role):
                    continue
                if reviewer.session_key in self._primed_reviewers:
                    continue
                prime_turn = await self._send_participant_instruction(
                    participant=reviewer,
                    config=config,
                    repo_path=repo_path,
                    prompt=self._reviewer_prime_prompt(repo_path=repo_path),
                    timeout=120,
                    completion_check=lambda session_key=reviewer.session_key: self._pane_has_text(
                        session_key,
                        "READY_FOR_REVIEW",
                    ),
                )
                if prime_turn.ok and prime_turn.is_ready:
                    self._primed_reviewers.add(reviewer.session_key)

        lead = next(participant for participant in participants if participant.role == cfg["lead"])
        return lead, reviewers

    def close_project(self, project: str, config: dict[str, Any]) -> None:
        cfg = self._normalized_config(config)
        repo_path = self._resolve_repo_path(config)
        for role in [cfg["lead"], *cfg["reviewers"]]:
            participant = self._participant(project=project, role=role)
            self._tmux.close_session(
                participant.session_key,
                tmux_session_name=participant.tmux_session_name,
            )
            self._primed_reviewers.discard(participant.session_key)
            if repo_path is not None and participant.provider == DevProvider.OPENCODE:
                session_id_paths = [
                    self._opencode_session_id_path(repo_path, participant.role),
                    self._opencode_session_id_path(repo_path, "opencode-compiler"),
                    *[
                        self._opencode_session_id_path(repo_path, f"opencode-{name}")
                        for name in _OPENCODE_REVIEWERS
                    ],
                ]
                for session_id_path in session_id_paths:
                    if session_id_path.exists():
                        session_id_path.unlink()

    def get_live_output(self, project: str, config: dict[str, Any]) -> str:
        cfg = self._normalized_config(config)
        lines: list[str] = []
        for role in [cfg["lead"], *cfg["reviewers"]]:
            participant = self._participant(project=project, role=role)
            content = self._tmux.get_current_output(participant.session_key, lines=30).strip()
            if not content:
                continue
            snippet = content.splitlines()[-8:]
            lines.append(f"[{role}]\n" + "\n".join(snippet))
        return "\n\n".join(lines).strip()

    async def _ensure_participant_session(
        self,
        participant: ConsensusParticipant,
        config: dict[str, Any],
        wait_ready: bool,
    ) -> None:
        command, env_vars, cwd = self._resolve_command(participant.provider, config, role=participant.role)
        shell_mode = participant.provider == DevProvider.OPENCODE
        start_command = [] if shell_mode else command
        tracked = self._tmux.register_existing_session(
            project=participant.session_key,
            provider=participant.provider,
            tmux_session_name=participant.tmux_session_name,
            command=start_command,
            cwd=cwd,
            shell_mode=shell_mode,
        )
        if not tracked:
            self._tmux.start_session(
                project=participant.session_key,
                provider=participant.provider,
                command=start_command,
                cwd=cwd,
                env=env_vars,
                tmux_session_name=participant.tmux_session_name,
                shell_mode=shell_mode,
            )
        if wait_ready:
            await self._tmux.wait_until_ready(participant.session_key, timeout=45)

    def _normalized_config(self, config: dict[str, Any]) -> dict[str, Any]:
        dev_cfg = config.get("dev", {}) or {}
        consensus_cfg = self._consensus_config(dev_cfg)
        lead = str(consensus_cfg.get("lead") or "claude").strip().lower() or "claude"
        reviewers = [str(item).strip().lower() for item in (consensus_cfg.get("reviewers") or ["codex", "gemini", "opencode"])]
        reviewers = [item for item in reviewers if item and item != lead]
        if not reviewers:
            reviewers = ["codex"]

        iterations = max(1, int(consensus_cfg.get("iterations", 2)))
        timeout_seconds = max(120, int(consensus_cfg.get("timeout_minutes", 15)) * 60)
        max_steps = max(1, int(consensus_cfg.get("max_steps", 12)))

        return {
            "lead": lead,
            "reviewers": reviewers,
            "iterations": iterations,
            "timeout_seconds": timeout_seconds,
            "max_steps": max_steps,
        }

    @staticmethod
    def _consensus_config(dev_cfg: dict[str, Any]) -> dict[str, Any]:
        tier_s_cfg = dev_cfg.get("tier_s", {}) or {}
        legacy_cfg = dev_cfg.get("critical_consensus", {}) or {}
        if not isinstance(tier_s_cfg, dict):
            tier_s_cfg = {}
        if not isinstance(legacy_cfg, dict):
            legacy_cfg = {}
        return {**legacy_cfg, **tier_s_cfg}

    def _participant(self, project: str, role: str) -> ConsensusParticipant:
        provider = self._provider_from_name(role)
        safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
        safe_role = re.sub(r"[^a-zA-Z0-9_-]", "-", role)
        return ConsensusParticipant(
            role=role,
            provider=provider,
            session_key=f"consensus::{project}::{role}",
            tmux_session_name=f"dev-{safe_project}-{safe_role}",
        )

    def _provider_from_name(self, value: str) -> DevProvider:
        normalized = value.strip().lower()
        if normalized not in DevProvider._value2member_map_:
            raise ValueError(f"Unsupported Tier S provider: {value}")
        return DevProvider(normalized)

    def _resolve_command(
        self,
        provider: DevProvider,
        config: dict[str, Any],
        role: str | None = None,
    ) -> tuple[list[str], dict[str, str], str | None]:
        provider_cfg = (config.get("dev", {}).get("providers", {}) or {}).get(provider.value, {})
        raw_command = provider_cfg.get("command")

        if isinstance(raw_command, list) and raw_command:
            command = [str(token) for token in raw_command]
        elif isinstance(raw_command, str) and raw_command.strip():
            command = shlex.split(raw_command)
        else:
            command = self._default_command(provider)
        if provider == DevProvider.OPENCODE:
            command = apply_opencode_model_override(command, self._settings.OPENCODE_MODEL_OVERRIDE)
            command = normalize_opencode_command(command)

        env_vars = {**build_mcp_provider_env(self._settings)}
        for key, value in (provider_cfg.get("env", {}) or {}).items():
            env_vars[str(key)] = str(value)

        cwd = provider_cfg.get("cwd")
        if cwd is None:
            repo_path = self._resolve_repo_path(config)
            cwd = str(repo_path) if repo_path else None
        elif cwd is not None:
            cwd = str(cwd)

        return command, env_vars, cwd

    def _default_command(self, provider: DevProvider) -> list[str]:
        if provider == DevProvider.CODEX:
            return [self._settings.CODEX_CLI_PATH, *shlex.split(self._settings.CODEX_DEFAULT_ARGS)]
        if provider == DevProvider.CLAUDE:
            return [self._settings.CLAUDE_CODE_PATH, *shlex.split(self._settings.CLAUDE_DEFAULT_ARGS)]
        if provider == DevProvider.GEMINI:
            return [self._settings.GEMINI_CLI_PATH, *shlex.split(self._settings.GEMINI_DEFAULT_ARGS)]
        if provider == DevProvider.OPENCODE:
            return [self._settings.OPENCODE_CLI_PATH, *shlex.split(self._settings.OPENCODE_DEFAULT_ARGS)]
        if provider == DevProvider.GLM:
            return [self._settings.GLM_CLI_PATH, *shlex.split(self._settings.GLM_DEFAULT_ARGS)]
        return [self._settings.CLAUDE_CODE_PATH, *shlex.split(self._settings.CLAUDE_DEFAULT_ARGS)]

    def _resolve_repo_path(self, config: dict[str, Any]) -> Path | None:
        repos = config.get("project", {}).get("repos", []) or []
        if not repos:
            return None
        first_repo = repos[0]
        local_path = first_repo.get("local_path") or first_repo.get("path")
        if not local_path:
            return None
        path = Path(str(local_path))
        return path if path.exists() else None

    def _feedback_paths(self, repo_path: Path, reviewers: list[str]) -> dict[str, Path]:
        return {
            reviewer: repo_path / f"FEEDBACK-{reviewer}.md"
            for reviewer in reviewers
        }

    def _initial_lead_prompt(self, instruction: str, repo_path: Path) -> str:
        return (
            f"Read {_CONTEXT_FILENAME} if it exists.\n"
            f"Ticket: {instruction}\n\n"
            f"Create {_PLAN_FILENAME} with:\n"
            "- The ticket objective\n"
            "- An ordered list of scripts to implement, one per step\n"
            "- For each script: what it does, what it depends on, and what risks you see\n\n"
            "Implement ONLY the first script from the plan.\n\n"
            f"After implementing, generate {_BRIEF_FILENAME} with:\n"
            "- The ticket objective\n"
            f"- Your plan from {_PLAN_FILENAME}\n"
            "- The path to the script you just wrote (path only, do not inline code)\n"
            f"- The relevant parts of {_CONTEXT_FILENAME} that a reviewer needs\n\n"
            f"When everything is ready, overwrite {_STATUS_FILENAME} with exactly STEP_COMPLETE_1 "
            "and print STEP_COMPLETE_1.\n"
            f"Work inside {repo_path}."
        )

    def _reviewer_prime_prompt(self, repo_path: Path | None) -> str:
        suffix = f"\nWork inside {repo_path}." if repo_path else ""
        return (
            _REVIEWER_SYSTEM_PROMPT
            + "\nFrom now on, every review you perform must write the analysis into the requested FEEDBACK file "
            "and end with the exact REVIEW_DONE signal requested for that step.\n"
            "Acknowledge with READY_FOR_REVIEW."
            + suffix
        )

    def _reviewer_prompt(self, reviewer: str, step_number: int, iteration: int, repo_path: Path) -> str:
        return (
            f"Read {_BRIEF_FILENAME} and open the script indicated there. Review it completely.\n"
            f"Write your analysis in FEEDBACK-{reviewer}.md.\n"
            f"When finished, make the last line exactly REVIEW_DONE_{step_number}_{iteration}.\n"
            f"Work inside {repo_path}."
        )

    def _reviewer_prompt_for_participant(
        self,
        participant: ConsensusParticipant,
        step_number: int,
        iteration: int,
        repo_path: Path,
    ) -> str:
        if self._is_opencode_team_reviewer(participant.role):
            return self._opencode_team_reviewer_prompt(
                step_number=step_number,
                iteration=iteration,
                repo_path=repo_path,
            )
        return self._reviewer_prompt(
            reviewer=participant.role,
            step_number=step_number,
            iteration=iteration,
            repo_path=repo_path,
        )

    def _opencode_team_reviewer_prompt(self, step_number: int, iteration: int, repo_path: Path) -> str:
        team_dir = (
            f"{_OPENCODE_TEAM_ARTIFACT_DIR}/step-{step_number:02d}-iteration-{iteration:02d}"
        )
        return (
            "Run the OpenCEO multi-model OpenCode review pipeline for this target.\n"
            f"Read {_BRIEF_FILENAME} and open the script indicated there.\n"
            "Launch 5 independent one-shot reviewers, one per configured OpenCode review model, using the build agent as the runtime shell.\n"
            f"Store intermediate reviewer artifacts under {team_dir}/.\n"
            f"Then run a standalone compiler pass with {self._opencode_compiler_model()} to merge and deduplicate the available "
            "artifacts into FEEDBACK-opencode.md.\n"
            f"The last line of FEEDBACK-opencode.md must be exactly REVIEW_DONE_{step_number}_{iteration}.\n"
            "Only FEEDBACK-opencode.md is consumed by OpenCEO.\n"
            f"Work inside {repo_path}."
        )

    @staticmethod
    def _extract_review_done_signal(prompt: str) -> tuple[int, int, str]:
        match = _REVIEW_DONE_SIGNAL_RE.search(prompt)
        if not match:
            raise ValueError("OpenCode review prompt did not include REVIEW_DONE signal")
        step_number = int(match.group(1))
        iteration = int(match.group(2))
        return step_number, iteration, f"REVIEW_DONE_{step_number}_{iteration}"

    @staticmethod
    def _opencode_artifact_dir(repo_path: Path, step_number: int, iteration: int) -> Path:
        return repo_path / _OPENCODE_TEAM_ARTIFACT_DIR / f"step-{step_number:02d}-iteration-{iteration:02d}"

    @staticmethod
    def _opencode_artifact_path(artifact_dir: Path, reviewer_name: str) -> Path:
        return artifact_dir / f"{reviewer_name}.md"

    def _opencode_reviewer_artifact_prompt(
        self,
        reviewer_name: str,
        reviewer_label: str,
        reviewer_lens: str,
        reviewer_context_focus: str,
        reviewer_specialty: str,
        repo_path: Path,
        artifact_path: Path,
        step_number: int,
        iteration: int,
    ) -> str:
        relative_artifact = artifact_path.relative_to(repo_path)
        return (
            f"You are a code reviewer using {reviewer_label}. "
            "You are running as an independent one-shot reviewer inside OpenCEO Tier S. "
            "Review independently and use your strongest judgment, not just a fixed checklist.\n\n"
            "Suggested strength areas:\n"
            f"- {reviewer_lens}\n\n"
            "For each file or set of files you receive:\n"
            "1. Read BRIEF.md for the context, open the referenced script, and pull enough surrounding context to "
            f"{reviewer_context_focus} before writing the review.\n"
            "2. Follow the most important signals you see. Common areas such as correctness, error handling, edge cases, "
            "security, performance, readability, naming, tests, maintainability, and conventions are starting points, not limits.\n"
            "3. Treat the strength area above as a suggestion, not a scope limit. Use it when helpful, but do not let it override stronger signals elsewhere.\n"
            f"4. One useful direction your model is often good at is: {reviewer_specialty}\n"
            "5. Prefer depth over breadth when one suspicious area deserves deeper analysis.\n"
            "6. If the code feels suspicious, incomplete, inconsistent, or under-specified, investigate that line of review directly instead of waiting for more explicit instructions.\n"
            "7. Create your review file with your findings.\n\n"
            "Review format:\n"
            "- Finding: [severity: critical/warning/suggestion] [exact path]:[line]\n"
            "  - Issue: [finding]\n"
            "  - Why: [explanation]\n"
            "  - Fix: [recommendation]\n"
            "- Cross-file finding: [severity] [paths]\n"
            "  - Issue: [systemic or multi-file problem]\n"
            "  - Why: [explanation]\n"
            "  - Fix: [recommendation]\n"
            "\nRules:\n"
            "- Do not modify source code. You only read and review.\n"
            "- Do not run bash commands.\n"
            "- Be specific: cite exact lines when possible, explain why it matters, and suggest a concrete fix.\n"
            "- If the code is correct and clean, say so explicitly. Do not invent issues.\n"
            "- Focus on real problems, not arbitrary nitpicks.\n"
            "- The checklist is a floor, not a ceiling. Use your own reasoning aggressively.\n"
            f"Write your analysis only to {relative_artifact}.\n"
            f"The last line of {relative_artifact} must be exactly REVIEW_DONE_{step_number}_{iteration}_{reviewer_name}.\n"
            "Do not modify source code. Do not create any other root-level feedback files.\n"
            f"Work inside {repo_path}."
        )

    def _opencode_compiler_prompt(
        self,
        repo_path: Path,
        artifact_paths: dict[str, Path],
        step_number: int,
        iteration: int,
    ) -> str:
        lines = "\n".join(
            f"- {name}: {path.relative_to(repo_path)}"
            for name, path in artifact_paths.items()
        )
        return (
            "This is a standalone compilation pass for OpenCEO Tier S.\n"
            "Do NOT use team_spawn, team_tasks, team_message, team_broadcast, or team_claim.\n"
            f"Read {_BRIEF_FILENAME} and then inspect the review artifact files listed below if they exist:\n"
            f"{lines}\n"
            "Some reviewer files may be missing. Missing files are unavailable reviewers, not blockers.\n"
            "Write a single consolidated FEEDBACK-opencode.md at the repo root.\n"
            "Structure FEEDBACK-opencode.md as:\n"
            "1. Header with reviewer coverage, for example '3/5 reviewers responded'\n"
            "2. Contributing reviewers\n"
            "3. Missing reviewers\n"
            "4. Merged findings ordered by severity: critical, warning, suggestion\n"
            "Deduplicate semantically identical findings and annotate consensus counts like 'found by 3/5 reviewers'.\n"
            "If zero reviewer artifacts exist, write that no reviews were available.\n"
            f"The last line of FEEDBACK-opencode.md must be exactly REVIEW_DONE_{step_number}_{iteration}.\n"
            "Do not modify source code.\n"
            f"Work inside {repo_path}."
        )

    async def _run_opencode_agent_once(
        self,
        agent_name: str | None,
        prompt: str,
        config: dict[str, Any],
        repo_path: Path,
        timeout: int,
        model_name: str | None = None,
        session_id: str | None = None,
    ) -> TmuxTurnResult:
        if not agent_name and not model_name:
            raise ValueError("OpenCode one-shot run requires an agent or model")
        provider_cfg = (config.get("dev", {}).get("providers", {}) or {}).get(DevProvider.OPENCODE.value, {})
        base_command, _env_vars, _cwd = self._resolve_command(DevProvider.OPENCODE, config)
        cli_path = base_command[0] if base_command else self._settings.OPENCODE_CLI_PATH
        env_vars = {**os.environ, **build_mcp_provider_env(self._settings)}
        for key, value in (provider_cfg.get("env", {}) or {}).items():
            env_vars[str(key)] = str(value)

        effective_agent = agent_name or _OPENCODE_BASE_AGENT
        command = [
            cli_path,
            "run",
            "--format",
            "json",
            "--agent",
            effective_agent,
            "--dir",
            str(repo_path),
        ]
        if session_id:
            command.extend(["--session", session_id])
        if model_name:
            command.extend(["--model", model_name])
        command.append(prompt)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo_path),
            env=env_vars,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()

        output = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            output = f"{output}\n{stderr_text}".strip()
        ok = process.returncode == 0 and not timed_out
        return TmuxTurnResult(
            ok=ok,
            output=output.strip(),
            is_ready=ok,
            timed_out=timed_out,
            error=None if ok else f"OpenCode run failed for {agent_name or model_name}",
        )

    async def _run_opencode_compiled_review(
        self,
        config: dict[str, Any],
        repo_path: Path,
        prompt: str,
        timeout: int,
        completion_check: Callable[[], bool] | None,
    ) -> TmuxTurnResult:
        step_number, iteration, signal = self._extract_review_done_signal(prompt)
        artifact_dir = self._opencode_artifact_dir(repo_path, step_number, iteration)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for existing in artifact_dir.glob("*.md"):
            existing.unlink()

        artifact_paths = {
            name: self._opencode_artifact_path(artifact_dir, name)
            for name in _OPENCODE_REVIEWERS
        }

        # Load persisted session IDs for each reviewer + compiler
        reviewer_session_ids: dict[str, str | None] = {}
        for name in _OPENCODE_REVIEWERS:
            sid_path = self._opencode_session_id_path(repo_path, f"opencode-{name}")
            reviewer_session_ids[name] = read_session_id(sid_path)
        compiler_sid_path = self._opencode_session_id_path(repo_path, "opencode-compiler")
        compiler_session_id = read_session_id(compiler_sid_path)

        review_turns_by_name: dict[str, TmuxTurnResult] = {}
        reviewer_timeout_seconds = _OPENCODE_REVIEWER_PROCESS_TIMEOUT_SECONDS
        compiler_timeout_seconds = _OPENCODE_COMPILER_PROCESS_TIMEOUT_SECONDS

        async def _run_named_reviewer(name: str, timeout_seconds: int) -> TmuxTurnResult:
            spec = _OPENCODE_REVIEWERS[name]
            return await self._run_opencode_agent_once(
                agent_name=None,
                prompt=self._opencode_reviewer_artifact_prompt(
                    reviewer_name=name,
                    reviewer_label=spec["label"],
                    reviewer_lens=spec["lens"],
                    reviewer_context_focus=spec["context_focus"],
                    reviewer_specialty=spec["specialty"],
                    repo_path=repo_path,
                    artifact_path=artifact_paths[name],
                    step_number=step_number,
                    iteration=iteration,
                ),
                config=config,
                repo_path=repo_path,
                timeout=timeout_seconds,
                model_name=self._opencode_reviewer_model(name),
                session_id=reviewer_session_ids.get(name),
            )

        deepseek_tasks: dict[str, asyncio.Task[TmuxTurnResult]] = {}
        for name in _OPENCODE_PARALLEL_REVIEWERS:
            deepseek_tasks[name] = asyncio.create_task(
                _run_named_reviewer(name, reviewer_timeout_seconds)
            )

        for batch in _OPENCODE_REVIEW_BATCHES:
            batch_turns = await asyncio.gather(
                *[
                    _run_named_reviewer(name, reviewer_timeout_seconds)
                    for name in batch
                ]
            )
            for name, turn in zip(batch, batch_turns, strict=False):
                review_turns_by_name[name] = turn

        for name, task in deepseek_tasks.items():
            review_turns_by_name[name] = await task

        review_turns = [review_turns_by_name[name] for name in _OPENCODE_REVIEWERS]

        # Persist session IDs from reviewer outputs
        for name, turn in zip(_OPENCODE_REVIEWERS, review_turns, strict=False):
            parsed = parse_run_output(turn.output)
            if parsed.session_id:
                sid_path = self._opencode_session_id_path(repo_path, f"opencode-{name}")
                write_session_id(sid_path, parsed.session_id)

        final_feedback = repo_path / "FEEDBACK-opencode.md"
        compiler_turn = await self._run_opencode_agent_once(
            agent_name=None,
            prompt=self._opencode_compiler_prompt(
                repo_path=repo_path,
                artifact_paths=artifact_paths,
                step_number=step_number,
                iteration=iteration,
            ),
            config=config,
            repo_path=repo_path,
            timeout=compiler_timeout_seconds,
            model_name=self._opencode_compiler_model(),
            session_id=compiler_session_id,
        )

        # Persist compiler session ID
        compiler_parsed = parse_run_output(compiler_turn.output)
        if compiler_parsed.session_id:
            write_session_id(compiler_sid_path, compiler_parsed.session_id)

        final_ready = self._feedback_file_ready(final_feedback, signal)
        if final_ready:
            for artifact_path in artifact_paths.values():
                if artifact_path.exists():
                    artifact_path.unlink()
            if artifact_dir.exists():
                try:
                    artifact_dir.rmdir()
                except OSError:
                    pass
                try:
                    artifact_dir.parent.rmdir()
                except OSError:
                    pass

        snippets: list[str] = []
        for name, turn in zip(_OPENCODE_REVIEWERS, review_turns, strict=False):
            parsed = parse_run_output(turn.output)
            summary = parsed.final_text or parsed.raw_text or turn.output or "no output"
            snippets.append(f"[{name}] {self._compact(summary, 220)}")
        compiler_parsed = parse_run_output(compiler_turn.output)
        compiler_summary = compiler_parsed.final_text or compiler_parsed.raw_text or compiler_turn.output or "no output"
        snippets.append(f"[compiler:{self._opencode_compiler_model()}] {self._compact(compiler_summary, 260)}")

        if completion_check is not None:
            final_ready = bool(completion_check())

        return TmuxTurnResult(
            ok=final_ready,
            output="\n".join(snippets).strip(),
            is_ready=final_ready,
            timed_out=not final_ready,
            error=None if final_ready else "OpenCode compiled review did not produce FEEDBACK-opencode.md",
        )

    def _correction_prompt(
        self,
        reviewers: list[str],
        step_number: int,
        iteration: int,
        repo_path: Path,
    ) -> str:
        feedback_list = ", ".join(f"FEEDBACK-{reviewer}.md" for reviewer in reviewers)
        return (
            f"Reviewers finished for step {step_number}, iteration {iteration}. "
            f"Read {feedback_list}.\n"
            "Correct what you consider valid in the current script and update BRIEF.md if the review requires it.\n"
            f"When done, overwrite {_STATUS_FILENAME} with exactly CORRECTION_DONE_{step_number}_{iteration} "
            f"and print CORRECTION_DONE_{step_number}_{iteration}.\n"
            f"Work inside {repo_path}."
        )

    def _next_step_prompt(self, step_number: int, repo_path: Path) -> str:
        next_step = step_number + 1
        return (
            f"Step {step_number} completed with the required review iterations.\n"
            f"Update {_PLAN_FILENAME} marking step {step_number} as done.\n"
            f"If there is another script to implement, build the next one, regenerate {_BRIEF_FILENAME}, "
            f"overwrite {_STATUS_FILENAME} with exactly STEP_COMPLETE_{next_step}, and print STEP_COMPLETE_{next_step}.\n"
            f"If there are no more scripts, update {_CONTEXT_FILENAME}, overwrite {_STATUS_FILENAME} with exactly "
            "TICKET_COMPLETE, and print TICKET_COMPLETE.\n"
            f"Work inside {repo_path}."
        )

    def _reset_signal_file(self, path: Path) -> None:
        path.write_text("", encoding="utf-8")

    def _read_signal(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()

    def _extract_step_signal(self, signal: str) -> int | None:
        match = _LEAD_STEP_SIGNAL_RE.match(signal.strip())
        if not match:
            return None
        return int(match.group(1))

    def _expect_lead_step_signal(
        self,
        status_path: Path,
        turn: TmuxTurnResult,
        expected_step: int,
    ) -> int | None:
        signal = self._read_signal(status_path)
        step_number = self._extract_step_signal(signal)
        if step_number == expected_step:
            return step_number
        if turn.timed_out and step_number == expected_step:
            return step_number
        return None

    def _expect_correction_signal(
        self,
        status_path: Path,
        turn: TmuxTurnResult,
        step_number: int,
        iteration: int,
    ) -> bool:
        signal = self._read_signal(status_path)
        match = _LEAD_CORRECTION_SIGNAL_RE.match(signal.strip())
        if not match:
            return False
        return int(match.group(1)) == step_number and int(match.group(2)) == iteration

    def _clear_feedback_files(self, feedback_paths: dict[str, Path]) -> None:
        for path in feedback_paths.values():
            if path.exists():
                path.unlink()

    def _count_ready_feedbacks(
        self,
        feedback_paths: dict[str, Path],
        step_number: int,
        iteration: int,
    ) -> int:
        ready = 0
        expected = f"REVIEW_DONE_{step_number}_{iteration}"
        for path in feedback_paths.values():
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content.endswith(expected):
                ready += 1
        return ready

    def _feedback_file_ready(self, path: Path, expected_signal: str) -> bool:
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        return content.endswith(expected_signal)

    def _is_next_step_resolved(self, status_path: Path, step_number: int) -> bool:
        signal = self._read_signal(status_path)
        if signal == "TICKET_COMPLETE":
            return True
        return signal == f"STEP_COMPLETE_{step_number + 1}"

    def _pane_has_text(self, session_key: str, needle: str) -> bool:
        content = self._tmux.get_current_output(session_key, lines=40)
        return needle in content

    async def _send_participant_instruction(
        self,
        participant: ConsensusParticipant,
        config: dict[str, Any],
        repo_path: Path | None,
        prompt: str,
        timeout: int,
        completion_check: Callable[[], bool] | None = None,
    ) -> TmuxTurnResult:
        if participant.provider != DevProvider.OPENCODE:
            return await self._tmux.send_instruction(
                participant.session_key,
                prompt,
                timeout=timeout,
                completion_check=completion_check,
            )

        if repo_path is None:
            return TmuxTurnResult(
                ok=False,
                output="",
                is_ready=False,
                error="Repository path not found for OpenCode reviewer",
            )

        return await self._run_opencode_compiled_review(
            config=config,
            repo_path=repo_path,
            prompt=prompt,
            timeout=timeout,
            completion_check=completion_check,
        )

    @staticmethod
    def _is_opencode_team_reviewer(role: str | None) -> bool:
        return (role or "").strip().lower() == DevProvider.OPENCODE.value

    @staticmethod
    def _opencode_session_id_path(repo_path: Path, role: str) -> Path:
        safe_role = re.sub(r"[^a-zA-Z0-9_-]", "-", role.strip().lower()) or "opencode"
        return repo_path / f".opencode-session-{safe_role}.id"

    def _build_success_summary(
        self,
        plan_path: Path,
        brief_path: Path,
        steps_completed: int,
        total_reviews: int,
    ) -> str:
        plan_excerpt = ""
        if plan_path.exists():
            plan_excerpt = self._compact(plan_path.read_text(encoding="utf-8", errors="replace"), 260)
        elif brief_path.exists():
            plan_excerpt = self._compact(brief_path.read_text(encoding="utf-8", errors="replace"), 260)

        summary = (
            f"Tier S completado: {steps_completed} paso(s), {total_reviews} review(s) recibidos. "
            "Claude actualizó el plan y el contexto del proyecto."
        )
        if plan_excerpt:
            summary += f" Plan actual: {plan_excerpt}"
        return summary

    def _lead_failure(
        self,
        turn: TmuxTurnResult,
        signal_path: Path,
        summary: str,
    ) -> ConsensusRunResult:
        signal = self._read_signal(signal_path)
        detail = signal or turn.error or turn.output.strip()[-1200:]
        return ConsensusRunResult(
            ok=False,
            summary=summary,
            last_output=turn.output.strip(),
            error=detail or "Unknown Tier S failure",
        )

    @staticmethod
    def _compact(text: str, max_len: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= max_len:
            return normalized
        return normalized[: max_len - 3] + "..."
