"""Rate-limit monitor for AI coding-tool subscriptions.

Manages dedicated tmux monitor sessions to poll `/status` (Codex) and `/usage`
(Claude), parses remaining quota, and gates ticket launches based on reserve
thresholds and concurrency slots.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import libtmux

from settings import Settings
from state import CodingTool, TaskStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monitor tmux session names (never share with working sessions)
# ---------------------------------------------------------------------------
_MONITOR_PREFIX = "dev-monitor-"

# ---------------------------------------------------------------------------
# Parse patterns
# ---------------------------------------------------------------------------

# Codex /status  – e.g. "5h limit:    [████████████████████] 100% left (resets 08:40)"
_CODEX_PCT_RE = re.compile(r"(\d+)%\s+left", re.IGNORECASE)
_CODEX_RESETS_5H_RE = re.compile(
    r"5h\s+limit.*resets\s+(\d{1,2}:\d{2})", re.IGNORECASE
)
_CODEX_RESETS_WEEKLY_RE = re.compile(
    r"weekly\s+limit.*resets\s+(\d{1,2}:\d{2})\s+on\s+(\d{1,2}\s+\w+)", re.IGNORECASE
)

# Claude /usage  – e.g. "██████████… 55% used"
_CLAUDE_USED_RE = re.compile(r"(\d+)%\s+used", re.IGNORECASE)
_CLAUDE_RESETS_RE = re.compile(r"Resets\s+(\d{1,2}(?:am|pm))\s*\(UTC\)", re.IGNORECASE)

# Codex session-id from /status output:  "Session: <uuid>"
_CODEX_SESSION_ID_RE = re.compile(r"Session:\s*([0-9a-f-]{36})", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Rate-limit trigger patterns (used by the polling loops in tmux_runtime)
# ---------------------------------------------------------------------------
RATE_LIMIT_PATTERNS: dict[str, dict[str, list[re.Pattern[str]]]] = {
    "claude": {
        "trigger": [
            re.compile(r"What do you want to do\?", re.IGNORECASE),
            re.compile(r"Stop and wait for limit to reset", re.IGNORECASE),
        ],
        "reset_time": [
            re.compile(r"Resets\s+(\d{1,2}(?:am|pm))\s*\(UTC\)", re.IGNORECASE),
        ],
        "used_percent": [
            re.compile(r"(\d+)%\s+used", re.IGNORECASE),
        ],
    },
    "codex": {
        "trigger": [
            re.compile(r"You've reached your 5-hour message limit", re.IGNORECASE),
            re.compile(r"reached your.*limit", re.IGNORECASE),
        ],
        "reset_time": [
            re.compile(r"Try again in\s+(\d+h\s*\d+m)", re.IGNORECASE),
        ],
        "remaining_percent": [
            re.compile(r"(\d+)%\s+left", re.IGNORECASE),
        ],
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ProviderStatus:
    provider: str  # "codex" | "claude"
    remaining_5h: float = 1.0  # 0.0 – 1.0
    remaining_weekly: float = 1.0
    resets_5h_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resets_weekly_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    codex_session_id: str | None = None  # persisted for crash recovery


# ---------------------------------------------------------------------------
# Main monitor class
# ---------------------------------------------------------------------------
class RateMonitor:
    """Singleton-ish service that tracks rate-limit state for coding providers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._server: libtmux.Server | None = None
        self._cache: dict[str, ProviderStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Optional reference to MongoRepository – set after wiring
        self._repo: Any = None

    def set_repo(self, repo: Any) -> None:
        self._repo = repo

    @property
    def server(self) -> libtmux.Server:
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    # ------------------------------------------------------------------
    # Monitor session management
    # ------------------------------------------------------------------

    def _monitor_session_name(self, provider: str) -> str:
        return f"{_MONITOR_PREFIX}{provider}"

    def _get_or_create_monitor_session(self, provider: str) -> libtmux.Session:
        name = self._monitor_session_name(provider)
        try:
            session = self.server.sessions.get(session_name=name)
            if session is not None:
                return session
        except Exception:
            pass

        # Monitor sessions need interactive REPLs so slash commands
        # (/usage, /status) work. DEFAULT_ARGS may contain -p for
        # batch dev sessions — strip it and use interactive-only flags.
        if provider == "codex":
            path = self._settings.CODEX_CLI_PATH
            # Start interactive; never pass -p/--print
            cmd = f"{path} --dangerously-bypass-approvals-and-sandbox"
        elif provider == "claude":
            path = self._settings.CLAUDE_CODE_PATH
            cmd = f"{path} --dangerously-skip-permissions"
        else:
            raise ValueError(f"No monitor support for provider: {provider}")

        session = self.server.new_session(session_name=name)
        pane = session.active_window.active_pane
        pane.send_keys(cmd, enter=True)
        logger.info("Created rate-monitor tmux session '%s'", name)
        # Let the CLI finish booting before we send slash commands.
        # Codex MCP servers take ~40-50s; Claude initializes in ~5-8s.
        boot_wait = 45 if provider == "codex" else 8
        time.sleep(boot_wait)
        return session

    def _send_to_monitor(self, provider: str, text: str) -> None:
        session = self._get_or_create_monitor_session(provider)
        pane = session.active_window.active_pane
        pane.send_keys(text, enter=True)
        # Give the CLI a moment to process the slash command
        time.sleep(3)

    def _capture_monitor(self, provider: str, lines: int = 80) -> str:
        name = self._monitor_session_name(provider)
        try:
            session = self.server.sessions.get(session_name=name)
        except Exception:
            return ""
        if session is None:
            return ""
        pane = session.active_window.active_pane
        try:
            raw = pane.capture_pane(start=-lines, end="-", join_wrapped=True)
            return "\n".join(raw)
        except Exception:
            return ""

    def close_monitor_sessions(self) -> None:
        for provider in ("codex", "claude"):
            name = self._monitor_session_name(provider)
            try:
                session = self.server.sessions.get(session_name=name)
                if session is not None:
                    session.kill()
                    logger.info("Killed monitor session '%s'", name)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_codex_status(content: str) -> ProviderStatus:
        """Parse the two-line output from Codex `/status`."""
        status = ProviderStatus(provider="codex")
        now = datetime.now(timezone.utc)

        pct_matches = _CODEX_PCT_RE.findall(content)
        if len(pct_matches) >= 1:
            status.remaining_5h = int(pct_matches[0]) / 100.0
        if len(pct_matches) >= 2:
            status.remaining_weekly = int(pct_matches[1]) / 100.0

        m5h = _CODEX_RESETS_5H_RE.search(content)
        if m5h:
            status.resets_5h_at = RateMonitor._codex_time_to_utc(m5h.group(1), now)

        mw = _CODEX_RESETS_WEEKLY_RE.search(content)
        if mw:
            status.resets_weekly_at = RateMonitor._codex_weekly_to_utc(
                mw.group(1), mw.group(2), now
            )

        sid = _CODEX_SESSION_ID_RE.search(content)
        if sid:
            status.codex_session_id = sid.group(1)

        status.updated_at = now
        return status

    @staticmethod
    def _parse_claude_usage(content: str) -> ProviderStatus:
        """Parse Claude `/usage` output (shows % used – invert to remaining)."""
        status = ProviderStatus(provider="claude")
        now = datetime.now(timezone.utc)

        used_matches = _CLAUDE_USED_RE.findall(content)
        if len(used_matches) >= 1:
            status.remaining_5h = max(0.0, 1.0 - int(used_matches[0]) / 100.0)
        if len(used_matches) >= 2:
            status.remaining_weekly = max(0.0, 1.0 - int(used_matches[1]) / 100.0)

        reset_matches = _CLAUDE_RESETS_RE.findall(content)
        if len(reset_matches) >= 1:
            status.resets_5h_at = RateMonitor._claude_time_to_utc(reset_matches[0], now)
        if len(reset_matches) >= 2:
            status.resets_weekly_at = RateMonitor._claude_time_to_utc(reset_matches[1], now)

        status.updated_at = now
        return status

    # ------------------------------------------------------------------
    # Time normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _codex_time_to_utc(time_str: str, now: datetime) -> datetime:
        """Codex shows local time (UTC-4). Convert HH:MM local → UTC datetime."""
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
        except (ValueError, AttributeError):
            return now + timedelta(hours=5)

        local_offset = timedelta(hours=-4)
        local_tz = timezone(local_offset)
        local_now = now.astimezone(local_tz)
        candidate = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc).replace(tzinfo=timezone.utc)

    @staticmethod
    def _codex_weekly_to_utc(time_str: str, date_str: str, now: datetime) -> datetime:
        """Codex weekly: 'HH:MM' + 'DD Mon'. Convert to UTC."""
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
        except (ValueError, AttributeError):
            return now + timedelta(days=7)

        local_offset = timedelta(hours=-4)
        local_tz = timezone(local_offset)

        # Parse "18 Mar" style
        parts = date_str.strip().split()
        if len(parts) == 2:
            day = int(parts[0])
            month_map = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            }
            month = month_map.get(parts[1].lower()[:3], now.month)
            year = now.year
            candidate = datetime(year, month, day, hh, mm, tzinfo=local_tz)
            if candidate < now.astimezone(local_tz) - timedelta(days=1):
                candidate = candidate.replace(year=year + 1)
            return candidate.astimezone(timezone.utc).replace(tzinfo=timezone.utc)

        return now + timedelta(days=7)

    @staticmethod
    def _claude_time_to_utc(ampm_str: str, now: datetime) -> datetime:
        """Claude shows reset time in UTC like '4am' or '2pm'."""
        try:
            ampm_str = ampm_str.strip().lower()
            m = re.match(r"(\d{1,2})(am|pm)", ampm_str)
            if not m:
                return now + timedelta(hours=5)
            hour = int(m.group(1))
            is_pm = m.group(2) == "pm"
            if is_pm and hour != 12:
                hour += 12
            elif not is_pm and hour == 12:
                hour = 0
            candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        except Exception:
            return now + timedelta(hours=5)

    # ------------------------------------------------------------------
    # Refresh / cache
    # ------------------------------------------------------------------

    async def refresh_status(self, provider: str) -> ProviderStatus:
        """Poll the monitor session for fresh limit data."""
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            if provider == "codex":
                self._send_to_monitor(provider, "/status")
            elif provider == "claude":
                self._send_to_monitor(provider, "/usage")
            else:
                # unsupported – return full availability
                return ProviderStatus(provider=provider)

            # Wait for the CLI to respond
            await asyncio.sleep(3)
            content = self._capture_monitor(provider, lines=40)

            if provider == "codex":
                status = self._parse_codex_status(content)
            else:
                status = self._parse_claude_usage(content)

            self._cache[provider] = status

            # Persist to Mongo (fire-and-forget)
            if self._repo is not None:
                try:
                    await self._repo.agent_status.update_one(
                        {"provider": provider},
                        {"$set": {
                            "provider": provider,
                            "remaining_5h": status.remaining_5h,
                            "remaining_weekly": status.remaining_weekly,
                            "resets_5h_at": status.resets_5h_at,
                            "resets_weekly_at": status.resets_weekly_at,
                            "codex_session_id": status.codex_session_id,
                            "updated_at": status.updated_at,
                        }},
                        upsert=True,
                    )
                except Exception:
                    logger.debug("Failed to persist rate status to Mongo", exc_info=True)

            logger.info(
                "Rate status [%s]: 5h=%.0f%% weekly=%.0f%% resets_5h=%s",
                provider,
                status.remaining_5h * 100,
                status.remaining_weekly * 100,
                status.resets_5h_at.isoformat(),
            )
            return status

    async def get_status(self, provider: str) -> ProviderStatus:
        """Return cached status; refresh if stale."""
        cached = self._cache.get(provider)
        stale_seconds = self._settings.RATE_STATUS_STALE_SECONDS
        now = datetime.now(timezone.utc)
        if cached is not None and (now - cached.updated_at).total_seconds() < stale_seconds:
            return cached
        return await self.refresh_status(provider)

    # ------------------------------------------------------------------
    # Launch gating
    # ------------------------------------------------------------------

    async def can_launch(self, provider: str) -> bool:
        """Check if we have enough quota to launch a new ticket on this provider."""
        # Gemini, OpenCode – always available (no tracking)
        if provider not in ("codex", "claude"):
            return True

        status = await self.get_status(provider)
        reserve_5h = (
            self._settings.RATE_CODEX_RESERVE_5H
            if provider == "codex"
            else self._settings.RATE_CLAUDE_RESERVE_5H
        )
        reserve_weekly = (
            self._settings.RATE_CODEX_RESERVE_WEEKLY
            if provider == "codex"
            else self._settings.RATE_CLAUDE_RESERVE_WEEKLY
        )
        return status.remaining_5h > reserve_5h and status.remaining_weekly > reserve_weekly

    async def can_launch_ticket(
        self,
        ticket: dict[str, Any],
        active_tickets: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, datetime | None]:
        """Full pre-launch check: concurrency + provider limits.

        Returns (True, None) if the ticket can launch now.
        Returns (False, resume_at) if it should wait.
        """
        # 1. Concurrency check
        if active_tickets is not None:
            used_slots = 0
            for t in active_tickets:
                tool = t.get("coding_tool", "")
                if tool == "tier_s":
                    used_slots += self._settings.RATE_TIER_S_WEIGHT
                else:
                    used_slots += self._settings.RATE_NORMAL_WEIGHT

            ticket_tool = ticket.get("coding_tool", "")
            needed = (
                self._settings.RATE_TIER_S_WEIGHT
                if ticket_tool == "tier_s"
                else self._settings.RATE_NORMAL_WEIGHT
            )
            if used_slots + needed > self._settings.RATE_MAX_CONCURRENT_TICKETS:
                # No slots → retry in 2 min (a ticket should finish soon)
                return False, datetime.now(timezone.utc) + timedelta(minutes=2)

        # 2. Provider limits
        ticket_tool = ticket.get("coding_tool", "")
        providers_to_check: list[str] = []
        if ticket_tool == "tier_s":
            providers_to_check = ["claude", "codex"]
        elif ticket_tool in ("codex", "claude"):
            providers_to_check = [ticket_tool]
        else:
            # gemini / opencode / unknown – always OK
            return True, None

        earliest_reset: datetime | None = None
        for provider in providers_to_check:
            if not await self.can_launch(provider):
                status = self._cache.get(provider)
                if status is not None:
                    reset = min(status.resets_5h_at, status.resets_weekly_at)
                    if earliest_reset is None or reset < earliest_reset:
                        earliest_reset = reset

        if earliest_reset is not None:
            # Add 2-min safety margin
            return False, earliest_reset + timedelta(minutes=2)

        return True, None

    async def on_ticket_completed(self, ticket: dict[str, Any]) -> None:
        """Refresh limits after a ticket finishes. Called by task_service."""
        tool = ticket.get("coding_tool", "")
        if tool == "tier_s":
            await self.refresh_status("claude")
            await self.refresh_status("codex")
        elif tool in ("codex", "claude"):
            await self.refresh_status(tool)

    # ------------------------------------------------------------------
    # Rate-limit detection helpers (used by tmux_runtime polling)
    # ------------------------------------------------------------------

    @staticmethod
    def detect_rate_limit(content: str, provider: str) -> dict[str, Any] | None:
        """Check capture-pane content for rate limit patterns.

        Returns a dict with detection info, or None if no rate limit detected.
        """
        patterns = RATE_LIMIT_PATTERNS.get(provider)
        if patterns is None:
            return None

        triggered = any(p.search(content) for p in patterns["trigger"])
        if not triggered:
            return None

        result: dict[str, Any] = {"provider": provider, "triggered": True}

        # Extract reset time
        for p in patterns.get("reset_time", []):
            m = p.search(content)
            if m:
                result["reset_time_raw"] = m.group(1)
                break

        # Extract percentage
        for key in ("used_percent", "remaining_percent"):
            for p in patterns.get(key, []):
                m = p.search(content)
                if m:
                    result[key] = int(m.group(1))
                    break

        return result

    @staticmethod
    def compute_wait_seconds(detection: dict[str, Any], provider: str) -> int:
        """Compute seconds to wait based on parsed rate-limit detection."""
        raw = detection.get("reset_time_raw", "")
        now = datetime.now(timezone.utc)

        if provider == "claude":
            reset_utc = RateMonitor._claude_time_to_utc(raw, now)
            delta = (reset_utc - now).total_seconds()
        elif provider == "codex":
            # Format: "3h 45m" or similar
            hours, minutes = 0, 0
            hm = re.match(r"(\d+)h\s*(\d+)m", raw)
            if hm:
                hours = int(hm.group(1))
                minutes = int(hm.group(2))
            delta = hours * 3600 + minutes * 60
        else:
            delta = 5 * 3600  # fallback: 5h

        # Minimum 60s, add 120s safety margin
        return max(60, int(delta)) + 120

    @staticmethod
    def detect_process_crash(content: str) -> bool:
        """Detect if the TUI process died and we see a shell prompt instead."""
        if not content.strip():
            return False
        lines = [l.rstrip() for l in content.splitlines() if l.strip()]
        if not lines:
            return False
        recent = lines[-4:]
        return any(re.search(r"^[^\n]*@[^\n:]+:.*[$#%]\s*$", line) for line in recent)
