#!/usr/bin/env python3
"""Epic runner: muse workers across sessions, read-only reviewers, own checks.

Drives one epic's children in dependency order, one ticket at a time:

  work    headless `muse exec` sessions on the ticket branch. A session that
          cannot finish hands off (TICKET_CONTINUE) and a fresh one resumes
          from the ticket comments and the branch, until TICKET_READY.
  review  opencode (free models, next one on quota) and agy2 run in
          parallel, read-only under bwrap. Every report that lands is used;
          one is enough, so a failed reviewer never blocks the ticket.
  close   a muse session weighs the findings, fixes, merges, pushes,
          restarts and closes the ticket (TICKET_DONE).
  verify  this loop re-checks git, tests, vault notes, service and ticket
          state itself before marking the ticket done.

Progress lives in .openceo-automatico/state.json, so every run resumes where
the last one stopped. A ticket that waits on the Owner (TICKET_OWNER) or is
blocked (TICKET_BLOCKED) parks only its dependents. Ticket state is read
from Jardal ERP through the business adapter, without spending tokens.

Usage:
    ./automatico.py --dry-run            # print the plan, spawn nothing
    ./automatico.py                      # run or resume the epic
    ./automatico.py --only TASK-2026-00323
    ./automatico.py --reset              # forget saved progress

Safety: refuses to start on a dirty worktree, a diverged main or a sick
service; single instance per repo (flock); per-session timeouts that kill
the whole process group; bounded sessions per ticket and run; nothing is
marked done without independent verification.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
CORE = REPO / "openceo-core"
PYTHON = REPO / ".venv" / "bin" / "python"
KNOWLEDGE = REPO.parent / "jardal-knowledge"
VAULT_PROJECT = KNOWLEDGE / "vault" / "projects" / "jardal" / "jardal-openceo"
SKILL = "llm-vault-links"


def _vault_check() -> Path:
    return (KNOWLEDGE / ".agents" / "skills" / SKILL
            / "scripts" / "vault_check.py")


def _knowledge_tests() -> list[str]:
    return [f".agents/skills/{SKILL}/scripts/test_vault_check.py",
            "scripts/test_nightly_git_backup.py"]
EPIC = "TASK-2026-00365"
STATE_DIR = REPO / ".openceo-automatico"
EPICS_DIR = STATE_DIR / "epics"
REVIEWS_DIR = STATE_DIR / "reviews"
HEALTH_URL = "http://127.0.0.1:18765/health"

WORK_READY = "TICKET_READY"
WORK_CONTINUE = "TICKET_CONTINUE"
WORK_DONE = "TICKET_DONE"
WORK_OWNER = "TICKET_OWNER"
WORK_BLOCKED = "TICKET_BLOCKED"
EPIC_DONE = "EPIC_DONE"
MARKERS = (WORK_READY, WORK_CONTINUE, WORK_DONE, WORK_OWNER, WORK_BLOCKED,
           EPIC_DONE)
CLOSED = ("Completed", "Cancelled")

GATE_TESTS = [
    "tests/test_tier_s_final.py",
    "tests/test_tier_s_contract.py",
    "tests/test_coding_tool_routing.py",
    "tests/test_tier_s_config_validation.py",
]
KNOWLEDGE_SCOPE = ("vault/projects/jardal/jardal-openceo/", "vault/templates/",
                   ".agents/skills/llm-vault-links/", "scripts/",
                   "config/", "README.md")
OPENCODE_MODELS = tuple(f"opencode/{name}" for name in (
    "deepseek-v4.1-flash-free", "glm-5.3-flash-free",
    "nemotron-3.5-super-vl-free", "flint-40b-free", "beam-free",
    "mimo-v2.6-flash-free", "nemotron-3.5-lightning-free",
    "nemotron-3-ultra-free"))


def _child(step: str, ticket: str, slug: str, deps: tuple[str, ...] = (),
           tests: tuple[str, ...] = (), knowledge: bool = False,
           workspace: Path = REPO) -> dict:
    branch = f"auto/{ticket[-5:]}-{slug}" if workspace == REPO else ""
    return {"step": step, "ticket": ticket, "branch": branch,
            "deps": list(deps), "tests": list(tests),
            "knowledge": knowledge, "workspace": workspace}


def direction(epic: str) -> str:
    return f"la épica {epic}"


def _coerce_child(raw: dict, epic: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("cada hijo debe ser un objeto")
    for key in ("step", "ticket", "branch", "deps", "tests", "knowledge"):
        if key not in raw:
            raise ValueError(f"hijo sin clave {key!r}: {raw!r}"[:200])
    ticket = str(raw["ticket"])
    if not ticket.startswith("TASK-"):
        raise ValueError(f"ticket inválido: {ticket!r}")
    workspace = KNOWLEDGE if raw.get("workspace") == "knowledge" else REPO
    return {"step": str(raw["step"]), "ticket": ticket,
            "branch": str(raw["branch"]), "epic": epic,
            "deps": [str(d) for d in raw["deps"]],
            "tests": [str(t) for t in raw["tests"]],
            "knowledge": bool(raw["knowledge"]), "workspace": workspace}


def _slugify(text: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:40] or "hijo"


def _children_from_erp(epic: str) -> list[dict]:
    names = sorted(read_erp([], epic)["children"])
    tickets = read_erp(names, epic)["tickets"] if names else {}
    plan = []
    for index, name in enumerate(names):
        info = tickets.get(name, {})
        deps = [d for d in (info.get("depends_on") or []) if d in names]
        plan.append({"step": str(index), "ticket": name, "epic": epic,
                     "branch": f"auto/{name[-5:]}-{_slugify(info.get('subject') or 'hijo')}",
                     "deps": deps, "tests": [],
                     "knowledge": False, "workspace": REPO})
    return plan


def load_epic_children(epic: str,
                       config_dir: Path = EPICS_DIR) -> tuple[list[dict], str]:
    path = Path(config_dir) / f"{epic}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"config de épica inválida en {path}: {exc}") from exc
        kids = data.get("children", data) if isinstance(data, dict) else data
        if not isinstance(kids, list) or not kids:
            raise ValueError(f"config de épica vacía en {path}")
        if isinstance(data, dict) and "epic" in data and data["epic"] != epic:
            raise ValueError(f"config de {path} es de {data['epic']}, no de {epic}")
        return [_coerce_child(raw, epic) for raw in kids], f"config {path.name}"
    return _children_from_erp(epic), "hijos del ERP"


def release_info() -> dict:
    try:
        data = json.loads((REPO / ".runtime" / "RELEASE").read_text())
    except (OSError, ValueError):
        return {"sha": "unknown", "dirty": None}
    if not isinstance(data, dict):
        return {"sha": "unknown", "dirty": None}
    dirty = data.get("dirty")
    return {"sha": str(data.get("sha") or "unknown"),
            "dirty": bool(dirty) if isinstance(dirty, bool) else None}

TICKET_READER = """
import asyncio, json, sys
from settings import get_settings
from business.provider import build_business_work

