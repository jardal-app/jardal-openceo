"""Pilot 1 preflight: deterministic readiness probe (TASK-2026-00234).

Answers ``listo`` or the exact list of what is missing. No tokens, no
external effects: localhost health at most, tmp-only skill dry-run,
in-process MCP checks, local git refs (no fetch).

Run it from ``openceo-core``::

    ../.venv/bin/python pilot_preflight.py [--json] [--port 18765]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "Check",
    "PreflightResult",
    "check_catalog_skills",
    "check_credentials",
    "check_health",
    "check_mcp_identity",
    "check_repo_main_sync",
    "check_roster_clis",
    "check_single_instance",
    "check_skill_dry_run",
    "main",
    "repo_root",
    "required_binaries",
    "run_preflight",
]

_PROBE_MIRROR = "TASK-2026-09999"
_PROBE_PROJECT = "jardal-openceo"
_OUT_OF_BOX_TOOL = "openceo_send_email"

#: Credentials the pilot needs. Presence only: values never surface.
REQUIRED_CREDENTIALS = (
    "JARDAL_ERP_URL",
    "JARDAL_ERP_SERVICE_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "MONGODB_URL",
)

_GIT_TIMEOUT_S = 60
_SKILL_TIMEOUT_S = 180
_HEALTH_TIMEOUT_S = 5


def repo_root() -> Path:
    """Live checkout root (this file lives in ``openceo-core/``)."""
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Check:
    """One readiness probe: pass/fail plus the exact detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightResult:
    """Every check, in run order."""

    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def summary(self) -> str:
        if self.ok:
            return "listo"
        missing = [check for check in self.checks if not check.ok]
        lines = [f"NO LISTO - faltan {len(missing)}:"]
        lines.extend(f"- {check.name}: {check.detail}" for check in missing)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [{"name": check.name, "ok": check.ok,
                        "detail": check.detail} for check in self.checks],
        }


def _live_proc_cmdlines() -> dict[int, str]:
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            found[int(entry.name)] = raw.replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
    return found


def _live_ss_text() -> str:
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _port_owner(ss_text: str, port: int) -> int | None:
    import re

    for line in ss_text.splitlines():
        if not re.search(rf":{port}\s", line):
            continue
        match = re.search(r"pid=(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def check_single_instance(
    *,
    port: int = 18765,
    proc_cmdlines: Mapping[int, str] | None = None,
    ss_text: str | None = None,
    venv_python: str | None = None,
) -> Check:
    """Exactly one service instance, owning the API port.

    Same match as the canonical launcher (``venv/bin/python main.py``).
    """
    expected = venv_python or str(repo_root() / ".venv" / "bin" / "python")
    procs = dict(proc_cmdlines) if proc_cmdlines is not None \
        else _live_proc_cmdlines()
    pids = sorted(pid for pid, cmd in procs.items()
                  if (lambda parts: len(parts) > 1
                      and parts[0] == expected
                      and parts[1] == "main.py")(cmd.split()))
    if not pids:
        return Check("single-instance", False,
                     "0 instancias con venv/bin/python main.py")
    if len(pids) > 1:
        return Check("single-instance", False,
                     f"{len(pids)} instancias (pids {pids}), solo una puede correr")
    text = ss_text if ss_text is not None else _live_ss_text()
    if not text:
        return Check("single-instance", False,
                     "ss no disponible: dueno del puerto sin verificar")
    owner = _port_owner(text, port)
    if owner is None:
        return Check("single-instance", False,
                     f"puerto {port} sin dueno (servicio caido?)")
    if owner != pids[0]:
        return Check(
            "single-instance", False,
            f"pid {pids[0]} no es dueno del puerto {port} (dueno: {owner})")
    return Check("single-instance", True,
                 f"pid {pids[0]} dueno del puerto {port}")


def _live_git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_S)
    return result.returncode, (result.stdout or "").strip()


