"""Pilot 1 rehearsal without tokens (TASK-2026-00234).

Dry run of the TASK-2026-00219 recorrido with the planner and the
operations agent simulated and everything else real: the planner path
(``plan_pedido`` over the live routing policy), the generic runner
(``run_generic_agent`` with the operations profile, a real badge and a
real ticket folder), the crear-proyecto skill script (real subprocess
against tmp roots plus the recipe git line against a bare tmp remote),
recording Jardal ERP/DNS doubles behind the catalog box, the structured
verdict parse, the cost fold and the crystallization save step (real
gate against a tmp origin fixture).

Order note: the simulated agent performs the skill work inside its
``run()`` — the same order as the pilot — and cites the outputs in its
hecho verdict. No network, no LLM, no Jardal ERP, no Cloudflare, no push
outside tmp. The only credential touching the ticket folder is the
rehearsal badge itself, by design (short-lived, memory-only, revoked
at the end); service secrets never leave the fake base env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog import load_catalog, load_role_profile
from objectives import ticket_cost as cost_mod
from objectives.agent_result import AgentOutcome
from orchestration.agent_sandbox import sandbox_env, ticket_dir
from orchestration.crystallization import (
    collect_drafts,
    has_drafts,
    run_save_step,
    SaveStepConfig,
)
from orchestration.generic_agent import (
    GenericAgentWiring,
    run_generic_agent,
)
from orchestration.role_muse import TicketBadgeProvider
from tool_locks import check_dns_delete_lock

logger = logging.getLogger(__name__)

__all__ = [
    "REHEARSAL_MIRROR",
    "RehearsalConfig",
    "RehearsalError",
    "RehearsalEvidence",
    "repo_root",
    "run_pilot_rehearsal",
]

#: Jardal ERP mirror the rehearsal badge binds to. Memory-only, never
#: persisted to Jardal ERP; far above the live numbering.
REHEARSAL_MIRROR = "TASK-2026-09999"

#: Service-secret sentinels: seeded in the fake base env, must appear
#: neither in the child env nor in any run artifact.
SECRET_SENTINELS = {
    "CLOUDFLARE_API_TOKEN": "rehearsal-sentinel-cf-token-00234",
    "JARDAL_ERP_SERVICE_TOKEN": "rehearsal-sentinel-erp-token-00234",
    "DEEPSEEK_API_KEY": "rehearsal-sentinel-deepseek-key-00234",
}

_OUT_OF_BOX_TOOL = "openceo_send_email"

_DRAFT_NAME = "rehearsal-notes"

_GIT_TIMEOUT_S = 60
_SKILL_TIMEOUT_S = 120


class RehearsalError(RuntimeError):
    """A rehearsal step failed: the pilot is not ready as rehearsed."""


def repo_root() -> Path:
    """Live checkout root (skills, catalog and profiles read from here)."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RehearsalConfig:
    """What one rehearsal run needs. ``run_dir`` is tmp scratch."""

    run_dir: str | Path
    project_key: str = "rehearsal-pilot1"
    display_name: str = "Rehearsal Pilot 1"
    hostname: str = "rehearsal-pilot1.invalid"
    task_id: str = ""
    project: str = "jardal-openceo"
    mirror_ticket: str = REHEARSAL_MIRROR
    validate: Callable[[Path], tuple[bool, str]] | None = None
    run_skill_tests: Callable[..., tuple[bool, str]] | None = None


@dataclass
class RehearsalEvidence:
    """Structured proof of one rehearsal run (JSON-able)."""

    decision: dict[str, Any] = field(default_factory=dict)
    run_spec: dict[str, Any] = field(default_factory=dict)
    badge: dict[str, Any] = field(default_factory=dict)
    boundary_calls: list[dict[str, Any]] = field(default_factory=list)
    denied_calls: list[dict[str, Any]] = field(default_factory=list)
    skill_outputs: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    crystallization: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    dns_delete_requires_owner: bool = False


