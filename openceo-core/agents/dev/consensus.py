from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp_server import build_mcp_provider_env
from rate_monitor import RateMonitor
from settings import Settings
from state import DevProvider

from .opencode_models import apply_opencode_model_override
from .opencode_paths import normalize_opencode_command
from .opencode_run import build_run_command
from .tmux_runtime import TmuxRuntime, TmuxTurnResult

logger = logging.getLogger(__name__)

# === Tier S file conventions ===
_TIER_S_DIR = ".tier-s"
_STATUS_FILENAME = f"{_TIER_S_DIR}/status"
_PLAN_FILENAME = f"{_TIER_S_DIR}/CURRENT_PLAN.md"
_CONTEXT_FILENAME = "CONTEXT.md"  # repo root

_TOTAL_PLANS_HEADER_RE = re.compile(r"^TOTAL_PLANS:\s*(\d+)\s*$", re.M)
_PLAN_READY_RE = re.compile(r"^PLAN_READY_(\d+)$")

_ROLE_PLANNER = "planner"
_ROLE_PLAN_REVIEWER = "plan-reviewer"
_ROLE_BUILDER = "builder"
_ROLE_GEMINI_REVIEWER = "gemini-reviewer"

# (reviewer_label, session_role) — the session_role tells us which tmux session to reuse
_REVIEW_LABELS: tuple[tuple[str, str], ...] = (
    ("clauded", _ROLE_PLANNER),
    ("codexf", _ROLE_PLAN_REVIEWER),
    ("gemini", _ROLE_GEMINI_REVIEWER),
)

# OpenCode one-shot reviewers (no tmux). (label, opencode model slug)
# Default set; override via dev.tier_s.opencode_reviewers in project.yaml.
# An explicit empty list in config disables OpenCode reviewers.
_DEFAULT_OPENCODE_REVIEWERS: tuple[tuple[str, str], ...] = (
    ("kimi26", "opencode-go/kimi-k2.6"),
    ("qwen36", "opencode-go/qwen3.6-plus"),
)


