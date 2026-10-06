"""Rate-limit monitor for AI coding-tool subscriptions.

Manages dedicated tmux monitor sessions to poll `/status` (Codex) and `/usage`
(Claude, muse), parses remaining quota, and gates ticket launches based on
reserve thresholds and concurrency slots.
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

from agents.dev.routing import resolved_tool_name
from settings import Settings
from state import TaskStatus

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
# Resets line can be "Resets 6pm (UTC)" or "Resets May 31, 5pm (UTC)"
_CLAUDE_RESETS_RE = re.compile(
    r"Resets\s+.*?(\d{1,2}(?::\d{2})?(?:am|pm))\s*\(UTC\)",
    re.IGNORECASE,
)

# Codex session-id from /status output:  "Session: <uuid>"
_CODEX_SESSION_ID_RE = re.compile(r"Session:\s*([0-9a-f-]{36})", re.IGNORECASE)

# muse /usage subscription box – e.g. "Current  0% used · Resets at 4:54 PM"
# and "Weekly  4% used · Resets Oct 5 at 12:00 AM" (verified 2026-10-04).
_MUSE_CURRENT_RE = re.compile(
    r"Current\s+(\d+)%\s+used\W+Resets?\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)
_MUSE_WEEKLY_RE = re.compile(
    r"Weekly\s+(\d+)%\s+used\W+Resets?\s+(\w+\s+\d{1,2})\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)

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
        # Codex: measure the codexj account (owner 2026-10-04), the one
        # Tier S actually spends — never the default codex login. No extra
        # flags: the wrapper already carries bypass + model + effort, and a
        # -c override wedges the TUI behind a blocking warning (00086).
        if provider == "codex":
            cmd = self._settings.CODEXJ_CLI_PATH
        elif provider == "claude":
            path = self._settings.CLAUDE_CODE_PATH
            cmd = f"{path} --dangerously-skip-permissions"
        elif provider == "muse":
            # Dev default (musej): the monitor only runs /usage, no tools,
            # so the wrapper flags are inert and the same account is measured.
            cmd = self._settings.MUSE_CLI_PATH
        else:
            raise ValueError(f"No monitor support for provider: {provider}")

        session = self.server.new_session(session_name=name)
        pane = session.active_window.active_pane
        pane.send_keys(cmd, enter=True)
        logger.info("Created rate-monitor tmux session '%s'", name)
        # Let the CLI finish booting before we send slash commands.
        # Codex model loading takes ~25-30s even without MCP servers;
        # muse TUI takes ~45s (measured 2026-10-04).
        if provider == "muse":
            boot_wait = 50
        else:
            boot_wait = 30 if provider == "codex" else 8
        time.sleep(boot_wait)
        if provider == "codex":
            # Dismiss any blocking startup notice so Enter submits.
            pane.send_keys("Escape")
            time.sleep(2)
        return session

    def _send_to_monitor(self, provider: str, text: str) -> None:
        session = self._get_or_create_monitor_session(provider)
        pane = session.active_window.active_pane
        if provider == "muse":
            # muse TUI ignores send_keys(..., enter=True): literal + explicit
            # enter, same mechanism tmux_runtime uses for builders.
            pane.send_keys(text, enter=False, literal=True)
            time.sleep(1)
            pane.enter()
        else:
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
        for provider in ("codex", "claude", "muse"):
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

        # Scrollback may hold several /status outputs: the last one is freshest.
        pct_matches = _CODEX_PCT_RE.findall(content)
        if len(pct_matches) >= 2:
            status.remaining_5h = int(pct_matches[-2]) / 100.0
            status.remaining_weekly = int(pct_matches[-1]) / 100.0
        elif len(pct_matches) == 1:
            status.remaining_5h = int(pct_matches[-1]) / 100.0

        m5h_list = _CODEX_RESETS_5H_RE.findall(content)
        if m5h_list:
            status.resets_5h_at = RateMonitor._codex_time_to_utc(m5h_list[-1], now)

        mw_list = _CODEX_RESETS_WEEKLY_RE.findall(content)
        if mw_list:
            status.resets_weekly_at = RateMonitor._codex_weekly_to_utc(
                mw_list[-1][0], mw_list[-1][1], now
            )

        sid_list = _CODEX_SESSION_ID_RE.findall(content)
        if sid_list:
            status.codex_session_id = sid_list[-1]

        status.updated_at = now
        return status

    @staticmethod
    def _parse_claude_usage(content: str) -> ProviderStatus:
        """Parse Claude `/usage` output (shows % used – invert to remaining)."""
        status = ProviderStatus(provider="claude")
        now = datetime.now(timezone.utc)

        # Scrollback may hold several /usage outputs: the last one is freshest.
        # Each output yields 3 matches (session, week, fable); map session→5h.
        used_matches = _CLAUDE_USED_RE.findall(content)
        if len(used_matches) >= 3:
            status.remaining_5h = max(0.0, 1.0 - int(used_matches[-3]) / 100.0)
            status.remaining_weekly = max(0.0, 1.0 - int(used_matches[-2]) / 100.0)
        elif len(used_matches) == 2:
            status.remaining_5h = max(0.0, 1.0 - int(used_matches[-2]) / 100.0)
            status.remaining_weekly = max(0.0, 1.0 - int(used_matches[-1]) / 100.0)
        elif len(used_matches) == 1:
            status.remaining_5h = max(0.0, 1.0 - int(used_matches[-1]) / 100.0)

        reset_matches = _CLAUDE_RESETS_RE.findall(content)
        if len(reset_matches) >= 2:
            status.resets_5h_at = RateMonitor._claude_time_to_utc(reset_matches[-2], now)
            status.resets_weekly_at = RateMonitor._claude_time_to_utc(reset_matches[-1], now)
        elif len(reset_matches) == 1:
            status.resets_5h_at = RateMonitor._claude_time_to_utc(reset_matches[-1], now)

        status.updated_at = now
        return status

    @staticmethod
    def _parse_muse_usage(content: str) -> ProviderStatus:
        """Parse muse `/usage` subscription box (Current + Weekly % used)."""
        status = ProviderStatus(provider="muse")
        now = datetime.now(timezone.utc)

        # Scrollback may hold several outputs: the last one is freshest.
        cur_list = _MUSE_CURRENT_RE.findall(content)
        if cur_list:
            used, reset = cur_list[-1]
            status.remaining_5h = max(0.0, 1.0 - int(used) / 100.0)
            status.resets_5h_at = RateMonitor._muse_current_to_utc(reset, now)

        wk_list = _MUSE_WEEKLY_RE.findall(content)
        if wk_list:
            used, date_part, time_part = wk_list[-1]
            status.remaining_weekly = max(0.0, 1.0 - int(used) / 100.0)
            status.resets_weekly_at = RateMonitor._muse_weekly_to_utc(
                date_part, time_part, now
            )

        status.updated_at = now
        return status

    # ------------------------------------------------------------------
    # Time normalisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _muse_current_to_utc(time_str: str, now: datetime) -> datetime:
        """muse shows server-local time (UTC here), e.g. '4:54 PM'."""
        try:
            m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_str.strip(), re.IGNORECASE)
            if not m:
                return now + timedelta(hours=5)
            hour = int(m.group(1))
            minute = int(m.group(2))
            if m.group(3).lower() == "pm" and hour != 12:
                hour += 12
            elif m.group(3).lower() == "am" and hour == 12:
                hour = 0
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        except Exception:
            return now + timedelta(hours=5)

    @staticmethod
    def _muse_weekly_to_utc(date_str: str, time_str: str, now: datetime) -> datetime:
        """muse weekly: 'Oct 5' + '12:00 AM', server-local (UTC here)."""
        try:
            parts = date_str.strip().split()
            month_map = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            }
            month = month_map.get(parts[0].lower()[:3], now.month)
            day = int(parts[1])
            m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_str.strip(), re.IGNORECASE)
            if not m:
                return now + timedelta(days=7)
            hour = int(m.group(1))
            minute = int(m.group(2))
            if m.group(3).lower() == "pm" and hour != 12:
                hour += 12
            elif m.group(3).lower() == "am" and hour == 12:
                hour = 0
            candidate = datetime(now.year, month, day, hour, minute, tzinfo=timezone.utc)
            if candidate < now - timedelta(days=1):
                candidate = candidate.replace(year=now.year + 1)
            return candidate
        except Exception:
            return now + timedelta(days=7)

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
        """Claude shows reset time in UTC like '6pm' or '6:20pm'."""
        try:
            ampm_str = ampm_str.strip().lower()
            m = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)", ampm_str)
            if not m:
                return now + timedelta(hours=5)
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.group(2) else 0
            is_pm = m.group(3) == "pm"
            if is_pm and hour != 12:
                hour += 12
            elif not is_pm and hour == 12:
                hour = 0
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
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
                # The TUI sometimes eats the first Enter (autocomplete popup):
                # a second Enter submits, while a stray empty submit is harmless.
                self._send_to_monitor(provider, "")
            elif provider == "claude":
                self._send_to_monitor(provider, "/usage")
            elif provider == "muse":
                self._send_to_monitor(provider, "/usage")
            else:
                # unsupported – return full availability
                return ProviderStatus(provider=provider)

            # Wait for the CLI to respond
            await asyncio.sleep(3)
            content = self._capture_monitor(provider, lines=40)
            missing_output = (
                (provider == "codex" and "left" not in content.lower())
                or (provider == "muse" and "resets" not in content.lower())
            )
            if missing_output:
                # Command did not execute — one more Enter before giving up.
                self._send_to_monitor(provider, "")
                await asyncio.sleep(3)
                content = self._capture_monitor(provider, lines=60)

            if provider == "codex":
                status = self._parse_codex_status(content)
            elif provider == "muse":
                status = self._parse_muse_usage(content)
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
        # Gemini, OpenCode, GLM, Kimi – always available (no tracking)
        if provider not in ("codex", "claude", "muse"):
            return True

        status = await self.get_status(provider)
        if provider == "codex":
            reserve_5h = self._settings.RATE_CODEX_RESERVE_5H
            reserve_weekly = self._settings.RATE_CODEX_RESERVE_WEEKLY
        elif provider == "muse":
            reserve_5h = self._settings.RATE_MUSE_RESERVE_5H
            reserve_weekly = self._settings.RATE_MUSE_RESERVE_WEEKLY
        else:
            reserve_5h = self._settings.RATE_CLAUDE_RESERVE_5H
            reserve_weekly = self._settings.RATE_CLAUDE_RESERVE_WEEKLY
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
                tool = resolved_tool_name(t)
                if tool == "tier_s":
                    used_slots += self._settings.RATE_TIER_S_WEIGHT
                else:
                    used_slots += self._settings.RATE_NORMAL_WEIGHT

            ticket_tool = resolved_tool_name(ticket)
            needed = (
                self._settings.RATE_TIER_S_WEIGHT
                if ticket_tool == "tier_s"
                else self._settings.RATE_NORMAL_WEIGHT
            )
            if used_slots + needed > self._settings.RATE_MAX_CONCURRENT_TICKETS:
                # No slots → retry in 2 min (a ticket should finish soon)
                return False, datetime.now(timezone.utc) + timedelta(minutes=2)

        # 2. Quota limits per tier (design v2 §4.3, F0.1): Tier S needs the
        # scarce Claude + Codex accounts before the cycle starts; Tier A/B
        # need theirs. Tier C always launches: muse is checked at chain
        # selection and the dsh/claudep links are reactive.
        ticket_tool = resolved_tool_name(ticket)
        providers_to_check: list[str] = []
        if ticket_tool == "tier_s":
            providers_to_check = ["claude", "codex", "muse"]
        elif ticket_tool == "tier_a":
            providers_to_check = ["claude"]
        elif ticket_tool == "tier_b":
            providers_to_check = ["codex"]
        else:
            # tier_c / unknown – always OK
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
        tool = resolved_tool_name(ticket)
        if tool == "tier_s":
            await self.refresh_status("claude")
            await self.refresh_status("codex")
            await self.refresh_status("muse")
        elif tool == "tier_a":
            await self.refresh_status("claude")
        elif tool == "tier_b":
            await self.refresh_status("codex")
        elif tool == "tier_c":
            await self.refresh_status("muse")

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