def _check(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise RehearsalError(
            f"{what} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:500]}")
    return result.stdout or ""


def _git(args: list[str], cwd: Path) -> str:
    return _check(subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_S), f"git {' '.join(args)}")


class _BoxGateway:
    """Recording boundary-tool doubles behind the catalog box."""

    def __init__(self, box: frozenset[str] | set[str]) -> None:
        self._box = frozenset(box)
        self.allowed: list[dict[str, Any]] = []
        self.denied: list[dict[str, Any]] = []

    def call(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        if tool not in self._box:
            self.denied.append({"tool": tool, "reason": "outside agent box"})
            return {"ok": False, "denied": "outside agent box"}
        if tool == "openceo_ensure_business_project":
            record = {"tool": tool, "project_key": kwargs.get("project_key"),
                      "display_name": kwargs.get("display_name"),
                      "existed": False}
        elif tool == "manage_dns_record":
            record = {"tool": tool, "hostname": kwargs.get("hostname"),
                      "action": kwargs.get("action", "create")}
        else:  # pragma: no cover - rehearsal only routes two tools
            record = {"tool": tool, **kwargs}
        self.allowed.append(record)
        return {"ok": True, **record}


class _SimulatedPlanner:
    """Fake headless planner: writes one canned valid PLANNER_RESULT."""

    def __init__(self, task_id: str, payload: Mapping[str, Any]) -> None:
        self._task_id = task_id
        self._payload = dict(payload)
        self.runs = 0

    async def run(self, role: str, prompt: str, repo: Any,
                  timeout_s: int) -> Any:
        from objectives.engine import pedido_scratch

        del role, prompt, timeout_s
        self.runs += 1
        target = pedido_scratch(Path(repo), self._task_id) \
            / "PLANNER_RESULT.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self._payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


class _SimulatedOperations:
    """The rehearsal agent: mints its badge, does the skill work, cites it."""

    def __init__(self, hands: "_SkillHands", provider: Any = None) -> None:
        self._hands = hands
        self.provider = provider
        self.badge = ""
        self.calls: list[dict[str, Any]] = []

    async def run(self, role: str, prompt: str, workdir: Any,
                  timeout_s: int) -> Any:
        if self.provider is not None:
            self.badge = await self.provider.mint(role)
        self.calls.append({"role": role, "timeout_s": timeout_s,
                           "prompt_chars": len(prompt),
                           "badged": bool(self.badge)})
        work = await self._hands.perform(Path(workdir))
        verdict = {
            "resultado": "hecho",
            "motivo": ("project skeleton ready in tmp roots; "
                       f"repo={work['repo']}, dns={work['dns']}"),
            "necesidad": "",
            "entregables": ["tmp repo", "tmp project.yaml", "tmp vault hub"],
            "evidencia": json.dumps(work, sort_keys=True)[:1500],
        }
        tail = ("skill crear-proyecto executed against doubles.\n"
                "```agent-result\n" + json.dumps(verdict) + "\n```")
        return SimpleNamespace(returncode=0, output_tail=tail, usage={},
                               elapsed_s=2.5, model="rehearsal-simulated")

    async def cleanup(self, role: str) -> None:
        del role


class _SkillHands:
    """Real skill mechanics against tmp doubles (no network)."""

    def __init__(self, config: RehearsalConfig, roots: Mapping[str, Path],
                 gateway: _BoxGateway, python: str) -> None:
        self._config = config
        self._roots = dict(roots)
        self._gateway = gateway
        self._python = python
        self.outputs: dict[str, Any] = {}

    def _cli(self, *args: str) -> dict[str, Any]:
        script = (repo_root() / "skills" / "operations" / "crear-proyecto"
                  / "scripts" / "crear_proyecto.py")
        result = subprocess.run(
            [self._python, str(script), *args,
             "--repos-root", str(self._roots["repos"]),
             "--projects-root", str(self._roots["projects"]),
             "--vault-root", str(self._roots["vault"])],
            capture_output=True, text=True, timeout=_SKILL_TIMEOUT_S)
        if result.returncode != 0:
            raise RehearsalError(
                f"skill CLI {' '.join(args)} failed: "
                f"{(result.stderr or result.stdout).strip()[:500]}")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RehearsalError(
                f"skill CLI {' '.join(args)} answered no JSON: {exc}")
        if not payload.get("ok"):
            raise RehearsalError(
                f"skill CLI {' '.join(args)} answered not-ok: "
                f"{str(payload)[:300]}")
        return payload

    def _identity(self) -> list[str]:
        config = self._config
        return ["--project-key", config.project_key, "--repo",
                config.project_key, "--display-name", config.display_name,
                "--org", "jardal", "--description", "Pilot 1 rehearsal",
                "--remote-url", str(self._roots["remote"])]

    async def perform(self, workdir: Path) -> dict[str, Any]:
        config = self._config
        erp = self._gateway.call(
            "openceo_ensure_business_project", project_key=config.project_key,
            display_name=config.display_name)
        if not erp.get("ok"):
            raise RehearsalError("Jardal ERP double refused the call")
        repo = self._roots["repos"] / config.project_key
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").is_dir():
            _git(["init", "-b", "main", "-q"], repo)
        current = _git(["remote"], repo).split()
        if "origin" in current:
            _git(["remote", "set-url", "origin",
                  str(self._roots["remote"])], repo)
        else:
            _git(["remote", "add", "origin",
                  str(self._roots["remote"])], repo)
        self.outputs["roots"] = self._cli("roots")
        self.outputs["templates"] = self._cli("templates", *self._identity())
        self.outputs["project-config"] = self._cli(
            "project-config", *self._identity())
        self.outputs["vault-entry"] = self._cli(
            "vault-entry", *self._identity())
        dns = self._gateway.call("manage_dns_record",
                                 hostname=config.hostname, action="create")
        if not dns.get("ok"):
            raise RehearsalError("DNS double refused the call")
        self._leave_draft(workdir)
        return {"repo": str(repo), "dns": config.hostname,
                "erp_existed": erp.get("existed")}

    def _leave_draft(self, workdir: Path) -> None:
        target = workdir / "skill_drafts" / _DRAFT_NAME
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            f"---\nname: {_DRAFT_NAME}\ndescription: "
            "\"Rehearsal draft: tmp-root project skeleton notes.\"\n---\n\n"
            "# Rehearsal notes\n\nTmp roots plus recording doubles prove "
            "the crear-proyecto mechanics without external effects.\n",
            encoding="utf-8")