def check_repo_main_sync(
    *,
    repo: str | Path | None = None,
    run: Callable[[Sequence[str], Path], tuple[int, str]] | None = None,
) -> Check:
    """The checkout runs main: branch, clean tree, level with origin."""
    root = Path(repo) if repo is not None else repo_root()
    git = run or _live_git
    code, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if code != 0:
        return Check("repo-main-sync", False,
                     f"no es un repo git ({root})")
    if branch != "main":
        return Check("repo-main-sync", False,
                     f"rama {branch}, se necesita main")
    code, dirty = git(["status", "--porcelain"], root)
    if code != 0:
        return Check("repo-main-sync", False,
                     "repo ilegible (git status fallo)")
    if dirty:
        first = dirty.splitlines()[0].strip()
        return Check("repo-main-sync", False,
                     f"arbol sucio ({first}...)")
    code, head = git(["rev-parse", "HEAD"], root)
    if code != 0 or not head:
        return Check("repo-main-sync", False,
                     "repo ilegible (rev-parse HEAD fallo)")
    code_origin, origin = git(["rev-parse", "refs/remotes/origin/main"], root)
    if code_origin != 0 or not origin:
        return Check("repo-main-sync", False,
                     "sin ref origin/main (fetch pendiente)")
    if head != origin:
        return Check("repo-main-sync", False,
                     f"main local {head[:12]} != origin/main {origin[:12]}")
    return Check("repo-main-sync", True,
                 f"main==origin/main {head[:12]}, limpio")


def check_catalog_skills(
    *,
    repo: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., Any] | None = None,
) -> Check:
    """Catalog loads, operations skills load, the skill validates."""
    from catalog import CatalogError, load_catalog, load_role_skills

    root = Path(repo) if repo is not None else repo_root()
    try:
        catalog = load_catalog()
    except CatalogError as exc:
        return Check("catalog-skills", False, f"catalogo invalido: {exc}")
    try:
        skills = load_role_skills("operations", "skills/operations", root)
    except CatalogError as exc:
        return Check("catalog-skills", False,
                     f"skills de operations invalidas: {exc}")
    names = sorted(skill.name for skill in skills)
    if "crear-proyecto" not in names:
        return Check("catalog-skills", False,
                     f"falta crear-proyecto en {names}")
    find = which or shutil.which
    muse = find("muse")
    if not muse:
        return Check("catalog-skills", False, "binario muse ausente")
    skill_dir = root / "skills" / "operations" / "crear-proyecto"
    runner = run or subprocess.run
    result = runner([muse, "skills", "validate", str(skill_dir)],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "MUSE_NO_AUTO_UPDATE": "1"})
    if result.returncode != 0:
        out = (result.stderr or result.stdout or "").strip()[:300]
        return Check("catalog-skills", False,
                     f"muse skills validate fallo: {out}")
    agents = len(catalog.agents)
    return Check("catalog-skills", True,
                 f"{agents} agentes, operations={names}, validate ok")


def check_mcp_identity() -> Check:
    """Badge minting, box filtering and unauthenticated rejection."""
    from catalog import load_catalog
    from mcp_identity import (
        McpAuthError,
        MemoryTokenStore,
        TokenIssuer,
        box_for_agent,
    )

    async def _probe() -> tuple[frozenset[str], frozenset[str]]:
        catalog = load_catalog()
        card = catalog.agents.get("operations")
        if card is None:
            raise LookupError("operations card missing")
        issuer = TokenIssuer(MemoryTokenStore(), catalog.agents)
        badge = await issuer.issue_agent(
            "operations", _PROBE_MIRROR, _PROBE_PROJECT)
        grant = await issuer.authenticate(badge)
        box = box_for_agent(card)
        if frozenset(grant.tools) != box:
            raise AssertionError("grant box != card box")
        if _OUT_OF_BOX_TOOL in grant.tools:
            raise AssertionError(f"{_OUT_OF_BOX_TOOL} leaked into the box")
        for bogus in ("", "bogus-badge"):
            try:
                await issuer.authenticate(bogus)
            except McpAuthError:
                continue
            raise AssertionError(f"badge {bogus!r} accepted")
        await issuer.revoke(badge, "preflight probe finished")
        try:
            await issuer.authenticate(badge)
        except McpAuthError:
            pass
        else:
            raise AssertionError("revoked badge still accepted")
        return box, frozenset(grant.tools)

    try:
        box, _ = asyncio.run(_probe())
    except Exception as exc:
        return Check("mcp-identity", False, f"{exc}"[:300])
    return Check("mcp-identity", True,
                 f"operations ve {len(box)} tools, fuera de caja denegado, "
                 "sin gafete rechazado")


