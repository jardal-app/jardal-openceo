"""Agent sandbox: bwrap fence + ticket folders (TASK-2026-00192).

Every model-driven CLI (planner, CEO, builder, closer, reviewers, PM,
opencode QA, live tmux panes) runs inside this fence:

* env: :func:`sandbox_env` (allowlist + forced headless vars, overlay wins);
* files: bubblewrap over :class:`BubblewrapBackend` with an agent profile —
  system read-only, motor/toolchain paths read-only, the ticket workspace
  read-write, private /tmp, own pid namespace, shared network (motors and
  the local MCP need it);
* secrets: anything not allowlisted is simply not mounted (ssh keys, gh/aws
  credentials, pasted token files, sibling checkouts, the vault, foreign
  /tmp, the service ``.env``/``.runtime``).

No backend, no run: :func:`plan_agent` raises :class:`IsolationUnavailable`
when bwrap is missing instead of degrading silently.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from agents.dev.tier_s_contract import child_env
from research.runtimes.base import IsolationUnavailable
from research.runtimes.isolation import BubblewrapBackend, IsolationPlan

__all__ = [
    "SANDBOX_FORCED_ENV",
    "cleanup_sandbox_scratch",
    "ensure_sandbox_scratch",
    "ensure_ticket_dir",
    "plan_agent",
    "remove_ticket_dir",
    "runtime_base_dir",
    "sandbox_available",
    "sandbox_env",
    "sandboxed_shell_command",
]

#: Vars forced on every agent spawn, over base and overlay alike.
SANDBOX_FORCED_ENV = {
    "MUSE_NO_AUTO_UPDATE": "1",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

_HOME_DIRS_RO = (
    ".codex", ".claude", ".claude-j", ".qwen", ".dsh", ".opencode",
    ".config", ".local/share", ".local/lib", ".npm-global",
)
_HOME_FILES_RO = (
    ".claude.json",
    ".local/bin/muse", ".local/bin/musej",
    ".local/bin/codexj", ".local/bin/claudej", ".local/bin/clauded",
    ".local/bin/claudep", ".local/bin/qwen",
    ".local/bin/agyd", ".local/bin/agy",
    ".local/bin/uv", ".local/bin/uvx",
    ".local/bin/.muse-version", ".local/bin/.muse-release-info.json",
)
_HOME_FILE_GLOBS_RO = (".local/bin/muse-bin-*",)
_HOME_SYMLINKS = (".local/bin/codex", ".local/bin/claude")
_HOME_TMPFS = (
    ".dsh/sessions", ".config/gh", ".local/share/opencode",
    ".local/share/muse", ".cache", ".npm", ".ssh",
)

_SCRATCH_DIRNAME = ".tier-s"
_SCRATCH_SUBDIR = "sandbox"
_TICKET_WORK_BASE = "agent-work"


def sandbox_available() -> bool:
    """True when the bwrap backend can fence agent runs."""
    return BubblewrapBackend().available


def sandbox_env(base: Mapping[str, str],
                overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allowlisted child env plus forced headless vars (forced wins)."""
    env = child_env(base, overlay)
    env.update(SANDBOX_FORCED_ENV)
    return env


def runtime_base_dir() -> Path:
    """Repo ``.runtime/`` dir (host side, next to ``openceo-core/``)."""
    return Path(__file__).resolve().parents[2] / ".runtime"


def _check_ticket_id(ticket_id: str) -> str:
    cleaned = str(ticket_id or "").strip()
    if not cleaned or not all(ch.isalnum() or ch in "_-" for ch in cleaned):
        raise ValueError(f"invalid ticket id for a workspace: {ticket_id!r}")
    return cleaned


def ensure_ticket_dir(ticket_id: str, base_dir: Path | None = None) -> Path:
    """Empty per-ticket folder, created 0700. Removed via remove_ticket_dir."""
    target = Path(base_dir) if base_dir is not None else (
        runtime_base_dir() / _TICKET_WORK_BASE)
    path = target / _check_ticket_id(ticket_id)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def remove_ticket_dir(ticket_id: str, base_dir: Path | None = None) -> None:
    """Best-effort removal; leftovers never fail the run."""
    try:
        target = Path(base_dir) if base_dir is not None else (
            runtime_base_dir() / _TICKET_WORK_BASE)
        shutil.rmtree(target / _check_ticket_id(ticket_id),
                      ignore_errors=True)
    except (OSError, ValueError):
        pass


def ensure_sandbox_scratch(workspace: Path) -> Path:
    """Orchestrator scratch inside the workspace (resolver copy, masks)."""
    scratch = Path(workspace) / _SCRATCH_DIRNAME / _SCRATCH_SUBDIR
    scratch.mkdir(parents=True, exist_ok=True)
    os.chmod(scratch, 0o700)
    empty = scratch / "empty"
    try:
        empty.touch(exist_ok=True)
        os.chmod(empty, 0o600)
    except OSError:
        pass
    return scratch


def cleanup_sandbox_scratch(workspace: Path) -> None:
    """Best-effort scratch removal (keeps review worktree digests clean)."""
    try:
        shutil.rmtree(Path(workspace) / _SCRATCH_DIRNAME / _SCRATCH_SUBDIR,
                      ignore_errors=True)
    except OSError:
        pass


def _git_common_dir(workspace: Path) -> Path | None:
    gitfile = Path(workspace) / ".git"
    if not gitfile.is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    common = (Path(workspace) / proc.stdout.strip()).resolve()
    if not common.is_dir():
        return None
    try:
        common.relative_to(Path(workspace).resolve())
        return None
    except ValueError:
        return common


