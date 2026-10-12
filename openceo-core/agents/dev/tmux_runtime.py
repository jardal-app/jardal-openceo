from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import libtmux

from rate_monitor import RateMonitor
from settings import Settings
from state import DevProvider

logger = logging.getLogger(__name__)

_TMUX_PREFIX = "openceo-dev-"

# Default prompt patterns that signal the CLI is ready for new input.
# Each provider can override these via settings or project config.
_DEFAULT_PROMPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "claude": re.compile(r"(>\s*$|›\s*$|❯\s*$|\$\s*$|claude.*>\s*$)", re.MULTILINE),
    "codex": re.compile(r"(>\s*$|›\s*$|❯\s*$|\$\s*$|codex.*>\s*$)", re.MULTILINE),
    "gemini": re.compile(
        r"(^\s*[›❯>]\s+Type your message\b.*$|^\s*/model\s+gemini[^\n]*$|\?\s+for\s+shortcuts|>\s*$|›\s*$|❯\s*$|\$\s*$|gemini.*>\s*$)",
        re.MULTILINE | re.IGNORECASE,
    ),
    "opencode": re.compile(
        r"(^\s*┃\s+Ask anything\.\.\..*$|^\s*.*\s/status\s+.*$|^\s*.*tab agents\s+ctrl\+p commands\s*$|>\s*$|›\s*$|❯\s*$|\$\s*$|opencode.*>\s*$)",
        re.MULTILINE | re.IGNORECASE,
    ),
    "glm": re.compile(r"(>\s*$|›\s*$|❯\s*$|\$\s*$)", re.MULTILINE),
    "muse": re.compile(r"(>\s*$|›\s*$|❯\s*$|\$\s*$|muse.*>\s*$)", re.MULTILINE),
}

_SHELL_PROMPT_PATTERN = re.compile(r"(^[^\n]*@[^\n:]+:.*[$#%]\s*$|^\s*\$\s*$)", re.MULTILINE)
_VOICE_PLACEHOLDER_PREFIXES = (
    "<frase corta en espanol natural para voz",
    "<respuesta final en espanol natural para voz",
)


@dataclass
class TmuxTurnResult:
    ok: bool
    output: str
    is_ready: bool
    timed_out: bool = False
    completion_marker_found: bool = False
    error: str | None = None
    rate_limited: bool = False
    rate_limit_info: dict[str, Any] | None = None


@dataclass
class _SessionInfo:
    project: str
    provider: DevProvider
    tmux_session_name: str
    command: list[str]
    cwd: str | None = None
    prompt_pattern: re.Pattern[str] | None = None
    shell_mode: bool = False
    env: dict[str, str] | None = None