def _planner_payload() -> dict[str, Any]:
    return {
        "objective_status": "active",
        "state_summary": "rehearsal pedido decided for operations",
        "tickets": [{
            "local_ref": "rehearsal-1",
            "title": "Crear rehearsal-pilot1",
            "target_agent": "operations",
            "work_type": "project_setup",
            "goal": "create the rehearsal project skeleton",
            "why_now": "pilot rehearsal",
            "acceptance_criteria": ["skeleton validates"],
            "expected_artifacts": ["repo", "project.yaml", "vault hub"],
            "skill": "crear-proyecto",
        }],
        "roadmap_remaining": [],
    }


def _pedido(config: RehearsalConfig, task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "ticket_number": 0,
        "title": f"Crear proyecto {config.project_key}",
        "description": ("Pilot 1 rehearsal: create the project skeleton "
                        "with the operations skill against doubles."),
        "project": config.project,
        "source": "api",
        "agent": "ceo",
        "status": "queued",
        "params": {
            "project_key": config.project_key,
            "display_name": config.display_name,
            "hostname": config.hostname,
            "_mirrors": {"erpnext": {"name": config.mirror_ticket}},
        },
    }


def _seed_origin(origin: Path, checkout: Path) -> None:
    _git(["init", "--bare", "-q", str(origin)], origin.parent)
    _git(["clone", "-q", str(origin), str(checkout)], origin.parent)
    _git(["checkout", "-q", "-b", "main"], checkout)
    root = repo_root()
    seeds = [
        (root / "catalog" / "agents" / "operations.yaml",
         checkout / "catalog" / "agents" / "operations.yaml"),
        (root / "catalog" / "profiles" / "operations.yaml",
         checkout / "catalog" / "profiles" / "operations.yaml"),
        (root / "skills" / "operations" / "crear-proyecto" / "SKILL.md",
         checkout / "skills" / "operations" / "crear-proyecto"
         / "SKILL.md"),
    ]
    for source, target in seeds:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    _git(["add", "."], checkout)
    _git(["-c", "user.name=rehearsal", "-c",
          "user.email=rehearsal@jardal.app", "commit", "-q", "-m",
          "rehearsal seed"], checkout)
    _git(["push", "-q", "-u", "origin", "main"], checkout)