def _config_path_roots(config_args: Sequence[str], home: Path) -> list[Path]:
    """Read-only roots for absolute config paths (never prompt text).

    Only argv positions the orchestrator built (binary, headless/model args)
    are scanned: the prompt travels last (argv) or via stdin, never here.
    A path under ``~/development/<org>/<repo>/`` mounts that repo root, so
    out-of-tree motors (dsh harness) resolve without exposing siblings.
    """
    roots: list[Path] = []
    seen: set[str] = set()
    try:
        dev = (home / "development").resolve()
    except OSError:
        dev = home / "development"
    for raw in config_args:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.exists():
            continue
        try:
            rel = resolved.relative_to(dev)
            if len(rel.parts) >= 2:
                resolved = dev.joinpath(*rel.parts[:2])
            else:
                continue
        except ValueError:
            pass
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def _snapshot_dsh_profile(overlay: Mapping[str, str] | None,
                        scratch: Path) -> list[list[str]]:
    """Writable copy of the dsh headless profile, bound over the original.

    dsh rewrites cordis.yml + plugin state at boot: a read-only home fails
    it with EROFS. Only runs carrying DSH_HOME pay the copy; reads see the
    current profile, writes stay in the scratch and die with the run.
    """
    raw = (overlay or {}).get("DSH_HOME") or ""
    src = Path(str(raw)) / "profiles" / "headless"
    if not src.is_dir():
        return []
    dst = scratch / "dsh-headless"
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True)
    except OSError:
        return []
    return [["--bind", str(dst), str(src)]]


def _home_ops(home: Path, opencode_state_rw: bool) -> list[list[str]]:
    ops: list[list[str]] = []
    for entry in _HOME_DIRS_RO:
        path = home / entry
        if path.exists():
            ops.append(["--ro-bind", str(path), str(path)])
    for entry in _HOME_FILES_RO:
        path = home / entry
        if path.is_file() and not path.is_symlink():
            ops.append(["--ro-bind", str(path), str(path)])
    for pattern in _HOME_FILE_GLOBS_RO:
        for match in sorted(home.glob(pattern)):
            if match.is_file() and not match.is_symlink():
                ops.append(["--ro-bind", str(match), str(match)])
    for entry in _HOME_SYMLINKS:
        link = home / entry
        if link.is_symlink():
            try:
                real = link.resolve()
            except OSError:
                continue
            if real.is_file():
                ops.append(["--ro-bind", str(real), str(real)])
                ops.append(["--symlink", str(real), str(link)])
    for entry in _HOME_TMPFS:
        if entry == ".local/share/opencode" and opencode_state_rw:
            continue
        ops.append(["--tmpfs", str(home / entry)])
    if opencode_state_rw:
        state = home / ".local/share/opencode"
        if state.is_dir():
            ops.append(["--bind", str(state), str(state)])
    return ops


def plan_agent(
    *,
    argv: Sequence[str],
    workspace: Path | str,
    base: Mapping[str, str],
    overlay: Mapping[str, str] | None = None,
    config_args: Sequence[str] | None = None,
    home: Path | None = None,
    opencode_state_rw: bool = False,
) -> IsolationPlan:
    """Fenced argv/env/cwd for one agent run. Raises IsolationUnavailable."""
    backend = BubblewrapBackend()
    if not backend.available:
        raise IsolationUnavailable("bwrap is missing: refusing agent spawn")
    workdir = Path(workspace)
    if not workdir.is_dir():
        raise ValueError(f"agent workspace is not a dir: {workdir}")
    host_home = Path(home) if home is not None else Path(
        os.path.expanduser("~"))
    env = sandbox_env(base, overlay)
    scratch = ensure_sandbox_scratch(workdir)
    ops = _home_ops(host_home, opencode_state_rw)
    ops.extend(_snapshot_dsh_profile(overlay, scratch))
    for root in _config_path_roots(config_args if config_args is not None
                                   else [str(argv[0])] if argv else [],
                                   host_home):
        ops.append(["--ro-bind", str(root), str(root)])
    common = _git_common_dir(workdir)
    if common is not None:
        ops.append(["--ro-bind", str(common), str(common)])
    if (workdir / ".env").is_file():
        ops.append(["--ro-bind", str(scratch / "empty"),
                    str(workdir / ".env")])
    if (workdir / ".runtime").is_dir():
        ops.append(["--tmpfs", str(workdir / ".runtime")])
    return backend.plan(
        argv=list(argv),
        workspace=workdir,
        worker_home=scratch,
        env=dict(env),
        allow_network=True,
        home_env=str(host_home),
        skip_worker_binds=True,
        extra_ops=ops,
    )


def sandboxed_shell_command(
    *,
    command: Sequence[str],
    workspace: Path | str,
    base: Mapping[str, str],
    overlay: Mapping[str, str] | None = None,
    config_args: Sequence[str] | None = None,
    home: Path | None = None,
) -> str:
    """Single shell line running ``command`` fenced (tmux panes).

    ``env -i`` rebuilds the pane child env from scratch: the dirty
    interactive shell never leaks into the agent, and CLI shell escapes
    inherit the fence.
    """
    plan = plan_agent(argv=list(command), workspace=workspace, base=base,
                      overlay=overlay, config_args=config_args, home=home)
    parts: list[str] = ["env", "-i"]
    parts.extend(f"{key}={value}" for key, value in plan.env.items())
    parts.extend(plan.argv)
    return shlex.join(parts)
