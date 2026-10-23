"""Live CLI executors shared by the engines (dispatch wiring, F3–F6).

:class:`ProcessExecutor` runs every engine role as a one-shot OS process:
role and pool commands come from the global config (``command`` +
``headless_args``), the prompt travels per ``prompt_via``, and the report or
signal is read back from stdout / ``.openceo/status``. No tmux, no lingering
sessions: each call starts and reaps exactly one process.

:class:`RateMonitorQuota` adapts the service :class:`RateMonitor` to the
engine :class:`QuotaChecker` protocol, mapping accounts to monitored
providers. Unknown/reactive accounts fail open (tried until they fail).

Home of the executor result/quota types (TASK-2026-00141): both the Tier S
cycle and the objectives engine import them from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from catalog import CatalogError, load_role_profile
from model_routing.reviewer_isolation import (
    opencode_argv_extras as reviewer_agent_extras,
)
from model_routing.reviewer_isolation import plan_reviewer
from orchestration.agent_sandbox import cleanup_sandbox_scratch, plan_agent
from orchestration.role_claude import (
    CLAUDE_MCP_OVERLAY_RELPATH,
    claude_config_bytes,
    ensure_mcp_placeholder,
    profile_box_tools,
)
from orchestration.role_agy import (
    agy_config_bytes,
    overlay_relpath as agy_overlay_relpath,
)
from orchestration.role_agy import render_argv_extras as agy_argv_extras
from orchestration.role_claude import render_argv_extras as claude_argv_extras
from orchestration.role_codex import codex_mcp_env
from orchestration.role_codex import render_argv_extras as codex_argv_extras
from orchestration.review import ReviewerConfigError, with_resolved_command
from orchestration.provider_recovery import (
    PROVIDER_RESUME_PROMPT,
    PROVIDER_RETRY_DELAYS,
    model_responded,
    transient_provider_error,
)
from orchestration.role_muse import (
    SETTINGS_OVERLAY_RELPATH,
    RoleProfileError,
    render_argv_extras,
    render_card_argv,
    settings_overlay_bytes,
)
from orchestration.role_opencode import (
    OPENCODE_OVERLAY_RELPATH,
    opencode_config_bytes,
)
from orchestration.role_opencode import (
    render_argv_extras as opencode_argv_extras,
)
from research.runtimes.base import IsolationUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecResult:
    """What an executor reports for one role run."""

    returncode: int
    elapsed_s: float
    signal: str | None = None  # last signal read from .openceo/status
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


def _reviewer_output(stdout: str, stderr: str, provider: str,
                     returncode: int) -> tuple[int, str]:
    text = stdout.strip() or stderr.strip()
    if provider == "muse":
        terminal = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict)
                    and str(event.get("payload_type") or "").startswith("run.terminal.")):
                terminal = event.get("payload")
        if isinstance(terminal, dict):
            text = str(terminal.get("text") or terminal.get("error") or "")
            if terminal.get("terminal") != "completed" or not text.strip():
                returncode = returncode or 1
    elif provider in {"agy", "gemini"}:
        try:
            envelope = json.loads(stdout)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict) and "status" in envelope:
            if envelope["status"] != "SUCCESS":
                return returncode or 1, str(envelope.get("error") or envelope["status"])
            text = str(envelope.get("response") or "")
    elif provider == "opencode":
        parts: dict[str, str] = {}
        for index, line in enumerate(stdout.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                return returncode or 1, json.dumps(event.get("error") or event)
            part = event.get("part")
            if event.get("type") == "text" and isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts[str(part.get("id", index))] = part["text"]
        if parts:
            text = "\n".join(parts.values())
    if returncode and stderr.strip() and stderr.strip() != text:
        text = stderr.strip() + "\n" + text
    return returncode, text


class ProcessExecutor:
    """One-shot process executor over global role/pool commands."""

    def __init__(
        self,
        roles: Mapping[str, Any] | None = None,
        pool: Sequence[Mapping[str, Any]] | None = None,
        badge_provider: Any | None = None,
    ) -> None:
        self._roles = dict(roles or {})
        self._pool = {str(slot.get("label")): dict(slot)
                      for slot in (pool or []) if slot.get("label")}
        self._badge_provider = badge_provider
        self._responded: set[tuple[str, str]] = set()

    # -- Executor protocol -------------------------------------------------

    async def run(
        self,
        role: str,
        prompt: str,
        workdir: Path,
        timeout_s: int,
    ) -> ExecResult:
        retryable_role = role.replace("-", "_") in {
            "planner", "plan_reviewer", "builder", "arbiter", "closer",
            "objective_planner", "ceo_reviewer",
        }
        elapsed = 0.0
        attempts: list[dict[str, Any]] = []
        for attempt in range(len(PROVIDER_RETRY_DELAYS) + 1):
            result = await self._run_once(
                role, PROVIDER_RESUME_PROMPT + prompt if attempt else prompt,
                workdir, timeout_s)
            elapsed += result.elapsed_s
            key = (result.slot, result.model)
            responded = key in self._responded or model_responded(
                result.output_tail, result.model) or bool(
                    (result.usage or {}).get("model_responded"))
            if responded or result.returncode == 0:
                self._responded.add(key)
            attempts.append({"returncode": result.returncode,
                             "elapsed_s": result.elapsed_s})
            if (result.returncode == 0 or not retryable_role
                    or attempt == len(PROVIDER_RETRY_DELAYS)
                    or not transient_provider_error(
                        result.output_tail, responded=responded)):
                usage = dict(result.usage or {})
                if len(attempts) > 1:
                    usage["provider_attempts"] = attempts
                if responded:
                    usage["model_responded"] = True
                return replace(result, elapsed_s=elapsed, usage=usage)
            delay = PROVIDER_RETRY_DELAYS[attempt]
            logger.warning("Role %s provider failed; retry %s in %ss",
                           role, attempt + 1, delay)
            if role == "objective_planner":
                for line in prompt.splitlines():
                    if line.startswith("OUTPUT: write "):
                        relative = line[len("OUTPUT: write "):].split(" with exactly")[0].strip()
                        output = (workdir / relative).resolve()
                        if (output.name == "PLANNER_RESULT.json"
                                and output.is_relative_to((workdir / ".openceo").resolve())):
                            output.unlink(missing_ok=True)
            await asyncio.sleep(delay)
        raise AssertionError("provider retry exhausted without a result")

    async def _run_once(
        self, role: str, prompt: str, workdir: Path, timeout_s: int,
    ) -> ExecResult:
        spec, via, extra_env = self._resolve(role)
        if spec is None:
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"unknown live role: {role}")
        if spec.get("active") is False:
            return ExecResult(returncode=1, elapsed_s=0.0,
                              output_tail=f"role {role} is disabled")
        if role.startswith("review:"):
            try:
                spec = with_resolved_command(spec)
            except ReviewerConfigError as exc:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=f"reviewer misconfigured: {exc}",
                    model=str(spec.get("model") or ""),
                    slot=str(spec.get("account") or spec.get("label")
                             or role))
        config_argv = [str(token) for token in spec.get("command", [])]
        config_argv += [str(token) for token in spec.get("headless_args", [])]
        config_argv += [str(token) for token in spec.get("model_args", [])]
        if not config_argv:
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"role {role} has no command")
        if spec.get("provider") == "muse" and "--json" not in config_argv:
            config_argv.append("--json")
        if role.startswith("review:"):
            provider = str(spec.get("provider") or "")
            if provider in {"agy", "gemini"} and "--output-format" not in config_argv:
                position = config_argv.index("--print") if "--print" in config_argv else len(config_argv)
                config_argv[position:position] = ["--output-format", "json"]
            elif provider == "opencode":
                if "--format" not in config_argv:
                    config_argv += ["--format", "json"]
                if "--standalone" not in config_argv:
                    config_argv.append("--standalone")
        hardened = (role.startswith("review:")
                    and bool(spec.get("review_hardened"))
                    and len(config_argv) > 0
                    and Path(config_argv[0]).name == "opencode")
        if hardened:
            config_argv += [token for token in reviewer_agent_extras()
                            if token not in config_argv]
        badge: str | None = None
        file_overlays: dict[str, bytes] | None = None
        translator_env: dict[str, str] = {}
        profile_name = spec.get("role_profile")
        if profile_name:
            if self._badge_provider is None:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=(f"role {role} needs a badge provider "
                                 f"for profile {profile_name}"),
                    model=str(spec.get("model") or ""),
                    slot=str(spec.get("account") or spec.get("label")
                             or role))
            try:
                profile = load_role_profile(str(profile_name))
            except CatalogError as exc:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=f"role profile refused: {exc}",
                    model=str(spec.get("model") or ""),
                    slot=str(spec.get("account") or spec.get("label")
                             or role))
            try:
                badge = await self._badge_provider.mint(role)
            except Exception as exc:
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=f"role badge mint failed: {exc}",
                    model=str(spec.get("model") or ""),
                    slot=str(spec.get("account") or spec.get("label")
                             or role))
            try:
                servers = self._badge_provider.mcp_servers(badge)
                home = Path(os.path.expanduser("~"))
                provider = str(spec.get("provider") or "muse")
                if provider == "muse":
                    rendered = settings_overlay_bytes(
                        profile, home, workdir, servers)
                    file_overlays = {SETTINGS_OVERLAY_RELPATH: rendered}
                    config_argv += render_argv_extras(profile)
                    config_argv += render_card_argv(profile)
                elif provider == "claude":
                    box = profile_box_tools(profile)
                    mcp_path = ensure_mcp_placeholder(home)
                    rendered = claude_config_bytes(servers)
                    file_overlays = {CLAUDE_MCP_OVERLAY_RELPATH: rendered}
                    config_argv += claude_argv_extras(
                        profile, box, str(mcp_path))
                elif provider == "opencode":
                    rendered = opencode_config_bytes(
                        profile, home, servers)
                    file_overlays = {OPENCODE_OVERLAY_RELPATH: rendered}
                    config_argv += opencode_argv_extras(profile.name)
                elif provider == "codex":
                    config_argv += codex_argv_extras()
                    translator_env.update(codex_mcp_env(badge))
                elif provider == "agy":
                    account = str(spec.get("account") or "")
                    rendered = agy_config_bytes(home, account, servers)
                    file_overlays = {
                        agy_overlay_relpath(account): rendered}
                    config_argv += agy_argv_extras()
                else:
                    raise RoleProfileError(
                        f"no translator for provider {provider!r}: "
                        "profile runs support muse, claude, opencode, "
                        "codex and agy")
            except (RoleProfileError, CatalogError, ValueError,
                    OSError) as exc:
                await self._badge_provider.revoke(badge)
                return ExecResult(
                    returncode=1, elapsed_s=0.0,
                    output_tail=f"role profile refused: {exc}",
                    model=str(spec.get("model") or ""),
                    slot=str(spec.get("account") or spec.get("label")
                             or role))
            except BaseException:
                await self._badge_provider.revoke(badge)
                raise
        stdin_payload: str | None = None
        full_argv = list(config_argv)
        if via == "stdin":
            stdin_payload = prompt
        else:
            full_argv.append(prompt)
        overlay = {**extra_env, **translator_env, **{
            str(k): str(v)
            for k, v in dict(spec.get("env") or {}).items()}}
        if role.startswith("review:"):
            overlay["GIT_OPTIONAL_LOCKS"] = "0"
        model = str(spec.get("model") or "")
        slot = str(spec.get("account") or spec.get("label") or role)
        try:
            if hardened:
                plan = plan_reviewer(
                    argv=full_argv, workspace=workdir, base=os.environ,
                    report=None, project=workdir, overlay=overlay,
                    config_args=config_argv,
                    extra_read_roots=spec.get("extra_read_roots") or (),
                    extra_write_roots=spec.get("extra_write_roots") or ())
            else:
                plan = plan_agent(argv=full_argv, workspace=workdir,
                                  base=os.environ, overlay=overlay,
                                  config_args=config_argv,
                                  home_file_overlays=file_overlays,
                                  extra_read_roots=spec.get(
                                      "extra_read_roots") or (),
                                  extra_write_roots=spec.get(
                                      "extra_write_roots") or ())
        except (IsolationUnavailable, ValueError, OSError) as exc:
            if badge is not None and self._badge_provider is not None:
                await self._badge_provider.revoke(badge)
            return ExecResult(
                returncode=1, elapsed_s=0.0,
                output_tail=f"sandbox refused: {exc}",
                model=model, slot=slot)
        except BaseException:
            if badge is not None and self._badge_provider is not None:
                await self._badge_provider.revoke(badge)
            raise
        started = time.monotonic()
        process = None
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
            returncode = int(process.returncode or 0)
            returncode, text = _reviewer_output(
                stdout, stderr, str(spec.get("provider") or ""), returncode)
            truncated = len(text) > OUTPUT_CAP_CHARS
            signal = self._read_signal(workdir, text)
            return ExecResult(
                returncode=returncode,
                elapsed_s=time.monotonic() - started,
                signal=signal,
                usage={"stdout_chars": len(stdout),
                       "stderr_chars": len(stderr),
                       "model_responded": model_responded(stdout, model),
                       "truncated": truncated},
                output_tail=text[:OUTPUT_CAP_CHARS],
                model=model, slot=slot)
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if badge is not None and self._badge_provider is not None:
                await self._badge_provider.revoke(badge)
            cleanup_sandbox_scratch(workdir)

    def register_pool_entries(
        self, entries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Upsert round-scoped pool entries by label.

        The dev engines refresh ranking-fed slots and hardening marks
        per review round (TASK-2026-00293); scripted executors resolve
        roles by name and never need this.
        """
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("label"):
                self._pool[str(entry["label"])] = dict(entry)

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
        """Last ``.openceo/status`` line, else a trailing ``REVIEW_DONE_*``."""
        try:
            status = (workdir / ".openceo" / "status").read_text(
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
