from __future__ import annotations

import asyncio
import logging
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents.dev.consensus import CriticalConsensusOrchestrator
from agents.dev.opencode_models import apply_opencode_model_override
from agents.dev.opencode_paths import normalize_opencode_command
from agents.dev.opencode_run import (
    build_run_shell_command,
    has_response_sentinel,
    parse_run_output,
)
from agents.dev.tmux_runtime import TmuxRuntime
from config_loader import ConfigLoader
from db.mongo import MongoRepository
from events import RealtimeHub
from mcp_server import build_mcp_provider_env
from project_service import ProjectService
from rate_monitor import RateMonitor
from settings import Settings
from state import (
    DevProvider,
    DevSessionControlRequest,
    DevSessionCreateRequest,
    DevSessionMessageRequest,
    DevSessionMode,
    DevSessionStatus,
    SessionMessage,
    SessionMessageChannel,
    SessionMessageRole,
    make_dev_session_document,
)

logger = logging.getLogger(__name__)

_FINAL_BEGIN_PREFIX = "OPENCEO_FINAL_BEGIN::"
_FINAL_END_PREFIX = "OPENCEO_FINAL_END::"
_VOICE_PLACEHOLDER_PREFIXES = (
    "<frase corta en espanol natural para voz",
    "<respuesta final en espanol natural para voz",
)
_ANY_FINAL_MARKED_BLOCK_RE = re.compile(
    r"(?ms)^[^\S\r\n]*[^\w\r\n]*OPENCEO_FINAL_BEGIN::(?P<marker>[^\s]+)[^\w\r\n]*$"
    r"(?P<body>.*?)"
    r"^[^\S\r\n]*[^\w\r\n]*OPENCEO_FINAL_END::(?P=marker)[^\w\r\n]*$"
)

_CONSENSUS_TMUX_SESSION_RE = re.compile(
    r"^dev-(?P<project>.+)-(?P<role>planner|plan-reviewer|builder|gemini-reviewer)$",
    flags=re.IGNORECASE,
)


