"""Role skill delivery to builder/closer run workspaces (TASK-2026-00896).

Every run under the dev profile (simple-tier builders, the Tier S
builder, the objectives closer) sees the role skills where its engine
discovers them. Installing them as user-global skills never reaches a
builder: the muse overlay switches every non-profile skill off, the
fence does not mount ``~/.agents/skills``, and reviewers must stay on
code. So each run stages read-only copies of the profile skills into
its own workspace before the fence goes up:

* ``.codex/skills/<skill>/`` — muse and Codex project skills;
* ``.claude/skills/<skill>/`` — Claude Code project skills (muse alt).

Codex 0.171.0 advertises no skill discovery (``codexf --help`` shows no
skill flag or subcommand and ``~/.codex/config.toml`` carries no skill
key), so Codex builders read the canonical ``SKILL.md`` path straight
from the builder contract instead.

Staged copies never pollute the destination worktree: each copy carries
a marker with its source fingerprint (a second run in the same worktree
refreshes only on drift), and the skill paths land in the worktree's
own ``info/exclude`` so ``git status`` stays clean — they enter neither
the squash nor the size brake. Anything already at a staged path that
is not our marker is foreign and is left alone, never deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_SKILLS_DIR",
    "DEV_PROFILE",
    "DISCOVERY_DIRS",
    "MARKER_NAME",
    "SKILL_CONTRACT_PATH",
    "role_skill_sources",
    "stage_role_skills",
]

#: Profile whose runs receive role skills (builders and the closer).
DEV_PROFILE = "dev"

#: Workspace-relative dirs every engine scans for project skills.
DISCOVERY_DIRS = (".codex/skills", ".claude/skills")

#: Discovery dir the builder contract names as the canonical read path.
CANONICAL_SKILLS_DIR = ".codex/skills"

#: Marker written inside each staged copy (source + fingerprint).
MARKER_NAME = ".openceo-staged"

#: Contract path of one skill's recipe, relative to the run workspace.
SKILL_CONTRACT_PATH = f"{CANONICAL_SKILLS_DIR}/{{skill}}/SKILL.md"

_EXCLUDE_HEADER = "# openceo role skills (TASK-2026-00896): staged per run, never committed"
_SKIP_NAMES = frozenset({"__pycache__", ".git", ".hg", ".svn"})


def _repo_root() -> Path:
    from catalog import catalog_root

    return catalog_root().parent


def role_skill_sources(
    profile_name: str = DEV_PROFILE,
    repo_root: str | Path | None = None,
) -> dict[str, Path]:
    """Skill name → source dir for a profile's non-engine skills.

    Engine skills (``create-skill``, ``git``) ship inside the engine and
    have no source dir. Unknown profiles and missing dirs yield {}: the
    caller stages nothing instead of refusing the run.
    """
    from catalog import (
        ENGINE_SKILLS,
        CatalogError,
        load_catalog,
        load_role_profile,
        load_role_skills,
    )

    root = Path(repo_root) if repo_root is not None else _repo_root()
    try:
        profile = load_role_profile(profile_name)
        card = load_catalog(root / "catalog").agents.get(profile.agent)
        if card is None:
            return {}
        on_disk = {skill.name: Path(skill.source)
                   for skill in load_role_skills(
                       profile.agent, card.skills_dir, root)}
    except (CatalogError, ValueError, OSError) as exc:
        logger.warning("role skill sources unreadable for %s: %s",
                       profile_name, exc)
        return {}
    wanted = [skill for skill in profile.skills
              if skill not in ENGINE_SKILLS]
    sources: dict[str, Path] = {}
    for name in wanted:
        source = on_disk.get(name)
        if source is None:
            logger.warning("role skill %s of profile %s is not on disk",
                           name, profile_name)
            continue
        if not source.is_absolute():
            source = root / source
        if source.is_dir():
            sources[name] = source
    return sources


def _fingerprint(source: Path) -> str:
    """Cheap content identity: sorted relpath + size + mtime."""
    rows: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.name in _SKIP_NAMES:
            continue
        try:
            kind = "d" if path.is_dir() else "f"
            stat_result = path.stat()
        except OSError:
            continue
        rows.append(f"{kind} {path.relative_to(source).as_posix()} "
                    f"{stat_result.st_size} {stat_result.st_mtime_ns}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _read_marker(target: Path) -> Mapping[str, Any] | None:
    marker = target / MARKER_NAME
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, Mapping) else None


def _ignore_tree(_base: str, names: list[str]) -> set[str]:
    return ({name for name in names if name in _SKIP_NAMES}
            | {name for name in names if name.endswith((".pyc", ".pyo"))})


def _make_writable(target: Path) -> None:
    for path in [target, *sorted(target.rglob("*"), reverse=True)]:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR | stat.S_IXUSR
                       if path.is_dir() else mode | stat.S_IWUSR)
        except OSError:
            continue


def _make_readonly(target: Path) -> None:
    for path in [target, *sorted(target.rglob("*"))]:
        try:
            path.chmod(0o555 if path.is_dir() else 0o444)
        except OSError:
            continue


def _stage_one(source: Path, target: Path) -> bool:
    """Copy one skill read-only; True when (re)staged, False when skipped."""
    if target.is_symlink() or target.exists():
        marker = _read_marker(target) if target.is_dir() else None
        if marker is None:
            logger.debug("skill path %s is foreign, left alone", target)
            return False
        if (marker.get("source") == str(source)
                and marker.get("fingerprint") == _fingerprint(source)):
            return False
        try:
            _make_writable(target)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            logger.warning("staged skill %s not refreshable: %s", target, exc)
            return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=_ignore_tree)
        (target / MARKER_NAME).write_text(json.dumps(
            {"source": str(source), "fingerprint": _fingerprint(source),
             "ticket": "TASK-2026-00896"}) + "\n", encoding="utf-8")
        _make_readonly(target)
    except OSError as exc:
        logger.warning("skill %s not staged into %s: %s", source, target, exc)
        return False
    return True


def _find_gitdir(workspace: Path) -> Path | None:
    """Owning gitdir walking up (plain dir or worktree file), None outside."""
    node = workspace.resolve()
    while True:
        dotgit = node / ".git"
        if dotgit.is_dir():
            return dotgit
        if dotgit.is_file():
            try:
                first = dotgit.read_text(
                    encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            if first.startswith("gitdir:"):
                candidate = (dotgit.parent
                             / first[len("gitdir:"):].strip())
                return candidate if candidate.is_dir() else None
            return None
        parent = node.parent
        if parent == node:
            return None
        node = parent


def _exclude_staged(gitdir: Path, names: list[str]) -> None:
    """Hide staged skill dirs from this worktree's git status (idempotent)."""
    if not names:
        return
    exclude = gitdir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = (exclude.read_text(encoding="utf-8").splitlines()
                   if exclude.is_file() else [])
    except OSError as exc:
        logger.warning("git exclude unreadable at %s: %s", exclude, exc)
        return
    wanted = [f"{parent}/{name}/"
              for parent in DISCOVERY_DIRS for name in names]
    missing = [line for line in wanted if line not in current]
    if not missing:
        return
    if _EXCLUDE_HEADER not in current:
        lines = [*current, _EXCLUDE_HEADER, *missing]
    else:
        lines = [*current, *missing]
    try:
        exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("git exclude not writable at %s: %s", exclude, exc)


