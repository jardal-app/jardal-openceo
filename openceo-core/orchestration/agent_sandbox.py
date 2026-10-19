"""Agent sandbox: bwrap fence + ticket folders (TASK-2026-00192).

Every model-driven CLI (planner, CEO, builder, closer, reviewers,
opencode QA, live tmux panes) runs inside this fence:

* env: :func:`sandbox_env` (allowlist + forced headless vars, overlay wins);
* files: bubblewrap over :class:`BubblewrapBackend` with an agent profile —
  system read-only, motor/toolchain paths read-only, the ticket workspace
  read-write, private /tmp, own pid namespace, shared network (motors and
  the local MCP need it). Declared extra roots (the repo read-only for
  skilled runs, writable creation roots for operations) mount
  depth-ordered with the same secret masking, and the workspace rebinds
  read-write when an extra root covers it;
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
import uuid
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
    "ticket_dir",
]

#: Vars forced on every agent spawn, over base and overlay alike.
SANDBOX_FORCED_ENV = {
    "MUSE_NO_AUTO_UPDATE": "1",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

_HOME_DIRS_RO = (
    ".codex", ".claude", ".claude-j", ".qwen", ".dsh", ".opencode",
    ".gemini", ".gemini-j", ".gemini-2",
    ".config/muse", ".config/opencode",
    ".local/share/claude", ".local/share/uv",
    ".local/share/claude-skill-wrappers",
    ".local/lib", ".npm-global",
)
_HOME_FILES_RO = (
    ".claude.json",
    ".local/bin/muse", ".local/bin/musej",
    ".local/bin/codexj", ".local/bin/claudej", ".local/bin/clauded",
    ".local/bin/claudep", ".local/bin/qwen",
    ".local/bin/agyd", ".local/bin/agyj", ".local/bin/agy2",
    ".local/bin/agy",
    ".local/bin/uv", ".local/bin/uvx",
    ".local/bin/.muse-version", ".local/bin/.muse-release-info.json",
)
_HOME_FILE_GLOBS_RO = (".local/bin/muse-bin-*",)
_HOME_SYMLINKS = (".local/bin/codex", ".local/bin/claude",
                  ".local/bin/kimi")
_HOME_TMPFS = (
    ".dsh/sessions", ".local/share/opencode",
    ".local/share/muse", ".cache", ".npm", ".ssh",
)

_GEMINI_STATE_SUBDIRS = (".gemini/history", ".gemini/tmp", ".gemini/projects")
_GEMINI_STATE_FILES = ("settings.json", "state.json", "projects.json",
                       "trustedFolders.json", "google_accounts.json")
_GEMINI_BINARIES = frozenset({"agy", "agyd", "agyj", "agy2"})

#: agy binary → home subdir holding its state (account dirs come from
#: the agy translator; bare ``agy`` shares the default dir).
_GEMINI_BINARY_SUBDIRS = {"agyj": ".gemini-j", "agy2": ".gemini-2"}

_WALK_PRUNE_DIRS = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "target", "dist", "build", ".gradle", ".cache",
})
_ENV_DOC_MARKERS = ("example", "sample", "template", "dist")

_SCRATCH_DIRNAME = ".openceo"
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


def ticket_dir(ticket_id: str, base_dir: Path | None = None) -> Path:
    """Per-ticket folder path without creating it (close hooks)."""
    target = Path(base_dir) if base_dir is not None else (
        runtime_base_dir() / _TICKET_WORK_BASE)
    return target / _check_ticket_id(ticket_id)


def ensure_ticket_dir(ticket_id: str, base_dir: Path | None = None) -> Path:
    """Empty per-ticket folder, created 0700. Removed via remove_ticket_dir."""
    path = ticket_dir(ticket_id, base_dir=base_dir)
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


def _config_path_roots(config_args: Sequence[str], home: Path,
                       workspace: Path | None = None) -> list[list[str]]:
    """Read-only mounts for absolute config paths (never prompt text).

    Only argv positions the orchestrator built (binary, headless/model args)
    are scanned: the prompt travels last (argv) or via stdin, never here.
    A path under ``~/development/<org>/<repo>/`` mounts that repo root, so
    out-of-tree motors (dsh harness) resolve without exposing siblings.
    Roots covering the workspace are skipped: inside it the rw bind rules,
    and an ancestor would shadow the workspace read-only (review H3).
    Symlinked motors are recreated, or argv[0] would dangle (review H4).
    """
    ops: list[list[str]] = []
    seen: set[str] = set()
    try:
        dev = (home / "development").resolve()
    except OSError:
        dev = home / "development"
    try:
        work = Path(workspace).resolve() if workspace is not None else None
    except OSError:
        work = None
    try:
        home_dir = home.resolve()
    except OSError:
        home_dir = home
    mounted_home = [home_dir / entry for entry in _HOME_DIRS_RO
                    if (home_dir / entry).exists()]

    def _covered(path: Path) -> bool:
        if work is not None and (path == work or work in path.parents):
            return True
        return any(path == root or root in path.parents
                   for root in mounted_home)

    for raw in config_args:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            continue
        try:
            link = candidate if candidate.is_symlink() else None
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
        if work is not None:
            try:
                resolved.relative_to(work)
                continue
            except ValueError:
                pass
            try:
                work.relative_to(resolved)
                continue
            except ValueError:
                pass
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            ops.append(["--ro-bind", str(resolved), str(resolved)])
        if link is not None and str(link) != str(resolved):
            try:
                link_abs = link.parent.resolve() / link.name
            except OSError:
                link_abs = link
            if ((link_abs == home_dir or home_dir in link_abs.parents)
                    and not _covered(link_abs)):
                ops.append(["--symlink", str(resolved), str(link)])
    return ops


def _iter_nested_secrets(root: Path):
    """``(path, is_dir)`` for ``.env*`` files and ``.runtime`` dirs.

    Pruned walk, never follows symlinks. Finds the service env nested
    in checkouts (``openceo-core/.env``) that a root-only mask misses
    (review H1). ``*.example``-style docs stay readable: agents may
    legitimately read config-shape docs, which hold no secrets.
    """
    stack = [Path(root)]
    while stack:
        try:
            entries = list(stack.pop().iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in _WALK_PRUNE_DIRS:
                        continue
                    if (entry.name == ".runtime"
                            or entry.name.startswith(".env")):
                        yield entry, True
                        continue
                    stack.append(entry)
                elif entry.name.startswith(".env"):
                    lowered = entry.name.lower()
                    if any(mark in lowered for mark in _ENV_DOC_MARKERS):
                        continue
                    yield entry, False
            except OSError:
                continue


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


def _gemini_state_ops(home: Path, scratch: Path,
                      argv: Sequence[str]) -> list[list[str]]:
    """Writable agy state over the read-only account dir (review H4).

    agy writes history, tmp, projects and root-level state JSONs; a
    pure ro bind would EROFS it. Only agy-family runs pay for it, each
    over its own account dir (TASK-2026-00246).
    """
    if not argv or Path(str(argv[0])).name not in _GEMINI_BINARIES:
        return []
    subdir = _GEMINI_BINARY_SUBDIRS.get(Path(str(argv[0])).name,
                                        ".gemini")
    account_dir = home / subdir
    ops = [["--tmpfs", str(account_dir)]]
    if account_dir.is_dir():
        for entry in sorted(account_dir.iterdir()):
            if entry.is_symlink():
                continue
            ops.append(["--ro-bind", str(entry), str(entry)])
    ops.extend(["--tmpfs", str(account_dir / entry.split("/", 1)[1])]
               for entry in _GEMINI_STATE_SUBDIRS)
    for name in _GEMINI_STATE_FILES:
        src = home / subdir / name
        if not src.is_file() or src.is_symlink():
            continue
        dst = scratch / f"gemini-{subdir[1:]}-{name}"
        try:
            shutil.copy2(src, dst)
        except OSError:
            continue
        ops.append(["--bind", str(dst), str(src)])
    return ops


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


def _checked_extra_root(raw: str | Path, home: Path, kind: str) -> Path:
    """One declared extra root, resolved. Refuses anything unusable."""
    try:
        resolved = Path(str(raw)).expanduser().resolve()
    except OSError as exc:
        raise ValueError(
            f"extra {kind} root {raw!r} is unusable: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"extra {kind} root is not a dir: {resolved}")
    try:
        home_dir = home.resolve()
    except OSError:
        home_dir = home
    if resolved == Path("/") or resolved == home_dir:
        raise ValueError(f"extra {kind} root {resolved} is too broad")
    return resolved


def _prune_contained(roots: list[Path]) -> list[Path]:
    """Drop roots another root already covers (walk each tree once)."""
    pruned: list[Path] = []
    for root in sorted(roots, key=lambda path: len(path.parts)):
        if any(root == kept or kept in root.parents for kept in pruned):
            continue
        pruned.append(root)
    return pruned


def plan_agent(
    *,
    argv: Sequence[str],
    workspace: Path | str,
    base: Mapping[str, str],
    overlay: Mapping[str, str] | None = None,
    config_args: Sequence[str] | None = None,
    home: Path | None = None,
    opencode_state_rw: bool = False,
    home_file_overlays: Mapping[str, bytes | str] | None = None,
    extra_read_roots: Sequence[str | Path] = (),
    extra_write_roots: Sequence[str | Path] = (),
) -> IsolationPlan:
    """Fenced argv/env/cwd for one agent run. Raises IsolationUnavailable.

    ``home_file_overlays`` bind-mounts rendered content over
    home-relative paths (role settings, TASK-2026-00197): each value is
    written 0600 into the run scratch (cleaned with it) and bound
    read-only after the home mounts, so it shadows them per run.

    ``extra_read_roots``/``extra_write_roots`` mount declared trees
    (TASK-2026-00214): shallow first so narrow mounts shadow broad ones,
    secret masks over every extra root, and the workspace rebound
    read-write after the masks when one covers it (an ancestor mask
    must not swallow it; its own masks re-apply last). A missing or
    too-broad root refuses the run.
    """
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
    full_argv = [str(token) for token in argv]
    ops = _home_ops(host_home, opencode_state_rw)
    ops.extend(_snapshot_dsh_profile(overlay, scratch))
    ops.extend(_gemini_state_ops(host_home, scratch, full_argv))
    config_ops = _config_path_roots(
        config_args if config_args is not None
        else ([full_argv[0]] if full_argv else []),
        host_home, workdir)
    ops.extend(config_ops)
    common = _git_common_dir(workdir)
    if common is not None:
        ops.append(["--ro-bind", str(common), str(common)])
    extras = [(_checked_extra_root(raw, host_home, "read"), False)
              for raw in extra_read_roots]
    extras += [(_checked_extra_root(raw, host_home, "write"), True)
               for raw in extra_write_roots]
    extras.sort(key=lambda item: len(item[0].parts))
    for path, writable in extras:
        ops.append(["--bind" if writable else "--ro-bind",
                    str(path), str(path)])
    # The backend binds the workspace before extra_ops: a covering extra
    # mount would shadow it back to read-only, so rebind it after the
    # masks. Masks first matters the other way too: a mask over a
    # workspace ancestor (tmpfs on <repo>/.runtime) must not swallow the
    # workspace (review 00007); the workspace's own masks re-apply last.
    try:
        work_resolved = workdir.resolve()
    except OSError:
        work_resolved = workdir
    needs_rebind = any(
        path == work_resolved or path in work_resolved.parents
        for path, _writable in extras)

    def _mask(root: Path) -> None:
        for path, is_dir in _iter_nested_secrets(root):
            if is_dir:
                ops.append(["--tmpfs", str(path)])
            else:
                ops.append(["--ro-bind", str(scratch / "empty"), str(path)])

    mask_roots = [workdir]
    for op in config_ops:
        if op[0] == "--ro-bind":
            candidate = Path(op[2])
            if candidate.is_dir():
                mask_roots.append(candidate)
    mask_roots.extend(_prune_contained([path for path, _ in extras]))
    for masked in mask_roots:
        if needs_rebind and masked == workdir:
            continue
        _mask(masked)
    if needs_rebind:
        ops.append(["--bind", str(workdir), str(workdir)])
        _mask(workdir)
    if home_file_overlays:
        overlay_dir = scratch / "overlays"
        overlay_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(overlay_dir, 0o700)
        run_tag = uuid.uuid4().hex
        for index, key in enumerate(sorted(home_file_overlays)):
            relpath = Path(str(key))
            if relpath.is_absolute() or ".." in relpath.parts:
                raise ValueError(
                    f"overlay escapes the home dir: {key!r}")
            content = home_file_overlays[key]
            data = (content.encode("utf-8") if isinstance(content, str)
                    else bytes(content))
            # Unique per plan: concurrent runs in one workspace must
            # never share the bind source (review 00003/F1).
            target = overlay_dir / f"{run_tag}-{index}.overlay"
            with open(target, "wb") as handle:
                handle.write(data)
            os.chmod(target, 0o600)
            ops.append(["--ro-bind", str(target),
                        str(host_home / relpath)])
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
    home_file_overlays: Mapping[str, bytes | str] | None = None,
) -> str:
    """Single shell line running ``command`` fenced (tmux panes).

    ``env -i`` rebuilds the pane child env from scratch: the dirty
    interactive shell never leaks into the agent, and CLI shell escapes
    inherit the fence.
    """
    plan = plan_agent(argv=list(command), workspace=workspace, base=base,
                      overlay=overlay, config_args=config_args, home=home,
                      home_file_overlays=home_file_overlays)
    # Pane TUIs need a terminal even when the service started without one.
    env = {"TERM": "tmux-256color", "LANG": "C.UTF-8", **plan.env}
    parts: list[str] = ["env", "-i"]
    parts.extend(f"{key}={value}" for key, value in env.items())
    parts.extend(plan.argv)
    return shlex.join(parts)