class DevSessionService:
    """Session orchestration for the Dev Agent using persistent tmux sessions."""

    def __init__(
        self,
        settings: Settings,
        repo: MongoRepository,
        config_loader: ConfigLoader,
        project_service: ProjectService,
        realtime_hub: RealtimeHub,
        rate_monitor: RateMonitor | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._config_loader = config_loader
        self._project_service = project_service
        self._hub = realtime_hub
        self._tmux = TmuxRuntime(settings, rate_monitor=rate_monitor)
        self._consensus = CriticalConsensusOrchestrator(settings, self._tmux, rate_monitor=rate_monitor, repo=repo)
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._peer_turn_tasks: dict[str, asyncio.Task[None]] = {}

    async def create_session(self, payload: DevSessionCreateRequest) -> dict[str, Any]:
        project = await self._project_service.resolve_project(payload.user_id, payload.project)
        lock = self._project_locks.setdefault(project, asyncio.Lock())
        async with lock:
            await self._repo.set_selected_project(payload.user_id, project)
            config = self._config_loader.load_project(project)
            config_errors = self._config_loader.validate_project(project)
            if config_errors:
                raise ValueError(
                    f"Invalid project config for '{project}': {'; '.join(config_errors)}"
                )
            consensus_mode = payload.provider == DevProvider.AUTO and self._is_consensus_enabled(config)

            # One session per project maximum — reuse if tmux is actually running
            active = await self._repo.find_active_project_session(project, user_id=payload.user_id)
            if active is not None:
                runtime_matches = self._is_consensus_session(active) == consensus_mode
                if runtime_matches:
                    if self._settings.DEV_EXECUTION_MODE == "mock":
                        return active
                    if consensus_mode:
                        try:
                            await self._consensus.ensure_project_sessions(
                                project=project,
                                config=config,
                                wait_ready=False,
                                prime_reviewers=False,
                            )
                            return active
                        except Exception as exc:
                            logger.warning("Consensus runtime for '%s' looked stale: %s", project, exc)
                    elif self._tmux.has_session(project):
                        return active

                logger.warning(
                    "DB session %s says active but no tmux session for project '%s' — canceling stale",
                    active["session_id"], project,
                )
                await self._close_missing_runtime_session(
                    active,
                    "Stale session: tmux no longer exists",
                    terminal_status=DevSessionStatus.CANCELED,
                )

            session_doc = make_dev_session_document(payload, project)
            provider = self._resolve_provider(session_doc.model_dump(mode="python"), config)

            cwd = self._resolve_repo_path(config)
            if self._settings.DEV_EXECUTION_MODE == "mock":
                tmux_name = f"dev-{project}-claude" if consensus_mode else f"openceo-dev-{project}"
                session_doc.status = DevSessionStatus.WAITING_USER
            else:
                if consensus_mode:
                    try:
                        lead, reviewers = await self._consensus.ensure_project_sessions(
                            project=project,
                            config=config,
                            wait_ready=False,
                            prime_reviewers=False,
                        )
                        tmux_name = lead.tmux_session_name
                        provider = lead.provider
                        session_doc.metadata["execution_mode"] = "tier_s"
                        session_doc.metadata["consensus"] = {
                            "lead": "planner",
                            "reviewers": ["codexj", "qwen", "claudeg", "claudep"],
                        }
                    except Exception as exc:
                        logger.error("Failed to start Tier S for '%s': %s", project, exc)
                        raise ValueError(f"Failed to start Tier S: {exc}") from exc
                else:
                    command, env_vars, cwd = self._resolve_command(provider, config)
                    try:
                        shell_mode = provider == DevProvider.OPENCODE
                        if shell_mode:
                            session_doc.metadata["opencode_run_mode"] = True
                        start_command = [] if shell_mode else command
                        tmux_name = self._tmux.start_session(
                            project=project,
                            provider=provider,
                            command=start_command,
                            cwd=cwd,
                            env=env_vars,
                            shell_mode=shell_mode,
                        )
                    except ValueError as exc:
                        if "already exists" in str(exc):
                            # tmux session already exists (e.g., orphan from crash) — reconnect
                            tmux_name = self._tmux._session_name(project)
                            logger.warning("tmux session '%s' already existed, reusing", tmux_name)
                        else:
                            raise
                    except Exception as exc:
                        logger.error("Failed to start tmux session for '%s': %s", project, exc)
                        raise ValueError(f"Failed to start tmux session: {exc}") from exc

                    try:
                        await self._tmux.wait_until_ready(project=project, timeout=45)
                    except Exception:
                        logger.warning("tmux session '%s' started but initial readiness check failed", tmux_name)
                session_doc.status = DevSessionStatus.WAITING_USER

            session_doc.tmux_session_name = tmux_name
            session_doc.provider = provider
            session_doc.started_at = datetime.now(timezone.utc)
            session_doc.repo_path = cwd
            session_doc.repo_name = self._resolve_repo_name(config)

            session_doc.messages.append(
                SessionMessage(
                    role=SessionMessageRole.SYSTEM,
                    channel=SessionMessageChannel.SYSTEM,
                    content=(
                        f"Sesión tmux '{tmux_name}' creada. "
                        f"Provider: {provider.value}. "
                        + (
                            "Modo: tier_s. Equipo configurado por suscripciones activas; el planner y el builder son obligatorios."
                            if consensus_mode
                            else "Esperando instrucciones."
                        )
                    ),
                )
            )

            created = await self._repo.create_dev_session(session_doc.model_dump(mode="python"))
            await self._hub.broadcast("dev_session_created", created)
            return created

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self._repo.get_dev_session(session_id)

    async def list_sessions(
        self,
        project: str | None = None,
        user_id: str | None = None,
        status: DevSessionStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._repo.list_dev_sessions(
            project=project,
            user_id=user_id,
            status=status,
            limit=limit,
        )

    async def send_message(self, session_id: str, payload: DevSessionMessageRequest) -> dict[str, Any]:
        """Synchronous send: wait for the tmux turn to complete."""
        session = await self._validate_and_enqueue_message(session_id, payload)
        project = session["project"]

        lock = self._project_locks.setdefault(project, asyncio.Lock())
        async with lock:
            latest = await self._repo.get_dev_session(session_id)
            if latest is None:
                raise ValueError("Session not found")
            if self._is_consensus_session(latest):
                updated = await self._run_consensus_turn(latest, payload.message)
            else:
                updated = await self._run_tmux_turn(latest, payload.message)

        return updated

    async def send_message_async(self, session_id: str, payload: DevSessionMessageRequest) -> dict[str, Any]:
        """Async send: return immediately while execution continues in background."""
        session = await self._validate_and_enqueue_message(session_id, payload)
        self._start_async_peer_turn(session["session_id"], session["project"], payload.message)
        return session

    async def control_session(self, session_id: str, payload: DevSessionControlRequest) -> dict[str, Any]:
        session = await self._repo.get_dev_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if session.get("user_id") != payload.user_id:
            raise ValueError("User is not allowed to control this session")

        action = payload.action.value

        if action == "cancel":
            project = session["project"]
            self._cancel_async_peer_turn(session_id)
            config = self._config_loader.load_project(project)
            if self._is_consensus_session(session):
                if not await self._consensus.cancel_run(session_id, config):
                    self._consensus.close_project(project=project, config=config)
            else:
                self._tmux.close_session(project)

            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.CANCELED.value,
                    "waiting_reason": payload.reason,
                    "completed_at": datetime.now(timezone.utc),
                },
            )
            if updated is None:
                raise ValueError("Session not found")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        if action in {"approve", "reject"}:
            return await self.send_message_async(
                session_id,
                DevSessionMessageRequest(
                    user_id=payload.user_id,
                    message=f"Human decision: {action}. {payload.reason or ''}".strip(),
                    channel=SessionMessageChannel.SYSTEM,
                ),
            )

        if action == "complete":
            project = session["project"]
            self._cancel_async_peer_turn(session_id)
            config = self._config_loader.load_project(project)
            if self._is_consensus_session(session):
                if not await self._consensus.cancel_run(session_id, config):
                    self._consensus.close_project(project=project, config=config)
            else:
                self._tmux.close_session(project)

            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.COMPLETED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "waiting_reason": payload.reason,
                },
            )
            if updated is None:
                raise ValueError("Session not found")
            await self._finalize_project_memory(updated)
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        if action == "resume":
            if session.get("status") not in {
                DevSessionStatus.RUNNING.value,
                DevSessionStatus.WAITING_USER.value,
            }:
                raise ValueError("Session is closed; create or select another open session")

            runtime_error = await self._detect_missing_runtime(session)
            if runtime_error:
                await self._close_missing_runtime_session(
                    session,
                    runtime_error,
                    terminal_status=DevSessionStatus.CANCELED,
                )
                raise ValueError(runtime_error)

            return session

        raise ValueError(f"Unsupported action: {action}")

    async def resume_provider_conversation(self, session_id: str) -> dict[str, Any]:
        session = await self._repo.get_dev_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if session.get("status") not in {
            DevSessionStatus.RUNNING.value,
            DevSessionStatus.WAITING_USER.value,
        }:
            raise ValueError("Session is closed; create or select another open session")

        runtime_error = await self._detect_missing_runtime(session)
        if runtime_error:
            await self._close_missing_runtime_session(
                session,
                runtime_error,
                terminal_status=DevSessionStatus.CANCELED,
            )
            raise ValueError(runtime_error)

        if self._settings.DEV_EXECUTION_MODE == "mock":
            return session
        if self._is_consensus_session(session) or self._is_opencode_run_session(session):
            return session

        self._ensure_tmux_session_registered(session)
        project = str(session.get("project") or "").strip()
        if not project:
            raise ValueError("Session has no project configured")

        result = await self._tmux.submit_instruction(project=project, text="/resume")
        if not result.ok:
            raise ValueError(result.error or "No pude enviar /resume al provider")
        return session

    async def handle_voice_turn(
        self,
        user_id: str,
        transcript: str,
        session_id: str | None = None,
        project: str | None = None,
        provider: DevProvider = DevProvider.AUTO,
    ) -> dict[str, Any]:
        transcript = transcript.strip()
        if not transcript:
            raise ValueError("Empty transcript")

        target_session_id = session_id
        if target_session_id is None:
            created = await self.create_session(
                DevSessionCreateRequest(
                    user_id=user_id,
                    project=project,
                    mode=DevSessionMode.PEER,
                    provider=provider,
                    title=transcript[:80],
                    objective="Voice peer-programming session",
                )
            )
            target_session_id = created["session_id"]

        updated = await self.send_message_async(
            target_session_id,
            DevSessionMessageRequest(
                user_id=user_id,
                message=transcript,
                channel=SessionMessageChannel.VOICE,
            ),
        )
        return updated

    def get_live_output(self, project: str) -> str:
        """Capture the current tmux pane output for a project."""
        config = self._config_loader.load_project(project)
        if self._is_consensus_enabled(config):
            raw = self._consensus.get_live_output(project=project, config=config)
        else:
            raw = self._tmux.get_current_output(project)
        return self._sanitize_output(raw)

    def is_turn_running(self, session_id: str) -> bool:
        task = self._peer_turn_tasks.get(session_id)
        return bool(task and not task.done())

    async def shutdown(self) -> None:
        """Cleanup on service shutdown. Sessions survive for resilience."""
        # Cancel any in-flight async tasks
        for task in self._peer_turn_tasks.values():
            if not task.done():
                task.cancel()
        self._peer_turn_tasks.clear()
        # Don't kill tmux sessions — they survive restarts by design

    async def cleanup_runtime_orphans(self) -> None:
        """Kill tmux sessions that no longer have an open DB session backing them."""
        all_active = await self._load_open_sessions()
        actions = self._tmux.reconcile(all_active)
        for project, action in actions.items():
            logger.info("Runtime cleanup: project '%s' → %s", project, action)

        consensus_projects = {
            str(session.get("project") or "")
            for session in all_active
            if self._is_consensus_session(session)
        }
        for session_name in self._tmux.all_session_names():
            parsed = self._parse_consensus_tmux_session_name(session_name)
            if parsed is None:
                continue
            project, role = parsed
            if project in consensus_projects:
                continue
            self._tmux.close_session(
                project=f"tier_s::{project}::{role}",
                tmux_session_name=session_name,
            )
            logger.info("Runtime cleanup: killed orphan Tier S tmux session '%s'", session_name)

    async def startup_reconcile(self) -> None:
        """On startup, reconcile tmux sessions with DB records.

        1. Tell TmuxRuntime about live tmux sessions that match DB records (reconnect).
        2. Kill orphan tmux sessions that have no DB counterpart.
        3. Cancel DB sessions whose tmux session no longer exists.
        """
        all_active = await self._load_open_sessions()
        await self.cleanup_runtime_orphans()

        # Cancel DB sessions that have no live tmux session
        for sess in all_active:
            project = sess.get("project", "")
            if self._is_consensus_session(sess):
                config = self._config_loader.load_project(project)
                try:
                    await self._consensus.ensure_project_sessions(
                        project=project,
                        config=config,
                        wait_ready=False,
                        prime_reviewers=False,
                    )
                    if sess.get("status") == DevSessionStatus.RUNNING.value:
                        updated = await self._repo.patch_dev_session(
                            sess["session_id"],
                            {
                                "status": DevSessionStatus.WAITING_USER.value,
                                "waiting_reason": "Service restart: consensus paused, waiting for user resume",
                            },
                        )
                        if updated is not None:
                            await self._hub.broadcast("dev_session_updated", updated)
                    continue
                except Exception as exc:
                    logger.warning("Consensus startup reconcile failed for '%s': %s", project, exc)
            if not self._tmux.has_session(project):
                sid = sess["session_id"]
                logger.info(
                    "Canceling orphan DB session %s (project=%s) — no live tmux",
                    sid, project,
                )
                await self._repo.patch_dev_session(sid, {
                    "status": DevSessionStatus.CANCELED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "waiting_reason": "Service restart: no live tmux session found",
                })

    async def reconcile_open_sessions(
        self,
        user_id: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        """Close DB-open sessions whose backing runtime no longer exists."""
        sessions = await self.list_sessions(user_id=user_id, project=project, limit=500)
        reconciled: list[dict[str, Any]] = []

        for session in sessions:
            if session.get("status") not in {
                DevSessionStatus.RUNNING.value,
                DevSessionStatus.WAITING_USER.value,
            }:
                continue
            completed = await self._reconcile_observed_completion(session)
            if completed is not None:
                reconciled.append(completed)
                continue
            reason = await self._detect_missing_runtime(session)
            if not reason:
                continue
            updated = await self._close_missing_runtime_session(
                session,
                reason,
                terminal_status=DevSessionStatus.CANCELED,
            )
            if updated is not None:
                reconciled.append(updated)

        return reconciled

    async def _load_open_sessions(self) -> list[dict[str, Any]]:
        active_sessions = await self._repo.list_dev_sessions(
            status=DevSessionStatus.RUNNING,
            limit=500,
        )
        waiting_sessions = await self._repo.list_dev_sessions(
            status=DevSessionStatus.WAITING_USER,
            limit=500,
        )
        return active_sessions + waiting_sessions

    def _parse_consensus_tmux_session_name(self, session_name: str) -> tuple[str, str] | None:
        match = _CONSENSUS_TMUX_SESSION_RE.match(str(session_name or "").strip())
        if not match:
            return None
        project = str(match.group("project") or "").strip()
        role = str(match.group("role") or "").strip().lower()
        if not project or not role:
            return None
        return project, role

    # ---- internal ----

    async def _validate_and_enqueue_message(
        self,
        session_id: str,
        payload: DevSessionMessageRequest,
    ) -> dict[str, Any]:
        session = await self._repo.get_dev_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if session.get("user_id") != payload.user_id:
            raise ValueError("User is not allowed to control this session")

        status = session.get("status")
        if status in {
            DevSessionStatus.COMPLETED.value,
            DevSessionStatus.FAILED.value,
            DevSessionStatus.CANCELED.value,
        }:
            raise ValueError(f"Session is closed with status {status}")

        if status == DevSessionStatus.RUNNING.value:
            raise ValueError("Session is already running a command")

        runtime_error = await self._detect_missing_runtime(session)
        if runtime_error:
            await self._close_missing_runtime_session(
                session,
                runtime_error,
                terminal_status=DevSessionStatus.FAILED,
            )
            raise ValueError(runtime_error)

        # Append user message
        user_msg = SessionMessage(
            role=SessionMessageRole.USER,
            channel=payload.channel,
            content=payload.message,
        )
        await self._repo.append_dev_message(session_id, user_msg.model_dump(mode="python"))

        # Mark as running
        started_at = session.get("started_at") or datetime.now(timezone.utc)
        waiting_reason = (
            "Esperando salida JSON determinista de OpenCode"
            if self._is_opencode_run_session(session)
            else "Esperando marcador final determinista del proveedor"
        )
        session = await self._repo.patch_dev_session(
            session_id,
            {
                "status": DevSessionStatus.RUNNING.value,
                "started_at": started_at,
                "waiting_reason": waiting_reason,
            },
        )
        if session is None:
            raise ValueError("Session not found after update")

        await self._hub.broadcast("dev_session_updated", session)
        return session

    def _start_async_peer_turn(self, session_id: str, project: str, instruction: str) -> None:
        running = self._peer_turn_tasks.get(session_id)
        if running is not None and not running.done():
            logger.warning("Replacing stale async peer turn task for session %s", session_id)
            running.cancel()
        task = asyncio.create_task(self._run_async_peer_turn(session_id, project, instruction))
        self._peer_turn_tasks[session_id] = task

    def _cancel_async_peer_turn(self, session_id: str) -> None:
        running = self._peer_turn_tasks.pop(session_id, None)
        if running is not None and not running.done():
            running.cancel()

    async def _run_async_peer_turn(self, session_id: str, project: str, instruction: str) -> None:
        lock = self._project_locks.setdefault(project, asyncio.Lock())
        try:
            async with lock:
                session = await self._repo.get_dev_session(session_id)
                if session is None:
                    return
                if session.get("status") != DevSessionStatus.RUNNING.value:
                    return
                if self._is_consensus_session(session):
                    await self._run_consensus_turn(session, instruction)
                else:
                    await self._run_tmux_turn(session, instruction)
        except Exception as exc:
            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.FAILED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "waiting_reason": f"Async peer turn crashed: {exc}",
                    "last_error": str(exc),
                },
            )
            if updated is not None:
                await self._hub.broadcast("dev_session_updated", updated)
        finally:
            current = asyncio.current_task()
            tracked = self._peer_turn_tasks.get(session_id)
            if tracked is current:
                self._peer_turn_tasks.pop(session_id, None)

    async def _run_consensus_turn(self, session: dict[str, Any], instruction: str) -> dict[str, Any]:
        session_id = session["session_id"]
        project = session["project"]
        config = self._config_loader.load_project(project)
        active_run = await self._repo.active_tier_s_run(session_id)
        if active_run and active_run.get("question"):
            await self._repo.patch_tier_s_run(active_run["run_id"], {"human_answer": instruction})

        async def progress(detail: str) -> None:
            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.RUNNING.value,
                    "waiting_reason": detail,
                },
            )
            if updated is not None:
                await self._hub.broadcast("dev_session_updated", updated)

        result = await self._consensus.execute(
            project=project,
            instruction=instruction,
            config=config,
            progress=progress,
            session_id=session_id,
        )

        assistant_text = result.summary or "Tier S terminó sin resumen."
        assistant_msg = SessionMessage(
            role=SessionMessageRole.ASSISTANT,
            channel=SessionMessageChannel.TEXT,
            content=assistant_text,
        )
        await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))

        updates: dict[str, Any] = {
            "last_output": (result.last_output or assistant_text)[-4000:],
            "last_error": result.error,
        }
        if result.waiting:
            updates["status"] = DevSessionStatus.WAITING_USER.value
            updates["waiting_reason"] = result.summary
        elif result.ok:
            updates["status"] = DevSessionStatus.WAITING_USER.value
            updates["waiting_reason"] = "Tier S listo para la siguiente instrucción"
        else:
            updates["status"] = DevSessionStatus.FAILED.value
            updates["waiting_reason"] = result.summary
            updates["completed_at"] = datetime.now(timezone.utc)

        updated = await self._repo.patch_dev_session(session_id, updates)
        if updated is None:
            raise ValueError("Session not found after consensus update")

        await self._hub.broadcast("dev_session_updated", updated)
        return updated

    async def _run_tmux_turn(self, session: dict[str, Any], instruction: str) -> dict[str, Any]:
        """Send instruction to tmux and capture the result."""
        session_id = session["session_id"]
        project = session["project"]

        if self._settings.DEV_EXECUTION_MODE == "mock":
            # Mock mode: simulate response
            assistant_msg = SessionMessage(
                role=SessionMessageRole.ASSISTANT,
                channel=SessionMessageChannel.TEXT,
                content="Mock mode active: simulated tmux response.",
            )
            await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))
            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.WAITING_USER.value,
                    "last_output": "Mock response",
                    "waiting_reason": "Waiting for next instruction",
                },
            )
            if updated is None:
                raise ValueError("Session not found after mock update")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        if self._is_opencode_run_session(session):
            return await self._run_opencode_turn(session, instruction)

        wrapped_instruction, begin_marker, end_marker = self._build_marker_wrapped_instruction(instruction)

        # Send to tmux
        result = await self._tmux.send_instruction(
            project=project,
            text=wrapped_instruction,
            timeout=self._settings.TMUX_TURN_TIMEOUT_SECONDS,
            completion_marker_begin=begin_marker,
            completion_marker_end=end_marker,
        )

        if not result.ok:
            error_msg = result.error or "tmux session unavailable"
            assistant_msg = SessionMessage(
                role=SessionMessageRole.ASSISTANT,
                channel=SessionMessageChannel.TEXT,
                content=f"Error: {error_msg}",
            )
            await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))
            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.FAILED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "waiting_reason": error_msg,
                    "last_error": error_msg,
                },
            )
            if updated is None:
                raise ValueError("Session not found after failure update")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        # Success — capture output
        raw_output = result.output.strip()
        output = self._sanitize_output(raw_output)
        if not output and raw_output:
            output = raw_output
        updates: dict[str, Any] = {
            "last_output": output[-4000:] if len(output) > 4000 else output,
            "last_error": None,
        }

        if result.timed_out and not result.completion_marker_found:
            updates["status"] = DevSessionStatus.RUNNING.value
            updates["waiting_reason"] = "Esperando marcador final determinista del proveedor"
            updated = await self._repo.patch_dev_session(session_id, updates)
            if updated is None:
                raise ValueError("Session not found after update")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        final_summary = self._extract_marked_summary(output, begin_marker=begin_marker, end_marker=end_marker)
        summary = final_summary
        if not summary:
            summary = "(sin resumen final marcado)"

        assistant_msg = SessionMessage(
            role=SessionMessageRole.ASSISTANT,
            channel=SessionMessageChannel.TEXT,
            content=summary,
        )
        await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))

        updates["status"] = DevSessionStatus.WAITING_USER.value
        updates["waiting_reason"] = "Waiting for next instruction"

        updated = await self._repo.patch_dev_session(session_id, updates)
        if updated is None:
            raise ValueError("Session not found after update")

        await self._hub.broadcast("dev_session_updated", updated)
        return updated

    async def _run_opencode_turn(self, session: dict[str, Any], instruction: str) -> dict[str, Any]:
        session_id = session["session_id"]
        project = session["project"]
        config = self._config_loader.load_project(project)
        command, _env_vars, cwd = self._resolve_command(DevProvider.OPENCODE, config)

        metadata = dict(session.get("metadata") or {})
        opencode_session_id = str(metadata.get("opencode_session_id") or "").strip() or None
        prompt = self._build_opencode_run_instruction(instruction)
        shell_cmd, sentinel = build_run_shell_command(
            base_command=command,
            prompt=prompt,
            cwd=cwd,
            session_id=opencode_session_id,
            title=str(session.get("title") or "").strip() or None,
        )
        result = await self._tmux.send_instruction(
            project=project,
            text=shell_cmd,
            timeout=self._settings.TMUX_TURN_TIMEOUT_SECONDS,
            completion_check=lambda: self._pane_has_response_token(project, sentinel),
        )

        if not result.ok:
            error_msg = result.error or "OpenCode shell session unavailable"
            assistant_msg = SessionMessage(
                role=SessionMessageRole.ASSISTANT,
                channel=SessionMessageChannel.TEXT,
                content=f"Error: {error_msg}",
            )
            await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))
            updated = await self._repo.patch_dev_session(
                session_id,
                {
                    "status": DevSessionStatus.FAILED.value,
                    "completed_at": datetime.now(timezone.utc),
                    "waiting_reason": error_msg,
                    "last_error": error_msg,
                },
            )
            if updated is None:
                raise ValueError("Session not found after OpenCode failure update")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        raw_output = result.output.strip()
        parsed = parse_run_output(raw_output)
        summary = self._normalize_final_summary(parsed.final_text or parsed.raw_text or self._sanitize_output(raw_output))
        if not summary:
            summary = "(sin respuesta final de OpenCode)"

        updates: dict[str, Any] = {
            "last_output": summary[-4000:] if len(summary) > 4000 else summary,
            "last_error": None,
        }
        if parsed.session_id and parsed.session_id != opencode_session_id:
            metadata["opencode_session_id"] = parsed.session_id
            updates["metadata"] = metadata

        if result.timed_out and not self._pane_has_response_token(project, sentinel):
            updates["status"] = DevSessionStatus.RUNNING.value
            updates["waiting_reason"] = "OpenCode sigue ejecutando por session id"
            updated = await self._repo.patch_dev_session(session_id, updates)
            if updated is None:
                raise ValueError("Session not found after OpenCode timeout update")
            await self._hub.broadcast("dev_session_updated", updated)
            return updated

        assistant_msg = SessionMessage(
            role=SessionMessageRole.ASSISTANT,
            channel=SessionMessageChannel.TEXT,
            content=summary,
        )
        await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))

        updates["status"] = DevSessionStatus.WAITING_USER.value
        updates["waiting_reason"] = "Waiting for next instruction"

        updated = await self._repo.patch_dev_session(session_id, updates)
        if updated is None:
            raise ValueError("Session not found after OpenCode update")

        await self._hub.broadcast("dev_session_updated", updated)
        return updated

    def _sanitize_output(self, text: str) -> str:
        if not text:
            return ""

        cleaned = text.replace("\r", "")
        cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", cleaned)

        lines: list[str] = []
        for raw_line in cleaned.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                lines.append("")
                continue

            if re.match(r"^\s*[╭╰│─].*$", line):
                continue
            if re.match(r"^\s*Tip:\s+", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*model:\s+", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*directory:\s+", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*gpt-[^\n]*\s+·\s+.*$", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*develop@[^\n$]*\$\s*codex\b.*$", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*codex\s+--dangerously-", line, flags=re.IGNORECASE):
                continue
            if re.match(r"^\s*[›❯>]\s+.+$", line):
                # Input line echoed by TUI prompt
                continue

            lines.append(line)

        normalized: list[str] = []
        prev_blank = True
        for line in lines:
            if not line.strip():
                if not prev_blank:
                    normalized.append("")
                prev_blank = True
                continue
            normalized.append(line)
            prev_blank = False

        return "\n".join(normalized).strip()

    def _build_marker_wrapped_instruction(self, instruction: str) -> tuple[str, str, str]:
        turn_id = uuid4().hex[:12]
        begin_marker = f"{_FINAL_BEGIN_PREFIX}{turn_id}"
        end_marker = f"{_FINAL_END_PREFIX}{turn_id}"
        body = instruction.strip() or "(sin instruccion)"
        wrapped = (
            f"{body}\n\n"
            "Tu salida final debe cerrar con un unico bloque delimitado por marcadores exactos.\n"
            f"Marcador inicial exacto: {begin_marker}\n"
            f"Marcador final exacto: {end_marker}\n"
            "Entre ambos escribe solo la respuesta final. "
            "Evita URLs, IDs, rutas y nombres de archivos salvo que sean indispensables. "
            "No repitas ni expliques los marcadores fuera del bloque final."
        )
        return wrapped, begin_marker, end_marker

    @staticmethod
    def _build_opencode_run_instruction(instruction: str) -> str:
        body = instruction.strip() or "(sin instruccion)"
        return (
            f"{body}\n\n"
            "Responde solo con la respuesta final. "
            "Evita URLs, IDs, rutas y nombres de archivos salvo que sean indispensables."
        )

    def _extract_marked_summary(self, text: str, begin_marker: str, end_marker: str) -> str:
        if not text or not begin_marker or not end_marker:
            return ""
        block = self._find_standalone_marked_block(text, begin_marker, end_marker)
        if block is not None:
            body_start, end_idx = block
            body = text[body_start:end_idx]
        else:
            begin_idx = self._find_response_marker_occurrence(text, begin_marker)
            end_idx = self._find_response_marker_occurrence(text, end_marker)
            if begin_idx < 0 or end_idx <= begin_idx:
                return ""
            body = text[begin_idx + len(begin_marker):end_idx]
        cleaned = self._normalize_final_summary(body)
        return "" if self._looks_like_voice_placeholder(cleaned) else cleaned

    @staticmethod
    def _find_standalone_marked_block(
        text: str,
        begin_marker: str,
        end_marker: str,
    ) -> tuple[int, int] | None:
        begin_re = re.compile(
            rf"(?m)^[^\S\r\n]*[^\w\r\n]*{re.escape(begin_marker.strip())}[^\w\r\n]*$"
        )
        end_re = re.compile(
            rf"(?m)^[^\S\r\n]*[^\w\r\n]*{re.escape(end_marker.strip())}[^\w\r\n]*$"
        )

        begin_matches = list(begin_re.finditer(text))
        end_matches = list(end_re.finditer(text))
        if not begin_matches or not end_matches:
            return None

        for begin_match in reversed(begin_matches):
            end_match = next((m for m in end_matches if m.start() > begin_match.end()), None)
            if end_match is not None:
                return begin_match.end(), end_match.start()
        return None

    @staticmethod
    def _clean_final_summary_line(line: str) -> str:
        cleaned = line.strip()
        cleaned = re.sub(r"[ \t]*[█▄▀▌▐▍▎▏▐▊▋▉▁▂▃▄▅▆▇▉▣]+[ \t]*$", "", cleaned)
        return cleaned.strip()

    @classmethod
    def _normalize_final_summary(cls, text: str) -> str:
        if not text:
            return ""

        normalized = text.replace("\r", "")
        normalized = cls._repair_wrapped_word_breaks(normalized)
        normalized = re.sub(r":\s*https?://\S+(?:\s*,\s*https?://\S+)*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"https?://\S+", "", normalized, flags=re.IGNORECASE)

        lines_out: list[str] = []
        current_paragraph: list[str] = []
        for raw_line in normalized.splitlines():
            line = cls._clean_final_summary_line(raw_line)
            if not line:
                if current_paragraph:
                    lines_out.append(" ".join(current_paragraph))
                    current_paragraph = []
                if lines_out and lines_out[-1] != "":
                    lines_out.append("")
                continue
            if re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)", line):
                if current_paragraph:
                    lines_out.append(" ".join(current_paragraph))
                    current_paragraph = []
                lines_out.append(line)
                continue
            current_paragraph.append(line)
        if current_paragraph:
            lines_out.append(" ".join(current_paragraph))

        cleaned = "\n".join(lines_out).strip()
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        cleaned = re.sub(r":\s*(?=[.,;!?]|$)", "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _repair_wrapped_word_breaks(text: str) -> str:
        pattern = re.compile(r"([A-Za-zÁÉÍÓÚáéíóúÑñ]{1,16})\n\s*([A-Za-zÁÉÍÓÚáéíóúÑñ]{1,16})")

        def repl(match: re.Match[str]) -> str:
            left = match.group(1)
            right = match.group(2)
            if (
                (2 <= len(left) <= 3 and len(right) <= 4)
                or (len(left) >= 5 and len(right) <= 2)
                or (len(left) == 1 and left.lower() not in {"a", "e", "o", "u", "y"} and len(right) <= 4)
            ):
                return f"{left}{right}"
            return f"{left} {right}"

        previous = text
        current = pattern.sub(repl, text)
        while current != previous:
            previous = current
            current = pattern.sub(repl, previous)
        return current

    @staticmethod
    def _find_response_marker_occurrence(text: str, marker: str) -> int:
        positions = [match.start() for match in re.finditer(re.escape(marker.strip()), text)]
        if len(positions) < 2:
            return -1
        return positions[1]

    async def _detect_missing_runtime(self, session: dict[str, Any]) -> str | None:
        if self._settings.DEV_EXECUTION_MODE == "mock":
            return None

        project = str(session.get("project") or "").strip()
        if not project:
            return "Session has no project configured"

        if self._is_consensus_session(session):
            config = self._config_loader.load_project(project)
            try:
                await self._consensus.ensure_project_sessions(
                    project=project,
                    config=config,
                    wait_ready=False,
                    prime_reviewers=False,
                )
                return None
            except Exception as exc:
                return f"Tier S runtime no longer exists: {exc}"

        if self._tmux.has_session(project):
            return None

        tmux_name = str(session.get("tmux_session_name") or "").strip() or f"openceo-dev-{project}"
        return f"tmux session '{tmux_name}' no longer exists"

    async def _close_missing_runtime_session(
        self,
        session: dict[str, Any],
        reason: str,
        terminal_status: DevSessionStatus,
    ) -> dict[str, Any] | None:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None

        updated = await self._repo.patch_dev_session(
            session_id,
            {
                "status": terminal_status.value,
                "completed_at": datetime.now(timezone.utc),
                "waiting_reason": reason,
                "last_error": reason,
            },
        )
        if updated is None:
            return None

        if self._is_consensus_session(session):
            config = self._config_loader.load_project(str(session.get("project") or ""))
            await self._consensus.cancel_run(session_id, config)

        user_id = str(session.get("user_id") or "").strip()
        project = str(session.get("project") or "").strip()
        if user_id and project:
            await self._repo.clear_project_session(user_id, project)
            active_session_id, _ = await self._repo.get_active_dev_chat_session(user_id)
            if active_session_id == session_id:
                await self._repo.clear_active_dev_chat_session(user_id)

        await self._hub.broadcast("dev_session_updated", updated)
        return updated

    @staticmethod
    def _looks_like_voice_placeholder(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return False
        return any(normalized.startswith(prefix) for prefix in _VOICE_PLACEHOLDER_PREFIXES)

    async def _reconcile_observed_completion(
        self,
        session: dict[str, Any],
    ) -> dict[str, Any] | None:
        if session.get("status") != DevSessionStatus.RUNNING.value:
            return None
        if self._is_consensus_session(session) or self._is_opencode_run_session(session):
            return None

        project = str(session.get("project") or "").strip()
        session_id = str(session.get("session_id") or "").strip()
        if not project or not session_id or not self._tmux.has_session(project):
            return None

        raw_output = self._tmux.get_current_output(project, lines=600)
        summary = self._extract_latest_marked_summary_any(raw_output)
        if not summary:
            return None

        self._cancel_async_peer_turn(session_id)
        sanitized_output = self._sanitize_output(raw_output) or raw_output.strip()

        latest_assistant = self._latest_assistant_message_content(session)
        if latest_assistant != summary:
            assistant_msg = SessionMessage(
                role=SessionMessageRole.ASSISTANT,
                channel=SessionMessageChannel.TEXT,
                content=summary,
            )
            await self._repo.append_dev_message(session_id, assistant_msg.model_dump(mode="python"))

        updated = await self._repo.patch_dev_session(
            session_id,
            {
                "status": DevSessionStatus.WAITING_USER.value,
                "waiting_reason": "Waiting for next instruction",
                "last_output": sanitized_output[-4000:] if len(sanitized_output) > 4000 else sanitized_output,
                "last_error": None,
            },
        )
        if updated is not None:
            await self._hub.broadcast("dev_session_updated", updated)
        return updated

    def _extract_latest_marked_summary_any(self, text: str) -> str:
        if not text:
            return ""

        matches = list(_ANY_FINAL_MARKED_BLOCK_RE.finditer(text.replace("\r", "")))
        if not matches:
            return ""

        for match in reversed(matches):
            body = match.group("body")
            summary = self._normalize_final_summary(body)
            if summary and not self._looks_like_voice_placeholder(summary):
                return summary
        return ""

    @staticmethod
    def _latest_assistant_message_content(session: dict[str, Any]) -> str | None:
        for msg in reversed(session.get("messages", [])):
            if msg.get("role") == SessionMessageRole.ASSISTANT.value:
                return str(msg.get("content") or "").strip() or None
        return None

    def _is_consensus_enabled(self, config: dict[str, Any]) -> bool:
        return self._consensus.is_enabled(config)

    def _is_opencode_run_session(self, session: dict[str, Any]) -> bool:
        if session.get("provider") != DevProvider.OPENCODE.value:
            return False
        metadata = session.get("metadata") or {}
        return isinstance(metadata, dict) and bool(metadata.get("opencode_run_mode"))

    def _is_consensus_session(self, session: dict[str, Any]) -> bool:
        metadata = session.get("metadata") or {}
        if not isinstance(metadata, dict):
            return False
        mode = str(metadata.get("execution_mode") or "").strip().lower()
        return mode == "tier_s"

    def _resolve_provider(self, session: dict[str, Any], config: dict[str, Any]) -> DevProvider:
        requested = DevProvider(session.get("provider", DevProvider.AUTO.value))
        if requested != DevProvider.AUTO:
            return requested

        if self._is_consensus_enabled(config):
            tier_cfg = (config.get("dev", {}).get("tier_s", {}) or {})
            roles = tier_cfg.get("roles", {}) or {}
            planner_cfg = roles.get("planner", {}) or {}
            lead = str(planner_cfg.get("provider", "claude")).strip().lower()
            if lead in DevProvider._value2member_map_:
                return DevProvider(lead)
            return DevProvider.CLAUDE

        # No dev.coding_tool fallback: the key was removed (§15.3) and no
        # consumer may read it. AUTO resolves via default_provider only.
        default_provider = str(config.get("dev", {}).get("default_provider", "auto")).strip().lower()
        if default_provider in DevProvider._value2member_map_ and default_provider != DevProvider.AUTO.value:
            return DevProvider(default_provider)

        tier = str(config.get("dev", {}).get("coding_tier", "auto")).strip().lower()
        if tier == "tier1":
            return DevProvider.CODEX
        if tier == "tier3":
            return DevProvider.OPENCODE
        return DevProvider.CLAUDE

    def _resolve_command(
        self,
        provider: DevProvider,
        config: dict[str, Any],
    ) -> tuple[list[str], dict[str, str], str | None]:
        """Resolve the interactive command, env vars, and cwd for a provider."""
        pkey = provider.value
        provider_cfg = (config.get("dev", {}).get("providers", {}) or {}).get(pkey, {})

        cmd_from_config = provider_cfg.get("command")

        if isinstance(cmd_from_config, list) and cmd_from_config:
            command = [str(token) for token in cmd_from_config]
        elif isinstance(cmd_from_config, str) and cmd_from_config.strip():
            command = shlex.split(cmd_from_config)
        else:
            command = self._default_command(provider)
        if provider == DevProvider.OPENCODE:
            command = apply_opencode_model_override(command, self._settings.OPENCODE_MODEL_OVERRIDE)
            command = normalize_opencode_command(command)

        env_vars: dict[str, str] = {}
        for key, val in (provider_cfg.get("env", {}) or {}).items():
            env_vars[str(key)] = str(val)
        env_vars = {
            **build_mcp_provider_env(self._settings),
            **env_vars,
        }

        cwd = provider_cfg.get("cwd")
        if cwd is None:
            cwd = self._resolve_repo_path(config)
        if cwd is not None:
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

    def _pane_has_response_token(self, project: str, token: str) -> bool:
        content = self._tmux.get_current_output(project, lines=400)
        return has_response_sentinel(content, token)

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

    def _resolve_repo_name(self, config: dict[str, Any]) -> str | None:
        repos = config.get("project", {}).get("repos", [])
        if not repos:
            return None
        return str(repos[0].get("name")) if repos[0].get("name") else None

    def _ensure_tmux_session_registered(self, session: dict[str, Any]) -> None:
        project = str(session.get("project") or "").strip()
        if not project or self._tmux.is_session_tracked(project):
            return
        if not self._tmux.has_session(project):
            return
        if self._is_consensus_session(session) or self._is_opencode_run_session(session):
            return

        provider_value = str(session.get("provider") or "").strip() or DevProvider.AUTO.value
        provider = DevProvider(provider_value)
        config = self._config_loader.load_project(project)
        provider = self._resolve_provider(session, config) if provider == DevProvider.AUTO else provider
        command, _env_vars, cwd = self._resolve_command(provider, config)
        tmux_name = str(session.get("tmux_session_name") or "").strip() or self._tmux._session_name(project)
        self._tmux.register_existing_session(
            project=project,
            provider=provider,
            tmux_session_name=tmux_name,
            command=command,
            cwd=cwd,
            shell_mode=False,
        )

    async def _finalize_project_memory(self, session: dict[str, Any]) -> None:
        project = session["project"]
        context_path = self._config_loader.context_path(project)

        completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        block = (
            f"\n## {completed_at} | Session {session['session_id']}\n"
            f"- Title: {session.get('title')}\n"
            f"- Provider: {session.get('provider')}\n"
            f"- Summary: {str(session.get('last_output') or '')[:600]}\n"
        )
        _append_text(context_path, block, max_chars=self._settings.DEV_CONTEXT_MAX_CHARS)


def _append_text(path: Path, text: str, max_chars: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if max_chars and max_chars > 0:
        existing = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8", errors="replace")
        merged = existing + text
        if len(merged) > max_chars:
            merged = merged[-max_chars:]
        path.write_text(merged, encoding="utf-8")
        return

    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
