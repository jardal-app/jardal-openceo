"""End-to-end project creation (autonomy F6, TASK-2026-00115).

Deterministic, idempotent operations behind the ``ops.*`` workflow tools and
the ``operations.create_project`` Tier-0 workflow. Every ``ensure_*`` returns
what already existed untouched and reports ``created``/``existed`` per
artifact, so re-running a creation never duplicates anything. Every
``remove_*`` only deletes what this module could have created (guarded by
identity checks), for clean teardown of test projects.

All filesystem writes stay under caller-provided roots (resolved + confined);
identifiers are restricted to ``[a-z0-9-]`` so no traversal is expressible.
No tokens, no LLM, no network except through the injected ERPNext adapter.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
ORG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

REMOTE_URL_TEMPLATE = "git@github-jardal:jardal-app/{repo}.git"
COMMIT_AUTHOR_NAME = "OpenCEO"
COMMIT_AUTHOR_EMAIL = "openceo@jardal.app"
COMMIT_MESSAGE = "chore: initial project scaffold (OpenCEO F6)"
GIT_TIMEOUT_SECONDS = 60

GITIGNORE_REQUIRED_LINES = ("docs/work/", "tmp/")


class ProjectCreationError(ValueError):
    """Deterministic project-creation failure (bad input or failed guard)."""


def normalize_key(value: Any, *, field: str = "project_key") -> str:
    """Lowercase identifier or raise. Mirrors ``POST /api/projects``."""
    key = str(value or "").strip().lower()
    if not KEY_RE.match(key):
        raise ProjectCreationError(
            f"{field} must match {KEY_RE.pattern}, got {str(value)[:60]!r}"
        )
    return key


def normalize_org(value: Any) -> str:
    org = str(value or "").strip().lower() or "jardal"
    if not ORG_RE.match(org):
        raise ProjectCreationError(
            f"org must match {ORG_RE.pattern}, got {str(value)[:60]!r}"
        )
    return org


def confined(root: Path, *parts: str) -> Path:
    """Join under ``root``; refuse anything resolving outside it."""
    base = Path(root).expanduser().resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise ProjectCreationError(f"path escapes root {base}: {target}")
    return target


def default_remote_url(repo_name: str) -> str:
    return REMOTE_URL_TEMPLATE.format(repo=repo_name)


def escalation_source_id(project_key: str) -> str:
    return f"f6-create-project:{project_key}"


# ---------------------------------------------------------------------------
# Spend gate (mandate F4, TASK-2026-00114)
# ---------------------------------------------------------------------------

def check_spend(
    *,
    project_key: str,
    summary: str,
    costs: Sequence[Mapping[str, Any]] | None,
    mandate: Any | None,
) -> dict[str, Any]:
    """Decide whether creating the project needs the owner. Pure.

    ``costs`` is ``[{"label": ..., "amount": <decimal-string|int>}]`` (domain
    purchase, hosting, paid service...). Total over the mandate threshold (or
    no valid mandate at all) escalates fail-closed; anything else is automatic.
    """
    from company_mandate import OwnerAction, requires_owner

    total = Decimal("0")
    details: dict[str, str] = {}
    for index, cost in enumerate(costs or []):
        if not isinstance(cost, Mapping):
            raise ProjectCreationError(f"costs[{index}] must be an object")
        label = str(cost.get("label") or f"cost-{index}").strip() or f"cost-{index}"
        raw = cost.get("amount", "0")
        if isinstance(raw, bool) or isinstance(raw, float):
            raise ProjectCreationError(
                f"costs[{index}].amount must be a decimal string or int, "
                f"got {type(raw).__name__}"
            )
        try:
            amount = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, AttributeError) as exc:
            raise ProjectCreationError(
                f"costs[{index}].amount is not a number: {raw!r}"
            ) from exc
        if amount < 0:
            raise ProjectCreationError(f"costs[{index}].amount must not be negative")
        total += amount
        details[label] = str(amount)

    action = OwnerAction(
        kind="crear-proyecto",
        summary=(summary or f"Crear el proyecto {project_key}").strip(),
        project=None,
        amount=total,
        details={"project_key": project_key, **details},
    )
    decision = requires_owner(action, mandate)
    currency = "?"
    if mandate is not None:
        try:
            currency = str(mandate.presupuesto.moneda)
        except AttributeError:
            currency = "?"
    return {
        "escalate": bool(decision.escalate),
        "category": decision.category.value if decision.category else None,
        "reason": decision.reason,
        "human_action": decision.human_action,
        "amount": str(total),
        "currency": currency,
    }


# ---------------------------------------------------------------------------
# Git repo (local only: init + remote URL + initial commit, no network)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProjectCreationError("git is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectCreationError(f"git {' '.join(args)} timed out") from exc


def ensure_git_repo(
    repos_root: Path | str,
    repo_name: str,
    *,
    remote_url: str | None = None,
) -> dict[str, Any]:
    """Create (or adopt) the local repo with the conventional remote URL."""
    name = normalize_key(repo_name, field="repo_name")
    path = confined(Path(repos_root), name)
    url = (remote_url or "").strip() or default_remote_url(name)

    path.mkdir(parents=True, exist_ok=True)
    created = not (path / ".git").exists()
    if created:
        proc = _git(path, "init", "-b", "main")
        if proc.returncode != 0:
            raise ProjectCreationError(f"git init failed: {proc.stderr.strip()[:300]}")

    current = ""
    proc = _git(path, "remote", "get-url", "origin")
    if proc.returncode == 0:
        current = proc.stdout.strip()
    remote_changed = current != url
    if remote_changed:
        args = ["remote", "add", "origin", url] if not current else ["remote", "set-url", "origin", url]
        proc = _git(path, *args)
        if proc.returncode != 0:
            raise ProjectCreationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")

    return {
        "path": str(path),
        "created": created,
        "remote_url": url,
        "remote_changed": remote_changed,
    }


# ---------------------------------------------------------------------------
# Repo templates (mirror of the existing projects)
# ---------------------------------------------------------------------------

def _readme_md(*, display_name: str, description: str, project_key: str) -> str:
    blurb = description.strip() or f"Proyecto {display_name}."
    return (
        f"# {display_name}\n"
        f"\n"
        f"{blurb}\n"
        f"\n"
        f"## Mapa\n"
        f"\n"
        f"- `CLAUDE.md` — instrucciones operativas del repo (los agentes las cargan directo).\n"
        f"- `docs/work/` — trabajo temporal por ticket (ver CLAUDE.md).\n"
        f"- OpenCEO: `projects/{project_key}/project.yaml` en el repo de OpenCEO.\n"
        f"\n"
        f"## Reglas\n"
        f"\n"
        f"- ERPNext es el registro de negocio; este repo es la verdad de implementación.\n"
        f"- Sin secretos, credenciales ni datos personales en el repo ni en notas.\n"
    )


def _claude_md(
    *,
    display_name: str,
    description: str,
    project_key: str,
    org: str,
    vault_root: Path,
    remote_url: str,
) -> str:
    blurb = description.strip() or f"Proyecto {display_name}."
    vault_project = f"projects/{org}/{project_key}"
    return (
        f"# CLAUDE.md - {display_name}\n"
        f"\n"
        f"{blurb}\n"
        f"\n"
        f"**Vault de este proyecto**: `{vault_project}` dentro de\n"
        f"`{vault_root}/` (usar el skill `obsidian-vault-links`).\n"
        f"Trabajo temporal de tickets: `docs/work/TASK-2026-000NN-tema.md`.\n"
        f"Remoto: `{remote_url}`.\n"
        f"\n"
        f"## Layout\n"
        f"\n"
        f"- `README.md` — entrada breve: mapa, reglas y rutas de lectura.\n"
        f"- `docs/work/` — un fichero temporal por ticket (se retira al cerrar).\n"
        f"\n"
        f"## Validar\n"
        f"\n"
        f"Define aquí cómo validar cuando el stack exista (tests, lint, build).\n"
        f"Hasta entonces: `git status` limpio y revisión del diff antes de mergear.\n"
        f"\n"
        f"## Reglas\n"
        f"\n"
        f"- ERPNext es el registro de negocio; Mongo (vía OpenCEO) conserva la ejecución.\n"
        f"- Sin secretos, credenciales ni datos personales en notas ni docs temporales.\n"
        f"- Convenciones sobre configuración: espejar el código hermano, reusar sus helpers.\n"
    )


def _gitignore() -> str:
    return (
        "# OpenCEO F6 scaffold: temporary ticket work + scratch\n"
        "docs/work/\n"
        "tmp/\n"
        "\n"
        "# Python\n"
        "__pycache__/\n"
        "*.py[cod]\n"
        ".pytest_cache/\n"
        ".venv/\n"
        "\n"
        "# Node\n"
        "node_modules/\n"
        "dist/\n"
        "build/\n"
        "\n"
        "# Agents\n"
        ".tier-s/\n"
        ".runtime/\n"
    )


def ensure_repo_templates(
    repo_path: Path | str,
    *,
    project_key: str,
    display_name: str,
    org: str,
    description: str,
    vault_root: Path | str,
    remote_url: str,
) -> dict[str, Any]:
    """Write README/CLAUDE.md/AGENTS.md/.gitignore/docs/work + initial commit.

    Never overwrites an existing divergent file: a present file is kept and
    reported under ``existed`` (``.gitignore`` only gains the two required
    lines when missing). The commit runs only when the tree has changes.
    """
    key = normalize_key(project_key)
    org_name = normalize_org(org)
    repo = Path(repo_path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise ProjectCreationError(f"not a git repo: {repo}")

    description = str(description or "")
    display = (display_name or "").strip() or key
    rendered = {
        "README.md": _readme_md(display_name=display, description=description, project_key=key),
        "CLAUDE.md": _claude_md(
            display_name=display,
            description=description,
            project_key=key,
            org=org_name,
            vault_root=Path(vault_root).expanduser(),
            remote_url=remote_url,
        ),
        ".gitignore": _gitignore(),
    }
    written: list[str] = []
    existed: list[str] = []
    for name, content in rendered.items():
        target = confined(repo, name)
        if target.exists():
            if name == ".gitignore":
                current = target.read_text(encoding="utf-8")
                missing = [line for line in GITIGNORE_REQUIRED_LINES if line not in current.splitlines()]
                if missing:
                    suffix = "" if current.endswith("\n") else "\n"
                    target.write_text(current + suffix + "".join(f"{line}\n" for line in missing), encoding="utf-8")
                    written.append(name)
                else:
                    existed.append(name)
            else:
                existed.append(name)
            continue
        target.write_text(content, encoding="utf-8")
        written.append(name)

    # Fixed name under the resolved repo dir: join directly, because
    # confined() resolves the existing symlink to its target.
    agents = repo / "AGENTS.md"
    if agents.is_symlink() and agents.readlink() == Path("CLAUDE.md"):
        existed.append("AGENTS.md")
    elif agents.exists() or agents.is_symlink():
        raise ProjectCreationError(
            f"AGENTS.md already exists and is not the CLAUDE.md symlink: {agents}"
        )
    else:
        agents.symlink_to("CLAUDE.md")
        written.append("AGENTS.md")

    work = confined(repo, "docs", "work")
    work.mkdir(parents=True, exist_ok=True)
    gitkeep = confined(repo, "docs", "work", ".gitkeep")
    if gitkeep.exists():
        existed.append("docs/work/.gitkeep")
    else:
        gitkeep.write_text("", encoding="utf-8")
        written.append("docs/work/.gitkeep")

    committed, sha = _commit_if_needed(repo)
    return {
        "path": str(repo),
        "written": written,
        "existed": existed,
        "committed": committed,
        "sha": sha,
    }


def _commit_if_needed(repo: Path) -> tuple[bool, str | None]:
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    if head.returncode == 0:
        status = _git(repo, "status", "--porcelain")
        if status.returncode == 0 and not status.stdout.strip():
            sha = _git(repo, "rev-parse", "HEAD")
            return False, sha.stdout.strip() if sha.returncode == 0 else None
    add = _git(repo, "add", "-A")
    if add.returncode != 0:
        raise ProjectCreationError(f"git add failed: {add.stderr.strip()[:300]}")
    commit = _git(
        repo,
        "-c",
        f"user.name={COMMIT_AUTHOR_NAME}",
        "-c",
        f"user.email={COMMIT_AUTHOR_EMAIL}",
        "commit",
        "-m",
        COMMIT_MESSAGE,
    )
    if commit.returncode != 0:
        raise ProjectCreationError(f"git commit failed: {commit.stderr.strip()[:300]}")
    sha = _git(repo, "rev-parse", "HEAD")
    return True, sha.stdout.strip() if sha.returncode == 0 else None


# ---------------------------------------------------------------------------
# projects/<key>/project.yaml (mirror of jardal-openceo, validated)
# ---------------------------------------------------------------------------

def _presets_yaml() -> str:
    return (
        "presets:\n"
        "  review:\n"
        "    number: 1\n"
        '    prompt: "Revisa los cambios recientes en git diff, valida el cumplimiento estricto de las reglas de AGENTS.md y confirma que no haya errores de sintaxis ni regresiones."\n'
        "    description: \"Revisar git diff y reglas de AGENTS.md\"\n"
        "  test_fix:\n"
        "    number: 2\n"
        '    prompt: "Ejecuta las pruebas unitarias del proyecto y si alguna falla analiza el traceback y corrige el código hasta que todas pasen."\n'
        "    description: \"Ejecutar tests y autocorregir fallos\"\n"
        "  refactor:\n"
        "    number: 3\n"
        '    prompt: "Analiza el código reciente, simplifica y optimiza la lógica manteniendo total compatibilidad hacia atrás y estilo limpio."\n'
        "    description: \"Refactorizar y optimizar código reciente\"\n"
        "  sync_context:\n"
        "    number: 4\n"
        '    prompt: "Revisa el estado del proyecto y actualiza STATUS.md y el ticket ERPNext correspondiente con un resumen claro del trabajo realizado y estado vigente."\n'
        "    description: \"Sincronizar STATUS.md y ticket\"\n"
        "  security_audit:\n"
        "    number: 5\n"
        '    prompt: "Audita el código en busca de posibles vulnerabilidades, endpoints expuestos sin autenticación o fugas de configuración."\n'
        "    description: \"Auditoría de seguridad y endpoints\"\n"
    )


def _project_yaml(
    *,
    project_key: str,
    number: int,
    remote_url: str,
    repo_path: str,
    vault_root: str,
    vault_project: str,
) -> str:
    providers = ""
    for provider, command in (
        ("codex", '["/home/develop/.local/bin/codexf"]'),
        ("claude", '["/home/develop/.local/bin/claudej"]'),
        ("muse", '["/home/develop/.local/bin/musej"]'),
        ("gemini", '["/home/develop/.local/bin/agyd"]'),
        ("opencode", '["/home/develop/.opencode/bin/opencode", "-m", "deepseek/deepseek-reasoner"]'),
    ):
        providers += (
            f"    {provider}:\n"
            f"      command: {command}\n"
            f"      cwd: {repo_path}\n"
            f"      env: {{}}\n"
            f"\n"
        )
    return (
        f"project:\n"
        f"  number: {number}\n"
        f"  name: {project_key}\n"
        f"  repos:\n"
        f"    - name: {project_key}\n"
        f"      url: {remote_url}\n"
        f"      branch_base: main\n"
        f"      local_path: {repo_path}\n"
        f"\n"
        f"dev:\n"
        f"  # Tier S design §3/§15: no dev.coding_tool (routing is per-task) and no\n"
        f"  # dev.tier_s (cycle config is global in config_loader.py, as are the\n"
        f"  # shared review/objectives/objective_planner blocks).\n"
        f"  coding_tier: auto\n"
        f"  default_provider: muse\n"
        f"  branch_pattern: \"{{session_id}}-{{slug}}\"\n"
        f"  auto_push: false\n"
        f"  create_pr: true\n"
        f"  pr_body: auto\n"
        f"  auto_merge: true\n"
        f"  budget_exclude: []\n"
        f"  gates: []\n"
        f"  # Vault location for Tier S knowledge notes (design §9.8, F7b).\n"
        f"  vault_project:\n"
        f"    dir: {vault_root}\n"
        f"    project: {vault_project}\n"
        f"\n"
        f"  providers:\n"
        f"{providers}"
        f"  qa:\n"
        f"    playwright: skip\n"
        f"    confidence_threshold: 0.90\n"
        f"    max_retries: 2\n"
        f"\n"
        f"voice:\n"
        f"  peer_programming:\n"
        f"    enabled: true\n"
        f"    require_project_selection: false\n"
        f"\n"
        f"notifications: []\n"
        f"\n"
        f"{_presets_yaml()}"
    )


def next_project_number(projects_root: Path | str) -> int:
    """Max ``project.number`` + 1. Fail-closed on an unreadable project.yaml."""
    import yaml

    root = Path(projects_root).expanduser().resolve()
    best = 0
    if not root.exists():
        return 1
    for candidate in sorted(root.iterdir()):
        cfg = candidate / "project.yaml"
        if not candidate.is_dir() or not cfg.exists():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ProjectCreationError(f"{candidate.name}/project.yaml: unreadable ({exc})") from exc
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        number = data.get("number", project.get("number"))
        try:
            value = int(number) if number is not None else 0
        except (TypeError, ValueError):
            value = 0
        best = max(best, value)
    return best + 1


def ensure_project_config(
    projects_root: Path | str,
    project_key: str,
    *,
    remote_url: str,
    repo_path: str,
    vault_root: str,
    vault_project: str,
) -> dict[str, Any]:
    """Write ``projects/<key>/project.yaml`` once; never overwrite.

    Validation runs in the workflow tool (``ConfigLoader.validate_project``)
    so a template bug fails the run instead of persisting a bad config.
    """
    import yaml

    key = normalize_key(project_key)
    root = Path(projects_root).expanduser().resolve()
    target = confined(root, key, "project.yaml")
    if target.exists():
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise ProjectCreationError(f"{key}/project.yaml: unreadable ({exc})") from exc
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        if project.get("name") != key:
            raise ProjectCreationError(
                f"{key}/project.yaml already exists for another project "
                f"({project.get('name')!r}): refusing to overwrite"
            )
        number = project.get("number", data.get("number"))
        return {"path": str(target), "created": False, "number": number}

    number = next_project_number(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _project_yaml(
            project_key=key,
            number=number,
            remote_url=remote_url,
            repo_path=repo_path,
            vault_root=vault_root,
            vault_project=vault_project,
        ),
        encoding="utf-8",
    )
    return {"path": str(target), "created": True, "number": number}


# ---------------------------------------------------------------------------
# Vault hub + README line (index maintenance: own entry only)
# ---------------------------------------------------------------------------

def _hub_md(*, project_key: str, org: str, display_name: str, description: str) -> str:
    blurb = description.strip() or f"Proyecto {display_name}."
    today = date.today().isoformat()
    base = f"projects/{org}/{project_key}"
    _ = base  # hub starts link-free; notes link back to it as they appear.
    return (
        "---\n"
        "type: hub\n"
        f"org: {org}\n"
        f"project: {project_key}\n"
        f"date: {today}\n"
        "---\n"
        "\n"
        f"# {project_key}\n"
        "\n"
        f"{blurb} Este hub contiene conocimiento estable; los contratos completos, "
        f"instrucciones y estado operativo siguen en el repositorio y el codigo "
        f"prevalece ante discrepancias.\n"
        "\n"
        "## Arquitectura\n"
        "\n"
        "(sin notas todavía)\n"
        "\n"
        "## Componentes\n"
        "\n"
        "(sin notas todavía)\n"
        "\n"
        "## Decisiones\n"
        "\n"
        "(sin notas todavía)\n"
        "\n"
        "## Incidentes\n"
        "\n"
        "(sin notas todavía)\n"
    )


def readme_line(*, org: str, project_key: str, display_name: str, source_path: str) -> str:
    return f"- [[projects/{org}/{project_key}|{display_name}]] — {source_path}"


def ensure_vault_entry(
    vault_root: Path | str,
    org: str,
    project_key: str,
    *,
    display_name: str,
    description: str,
    source_path: str,
) -> dict[str, Any]:
    """Create the project hub and append its README line, both idempotent.

    Never touches another project's lines: the README update is append-only
    and skips when the hub path is already referenced.
    """
    org_name = normalize_org(org)
    key = normalize_key(project_key)
    description = str(description or "")
    root = Path(vault_root).expanduser().resolve()
    hub_dir = confined(root, "projects", org_name, key)
    hub_dir.mkdir(parents=True, exist_ok=True)

    hub = confined(root, "projects", org_name, key, f"{key}.md")
    display = (display_name or "").strip() or key
    if hub.exists():
        created_hub = False
    else:
        hub.write_text(
            _hub_md(project_key=key, org=org_name, display_name=display, description=description),
            encoding="utf-8",
        )
        created_hub = True

    readme = confined(root, "README.md")
    if not readme.exists():
        raise ProjectCreationError(f"vault README not found: {readme}")
    lines = readme.read_text(encoding="utf-8").splitlines()
    marker = f"[[projects/{org_name}/{key}"
    line = readme_line(org=org_name, project_key=key, display_name=display, source_path=source_path)
    if any(marker in existing for existing in lines):
        readme_updated = False
    else:
        current = readme.read_text(encoding="utf-8")
        suffix = "" if current.endswith("\n") else "\n"
        readme.write_text(current + suffix + line + "\n", encoding="utf-8")
        readme_updated = True

    return {
        "hub": str(hub),
        "created_hub": created_hub,
        "readme_updated": readme_updated,
    }


# ---------------------------------------------------------------------------
# Teardown (test projects only; guarded by identity checks)
# ---------------------------------------------------------------------------

def remove_git_repo(repos_root: Path | str, repo_name: str) -> dict[str, Any]:
    """Delete the local repo. Refuses a directory without ``.git``."""
    name = normalize_key(repo_name, field="repo_name")
    path = confined(Path(repos_root), name)
    if not path.exists():
        return {"path": str(path), "removed": False}
    if not (path / ".git").exists():
        raise ProjectCreationError(f"refusing to delete a non-git directory: {path}")
    shutil.rmtree(path)
    return {"path": str(path), "removed": True}


def remove_project_config(projects_root: Path | str, project_key: str) -> dict[str, Any]:
    """Delete ``projects/<key>/``. Refuses a config naming another project."""
    import yaml

    key = normalize_key(project_key)
    target = confined(Path(projects_root), key, "project.yaml")
    if not target.exists():
        return {"path": str(target.parent), "removed": False}
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ProjectCreationError(f"{key}/project.yaml: unreadable ({exc})") from exc
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    if project.get("name") != key:
        raise ProjectCreationError(
            f"refusing to delete {key}/project.yaml naming {project.get('name')!r}"
        )
    shutil.rmtree(target.parent)
    return {"path": str(target.parent), "removed": True}


def remove_vault_entry(vault_root: Path | str, org: str, project_key: str) -> dict[str, Any]:
    """Delete the hub (only when it holds no notes) and its README line."""
    import yaml

    org_name = normalize_org(org)
    key = normalize_key(project_key)
    root = Path(vault_root).expanduser().resolve()
    hub = confined(root, "projects", org_name, key, f"{key}.md")

    removed_hub = False
    if hub.exists():
        try:
            frontmatter = (yaml.safe_load_all(hub.read_text(encoding="utf-8")) or [])
            first = next(iter(frontmatter), None)
        except Exception as exc:
            raise ProjectCreationError(f"hub {hub} unreadable: {exc}") from exc
        if not isinstance(first, dict) or first.get("project") != key:
            raise ProjectCreationError(f"refusing to delete a hub not naming {key}: {hub}")
        extras = [p for p in hub.parent.rglob("*") if p != hub and p.name != "__pycache__"]
        if extras:
            raise ProjectCreationError(
                f"refusing to delete hub {hub.parent}: it holds notes ({len(extras)} extra paths)"
            )
        shutil.rmtree(hub.parent)
        removed_hub = True

    readme = confined(root, "README.md")
    removed_line = False
    if readme.exists():
        lines = readme.read_text(encoding="utf-8").splitlines()
        marker = f"[[projects/{org_name}/{key}"
        matches = [line for line in lines if marker in line]
        if len(matches) > 1:
            raise ProjectCreationError(f"ambiguous README lines for {org_name}/{key}: refusing")
        if matches:
            kept = [line for line in lines if marker not in line]
            readme.write_text("\n".join(kept) + "\n", encoding="utf-8")
            removed_line = True

    return {"hub": str(hub), "removed_hub": removed_hub, "removed_line": removed_line}
