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

Builders of ``dev.isolation: host`` projects run host-confined instead
(:func:`plan_host_agent`): host toolchain, caches and home, role overlays
kept, no privilege gain, and neither other checkouts, secrets, agent
sockets nor the production Docker daemon (TASK-2026-00985).

No backend, no run: :func:`plan_agent` raises :class:`IsolationUnavailable`
when bwrap is missing instead of degrading silently.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from agents.dev.tier_s_contract import child_env
from research.runtimes.base import IsolationUnavailable
from research.runtimes.isolation import BubblewrapBackend, IsolationPlan

__all__ = [
    "SANDBOX_FORCED_ENV",
    "cleanup_sandbox_scratch",
    "ensure_sandbox_scratch",
    "ensure_ticket_dir",
    "plan_agent",
    "plan_host_agent",
    "project_isolation",
    "remove_ticket_dir",
    "runtime_base_dir",
    "sandbox_available",
    "sandbox_env",
    "sandboxed_shell_command",
    "ticket_dir",
    "user_instruction_masks",
    "vault_project_read_root",
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
    ".local/bin/codexf", ".local/bin/codexj",
    ".local/bin/claudej", ".local/bin/clauded",
    ".local/bin/claudep", ".local/bin/qwen",
    ".local/bin/agyd", ".local/bin/agyj", ".local/bin/agy2",
    ".local/bin/agy",
    ".local/bin/uv", ".local/bin/uvx",
    ".local/bin/.muse-version", ".local/bin/.muse-release-info.json",
)
_HOME_FILE_GLOBS_RO = (".local/bin/muse-bin-*",)
#: OAuth credential files rebound read-write over the ro home binds:
#: the CLIs persist rotated access tokens by rewriting them, and a
#: read-only bind turns every post-expiry run into a 401 (TASK-2026-00442).
_HOME_CRED_RW_FILES = (
    ".claude/.credentials.json",
    ".claude-j/.credentials.json",
)
#: Claude Code config dirs → their global config file. A token refresh
#: first locks ``<dir>/.oauth_refresh.lock``: on a read-only dir the lock
#: fails, the CLI skips the refresh and the run gets a 401
#: (TASK-2026-00450). Each dir is a per-run tmpfs over its real entries.
_CLAUDE_CONFIG_DIRS = {
    ".claude": ".claude.json",
    ".claude-j": ".claude-j/.claude.json",
}
#: Entries the per-run claude dir never binds read-only: the credentials
#: (rebound rw below) and a host lock caught mid-refresh. The global
#: config, when it lives in the dir, gets a writable copy instead.
_CLAUDE_DIR_UNBOUND = frozenset({".credentials.json", ".oauth_refresh.lock"})
#: User-level instruction files the CLIs auto-load (the Owner's session
#: rules). Read-only roles run with them masked (TASK-2026-00443).
USER_INSTRUCTION_FILES = (
    ".claude/CLAUDE.md", ".claude-j/CLAUDE.md",
    ".codex/AGENTS.md", ".codex-j/AGENTS.md",
    ".gemini/GEMINI.md", ".gemini-j/GEMINI.md", ".gemini-2/GEMINI.md",
    ".config/opencode/AGENTS.md", ".qwen/QWEN.md",
)
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

#: Reviewer home profiles (TASK-2026-00288): each reviewer CLI keeps only
#: its own auth/config; sibling motor dirs stay unmounted. ``None`` keeps
#: the full agent home (builders and other roles, unchanged).
_HOME_PROFILE_DIRS = {
    "opencode": frozenset({".opencode", ".config/opencode"}),
}
_HOME_PROFILE_FILES = {
    "opencode": frozenset(),
}

#: agy binary → home subdir holding its state (account dirs come from
#: the agy translator; bare ``agy`` shares the default dir).
_GEMINI_BINARY_SUBDIRS = {"agyj": ".gemini-j", "agy2": ".gemini-2"}

#: codex wrapper → its CODEX_HOME, plus the entries a run reads from it.
#: Read-write: codex opens installation_id O_RDWR|O_CREAT at boot (EROFS kills
#: its in-process app-server), rewrites auth.json on token refresh, and
#: refreshes models_cache.json when the model catalog changes: read-only
#: left runs on fallback metadata (TASK-2026-00476).
#: ``~/.codex`` is also bound read-only with the home; codexf's per-run
#: tmpfs over it shadows that bind (TASK-2026-00475).
_CODEX_BINARY_SUBDIRS = {"codexf": ".codex", "codexj": ".codex-j"}
_CODEX_READ_ENTRIES = ("config.toml", "version.json", "plugins")
_CODEX_WRITE_ENTRIES = ("installation_id", "auth.json", "models_cache.json")

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


