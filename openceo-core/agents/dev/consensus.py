from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from mcp_server import build_mcp_provider_env
from rate_monitor import RateMonitor
from settings import Settings
from state import DevProvider

from .tmux_runtime import TmuxRuntime, TmuxTurnResult
from .tier_s_contract import (
    GitSnapshot, blocking_findings, detect_claude_question, git_snapshot,
    review_quorum, valid_review,
)

logger = logging.getLogger(__name__)

# === Tier S file conventions ===
_TIER_S_DIR = ".tier-s"
_PLAN_FILENAME = f"{_TIER_S_DIR}/CURRENT_PLAN.md"

_TOTAL_PLANS_HEADER_RE = re.compile(r"^TOTAL_PLANS:\s*(\d+)\s*$", re.M)
_PLAN_READY_RE = re.compile(r"^PLAN_READY_(\d+)$")

# P1 TUI fallback signal patterns — detect signals printed to TUI but not persisted to file.
# Anchored with ^...$ + re.MULTILINE so the signal must appear on its own line,
# avoiding false positives from prompt text that mentions the signal name inline.
_ROLE_PLANNER = "planner"
_ROLE_PLAN_REVIEWER = "plan-reviewer"
_ROLE_BUILDER = "builder"

# Transitional execution constants. review_rounds/max_plans were removed from
# the config surface (Tier S design §15.3, F0); F6 replaces them with
# max_fix_rounds and package budgets.
_REVIEW_ROUNDS = 2
_MAX_PLANS = 8