def required_binaries(config: Mapping[str, Any]) -> list[str]:
    """Every binary an active config command needs, plus git and muse.

    Walks ``command`` lists: the head is the binary (PATH lookup, or a
    file check when absolute), absolute path members are files too.
    Entries with ``active: false`` are skipped with their subtree.
    """
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if node.get("active") is False:
                return
            command = node.get("command")
            if isinstance(command, (list, tuple)) and command:
                head = str(command[0])
                if head:
                    found.add(head)
                for member in command[1:]:
                    text = str(member)
                    if text.startswith("/"):
                        found.add(text)
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(config)
    found.update(("git", "muse"))
    return sorted(found)


def _live_config() -> Mapping[str, Any]:
    from config_loader import ConfigLoader
    from settings import Settings

    return ConfigLoader(Settings()).load_project(None)


def check_roster_clis(
    *,
    config: Mapping[str, Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    is_file: Callable[[str], bool] | None = None,
) -> Check:
    """Every binary the live roster needs is present."""
    find = which or shutil.which
    exists = is_file or (lambda path: Path(path).is_file())
    try:
        needed = required_binaries(
            config if config is not None else _live_config())
    except Exception as exc:
        return Check("roster-clis", False, f"config ilegible: {exc}"[:200])
    missing = [name for name in needed
               if (not exists(name) if name.startswith("/")
                   else not find(name))]
    if missing:
        return Check("roster-clis", False, f"faltan: {', '.join(missing)}")
    return Check("roster-clis", True, f"{len(needed)}/{len(needed)} presentes")


def _live_dotenv() -> dict[str, str]:
    """Both Settings sources merged (.env, then .env.finance.local)."""
    try:
        from dotenv import dotenv_values

        merged: dict[str, str] = {}
        for name in (".env", ".env.finance.local"):
            path = repo_root() / "openceo-core" / name
            if path.is_file():
                merged.update({str(key): str(value or "")
                               for key, value in dotenv_values(path).items()})
        return merged
    except ImportError:
        values: dict[str, str] = {}
        for name in (".env", ".env.finance.local"):
            path = repo_root() / "openceo-core" / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
        return values


def check_credentials(
    *,
    dotenv: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Check:
    """Required credentials are present, named but never shown.

    Both Settings sources count: the dotenv files and the process
    environment (pydantic-settings reads env too).
    """
    values = dict(dotenv) if dotenv is not None else _live_dotenv()
    env = dict(environ) if environ is not None else dict(os.environ)
    for name in REQUIRED_CREDENTIALS:
        if str(env.get(name) or "").strip():
            values[name] = env[name]
    missing = [name for name in REQUIRED_CREDENTIALS
               if not str(values.get(name) or "").strip()]
    if missing:
        return Check("credentials", False,
                     f"faltan: {', '.join(missing)} "
                     "(openceo-core/.env + .env.finance.local)")
    return Check("credentials", True,
                 f"{len(REQUIRED_CREDENTIALS)}/{len(REQUIRED_CREDENTIALS)} "
                 "presentes (nombres)")


def check_health(
    *,
    port: int = 18765,
    get: Callable[[str], int] | None = None,
) -> Check:
    """The local API answers health on the service port."""
    def _live_get(url: str) -> int:
        with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as resp:
            return int(resp.status)

    try:
        status = (get or _live_get)(f"http://127.0.0.1:{port}/health")
    except Exception as exc:
        return Check("health", False, f"sin respuesta: {exc}"[:200])
    if status != 200:
        return Check("health", False, f"health devolvio {status}")
    return Check("health", True, "200")


def check_skill_dry_run(
    *,
    repo: str | Path | None = None,
    run: Callable[..., Any] | None = None,
) -> Check:
    """The real crear-proyecto CLI passes dry-run end to end."""
    root = Path(repo) if repo is not None else repo_root()
    script = (root / "skills" / "operations" / "crear-proyecto"
              / "scripts" / "crear_proyecto.py")
    if not script.is_file():
        return Check("skill-dry-run", False, f"falta {script}")
    runner = run or subprocess.run
    identity = ["--project-key", "preflight-probe",
                "--display-name", "Preflight probe"]
    plans: list[list[str]] = [
        ["roots", "--dry-run"],
        ["templates", *identity, "--dry-run"],
        ["project-config", *identity, "--dry-run"],
        ["vault-entry", *identity, "--dry-run"],
    ]
    for plan in plans:
        result = runner([sys.executable, str(script), *plan],
                        capture_output=True, text=True,
                        timeout=_SKILL_TIMEOUT_S)
        if result.returncode != 0:
            out = (result.stderr or result.stdout or "").strip()[:300]
            return Check("skill-dry-run", False,
                         f"{' '.join(plan)} fallo: {out}")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            return Check("skill-dry-run", False,
                         f"{' '.join(plan)} sin JSON")
        if not payload.get("ok") or not payload.get("dry_run"):
            return Check("skill-dry-run", False,
                         f"{' '.join(plan)} no-ok: {str(payload)[:200]}")
    return Check("skill-dry-run", True,
                 "roots+templates+project-config+vault-entry ok (dry-run)")


def _default_port() -> int:
    """Service port the launcher way: OPENCEO_HOST_PORT, OPENCEO_PORT."""
    for name in ("OPENCEO_HOST_PORT", "OPENCEO_PORT"):
        raw = str(os.environ.get(name) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 18765


def run_preflight(
    *,
    port: int | None = None,
    proc_cmdlines: Mapping[int, str] | None = None,
    ss_text: str | None = None,
    venv_python: str | None = None,
    git_run: Callable[[Sequence[str], Path], tuple[int, str]] | None = None,
    which: Callable[[str], str | None] | None = None,
    is_file: Callable[[str], bool] | None = None,
    config: Mapping[str, Any] | None = None,
    dotenv: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    health_get: Callable[[str], int] | None = None,
    skill_run: Callable[..., Any] | None = None,
) -> PreflightResult:
    """Run every check with live probes unless fakes are injected."""
    port = _default_port() if port is None else port
    checks = (
        check_single_instance(port=port, proc_cmdlines=proc_cmdlines,
                              ss_text=ss_text, venv_python=venv_python),
        check_repo_main_sync(run=git_run),
        check_catalog_skills(which=which, run=skill_run),
        check_mcp_identity(),
        check_roster_clis(config=config, which=which, is_file=is_file),
        check_credentials(dotenv=dotenv, environ=environ),
        check_health(port=port, get=health_get),
        check_skill_dry_run(run=skill_run),
    )
    return PreflightResult(checks=checks)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: print ``listo`` or the missing list, exit 0/1."""
    parser = argparse.ArgumentParser(
        description="Pilot 1 preflight: listo o la lista exacta de faltas")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_preflight(port=args.port)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.summary())
        if result.ok:
            for check in result.checks:
                print(f"- {check.name}: {check.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