def project_isolation(dev_cfg: Mapping[str, Any] | None) -> str:
    """Execution fence for a project's builders: ``sandbox`` or ``host``.

    ``dev.isolation: host`` runs the builder host-confined
    (:func:`plan_host_agent`: real home, toolchain, package caches and the
    agents' Docker daemon, with role overlays and no privilege gain). It
    is a deliberate per-project choice for trusted repos on the host stack
    whose tests the sandbox cannot run. Absent or malformed values keep
    the safe default.
    """
    raw = str((dev_cfg or {}).get("isolation") or "sandbox").strip().lower()
    return raw if raw in ("sandbox", "host") else "sandbox"


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


def user_instruction_masks(home: Path | None = None) -> dict[str, bytes]:
    """Empty ``home_file_overlays`` over the real user instruction files.

    Each path resolves on the host first, so all the CLI aliases that
    symlink one shared file mask that file once.
    """
    root = Path(home) if home is not None else Path(os.path.expanduser("~"))
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return {}
    masks: dict[str, bytes] = {}
    for entry in USER_INSTRUCTION_FILES:
        try:
            real = (root / entry).resolve(strict=True)
            relative = real.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if real.is_file():
            masks[relative.as_posix()] = b""
    return masks


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


def _codex_motor_ops(home: Path, account_dir: Path) -> list[list[str]]:
    """Re-expose the live codex motor hidden by the per-run tmpfs.

    ``~/.local/bin/codex`` may resolve inside the shadowed account dir
    (the standalone install lives under ``~/.codex/packages``): without
    this the recreated link dangles and ``codex`` falls through PATH to
    a stale system install that cannot parse the model catalog
    (TASK-2026-00477). Only the resolved ``bin/`` dir is exposed (binary
    plus helper siblings, never sibling releases), over a tmpfs chain
    that recreates each parent level. A motor outside the account dir
    stays visible and needs nothing.
    """
    try:
        real = (home / ".local/bin/codex").resolve()
        anchor = account_dir.resolve()
    except OSError:
        return []
    if not real.is_file():
        return []
    try:
        rel = real.relative_to(anchor)
    except ValueError:
        return []
    if len(rel.parts) < 2:
        return [["--ro-bind", str(real), str(real)]]
    ops: list[list[str]] = []
    node = anchor
    for part in rel.parent.parts:
        node = node / part
        ops.append(["--tmpfs", str(node)])
    ops.append(["--ro-bind", str(real.parent), str(node)])
    return ops


def _codex_state_ops(home: Path, argv: Sequence[str]) -> list[list[str]]:
    """Per-run CODEX_HOME over the account dir (TASK-2026-00443).

    The agent home never mounted ``~/.codex-j``, so codexj died at boot
    ("CODEX_HOME ... does not exist"). codex writes sqlite state, logs and
    sessions there: a tmpfs holds them for the run, the entries it only
    reads are bound read-only and the three it opens for writing persist.
    Memories and the user-level AGENTS.md stay out.
    """
    if not argv:
        return []
    subdir = _CODEX_BINARY_SUBDIRS.get(Path(str(argv[0])).name)
    account_dir = home / subdir if subdir else None
    if account_dir is None or not account_dir.is_dir():
        return []
    ops = [["--tmpfs", str(account_dir)]]
    for name in _CODEX_READ_ENTRIES:
        entry = account_dir / name
        if entry.exists() and not entry.is_symlink():
            ops.append(["--ro-bind", str(entry), str(entry)])
    for name in _CODEX_WRITE_ENTRIES:
        entry = account_dir / name
        if entry.is_file() and not entry.is_symlink():
            ops.append(["--bind", str(entry), str(entry)])
    ops.extend(_codex_motor_ops(home, account_dir))
    return ops


