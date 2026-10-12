#!/usr/bin/env python3
"""Exact project-creation steps for the operations skill (TASK-2026-00214).

Thin CLI over ``ops.create_project``: repo templates, the validated
``project.yaml`` and the vault entry. Idempotent (every rerun reports
``created``/``existed``), JSON on stdout, exit 0 on success and 1 on a
functional failure (argparse keeps 2 for usage errors).

Roots default to the service configuration; flags override them so the
rehearsal (TASK-2026-00234) runs this same binary against doubles.
``--dry-run`` executes the real path inside fresh tmp roots and writes
nothing else, which is what the preflight runs.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def _bootstrap_core() -> Path:
    """Put ``openceo-core/`` on the path, following staged symlinks."""
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "openceo-core"
        if candidate.is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    raise RuntimeError(
        "openceo-core/ not found above the skill script: "
        f"{here} must run from an OpenCEO checkout")


_CORE_DIR = _bootstrap_core()

from ops.create_project import (  # noqa: E402
    ProjectCreationError,
    default_remote_url,
    ensure_git_repo,
    ensure_project_config,
    ensure_repo_templates,
    ensure_vault_entry,
    normalize_key,
)

_ERROR_MAX_CHARS = 2000


def service_roots() -> dict[str, Path]:
    """Creation roots from the service configuration (same source).

    Absolute: the agent may call from the ticket folder and reuse the
    ``roots`` output after changing directories. These must match the
    fence mounts, which resolve the same Settings keys: the service
    .env must not override them (review 00007).
    """
    from config_loader import ConfigLoader
    from settings import Settings
    settings = Settings()
    return {
        "repos": Path(settings.PROJECT_REPOS_ROOT).expanduser().resolve(),
        "projects": ConfigLoader(settings).projects_root().resolve(),
        "vault": Path(settings.VAULT_ROOT).expanduser().resolve(),
    }


def resolve_roots(args: argparse.Namespace) -> tuple[dict[str, Path], bool]:
    """Flag roots over service roots; whether projects was explicit."""
    service = service_roots()
    roots = {
        "repos": Path(args.repos_root).expanduser().resolve()
        if args.repos_root else service["repos"],
        "projects": Path(args.projects_root).expanduser().resolve()
        if args.projects_root else service["projects"],
        "vault": Path(args.vault_root).expanduser().resolve()
        if args.vault_root else service["vault"],
    }
    return roots, bool(args.projects_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crear_proyecto.py",
        description="Exact project-creation steps (idempotent, JSON).")
    sub = parser.add_subparsers(dest="command", required=True)

    def _roots(item: argparse.ArgumentParser) -> None:
        item.add_argument("--repos-root", default=None)
        item.add_argument("--projects-root", default=None)
        item.add_argument("--vault-root", default=None)
        item.add_argument("--dry-run", action="store_true")

    def _identity(item: argparse.ArgumentParser) -> None:
        item.add_argument("--project-key", required=True)
        item.add_argument("--repo", default="")
        item.add_argument("--display-name", default="")
        item.add_argument("--org", default="")
        item.add_argument("--description", default="")
        item.add_argument("--remote-url", default="")

    roots = sub.add_parser("roots", help="Print the resolved roots.")
    _roots(roots)

    templates = sub.add_parser(
        "templates", help="README/CLAUDE.md/AGENTS.md/.gitignore + commit.")
    _identity(templates)
    _roots(templates)

    config = sub.add_parser(
        "project-config", help="projects/<key>/project.yaml, validated.")
    _identity(config)
    _roots(config)

    vault = sub.add_parser(
        "vault-entry", help="Vault hub plus its own README line.")
    _identity(vault)
    _roots(vault)
    return parser


def _key_repo_org(args: argparse.Namespace) -> tuple[str, str, str, str]:
    key = normalize_key(args.project_key)
    repo = (args.repo or "").strip() or key
    org = (args.org or "").strip() or "jardal"
    url = (args.remote_url or "").strip() or default_remote_url(repo)
    return key, repo, org, url


def cmd_templates(args: argparse.Namespace,
                  roots: dict[str, Path]) -> dict[str, Any]:
    key, repo, org, url = _key_repo_org(args)
    return ensure_repo_templates(
        str(roots["repos"] / repo),
        project_key=key,
        display_name=args.display_name or "",
        org=org,
        description=args.description or "",
        vault_root=str(roots["vault"]),
        remote_url=url,
    )


def _validate_project(key: str, projects_root: Path,
                      explicit: bool) -> list[str]:
    from config_loader import ConfigLoader
    if explicit:
        from settings import Settings
        loader = ConfigLoader(
            Settings(_env_file=None, PROJECTS_DIR=str(projects_root)))
    else:
        from settings import Settings
        loader = ConfigLoader(Settings())
    return loader.validate_project(key)


def cmd_project_config(args: argparse.Namespace, roots: dict[str, Path],
                       explicit_projects: bool) -> dict[str, Any]:
    key, repo, org, url = _key_repo_org(args)
    result = ensure_project_config(
        roots["projects"],
        key,
        remote_url=url,
        repo_path=str(roots["repos"] / repo),
        vault_root=str(roots["vault"]),
        vault_project=f"projects/{org}/{key}",
    )
    errors = _validate_project(key, roots["projects"], explicit_projects)
    if errors:
        raise ProjectCreationError(
            f"{key}/project.yaml invalid: {'; '.join(errors)}")
    return {**result, "errors": []}


def cmd_vault_entry(args: argparse.Namespace,
                    roots: dict[str, Path]) -> dict[str, Any]:
    key, repo, org, _url = _key_repo_org(args)
    return ensure_vault_entry(
        roots["vault"],
        org,
        key,
        display_name=args.display_name or "",
        description=args.description or "",
        source_path=str(roots["repos"] / repo),
    )


def dry_run_roots() -> tuple[tempfile.TemporaryDirectory, dict[str, Path]]:
    """Fresh tmp roots with a seeded vault README (nothing else)."""
    tmp = tempfile.TemporaryDirectory(prefix="crear-proyecto-dry-")
    base = Path(tmp.name)
    roots = {
        "repos": base / "repos",
        "projects": base / "projects",
        "vault": base / "vault",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    (roots["vault"] / "README.md").write_text(
        "# Vault (dry run)\n", encoding="utf-8")
    return tmp, roots


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    """Execute one subcommand; dry-run swaps in tmp roots first."""
    if args.command == "roots":
        roots, _explicit = resolve_roots(args)
        return {name: str(path) for name, path in roots.items()}
    key = normalize_key(args.project_key)
    repo = (args.repo or "").strip() or key
    if args.dry_run:
        keeper, roots = dry_run_roots()
        try:
            if args.command == "templates":
                ensure_git_repo(roots["repos"], repo)
            return _dispatch(args, roots, explicit_projects=True,
                             dry_base=str(Path(keeper.name)))
        finally:
            keeper.cleanup()
    roots, explicit = resolve_roots(args)
    return _dispatch(args, roots, explicit_projects=explicit)


def _dispatch(args: argparse.Namespace, roots: dict[str, Path], *,
              explicit_projects: bool,
              dry_base: str | None = None) -> dict[str, Any]:
    if args.command == "templates":
        result = cmd_templates(args, roots)
    elif args.command == "project-config":
        result = cmd_project_config(args, roots, explicit_projects)
    elif args.command == "vault-entry":
        result = cmd_vault_entry(args, roots)
    else:  # pragma: no cover - argparse restricts the choices
        raise ProjectCreationError(f"unknown command {args.command!r}")
    if dry_base is not None:
        result = {"dry_base": dry_base, **result}
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry = bool(getattr(args, "dry_run", False))
    try:
        result = run_command(args)
    except Exception as exc:  # JSON envelope, never a traceback
        print(json.dumps({"ok": False, "command": args.command,
                          "dry_run": dry,
                          "error": f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]}))
        return 1
    print(json.dumps({"ok": True, "command": args.command,
                      "dry_run": dry, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