@dataclass
class _TierSPaths:
    dir: Path
    status_path: Path
    plan_path: Path

    def build_summary(self, p: int) -> Path:
        return self.dir / f"BUILD_SUMMARY_{p}.md"

    def review(self, reviewer: str, p: int, round_: int) -> Path:
        return self.dir / f"REVIEW-{reviewer}-{p}-{round_}.md"


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
    """Deterministic tmux orchestration — Planner → Plan Reviewer → Builder → 3 Reviewers (2 rounds)."""

    def __init__(self, settings: Settings, tmux: TmuxRuntime, rate_monitor: RateMonitor | None = None) -> None:
        self._settings = settings
        self._tmux = tmux
        self._rate_monitor = rate_monitor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def is_enabled(config: dict[str, Any]) -> bool:
        dev_cfg = config.get("dev", {}) or {}
        return str(dev_cfg.get("coding_tool", "")).strip().lower() == "tier_s"

    async def execute(
        self,
        project: str,
        instruction: str,
        config: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        task_id: str | None = None,
        is_spillover: bool = False,
    ) -> ConsensusRunResult:
        try:
            return await self._execute_impl(project, instruction, config, progress, task_id=task_id, is_spillover=is_spillover)
        except Exception as exc:
            logger.exception("Tier S execute() raised unhandled exception")
            return ConsensusRunResult(
                ok=False,
                summary=f"Tier S falló con un error inesperado: {exc}",
                error=str(exc)[:2000],
            )

    async def _execute_impl(
        self,
        project: str,
        instruction: str,
        config: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        task_id: str | None = None,
        is_spillover: bool = False,
    ) -> ConsensusRunResult:
        cfg = self._normalized_config(config)
        repo_path = self._resolve_repo_path(config)
        if repo_path is None:
            return ConsensusRunResult(
                ok=False,
                summary="No pude iniciar Tier S porque no encontré el repo local del proyecto.",
                error="Repository path not found",
            )

        paths = self._tier_s_paths(repo_path)
        paths.dir.mkdir(parents=True, exist_ok=True)

        # Pre-launch rate-limit check for claude/codex providers used by roles
        if self._rate_monitor is not None:
            providers_seen: set[str] = set()
            for role_cfg in cfg["roles"].values():
                provider_name = str(role_cfg.get("provider", "")).strip().lower()
                if provider_name in ("codex", "claude") and provider_name not in providers_seen:
                    providers_seen.add(provider_name)
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

        planner, others = await self.ensure_project_sessions(project=project, config=config)
        sessions: dict[str, ConsensusParticipant] = {
            _ROLE_PLANNER: planner,
            _ROLE_PLAN_REVIEWER: others[0],
            _ROLE_BUILDER: others[1],
            _ROLE_GEMINI_REVIEWER: others[2],
        }

        await progress("Tier S activo. Planner diseñando el plan de implementación.")
        n_plans, planner_turn = await self._run_planner(sessions, instruction, paths, cfg, progress, task_id=task_id, is_spillover=is_spillover)
        if n_plans is None:
            return self._lead_failure(
                turn=planner_turn,
                signal_path=paths.status_path,
                summary="Planner no confirmó PLAN_READY_N. Tier S no puede continuar.",
            )

        await progress("Plan Reviewer revisando y refinando el plan.")
        await self._run_plan_reviewer(sessions, paths, cfg, progress)
        n_plans = self._reread_total_plans(paths.plan_path) or n_plans
        if n_plans < 1 or n_plans > cfg["max_plans"]:
            return ConsensusRunResult(
                ok=False,
                summary=f"Plan inválido: {n_plans} sub-planes (debe ser 1-{cfg['max_plans']}).",
                error=f"Invalid plan count: {n_plans}",
            )

        total_reviews = 0
        for p in range(1, n_plans + 1):
            await progress(f"Builder implementando sub-plan {p}/{n_plans}.")
            build_turn = await self._run_builder(sessions, p, paths, cfg, progress)
            if not build_turn.ok:
                return self._lead_failure(
                    turn=build_turn,
                    signal_path=paths.status_path,
                    summary=f"Builder falló en sub-plan {p}.",
                )

            for r in range(1, cfg["review_rounds"] + 1):
                await progress(f"Review round {r}/{cfg['review_rounds']} para sub-plan {p}.")
                reviews_done = await self._run_review_round(sessions, p, r, paths, cfg, progress, config)
                total_reviews += reviews_done

                await progress(f"Builder aplicando fixes del round {r} para sub-plan {p}.")
                fix_turn = await self._run_builder_fix(sessions, p, r, paths, cfg, progress)
                if not fix_turn.ok:
                    return self._lead_failure(
                        turn=fix_turn,
                        signal_path=paths.status_path,
                        summary=f"Builder no aplicó los fixes del round {r} para sub-plan {p}.",
                    )

        await progress("Finalizando ticket.")
        finalize_turn = await self._run_finalize(sessions, paths, cfg, progress)
        finalize_signal = self._read_signal(paths.status_path)
        if finalize_signal != "TICKET_COMPLETE":
            return ConsensusRunResult(
                ok=False,
                summary="Tier S finalizó con warning: el planner no marcó TICKET_COMPLETE. Revisar manualmente.",
                last_output=finalize_turn.output.strip(),
                steps_completed=n_plans,
                reviews_received=total_reviews,
                error=f"Finalize signal mismatch: {finalize_signal!r}",
            )

        return ConsensusRunResult(
            ok=True,
            summary=self._build_success_summary(
                plan_path=paths.plan_path,
                brief_path=paths.plan_path,
                steps_completed=n_plans,
                total_reviews=total_reviews,
            ),
            last_output=finalize_turn.output.strip(),
            steps_completed=n_plans,
            reviews_received=total_reviews,
        )

    async def ensure_project_sessions(
        self,
        project: str,
        config: dict[str, Any],
        wait_ready: bool = True,
        prime_reviewers: bool = True,
    ) -> tuple[ConsensusParticipant, list[ConsensusParticipant]]:
        cfg = self._normalized_config(config)

        def _make(role: str) -> ConsensusParticipant:
            role_cfg = cfg["roles"].get(role, {})
            provider_name = str(role_cfg.get("provider", "")).strip().lower() or role
            if provider_name not in DevProvider._value2member_map_:
                raise ValueError(f"Unsupported Tier S provider for {role}: {provider_name}")
            provider = DevProvider(provider_name)
            safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
            safe_role = re.sub(r"[^a-zA-Z0-9_-]", "-", role)
            return ConsensusParticipant(
                role=role,
                provider=provider,
                session_key=f"tier_s::{project}::{role}",
                tmux_session_name=f"dev-{safe_project}-{safe_role}",
            )

        planner = _make(_ROLE_PLANNER)
        plan_reviewer = _make(_ROLE_PLAN_REVIEWER)
        builder = _make(_ROLE_BUILDER)
        gemini_reviewer = _make(_ROLE_GEMINI_REVIEWER)

        for participant in [planner, plan_reviewer, builder, gemini_reviewer]:
            await self._ensure_participant_session(
                participant=participant,
                config=config,
                wait_ready=wait_ready,
            )

        return planner, [plan_reviewer, builder, gemini_reviewer]

    def close_project(self, project: str, config: dict[str, Any]) -> None:
        for role in [_ROLE_PLANNER, _ROLE_PLAN_REVIEWER, _ROLE_BUILDER, _ROLE_GEMINI_REVIEWER]:
            safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
            safe_role = re.sub(r"[^a-zA-Z0-9_-]", "-", role)
            self._tmux.close_session(
                project=f"tier_s::{project}::{role}",
                tmux_session_name=f"dev-{safe_project}-{safe_role}",
            )

    def get_live_output(self, project: str, config: dict[str, Any]) -> str:
        lines: list[str] = []
        for role in [_ROLE_PLANNER, _ROLE_PLAN_REVIEWER, _ROLE_BUILDER, _ROLE_GEMINI_REVIEWER]:
            session_key = f"tier_s::{project}::{role}"
            content = self._tmux.get_current_output(session_key, lines=30).strip()
            if not content:
                continue
            snippet = content.splitlines()[-8:]
            lines.append(f"[{role}]\n" + "\n".join(snippet))
        return "\n\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Session infrastructure
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Config normalization
    # ------------------------------------------------------------------

    def _normalized_config(self, config: dict[str, Any]) -> dict[str, Any]:
        dev_cfg = config.get("dev", {}) or {}
        tier_s_cfg = dev_cfg.get("tier_s", {}) or {}
        if not isinstance(tier_s_cfg, dict):
            tier_s_cfg = {}

        review_rounds = max(1, int(tier_s_cfg.get("review_rounds", 2)))
        max_plans = max(1, int(tier_s_cfg.get("max_plans", 15)))
        timeout_seconds = max(120, int(tier_s_cfg.get("timeout_minutes", 15)) * 60)

        raw_roles = tier_s_cfg.get("roles", {}) or {}
        roles: dict[str, dict[str, Any]] = {}
        for role_name in [_ROLE_PLANNER, _ROLE_PLAN_REVIEWER, _ROLE_BUILDER, _ROLE_GEMINI_REVIEWER]:
            role_cfg = raw_roles.get(role_name, {}) if isinstance(raw_roles, dict) else {}
            if not isinstance(role_cfg, dict):
                role_cfg = {}
            roles[role_name] = {
                "provider": str(role_cfg.get("provider", "")).strip().lower() or self._default_provider_for_role(role_name),
                "command": role_cfg.get("command") or [],
                "env": role_cfg.get("env", {}) or {},
            }

        raw_oc = tier_s_cfg.get("opencode_reviewers", None)
        if raw_oc is None:
            opencode_reviewers = list(_DEFAULT_OPENCODE_REVIEWERS)
        elif isinstance(raw_oc, list):
            opencode_reviewers = []
            for item in raw_oc:
                if isinstance(item, dict):
                    label = str(item.get("label", "")).strip()
                    model = str(item.get("model", "")).strip()
                    if label and model:
                        opencode_reviewers.append((label, model))
        else:
            opencode_reviewers = list(_DEFAULT_OPENCODE_REVIEWERS)

        return {
            "roles": roles,
            "review_rounds": review_rounds,
            "max_plans": max_plans,
            "timeout_seconds": timeout_seconds,
            "opencode_reviewers": opencode_reviewers,
        }

    @staticmethod
    def _default_provider_for_role(role: str) -> str:
        if role in (_ROLE_PLANNER, _ROLE_BUILDER):
            return "claude"
        if role == _ROLE_PLAN_REVIEWER:
            return "codex"
        if role == _ROLE_GEMINI_REVIEWER:
            return "gemini"
        return "claude"

    # ------------------------------------------------------------------
    # Command resolution (role-first, then provider)
    # ------------------------------------------------------------------

    def _resolve_command(
        self,
        provider: DevProvider,
        config: dict[str, Any],
        role: str | None = None,
    ) -> tuple[list[str], dict[str, str], str | None]:
        dev_cfg = config.get("dev", {}) or {}
        provider_cfg = (dev_cfg.get("providers", {}) or {}).get(provider.value, {}) or {}

        # Role-specific overrides from dev.tier_s.roles.{role}
        role_cfg: dict[str, Any] = {}
        if role:
            roles_cfg = (dev_cfg.get("tier_s", {}) or {}).get("roles", {}) or {}
            role_cfg = roles_cfg.get(role, {}) or {}
            if not isinstance(role_cfg, dict):
                role_cfg = {}

        raw_command = role_cfg.get("command") or provider_cfg.get("command")
        if isinstance(raw_command, list) and raw_command:
            command = [str(token) for token in raw_command]
        elif isinstance(raw_command, str) and raw_command.strip():
            command = shlex.split(raw_command)
        else:
            command = self._default_command(provider)

        # Env: mcp + provider env + role env (role overrides provider)
        env_vars = {**build_mcp_provider_env(self._settings)}
        for key, value in (provider_cfg.get("env", {}) or {}).items():
            env_vars[str(key)] = str(value)
        for key, value in (role_cfg.get("env", {}) or {}).items():
            env_vars[str(key)] = str(value)

        # cwd: role takes priority
        cwd = role_cfg.get("cwd") or provider_cfg.get("cwd")
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

    # ------------------------------------------------------------------
    # Phase A — Planner
    # ------------------------------------------------------------------

    async def _run_planner(
        self,
        sessions: dict[str, ConsensusParticipant],
        instruction: str,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        task_id: str | None = None,
        is_spillover: bool = False,
    ) -> tuple[int | None, TmuxTurnResult]:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_PLANNER],
            config=None,
            repo_path=None,
            prompt=self._planner_prompt(instruction, cfg["max_plans"], paths, task_id=task_id, is_spillover=is_spillover),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: bool(_PLAN_READY_RE.match(self._read_signal(paths.status_path))),
        )
        signal = self._read_signal(paths.status_path)
        match = _PLAN_READY_RE.match(signal)
        if match:
            return int(match.group(1)), turn
        return None, turn

    def _planner_prompt(self, instruction: str, max_plans: int, paths: _TierSPaths,
                        task_id: str | None = None, is_spillover: bool = False) -> str:
        spillover_block = ""
        if not is_spillover:
            spillover_block = (
                f"\n## Spillover (overflow)\n"
                f"If the ticket is so large that you would need MORE than {max_plans} sub-plans:\n"
                f"- Do NOT fragment further. Use the MCP tool `openceo_create_pm_tickets_batch` with:\n"
                f"  - project: (this project)\n"
                f"  - source_id: \"{task_id or 'UNKNOWN'}\"\n"
                f"  - source: \"spillover\"\n"
                f"  - tickets: JSON array of derived sub-tickets, each self-contained\n"
                f"    (include enough context from the original so each runs alone in a new Tier S cycle)\n"
                f"- In CURRENT_PLAN.md, list the derived ticket IDs in a final '## Derived (spillover)' section\n"
                f"  so we have traceability.\n"
                f"- Keep this ticket's TOTAL_PLANS to the largest atomic chunk that fits in {max_plans}.\n"
                f"- If the MCP call fails, retry once. If it fails again, document the failure in CURRENT_PLAN.md\n"
                f"  and continue fragmenting up to {max_plans} as best as possible.\n"
            )
        else:
            spillover_block = (
                f"\n## Anti-loop\n"
                f"This ticket is itself a spillover from a parent ticket. "
                f"DO NOT create further spillover tickets — fragment your work into <= {max_plans} sub-plans only.\n"
            )

        return (
            f"Read {_CONTEXT_FILENAME} if it exists at the repo root for project context.\n"
            f"Ticket: {instruction}\n\n"
            f"Create {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]} with this exact format:\n\n"
            f"TOTAL_PLANS: N\n\n"
            f"## Sub-Plan 1\n"
            f"- Objective: ...\n"
            f"- Files to touch: ...\n"
            f"- Dependencies: ...\n"
            f"- Success criteria: ...\n\n"
            f"## Sub-Plan 2\n"
            f"...\n\n"
            f"Rules:\n"
            f"- 1 <= N <= {max_plans}. Choose the minimum number that fully solves the ticket.\n"
            f"- Each sub-plan must be independently implementable and reviewable.\n"
            f"- Be specific about files, not vague.\n"
            f"- Order sub-plans so dependencies are satisfied left-to-right.\n"
            f"{spillover_block}\n"
            f"When done, overwrite {_TIER_S_DIR}/status with exactly PLAN_READY_N (where N is your chosen number) and print it.\n"
            f"Work inside {paths.dir.parent}."
        )

    # ------------------------------------------------------------------
    # Phase B — Plan Reviewer
    # ------------------------------------------------------------------

    async def _run_plan_reviewer(
        self,
        sessions: dict[str, ConsensusParticipant],
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
    ) -> None:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_PLAN_REVIEWER],
            config=None,
            repo_path=None,
            prompt=self._plan_reviewer_prompt(paths),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: self._read_signal(paths.status_path) == "PLAN_REVIEWED",
        )
        signal = self._read_signal(paths.status_path)
        if signal != "PLAN_REVIEWED":
            await progress("Plan reviewer no firmó PLAN_REVIEWED — sigo con el plan original.")
            if not turn.ok:
                logger.warning("Plan reviewer failed. Status: %r, turn error: %s", signal, turn.error)

    def _plan_reviewer_prompt(self, paths: _TierSPaths) -> str:
        return (
            f"Read {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]} carefully. You are the Plan Reviewer.\n\n"
            f"Edit the file inline if you find issues:\n"
            f"- Missing steps? Add them and update TOTAL_PLANS.\n"
            f"- Wrong order? Reorder steps.\n"
            f"- Too many or too few sub-plans? Adjust TOTAL_PLANS: N.\n"
            f"- Unclear objectives or missing files? Clarify them.\n"
            f"- Missing dependencies or success criteria? Add them.\n\n"
            f"Do NOT re-prompt the planner. You are the final authority on the plan.\n"
            f"Make your edits directly in the file.\n\n"
            f"When done, overwrite {_TIER_S_DIR}/status with exactly PLAN_REVIEWED and print it.\n"
            f"Work inside {paths.dir.parent}."
        )

    def _reread_total_plans(self, plan_path: Path) -> int | None:
        if not plan_path.exists():
            return None
        text = plan_path.read_text(encoding="utf-8", errors="replace")
        match = _TOTAL_PLANS_HEADER_RE.search(text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Phase C — Builder
    # ------------------------------------------------------------------

    async def _run_builder(
        self,
        sessions: dict[str, ConsensusParticipant],
        p: int,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
    ) -> TmuxTurnResult:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_BUILDER],
            config=None,
            repo_path=None,
            prompt=self._builder_prompt(p, paths),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: (
                self._read_signal(paths.status_path) == f"BUILD_DONE_{p}"
                and paths.build_summary(p).exists()
            ),
        )
        return turn

    def _builder_prompt(self, p: int, paths: _TierSPaths) -> str:
        return (
            f"Implement ONLY sub-plan {p} from {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]}.\n\n"
            f"1. Read {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]} and understand sub-plan {p}.\n"
            f"2. Implement the changes exactly as specified in the sub-plan.\n"
            f"3. Write {_TIER_S_DIR}/BUILD_SUMMARY_{p}.md with:\n"
            f"   - Files modified (full paths relative to repo root)\n"
            f"   - Key decisions made\n"
            f"   - Known limitations\n"
            f"   - Tests run or needed\n"
            f"4. Overwrite {_TIER_S_DIR}/status with exactly BUILD_DONE_{p} and print it.\n\n"
            f"Do NOT touch other sub-plans.\n"
            f"Work inside {paths.dir.parent}."
        )

    # ------------------------------------------------------------------
    # Phase D — Code Reviewers (parallel) + Builder Fixes
    # ------------------------------------------------------------------

    async def _run_review_round(
        self,
        sessions: dict[str, ConsensusParticipant],
        p: int,
        round_: int,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        config: dict[str, Any] | None = None,
    ) -> int:
        # Delete any stale review files for this round
        for reviewer_label, _session_role in _REVIEW_LABELS:
            rpath = paths.review(reviewer_label, p, round_)
            if rpath.exists():
                rpath.unlink()
        for label, _model in cfg["opencode_reviewers"]:
            rpath = paths.review(label, p, round_)
            if rpath.exists():
                rpath.unlink()

        async def _one_review(reviewer_label: str, session_role: str) -> bool:
            rpath = paths.review(reviewer_label, p, round_)
            expected_signal = f"REVIEW_DONE_{p}_{round_}_{reviewer_label}"
            try:
                turn = await self._send_participant_instruction(
                    participant=sessions[session_role],
                    config=None,
                    repo_path=None,
                    prompt=self._code_reviewer_prompt(reviewer_label, p, round_, paths),
                    timeout=cfg["timeout_seconds"],
                    completion_check=lambda rp=rpath, es=expected_signal: self._review_file_done(rp, es),
                )
                return turn.ok
            except Exception:
                logger.warning("Reviewer %s round %d sub-plan %d failed", reviewer_label, round_, p, exc_info=True)
                return False

        tmux_tasks = [
            _one_review(reviewer_label, session_role)
            for reviewer_label, session_role in _REVIEW_LABELS
        ]

        oc_reviewers = cfg["opencode_reviewers"]
        api_key = self._settings.OPENCODE_GO_API_KEY or os.environ.get("OPENCODE_GO_API_KEY", "")
        if oc_reviewers and not api_key:
            logger.warning("OpenCode reviewers configured but OPENCODE_GO_API_KEY is empty; skipping them")
            oc_reviewers = []
        oc_tasks = [
            self._one_opencode_review(label, model, p, round_, paths, cfg, config or {})
            for label, model in oc_reviewers
        ]

        results = await asyncio.gather(*tmux_tasks, *oc_tasks, return_exceptions=True)
        ok_count = sum(1 for r in results if r is True)
        expected = len(_REVIEW_LABELS) + len(oc_reviewers)
        if ok_count < expected:
            logger.warning("Review round %d sub-plan %d: %d/%d reviewers completed", round_, p, ok_count, expected)
        return ok_count

    def _code_reviewer_prompt(self, reviewer_label: str, p: int, round_: int, paths: _TierSPaths) -> str:
        return (
            f"You are a senior code reviewer ({reviewer_label}) in round {round_} for sub-plan {p}.\n\n"
            f"1. Read {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]} and understand sub-plan {p}.\n"
            f"2. Read {_TIER_S_DIR}/BUILD_SUMMARY_{p}.md for implementation context — use its 'Files modified' "
            f"section as the file list to review.\n"
            f"3. Open and review the actual code changes with absolute rigor.\n"
            f"4. Write your findings to {_TIER_S_DIR}/REVIEW-{reviewer_label}-{p}-{round_}.md.\n\n"
            f"Review format:\n"
            f"- Finding: [severity: critical/warning/suggestion] [path]:[line]\n"
            f"  - Issue: [description]\n"
            f"  - Why: [explanation]\n"
            f"  - Fix: [recommendation]\n\n"
            f"Rules:\n"
            f"- Do not modify source code. You only read and review.\n"
            f"- Be specific: cite exact lines, explain why it matters, suggest concrete fixes.\n"
            f"- If the code is correct and clean, say so explicitly. Do not invent issues.\n"
            f"- Focus on real problems: correctness, edge cases, error handling, security, performance.\n\n"
            f"The LAST LINE of {_TIER_S_DIR}/REVIEW-{reviewer_label}-{p}-{round_}.md must be exactly: REVIEW_DONE_{p}_{round_}_{reviewer_label}\n"
            f"Work inside {paths.dir.parent}."
        )

    async def _run_builder_fix(
        self,
        sessions: dict[str, ConsensusParticipant],
        p: int,
        round_: int,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
    ) -> TmuxTurnResult:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_BUILDER],
            config=None,
            repo_path=None,
            prompt=self._builder_fix_prompt(p, round_, paths, cfg["opencode_reviewers"]),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: self._read_signal(paths.status_path) == f"FIX_DONE_{p}_{round_}",
        )
        return turn

    def _builder_fix_prompt(self, p: int, round_: int, paths: _TierSPaths, opencode_reviewers: list | None = None) -> str:
        labels = [label for label, _ in _REVIEW_LABELS] + [label for label, _ in (opencode_reviewers or [])]
        review_files = [f"{_TIER_S_DIR}/REVIEW-{label}-{p}-{round_}.md" for label in labels]
        file_list = "\n".join(f"- {f} (if exists)" for f in review_files)
        return (
            f"Reviewers finished round {round_} for sub-plan {p}.\n\n"
            f"Read the review files:\n{file_list}\n\n"
            f"Apply the valid findings to the code. Ignore findings you disagree with (note why).\n"
            f"Update {_TIER_S_DIR}/BUILD_SUMMARY_{p}.md with what was changed.\n"
            f"Overwrite {_TIER_S_DIR}/status with exactly FIX_DONE_{p}_{round_} and print it.\n"
            f"Work inside {paths.dir.parent}."
        )

    @staticmethod
    def _review_file_done(rpath: Path, expected_signal: str) -> bool:
        if not rpath.exists():
            return False
        content = rpath.read_text(encoding="utf-8", errors="replace").strip()
        return content.endswith(expected_signal)

    async def _one_opencode_review(
        self,
        reviewer_label: str,
        model: str,
        p: int,
        round_: int,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        config: dict[str, Any],
    ) -> bool:
        rpath = paths.review(reviewer_label, p, round_)
        expected_signal = f"REVIEW_DONE_{p}_{round_}_{reviewer_label}"
        try:
            base_command, env_vars, cwd = self._resolve_command(DevProvider.OPENCODE, config)
            base_command = apply_opencode_model_override(base_command, model)
            base_command = normalize_opencode_command(base_command)
            argv = build_run_command(
                base_command=base_command,
                prompt=self._code_reviewer_prompt(reviewer_label, p, round_, paths),
                cwd=cwd,
                title=f"tier-s review {reviewer_label} p{p} r{round_}",
                agent_name="build",
            )
            env = {**os.environ, **env_vars}
            api_key = self._settings.OPENCODE_GO_API_KEY or os.environ.get("OPENCODE_GO_API_KEY", "")
            if api_key:
                env["OPENCODE_GO_API_KEY"] = api_key

            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout=cfg["timeout_seconds"])
            except asyncio.TimeoutError:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
                logger.warning("OpenCode reviewer %s timed out (sub-plan %d round %d)",
                               reviewer_label, p, round_)
                return self._review_file_done(rpath, expected_signal)

            return self._review_file_done(rpath, expected_signal)
        except Exception:
            logger.warning("OpenCode reviewer %s failed (sub-plan %d round %d)",
                           reviewer_label, p, round_, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Phase E — Finalize
    # ------------------------------------------------------------------

    async def _run_finalize(
        self,
        sessions: dict[str, ConsensusParticipant],
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
    ) -> TmuxTurnResult:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_PLANNER],
            config=None,
            repo_path=None,
            prompt=self._finalize_prompt(paths),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: self._read_signal(paths.status_path) == "TICKET_COMPLETE",
        )
        return turn

    def _finalize_prompt(self, paths: _TierSPaths) -> str:
        return (
            f"All sub-plans are implemented and reviewed.\n\n"
            f"1. Mark all sub-plans as done in {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]}.\n"
            f"2. Update {_CONTEXT_FILENAME} at the repo root with what was learned, new conventions, or gotchas.\n"
            f"3. Overwrite {_TIER_S_DIR}/status with exactly TICKET_COMPLETE and print it.\n"
            f"Work inside {paths.dir.parent}."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tier_s_paths(repo_path: Path) -> _TierSPaths:
        dir_ = repo_path / _TIER_S_DIR
        return _TierSPaths(
            dir=dir_,
            status_path=dir_ / "status",
            plan_path=dir_ / "CURRENT_PLAN.md",
        )

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

    async def _send_participant_instruction(
        self,
        participant: ConsensusParticipant,
        config: dict[str, Any] | None,
        repo_path: Path | None,
        prompt: str,
        timeout: int,
        completion_check: Callable[[], bool] | None = None,
    ) -> TmuxTurnResult:
        return await self._tmux.send_instruction(
            participant.session_key,
            prompt,
            timeout=timeout,
            completion_check=completion_check,
        )

    def _reset_signal_file(self, path: Path) -> None:
        path.write_text("", encoding="utf-8")

    def _read_signal(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()

    def _pane_has_text(self, session_key: str, needle: str) -> bool:
        content = self._tmux.get_current_output(session_key, lines=40)
        return needle in content

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
            f"Tier S completado: {steps_completed} sub-plan(s), {total_reviews} review(s) recibidos. "
            "Plan y contexto del proyecto actualizados."
        )
        if plan_excerpt:
            summary += f" Plan: {plan_excerpt}"
        return summary

    @staticmethod
    def _compact(text: str, max_len: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= max_len:
            return normalized
        return normalized[: max_len - 3] + "..."