def _reviewer_home_keep(profile: str, account: str | None) -> tuple[
        frozenset[str] | None, frozenset[str] | None]:
    """Home dirs/files a reviewer profile keeps (``None`` = all)."""
    if profile == "agy":
        subdir = _GEMINI_BINARY_SUBDIRS.get(account or "agy", ".gemini")
        files = {".local/bin/agy"}
        if account and account != "agy":
            files.add(f".local/bin/{account}")
        return frozenset({subdir}), frozenset(files)
    if profile in _HOME_PROFILE_DIRS:
        return _HOME_PROFILE_DIRS[profile], _HOME_PROFILE_FILES[profile]
    raise ValueError(f"unknown home profile: {profile!r}")


def _claude_dir_ops(account_dir: Path, config: str) -> list[list[str]]:
    """Per-run tmpfs dir over its real entries, bound read-only.

    New files (the refresh lock) stay in the tmpfs and never reach the
    host. Symlinks are recreated, not bound: an alias must keep resolving
    to its real file, which is the one the instruction masks cover.
    """
    ops = [["--tmpfs", str(account_dir)]]
    try:
        children = sorted(account_dir.iterdir())
    except OSError:
        return [["--ro-bind", str(account_dir), str(account_dir)]]
    skip = set(_CLAUDE_DIR_UNBOUND)
    own_config = account_dir / Path(config).name
    if Path(config).parent.name == account_dir.name \
            and own_config.is_file() and not own_config.is_symlink():
        skip.add(own_config.name)
    for child in children:
        if child.name in skip:
            continue
        if child.is_symlink():
            ops.append(["--symlink", os.readlink(child), str(child)])
        else:
            ops.append(["--ro-bind", str(child), str(child)])
    return ops


def _claude_config_ops(home: Path, scratch: Path,
                       keep_dirs: frozenset[str] | None) -> list[list[str]]:
    """Writable per-run copies of the claude global config files.

    A refresh may rewrite ``.claude.json`` (account profile) after the new
    token is issued; a failed write there drops that token unsaved. The
    copy takes those writes and is discarded with the scratch; an
    unreadable or invalid file keeps a read-only bind.
    """
    ops: list[list[str]] = []
    for account, config in _CLAUDE_CONFIG_DIRS.items():
        if keep_dirs is not None and account not in keep_dirs:
            continue
        source = home / config
        if source.is_symlink() or not source.is_file():
            continue
        try:
            data = source.read_bytes()
            json.loads(data)
        except (OSError, ValueError):
            ops.append(["--ro-bind", str(source), str(source)])
            continue
        copies = scratch / "claude-config"
        copies.mkdir(parents=True, exist_ok=True)
        os.chmod(copies, 0o700)
        target = copies / f"{uuid.uuid4().hex}-{source.name}"
        with open(target, "wb") as handle:
            handle.write(data)
        os.chmod(target, 0o600)
        ops.append(["--bind", str(target), str(source)])
    return ops


def _home_ops(home: Path, opencode_state_rw: bool,
              keep_dirs: frozenset[str] | None = None,
              keep_files: frozenset[str] | None = None) -> list[list[str]]:
    ops: list[list[str]] = []
    for entry in _HOME_DIRS_RO:
        if keep_dirs is not None and entry not in keep_dirs:
            continue
        path = home / entry
        if entry in _CLAUDE_CONFIG_DIRS and path.is_dir() \
                and not path.is_symlink():
            ops.extend(_claude_dir_ops(path, _CLAUDE_CONFIG_DIRS[entry]))
        elif path.exists():
            ops.append(["--ro-bind", str(path), str(path)])
    for entry in _HOME_FILES_RO:
        if keep_files is not None and entry not in keep_files:
            continue
        path = home / entry
        if path.is_file() and not path.is_symlink():
            ops.append(["--ro-bind", str(path), str(path)])
    if keep_files is None:
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
    if keep_files is None:
        for entry in _HOME_CRED_RW_FILES:
            path = home / entry
            if path.is_file() and not path.is_symlink():
                ops.append(["--bind", str(path), str(path)])
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