async def main(epic, names):
    work = build_business_work(get_settings())
    try:
        out = {"children": {}, "tickets": {}}
        if epic:
            for row in await work.list_children(epic):
                out["children"][row["name"]] = row.get("status")
        for name in names:
            thread = await work.list_thread(name, limit=1)
            ticket, comments = thread["ticket"], thread["comments"]
            out["tickets"][name] = {
                "status": ticket.get("status"),
                "subject": ticket.get("subject") or "",
                "description": ticket.get("description") or "",
                "depends_on": [d.get("task") for d in ticket.get("depends_on") or []],
                "owner_replied": bool(comments)
                and comments[0].get("authorKind") == "human",
            }
        print(json.dumps(out))
    finally:
        await work.aclose()

asyncio.run(main(sys.argv[1], sys.argv[2:]))
"""


@dataclass
class Settings:
    dry_run: bool = False
    reset: bool = False
    epic: str = ""
    only: str = ""
    worker_timeout: int = 5400
    reviewer_timeout: int = 1800
    max_sessions: int = 8
    max_fix: int = 2
    model: str = ""
    effort: str = "high"
    max_steps: int = 200


_LIVE: set[subprocess.Popen] = set()


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def run(args: list[str], timeout: int, cwd: Path = REPO,
        env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", start_new_session=True)
    _LIVE.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out or "", (err or "") + f"\n[timeout {timeout}s]"
    finally:
        _LIVE.discard(proc)


def sh(args: list[str], timeout: int, cwd: Path = REPO) -> tuple[int, str]:
    code, out, err = run(args, timeout, cwd)
    return code, (out + err)[-8000:]


def health() -> str:
    _code, out = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     HEALTH_URL], 30)
    return out.strip()


def read_erp(names: list[str], epic: str = "") -> dict:
    code, out, err = run([str(PYTHON), "-c", TICKET_READER, epic, *names],
                         180, cwd=CORE)
    lines = out.strip().splitlines()
    if code != 0 or not lines:
        raise RuntimeError(f"lectura de Jardal ERP falló (exit={code}): "
                           f"{err[-600:]}")
    return json.loads(lines[-1])


def ticket_info(ticket: str) -> dict:
    return read_erp([ticket])["tickets"][ticket]


def load_state(reset: bool, archive: bool, epic: str = EPIC) -> dict:
    path = STATE_DIR / "state.json"
    if path.exists() and not reset:
        state = json.loads(path.read_text())
        if state.get("epic") == epic and "tickets" in state:
            return state
        if archive:
            old = str(state.get("epic", "old"))[-5:]
            path.rename(STATE_DIR / f"state-{old}.json")
            log(f"estado de {state.get('epic')} archivado en state-{old}.json")
    return {"epic": epic, "tickets": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_DIR / "state.json")


def ticket_state(state: dict, ticket: str) -> dict:
    return state["tickets"].setdefault(ticket, {
        "phase": "work", "parked": "", "note": "", "failure": "",
        "sessions": 0, "base": {}, "reviews": [], "ready": ""})


def git_head(repo: Path, ref: str = "HEAD") -> str:
    _code, out, _err = run(["git", "-C", str(repo), "rev-parse", ref], 30)
    return out.strip()


def branch_exists(branch: str) -> bool:
    code, _out, _err = run(["git", "rev-parse", "--verify", "--quiet",
                            f"refs/heads/{branch}"], 30)
    return code == 0


def repo_problem() -> str:
    _code, head = sh(["git", "symbolic-ref", "--short", "HEAD"], 30)
    if head.strip() != "main":
        return f"jardal-openceo quedó en {head.strip()}: deja el checkout en main"
    _code, dirty = sh(["git", "status", "--porcelain",
                       "--untracked-files=no"], 60)
    if dirty.strip():
        return "jardal-openceo quedó con cambios sin commitear:\n" + dirty[:800]
    _code, head = sh(["git", "-C", str(KNOWLEDGE), "symbolic-ref", "--short",
                      "HEAD"], 30)
    if head.strip() != "main":
        return "jardal-knowledge quedó fuera de main: el respaldo nocturno fallaría"
    return ""


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def last_marker(text: str) -> tuple[str, str]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("*`\"'“”‘’ ->")
        for marker in MARKERS:
            if line.startswith(marker):
                return marker, line[len(marker):].strip(" :-—")
    return "", ""


def base_rules(c: dict) -> list[str]:
    lines = [
        f"Proyecto jardal-openceo ({REPO}). Épica {c.get('epic', EPIC)}; ticket {c['ticket']}"
        f" (paso {c['step']} de {direction(c.get('epic', EPIC))}).",
        "Antes de tocar nada lee la épica y el ticket con sus comentarios "
        "(openceo_get_ticket), CLAUDE.md §9 (muse lo ignora porque hay "
        "AGENTS.md: léelo tú), AGENTS.md y las reglas globales "
        "/home/develop/.claude/CLAUDE.md (estados de ticket, workfile "
        "temporal y aislamiento del vault).",
        "Antes de cualquier escritura en git, lee el skill bundled:git.",
        "PROHIBIDO: autoruns, Tier S, rondas de review propias, subagentes "
        "revisores, pruebas con tokens y envíos externos reales. Tus gates "
        "son los tests (mocks, ejecutores simulados, repos y vaults "
        "temporales).",
        "No toques otros tickets ni archivos ajenos; verifica y relee cada "
        "escritura. No crees tickets ni tareas para el Owner.",
        "Al terminar la sesión, con cualquier marcador, deja jardal-openceo "
        "en main y sin cambios sin commitear (lo tuyo queda en la rama).",
    ]
    if c["branch"]:
        lines.append(f"Rama de jardal-openceo: {c['branch']} (créala desde "
                     "main si no existe; si existe, continúa en ella).")
    else:
        lines.append("Este ticket solo toca jardal-knowledge: sin rama en "
                     "jardal-openceo.")
    if c["knowledge"]:
        lines.append(
            f"jardal-knowledge ({KNOWLEDGE}): solo las rutas que autoriza el "
            "ticket; del vault, únicamente vault/projects/jardal/"
            "jardal-openceo/ (nunca otros proyectos, shared/ ni inbox/). "
            "Carga el skill llm-vault-links y pasa vault_check.py "
            "--project con tus rutas. Siempre en main, sin ramas (el respaldo "
            "nocturno exige main). Commit solo de tus rutas, con el número "
            f"del ticket y bajo el lock del respaldo: cd {KNOWLEDGE} && "
            "flock -w 600 .git/jardal-knowledge-backup.lock sh -c 'git add "
            f"-- <rutas> && git commit -m \"[{c['ticket']}] ...\" -- "
            "<rutas>'. Nunca git add -A ni toques cambios ajenos sin "
            "commitear; sin push (lo envía el respaldo nocturno, D8).")
    return lines


def carry_over(t: dict) -> list[str]:
    lines = []
    if t["note"]:
        lines += ["TRASPASO DE LA SESIÓN ANTERIOR (compruébalo contra el "
                  "ticket y la rama):", t["note"]]
    if t["failure"]:
        lines += ["FALLO ANTERIOR A CORREGIR:", t["failure"][-3000:]]
    return lines


def work_prompt(c: dict, t: dict) -> str:
    lines = [
        f"Sesión worker {t['sessions']}, fase de trabajo (headless, sin "
        "reviewers).", *base_rules(c),
        "Flujo: ticket en Working con motivo → implementa → tests del ticket "
        "y gates de AGENTS.md en verde → commit en la rama → comentario de "
        "avance en el ticket. Sin merge ni push a main: eso va tras los "
        "revisores, en la sesión de cierre.",
        "Tu ÚLTIMA línea debe ser una de estas:",
        f"- {WORK_READY} <resumen>: todo implementado y en verde, listo para "
        "revisión.",
        f"- {WORK_CONTINUE} <traspaso>: no terminas en esta sesión; lo hecho "
        "queda commiteado en la rama y en un comentario de traspaso del "
        "ticket (hecho, falta, siguiente paso). Una sesión nueva sigue.",
        f"- {WORK_OWNER} <acción exacta>: solo si falta gasto, una credencial "
        "que solo tiene el Owner, una acción irreversible, una "
        "replanificación estratégica o un cambio de mandato; ticket en "
        "Pending Review con esa human_action. Cualquier otra duda la decides "
        "tú con un valor por defecto seguro, la registras como decisión del "
        "agente y sigues.",
        f"- {WORK_BLOCKED} <motivo>: bloqueo que otra sesión no puede "
        "resolver (credencial, acción del host); ticket en Working con el "
        "motivo y la condición de reanudación.",
        "La revisión y el cierre los ejecuta el orquestador tras tu "
        "TICKET_READY: no los esperes ni los lances tú.",
        "Si tu implementación está commiteada y en verde y solo faltan "
        "revisión o cierre (live, merge, push, restart), emite TICKET_READY; "
        "reserva TICKET_CONTINUE para implementación sin terminar.",
        *carry_over(t),
    ]
    return "\n".join(lines) + "\n"


def close_prompt(c: dict, t: dict) -> str:
    reports = [f"- {path}" for path in t["reviews"]] or ["- (ninguno)"]
    lines = [
        f"Sesión de cierre {t['sessions']} (headless).", *base_rules(c),
        "Informes de los revisores (solo lectura; no los borres):", *reports,
        "1. Evalúa cada hallazgo; tú decides cuáles son válidos. Corrige y "
        "verifica los válidos (en la rama o, en jardal-knowledge, con un "
        "commit de corrección) y deja en el ticket un comentario con los "
        "aceptados y los descartados con su motivo.",
        "2. Cierra el ciclo (CLAUDE.md §9): tests del ticket y gates de "
        "AGENTS.md en verde → si hay rama en jardal-openceo: merge --no-ff a "
        "main, push origin main y borrado de la rama local y remota → si "
        "cambió código del servicio: scripts/openceo-core-host.sh restart + "
        "status + smoke (health 200, instancia única) → checklist global del "
        "workfile docs/work del ticket → Completed con evidence, verificado "
        "por relectura.",
        "Tu ÚLTIMA línea debe ser una de estas:",
        f"- {WORK_DONE} <commit de merge o sin-codigo>",
        f"- {WORK_CONTINUE} <traspaso>, {WORK_OWNER} <acción exacta> o "
        f"{WORK_BLOCKED} <motivo>, con el mismo sentido que en la fase de "
        "trabajo.",
        *carry_over(t),
    ]
    return "\n".join(lines) + "\n"


def spawn(prompt: str, timeout: int, s: Settings, tag: str,
          workspace: Path = REPO) -> tuple[int, str]:
    prompt_file = STATE_DIR / f"{tag}.prompt.md"
    prompt_file.write_text(prompt)
    cmd = ["muse", "exec", "--yolo", "--workspace", str(workspace),
           "--reasoning-effort", s.effort,
           "--max-model-steps", str(s.max_steps),
           "--disable-request-user-input",
           "--prompt-file", str(prompt_file)]
    if s.model:
        cmd += ["--model", s.model]
    log(f"spawn {tag} en {workspace.name} (timeout {timeout}s)")
    code, out, err = run(cmd, timeout, cwd=workspace)
    STATE_DIR.joinpath(f"{tag}.log").write_text((out + "\n" + err)[-200000:])
    return code, out


def fenced(text: str, limit: int = 150_000) -> str:
    if len(text) > limit:
        text = text[:limit] + "\n[... recortado: lee los archivos directamente]"
    return f"~~~~\n{text.rstrip()}\n~~~~"


def knowledge_commits(c: dict, t: dict) -> list[str]:
    if not c["knowledge"]:
        return []
    base = t["base"].get("knowledge", "")
    span = [f"{base}..HEAD"] if base else ["-n", "50"]
    code, out, _err = run(["git", "-C", str(KNOWLEDGE), "log", "--format=%H",
                           f"--grep={c['ticket']}", *span], 60)
    return out.split() if code == 0 else []


def review_bundle(c: dict, t: dict) -> Path:
    info = ticket_info(c["ticket"])
    parts = [f"# Revisión de {c['ticket']} (paso {c['step']} de {direction(c.get('epic', EPIC))})",
             f"## Ticket: {info['subject']}", info["description"],
             "## Resumen final del builder", t["ready"] or "(sin resumen)"]
    base = t["base"].get("openceo", "")
    if base:
        span = (f"main...{c['branch']}" if c["branch"]
                and branch_exists(c["branch"]) else f"{base}..main")
        _code, stat, _err = run(["git", "diff", "--stat", span], 60)
        _code, diff, _err = run(["git", "diff", span], 120)
        parts += [f"## Diff de jardal-openceo ({span})", fenced(stat),
                  fenced(diff)]
    for sha in knowledge_commits(c, t):
        _code, names, _err = run(["git", "-C", str(KNOWLEDGE), "show",
                                  "--name-only", "--format=%h %s", sha], 60)
        _code, diff, _err = run(["git", "-C", str(KNOWLEDGE), "show",
                                 "--format=", sha, "--", *KNOWLEDGE_SCOPE], 120)
        parts += [f"## Commit {sha[:8]} de jardal-knowledge", fenced(names),
                  fenced(diff)]
    for path in sorted((REPO / "docs" / "work").glob(f"{c['ticket']}*")):
        parts += [f"## Workfile {path.relative_to(REPO)}",
                  fenced(path.read_text(errors="replace"))]
    folder = REVIEWS_DIR / c["ticket"]
    folder.mkdir(parents=True, exist_ok=True)
    bundle = folder / "context.md"
    bundle.write_text("\n\n".join(parts) + "\n")
    return bundle


def reviewer_prompt(c: dict, bundle: Path) -> str:
    return (
        f"Revisa en solo lectura el trabajo de otra sesión sobre el ticket "
        f"{c['ticket']} (jardal-openceo, paso {c['step']} de {direction(c.get('epic', EPIC))}). "
        f"Lee {bundle}: trae el ticket, el resumen del builder y el diff. "
        f"Puedes leer los archivos pertinentes de {REPO} y, si el diff toca "
        f"jardal-knowledge, de {KNOWLEDGE} (del vault solo ves la carpeta del "
        "proyecto). No ejecutes comandos, pruebas ni correcciones, no "
        "escribas archivos y no toques tickets ni servicios. Comprueba que "
        "el trabajo cumple el ALCANCE y la ACEPTACIÓN del ticket y la "
        "decisión que cita; busca errores de corrección, tests que no prueban "
        "lo que dicen, fugas entre proyectos, secretos y cambios fuera de "
        "alcance. Tu respuesta final es el informe completo en Markdown: "
        "hallazgos prudentes con archivo, línea y severidad, o que no "
        "encontraste problemas; distingue lo comprobado de lo que no pudiste "
        "verificar.")


def reviewer_env() -> dict[str, str]:
    env = os.environ.copy()
    policies = [{"action": "permission", "resource": "*", "effect": "deny"}]
    policies += [{"action": "permission", "resource": f"{action}:*",
                  "effect": "allow"} for action in ("read", "glob", "grep")]
    policies += [{"action": "permission", "resource": f"read:{pattern}",
                  "effect": "deny"}
                 for pattern in ("*.env", "*.env.*", "*/.ssh/*")]
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
        "snapshots": False, "formatter": False,
        "experimental": {"policies": policies}})
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    return env


def sandboxed(cmd: list[str]) -> list[str]:
    # The tmpfs hides every other project's notes; only this project's
    # vault folder is bound back, read-only.
    return ["bwrap", "--die-with-parent", "--bind", "/", "/",
            "--proc", "/proc", "--dev", "/dev",
            "--ro-bind", str(REPO), str(REPO),
            "--ro-bind", str(KNOWLEDGE), str(KNOWLEDGE),
            "--tmpfs", str(KNOWLEDGE / "vault"),
            "--ro-bind", str(VAULT_PROJECT), str(VAULT_PROJECT),
            "--chdir", str(REPO), "--", *cmd]


def parse_opencode(out: str) -> str:
    parts = {}
    for index, line in enumerate(out.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            raise RuntimeError("opencode notificó un error")
        if event.get("type") == "text":
            part = event.get("part", {})
            if isinstance(part.get("text"), str):
                parts[part.get("id", str(index))] = part["text"]
    text = "\n".join(parts.values()).strip()
    if not text:
        raise RuntimeError("opencode terminó sin respuesta")
    return text


def is_quota_error(out: str) -> bool:
    return any(sign in out for sign in (
        "provider.quota", "FreeUsageLimitError", '"status": 429',
        '"status":429'))


def parse_agy2(out: str) -> str:
    envelope = json.loads(out)
    if not isinstance(envelope, dict) or envelope.get("status") != "SUCCESS":
        raise RuntimeError("agy2 no confirmó SUCCESS")
    text = envelope.get("response")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("agy2 terminó sin respuesta")
    return text


def append_log(path: Path, title: str, out: str, err: str) -> None:
    with path.open("a") as handle:
        handle.write(f"\n===== {title}\n{out[-50000:]}\n{err[-20000:]}\n")


def review_opencode(prompt: str, s: Settings,
                    log_path: Path) -> tuple[str, str]:
    deadline = time.monotonic() + 3 * s.reviewer_timeout
    last = "sin intentos"
    for model in OPENCODE_MODELS:
        for _attempt in range(2):
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeError(f"opencode sin tiempo ({last})")
            code, out, err = run(sandboxed([
                "opencode", "run", "--standalone", "--auto", "--model", model,
                "--agent", "build", "--format", "json", prompt]),
                min(s.reviewer_timeout, remaining), env=reviewer_env())
            append_log(log_path, f"{model} exit={code}", out, err)
            try:
                if code != 0:
                    raise RuntimeError(f"exit={code}")
                return model, parse_opencode(out)
            except RuntimeError as exc:
                last = f"{model}: {exc}"
            if is_quota_error(out + err):
                log(f"revisor opencode: {model} sin cuota, paso al siguiente")
                break
    raise RuntimeError(f"opencode no revisó ({last})")


def review_agy2(prompt: str, s: Settings, log_path: Path) -> tuple[str, str]:
    last = "sin intentos"
    for attempt in range(1, 3):
        code, out, err = run(sandboxed([
            "agy2", "--print", prompt, "--output-format", "json",
            "--sandbox"]), s.reviewer_timeout)
        append_log(log_path, f"agy2 intento {attempt} exit={code}", out, err)
        try:
            if code != 0:
                raise RuntimeError(f"exit={code}")
            return "agy2", parse_agy2(out)
        except (RuntimeError, ValueError) as exc:
            last = str(exc)
    raise RuntimeError(f"agy2 no revisó ({last})")


def run_reviews(c: dict, t: dict, s: Settings) -> list[str]:
    bundle = review_bundle(c, t)
    prompt = reviewer_prompt(c, bundle)
    reviewers = {name: fn for name, fn in (("opencode", review_opencode),
                                           ("agy2", review_agy2))
                 if shutil.which(name)}
    log(f"{c['ticket']}: revisores en paralelo: {', '.join(reviewers)}")
    reports = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {name: pool.submit(
            fn, prompt, s, STATE_DIR / f"review-{c['ticket'][-5:]}-{name}.log")
            for name, fn in reviewers.items()}
        for name, job in jobs.items():
            try:
                model, text = job.result()
            except (RuntimeError, ValueError, OSError) as exc:
                log(f"{c['ticket']}: revisor {name} falló: {exc}")
                continue
            report = bundle.parent / f"{name}.md"
            report.write_text(f"<!-- revisor {name}: {model} -->\n"
                              f"{text.rstrip()}\n")
            reports.append(str(report))
            log(f"{c['ticket']}: revisión de {name} ({model}) lista")
    return reports


def verify_knowledge(c: dict, t: dict) -> tuple[bool, str]:
    if not c["knowledge"]:
        return True, ""
    paths: set[str] = set()
    for sha in knowledge_commits(c, t):
        _code, names, _err = run(["git", "-C", str(KNOWLEDGE), "show",
                                  "--name-only", "--format=", sha], 60)
        paths.update(names.split())
    outside = sorted(p for p in paths if not p.startswith(KNOWLEDGE_SCOPE))
    if outside:
        return False, (f"commits de {c['ticket']} fuera de alcance en "
                       f"jardal-knowledge: {outside[:5]}")
    if paths:
        _code, pending, _err = run(["git", "-C", str(KNOWLEDGE), "status",
                                    "--porcelain", "--", *sorted(paths)], 60)
        if pending.strip():
            return False, ("rutas del ticket sin commitear en "
                           "jardal-knowledge:\n" + pending[-1500:])
    notes = sorted(p.removeprefix("vault/") for p in paths
                   if p.startswith("vault/projects/jardal/jardal-openceo/")
                   and p.endswith(".md") and (KNOWLEDGE / p).exists())
    if notes:
        code, out, err = run([str(PYTHON), str(_vault_check()), "--project",
                              "projects/jardal/jardal-openceo", "check",
                              *notes], 120, cwd=KNOWLEDGE)
        if code != 0:
            return False, "vault_check falló:\n" + (out + err)[-1500:]
    if c["workspace"] == KNOWLEDGE or any(
            p.startswith((".agents/", "scripts/")) for p in paths):
        code, out, err = run([str(PYTHON), "-m", "pytest", "-q",
                              *_knowledge_tests()], 300, cwd=KNOWLEDGE)
        if code != 0:
            return False, ("tests de jardal-knowledge rojos:\n"
                           + (out + err)[-1500:])
    return True, ""


def verify_code(c: dict, t: dict) -> tuple[bool, str]:
    problem = repo_problem()
    if problem:
        return False, problem
    sh(["git", "fetch", "origin", "main"], 120)
    code, out = sh(["git", "rev-list", "--left-right", "--count",
                    "main...origin/main"], 60)
    if code != 0 or out.strip() != "0\t0":
        return False, f"main no pusheado ({out.strip()})"
    targets = GATE_TESTS + [p for p in c["tests"] if (REPO / p).exists()]
    base = t["base"].get("openceo", "")
    if base:
        _code, changed, _err = run(["git", "diff", "--name-only",
                                    f"{base}..main", "--", "tests/"], 60)
        targets += [p for p in changed.split()
                    if Path(p).name.startswith("test_") and p.endswith(".py")
                    and (REPO / p).exists()]
    code, out = sh([str(PYTHON), "-m", "pytest", *dict.fromkeys(targets),
                    "-q"], 900)
    if code != 0:
        return False, "tests rojos:\n" + out[-2000:]
    ok, why = verify_knowledge(c, t)
    if not ok:
        return False, why
    status = health()
    if status != "200":
        return False, f"smoke health:{status}"
    code, out = sh(["bash", "-c",
                    "(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null)"
                    " | grep -c 18765"], 30)
    if out.strip() != "1":
        return False, f"instancias en 18765: {out.strip()}"
    return True, "git+tests+knowledge+smoke OK"


def advance(c: dict, s: Settings, state: dict) -> str:
    ticket = c["ticket"]
    t = ticket_state(state, ticket)
    if t["phase"] == "done":
        return "done"
    info = ticket_info(ticket)
    if info["status"] == "Cancelled":
        t["phase"] = "done"
        save_state(state)
        log(f"{ticket}: Cancelled en el ERP; no frena a sus dependientes")
        return "done"
    if info["status"] == "Pending Review" and not info["owner_replied"]:
        t["parked"] = "owner"
        save_state(state)
        log(f"{ticket}: espera al Owner en Pending Review. {t['note']}")
        return "owner"
    if t["parked"] == "owner":
        t["note"] = (f"El Owner respondió (ticket en {info['status']}): lee "
                     "sus comentarios recientes y sigue su decisión.")
    elif t["parked"] == "blocked":
        t["note"] = (f"La ejecución anterior terminó bloqueada: {t['note']}. "
                     "Comprueba si ya se cumple la condición de reanudación; "
                     f"si no, termina de nuevo con {WORK_BLOCKED}.")
    t["parked"] = ""
    if info["status"] == "Completed" and (t["phase"] == "close"
                                          or t["sessions"] == 0):
        t["phase"] = "verify"
    if not t["base"]:
        t["base"] = {"openceo": git_head(REPO, "main"),
                     "knowledge": git_head(KNOWLEDGE)}
    save_state(state)
    sessions = failures = 0
    while True:
        phase = t["phase"]
        if phase == "review":
            if not t["reviews"]:
                t["reviews"] = run_reviews(c, t, s)
                save_state(state)
            if not t["reviews"]:
                log(f"{ticket}: ningún revisor terminó; reanuda más tarde")
                return "stop"
            t["phase"] = "close"
            save_state(state)
            continue
        if phase == "verify":
            ok, why = verify_code(c, t)
            if ok:
                status = ticket_info(ticket)["status"]
                if status == "Completed":
                    t.update(phase="done", failure="", note="")
                    save_state(state)
                    log(f"{ticket}: DONE ({why})")
                    return "done"
                why = (f"el ticket está en {status}: transiciónalo a "
                       "Completed con evidence y relee")
            failures += 1
            t.update(phase="close", failure=f"verificación propia falló: {why}")
            save_state(state)
            log(f"{ticket}: {why[:300]}")
            if failures > s.max_fix:
                return "stop"
            continue
        if sessions >= s.max_sessions:
            log(f"{ticket}: {sessions} sesiones sin terminar en esta ejecución")
            return "stop"
        sessions += 1
        t["sessions"] += 1
        prompt = work_prompt(c, t) if phase == "work" else close_prompt(c, t)
        code, out = spawn(prompt, s.worker_timeout, s,
                          f"{phase}-{ticket[-5:]}-s{t['sessions']}",
                          c["workspace"])
        marker, detail = last_marker(out)
        problem = repo_problem()
        if problem:
            marker = ""
        if marker in (WORK_OWNER, WORK_BLOCKED):
            parked = "owner" if marker == WORK_OWNER else "blocked"
            t.update(parked=parked, note=detail, failure="")
            save_state(state)
            log(f"{ticket}: {parked}: {detail}")
            return parked
        if marker == WORK_CONTINUE:
            t.update(note=detail, failure="")
            failures = 0
            log(f"{ticket}: sigue en otra sesión: {detail[:200]}")
        elif phase == "work" and marker in (WORK_READY, WORK_DONE):
            t.update(phase="review", ready=tail(out), note="", failure="")
            failures = 0
        elif phase == "close" and marker == WORK_DONE:
            t.update(phase="verify", note="", failure="")
            failures = 0
        else:
            failures += 1
            t["failure"] = problem or (f"sin marcador válido para la fase "
                                       f"{phase} (exit={code}):\n{out[-2000:]}")
            log(f"{ticket}: {t['failure'][:200]}")
            if failures > s.max_fix:
                save_state(state)
                return "stop"
        save_state(state)


def discover_extra(plan: list[dict], children: dict[str, str]) -> list[dict]:
    known = {c["ticket"] for c in plan}
    extra = sorted(name for name in children if name not in known)
    if not extra:
        return []
    tickets = read_erp(extra)["tickets"]
    log(f"hijos fuera del plan fijo: {', '.join(extra)}")
    epic = plan[0].get("epic", EPIC) if plan else EPIC
    out = []
    for name in extra:
        child = _child("+", name, "extra", knowledge=True,
                       deps=tuple(tickets[name]["depends_on"]))
        child["epic"] = epic
        out.append(child)
    return out


def blocking_deps(c: dict, state: dict, children: dict[str, str]) -> list[str]:
    return [d for d in c["deps"]
            if state["tickets"].get(d, {}).get("phase") != "done"
            and children.get(d) not in CLOSED]


def close_epic(s: Settings, epic: str) -> bool:
    data = read_erp([epic], epic)
    if data["tickets"][epic]["status"] == "Completed":
        return True
    pending = sorted(n for n, st in data["children"].items() if st not in CLOSED)
    if pending:
        log(f"épica: hijos sin cerrar en el ERP: {', '.join(pending)}")
        return False
    prompt = (
        f"Sesión de cierre de la épica {epic} (headless, sin reviewers). "
        f"Checkout {REPO}.\n"
        "Lee CLAUDE.md §9 y la épica con openceo_get_ticket; relee cada hijo "
        "y confirma que está Completed o Cancelled.\n"
        "Escribe en la épica un comentario de evidencia agregada: una línea "
        "por hijo (paso, resultado y commit), más los tickets del Owner que "
        "hayan quedado fuera de la épica.\n"
        "Transiciónala a Completed con evidence, verificada por relectura.\n"
        f"Última línea: {EPIC_DONE} o {WORK_BLOCKED} <motivo>.\n")
    _code, out = spawn(prompt, s.worker_timeout, s, "close-epic")
    marker, detail = last_marker(out)
    status = ticket_info(epic)["status"]
    if status != "Completed":
        log(f"épica: {marker or 'sin marcador'} {detail} (status={status})")
    return status == "Completed"


def preflight(epic: str = EPIC) -> bool:
    missing = [tool for tool in ("muse", "bwrap") if not shutil.which(tool)]
    if missing:
        log(f"ABORT: faltan {', '.join(missing)}")
        return False
    if not any(shutil.which(tool) for tool in ("opencode", "agy2")):
        log("ABORT: no hay revisores (ni opencode ni agy2)")
        return False
    problem = repo_problem()
    if problem:
        log(f"ABORT: {problem}")
        return False
    sh(["git", "fetch", "origin", "main"], 120)
    code, out = sh(["git", "rev-list", "--left-right", "--count",
                    "main...origin/main"], 60)
    if code != 0 or out.strip() != "0\t0":
        log(f"ABORT: main diverge de origin/main ({out.strip()})")
        return False
    status = health()
    if status != "200":
        log(f"ABORT: servicio no sano (health:{status})")
        return False
    try:
        read_erp([epic])
    except (RuntimeError, ValueError) as exc:
        log(f"ABORT: Jardal ERP no responde: {exc}")
        return False
    rel = release_info()
    log(f"preflight OK: limpio, main==origin, knowledge en main, servicio 200, ERP; release {rel['sha']} dirty={rel['dirty']}")
    return True


def print_plan(plan: list[dict], state: dict) -> None:
    for c in plan:
        t = state["tickets"].get(c["ticket"], {})
        where = "knowledge" if c["workspace"] == KNOWLEDGE else (
            "openceo+knowledge" if c["knowledge"] else "openceo")
        deps = ",".join(d[-5:] for d in c["deps"]) or "-"
        phase = t.get("phase", "work") + (
            f" ({t['parked']})" if t.get("parked") else "")
        log(f"{c['step']:>3} {c['ticket']} [{where}] deps={deps} "
            f"fase={phase} rama={c['branch'] or '-'}")


def reopen_if_needed(c: dict, state: dict, children: dict[str, str]) -> None:
    t = state["tickets"].get(c["ticket"])
    status = children.get(c["ticket"])
    if not t or t["phase"] != "done" or status in (None, *CLOSED):
        return
    log(f"{c['ticket']}: reabierto en el ERP ({status}); vuelve a trabajo")
    t.update(phase="work", reviews=[], ready="", base={}, failure="",
             note="El ticket se reabrió en el ERP: lee sus comentarios "
                  "recientes para saber qué falta.")


def drive(s: Settings) -> int:
    epic = s.epic or EPIC
    state = load_state(s.reset, archive=not s.dry_run, epic=epic)
    try:
        plan, source = load_epic_children(epic)
    except (RuntimeError, ValueError) as exc:
        log(f"ABORT: no se pudo cargar el plan de {epic}: {exc}")
        return 2
    children: dict[str, str] = {}
    if not s.dry_run:
        save_state(state)
        if not preflight(epic):
            return 3
        children = read_erp([], epic)["children"]
        plan += discover_extra(plan, children)
    if s.only:
        plan = [c for c in plan if c["ticket"] == s.only]
        if not plan:
            print(f"Ticket {s.only} no es hijo de {epic}.")
            return 2
    rel = release_info()
    log(f"épica {epic}: {len(plan)} tickets ({source})"
        + (" (dry-run)" if s.dry_run else "")
        + f"; release {rel['sha']} dirty={rel['dirty']}")
    if s.dry_run:
        print_plan(plan, state)
        return 0
    outcome: dict[str, str] = {}
    for c in plan:
        reopen_if_needed(c, state, children)
        waiting = blocking_deps(c, state, children)
        if waiting:
            outcome[c["ticket"]] = "espera " + ",".join(d[-5:] for d in waiting)
            continue
        try:
            result = advance(c, s, state)
        except (RuntimeError, ValueError) as exc:
            log(f"{c['ticket']}: {exc}")
            result = "stop"
        outcome[c["ticket"]] = result
        if result == "stop":
            log(f"STOP en {c['ticket']}: estado guardado; repite "
                "./automatico.py para reanudar.")
            break
    log("resumen: " + " · ".join(f"{t[-5:]} {o}" for t, o in outcome.items()))
    if s.only:
        return 0 if outcome.get(s.only) == "done" else 1
    if any(outcome.get(c["ticket"]) != "done" for c in plan):
        return 1
    if not close_epic(s, epic):
        log("STOP: cierre de épica pendiente.")
        return 1
    log(f"{EPIC_DONE}: épica {epic} cerrada y verificada.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset", action="store_true",
                        help="olvida el progreso guardado")
    parser.add_argument("--resume", action="store_true",
                        help="compatibilidad: reanudar ya es el defecto")
    parser.add_argument("--epic", default="",
                        help=f"épica a ejecutar (defecto {EPIC}); el plan sale de "
                             ".openceo-automatico/epics/<epica>.json o de los hijos del ERP")
    parser.add_argument("--only", default="")
    parser.add_argument("--worker-timeout", type=int, default=5400)
    parser.add_argument("--reviewer-timeout", type=int, default=1800)
    parser.add_argument("--max-sessions", type=int, default=8,
                        help="sesiones por ticket en cada ejecución")
    parser.add_argument("--max-fix", type=int, default=2)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--max-steps", type=int, default=200)
    args = parser.parse_args()
    s = Settings(dry_run=args.dry_run, reset=args.reset, epic=args.epic,
                 only=args.only,
                 worker_timeout=args.worker_timeout,
                 reviewer_timeout=args.reviewer_timeout,
                 max_sessions=args.max_sessions, max_fix=args.max_fix,
                 model=args.model, effort=args.effort,
                 max_steps=args.max_steps)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Otra instancia está corriendo (lock).")
        return 2
    try:
        return drive(s)
    except KeyboardInterrupt:
        for proc in list(_LIVE):
            _kill(proc)
        log("interrumpido: estado guardado; repite ./automatico.py para "
            "reanudar")
        return 130


if __name__ == "__main__":
    sys.exit(main())
