"""Live CLI executors shared by the engines (dispatch wiring, F3–F6).

:class:`ProcessExecutor` runs every engine role as a one-shot OS process:
role and pool commands come from the global config (``command`` +
``headless_args``), the prompt travels per ``prompt_via``, and the report or
signal is read back from stdout / ``.tier-s/status``. No tmux, no lingering
sessions: each call starts and reaps exactly one process.

:class:`RateMonitorQuota` adapts the service :class:`RateMonitor` to the
engine :class:`QuotaChecker` protocol, mapping accounts to monitored
providers. Unknown/reactive accounts fail open (tried until they fail).

Home of the executor result/quota types (TASK-2026-00141): both the Tier S
cycle and the objectives engine import them from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from orchestration.agent_sandbox import cleanup_sandbox_scratch, plan_agent
from research.runtimes.base import IsolationUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecResult:
    """What an executor reports for one role run."""

    returncode: int
    elapsed_s: float
    signal: str | None = None  # last signal read from .tier-s/status
    usage: dict[str, Any] | None = None
    output_tail: str = ""
    model: str = ""
    slot: str = ""


@dataclass(frozen=True)
class QuotaVerdict:
    ok: bool
    resume_at: str | None = None  # ISO-8601 UTC when quota returns
    detail: str = ""


class Executor(Protocol):
    """Runs one role. Implementations own the CLI session lifecycle."""

    async def run(
        self,
        role: str,
        prompt: str,
        workdir: Path,
        timeout_s: int,
    ) -> ExecResult: ...

    async def cleanup(self, role: str) -> None:
        """Kill the role session. Nothing stays alive between cycles."""


class QuotaChecker(Protocol):
    async def check(
        self, accounts: Sequence[str]) -> dict[str, QuotaVerdict]: ...


class AllowAllQuota:
    """Quota checker for tests and quota-less accounts."""

    async def check(
        self, accounts: Sequence[str]) -> dict[str, QuotaVerdict]:
        return {account: QuotaVerdict(ok=True) for account in accounts}

#: stdout kept per role call: full reviewer reports fit; huge dumps are cut.
OUTPUT_CAP_CHARS = 40000

#: Conventional timeout exit code (telemetry only; engines treat != 0 alike).
TIMEOUT_RETURNCODE = 124


class ProcessExecutor:
    """One-shot process executor over global role/pool commands."""

    def __init__(
        self,
        roles: Mapping[str, Any] | None = None,
        pool: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self._roles = dict(roles or {})
        self._pool = {str(slot.get("label")): dict(slot)
                      for slot in (pool or []) if slot.get("label")}

    # -- Executor protocol -------------------------------------------------

    async def run(
        self,
        role: str,
        prompt: str,
        workdir: Path,
        timeout_s: int,
    ) -> ExecResult:
        spec, via, extra_env = self._resolve(role)
        if spec is None:
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"unknown live role: {role}")
        config_argv = [str(token) for token in spec.get("command", [])]
        config_argv += [str(token) for token in spec.get("headless_args", [])]
        config_argv += [str(token) for token in spec.get("model_args", [])]
        if not config_argv:
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"role {role} has no command")
        stdin_payload: str | None = None
        full_argv = list(config_argv)
        if via == "stdin":
            stdin_payload = prompt
        else:
            full_argv.append(prompt)
        overlay = {**extra_env, **{
            str(k): str(v)
            for k, v in dict(spec.get("env") or {}).items()}}
        if role.startswith("review:"):
            overlay["GIT_OPTIONAL_LOCKS"] = "0"
        model = str(spec.get("model") or "")
        slot = str(spec.get("account") or spec.get("label") or role)
        try:
            plan = plan_agent(argv=full_argv, workspace=workdir,
                              base=os.environ, overlay=overlay,
                              config_args=config_argv)
        except (IsolationUnavailable, ValueError, OSError) as exc:
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"sandbox refused: {exc}",
                model=model, slot=slot)
        started = time.monotonic()
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *plan.argv,
                    stdin=(asyncio.subprocess.PIPE
                           if stdin_payload is not None
                           else asyncio.subprocess.DEVNULL),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=plan.cwd,
                    env=plan.env,
                )
            except FileNotFoundError:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=f"command not found: {plan.argv[0]}",
                    model=model, slot=slot)
            except OSError as exc:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=str(exc)[:2000],
                    model=model, slot=slot)
            try:
                raw_out, raw_err = await asyncio.wait_for(
                    process.communicate(
                        stdin_payload.encode("utf-8")
                        if stdin_payload is not None else None),
                    timeout=max(1, timeout_s),
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return ExecResult(
                    returncode=TIMEOUT_RETURNCODE,
                    elapsed_s=float(timeout_s),
                    output_tail=(f"timeout after {timeout_s}s: "
                                 f"{' '.join(config_argv[:3])}"),
                    model=model, slot=slot,
                    usage={"timeout": True})
            stdout = (raw_out or b"").decode("utf-8", errors="replace")
            stderr = (raw_err or b"").decode("utf-8", errors="replace")
            text = stdout.strip() or stderr.strip()
            truncated = len(text) > OUTPUT_CAP_CHARS
            signal = self._read_signal(workdir, text)
            return ExecResult(
                returncode=int(process.returncode or 0),
                elapsed_s=time.monotonic() - started,
                signal=signal,
                usage={"stdout_chars": len(stdout),
                       "stderr_chars": len(stderr),
                       "truncated": truncated},
                output_tail=text[:OUTPUT_CAP_CHARS],
                model=model, slot=slot)
        finally:
            cleanup_sandbox_scratch(workdir)

    async def cleanup(self, role: str) -> None:
        """One-shot processes own nothing between calls."""

    # -- resolution ----------------------------------------------------------

    def _resolve(
        self, role: str,
    ) -> tuple[dict[str, Any] | None, str, dict[str, str]]:
        if role.startswith("review:"):
            label = role[len("review:"):]
            spec = self._pool.get(label)
            if spec is None:
                return None, "argv", {}
            via = str(spec.get("prompt_via") or "argv")
            return spec, via, {}
        for key in (role, role.replace("_", "-"), role.replace("-", "_")):
            spec = self._roles.get(key)
            if isinstance(spec, Mapping):
                via = str(spec.get("prompt_via") or "argv")
                return dict(spec), via, {}
        return None, "argv", {}

    @staticmethod
    def _read_signal(workdir: Path, stdout_text: str) -> str | None:
        """Last ``.tier-s/status`` line, else a trailing ``REVIEW_DONE_*``."""
        try:
            status = (workdir / ".tier-s" / "status").read_text(
                encoding="utf-8", errors="replace")
            lines = [line.strip() for line in status.splitlines()
                     if line.strip()]
            if lines:
                return lines[-1][:64]
        except OSError:
            pass
        for line in reversed(stdout_text.splitlines()):
            stripped = line.strip()
            if stripped.startswith("REVIEW_DONE_"):
                return stripped[:64]
        return None


def _blocking_resume(status: Any) -> datetime:
    """Reset instant of the window(s) blocking launch (TASK-2026-00166).

    This gate consumes into the reserve (floors 0/0), so a window
    blocks only when really exhausted. One window exhausted: its own
    reset. Both: the later reset — the earliest instant both may have
    replenished. Neither (``can_launch`` raced a replenish between its
    read and ours): the earlier reset, a fail-safe early wake.
    """
    resets = []
    if status.remaining_5h <= 0:
        resets.append(status.resets_5h_at)
    if status.remaining_weekly <= 0:
        resets.append(status.resets_weekly_at)
    if len(resets) == 2:
        return max(resets)
    if resets:
        return resets[0]
    return min(status.resets_5h_at, status.resets_weekly_at)


class RateMonitorQuota:
    """Quota gates over the service rate monitor (design §4.3).

    Every consumer of this gate is the Tier S family: the S cycle
    preflight (planner + plan reviewer), the S arbiter and the objective
    final-review arbiter. Policy §28 reserves quota FOR that family, so
    these checks consume into the reserve down to zero
    (``honor_reserve=False``); only real exhaustion parks them. A/B
    admission and the Tier C primary link are not routed through here:
    they honor the reserve in ``RateMonitor`` itself (TASK-2026-00178).
    """

    #: Account → monitored provider. The claude monitor watches the default
    #: account (clauded), so claudej gates are approximate until the monitor
    #: measures per account (§20-9); codex already measures codexj.
    ACCOUNT_PROVIDERS = {
        "claudej": "claude",
        "clauded": "claude",
        "codexj": "codex",
        "codexf": "codex",
        "muse": "muse",
    }

    def __init__(self, rate_monitor: Any, reset_margin_minutes: int = 5) -> None:
        self._monitor = rate_monitor
        self._margin = timedelta(minutes=reset_margin_minutes)

    async def check(
        self, accounts: Sequence[str],
    ) -> dict[str, QuotaVerdict]:
        verdicts: dict[str, QuotaVerdict] = {}
        for account in accounts:
            provider = self.ACCOUNT_PROVIDERS.get(str(account))
            if provider is None or self._monitor is None:
                verdicts[str(account)] = QuotaVerdict(ok=True)
                continue
            try:
                launchable = await self._monitor.can_launch(
                    provider, honor_reserve=False)
            except Exception:
                logger.warning("quota probe failed for %s; failing open",
                               account, exc_info=True)
                verdicts[str(account)] = QuotaVerdict(ok=True)
                continue
            if launchable:
                verdicts[str(account)] = QuotaVerdict(ok=True)
                continue
            try:
                status = await self._monitor.get_status(provider)
                resume = (_blocking_resume(status) + self._margin).isoformat()
                detail = (f"5h={status.remaining_5h:.0%} "
                          f"weekly={status.remaining_weekly:.0%}")
            except Exception:
                resume, detail = None, "monitor unreadable"
            verdicts[str(account)] = QuotaVerdict(
                ok=False, resume_at=resume, detail=detail)
        return verdicts