def vault_project_read_root(
    project_config: Mapping[str, object] | None,
) -> list[str]:
    """Active-project vault dir for read-only Dev mounts (TASK-2026-00328).

    Resolves ``dev.vault_project`` (``{dir, project}``) to the project
    folder inside the vault, for ``extra_read_roots``. Never fails: a
    missing mapping, an unreadable dir, or a path escaping the vault
    (``..`` or a symlink pointing out) yields ``[]`` — a project
    without ``vault_project`` runs exactly as before, with no mount.
    Only this project folder is ever returned: never the whole vault,
    another project, ``shared/`` or ``inbox/``. Any symlink along the
    configured project path is rejected, even when it resolves inside
    the vault.
    """
    if not isinstance(project_config, Mapping):
        return []
    dev = project_config.get("dev")
    if not isinstance(dev, Mapping):
        return []
    vault = dev.get("vault_project")
    if not isinstance(vault, Mapping):
        return []
    raw_dir = vault.get("dir")
    raw_project = vault.get("project")
    if not raw_dir or not raw_project:
        return []
    if Path(str(raw_project)).is_absolute():
        return []
    try:
        base = Path(str(raw_dir)).expanduser().resolve()
    except OSError:
        return []
    probe = base
    for part in Path(str(raw_project)).parts:
        if part in ("", ".", "/", ".."):
            continue
        probe = probe / part
        try:
            if probe.is_symlink():
                return []
        except OSError:
            return []
    try:
        target = (base / str(raw_project)).resolve()
    except OSError:
        return []
    if not target.is_dir():
        return []
    try:
        rel = target.relative_to(base / "projects")
    except ValueError:
        return []
    if len(rel.parts) < 2:
        return []
    return [str(target)]


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
    extra_ops: Sequence[Sequence[str]] = (),
    home_profile: str | None = None,
) -> IsolationPlan:
    """Fenced argv/env/cwd for one agent run. Raises IsolationUnavailable.

    ``extra_ops`` appends caller-built bwrap ops (reviewer secret masks,
    TASK-2026-00288); the backend still rejects unknown op names.
    ``home_profile`` (``"opencode"``/``"agy"``) mounts only that CLI's
    own home entries instead of the full agent home; ``None`` keeps the
    full home (builders and other roles, unchanged).

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
    if home_profile == "opencode" and opencode_state_rw:
        raise ValueError("opencode reviewers never persist opencode state")
    if home_profile is not None:
        account = Path(full_argv[0]).name if full_argv else None
        keep_dirs, keep_files = _reviewer_home_keep(home_profile, account)
    else:
        keep_dirs, keep_files = None, None
    ops = _home_ops(host_home, opencode_state_rw, keep_dirs, keep_files)
    ops.extend(_claude_config_ops(host_home, scratch, keep_dirs))
    ops.extend(_snapshot_dsh_profile(overlay, scratch))
    ops.extend(_gemini_state_ops(host_home, scratch, full_argv))
    ops.extend(_codex_state_ops(host_home, full_argv))
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
        ops.extend(_home_overlay_ops(scratch, host_home, home_file_overlays))
    ops.extend([str(token) for token in op] for op in extra_ops)
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


def _overlay_relpath(key: str) -> Path:
    relpath = Path(str(key))
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError(f"overlay escapes the home dir: {key!r}")
    return relpath


def _home_overlay_ops(
    scratch: Path, host_home: Path, overlays: Mapping[str, bytes | str],
) -> list[list[str]]:
    """Write each overlay 0600 into the run scratch and bind it read-only."""
    overlay_dir = scratch / "overlays"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(overlay_dir, 0o700)
    run_tag = uuid.uuid4().hex
    ops: list[list[str]] = []
    for index, key in enumerate(sorted(overlays)):
        relpath = _overlay_relpath(key)
        content = overlays[key]
        data = (content.encode("utf-8") if isinstance(content, str)
                else bytes(content))
        # Unique per plan: concurrent runs in one workspace must
        # never share the bind source (review 00003/F1).
        target = overlay_dir / f"{run_tag}-{index}.overlay"
        with open(target, "wb") as handle:
            handle.write(data)
        os.chmod(target, 0o600)
        ops.append(["--ro-bind", str(target), str(host_home / relpath)])
    return ops


#: Caller ops a host-confined run accepts (per-run trace dirs).
_HOST_EXTRA_OPS = frozenset({"--bind", "--ro-bind"})

#: Rootless daemon for host-mode builders (TASK-2026-00985): a user of its
#: own, so containers reach neither production nor the Owner's home.
AGENT_DOCKER_USER = "openceo-docker"
#: Production daemon socket (``/var/run`` links to ``/run``).
_HOST_DOCKER_SOCKET = Path("/run/docker.sock")
#: Owner credential stores no builder needs: dirs become an empty tmpfs and
#: files read empty. Only existing paths are masked: on the shared root,
#: bwrap would create a missing target on the real filesystem.
_HOST_SECRET_HOME_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".config/gcloud",
    ".config/gh", ".local/share/keyrings", ".password-store", ".mongodb",
    ".codex/worktrees",
)
_HOST_SECRET_HOME_FILES = (
    ".git-credentials", ".netrc", ".pgpass", ".pypirc", ".vault-token",
    ".Xauthority", ".docker/config.json", ".bash_history", ".zsh_history",
    ".python_history", ".psql_history", ".mysql_history", ".dbshell",
)


def agent_docker_socket() -> Path | None:
    """Socket of the agents' rootless Docker daemon, ``None`` if absent."""
    try:
        uid = pwd.getpwnam(AGENT_DOCKER_USER).pw_uid
    except KeyError:
        return None
    socket_path = Path(f"/run/user/{uid}/docker.sock")
    return socket_path if socket_path.is_socket() else None