# Builder sessions (muse) ignore repo CLAUDE.md when AGENTS.md exists, but the
# owner keeps standing rules in CLAUDE.md — order it explicitly, first line.
_BUILDER_RULES_PREAMBLE = (
    "Read CLAUDE.md and AGENTS.md in the repo root and follow all applicable "
    "rules before doing anything else.\n\n"
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
    waiting: bool = False
    requires_human_attention: bool = False
    run_id: str | None = None


class _RepoBusy(RuntimeError):
    pass


class CriticalConsensusOrchestrator:
    """Deterministic tmux orchestration — Planner → Plan Reviewer → Builder → 3 Reviewers (2 rounds)."""

    def __init__(self, settings: Settings, tmux: TmuxRuntime, rate_monitor: RateMonitor | None = None,
                 repo: Any | None = None) -> None:
        self._settings = settings
        self._tmux = tmux
        self._rate_monitor = rate_monitor
        self._repo = repo
        self._run_cache: dict[str, dict[str, Any]] = {}
        self._review_failures: dict[tuple[str, str], str] = {}
        self._projection_tasks: dict[str, asyncio.Task[None]] = {}
        self._projection_pending: dict[str, tuple[str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def is_enabled(config: dict[str, Any]) -> bool:
        """Interactive Tier S trigger: none (product decision, F0 cleanup).

        The old AUTO + ``dev.coding_tool: tier_s`` project flag is gone
        (§15.3 removed the key), and an explicit ``tier_s`` session was
        discarded: loose sessions are always single-provider. The
        orchestrator machinery stays dormant for the autonomous queue,
        which routes per-task through ``resolve_coding_tool`` instead.
        """
        _ = config
        return False

    async def execute(
        self,
        project: str,
        instruction: str,
        config: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        task_id: str | None = None,
        is_spillover: bool = False,
        session_id: str | None = None,
    ) -> ConsensusRunResult:
        try:
            return await self._execute_impl(project, instruction, config, progress, task_id=task_id,
                                            is_spillover=is_spillover, session_id=session_id)
        except _RepoBusy as exc:
            return ConsensusRunResult(False, str(exc), error=str(exc), waiting=True)
        except Exception as exc:
            logger.exception("Tier S execute() raised unhandled exception")
            run_id = None
            owner_id = task_id or session_id
            if self._repo is not None and owner_id:
                try:
                    run = await self._repo.active_tier_s_run(owner_id)
                    if run:
                        run_id = run["run_id"]
                        self._run_cache[run_id] = run
                        await self._checkpoint(run_id, task_id, session_id,
                                               status="failed", active_repo=None,
                                               waiting_reason=str(exc)[:500],
                                               completed_at=datetime.now(timezone.utc),
                                               cleanup_at=datetime.now(timezone.utc) + timedelta(days=30))
                        path = Path(run["repo_path"])
                        self._archive_artifacts(self._tier_s_paths(path), run_id)
                        self._schedule_cleanup(path, days=30)
                except Exception:
                    logger.warning("Could not checkpoint terminal Tier S failure", exc_info=True)
            return ConsensusRunResult(
                ok=False,
                summary=f"Tier S falló con un error inesperado: {exc}",
                error=str(exc)[:2000],
                run_id=run_id,
            )

    async def _execute_impl(
        self,
        project: str,
        instruction: str,
        config: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        task_id: str | None = None,
        is_spillover: bool = False,
        session_id: str | None = None,
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
        if paths.dir.is_symlink():
            return ConsensusRunResult(False, "La carpeta .tier-s no puede ser un enlace simbólico.",
                                      error="unsafe_tier_s_path")
        paths.dir.mkdir(parents=True, exist_ok=True)
        self._ensure_gitignore(repo_path)
        run = await self._open_run(project, repo_path, cfg, task_id, session_id)
        run_id = run["run_id"]

        async def wait(reason: str, *, question: dict[str, object] | None = None) -> ConsensusRunResult:
            updates: dict[str, Any] = {"status": "waiting", "waiting_reason": reason,
                                       "requires_human_attention": question is not None or bool(run.get("question"))}
            if question is not None:
                updates.update(question=question, human_answer=None,
                               requires_human_attention=True)
            await self._checkpoint(run_id, task_id, session_id, **updates)
            return ConsensusRunResult(False, reason, error=reason, waiting=True,
                                      requires_human_attention=bool(run.get("question")), run_id=run_id)

        async def resume_question(participant: ConsensusParticipant,
                                  completion_check: Callable[[], bool],
                                  patterns: list[re.Pattern[str]]) -> TmuxTurnResult:
            answer = str(run.get("human_answer") or "").strip()
            turn = await self._tmux.answer_question(
                participant.session_key, answer, timeout=cfg["timeout_seconds"],
                completion_check=completion_check, tui_signal_patterns=patterns,
            )
            # The answer belongs to one TUI panel only. A later retry must not
            # try to select an option on a panel that has already moved on.
            await self._checkpoint(run_id, task_id, session_id, question=None,
                                   human_answer=None, requires_human_attention=False)
            return turn

        async def turn_wait(turn: TmuxTurnResult, fallback: str) -> ConsensusRunResult:
            question = detect_claude_question(turn.output)
            if question:
                return await wait("Tier S requiere una decisión del propietario.", question=question)
            return await wait(fallback)

        required = cfg["roles"]
        for role in (_ROLE_PLANNER, _ROLE_PLAN_REVIEWER, _ROLE_BUILDER):
            account = required[role]
            if not account["active"]:
                return await wait(f"Tier S espera: {account['label']} está inactivo. Activa la suscripción para reanudar.")
            command = self._account_command(account)
            if not command or not (Path(command[0]).exists() or shutil.which(command[0])):
                return await wait(f"Tier S espera: no se encontró el comando de {account['label']}.")
        active_reviewers = [item for item in cfg["reviewers"] if item["active"]]
        plan_reviewer_label = required[_ROLE_PLAN_REVIEWER]["label"]
        planner_label = required[_ROLE_PLANNER]["label"]
        builder_label = required[_ROLE_BUILDER]["label"]
        families = {item["label"]: item["family"] for item in active_reviewers}
        if len(families) != len(active_reviewers):
            return await wait("Tier S espera: las etiquetas de revisores deben ser únicas.")
        if required[_ROLE_PLAN_REVIEWER]["provider"] != "codex":
            return await wait("Tier S espera: la revisión del plan requiere el adaptador Codex.")
        distinct_families = set(families.values())
        if len(distinct_families) < 2:
            return await wait("Tier S espera: el pool necesita al menos 2 familias de revisores activas para el quorum.")
        if len(distinct_families) < 3:
            logger.warning("Tier S pool degraded: only %d distinct families, no spare reviewer slot",
                           len(distinct_families))
        for account in active_reviewers:
            if account["provider"] not in {"qwen", "claude", "codex"}:
                return await wait(f"Tier S espera: {account['label']} necesita un adaptador de lectura; desactívalo hasta que esté disponible.")

        roster = self._frozen_roster(cfg)
        if run.get("roster") and run["roster"] != roster:
            return await wait("El roster o los comandos cambiaron durante el intento. Cancela este intento antes de iniciar otro.")
        if not run.get("roster"):
            await self._checkpoint(run_id, task_id, session_id, roster=roster,
                                   initial_snapshot=git_snapshot(repo_path).files)

        if run.get("phase") == "complete" and run.get("status") == "completed":
            return ConsensusRunResult(True, "Tier S ya estaba completado.", run_id=run_id)
        await self._checkpoint(run_id, task_id, session_id, status="active", waiting_reason=None)

        planner, others = await self.ensure_project_sessions(
            project=project, config=config,
            wait_ready=run.get("phase") == "planner" and not bool(run.get("question")),
        )
        sessions = {_ROLE_PLANNER: planner, _ROLE_BUILDER: others[0]}
        phase = run.get("phase") or "planner"
        if phase == "planner":
            await self._checkpoint(run_id, task_id, session_id, status="running", phase="planner")
            await progress("Planner diseñando el plan.")
            if run.get("question"):
                if not str(run.get("human_answer") or "").strip():
                    return await wait("Tier S espera respuesta a la pregunta del planner.", question=run["question"])
                turn = await resume_question(
                    sessions[_ROLE_PLANNER],
                    completion_check=lambda: bool(_PLAN_READY_RE.match(self._read_signal(paths.status_path))),
                    patterns=[],
                )
                n_plans = self._reread_total_plans(paths.plan_path) if turn.ok else None
            else:
                n_plans, turn = await self._run_planner(sessions, instruction, paths, cfg, progress,
                                                        task_id=task_id, is_spillover=is_spillover)
            if n_plans is None or not 1 <= n_plans <= cfg["max_plans"] or not paths.plan_path.exists():
                if turn.rate_limited:
                    return await wait(f"Tier S espera por cuota temporal de {planner_label}.")
                return await turn_wait(turn, "Planner sin PLAN_READY_N válido; se conserva el intento para reintentar.")
            await self._checkpoint(run_id, task_id, session_id, phase="plan_review", total_plans=n_plans,
                                   question=None, human_answer=None, requires_human_attention=False)
            phase = "plan_review"

        if phase == "plan_review":
            if run.get("question"):
                if not str(run.get("human_answer") or "").strip():
                    return await wait("Tier S espera respuesta a la pregunta del planner.", question=run["question"])
                turn = await resume_question(
                    sessions[_ROLE_PLANNER],
                    lambda: bool(_PLAN_READY_RE.match(self._read_signal(paths.status_path))),
                    [],
                )
                if not _PLAN_READY_RE.match(self._read_signal(paths.status_path)):
                    return await turn_wait(turn, "Planner no corrigió el plan.")
            await progress("Codex revisando el plan.")
            for attempt in range(2):
                report = await self._readonly_review(required[_ROLE_PLAN_REVIEWER],
                    self._plan_reviewer_prompt(paths) + "\nTicket:\n" + instruction,
                    repo_path, cfg["timeout_seconds"])
                if not valid_review(report):
                    await self._checkpoint(run_id, task_id, session_id,
                        reviewer_availability={plan_reviewer_label: self._review_failures.get(
                            (str(repo_path), plan_reviewer_label), "invalid_report")})
                    return await wait("Codex no entregó una revisión válida del plan.")
                (paths.dir / "PLAN_REVIEW.md").write_text(report, encoding="utf-8")
                if not blocking_findings(report):
                    break
                if attempt:
                    return await wait("El plan conserva hallazgos tras la corrección.")
                self._reset_signal_file(paths.status_path)
                turn = await self._send_participant_instruction(
                    sessions[_ROLE_PLANNER], None, None,
                    f"Corrige hallazgos de .tier-s/PLAN_REVIEW.md en CURRENT_PLAN.md. Conserva TOTAL_PLANS y anuncia PLAN_READY_N. Directorio: {repo_path}",
                    cfg["timeout_seconds"],
                    completion_check=lambda: bool(_PLAN_READY_RE.match(self._read_signal(paths.status_path))),
                    tui_signal_patterns=[],
                    interrupt_check=lambda pane: detect_claude_question(pane) is not None,
                )
                if not turn.ok:
                    return await turn_wait(turn, "Planner no corrigió el plan.")
            n_plans = self._reread_total_plans(paths.plan_path)
            if n_plans is None or not 1 <= n_plans <= cfg["max_plans"]:
                return await wait("TOTAL_PLANS inválido tras la revisión.")
            paths.status_path.write_text("PLAN_REVIEWED", encoding="utf-8")
            await self._checkpoint(run_id, task_id, session_id, phase="build", total_plans=n_plans,
                                   subplan=1, round=1)
            phase = "build"

        n_plans = int(run.get("total_plans") or self._reread_total_plans(paths.plan_path) or 0)
        if n_plans < 1 or n_plans > cfg["max_plans"]:
            return await wait("No hay plan aprobado recuperable.")
        total_reviews = int(run.get("reviews_received") or 0)
        p = int(run.get("subplan") or 1)
        if p <= n_plans:
            await self._ensure_participant_session(sessions[_ROLE_BUILDER], config, wait_ready=True)
        while p <= n_plans:
            if phase == "build":
                before = (GitSnapshot(run["baseline"], "") if run.get("baseline")
                          and run.get("subplan") == p else git_snapshot(repo_path))
                await self._checkpoint(run_id, task_id, session_id, phase="build", subplan=p,
                                       baseline=before.files)
                await progress(f"Builder implementando subplan {p}/{n_plans}.")
                if not (self._read_signal(paths.status_path) == f"BUILD_DONE_{p}"
                        and paths.build_summary(p).exists()):
                    if run.get("question"):
                        if not str(run.get("human_answer") or "").strip():
                            return await wait("Tier S espera respuesta a la pregunta del builder.", question=run["question"])
                        build_turn = await resume_question(
                            sessions[_ROLE_BUILDER],
                            lambda: self._read_signal(paths.status_path) == f"BUILD_DONE_{p}" and paths.build_summary(p).exists(),
                            [re.compile(rf"^BUILD_DONE_{p}$", re.MULTILINE)],
                        )
                    else:
                        build_turn = await self._run_builder(sessions, p, paths, cfg, progress)
                    if build_turn.rate_limited:
                        return await wait(f"Tier S espera por cuota temporal de {builder_label}.")
                if self._read_signal(paths.status_path) != f"BUILD_DONE_{p}" or not paths.build_summary(p).exists():
                    return await turn_wait(build_turn, f"Builder no confirmó BUILD_DONE_{p}.")
                after = git_snapshot(repo_path)
                changed = after.changed_since(before)
                if not changed:
                    return await wait(f"Subplan {p} no produjo cambios verificables en Git.")
                await self._checkpoint(run_id, task_id, session_id, phase="review", round=1,
                                       changed_files=changed, snapshot_hash=after.digest)
                phase = "review"
            if phase == "fix":
                r = int(run.get("round") or 1)
                before_fix = GitSnapshot(run["fix_baseline"], "") if run.get("fix_baseline") else git_snapshot(repo_path)
                if not run.get("fix_baseline"):
                    await self._checkpoint(run_id, task_id, session_id, fix_baseline=before_fix.files)
                labels = [item["label"] for item in active_reviewers]
                if self._read_signal(paths.status_path) != f"FIX_DONE_{p}_{r}":
                    if run.get("question"):
                        if not str(run.get("human_answer") or "").strip():
                            return await wait("Tier S espera respuesta a la pregunta del builder.", question=run["question"])
                        fix = await resume_question(sessions[_ROLE_BUILDER],
                            lambda: self._read_signal(paths.status_path) == f"FIX_DONE_{p}_{r}",
                            [re.compile(rf"^FIX_DONE_{p}_{r}$", re.MULTILINE)])
                    else:
                        fix = await self._run_builder_fix(sessions, p, r, paths, cfg, progress, labels=labels)
                    if fix.rate_limited:
                        return await wait(f"Tier S espera por cuota temporal de {builder_label}.")
                    if self._read_signal(paths.status_path) != f"FIX_DONE_{p}_{r}":
                        return await turn_wait(fix, f"Builder no confirmó FIX_DONE_{p}_{r}.")
                delta = git_snapshot(repo_path).changed_since(before_fix)
                if not delta:
                    return await wait("El builder no cambió código tras los hallazgos.")
                await self._checkpoint(run_id, task_id, session_id, phase="review", round=r + 1,
                                       changed_files=delta, fix_baseline=None)
                phase = "review"
            r = int(run.get("round") or 1) if phase == "review" else 1
            while phase == "review" and r <= cfg["review_rounds"]:
                changed = (run.get("changed_files") or []) if r == int(run.get("round") or 1) else changed
                snapshot = git_snapshot(repo_path)
                if not changed:
                    changed = snapshot.changed_since(GitSnapshot(run.get("baseline") or {}, ""))
                if not changed:
                    return await wait("El diff del subplan desapareció antes de la revisión.")
                await progress(f"Revisión {r} del subplan {p}.")
                reports, availability = await self._review_snapshot(active_reviewers,
                                                       p, r, changed, paths, cfg, repo_path)
                total_reviews += sum(valid_review(value) for value in reports.values())
                await self._checkpoint(run_id, task_id, session_id, reviews_received=total_reviews,
                                       findings_count=int(run.get("findings_count") or 0) + sum(
                                           len(re.findall(r"^\[(?:CRITICAL|WARN|SUGGEST)\] ", value, re.MULTILINE))
                                           for value in reports.values()),
                                       reviewer_results={key: bool(valid_review(value)) for key, value in reports.items()},
                                       reviewer_availability=availability,
                                       snapshot_hash=snapshot.digest)
                if git_snapshot(repo_path).digest != snapshot.digest:
                    return await wait("Un revisor modificó el árbol de trabajo; revisar antes de continuar.")
                if not review_quorum(reports, families):
                    return await wait(f"Revisión {r} sin quorum: se necesitan 2 revisores válidos de familias distintas.")
                if not any(blocking_findings(value) for value in reports.values()):
                    phase = "next"
                    break
                if r >= cfg["review_rounds"]:
                    return await wait(f"Quedan hallazgos bloqueantes en subplan {p} tras {r} rondas.")
                await self._checkpoint(run_id, task_id, session_id, phase="fix", round=r,
                                       fix_baseline=snapshot.files)
                fix = await self._run_builder_fix(sessions, p, r, paths, cfg, progress,
                                                  labels=list(reports))
                if fix.rate_limited:
                    return await wait(f"Tier S espera por cuota temporal de {builder_label}.")
                if self._read_signal(paths.status_path) != f"FIX_DONE_{p}_{r}" or not fix.ok:
                    return await turn_wait(fix, f"Builder no confirmó FIX_DONE_{p}_{r}.")
                after_fix = git_snapshot(repo_path)
                delta = after_fix.changed_since(snapshot)
                if not delta:
                    return await wait("El builder no cambió código tras los hallazgos.")
                changed = delta
                r += 1
                await self._checkpoint(run_id, task_id, session_id, phase="review", round=r,
                                       changed_files=delta, snapshot_hash=after_fix.digest,
                                       fix_baseline=None)
                phase = "review"
            p += 1
            if p <= n_plans:
                await self._checkpoint(run_id, task_id, session_id, phase="build", subplan=p,
                                       round=1, baseline=None, changed_files=[])
                phase = "build"

        if phase != "integrated_fix":
            await self._checkpoint(run_id, task_id, session_id, phase="integrated_review", subplan=n_plans + 1)
        for attempt in range(int(run.get("integrated_attempt") or 0), 3):
            if phase != "integrated_fix":
                cumulative = git_snapshot(repo_path)
                files = cumulative.changed_since(GitSnapshot(run.get("initial_snapshot") or {}, ""))
                if not files:
                    return await wait("No quedó diff acumulado para la revisión integrada.")
                await progress("Revisión integrada del ticket.")
                integrated_reports, integrated_availability = await self._review_snapshot(
                    active_reviewers, 0, attempt + 1, files, paths, cfg, repo_path,
                    prompt_builder=lambda label: (
                        f"Read-only integrated review, {label}. " + self._integrated_prompt(files)),
                )
                total_reviews += sum(valid_review(value) for value in integrated_reports.values())
                await self._checkpoint(run_id, task_id, session_id, reviews_received=total_reviews,
                                       reviewer_results={key: bool(valid_review(value)) for key, value in integrated_reports.items()},
                                       reviewer_availability=integrated_availability,
                                       snapshot_hash=cumulative.digest)
                if git_snapshot(repo_path).digest != cumulative.digest:
                    return await wait("Un revisor modificó el árbol de trabajo; revisar antes de continuar.")
                if not review_quorum(integrated_reports, families):
                    return await wait("Revisión integrada sin quorum: se necesitan 2 revisores válidos de familias distintas.")
                if not any(blocking_findings(value) for value in integrated_reports.values()):
                    break
                if attempt >= 2:
                    return await wait("Quedan hallazgos en la revisión integrada.")
                await self._checkpoint(run_id, task_id, session_id, phase="integrated_fix",
                                       integrated_baseline=cumulative.files, integrated_attempt=attempt)
                phase = "integrated_fix"

            before_fix = GitSnapshot(run.get("integrated_baseline") or {}, "")
            if self._read_signal(paths.status_path) != "FIX_DONE_INTEGRATED":
                if run.get("question"):
                    if not str(run.get("human_answer") or "").strip():
                        return await wait("Tier S espera respuesta a la pregunta del builder.", question=run["question"])
                    fix_turn = await resume_question(
                        sessions[_ROLE_BUILDER],
                        lambda: self._read_signal(paths.status_path) == "FIX_DONE_INTEGRATED",
                        [re.compile(r"^FIX_DONE_INTEGRATED$", re.MULTILINE)],
                    )
                else:
                    self._reset_signal_file(paths.status_path)
                    integrated_files = "\n".join(
                        f"- {paths.review(item['label'], 0, attempt + 1)}"
                        for item in active_reviewers
                        if paths.review(item["label"], 0, attempt + 1).exists())
                    fix_turn = await self._send_participant_instruction(
                        sessions[_ROLE_BUILDER], None, None,
                        "Fix valid blocking findings in these integrated review files:\n"
                        f"{integrated_files}\n"
                        "Update the relevant BUILD_SUMMARY_N.md files. Write FIX_DONE_INTEGRATED to .tier-s/status when done. "
                        f"Work inside {repo_path}.",
                        cfg["timeout_seconds"],
                        completion_check=lambda: self._read_signal(paths.status_path) == "FIX_DONE_INTEGRATED",
                        tui_signal_patterns=[re.compile(r"^FIX_DONE_INTEGRATED$", re.MULTILINE)],
                        interrupt_check=lambda pane: detect_claude_question(pane) is not None,
                    )
                if fix_turn.rate_limited:
                    return await wait(f"Tier S espera por cuota temporal de {builder_label}.")
                if not fix_turn.ok or self._read_signal(paths.status_path) != "FIX_DONE_INTEGRATED":
                    return await turn_wait(fix_turn, "Builder no confirmó el fix integrado.")
            after_fix = git_snapshot(repo_path)
            delta = after_fix.changed_since(before_fix)
            if not delta:
                return await wait("El fix integrado no cambió código verificable.")
            reports, availability = await self._review_snapshot(active_reviewers,
                n_plans, cfg["review_rounds"] + 1 + attempt, delta, paths, cfg, repo_path)
            total_reviews += sum(valid_review(value) for value in reports.values())
            await self._checkpoint(run_id, task_id, session_id, reviews_received=total_reviews,
                                   findings_count=int(run.get("findings_count") or 0) + sum(
                                       len(re.findall(r"^\[(?:CRITICAL|WARN|SUGGEST)\] ", value, re.MULTILINE))
                                       for value in reports.values()),
                                   reviewer_results={key: bool(valid_review(value)) for key, value in reports.items()},
                                   reviewer_availability=availability)
            if git_snapshot(repo_path).digest != after_fix.digest or not review_quorum(reports, families):
                return await wait("La confirmación del último delta no alcanzó quorum o cambió el diff.")
            if any(blocking_findings(value) for value in reports.values()):
                return await wait("El último delta conserva hallazgos bloqueantes.")
            await self._checkpoint(run_id, task_id, session_id, phase="integrated_review",
                                   integrated_attempt=attempt + 1)
            phase = "integrated_review"
        else:
            return await wait("No se confirmó la revisión integrada final.")

        if git_snapshot(repo_path).digest != cumulative.digest:
            return await wait("El diff cambió después de la confirmación final.")
        paths.status_path.write_text("TICKET_COMPLETE", encoding="utf-8")
        await self._checkpoint(run_id, task_id, session_id, status="completed", phase="complete",
                               active_repo=None, completed_at=datetime.now(timezone.utc),
                               cleanup_at=datetime.now(timezone.utc) + timedelta(days=7),
                               snapshot_hash=cumulative.digest)
        self._archive_artifacts(paths, run_id)
        self._schedule_cleanup(repo_path, days=7)
        return ConsensusRunResult(True, f"Tier S completado: {n_plans} subplan(es), {total_reviews} revisiones válidas.",
                                  steps_completed=n_plans, reviews_received=total_reviews, run_id=run_id)

    @staticmethod
    def _ensure_gitignore(repo_path: Path) -> None:
        result = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"],
                                cwd=repo_path, check=True, capture_output=True, text=True)
        exclude = Path(result.stdout.strip())
        if not exclude.is_absolute():
            exclude = repo_path / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if ".tier-s/" not in lines and ".tier-s" not in lines:
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(("\n" if lines else "") + ".tier-s/\n")

    @staticmethod
    def _archive_artifacts(paths: _TierSPaths, run_id: str) -> None:
        if (paths.dir / "runs").is_symlink():
            raise ValueError("Unsafe .tier-s/runs symlink")
        archive = paths.dir / "runs" / run_id
        archive.mkdir(parents=True, exist_ok=True)
        for path in paths.dir.iterdir():
            if path.is_file():
                path.replace(archive / path.name)

    @staticmethod
    def _account_command(account: dict[str, Any]) -> list[str]:
        raw = account.get("command")
        if isinstance(raw, str):
            return shlex.split(raw)
        if isinstance(raw, list) and raw:
            return [str(part) for part in raw]
        return [str(account.get("label") or "")]

    def _frozen_roster(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        accounts = [{"role": role, **item} for role, item in cfg["roles"].items()]
        accounts += [{"role": "code-reviewer", **item} for item in cfg["reviewers"]]
        return [{
            "role": item["role"], "label": item["label"], "model": item["model"],
            "provider": item["provider"], "active": item["active"],
            "command_hash": hashlib.sha256(
                repr(self._account_command(item)).encode()).hexdigest(),
        } for item in accounts]

    async def _open_run(self, project: str, repo_path: Path, cfg: dict[str, Any],
                        task_id: str | None, session_id: str | None) -> dict[str, Any]:
        owner = task_id or session_id or str(uuid4())
        if self._repo is not None:
            await self._cleanup_expired(repo_path)
            existing = await self._repo.active_tier_s_run(owner)
            if existing:
                self._run_cache[existing["run_id"]] = existing
                if await self._repo.acquire_repo_lock(str(repo_path), owner) is None:
                    raise _RepoBusy("Tier S espera: el repo está bloqueado por otra ejecución.")
                return existing
            occupant = await self._repo.active_tier_s_run_for_repo(str(repo_path))
            if occupant:
                raise _RepoBusy(f"Tier S espera: el repo ya tiene un intento activo ({occupant['run_id']}).")
            if await self._repo.acquire_repo_lock(str(repo_path), owner) is None:
                raise _RepoBusy("Tier S espera: el repo está bloqueado por otra ejecución.")
        now = datetime.now(timezone.utc)
        doc = {"run_id": str(uuid4()), "owner_id": owner, "task_id": task_id,
               "session_id": session_id, "project": project, "repo_path": str(repo_path),
               "active_repo": str(repo_path), "status": "active", "phase": "planner",
               "subplan": 1, "round": 1, "roster": [],
               "initial_snapshot": {},
               "phase_started_at": now, "phase_durations_seconds": {},
               "created_at": now, "updated_at": now}
        if self._repo is not None:
            try:
                await self._repo.create_tier_s_run(doc)
            except DuplicateKeyError as exc:
                if await self._repo.active_tier_s_run(owner) is None:
                    # Another owner won the repo: don't orphan our lock on it.
                    await self._repo.release_repo_lock(str(repo_path), owner)
                raise _RepoBusy("Tier S espera: otro intento tomó el repo antes de iniciar.") from exc
            if task_id:
                await self._repo.patch_task(task_id, {"tier_s_run_id": doc["run_id"]})
            if session_id:
                await self._repo.patch_dev_session(session_id, {"tier_s_run_id": doc["run_id"]})
        self._run_cache[doc["run_id"]] = doc
        return doc

    async def _cleanup_expired(self, repo_path: Path) -> None:
        if self._repo is None:
            return
        if (repo_path / _TIER_S_DIR).is_symlink():
            logger.warning("Refusing Tier S cleanup through symlink in %s", repo_path)
            return
        if (repo_path / _TIER_S_DIR / "runs").is_symlink():
            logger.warning("Refusing Tier S cleanup through runs symlink in %s", repo_path)
            return
        for run in await self._repo.expired_tier_s_runs(datetime.now(timezone.utc), str(repo_path)):
            run_id = str(run.get("run_id") or "")
            try:
                from uuid import UUID
                UUID(run_id)
            except ValueError:
                continue
            archive = repo_path / _TIER_S_DIR / "runs" / run_id
            if archive.is_dir() and not archive.is_symlink():
                shutil.rmtree(archive)
            await self._repo.patch_tier_s_run(run_id, {
                "cleanup_at": None, "artifacts_cleaned_at": datetime.now(timezone.utc),
            })

    async def cleanup_expired_all(self) -> None:
        """Reconcile due artifact cleanup once at service startup."""
        if self._repo is None:
            return
        previous_ids: set[str] = set()
        while True:
            due = await self._repo.expired_tier_s_runs_any(datetime.now(timezone.utc))
            if not due:
                return
            current_ids = {str(run["run_id"]) for run in due}
            if current_ids == previous_ids:
                logger.warning("Tier S cleanup made no progress; retaining remaining records")
                return
            previous_ids = current_ids
            paths = {Path(str(run["repo_path"])) for run in due}
            for path in paths:
                await self._cleanup_expired(path)
            if len(due) < 200:
                return

    async def reconcile_interrupted_runs(self) -> None:
        """A restart pauses active attempts without discarding their phase or TUI."""
        if self._repo is None:
            return
        while True:
            runs = await self._repo.interrupted_tier_s_runs()
            if not runs:
                return
            for run in runs:
                self._run_cache[run["run_id"]] = run
                await self._checkpoint(
                    run["run_id"], run.get("task_id"), run.get("session_id"),
                    status="waiting", waiting_reason="Tier S pausado por reinicio; relanza el ticket para reanudar el mismo intento",
                )
            if len(runs) < 200:
                return

    def _schedule_cleanup(self, repo_path: Path, *, days: int) -> None:
        if self._repo is None:
            return
        asyncio.get_running_loop().call_later(
            days * 24 * 3600,
            lambda: asyncio.create_task(self._cleanup_expired(repo_path)),
        )

    async def cancel_run(self, owner_id: str, config: dict[str, Any]) -> bool:
        if self._repo is None:
            return False
        run = await self._repo.active_tier_s_run(owner_id)
        if run is None:
            return False
        repo_path = self._resolve_repo_path(config)
        if repo_path is None or str(repo_path) != run.get("repo_path"):
            return False
        self._run_cache[run["run_id"]] = run
        await self._checkpoint(run["run_id"], run.get("task_id"), run.get("session_id"),
                               status="cancelled", active_repo=None, question=None,
                               requires_human_attention=False,
                               waiting_reason="Intento TIER S cancelado por el propietario",
                               completed_at=datetime.now(timezone.utc),
                               cleanup_at=datetime.now(timezone.utc) + timedelta(days=30))
        self._archive_artifacts(self._tier_s_paths(repo_path), run["run_id"])
        self._schedule_cleanup(repo_path, days=30)
        self.close_project(run["project"], config)
        return True

    async def _checkpoint(self, run_id: str, task_id: str | None, session_id: str | None,
                          **updates: Any) -> None:
        run = self._run_cache[run_id]
        public_keys = {"phase", "status", "subplan", "round", "reviewer_results", "reviewer_availability",
                       "requires_human_attention", "question", "waiting_reason"}
        changed_public = any(key in public_keys and run.get(key) != value for key, value in updates.items())
        next_phase = updates.get("phase")
        if next_phase and next_phase != run.get("phase"):
            now = datetime.now(timezone.utc)
            started = run.get("phase_started_at") or run.get("updated_at") or now
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            durations = dict(run.get("phase_durations_seconds") or {})
            old_phase = str(run.get("phase") or "preflight")
            durations[old_phase] = round(float(durations.get(old_phase) or 0) + (now - started).total_seconds(), 2)
            updates["phase_started_at"] = now
            updates["phase_durations_seconds"] = durations
        run.update(updates)
        run["updated_at"] = datetime.now(timezone.utc)
        if self._repo is not None:
            await self._repo.patch_tier_s_run(run_id, updates)
            if task_id:
                summary = {
                    "run_id": run_id, "phase": run.get("phase"), "status": run.get("status"),
                    "subplan": run.get("subplan"), "round": run.get("round"),
                    "reviewers": run.get("reviewer_results") or {},
                    "reviewer_availability": run.get("reviewer_availability") or {},
                    "requires_human_attention": bool(run.get("requires_human_attention")),
                    "question": run.get("question"), "waiting_reason": run.get("waiting_reason"),
                }
                task = await self._repo.patch_task(task_id, {"tier_s_progress": summary})
                adapter = getattr(self, "_erpnext", None)
                if changed_public and adapter is not None and task is not None:
                    mirrors = (task.get("params") or {}).get("_mirrors") or {}
                    mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
                    name = str(mirror.get("name") or "") if isinstance(mirror, dict) else ""
                    if name:
                        self._projection_pending[task_id] = (name, summary)
                        if task_id not in self._projection_tasks:
                            self._projection_tasks[task_id] = asyncio.create_task(
                                self._flush_projection(task_id, adapter))
            if session_id:
                await self._repo.patch_dev_session(session_id, {"tier_s_progress": {
                    "phase": run.get("phase"), "status": run.get("status"),
                    "waiting_reason": run.get("waiting_reason"), "question": run.get("question"),
                }})
        if run.get("status") in {"completed", "failed", "cancelled"} and run.get("active_repo") is None:
            if self._repo is not None and task_id is None and session_id is not None:
                # Interactive-owned run: autonomous runs are released by
                # DevSessionManager.run, which owns the whole dev turn.
                try:
                    await self._repo.release_repo_lock(
                        str(run.get("repo_path") or ""), str(run.get("owner_id") or session_id))
                except Exception:
                    logger.warning("Could not release Tier S repo lock", exc_info=True)
            self._run_cache.pop(run_id, None)
            repo_path = str(run.get("repo_path") or "")
            for key in [key for key in self._review_failures if key[0] == repo_path]:
                self._review_failures.pop(key, None)

    async def _flush_projection(self, task_id: str, adapter: Any) -> None:
        while task_id in self._projection_pending:
            name, summary = self._projection_pending.pop(task_id)
            try:
                await asyncio.wait_for(adapter.update_tier_s_progress(name, summary), timeout=10)
            except Exception:
                logger.warning("Tier S progress projection failed for %s", task_id, exc_info=True)
        self._projection_tasks.pop(task_id, None)

    async def _readonly_review(self, account: dict[str, Any], prompt: str,
                               repo_path: Path, timeout: int) -> str:
        command = self._account_command(account)
        provider = account["provider"]
        if provider == "codex":
            argv = [*command, "exec", "--ephemeral", "-"]
        elif provider == "qwen":
            argv = [*command, "--output-format", "text", prompt]
        elif provider == "claude":
            argv = [*command, "--print", "--permission-mode", "plan",
                    "--no-session-persistence", prompt]
        else:
            return ""
        try:
            with self._isolated_review_worktree(repo_path) as review_path:
                return await self._run_review_process(account, argv, prompt, review_path,
                                                      timeout, check_writes=True,
                                                      origin_repo=repo_path)
        except Exception:
            self._review_failures[(str(repo_path), str(account["label"]))] = "worktree_error"
            logger.warning("Tier S isolated reviewer workspace failed", exc_info=True)
            return ""

    @staticmethod
    @contextmanager
    def _isolated_review_worktree(repo_path: Path):
        """Reviewers inspect the same snapshot away from the builder's real tree."""
        with tempfile.TemporaryDirectory(prefix="tier-s-review-") as directory:
            target = Path(directory) / "repo"
            subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(target), "HEAD"],
                           cwd=repo_path, check=True, capture_output=True)
            try:
                for name, digest in git_snapshot(repo_path).files.items():
                    source = repo_path / name
                    dest = target / name
                    if digest is None:
                        if dest.is_dir() and not dest.is_symlink():
                            shutil.rmtree(dest)
                        elif dest.exists() or dest.is_symlink():
                            dest.unlink()
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    if source.is_symlink():
                        dest.symlink_to(os.readlink(source))
                    else:
                        shutil.copy2(source, dest)
                source_artifacts = repo_path / _TIER_S_DIR
                if source_artifacts.is_dir():
                    dest_artifacts = target / _TIER_S_DIR
                    dest_artifacts.mkdir(exist_ok=True)
                    for source in source_artifacts.iterdir():
                        if source.is_file():
                            shutil.copy2(source, dest_artifacts / source.name)
                yield target
            finally:
                removal = subprocess.run(["git", "worktree", "remove", "--force", str(target)],
                                         cwd=repo_path, check=False, capture_output=True)
                if removal.returncode != 0:
                    shutil.rmtree(target, ignore_errors=True)
                    subprocess.run(["git", "worktree", "prune"], cwd=repo_path,
                                   check=False, capture_output=True)

    async def _run_review_process(self, account: dict[str, Any], argv: list[str], prompt: str,
                                  cwd: Path, timeout: int, *, check_writes: bool = False,
                                  origin_repo: Path) -> str:
        before = git_snapshot(cwd) if check_writes else None
        failure_key = (str(origin_repo), str(account["label"]))
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode() if account["provider"] == "codex" else None),
                timeout=timeout,
            )
            if process.returncode != 0:
                error_text = stderr.decode(errors="replace").lower()
                self._review_failures[failure_key] = (
                    "quota" if any(word in error_text for word in ("rate limit", "quota", "429"))
                    else "access" if any(word in error_text for word in ("unauthorized", "forbidden", "auth"))
                    else "payment" if any(word in error_text for word in ("payment", "billing"))
                    else "process_error"
                )
                logger.warning("Tier S reviewer %s failed: %s", account["label"],
                               stderr.decode(errors="replace")[-500:])
                return ""
            if before is not None and git_snapshot(cwd).digest != before.digest:
                self._review_failures[failure_key] = "write_attempt"
                logger.warning("Tier S reviewer %s changed its isolated worktree", account["label"])
                return ""
            report = stdout.decode(errors="replace").strip()
            if len(report) > 64000:
                self._review_failures[failure_key] = "oversized_report"
                return ""
            self._review_failures.pop(failure_key, None)
            return report
        except asyncio.TimeoutError:
            self._review_failures[failure_key] = "timeout"
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            logger.warning("Tier S reviewer %s timed out", account["label"])
            return ""
        except Exception:
            self._review_failures[failure_key] = "process_error"
            logger.warning("Tier S reviewer %s failed", account["label"], exc_info=True)
            return ""

    async def _review_snapshot(self, reviewers: list[dict[str, Any]],
                               p: int, round_: int, changed: list[str], paths: _TierSPaths,
                               cfg: dict[str, Any], repo_path: Path,
                               prompt_builder: Callable[[str], str] | None = None,
                               ) -> tuple[dict[str, str], dict[str, str]]:
        accounts = list(reviewers)
        labels = [account["label"] for account in accounts]
        if prompt_builder is None:
            prompts = [self._code_reviewer_prompt(label, p, round_, paths, changed)
                       for label in labels]
        else:
            prompts = [prompt_builder(label) for label in labels]
        outputs = await asyncio.gather(*(
            self._readonly_review(account, prompt, repo_path, cfg["timeout_seconds"])
            for account, prompt in zip(accounts, prompts)
        ))
        reports = dict(zip(labels, outputs))
        for label, content in reports.items():
            report_path = paths.review(label, p, round_)
            if valid_review(content):
                report_path.write_text(content.rstrip() + f"\nREVIEW_DONE_{p}_{round_}_{label}\n", encoding="utf-8")
            elif report_path.exists():
                report_path.unlink()
        availability = {
            label: ("available" if valid_review(content) else
                    self._review_failures.get((str(repo_path), label), "invalid_report"))
            for label, content in reports.items()
        }
        return reports, availability

    @staticmethod
    def _integrated_prompt(files: list[str]) -> str:
        names = "\n".join(f"- {name}" for name in files)
        return ("Read-only integrated review of the ticket's cumulative working-tree changes. "
                "Inspect the current Git diff and listed new files; check integration, contracts, "
                "and unresolved defects. Do not edit files or run tests. Output only actionable "
                "[CRITICAL]/[WARN]/[SUGGEST] path:line findings or exactly 'No findings.'.\n" + names)

    async def ensure_project_sessions(
        self,
        project: str,
        config: dict[str, Any],
        wait_ready: bool = True,
        prime_reviewers: bool = True,
        start_roles: set[str] | None = None,
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
        builder = _make(_ROLE_BUILDER)

        for participant in [planner, builder]:
            if participant.role not in (start_roles or {_ROLE_PLANNER}):
                continue
            if not cfg["roles"][participant.role]["active"]:
                continue
            await self._ensure_participant_session(
                participant=participant,
                config=config,
                wait_ready=wait_ready,
            )

        return planner, [builder]

    def close_project(self, project: str, config: dict[str, Any]) -> None:
        for role in [_ROLE_PLANNER, _ROLE_BUILDER]:
            safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
            safe_role = re.sub(r"[^a-zA-Z0-9_-]", "-", role)
            self._tmux.close_session(
                project=f"tier_s::{project}::{role}",
                tmux_session_name=f"dev-{safe_project}-{safe_role}",
            )

    def get_live_output(self, project: str, config: dict[str, Any]) -> str:
        lines: list[str] = []
        for role in [_ROLE_PLANNER, _ROLE_BUILDER]:
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

        review_rounds = _REVIEW_ROUNDS
        max_plans = _MAX_PLANS
        timeout_seconds = max(120, int(tier_s_cfg.get("timeout_minutes", 15)) * 60)

        raw_roles = tier_s_cfg.get("roles", {}) or {}
        roles: dict[str, dict[str, Any]] = {}
        for role_name in [_ROLE_PLANNER, _ROLE_PLAN_REVIEWER, _ROLE_BUILDER]:
            role_cfg = (raw_roles.get(role_name, raw_roles.get(role_name.replace("-", "_"), {}))
                        if isinstance(raw_roles, dict) else {})
            if not isinstance(role_cfg, dict):
                role_cfg = {}
            roles[role_name] = {
                "provider": str(role_cfg.get("provider", "")).strip().lower() or self._default_provider_for_role(role_name),
                "label": str(role_cfg.get("label") or {"planner": "claudej", "plan-reviewer": "codexj", "builder": "muse"}[role_name]),
                "model": str(role_cfg.get("model") or {"planner": "opus", "plan-reviewer": "gpt-6.1-sol", "builder": "muse-spark-1.3-contributor"}[role_name]),
                "active": role_cfg.get("active", True) is True,
                "command": role_cfg.get("command") or [],
                "env": role_cfg.get("env", {}) or {},
            }
            if not roles[role_name]["command"]:
                try:
                    provider = DevProvider(roles[role_name]["provider"])
                    roles[role_name]["command"] = self._resolve_command(provider, config, role=role_name)[0]
                except ValueError:
                    pass

        default_reviewers = [
            {"label": "qwen", "provider": "qwen", "model": "qwen3.8-max", "family": "qwen", "active": True,
             "command": ["qwen", "--approval-mode", "plan"]},
            {"label": "qwen-glm", "provider": "qwen", "model": "glm-5.3", "family": "glm", "active": True,
             "command": ["qwen", "-m", "glm-5.3", "--approval-mode", "plan"]},  # owner 2026-10-03: GLM via Qwen plan (claudeg retired)
            {"label": "agyd", "provider": "gemini", "model": "gemini", "family": "gemini", "active": False, "command": ["agyd"]},
            {"label": "mimo", "provider": "opencode", "model": "opencode-go/mimo-v2.6-pro", "family": "mimo", "active": False,
             "command": ["opencode"]},
        ]
        raw_reviewers = tier_s_cfg.get("reviewers", default_reviewers)
        reviewers: list[dict[str, Any]] = []
        if isinstance(raw_reviewers, list):
            for item in raw_reviewers:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                model = str(item.get("model") or "").strip()
                provider = str(item.get("provider") or "").strip().lower()
                if label and model and provider:
                    # Family falls back to provider (fail-closed for quorum:
                    # two slots sharing a provider never form a quorum).
                    reviewers.append({"label": label, "model": model, "provider": provider,
                                      "family": str(item.get("family") or provider).strip(),
                                      "active": item.get("active", True) is True,
                                      "command": item.get("command") or []})

        return {
            "roles": roles,
            "review_rounds": review_rounds,
            "max_plans": max_plans,
            "timeout_seconds": timeout_seconds,
            "reviewers": reviewers,
        }

    @staticmethod
    def _default_provider_for_role(role: str) -> str:
        if role == _ROLE_BUILDER:
            return "muse"
        if role == _ROLE_PLAN_REVIEWER:
            return "codex"
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
            role_cfg = roles_cfg.get(role, roles_cfg.get(role.replace("-", "_"), {})) or {}
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
        if provider == DevProvider.MUSE:
            return [self._settings.MUSE_CLI_PATH, *shlex.split(self._settings.MUSE_DEFAULT_ARGS)]
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
            tui_signal_patterns=[],
            interrupt_check=lambda pane: detect_claude_question(pane) is not None,
        )
        signal = self._read_signal(paths.status_path)
        match = _PLAN_READY_RE.match(signal)
        # A historical tmux line is not evidence for this attempt; the file is
        # authoritative and TOTAL_PLANS must agree with it.
        if match and self._reread_total_plans(paths.plan_path) != int(match.group(1)):
            match = None
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
            f"Read the ticket and only the project context needed. Resolve ordinary technical choices yourself.\n"
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
            f"- One coherent sub-plan is preferable. Split only for real dependencies or risk.\n"
            f"- Be specific about files, not vague.\n"
            f"- Order sub-plans so dependencies are satisfied left-to-right.\n"
            f"{spillover_block}\n"
            f"If a material owner decision blocks planning, use Claude Code's native question mode. "
            f"Do not announce PLAN_READY_N while a question is pending.\n"
            f"When done, overwrite {_TIER_S_DIR}/status with exactly PLAN_READY_N (where N is your chosen number) and print it.\n"
            f"Work inside {paths.dir.parent}."
        )

    # ------------------------------------------------------------------
    # Phase B — Plan Reviewer
    # ------------------------------------------------------------------

    def _plan_reviewer_prompt(self, paths: _TierSPaths) -> str:
        return (
            f"Review {_TIER_S_DIR}/CURRENT_PLAN.md against the ticket. Read only direct contracts needed. "
            f"Check completeness, ordering, invariants and whether the subplan count is minimal. "
            f"Do not edit files or run tests. Report each real issue as [WARN] path:line — issue → fix; "
            "otherwise output exactly 'No findings.'. Work in the current directory."
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
            tui_signal_patterns=[
                re.compile(rf"^BUILD_DONE_{re.escape(str(p))}$", re.MULTILINE),
            ],
            interrupt_check=lambda pane: detect_claude_question(pane) is not None,
        )
        return turn

    def _builder_prompt(self, p: int, paths: _TierSPaths) -> str:
        return (
            _BUILDER_RULES_PREAMBLE
            + f"Implement ONLY sub-plan {p} from {_TIER_S_DIR}/{_PLAN_FILENAME.split('/')[-1]}.\n\n"
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

    def _code_reviewer_prompt(self, reviewer_label: str, p: int, round_: int,
                              paths: _TierSPaths, changed: list[str]) -> str:
        names = "\n".join(f"- {name}" for name in changed)
        return (
            f"Read-only code review, {reviewer_label}, subplan {p}, round {round_}. "
            f"Review the current working-tree diff for these Git-derived paths, including new files:\n{names}\n"
            "Read direct contracts only. Do not edit files or run tests. Report actionable "
            "[CRITICAL]/[WARN]/[SUGGEST] path:line — issue → fix findings; "
            "otherwise output exactly 'No findings.'."
        )

    async def _run_builder_fix(
        self,
        sessions: dict[str, ConsensusParticipant],
        p: int,
        round_: int,
        paths: _TierSPaths,
        cfg: dict[str, Any],
        progress: Callable[[str], Awaitable[None]],
        labels: list[str] | None = None,
    ) -> TmuxTurnResult:
        self._reset_signal_file(paths.status_path)
        turn = await self._send_participant_instruction(
            participant=sessions[_ROLE_BUILDER],
            config=None,
            repo_path=None,
            prompt=self._builder_fix_prompt(p, round_, paths, labels=labels or []),
            timeout=cfg["timeout_seconds"],
            completion_check=lambda: self._read_signal(paths.status_path) == f"FIX_DONE_{p}_{round_}",
            tui_signal_patterns=[
                re.compile(rf"^FIX_DONE_{re.escape(str(p))}_{re.escape(str(round_))}$", re.MULTILINE),
            ],
            interrupt_check=lambda pane: detect_claude_question(pane) is not None,
        )
        return turn

    def _builder_fix_prompt(self, p: int, round_: int, paths: _TierSPaths,
                            labels: list[str]) -> str:
        review_files = [f"{_TIER_S_DIR}/REVIEW-{label}-{p}-{round_}.md" for label in labels]
        file_list = "\n".join(f"- {f} (if exists)" for f in review_files)
        return (
            _BUILDER_RULES_PREAMBLE
            + f"Reviewers finished round {round_} for sub-plan {p}.\n\n"
            f"Read the review files:\n{file_list}\n\n"
            f"Apply the valid findings to the code. Ignore findings you disagree with (note why).\n"
            f"Update {_TIER_S_DIR}/BUILD_SUMMARY_{p}.md with what was changed.\n"
            f"Overwrite {_TIER_S_DIR}/status with exactly FIX_DONE_{p}_{round_} and print it.\n"
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
        return path.resolve() if path.exists() else None

    async def _send_participant_instruction(
        self,
        participant: ConsensusParticipant,
        config: dict[str, Any] | None,
        repo_path: Path | None,
        prompt: str,
        timeout: int,
        completion_check: Callable[[], bool] | None = None,
        tui_signal_patterns: list[re.Pattern[str]] | None = None,
        interrupt_check: Callable[[str], bool] | None = None,
    ) -> TmuxTurnResult:
        return await self._tmux.send_instruction(
            participant.session_key,
            prompt,
            timeout=timeout,
            completion_check=completion_check,
            tui_signal_patterns=tui_signal_patterns,
            interrupt_check=interrupt_check,
            wait_on_rate_limit=False,
        )

    def _reset_signal_file(self, path: Path) -> None:
        path.write_text("", encoding="utf-8")

    def _read_signal(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