def _scan_for(secrets: Mapping[str, str], *bases: Path) -> list[str]:
    leaks: list[str] = []
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeError):
                continue
            for name, sentinel in secrets.items():
                if sentinel and sentinel in text:
                    leaks.append(f"{name} in {path}")
    return leaks


async def run_pilot_rehearsal(config: RehearsalConfig) -> RehearsalEvidence:
    """Run the full Pilot 1 recorrido with simulated planner and agent."""
    from objectives.engine import ObjectiveEnv, plan_pedido
    from mcp_identity import (
        MemoryTokenStore,
        TokenIssuer,
        box_for_agent,
    )

    if shutil.which("git") is None:
        raise RehearsalError("the rehearsal needs git for tmp remotes")
    evidence = RehearsalEvidence()
    run_dir = Path(config.run_dir)
    work_base = run_dir / "work"
    roots = {name: run_dir / name
             for name in ("repos", "projects", "vault")}
    for path in (*roots.values(), work_base):
        path.mkdir(parents=True, exist_ok=True)
    (roots["vault"] / "README.md").write_text(
        "# Vault (rehearsal)\n", encoding="utf-8")
    remote = run_dir / "remote.git"
    _git(["init", "--bare", "-q", str(remote)], run_dir)
    roots["remote"] = remote
    task_id = config.task_id or f"rehearsal-{uuid.uuid4().hex[:12]}"
    ticket = _pedido(config, task_id)

    patches: dict[str, dict[str, Any]] = {}

    async def patch_task(task_id: str, updates: Mapping[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    async def no_create(payload: Mapping[str, Any]) -> Any:
        raise RehearsalError(f"unexpected ticket creation: {payload!r}"[:200])

    env = ObjectiveEnv(
        objectives=None, tasks=None, create_ticket=no_create,  # type: ignore[arg-type]
        patch_task=patch_task,  # type: ignore[arg-type]
        executor=_SimulatedPlanner(task_id, _planner_payload()),  # type: ignore[arg-type]
        quota=None, slots=None, reviews=None, inbox=None,  # type: ignore[arg-type]
        notify=None, repo=run_dir / "planner-repo",
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(repo_root() / "openceo-core"
                               / "objectives" / "routing_policy.md"),
            "agents": {"operations": {"enabled": True}},
        })
    outcome = await plan_pedido(env, ticket)
    if not outcome.get("ok") or outcome.get("action") != "decided":
        raise RehearsalError(f"planner did not decide: {outcome!r}"[:300])
    ticket.update(patches.get(task_id, {}))
    params = ticket.get("params") or {}
    evidence.decision = {
        "action": outcome["action"],
        "target_agent": (outcome.get("decision") or {}).get("target_agent"),
        "skill": (outcome.get("decision") or {}).get("skill"),
        "params_skill": params.get("skill"),
        "agent_hint": ticket.get("agent_hint"),
    }
    if evidence.decision["target_agent"] != "operations" \
            or evidence.decision["skill"] != "crear-proyecto" \
            or params.get("skill") != "crear-proyecto":
        raise RehearsalError(f"wrong decision: {evidence.decision!r}")

    catalog = load_catalog()
    card = catalog.agents.get("operations")
    if card is None:  # pragma: no cover - catalog gate owns this
        raise RehearsalError("operations card missing from the catalog")
    box = box_for_agent(card)
    gateway = _BoxGateway(box)
    issuer = TokenIssuer(MemoryTokenStore(), catalog.agents)
    provider_holder: dict[str, Any] = {}

    def badge_factory(agent: str, ticket_name: str,
                      project: str) -> TicketBadgeProvider:
        return TicketBadgeProvider(issuer, SimpleNamespace(), agent,
                                   ticket_name, project)

    hands = _SkillHands(config, roots, gateway, sys.executable)
    agent_executor = _SimulatedOperations(hands)

    def executor_factory(role: str, spec: Mapping[str, Any],
                         provider: Any) -> _SimulatedOperations:
        provider_holder["spec"] = dict(spec)
        provider_holder["provider_arg"] = provider
        agent_executor.provider = provider
        return agent_executor

    settings = SimpleNamespace(
        PROJECT_REPOS_ROOT=str(roots["repos"]),
        VAULT_ROOT=str(roots["vault"]),
        PROJECTS_DIR=str(roots["projects"]))
    wiring = GenericAgentWiring(
        settings=settings, repo=None, executor_factory=executor_factory,
        badge_provider_factory=badge_factory,
        repo_root=repo_root(), work_base_dir=work_base)
    run_out = await run_generic_agent("operations", ticket, wiring)
    if run_out.get("status") != "completed":
        raise RehearsalError(
            f"agent run did not complete: {run_out.get('error')!r}"[:300])
    badge = agent_executor.badge
    grant = await issuer.authenticate(badge) if badge else None
    evidence.run_spec = {
        "command": (provider_holder.get("spec") or {}).get("command"),
        "role_profile": (provider_holder.get("spec") or {}).get(
            "role_profile"),
        "headless_args": (provider_holder.get("spec") or {}).get(
            "headless_args"),
        "workdir": str(ticket_dir(task_id, base_dir=work_base)),
        "prompt_chars": (agent_executor.calls[0]["prompt_chars"]
                         if agent_executor.calls else 0),
    }
    workdir = ticket_dir(task_id, base_dir=work_base)
    staged = sorted(entry.name for entry in
                    (workdir / ".codex" / "skills").iterdir()
                    if entry.is_symlink()) \
        if (workdir / ".codex" / "skills").is_dir() else []
    evidence.run_spec["staged_skills"] = staged
    evidence.run_spec["schema_exists"] = (
        repo_root() / load_role_profile("operations").output_schema
    ).is_file()
    evidence.badge = {
        "agent": grant.agent if grant else "",
        "ticket": grant.ticket if grant else "",
        "box": sorted(grant.tools) if grant else [],
        "box_matches_card": (frozenset(grant.tools) == box)
        if grant else False,
    }
    if grant is not None:
        await issuer.revoke(badge, "rehearsal finished")
        try:
            await issuer.authenticate(badge)
            evidence.badge["revoked"] = False
        except Exception:
            evidence.badge["revoked"] = True

    denied_probe = gateway.call(_OUT_OF_BOX_TOOL, to="nobody@x.invalid")
    if denied_probe.get("ok"):
        raise RehearsalError(f"{_OUT_OF_BOX_TOOL} passed the box")
    evidence.boundary_calls = gateway.allowed
    evidence.denied_calls = gateway.denied
    evidence.skill_outputs = {
        name: ("ok" if isinstance(payload, Mapping) and payload.get("ok")
               else payload)
        for name, payload in hands.outputs.items()}
    evidence.git = {
        "repo": str(roots["repos"] / config.project_key),
        "has_git": ((roots["repos"] / config.project_key / ".git").is_dir()),
        "remote": _git(["remote", "get-url", "origin"],
                       roots["repos"] / config.project_key).strip(),
    }

    stored = (run_out.get("params") or {}).get("agent_result") or {}
    evidence.verdict = {
        "resultado": stored.get("resultado"),
        "motivo": stored.get("motivo"),
        "comment": run_out.get("result_comment"),
    }
    if stored.get("resultado") != AgentOutcome.HECHO.value:
        raise RehearsalError(f"verdict is not hecho: {stored!r}"[:200])
    evidence.cost = cost_mod.record_runs(
        None, run_out.get(cost_mod.COST_RUNS_KEY) or [])

    from company_mandate import load_mandate_file

    mandate = load_mandate_file(
        repo_root() / "projects" / "company_mandate.yaml")
    lock = check_dns_delete_lock(hostname=config.hostname, mandate=mandate)
    evidence.dns_delete_requires_owner = bool(lock.get("escalate"))

    if not has_drafts(task_id, base_dir=work_base):
        raise RehearsalError("the simulated agent left no drafts")
    drafts, discarded = collect_drafts(
        "operations", workdir / "skill_drafts")
    if discarded:
        raise RehearsalError(f"drafts refused: {discarded!r}"[:200])
    origin = run_dir / "origin.git"
    checkout = run_dir / "checkout"
    _seed_origin(origin, checkout)
    save = run_save_step(SaveStepConfig(
        repo=checkout, agent="operations", ticket=config.mirror_ticket,
        drafts_dir=workdir / "skill_drafts",
        lock_path=run_dir / "crystallization.lock",
        validate=config.validate, run_skill_tests=config.run_skill_tests))
    if save.deferred or save.discarded or not save.commit:
        raise RehearsalError(
            f"save step kept nothing: deferred={save.deferred!r} "
            f"discarded={save.discarded!r}")
    origin_ref = _git(["--git-dir", str(origin), "rev-parse", "main"],
                      run_dir).strip()
    evidence.crystallization = {
        "drafts": [draft.name for draft in drafts],
        "created": save.created,
        "commit": save.commit,
        "pushed": origin_ref == save.commit,
    }

    child = sandbox_env(dict(SECRET_SENTINELS))
    leaked_env = [name for name, sentinel in SECRET_SENTINELS.items()
                  if sentinel in " ".join(child.values())]
    leaks = _scan_for(SECRET_SENTINELS, work_base, run_dir / "planner-repo",
                      roots["repos"], roots["projects"], roots["vault"])
    evidence.secrets = {
        "env_vars_checked": len(SECRET_SENTINELS),
        "files_leaked": leaks,
        "env_leaked": leaked_env,
    }
    if leaked_env or leaks:
        raise RehearsalError(
            f"secret leak: env={leaked_env!r} files={leaks!r}"[:300])
    return evidence


def _cli_main(argv: Sequence[str] | None = None) -> int:
    """Manual re-run: one rehearsal over tmp, evidence JSON on stdout."""
    parser = argparse.ArgumentParser(
        description="Pilot 1 rehearsal without tokens (tmp only)")
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.run_dir:
            run_dir = args.run_dir
        else:
            tmp = tempfile.TemporaryDirectory(prefix="pilot-rehearsal-")
            run_dir = tmp.name
        evidence = asyncio.run(run_pilot_rehearsal(
            RehearsalConfig(run_dir=run_dir)))
        print(json.dumps(asdict(evidence), indent=2, sort_keys=True,
                         default=str))
        return 0
    except RehearsalError as exc:
        print(f"rehearsal failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp is not None and not args.keep:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(_cli_main())