def _host_code_root(workdir: Path, home: Path) -> Path | None:
    """Top-level home folder holding the checkout (``~/development``).

    The other checkouts, the vault and their secrets sit beside the
    builder's repo there. ``None`` for a workspace outside the home,
    directly in it or under a dotdir (motor and toolchain state).
    """
    try:
        resolved_home = home.resolve()
        relative = workdir.resolve().relative_to(resolved_home)
    except (OSError, ValueError):
        return None
    if len(relative.parts) < 2 or relative.parts[0].startswith("."):
        return None
    return resolved_home / relative.parts[0]


def _host_runtime_masks(
    empty: Path, uid: int, *, docker_socket: Path = _HOST_DOCKER_SOCKET,
    run_user: Path = Path("/run/user"), tmp: Path = Path("/tmp"),
) -> list[list[str]]:
    """Sockets that would hand the run root or a process outside it.

    The production Docker socket reads as an empty file; the user runtime
    dir (session bus behind ``systemd-run --user``, keyring, gpg agent,
    VS Code IPC), the tmux server dir and ssh-agent dirs become empty.
    """
    ops: list[list[str]] = []
    if docker_socket.exists():
        ops.append(["--ro-bind", str(empty), str(docker_socket)])
    runtime = run_user / str(uid)
    if runtime.is_dir():
        ops.append(["--perms", "0700", "--tmpfs", str(runtime)])
    hidden = [tmp / f"tmux-{uid}"]
    try:
        hidden.extend(sorted(tmp.glob("ssh-*")))
    except OSError:
        pass
    for path in hidden:
        try:
            if path.is_dir() and not path.is_symlink() \
                    and path.stat().st_uid == uid:
                ops.append(["--tmpfs", str(path)])
        except OSError:
            continue
    return ops


def _empty_json(empty: Path) -> Path:
    """``{}`` beside the scratch ``empty`` file, for masked JSON configs."""
    target = empty.with_name("empty.json")
    target.write_text("{}\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def _host_secret_ops(
    workdir: Path, home: Path, empty: Path,
    read_roots: Sequence[str | Path], write_roots: Sequence[str | Path],
) -> list[list[str]]:
    """Hide what a host-mode builder must not read (TASK-2026-00985).

    Its code root goes behind a tmpfs with only the workspace (plus a
    linked worktree's git dir) and the declared roots rebound; service
    env files and ``.runtime`` dirs inside them read empty, like the
    fence; the home credential stores go empty.
    """
    ops: list[list[str]] = []
    work_resolved = workdir.resolve()
    extras = [(_checked_extra_root(raw, home, "read"), False)
              for raw in read_roots]
    extras += [(_checked_extra_root(raw, home, "write"), True)
               for raw in write_roots]
    extras.sort(key=lambda item: len(item[0].parts))
    code_root = _host_code_root(workdir, home)
    if code_root is not None and code_root.is_dir():
        ops.append(["--tmpfs", str(code_root)])
        ops.append(["--bind", str(work_resolved), str(work_resolved)])
        common = _git_common_dir(workdir)
        if common is not None and code_root in common.parents:
            ops.append(["--bind", str(common), str(common)])
    for path, writable in extras:
        ops.append(["--bind" if writable else "--ro-bind",
                    str(path), str(path)])
    for root in _prune_contained([work_resolved,
                                  *(path for path, _ in extras)]):
        for path, is_dir in _iter_nested_secrets(root):
            ops.append(["--tmpfs", str(path)] if is_dir
                       else ["--ro-bind", str(empty), str(path)])
    for relpath in _HOST_SECRET_HOME_DIRS:
        target = home / relpath
        if target.is_dir():
            ops.append(["--tmpfs", str(target)])
    empty_json = _empty_json(empty)
    for relpath in _HOST_SECRET_HOME_FILES:
        target = home / relpath
        if target.is_file():
            # Readers such as testcontainers parse ~/.docker/config.json.
            blank = empty_json if target.suffix == ".json" else empty
            ops.append(["--ro-bind", str(blank), str(target)])
    return ops