def stage_role_skills(
    workspace: str | Path,
    profile_name: str = DEV_PROFILE,
    *,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Stage a profile's skills into a run workspace. Best-effort.

    Copies land read-only under each engine discovery dir and hide from
    this worktree's git status. Never raises: every failure degrades to
    a warning and the run continues without that skill. Returns the
    staged skill names (fresh or already in place).
    """
    try:
        base = Path(workspace).resolve()
    except OSError as exc:
        logger.warning("role skills not staged: workspace unreadable: %s",
                       exc)
        return []
    sources = role_skill_sources(profile_name, repo_root)
    if not sources:
        return []
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("role skills not staged into %s: %s", base, exc)
        return []
    staged: list[str] = []
    for name, source in sorted(sources.items()):
        placed = [parent for parent in DISCOVERY_DIRS
                  if _stage_one(source, base / parent / name)]
        if placed or all((base / parent / name).exists()
                         or (base / parent / name).is_symlink()
                         for parent in DISCOVERY_DIRS):
            staged.append(name)
    try:
        gitdir = _find_gitdir(base)
    except OSError:
        gitdir = None
    if gitdir is not None:
        _exclude_staged(gitdir, staged)
    else:
        logger.debug("role skills staged outside a git worktree: %s", base)
    return sorted(staged)