class TmuxRuntime:
    """Manages persistent tmux sessions for dev agent providers."""

    def __init__(self, settings: Settings, rate_monitor: RateMonitor | None = None) -> None:
        self._settings = settings
        self._rate_monitor = rate_monitor
        self._server: libtmux.Server | None = None
        self._sessions: dict[str, _SessionInfo] = {}

    @property
    def server(self) -> libtmux.Server:
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    @staticmethod
    def _session_name(project: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
        return f"{_TMUX_PREFIX}{safe}"

    def has_session(self, project: str) -> bool:
        return self.has_session_name(self._session_name(project))

    def is_session_tracked(self, project: str) -> bool:
        return project in self._sessions

    def has_session_name(self, tmux_session_name: str) -> bool:
        try:
            session = self.server.sessions.get(session_name=tmux_session_name)
            return session is not None
        except Exception:
            return False

    def start_session(
        self,
        project: str,
        provider: DevProvider,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        prompt_pattern: re.Pattern[str] | None = None,
        tmux_session_name: str | None = None,
        shell_mode: bool = False,
    ) -> str:
        """Start a new tmux session with the given interactive command.

        Returns the tmux session name.
        Raises ValueError if session already exists.
        """
        name = tmux_session_name or self._session_name(project)

        if self.has_session_name(name):
            raise ValueError(f"tmux session '{name}' already exists for project '{project}'")

        # Create tmux session with a plain shell first (survives if command exits)
        start_dir = cwd or os.getcwd()

        # Fence first: a plan failure must not orphan a live session.
        if command:
            from orchestration.agent_sandbox import sandboxed_shell_command
            shell_cmd = sandboxed_shell_command(
                command=[str(token) for token in command],
                workspace=start_dir, base=os.environ,
                overlay=dict(env or {}),
                config_args=[str(token) for token in command])
        else:
            shell_cmd = ""

        session = self.server.new_session(
            session_name=name,
            start_directory=start_dir,
        )

        # Set environment variables before launching the command
        if env:
            for key, val in env.items():
                session.set_environment(key, val)

        pattern = prompt_pattern or (_SHELL_PROMPT_PATTERN if shell_mode else _DEFAULT_PROMPT_PATTERNS.get(provider.value))

        # Launch the interactive provider command inside the shell,
        # fenced: clean env + bwrap (CLI shell escapes inherit the fence).
        pane = session.active_window.active_pane
        if shell_cmd:
            # No scratch cleanup here: the pane execs async, so sources
            # must outlive this call (review H7 stays bytes on disk).
            pane.send_keys(shell_cmd, enter=True)

        self._sessions[project] = _SessionInfo(
            project=project,
            provider=provider,
            tmux_session_name=name,
            command=command,
            cwd=cwd,
            prompt_pattern=pattern,
            shell_mode=shell_mode,
            env=dict(env) if env else None,
        )

        logger.info("Started tmux session '%s' for project '%s' with command: %s", name, project, shell_cmd)
        return name

    def register_existing_session(
        self,
        project: str,
        provider: DevProvider,
        tmux_session_name: str,
        command: list[str] | None = None,
        cwd: str | None = None,
        prompt_pattern: re.Pattern[str] | None = None,
        shell_mode: bool = False,
    ) -> bool:
        """Track an already-existing tmux session without starting a new process."""
        if not self.has_session_name(tmux_session_name):
            return False

        self._sessions[project] = _SessionInfo(
            project=project,
            provider=provider,
            tmux_session_name=tmux_session_name,
            command=command or [],
            cwd=cwd,
            prompt_pattern=prompt_pattern or (_SHELL_PROMPT_PATTERN if shell_mode else _DEFAULT_PROMPT_PATTERNS.get(provider.value)),
            shell_mode=shell_mode,
        )
        return True

    async def send_instruction(
        self,
        project: str,
        text: str,
        timeout: int | None = None,
        completion_marker_begin: str | None = None,
        completion_marker_end: str | None = None,
        completion_check: Callable[[], bool] | None = None,
        tui_signal_patterns: list[re.Pattern[str]] | None = None,
        interrupt_check: Callable[[str], bool] | None = None,
        wait_on_rate_limit: bool = True,
    ) -> TmuxTurnResult:
        """Send text as keystrokes to the tmux pane and wait for deterministic completion.

        Returns TmuxTurnResult with captured output.
        """
        info = self._sessions.get(project)
        if info is None:
            return TmuxTurnResult(
                ok=False,
                output="",
                is_ready=False,
                error=f"No tmux session tracked for project '{project}'",
            )

        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            session = None

        if session is None:
            self._sessions.pop(project, None)
            return TmuxTurnResult(
                ok=False,
                output="",
                is_ready=False,
                error=f"tmux session '{info.tmux_session_name}' no longer exists",
            )

        pane = session.active_window.active_pane

        current_content = self._capture_pane(pane)
        if interrupt_check and interrupt_check(current_content):
            return TmuxTurnResult(False, current_content, False, error="human_question")

        # Ensure provider prompt is ready before sending keys (important right after session startup).
        await self.wait_until_ready(project=project, timeout=45)

        current_content = self._capture_pane(pane)
        if interrupt_check and interrupt_check(current_content):
            return TmuxTurnResult(False, current_content, False, error="human_question")

        # Send keystrokes
        await self._submit_text(pane, text)

        # Poll until the explicit end marker appears.
        poll_interval = (self._settings.TMUX_POLL_INTERVAL_MS or 300) / 1000.0
        turn_timeout = timeout or self._settings.TMUX_TURN_TIMEOUT_SECONDS or 600
        elapsed = 0.0

        while elapsed < turn_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            current_content = self._capture_pane(pane)

            # ---- Completion check (P3: checked FIRST, before rate-limit) ----
            marker_found = bool(
                completion_marker_begin
                and completion_marker_end
                and self._has_completion_block(
                    current_content,
                    begin_marker=completion_marker_begin,
                    end_marker=completion_marker_end,
                )
            )
            callback_satisfied = self._is_completion_check_satisfied(completion_check)

            # P1 fallback: scan TUI pane for signal patterns when file is empty
            tui_found = False
            if not marker_found and not callback_satisfied and tui_signal_patterns:
                tui_found = self._has_tui_signal(current_content, tui_signal_patterns)
                if tui_found:
                    logger.info(
                        "TUI fallback: signal pattern matched in session '%s'",
                        info.tmux_session_name,
                    )

            if marker_found or callback_satisfied or tui_found:
                return TmuxTurnResult(
                    ok=True,
                    output=current_content,
                    is_ready=True,
                    completion_marker_found=marker_found,
                )

            if interrupt_check and interrupt_check(current_content):
                return TmuxTurnResult(
                    ok=False, output=current_content, is_ready=False,
                    error="human_question",
                )

            # ---- Rate limit detection (P3: checked AFTER completion) ----
            provider_name = info.provider.value if info.provider else ""
            if provider_name in ("claude", "codex"):
                rl = RateMonitor.detect_rate_limit(current_content, provider_name)
                if rl is not None:
                    if not wait_on_rate_limit:
                        return TmuxTurnResult(False, current_content, False,
                                              error="rate_limit", rate_limited=True)
                    logger.warning(
                        "Rate limit hit for %s in session '%s' — entering wait-and-resume",
                        provider_name,
                        info.tmux_session_name,
                    )
                    result = await self._handle_rate_limit(
                        pane=pane,
                        info=info,
                        detection=rl,
                        text=text,
                        timeout=turn_timeout - elapsed,
                        completion_marker_begin=completion_marker_begin,
                        completion_marker_end=completion_marker_end,
                        completion_check=completion_check,
                        tui_signal_patterns=tui_signal_patterns,
                    )
                    return result

            # ---- Crash detection ----
            if provider_name in ("claude", "codex") and RateMonitor.detect_process_crash(current_content):
                logger.warning(
                    "Process crash detected for %s in session '%s'",
                    provider_name,
                    info.tmux_session_name,
                )
                recovered = await self._handle_crash_recovery(pane, info, text)
                if recovered:
                    # Reset polling to wait for the restarted process
                    elapsed = 0.0
                    continue
                return TmuxTurnResult(
                    ok=False,
                    output=current_content,
                    is_ready=False,
                    error=f"Process crash in {provider_name}; recovery failed",
                )

        # Timeout — capture whatever is there
        final_content = self._capture_pane(pane)
        marker_found = bool(
            completion_marker_begin
            and completion_marker_end
            and self._has_completion_block(
                final_content,
                begin_marker=completion_marker_begin,
                end_marker=completion_marker_end,
            )
        )
        callback_satisfied = self._is_completion_check_satisfied(completion_check)

        # P1 fallback on timeout: check TUI for signal patterns
        tui_found = False
        if not marker_found and not callback_satisfied and tui_signal_patterns:
            tui_found = self._has_tui_signal(final_content, tui_signal_patterns)
            if tui_found:
                logger.info(
                    "TUI fallback (timeout): signal pattern matched in session '%s'",
                    info.tmux_session_name,
                )

        return TmuxTurnResult(
            ok=True,
            output=final_content,
            is_ready=marker_found or callback_satisfied or tui_found,
            timed_out=True,
            completion_marker_found=marker_found,
        )

    async def answer_question(
        self,
        project: str,
        answer: str,
        *,
        timeout: int,
        completion_check: Callable[[], bool],
        tui_signal_patterns: list[re.Pattern[str]] | None = None,
    ) -> TmuxTurnResult:
        """Select a native Claude question option in the existing tmux session."""
        from .tier_s_contract import detect_claude_question

        info = self._sessions.get(project)
        if info is None:
            return TmuxTurnResult(False, "", False, error="planner_session_missing")
        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            session = None
        if session is None:
            return TmuxTurnResult(False, "", False, error="planner_session_missing")
        pane = session.active_window.active_pane
        current = self._capture_pane(pane)
        question = detect_claude_question(current)
        if question is None:
            return TmuxTurnResult(False, current, False, error="native_question_missing")
        options = [str(option) for option in question["options"]]
        option_indices = [int(index) for index in question.get("option_indices", range(len(options)))]
        value = answer.strip()
        desired: int | None = None
        if value.isdigit():
            if not 1 <= int(value) <= len(options):
                return TmuxTurnResult(False, current, False, error="option_out_of_range")
            desired = option_indices[int(value) - 1]
        else:
            desired = next((option_indices[i] for i, option in enumerate(options)
                            if option.casefold() == value.casefold()), None)
        freeform = desired is None
        if freeform:
            desired = question.get("input_index")
            if desired is None:
                return TmuxTurnResult(False, current, False, error="freeform_option_missing")
        offset = int(desired) - int(question.get("selected_index") or 0)
        key = "Down" if offset > 0 else "Up"
        for _ in range(abs(offset)):
            pane.send_keys(key, enter=False)
        pane.enter()
        if freeform:
            await asyncio.sleep(0.4)
            await self._submit_text(pane, value)

        interval = max(0.2, (self._settings.TMUX_POLL_INTERVAL_MS or 300) / 1000.0)
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            content = self._capture_pane(pane)
            if self._is_completion_check_satisfied(completion_check) or self._has_tui_signal(content, tui_signal_patterns):
                return TmuxTurnResult(True, content, True)
            next_question = detect_claude_question(content)
            if next_question and next_question["text"] != question["text"]:
                return TmuxTurnResult(False, content, False, error="human_question")
            if info.provider.value in {"claude", "codex"}:
                if RateMonitor.detect_rate_limit(content, info.provider.value):
                    return TmuxTurnResult(False, content, False, rate_limited=True,
                                          error="rate_limit")
        return TmuxTurnResult(False, self._capture_pane(pane), False,
                              timed_out=True, error="question_answer_timeout")

    async def submit_instruction(self, project: str, text: str) -> TmuxTurnResult:
        """Send text as keystrokes to the tmux pane and return immediately."""
        info = self._sessions.get(project)
        if info is None:
            return TmuxTurnResult(
                ok=False,
                output="",
                is_ready=False,
                error=f"No tmux session tracked for project '{project}'",
            )

        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            session = None

        if session is None:
            self._sessions.pop(project, None)
            return TmuxTurnResult(
                ok=False,
                output="",
                is_ready=False,
                error=f"tmux session '{info.tmux_session_name}' no longer exists",
            )

        pane = session.active_window.active_pane
        await self.wait_until_ready(project=project, timeout=45)
        await self._submit_text(pane, text)
        return TmuxTurnResult(
            ok=True,
            output=self._capture_pane(pane),
            is_ready=False,
        )

    async def _handle_rate_limit(
        self,
        pane: Any,
        info: _SessionInfo,
        detection: dict[str, Any],
        text: str,
        timeout: float,
        completion_marker_begin: str | None,
        completion_marker_end: str | None,
        completion_check: Callable[[], bool] | None,
        tui_signal_patterns: list[re.Pattern[str]] | None = None,
    ) -> TmuxTurnResult:
        """Handle a rate-limit hit: select wait option, sleep, then resume."""
        provider = info.provider.value

        # For Claude: send "1" to select "Stop and wait for limit to reset"
        if provider == "claude":
            pane.send_keys("1", enter=True)
            await asyncio.sleep(1)

        # Codex: TUI is just blocked, nothing to send

        wait_seconds = RateMonitor.compute_wait_seconds(detection, provider)
        logger.info(
            "Rate limit wait: %s — sleeping %d seconds (%.1f min)",
            provider,
            wait_seconds,
            wait_seconds / 60,
        )

        await asyncio.sleep(wait_seconds)

        # Send continuation prompt into the SAME tmux session
        brief_context = text[:200] if text else "the current task"
        if provider == "claude":
            continuation = (
                f"Continue the task where you left off. "
                f"You were working on: {brief_context}"
            )
        else:
            continuation = ""  # Codex: just press Enter

        if continuation:
            await self._submit_text(pane, continuation)
        else:
            pane.enter()

        # Resume normal polling for completion signals
        poll_interval = (self._settings.TMUX_POLL_INTERVAL_MS or 300) / 1000.0
        remaining_timeout = max(timeout, 300)  # at least 5 min after resume
        elapsed = 0.0

        while elapsed < remaining_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            current_content = self._capture_pane(pane)

            # P3: completion check FIRST, before rate-limit re-check
            marker_found = bool(
                completion_marker_begin
                and completion_marker_end
                and self._has_completion_block(
                    current_content,
                    begin_marker=completion_marker_begin,
                    end_marker=completion_marker_end,
                )
            )
            callback_satisfied = self._is_completion_check_satisfied(completion_check)

            # P1 fallback: scan TUI pane for signal patterns
            tui_found = False
            if not marker_found and not callback_satisfied and tui_signal_patterns:
                tui_found = self._has_tui_signal(current_content, tui_signal_patterns)
                if tui_found:
                    logger.info(
                        "TUI fallback (rate-limit resume): signal pattern matched in '%s'",
                        info.tmux_session_name,
                    )

            if marker_found or callback_satisfied or tui_found:
                return TmuxTurnResult(
                    ok=True,
                    output=current_content,
                    is_ready=True,
                    completion_marker_found=marker_found,
                    rate_limited=True,
                    rate_limit_info=detection,
                )

            # Check for another rate limit (can happen again)
            rl2 = RateMonitor.detect_rate_limit(current_content, provider)
            if rl2 is not None:
                logger.warning("Rate limit hit AGAIN for %s — re-entering wait", provider)
                return await self._handle_rate_limit(
                    pane=pane,
                    info=info,
                    detection=rl2,
                    text=text,
                    timeout=remaining_timeout - elapsed,
                    completion_marker_begin=completion_marker_begin,
                    completion_marker_end=completion_marker_end,
                    completion_check=completion_check,
                    tui_signal_patterns=tui_signal_patterns,
                )

        final_content = self._capture_pane(pane)

        # P1 fallback on timeout: check TUI for signal patterns
        tui_found = False
        if tui_signal_patterns:
            tui_found = self._has_tui_signal(final_content, tui_signal_patterns)
            if tui_found:
                logger.info(
                    "TUI fallback (rate-limit timeout): signal pattern matched in '%s'",
                    info.tmux_session_name,
                )

        return TmuxTurnResult(
            ok=True,
            output=final_content,
            is_ready=tui_found,
            timed_out=True,
            rate_limited=True,
            rate_limit_info=detection,
        )

    async def _handle_crash_recovery(
        self,
        pane: Any,
        info: _SessionInfo,
        original_text: str,
    ) -> bool:
        """Attempt to restart the TUI after a crash (process died → shell prompt).

        Returns True if recovery was attempted; caller should resume polling.
        """
        provider = info.provider.value
        logger.info("Attempting crash recovery for %s in '%s'", provider, info.tmux_session_name)

        if provider == "codex" and self._rate_monitor is not None:
            # Try to get saved session_id for `codex resume`
            cached = self._rate_monitor._cache.get("codex")
            session_id = cached.codex_session_id if cached else None
            if session_id:
                self._restart_interactive_command(
                    pane, [self._settings.CODEX_CLI_PATH, "resume",
                           session_id, "Continue the task"],
                    info.cwd, info.env)
                await asyncio.sleep(5)
                return True

        # Generic: relaunch the interactive command
        if info.command:
            self._restart_interactive_command(
                pane, info.command, info.cwd, info.env)
            await asyncio.sleep(5)

            if provider == "claude":
                continuation = (
                    "Continue working on the current ticket. "
                    "Check git status and recent files to see where you left off."
                )
                await asyncio.sleep(10)  # wait for TUI to load
                await self._submit_text(pane, continuation)

            return True

        return False

    async def wait_until_ready(self, project: str, timeout: int = 45) -> bool:
        """Wait until the interactive provider seems ready to accept input."""
        info = self._sessions.get(project)
        if info is None:
            return False

        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            return False
        if session is None:
            return False

        pane = session.active_window.active_pane
        poll_interval = max((self._settings.TMUX_POLL_INTERVAL_MS or 300) / 1000.0, 0.1)
        elapsed = 0.0
        tui_provider = (not info.shell_mode) and info.provider in {
            DevProvider.CODEX,
            DevProvider.CLAUDE,
            DevProvider.GEMINI,
            DevProvider.OPENCODE,
            DevProvider.MUSE,
        }
        codex_update_submitted = False
        codex_restart_submitted = False

        while elapsed < timeout:
            content = self._capture_pane(pane)
            lowered = content.lower()

            if info.provider == DevProvider.CODEX:
                if self._is_codex_update_prompt(content) and not codex_update_submitted:
                    logger.info(
                        "Codex update prompt detected in '%s'; confirming auto-update",
                        info.tmux_session_name,
                    )
                    pane.enter()
                    codex_update_submitted = True
                    await asyncio.sleep(max(poll_interval, 0.5))
                    elapsed += max(poll_interval, 0.5)
                    continue

                if codex_update_submitted and not codex_restart_submitted:
                    if self._should_restart_codex_after_update(content):
                        logger.info(
                            "Restarting Codex in '%s' after auto-update",
                            info.tmux_session_name,
                        )
                        self._restart_interactive_command(
                            pane, info.command, info.cwd, info.env)
                        codex_restart_submitted = True
                        await asyncio.sleep(max(poll_interval, 0.8))
                        elapsed += max(poll_interval, 0.8)
                        continue

            if tui_provider:
                # Avoid false positives from shell prompts before the TUI is fully loaded.
                if self._check_idle_tui_ready(content, submitted_text=""):
                    return True
                provider_banner_ready = (
                    ("openai codex" in lowered)
                    or ("muse" in lowered)
                    or ("claude" in lowered)
                    or ("antigravity" in lowered)
                    or ("gemini" in lowered and ("type your message or @path/to/file" in lowered or "? for shortcuts" in lowered))
                    or ("? for shortcuts" in lowered)
                    or ("ask anything..." in lowered and "/status" in lowered)
                )
                if provider_banner_ready and self._check_prompt_ready(content, info.prompt_pattern):
                    return True
            else:
                if self._check_prompt_ready(content, info.prompt_pattern):
                    return True

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return False

    def get_current_output(self, project: str, lines: int = 200) -> str:
        """Capture current pane content (last N lines)."""
        info = self._sessions.get(project)
        if info is None:
            # Try to find by tmux session name directly
            name = self._session_name(project)
            try:
                session = self.server.sessions.get(session_name=name)
            except Exception:
                return ""
            if session is None:
                return ""
            pane = session.active_window.active_pane
            return self._capture_pane(pane, max_lines=lines)

        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            return ""
        if session is None:
            return ""

        pane = session.active_window.active_pane
        return self._capture_pane(pane, max_lines=lines)

    def is_ready(self, project: str) -> bool:
        """Check if the provider in the tmux session is ready for new input."""
        info = self._sessions.get(project)
        if info is None:
            return False

        try:
            session = self.server.sessions.get(session_name=info.tmux_session_name)
        except Exception:
            return False
        if session is None:
            return False

        pane = session.active_window.active_pane
        content = self._capture_pane(pane)
        return self._check_prompt_ready(content, info.prompt_pattern)

    def close_session(self, project: str, tmux_session_name: str | None = None) -> None:
        """Kill the tmux session for a project."""
        info = self._sessions.pop(project, None)
        name = tmux_session_name or (info.tmux_session_name if info else self._session_name(project))

        try:
            session = self.server.sessions.get(session_name=name)
            if session is not None:
                session.kill()
                logger.info("Killed tmux session '%s'", name)
        except Exception as exc:
            logger.warning("Failed to kill tmux session '%s': %s", name, exc)

    def close_all(self) -> None:
        """Kill all openceo-dev tmux sessions."""
        try:
            for session in list(self.server.sessions):
                if session.name.startswith(_TMUX_PREFIX):
                    try:
                        session.kill()
                        logger.info("Killed tmux session '%s'", session.name)
                    except Exception as exc:
                        logger.warning("Failed to kill tmux session '%s': %s", session.name, exc)
        except Exception:
            pass
        self._sessions.clear()

    def list_sessions(self) -> dict[str, dict[str, Any]]:
        """List all openceo-dev tmux sessions with their status."""
        result: dict[str, dict[str, Any]] = {}
        try:
            for session in self.server.sessions:
                if not session.name.startswith(_TMUX_PREFIX):
                    continue
                project = session.name[len(_TMUX_PREFIX):]
                info = self._sessions.get(project)
                pane = session.active_window.active_pane
                content = self._capture_pane(pane, max_lines=5)

                result[project] = {
                    "tmux_session_name": session.name,
                    "provider": info.provider.value if info else "unknown",
                    "is_ready": self._check_prompt_ready(
                        self._capture_pane(pane),
                        info.prompt_pattern if info else None,
                    ),
                    "last_lines": content,
                }
        except Exception:
            pass
        return result

    def all_session_names(self) -> list[str]:
        """Return every tmux session name currently visible to the server."""
        try:
            return [str(session.name) for session in self.server.sessions]
        except Exception:
            return []

    def list_all_host_sessions(self) -> list[dict[str, Any]]:
        """List all tmux sessions on the host system with details and active pane content."""
        results: list[dict[str, Any]] = []
        try:
            for s in self.server.sessions:
                s_name = str(s.name)
                is_openceo = s_name.startswith(_TMUX_PREFIX)
                last_line = ""
                window_count = 1
                try:
                    window_count = len(s.windows)
                    pane = s.active_window.active_pane
                    lines = pane.capture_pane()
                    if lines:
                        # Find last non-empty line
                        for line in reversed(lines):
                            if line.strip():
                                last_line = line.strip()
                                break
                except Exception:
                    pass
                results.append(
                    {
                        "name": s_name,
                        "windows": window_count,
                        "is_openceo_dev": is_openceo,
                        "attached": getattr(s, "attached", "0") != "0",
                        "last_line": last_line,
                    }
                )
        except Exception as exc:
            logger.warning("Failed to list all host tmux sessions: %s", exc)
        return results

    def capture_pane_by_name(self, session_name: str, max_lines: int = 50) -> str | None:
        """Capture the current pane content of any host tmux session."""
        try:
            for s in self.server.sessions:
                if s.name == session_name:
                    pane = s.active_window.active_pane
                    lines = pane.capture_pane()
                    if max_lines and len(lines) > max_lines:
                        lines = lines[-max_lines:]
                    return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to capture pane for '%s': %s", session_name, exc)
        return None

    def send_keys_by_name(self, session_name: str, keys: str, enter: bool = True) -> bool:
        """Send keystrokes into any host tmux session."""
        try:
            for s in self.server.sessions:
                if s.name == session_name:
                    pane = s.active_window.active_pane
                    pane.send_keys(keys, enter=enter)
                    return True
        except Exception as exc:
            logger.warning("Failed to send keys to '%s': %s", session_name, exc)
        return False

    def create_generic_session(
        self,
        session_name: str,
        cwd: str | None = None,
        command: str | None = None,
    ) -> bool:
        """Create a new tmux session on the host."""
        try:
            kwargs: dict[str, Any] = {
                "session_name": session_name,
                "attach": False,
            }
            if cwd and os.path.isdir(cwd):
                kwargs["start_directory"] = cwd
            if command:
                kwargs["window_command"] = command
            self.server.new_session(**kwargs)
            return True
        except Exception as exc:
            logger.warning("Failed to create generic tmux session '%s': %s", session_name, exc)
            return False

    def kill_by_name(self, session_name: str) -> bool:
        """Kill any host tmux session by name."""
        try:
            for s in self.server.sessions:
                if s.name == session_name:
                    s.kill_session()
                    return True
        except Exception as exc:
            logger.warning("Failed to kill tmux session '%s': %s", session_name, exc)
        return False

    def reconcile(self, active_sessions_from_db: list[dict[str, Any]]) -> dict[str, str]:
        """On startup, reconcile tmux sessions with DB records.

        Returns dict of project -> action taken ("reconnected" | "orphan_killed").
        """
        actions: dict[str, str] = {}

        # Build set of projects that DB says should be active
        db_projects: set[str] = set()
        db_providers: dict[str, str] = {}
        db_metadata: dict[str, dict[str, Any]] = {}
        for session in active_sessions_from_db:
            project = str(session.get("project") or "")
            if project:
                db_projects.add(project)
                db_providers[project] = str(session.get("provider") or "auto")
                metadata = session.get("metadata") or {}
                db_metadata[project] = metadata if isinstance(metadata, dict) else {}

        # Check existing tmux sessions
        try:
            for tmux_session in list(self.server.sessions):
                if not tmux_session.name.startswith(_TMUX_PREFIX):
                    continue
                project = tmux_session.name[len(_TMUX_PREFIX):]

                if project in db_projects:
                    # Reconnect: register in our tracking
                    provider_str = db_providers.get(project, "claude")
                    try:
                        provider = DevProvider(provider_str)
                    except ValueError:
                        provider = DevProvider.CLAUDE

                    metadata = db_metadata.get(project) or {}
                    shell_mode = (
                        provider == DevProvider.OPENCODE
                        and bool(metadata.get("opencode_run_mode"))
                    )

                    self._sessions[project] = _SessionInfo(
                        project=project,
                        provider=provider,
                        tmux_session_name=tmux_session.name,
                        command=[],  # Unknown after restart, but session is alive
                        prompt_pattern=_SHELL_PROMPT_PATTERN if shell_mode else _DEFAULT_PROMPT_PATTERNS.get(provider.value),
                        shell_mode=shell_mode,
                    )
                    actions[project] = "reconnected"
                    logger.info("Reconnected tmux session '%s' for project '%s'", tmux_session.name, project)
                else:
                    # Orphan: kill it
                    try:
                        tmux_session.kill()
                        actions[project] = "orphan_killed"
                        logger.info("Killed orphan tmux session '%s'", tmux_session.name)
                    except Exception as exc:
                        logger.warning("Failed to kill orphan tmux session '%s': %s", tmux_session.name, exc)
        except Exception as exc:
            logger.warning("tmux reconciliation failed: %s", exc)

        return actions

    @staticmethod
    def _capture_pane(pane: Any, max_lines: int | None = None) -> str:
        """Capture visible pane content."""
        try:
            history = max_lines if max_lines and max_lines > 0 else 400
            lines = pane.capture_pane(start=-history, end="-", join_wrapped=True)
            if max_lines and len(lines) > max_lines:
                lines = lines[-max_lines:]
            return "\n".join(lines)
        except Exception:
            return ""

    @staticmethod
    def _check_prompt_ready(content: str, pattern: re.Pattern[str] | None) -> bool:
        """Check if recent non-empty lines indicate the CLI is ready for input."""
        if not content.strip():
            return False

        lines = [line.rstrip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False

        tail = lines[-12:]
        recent = tail[-3:]

        if pattern is not None:
            if any(pattern.search(line) for line in recent):
                return True

        # Fallback: common prompt endings
        for line in recent:
            stripped = line.rstrip()
            if stripped.endswith(">") or stripped.endswith("$") or stripped.endswith("%"):
                return True
            if re.match(r"^\s*[›❯>]\s*$", stripped):
                return True

        return False

    @staticmethod
    def _check_idle_tui_ready(content: str, submitted_text: str) -> bool:
        """Heuristic for Codex/Claude TUI readiness when prompt includes placeholder text."""
        if not content.strip():
            return False
        if re.search(r"\bworking\b|\besc to interrupt\b|\besc to cancel\b|\bgenerating\b|\bstarting mcp\b", content, flags=re.IGNORECASE):
            return False

        lines = [line.rstrip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        tail = lines[-20:]

        status_line_found = any(re.search(r"\bgpt-[^·\n]*\s+·\s+", line, flags=re.IGNORECASE) for line in tail)
        prompt_lines = [line for line in tail if re.match(r"^\s*[›❯>]\s+.+$", line)]
        if not prompt_lines:
            return False

        latest_prompt = prompt_lines[-1]
        prompt_text = re.sub(r"^\s*[›❯>]\s*", "", latest_prompt).strip().lower()
        submitted = submitted_text.strip().lower()
        if submitted and prompt_text == submitted:
            return False

        if "openai codex" in content.lower() and status_line_found:
            # Codex shows the banner before the model finishes loading.
            # The model line is inside the TUI frame: │ model: loading ...
            # Wait until it shows an actual model name, not "loading".
            if TmuxRuntime._codex_model_line_loading(content):
                return False
            return True

        lowered = content.lower()
        if ("antigravity" in lowered or "gemini" in lowered) and "? for shortcuts" in lowered:
            return True

        if "ask anything..." in lowered and "/status" in lowered and "tab agents" in lowered:
            return True

        return False

    @staticmethod
    def _codex_model_line_loading(content: str) -> bool:
        """Codex TUI shows its banner before the model finishes loading.

        The model line inside the TUI frame looks like:
            │ model:       loading   /model to change                  │   ← still loading
            │ model:       gpt-5.5 xhigh   /model to change            │   ← ready
        """
        return bool(re.search(r"│\s*model:\s+loading\s", content))

    @staticmethod
    def _is_codex_update_prompt(content: str) -> bool:
        lowered = content.lower()
        return (
            "update available!" in lowered
            and "@openai/codex" in lowered
            and "update now" in lowered
            and "press enter to continue" in lowered
        )

    @staticmethod
    def _should_restart_codex_after_update(content: str) -> bool:
        lowered = content.lower()
        if "update ran successfully! please restart codex." in lowered:
            return True
        if "updating codex via" in lowered:
            return False
        if "openai codex" in lowered:
            return False
        return TmuxRuntime._looks_like_shell_prompt(content)

    @staticmethod
    def _looks_like_shell_prompt(content: str) -> bool:
        if not content.strip():
            return False
        lines = [line.rstrip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        recent = lines[-4:]
        return any(re.search(r"^[^\n]*@[^\n:]+:.*[$#%]\s*$", line) for line in recent)

    @staticmethod
    def _restart_interactive_command(
        pane: Any, command: list[str],
        workspace: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not command:
            return
        from orchestration.agent_sandbox import sandboxed_shell_command
        line = sandboxed_shell_command(
            command=[str(token) for token in command],
            workspace=workspace or os.getcwd(), base=os.environ,
            overlay=dict(env or {}),
            config_args=[str(token) for token in command])
        pane.send_keys(line, enter=True)

    @staticmethod
    def _needs_extra_enter(content: str, submitted_text: str) -> bool:
        text = submitted_text.strip()
        if not text:
            return False
        # Prefix of the first line only: long prompts wrap mid-line in the
        # pane, so even the full first line never matches verbatim and
        # retries would never fire.
        first = re.escape(text.splitlines()[0].strip()[:40])
        if not re.search(rf"^\s*[›❯>]\s*{first}", content, flags=re.MULTILINE):
            return False
        if re.search(r"\bworking\b|\besc to interrupt\b|\besc to cancel\b|\bgenerating\b", content, flags=re.IGNORECASE):
            return False
        return True

    @staticmethod
    def _extract_new_output(before: str, after: str) -> str:
        """Extract output that appeared after sending instruction."""
        before_lines = before.splitlines()
        after_lines = after.splitlines()

        # Find where new content starts
        # Simple approach: skip the common prefix
        common = 0
        for i, (a, b) in enumerate(zip(before_lines, after_lines)):
            if a == b:
                common = i + 1
            else:
                break

        new_lines = after_lines[common:]

        # Strip trailing empty lines and prompt line
        while new_lines and not new_lines[-1].strip():
            new_lines.pop()

        return "\n".join(new_lines)

    @staticmethod
    async def _submit_text(pane: Any, text: str) -> None:
        pane.send_keys(text, enter=False, literal=True)
        # Let the TUI render a long paste before Enter, or the keystroke is
        # swallowed and the prompt sits unsent until the turn times out.
        await asyncio.sleep(max(0.12, min(2.0, len(text) / 800.0)))
        pane.enter()

        # Some TUI states occasionally swallow Enter during startup.
        for _ in range(16):
            await asyncio.sleep(0.5)
            after_send = TmuxRuntime._capture_pane(pane)
            if not TmuxRuntime._needs_extra_enter(after_send, text):
                break
            pane.enter()

    @staticmethod
    def _has_completion_block(content: str, begin_marker: str, end_marker: str) -> bool:
        if not content.strip() or not begin_marker.strip() or not end_marker.strip():
            return False
        block = TmuxRuntime._find_standalone_marked_block(content, begin_marker, end_marker)
        if block is not None:
            body_start, end_idx = block
            body = content[body_start:end_idx]
            if not TmuxRuntime._looks_like_prompt_placeholder(body):
                return True
        begin_idx = TmuxRuntime._find_response_marker_occurrence(content, begin_marker)
        end_idx = TmuxRuntime._find_response_marker_occurrence(content, end_marker)
        if begin_idx < 0 or end_idx <= begin_idx:
            return False
        body = content[begin_idx + len(begin_marker):end_idx]
        return not TmuxRuntime._looks_like_prompt_placeholder(body)

    @staticmethod
    def _find_standalone_marked_block(
        content: str,
        begin_marker: str,
        end_marker: str,
    ) -> tuple[int, int] | None:
        begin_re = re.compile(
            rf"(?m)^[^\S\r\n]*[^\w\r\n]*{re.escape(begin_marker.strip())}[^\w\r\n]*$"
        )
        end_re = re.compile(
            rf"(?m)^[^\S\r\n]*[^\w\r\n]*{re.escape(end_marker.strip())}[^\w\r\n]*$"
        )

        begin_matches = list(begin_re.finditer(content))
        end_matches = list(end_re.finditer(content))
        if not begin_matches or not end_matches:
            return None

        for begin_match in reversed(begin_matches):
            end_match = next((m for m in end_matches if m.start() > begin_match.end()), None)
            if end_match is not None:
                return begin_match.end(), end_match.start()
        return None

    @staticmethod
    def _find_response_marker_occurrence(content: str, marker: str) -> int:
        positions = [match.start() for match in re.finditer(re.escape(marker.strip()), content)]
        if len(positions) < 2:
            return -1
        return positions[1]

    @staticmethod
    def _looks_like_prompt_placeholder(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return True
        if any(normalized.startswith(prefix) for prefix in _VOICE_PLACEHOLDER_PREFIXES):
            return True
        return "marcador inicial exacto:" in normalized or "marcador final exacto:" in normalized

    @staticmethod
    def _is_completion_check_satisfied(completion_check: Callable[[], bool] | None) -> bool:
        if completion_check is None:
            return False
        try:
            return bool(completion_check())
        except Exception as exc:
            logger.debug("Completion check failed: %s", exc)
            return False

    @staticmethod
    def _has_tui_signal(content: str, patterns: list[re.Pattern[str]] | None) -> bool:
        """Check pane content for Tier-S signal patterns (P1 TUI fallback).

        When an AI agent prints the completion signal to the TUI but does NOT
        write it to the .tier-s/status file, this fallback detects the signal
        in the captured pane content so the orchestrator can continue.
        """
        if not patterns or not content.strip():
            return False
        for pattern in patterns:
            if pattern.search(content):
                return True
        return False