def plan_host_agent(
    *,
    argv: Sequence[str],
    workspace: Path | str,
    base: Mapping[str, str],
    overlay: Mapping[str, str] | None = None,
    home: Path | None = None,
    home_file_overlays: Mapping[str, bytes | str] | None = None,
    extra_ops: Sequence[Sequence[str]] = (),
    read_roots: Sequence[str | Path] = (),
    write_roots: Sequence[str | Path] = (),
) -> IsolationPlan:
    """Host-confined run for ``dev.isolation: host`` builders.

    The builder uses the host toolchain, package caches and its real home
    (TASK-2026-00483) without seeing what it must not touch
    (TASK-2026-00985): its own PID namespace and /proc, no other checkout
    (only its workspace and the declared roots stay under its code root), no
    service env files, credential stores or agent sockets, and Docker
    through the agents' rootless daemon (``DOCKER_HOST``) instead of the
    production socket. Role overlays shadow the global engine config,
    per-run trace binds apply, the run dies with its parent and bwrap's
    no_new_privs keeps sudo and setuid helpers inert. Raises
    :class:`IsolationUnavailable` without bwrap, like :func:`plan_agent`.
    """
    if not BubblewrapBackend().available:
        raise IsolationUnavailable(
            "bwrap is missing: refusing host-mode agent spawn")
    workdir = Path(workspace)
    if not workdir.is_dir():
        raise ValueError(f"agent workspace is not a dir: {workdir}")
    host_home = Path(home) if home is not None else Path(
        os.path.expanduser("~"))
    if home_file_overlays:
        for key in home_file_overlays:
            # The root is shared: bwrap would create a missing bind
            # target on the real filesystem instead of shadowing it.
            if not (host_home / _overlay_relpath(key)).exists():
                raise ValueError(
                    f"overlay target missing on the host: {key!r}")
    caller_ops: list[list[str]] = []
    for op in extra_ops:
        parts = [str(token) for token in op]
        if not parts or parts[0] not in _HOST_EXTRA_OPS:
            raise ValueError(f"unsupported host-mode op: {parts[:1]!r}")
        caller_ops.append(parts)
    scratch = ensure_sandbox_scratch(workdir)
    empty = scratch / "empty"
    command = ["bwrap", "--die-with-parent", "--new-session",
               "--unshare-pid", "--dev-bind", "/", "/", "--proc", "/proc"]
    for op in _host_secret_ops(workdir, host_home, empty, read_roots,
                               write_roots):
        command.extend(op)
    for op in _host_runtime_masks(empty, os.getuid()):
        command.extend(op)
    if home_file_overlays:
        for op in _home_overlay_ops(scratch, host_home, home_file_overlays):
            command.extend(op)
    for op in caller_ops:
        command.extend(op)
    command.extend(["--chdir", str(workdir), "--",
                    *[str(token) for token in argv]])
    env = sandbox_env(base, overlay)
    notes = [
        "Host toolchain, caches and home are visible; other checkouts, "
        "service env files, credential stores and agent sockets are not.",
        "no_new_privs in a user namespace: sudo, apt and setuid helpers "
        "cannot gain privileges.",
    ]
    docker_socket = agent_docker_socket()
    if docker_socket is not None:
        env["DOCKER_HOST"] = f"unix://{docker_socket}"
    else:
        env.pop("DOCKER_HOST", None)
        notes.append("No agent Docker daemon: Docker is unavailable.")
    return IsolationPlan(
        backend="bwrap-host",
        argv=command,
        env=env,
        cwd=str(workdir),
        network="unrestricted",
        filesystem_isolated=False,
        egress_restricted=False,
        notes=notes,
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
