from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.dev.kimi_session_manager import KimiSessionManager
from agents.dev.opencode_session_manager import OpenCodeSessionManager
from orchestration.knowledge import with_preamble
from config_loader import ConfigLoader
from db.mongo import MongoRepository
from settings import Settings
from state import (
    QAReviewState,
    TaskReviewAction,
    TaskReviewActorType,
    TaskReviewCreateRequest,
    TaskStatus,
    make_task_review_document,
)

logger = logging.getLogger(__name__)


class QAReviewService:
    """Orchestrate the human QA review gate before client delivery.

    When a ticket completes and ``qa_review_required`` is True, the ticket
    enters ``qa_review_state=PENDING``. The QA dashboard lists these tickets.
    The QA can:
    - Approve → ticket becomes COMPLETED and optionally client-visible.
    - Request changes → ticket goes back to QUEUED (or a new fix ticket is created).
    - Open a Kimi Web session → backend creates a new Kimi CLI session
      primed with full ticket context, AGENTS.md, git branch/PR info.
    - Open an OpenCode Web session → same rich context, model kimi-k2.6,
      deep-linked directly to the session.
    """

    def __init__(
        self,
        settings: Settings,
        repo: MongoRepository,
        task_service: Any | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._task_service = task_service
        self._config_loader = ConfigLoader(settings)
        self._kimi = KimiSessionManager(settings)
        self._opencode = OpenCodeSessionManager(settings)

    def set_task_service(self, task_service: Any) -> None:
        self._task_service = task_service

    # ------------------------------------------------------------------
    # Lifecycle hooks called by TaskService when a ticket finishes
    # ------------------------------------------------------------------

    async def on_ticket_completed(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Intercept completed tickets that require QA review.

        Returns the (possibly mutated) ticket dict.
        """
        if not ticket.get("qa_review_required"):
            return ticket

        if ticket.get("qa_review_state") == QAReviewState.APPROVED.value:
            return ticket

        patched = await self._repo.patch_task(
            ticket["task_id"],
            {
                "status": TaskStatus.WAITING.value,
                "waiting_reason": "qa_review_pending",
                "qa_review_state": QAReviewState.PENDING.value,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        if patched is not None:
            ticket = patched
            logger.info(
                "Ticket %s held for QA review (ticket_number=%s)",
                ticket["task_id"],
                ticket.get("ticket_number"),
            )
        return ticket

    # ------------------------------------------------------------------
    # QA actions
    # ------------------------------------------------------------------

    async def approve_ticket(
        self,
        task_id: str,
        reviewer_id: str,
        comment: str = "",
        make_client_visible: bool = True,
    ) -> dict[str, Any]:
        """QA approves the ticket → mark completed and optionally client-visible."""
        ticket = await self._repo.get_task(task_id)
        if ticket is None:
            raise ValueError("Task not found")

        if not ticket.get("qa_review_required"):
            raise ValueError("Ticket does not require QA review")

        updates: dict[str, Any] = {
            "status": TaskStatus.COMPLETED.value,
            "qa_review_state": QAReviewState.APPROVED.value,
            "qa_reviewer_id": reviewer_id,
            "waiting_reason": None,
            "resume_at": None,
            "completed_at": datetime.now(timezone.utc),
            "updated_by": reviewer_id,
        }
        if make_client_visible:
            updates["client_visible"] = True

        service = self._task_service
        if service is not None:
            patched = await service.patch_task(task_id, updates)
        else:
            patched = await self._repo.patch_task(task_id, updates)
        if patched is None:
            raise ValueError("Failed to patch task")

        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(patched.get("project") or ""),
            payload=TaskReviewCreateRequest(
                actor_type=TaskReviewActorType.INTERNAL,
                actor_id=reviewer_id,
                action=TaskReviewAction.APPROVE,
                comment=comment or "Aprobado por QA",
            ),
            approval_state_after=None,
        )
        await self._repo.create_task_review(review_doc.model_dump(mode="python"))

        return patched

    # ------------------------------------------------------------------
    # Rich QA context builder (shared by Kimi + OpenCode)
    # ------------------------------------------------------------------

    async def _build_rich_qa_prompt(
        self,
        ticket: dict[str, Any],
        work_dir: str,
    ) -> str:
        """Build a comprehensive QA review prompt with full ticket context,
        original coding prompt, AGENTS.md, and git branch/PR discovery."""
        ticket_number = ticket.get("ticket_number")
        title = ticket.get("title", "Sin título")
        description = ticket.get("description", "")
        params = dict(ticket.get("params") or {})
        modified_files = params.get("modified_files", [])
        if not isinstance(modified_files, list):
            modified_files = []
        dev_summary = ticket.get("last_response_summary") or ""
        project = str(ticket.get("project") or "default")

        # Original coding prompt (reconstruct the same one sent to the dev agent)
        original_prompt = self._build_coding_prompt_replica(ticket, repo=work_dir)

        # Search git for branch/PR
        branch_info, pr_info = await self._search_git_for_ticket(work_dir, ticket_number)

        # Build the rich context
        lines: list[str] = []

        # --- Ticket header ---
        ticket_ref = f"Ticket #{ticket_number}" if ticket_number else "Ticket"
        lines.append(f"# Revisión QA - {ticket_ref}: {title}")
        lines.append("")

        # --- Description ---
        lines.append("## Descripción del ticket")
        lines.append(description)
        lines.append("")

        # --- Original coding prompt ---
        lines.append("## Prompt original enviado al agente de desarrollo")
        lines.append("```")
        lines.append(original_prompt)
        lines.append("```")
        lines.append("")

        # --- Branch / PR discovery ---
        lines.append("## Rama y PR relacionados")
        if branch_info:
            lines.append(f"Rama encontrada: `{branch_info}`")
        elif pr_info:
            lines.append(f"PR encontrado: {pr_info}")
        else:
            lines.append(f"No se encontró rama activa ni PR para el ticket #{ticket_number}.")
            lines.append("(Las ramas se eliminan al mergear el PR, es normal.)")
            lines.append("Revisa el código directamente en develop, los cambios ya están mergeados.")
        lines.append("")

        # --- Modified files ---
        lines.append("## Archivos modificados según el ticket")
        if modified_files:
            for f in modified_files:
                lines.append(f"- {f}")
        else:
            lines.append("No se registraron archivos modificados.")
        lines.append("")

        # --- Dev summary ---
        lines.append("## Resumen del trabajo realizado")
        lines.append(dev_summary or "No hay resumen disponible.")
        lines.append("")

        # --- QA instructions ---
        lines.append("---")
        lines.append("")
        lines.append("## Tu rol en esta sesión")
        lines.append("")
        if branch_info:
            lines.append(f"1. Analiza la rama: git diff main...{branch_info} y git log --oneline {branch_info}")
            lines.append("2. Revisa los archivos modificados y el prompt original para entender el alcance.")
        elif pr_info:
            lines.append("1. El PR ya está mergeado. Revisa el diff en develop comparando con el commit anterior.")
            lines.append(f"   Usa: git log --all --oneline --grep=\"{ticket_number}\" para encontrar los commits.")
        else:
            lines.append("1. El código ya está en develop. Analízalo directamente, no busques ramas (se borran al mergear).")
            lines.append(f"   Usa: git log --all --oneline --grep=\"{ticket_number}\" para buscar commits.")
            lines.append("2. Si no encuentras commits, revisa el código en su estado actual en develop.")
        lines.append("3. Ponte al tanto del código y del contexto del ticket.")
        lines.append("4. **NO comiences a modificar código todavía.**")
        lines.append("5. **Espera las observaciones o correcciones que te dará el QA.**")
        lines.append("")
        lines.append("Cuando el QA te indique qué corregir, analiza el código existente,")
        lines.append("los cambios ya hechos, y aplica las correcciones necesarias.")
        lines.append("")
        lines.append(f"Proyecto: {project}")
        lines.append(f"Work dir: {work_dir}")

        return "\n".join(lines)

    def _build_coding_prompt_replica(
        self, ticket: dict[str, Any], repo: Path | str | None = None,
    ) -> str:
        """Reconstruct the same coding prompt that was sent to the dev agent."""
        pm = (ticket.get("params") or {}).get("pm_ticket") or {}
        ticket_num = ticket.get("ticket_number")

        lines: list[str] = ["TAREA"]

        if ticket_num is not None:
            lines.append(f"Ticket: #{ticket_num}")
        lines.append(f"Título: {ticket.get('title')}")
        lines.append(f"Descripción: {ticket.get('description')}")

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

        attachments = ticket.get("attachments")
        if attachments:
            import json
            lines.append("")
            lines.append(f"Adjuntos: {json.dumps(attachments)}")

        return with_preamble("\n".join(lines), repo, "es")

    async def _search_git_for_ticket(
        self,
        work_dir: str,
        ticket_number: int | None,
    ) -> tuple[str | None, str | None]:
        """Search git for branches and PRs related to the ticket number.

        Returns (branch_or_commit_info, pr_info) — each is None if not found.
        Branches are ephemeral (deleted after PR merge), so we also
        search commit messages for the ticket number.
        """
        if ticket_number is None:
            return None, None

        work_path = Path(work_dir)
        if not (work_path / ".git").exists():
            return None, None

        # Search for branches matching feat/{number} or containing the number
        branch_info = await self._run_git_branches(work_dir, ticket_number)

        # Search for merged/closed PRs via gh CLI
        pr_info = await self._run_gh_pr_list(work_dir, ticket_number)

        # If no branch found (likely merged/deleted), look for commits
        commit_info: str | None = None
        if branch_info is None:
            commit_info = await self._run_git_log_grep(work_dir, ticket_number)

        return branch_info or commit_info, pr_info

    async def _run_git_branches(self, work_dir: str, ticket_number: int) -> str | None:
        """Find git branches (local + remote) that contain the ticket number."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "branch", "-a",
                "--format=%(refname:short)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=work_dir,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            output = (stdout or b"").decode("utf-8", errors="replace")

            pattern = str(ticket_number)
            matches = []
            for line in output.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if pattern in stripped:
                    # Normalize: strip remote prefixes like origin/
                    clean = stripped
                    for prefix in ("remotes/origin/", "origin/", "remotes/"):
                        if clean.startswith(prefix):
                            clean = clean[len(prefix):]
                            break
                    if clean not in matches:
                        matches.append(clean)

            if matches:
                return matches[0]  # Return the first match
            return None
        except Exception:  # noqa: BLE001
            return None

    async def _run_gh_pr_list(self, work_dir: str, ticket_number: int) -> str | None:
        """Search for closed/merged PRs using gh CLI."""
        try:
            process = await asyncio.create_subprocess_exec(
                "gh", "pr", "list",
                "--search", str(ticket_number),
                "--state", "merged,closed",
                "--json", "number,title,headRefName,baseRefName,state",
                "--limit", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=work_dir,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            output = (stdout or b"").decode("utf-8", errors="replace")
            if not output.strip():
                return None

            import json
            prs = json.loads(output)
            if not prs:
                return None

            lines = []
            for pr in prs[:3]:
                state = pr.get("state", "closed")
                num = pr.get("number", "?")
                title = pr.get("title", "")
                head = pr.get("headRefName", "")
                base = pr.get("baseRefName", "")
                lines.append(f"  PR #{num} ({state}): {title}")
                lines.append(f"    branch: {head} → {base}")

            return "\n".join(lines) if lines else None
        except Exception:  # noqa: BLE001
            return None

    async def _run_git_log_grep(self, work_dir: str, ticket_number: int) -> str | None:
        """Search git log for commits mentioning the ticket number."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "log", "--all", "--oneline",
                "--grep", str(ticket_number),
                "-n", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=work_dir,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            output = (stdout or b"").decode("utf-8", errors="replace").strip()
            if output:
                return f"Commits relacionados:\n{output}"
            return None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Kimi Web integration (always creates NEW session)
    # ------------------------------------------------------------------

    async def launch_kimi_session(
        self,
        task_id: str,
        reviewer_id: str,
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        """Create a NEW Kimi CLI session for QA review (legacy path).

        Always creates a fresh session — never reuses existing ones.
        """
        ticket = await self._repo.get_task(task_id)
        if ticket is None:
            raise ValueError("Task not found")

        if work_dir is None:
            work_dir = self._resolve_work_dir(ticket)

        # Build the rich context prompt
        context_prompt = await self._build_rich_qa_prompt(ticket, work_dir)

        ticket_number = ticket.get("ticket_number")
        title = ticket.get("title", "Sin título")
        custom_title = f"QA #{ticket_number}" if ticket_number else f"QA: {title[:40]}"

        result = self._kimi.create_qa_session_with_prompt(
            work_dir=work_dir,
            custom_title=custom_title,
            context_prompt=context_prompt,
        )

        await self._repo.patch_task(
            task_id,
            {
                "qa_session_id": result["session_id"],
                "qa_reviewer_id": reviewer_id,
                "updated_at": datetime.now(timezone.utc),
            },
        )

        logger.info(
            "QA created new Kimi session %s for ticket %s",
            result["session_id"],
            task_id,
        )
        return {
            "action": "created",
            "session_id": result["session_id"],
            "url": result["url"],
        }

    async def launch_phase_kimi_session(
        self,
        task_id: str,
        reviewer_id: str,
        phase_key: str | None = None,
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        """Create a NEW Kimi session for any human workflow phase.

        Always creates a fresh session — no reuse.
        """
        ticket = await self._repo.get_task(task_id)
        if ticket is None:
            raise ValueError("Task not found")

        current_phase = str(phase_key or ticket.get("phase") or "review").strip() or "review"
        if work_dir is None:
            work_dir = self._resolve_work_dir(ticket)

        # Build the rich context prompt
        context_prompt = await self._build_rich_qa_prompt(ticket, work_dir)

        ticket_number = ticket.get("ticket_number")
        title = ticket.get("title", "Sin título")
        custom_title = f"QA #{ticket_number}" if ticket_number else f"QA: {title[:40]}"

        result = self._kimi.create_qa_session_with_prompt(
            work_dir=work_dir,
            custom_title=custom_title,
            context_prompt=context_prompt,
        )

        # Persist in phase_sessions
        params = dict(ticket.get("params") or {})
        phase_sessions = params.get("phase_sessions")
        if not isinstance(phase_sessions, dict):
            phase_sessions = {}
        phase_sessions[current_phase] = {
            "kimi_session_id": result["session_id"],
            "updated_by": reviewer_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        params["phase_sessions"] = phase_sessions
        await self._repo.patch_task(
            task_id,
            {
                "params": params,
                "updated_by": reviewer_id,
                "updated_at": datetime.now(timezone.utc),
            },
        )

        logger.info(
            "QA created new Kimi session %s for ticket %s phase=%s",
            result["session_id"],
            task_id,
            current_phase,
        )
        return {
            "action": "created",
            "phase": current_phase,
            "session_id": result["session_id"],
            "url": result["url"],
        }

    # ------------------------------------------------------------------
    # OpenCode Web integration (always creates NEW session)
    # ------------------------------------------------------------------

    async def launch_phase_opencode_session(
        self,
        task_id: str,
        reviewer_id: str,
        phase_key: str | None = None,
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        """Create a NEW OpenCode session for QA review, deep-linked to the web UI.

        Uses model opencode-go/kimi-k2.6. Always creates a fresh session.
        """
        ticket = await self._repo.get_task(task_id)
        if ticket is None:
            raise ValueError("Task not found")

        current_phase = str(phase_key or ticket.get("phase") or "review").strip() or "review"
        if work_dir is None:
            work_dir = self._resolve_work_dir(ticket)

        context_prompt = await self._build_rich_qa_prompt(ticket, work_dir)

        ticket_number = ticket.get("ticket_number")
        title = ticket.get("title", "Sin título")
        custom_title = f"QA #{ticket_number}" if ticket_number else f"QA: {title[:40]}"

        result = await self._opencode.create_qa_session(
            work_dir=work_dir,
            title=custom_title,
            context_prompt=context_prompt,
            model="opencode-go/kimi-k2.6",
        )

        # Persist in phase_sessions
        params = dict(ticket.get("params") or {})
        phase_sessions = params.get("phase_sessions")
        if not isinstance(phase_sessions, dict):
            phase_sessions = {}
        phase_sessions[current_phase] = {
            "opencode_session_id": result["session_id"],
            "updated_by": reviewer_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        params["phase_sessions"] = phase_sessions
        await self._repo.patch_task(
            task_id,
            {
                "params": params,
                "updated_by": reviewer_id,
                "updated_at": datetime.now(timezone.utc),
            },
        )

        logger.info(
            "QA created new OpenCode session %s for ticket %s phase=%s",
            result["session_id"],
            task_id,
            current_phase,
        )
        return {
            "action": "created",
            "phase": current_phase,
            "session_id": result["session_id"],
            "url": result["url"],
        }

    # ------------------------------------------------------------------
    # Work dir resolution
    # ------------------------------------------------------------------

    def _resolve_work_dir(self, ticket: dict[str, Any]) -> str:
        """Best-effort resolution of the repository path for a ticket."""
        project = str(ticket.get("project") or "default")

        cfg = self._config_loader.load_project(project)
        repos = cfg.get("project", {}).get("repos", [])
        if isinstance(repos, list):
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                local_path = str(repo.get("local_path") or "").strip()
                if local_path and Path(local_path).exists():
                    return str(Path(local_path).resolve())

        dev_cfg = cfg.get("dev", {}) if isinstance(cfg.get("dev"), dict) else {}
        providers = dev_cfg.get("providers", {}) if isinstance(dev_cfg.get("providers"), dict) else {}
        provider_order = [
            str(dev_cfg.get("default_provider") or "").strip(),
            "codex",
            "claude",
            "opencode",
            "gemini",
            "glm",
        ]
        for provider in provider_order:
            provider_cfg = providers.get(provider) if provider else None
            if not isinstance(provider_cfg, dict):
                continue
            cwd = str(provider_cfg.get("cwd") or "").strip()
            if cwd and Path(cwd).exists():
                return str(Path(cwd).resolve())

        projects_root = self._config_loader.project_root(project)
        if projects_root.exists() and projects_root.is_dir():
            return str(projects_root.resolve())

        openceo_root = Path(__file__).resolve().parent.parent
        return str(openceo_root)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def list_pending_qa_tasks(
        self,
        reviewer_id: str | None = None,
        assigned_only: bool = False,
        project: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return tickets waiting for QA review."""
        query: dict[str, Any] = {
            "qa_review_required": True,
            "qa_review_state": {"$in": [QAReviewState.PENDING.value, QAReviewState.CHANGES_REQUESTED.value]},
        }
        if project:
            query["project"] = project
        if reviewer_id and assigned_only:
            query["involved_user_ids"] = reviewer_id
        rows = self._repo.tasks.find(query, sort=[("updated_at", -1)]).limit(limit)
        return [self._repo._clean(row) async for row in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."
