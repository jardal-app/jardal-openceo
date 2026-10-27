"""Acceptance rehearsal without tokens (TASK-2026-00235).

Dry run of the TASK-2026-00224 recorrido with the CEO, the planner,
the agents and the Owner simulated and everything else real: a
mission with two objectives and three agents (dev plus two non-dev),
a loose pedido the planner converts to an objective, ticket-to-ticket
handoff, non-dev closes with verified evidence, a lock that escalates
to the Owner and resumes on the simulated cockpit answer, a draft
promoted from a second same-role ticket, a same-role skill update, a
playbook proposed and approved, a misassigned explicit request that
returns as no_aplica and is reassigned, an OpenCode role switch by
config alone, cost per ticket/objective/mission and declared spend
with prior approval.

No network, no LLM, no Jardal ERP, no Cloudflare, no push outside tmp.
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

from catalog import load_catalog, load_role_profile  # noqa: E402
from objectives import ticket_cost as cost_mod  # noqa: E402
from objectives.agent_result import AgentOutcome, parse_agent_result  # noqa: E402
from objectives.delivery_verification import (  # noqa: E402
    catalog_skill_resolver,
    verify_agent_delivery,
)
from objectives.plan_spend import check_plan_spend  # noqa: E402
from orchestration.crystallization import run_save_step, SaveStepConfig  # noqa: E402
from orchestration.role_opencode import (  # noqa: E402
    opencode_config_bytes,
    render_argv_extras,
)
from orchestration.ticket_handoff import (  # noqa: E402
    TICKET_OUTPUTS_KEY,
    publish_ticket_outputs,
    render_upstream_section,
    resolve_upstream_outputs,
)
from owner_channel import owner_answer_record  # noqa: E402
from router import resolve_explicit_name  # noqa: E402
from state import AgentName  # noqa: E402
from tool_locks import (  # noqa: E402
    check_email_lock,
    lock_hold_action,
    lock_question_spec,
    pending_lock_result,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AcceptanceConfig",
    "AcceptanceError",
    "AcceptanceEvidence",
    "repo_root",
    "run_acceptance_rehearsal",
]

#: Jardal ERP mirror the rehearsal tickets bind to. Memory-only, never
#: persisted; far above the live numbering.
REHEARSAL_MIRROR = "TASK-2026-09998"

_GIT_TIMEOUT_S = 60
_DRAFT_NAME = "acceptance-notes"


class AcceptanceError(RuntimeError):
    """A rehearsal step failed: acceptance is not ready as rehearsed."""


def repo_root() -> Path:
    """Live checkout root (catalog, skills and profiles read from here)."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AcceptanceConfig:
    """What one rehearsal run needs. ``run_dir`` is tmp scratch."""

    run_dir: str | Path
    project: str = "jardal-openceo"
    mirror_ticket: str = REHEARSAL_MIRROR
    validate: Callable[[Path], tuple[bool, str]] | None = None
    run_skill_tests: Callable[..., tuple[bool, str]] | None = None


@dataclass
class AcceptanceEvidence:
    """Structured proof of one rehearsal run (JSON-able)."""

    mission: dict[str, Any] = field(default_factory=dict)
    convert: dict[str, Any] = field(default_factory=dict)
    handoff: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    lock: dict[str, Any] = field(default_factory=dict)
    crystallization: dict[str, Any] = field(default_factory=dict)
    playbook: dict[str, Any] = field(default_factory=dict)
    no_aplica: dict[str, Any] = field(default_factory=dict)
    opencode: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    spend: dict[str, Any] = field(default_factory=dict)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_S)
    if result.returncode != 0:
        raise AcceptanceError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:500]}")
    return result.stdout or ""


class _FakeTickets:
    """create/patch/get/list over a dict (shells, pedidos, questions)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self._seq = 0

    async def create_ticket(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._seq += 1
        task_id = f"acc-{self._seq}"
        row = {**payload, "task_id": task_id, "status": "queued"}
        self.rows[task_id] = row
        self.created.append(row)
        return dict(row)

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> None:
        if task_id in self.rows:
            self.rows[task_id].update(updates)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.rows.get(task_id)
        return dict(row) if row is not None else None

    async def list_by_objective(
        self, objective_id: str
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()
                if row.get("objective_id") == objective_id]


class _FakeErp:
    """Business identity double: group conversions only, recorded."""

    def __init__(self) -> None:
        self.groups: list[str] = []

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        self.groups.append(str(identifier))
        return {"ok": True, "name": str(identifier)}


class _SimulatedPlanner:
    """Fake headless planner: writes one canned file per call."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.calls = 0

    def queue(self, payload: Mapping[str, Any]) -> None:
        self.payloads.append(dict(payload))

    async def run(self, role: str, prompt: str, repo: Any,
                  timeout_s: int) -> Any:
        from objectives.engine import (
            objective_scratch,
            pedido_scratch,
        )

        self.calls += 1
        if not self.payloads:
            raise AcceptanceError("simulated planner ran out of payloads")
        payload = self.payloads.pop(0)
        where = payload.pop("_rehearsal_path")
        if isinstance(where, (list, tuple)) and where[0] == "objective-auto":
            roots = sorted((Path(repo) / ".openceo" / "objectives").glob("*"))
            pending = [item for item in roots
                       if not (item / str(where[1])).is_file()]
            if len(pending) != 1:
                raise AcceptanceError(
                    "objective scratch ambiguous for the canned plan")
            path = pending[0] / str(where[1])
        elif isinstance(where, (list, tuple)):
            path = pedido_scratch(Path(repo), str(where[0])) / str(where[1])
        else:
            path = Path(repo) / str(where)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _pedido(task_id: str, title: str, description: str, project: str,
            mirror: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "ticket_number": 0,
        "title": title,
        "description": description,
        "project": project,
        "source": "api",
        "agent": "ceo",
        "status": "queued",
        "params": {"_mirrors": {"erpnext": {"name": mirror}}},
    }


def _decided_payload(task_id: str, agent: str,
                     skill: str | None = None) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "local_ref": f"acc-{task_id}",
        "title": f"Acceptance work {task_id}",
        "target_agent": agent,
        "work_type": "acceptance",
        "goal": "rehearse the acceptance recorrido",
        "why_now": "acceptance rehearsal",
        "acceptance_criteria": ["recorrido holds"],
        "expected_artifacts": ["evidence"],
    }
    if skill:
        ticket["skill"] = skill
    if agent == "dev":
        ticket["coding_tool"] = "tier_c"
        ticket["tier_reason"] = "specified rehearsal work, default tier"
    return {
        "_rehearsal_path": (task_id, "PLANNER_RESULT.json"),
        "objective_status": "active",
        "state_summary": f"rehearsal pedido decided for {agent}",
        "tickets": [ticket],
        "roadmap_remaining": [],
    }


async def _noop_notify(task_id: str, text: str) -> None:
    del task_id, text


def _birth_payload(*members: tuple[str, str | None]) -> dict[str, Any]:
    tickets = []
    for index, (agent, skill) in enumerate(members):
        planned: dict[str, Any] = {
            "local_ref": f"birth-{index}-{agent}",
            "title": f"Birth work for {agent}",
            "target_agent": agent,
            "work_type": "acceptance",
            "goal": "carry the rehearsed month",
            "why_now": "objective birth",
            "acceptance_criteria": ["recorrido holds"],
            "expected_artifacts": ["evidence"],
        }
        if skill:
            planned["skill"] = skill
        if agent == "dev":
            planned["coding_tool"] = "tier_c"
            planned["tier_reason"] = "specified rehearsal work, default tier"
        tickets.append(planned)
    return {
        "_rehearsal_path": ("objective-auto", "PLANNER_RESULT.json"),
        "objective_status": "active",
        "state_summary": "birth plan for the rehearsed objective",
        "tickets": tickets,
        "roadmap_remaining": [],
    }


async def _step_mission(config: AcceptanceConfig, run_dir: Path,
                        evidence: AcceptanceEvidence,
                        ) -> tuple[Any, _FakeTickets, list[str],
                                   dict[str, dict[str, str]]]:
    from objectives import missions as missions_mod
    from objectives.engine import (
        InMemoryObjectiveStore,
        create_objective,
    )
    from objectives.missions import InMemoryMissionStore, MissionEnv

    tickets = _FakeTickets()
    wakes: list[str] = []
    objectives = InMemoryObjectiveStore()
    env = MissionEnv(
        missions=InMemoryMissionStore(),
        objectives=objectives,
        tasks=tickets,
        create_ticket=tickets.create_ticket,
        patch_task=tickets.patch_task,
        notify=_noop_notify,
        ceo_wake=wakes.append,
    )
    mission = await missions_mod.create_mission(
        env, project=config.project,
        goal="Acceptance rehearsal: monthly close without surprises",
        acceptance=["close checklist done", "report sent to the Owner"],
        requester="owner")
    mission_id = str(mission["mission_id"])
    shell_id = str(mission.get("conversion_source_task_id") or "")
    shell = await tickets.get_task(shell_id) if shell_id else None
    if shell is None or shell.get("status") != "converted":
        raise AcceptanceError("mission shell did not convert at birth")
    if not wakes:
        raise AcceptanceError("mission creation did not wake the CEO")

    from objectives.engine import ObjectiveEnv

    engine_repo = run_dir / "engine-repo"
    engine_repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main", "-q"], engine_repo)
    _git(["-c", "user.name=rehearsal", "-c",
          "user.email=rehearsal@jardal.app", "commit", "-q",
          "--allow-empty", "-m", "seed"], engine_repo)
    births = _SimulatedPlanner()
    births.queue(_birth_payload(("dev", None), ("finance", "registrar-gasto")))
    births.queue(_birth_payload(("secretary", "enviar-correo"),))
    objective_env = ObjectiveEnv(
        objectives=objectives,
        tasks=tickets,  # type: ignore[arg-type]
        create_ticket=tickets.create_ticket,  # type: ignore[arg-type]
        patch_task=tickets.patch_task,  # type: ignore[arg-type]
        executor=births,  # type: ignore[arg-type]
        quota=None, slots=None, reviews=None, inbox=None,  # type: ignore[arg-type]
        notify=_noop_notify, repo=engine_repo, erp=_FakeErp(),  # type: ignore[arg-type]
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(repo_root() / "openceo-core"
                               / "objectives" / "routing_policy.md"),
            "agents": {"dev": {"enabled": True},
                       "finance": {"enabled": True},
                       "secretary": {"enabled": True}},
        },
        missions=env.missions)
    made = []
    children: dict[str, dict[str, str]] = {}
    for goal in ("Reconcile the month", "Report the month"):
        created = await create_objective(
            objective_env, project=config.project, goal=goal,
            acceptance=[f"{goal}: done with evidence"], requester="owner")
        objective_id = str(created["objective_id"])
        await objectives.patch_objective(
            objective_id, {"mission_id": mission_id})
        linked = await missions_mod.record_objective_linked(
            env.missions, mission_id, objective_id)
        if linked is None:
            raise AcceptanceError(f"{objective_id} missed the ledger")
        made.append(objective_id)
        born = [row for row in tickets.created
                if row.get("objective_id") == objective_id
                and row.get("origin") == "objective_planner"]
        children[objective_id] = {
            str((row.get("params") or {}).get("target_agent") or ""): str(
                row["task_id"]) for row in born}
    if set(children.get(made[0], {})) != {"dev", "finance"}:
        raise AcceptanceError(
            f"objective 1 birth missed its agents: {children!r}"[:300])
    if set(children.get(made[1], {})) != {"secretary"}:
        raise AcceptanceError(
            f"objective 2 birth missed its agent: {children!r}"[:300])
    evidence.mission = {
        "mission_id": mission_id,
        "shell_status": shell.get("status"),
        "ceo_wakes": len(wakes),
        "objectives": made,
        "birth_children": {key: sorted(value)
                           for key, value in children.items()},
    }
    return objective_env, tickets, made, children


async def _step_convert(config: AcceptanceConfig, run_dir: Path,
                        evidence: AcceptanceEvidence, tickets: _FakeTickets,
                        ) -> str:
    from objectives.engine import ObjectiveEnv, plan_pedido

    repo = run_dir / "convert-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main", "-q"], repo)
    _git(["-c", "user.name=rehearsal", "-c",
          "user.email=rehearsal@jardal.app", "commit", "-q",
          "--allow-empty", "-m", "seed"], repo)
    planner = _SimulatedPlanner()
    task_id = f"loose-{uuid.uuid4().hex[:12]}"
    planner.queue({
        "_rehearsal_path": (task_id, "PLANNER_RESULT.json"),
        "objective_status": "capability_gap",
        "state_summary": "too big for one ticket: needs an objective",
        "tickets": [],
        "roadmap_remaining": [],
    })
    ticket = _pedido(
        task_id, "Migrate the legacy inbox",
        "Acceptance rehearsal: a loose pedido the planner converts.",
        config.project, config.mirror_ticket)

    async def patch_task(task_id: str, updates: Mapping[str, Any]) -> None:
        ticket.update(updates)

    env = ObjectiveEnv(
        objectives=None, tasks=None, create_ticket=None,  # type: ignore[arg-type]
        patch_task=patch_task,  # type: ignore[arg-type]
        executor=planner,  # type: ignore[arg-type]
        quota=None, slots=None, reviews=None, inbox=None,  # type: ignore[arg-type]
        notify=None, repo=run_dir / "planner-repo",
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(repo_root() / "openceo-core"
                               / "objectives" / "routing_policy.md"),
            "agents": {"operations": {"enabled": True}},
        })
    outcome = await plan_pedido(env, ticket)
    if not outcome.get("ok") or outcome.get("action") != "convert":
        raise AcceptanceError(f"pedido did not convert: {outcome!r}"[:300])
    evidence.convert = {
        "action": outcome["action"],
        "goal": str(outcome.get("goal") or "")[:80],
    }
    return task_id


async def _step_handoff(config: AcceptanceConfig, run_dir: Path,
                        evidence: AcceptanceEvidence, tickets: _FakeTickets,
                        dev_id: str, fin_id: str) -> tuple[str, str]:
    work = run_dir / "ticket-work"
    dev_folder = work / "dev-ticket"
    dev_folder.mkdir(parents=True, exist_ok=True)
    (dev_folder / "reconciliation.csv").write_text(
        "month,total\n2026-10,1250.00\n", encoding="utf-8")
    dev_verdict = {
        "resultado": "hecho",
        "motivo": "month reconciled from the bank export",
        "necesidad": "",
        "entregables": ["reconciliation.csv"],
        "evidencia": "reconciliation.csv totals 1250.00 for 2026-10",
    }
    attached: list[dict[str, Any]] = []

    async def fake_attach(mirror: str, **kwargs: Any) -> dict[str, Any]:
        record = {"id": f"att-{len(attached) + 1}",
                  "file_name": kwargs.get("file_name")}
        attached.append(record)
        return {"attachment": {"name": record["id"],
                               "file_name": record["file_name"]},
                "duplicate": False}

    dev_row = await tickets.get_task(dev_id)
    if dev_row is None:
        raise AcceptanceError("birth dev child went missing")
    await tickets.patch_task(dev_id, {
        "params": {**dev_row.get("params", {}), "agent_result": dev_verdict}})
    record = await publish_ticket_outputs(
        agent="dev", skill=None, verdict=dev_verdict,
        ticket_folder=dev_folder, mirror=config.mirror_ticket,
        attach=fake_attach, declared_outputs=["reconciliation.csv"])
    if not record.get("files"):
        raise AcceptanceError("dev outputs carried no files")
    if any(entry.get("skipped") for entry in record["files"]):
        raise AcceptanceError(
            f"dev outputs skipped: {record['files']!r}"[:200])
    if not attached:
        raise AcceptanceError("dev file never reached the mirror")
    await tickets.patch_task(dev_id, {
        "status": "completed",
        "params": {**dev_row.get("params", {}), "agent_result": dev_verdict,
                   TICKET_OUTPUTS_KEY: record},
    })
    fin_row = await tickets.get_task(fin_id)
    if fin_row is None:
        raise AcceptanceError("birth finance child went missing")
    await tickets.patch_task(fin_id, {
        "params": {**fin_row.get("params", {}), "blocked_by": [dev_id]}})
    upstream = await resolve_upstream_outputs(tickets, [dev_id])
    if len(upstream) != 1 or upstream[0].get("skipped") is not None:
        raise AcceptanceError(f"upstream did not resolve: {upstream!r}"[:200])
    section = render_upstream_section(upstream)
    if "reconciliation.csv" not in section:
        raise AcceptanceError("upstream section hides the dev file")
    evidence.handoff = {
        "dev_ticket": dev_id,
        "finance_ticket": fin_id,
        "files": [entry.get("name") for entry in record["files"]],
        "attachments": len(attached),
        "upstream_section_chars": len(section),
    }
    return dev_id, fin_id


async def _step_verification(evidence: AcceptanceEvidence,
                             tickets: _FakeTickets, fin_id: str) -> None:
    child = await tickets.get_task(fin_id)
    if child is None:
        raise AcceptanceError("finance ticket went missing")
    verdict = {
        "resultado": "hecho",
        "motivo": "expense booked from the reconciled totals",
        "necesidad": "",
        "entregables": ["journal_entry JE-ACC-001"],
        "evidencia": "journal_entry JE-ACC-001 booked for 1250.00 BOB",
    }
    params = {**child.get("params", {}), "agent_result": verdict}
    child = {**child, "params": params}
    report = await verify_agent_delivery(
        child, resolve_skill=catalog_skill_resolver())
    if not report.get("ok"):
        raise AcceptanceError(
            f"finance delivery unverified: {report!r}"[:300])
    await tickets.patch_task(fin_id, {"status": "completed",
                                      "params": params})
    evidence.verification = {
        "ticket": fin_id,
        "skill": "registrar-gasto",
        "verdict": "hecho",
    }


async def _step_lock(config: AcceptanceConfig,
                     evidence: AcceptanceEvidence, tickets: _FakeTickets,
                     sec_id: str) -> None:
    from company_mandate import load_mandate_file

    mandate = load_mandate_file(
        repo_root() / "projects" / "company_mandate.yaml")
    project_config = {"secretary": {"allowed_recipients": ["owner@jardal.app"]}}
    decision = check_email_lock(
        to="auditor@example.com", project_config=project_config,
        mandate=mandate)
    if not decision.get("escalate"):
        raise AcceptanceError("off-list email did not escalate")
    action = {"to": ["auditor@example.com"],
              "subject": "Monthly report",
              "body": "The month closed clean."}
    fingerprint = "acc-email-monthly-report"
    spec = lock_question_spec(
        tool="openceo_send_email", name="openceo_send_email",
        question="Send the monthly report to auditor@example.com?",
        action=action, fingerprint=fingerprint, decision=decision,
        describe={"to": "auditor@example.com"},
        origin_title="Report the month", project=config.project,
        source_id="acc-lock:monthly-report")
    question = await tickets.create_ticket({
        "title": spec["title"],
        "description": spec["description"],
        "project": config.project,
        "params": spec["params"],
    })
    question_id = str(question["task_id"])
    pending = pending_lock_result(
        tool="openceo_send_email", question_task_id=question_id,
        waiting_reason="owner-question:lock",
        detail="waiting for the Owner to approve the send")
    if pending.get("status") != "pending_approval":
        raise AcceptanceError(f"lock did not park: {pending!r}"[:200])
    stored = lock_hold_action(question)
    if stored is None or stored.get("fingerprint") != fingerprint:
        raise AcceptanceError("held lock action unreadable")
    answer = owner_answer_record(
        approve=True, actor_id="owner",
        comment="Approved: the auditor expects it.",
        decided_at="2026-10-13T00:00:00+00:00")
    params = dict(question.get("params") or {})
    block = dict(params.get("owner_question") or {})
    block["decision"] = answer
    params["owner_question"] = block
    await tickets.patch_task(question_id, {"status": "completed",
                                           "params": params})
    sent = {"ok": True, "tool": stored["tool"], "to": action["to"],
            "fingerprint": stored["fingerprint"]}
    sec_row = await tickets.get_task(sec_id)
    if sec_row is None:
        raise AcceptanceError("birth secretary child went missing")
    await tickets.patch_task(sec_id, {
        "status": "completed",
        "params": {**sec_row.get("params", {}),
                   "agent_result": {
                       "resultado": "hecho",
                       "motivo": ("report sent after Owner approval "
                                  f"({question_id})"),
                       "necesidad": "",
                       "entregables": ["sent email to auditor@example.com"],
                       "evidencia": json.dumps(sent)[:500],
                   }},
    })
    evidence.lock = {
        "question": question_id,
        "decision": answer["decision"],
        "executed_tool": sent["tool"],
        "secretary_ticket": sec_id,
    }


def _seed_origin(origin: Path, checkout: Path) -> None:
    _git(["init", "--bare", "-q", str(origin)], origin.parent)
    _git(["clone", "-q", str(origin), str(checkout)], origin.parent)
    _git(["checkout", "-q", "-b", "main"], checkout)
    root = repo_root()
    for rel in ("catalog/agents/finance.yaml",
                "catalog/agents/secretary.yaml",
                "catalog/profiles/finance.yaml",
                "catalog/profiles/secretary.yaml"):
        target = checkout / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
    for role in ("finance", "secretary"):
        shutil.copytree(root / "skills" / role,
                        checkout / "skills" / role)
    _git(["add", "."], checkout)
    _git(["-c", "user.name=rehearsal", "-c",
          "user.email=rehearsal@jardal.app", "commit", "-q", "-m",
          "rehearsal seed"], checkout)
    _git(["push", "-q", "-u", "origin", "main"], checkout)


def _leave_draft(drafts_dir: Path, body: str) -> Path:
    target = drafts_dir / _DRAFT_NAME
    if target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {_DRAFT_NAME}\ndescription: "
        "\"Acceptance draft: monthly-close checklist notes.\"\n---\n\n"
        f"{body}\n",
        encoding="utf-8")
    return drafts_dir


async def _step_crystallization(config: AcceptanceConfig, run_dir: Path,
                                evidence: AcceptanceEvidence,
                                tickets: _FakeTickets) -> None:
    origin = run_dir / "origin.git"
    checkout = run_dir / "checkout"
    _seed_origin(origin, checkout)
    drafts = run_dir / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)

    def save(ticket: str) -> Any:
        return run_save_step(SaveStepConfig(
            repo=checkout, agent="finance", ticket=ticket,
            drafts_dir=drafts,
            lock_path=run_dir / "crystallization.lock",
            validate=(lambda path: (True, "rehearsal")),
            run_skill_tests=(lambda **kwargs: (True, "rehearsal"))))

    _leave_draft(drafts, "Ticket A notes: reconcile before booking.")
    first = save("TASK-2026-09998")
    if first.created != [_DRAFT_NAME] or not first.commit:
        raise AcceptanceError(
            f"draft not created: {first.created!r} {first.discarded!r}")
    _leave_draft(drafts, "Ticket A notes: reconcile before booking.")
    second = save("TASK-2026-09997")
    if second.promoted != [_DRAFT_NAME]:
        raise AcceptanceError(
            f"draft not promoted: {second.promoted!r} "
            f"{second.discarded!r}")
    _leave_draft(drafts, "Ticket C notes: reconcile before booking.\n"
                         "Confirmed twice: bank export first.")
    third = save("TASK-2026-09996")
    if third.updated != [_DRAFT_NAME]:
        raise AcceptanceError(
            f"same-role improvement missed: {third.updated!r} "
            f"{third.discarded!r}")
    _leave_draft(drafts, "Ticket C notes: reconcile before booking.\n"
                         "Confirmed twice: bank export first.")
    foreign = drafts / "enviar-correo"
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / "SKILL.md").write_text(
        "---\nname: enviar-correo\ndescription: \"Cross-role attempt.\"\n"
        "---\n\nMust not land in finance.\n",
        encoding="utf-8")
    fourth = save("TASK-2026-09995")
    if "enviar-correo" not in fourth.discarded:
        raise AcceptanceError("cross-role skill name was not refused")
    origin_ref = _git(["--git-dir", str(origin), "rev-parse", "main"],
                      run_dir).strip()
    evidence.crystallization = {
        "created": first.created,
        "promoted": second.promoted,
        "updated": third.updated,
        "cross_role_refused": sorted(fourth.discarded),
        "pushed": origin_ref == (fourth.commit or third.commit),
    }


async def _step_playbook(config: AcceptanceConfig, run_dir: Path,
                         evidence: AcceptanceEvidence, tickets: _FakeTickets,
                         objective_id: str) -> None:
    from objectives import playbooks as playbooks_mod

    objective = {"objective_id": objective_id, "status": "closed",
                 "project": config.project,
                 "goal": "Reconcile the month",
                 "anchor": {"goal": "Reconcile the month",
                            "acceptance": ["done with evidence"]},
                 "delivery": {"verdict": "closed with evidence"}}

    class _Objectives:
        async def get_objective(
            self, objective_id: str
        ) -> dict[str, Any] | None:
            if objective_id == objective["objective_id"]:
                return dict(objective)
            return None

    planner = _SimulatedPlanner()
    rel = (f"{playbooks_mod.PLAYBOOK_SCRATCH_PREFIX}/{objective_id}/"
           f"{playbooks_mod.PLAYBOOK_PROPOSAL_FILE}")
    planner.queue({
        "_rehearsal_path": rel,
        "recurrent": True,
        "reason": "every month reconciles, books and reports",
        "playbook": {
            "name": "monthly-close",
            "summary": "Reconcile, book and report the month",
            "when_to_use": "at every month end",
            "stages": [
                {"order": 1, "agent": "dev", "goal": "reconcile",
                 "inputs": [], "outputs": ["reconciliation.csv"]},
                {"order": 2, "agent": "finance",
                 "skill": "registrar-gasto", "goal": "book",
                 "inputs": ["reconciliation.csv"],
                 "outputs": ["journal_entry"]},
                {"order": 3, "agent": "secretary",
                 "skill": "enviar-correo", "goal": "report",
                 "inputs": ["journal_entry"], "outputs": ["sent email"]},
            ],
            "stop": ["report sent"],
        },
    })
    playbook_repo = run_dir / "playbook-repo"
    playbook_repo.mkdir(parents=True, exist_ok=True)
    env = SimpleNamespace(
        objectives=_Objectives(), tasks=tickets, repo=playbook_repo,
        planner_cfg={"timeout_minutes": 15}, executor=planner,
        create_ticket=tickets.create_ticket,
        patch_task=tickets.patch_task)
    agents = {name: {"enabled": True}
              for name in ("dev", "finance", "secretary")}
    skills = {"finance": ["registrar-gasto"],
              "secretary": ["enviar-correo"], "dev": []}
    proposed = await playbooks_mod.propose_playbook(
        env, objective_id, "close", agents=agents, skills=skills)
    if not proposed.get("ok") or proposed.get("action") != "proposed":
        raise AcceptanceError(f"playbook not proposed: {proposed!r}"[:300])
    question_id = str(proposed["question_task_id"])
    question = await tickets.get_task(question_id)
    if question is None:
        raise AcceptanceError("playbook question went missing")
    params = dict(question.get("params") or {})
    block = dict(params.get("owner_question") or {})
    if block.get("kind") != "playbook":
        raise AcceptanceError("proposal asked nothing of kind playbook")
    answer = owner_answer_record(
        approve=True, actor_id="owner",
        comment="Approved: monthly-close is our recipe.",
        decided_at="2026-10-13T00:00:00+00:00")
    block["decision"] = answer
    params["owner_question"] = block
    await tickets.patch_task(question_id, {"status": "completed",
                                           "params": params})
    books = run_dir / "playbooks.git"
    books_checkout = run_dir / "playbooks-checkout"
    _git(["init", "--bare", "-q", str(books)], run_dir)
    _git(["clone", "-q", str(books), str(books_checkout)], run_dir)
    _git(["checkout", "-q", "-b", "main"], books_checkout)
    (books_checkout / "README.md").write_text("playbooks\n", encoding="utf-8")
    _git(["add", "."], books_checkout)
    _git(["-c", "user.name=rehearsal", "-c",
          "user.email=rehearsal@jardal.app", "commit", "-q", "-m",
          "seed"], books_checkout)
    _git(["push", "-q", "-u", "origin", "main"], books_checkout)
    draft = dict(block.get("playbook") or {})
    saved = playbooks_mod.save_approved_playbook(
        books_checkout, draft, approval={
            "question": question_id,
            "actor": "owner",
            "at": answer["at"]})
    if not saved.get("ok"):
        raise AcceptanceError(f"playbook save refused: {saved!r}"[:200])
    _git(["pull", "-q", "origin", "main"], books_checkout)
    loaded = playbooks_mod.load_playbooks(books_checkout)
    if "monthly-close" not in loaded:
        raise AcceptanceError("approved playbook never landed in git")
    evidence.playbook = {
        "question": question_id,
        "decision": answer["decision"],
        "saved": saved.get("name"),
        "loaded": "monthly-close" in loaded,
    }


async def _step_no_aplica(config: AcceptanceConfig, run_dir: Path,
                           evidence: AcceptanceEvidence,
                           tickets: _FakeTickets, objective_id: str,
                           objectives: Any) -> None:
    from objectives.engine import (
        ObjectiveEnv,
        plan_pedido,
        record_planner_return,
    )

    resolved = resolve_explicit_name("finance", None)
    if resolved is not AgentName.FINANCE:
        raise AcceptanceError("explicit finance request did not validate")
    wrong = await tickets.create_ticket({
        "title": "Tune the reconciliation query",
        "project": config.project,
        "objective_id": objective_id,
        "params": {"target_agent": "finance", "decided_by": "explicit"},
    })
    wrong_id = str(wrong["task_id"])
    parsed = parse_agent_result(
        "Finance cannot tune SQL.\n```agent-result\n" + json.dumps({
            "resultado": "no_aplica",
            "motivo": "needs dev: query plan work",
            "necesidad": "a dev to rewrite the query",
            "entregables": [],
            "evidencia": "",
        }) + "\n```")
    if parsed is None or parsed.resultado != AgentOutcome.NO_APLICA:
        raise AcceptanceError("no_aplica verdict did not parse")
    await tickets.patch_task(wrong_id, {"status": "failed"})
    entry = await record_planner_return(
        objectives, objective_id, task_id=wrong_id,
        title="Tune the reconciliation query", agent="finance",
        resultado=parsed.resultado.value, motivo=parsed.motivo,
        necesidad=parsed.necesidad)
    if not entry:
        raise AcceptanceError("planner return missed the ledger")
    planner = _SimulatedPlanner()
    task_id = f"return-{uuid.uuid4().hex[:12]}"
    planner.queue(_decided_payload(task_id, "dev"))
    pedido = _pedido(
        task_id, "Tune the reconciliation query",
        "Acceptance rehearsal: the planner reassigns a returned pedido.",
        config.project, config.mirror_ticket)

    async def patch_task(task_id: str, updates: Mapping[str, Any]) -> None:
        pedido.update(updates)

    env = ObjectiveEnv(
        objectives=None, tasks=None, create_ticket=None,  # type: ignore[arg-type]
        patch_task=patch_task,  # type: ignore[arg-type]
        executor=planner,  # type: ignore[arg-type]
        quota=None, slots=None, reviews=None, inbox=None,  # type: ignore[arg-type]
        notify=None, repo=run_dir / "return-repo",
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(repo_root() / "openceo-core"
                               / "objectives" / "routing_policy.md"),
            "agents": {"dev": {"enabled": True}},
        })
    outcome = await plan_pedido(env, pedido)
    if not outcome.get("ok") or outcome.get("action") != "decided":
        raise AcceptanceError(f"return was not re-decided: {outcome!r}"[:300])
    decision = outcome.get("decision") or {}
    if decision.get("target_agent") != "dev":
        raise AcceptanceError(f"return misassigned again: {decision!r}"[:200])
    evidence.no_aplica = {
        "explicit_agent": resolved.value,
        "verdict": parsed.resultado.value,
        "reassigned_to": decision.get("target_agent"),
    }


def _step_opencode(evidence: AcceptanceEvidence, run_dir: Path) -> None:
    profile = load_role_profile("finance")
    home = run_dir / "fake-home"
    overlay = home / ".config" / "opencode" / "opencode.json"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(json.dumps({"$schema": "rehearsal",
                                   "agent": {}}), encoding="utf-8")
    blob = opencode_config_bytes(
        profile, home, {"openceo": {"type": "remote",
                                    "url": "http://127.0.0.1:18765/mcp",
                                    "headers": {"X-Rehearsal": "1"}}})
    rendered = json.loads(blob.decode("utf-8"))
    entry = (rendered.get("agent") or {}).get("finance") or {}
    if "registrar-gasto" not in str(entry.get("prompt") or ""):
        raise AcceptanceError("opencode entry lost the finance skills")
    argv = render_argv_extras("finance")
    if argv != ["--agent", "finance"]:
        raise AcceptanceError(f"opencode argv drifted: {argv!r}")
    binary = shutil.which("opencode")
    if binary is None:
        raise AcceptanceError("opencode binary missing: boot unverified")
    booted = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=60)
    if booted.returncode != 0:
        raise AcceptanceError("opencode --version refused to boot")
    evidence.opencode = {
        "entry_skills": sorted((entry.get("permission") or {}).get(
            "skill", {})) if isinstance(
                (entry.get("permission") or {}).get("skill"), dict)
        else str((entry.get("permission") or {}).get("skill")),
        "argv": argv,
        "version": (booted.stdout or booted.stderr or "").strip()[:40],
    }


def _step_cost(evidence: AcceptanceEvidence) -> None:
    dev_runs = [cost_mod.run_from_exec_result(
        usage={}, elapsed_s=12.0, engine="muse", model="rehearsal-sim")]
    fin_runs = [cost_mod.run_from_exec_result(
        usage={}, elapsed_s=3.5, engine="muse", model="rehearsal-sim")]
    sec_runs = [cost_mod.run_from_exec_result(
        usage={}, elapsed_s=1.0, engine="muse", model="rehearsal-sim")]
    dev_cost = cost_mod.record_runs(None, dev_runs)
    fin_cost = cost_mod.record_runs(None, fin_runs)
    sec_cost = cost_mod.record_runs(None, sec_runs)
    obj1 = cost_mod.aggregate_objective_cost([dev_cost, fin_cost])
    obj2 = cost_mod.aggregate_objective_cost([sec_cost])
    mission = cost_mod.aggregate_mission_cost([dev_cost, fin_cost, sec_cost])
    if obj1 is None or obj1["runs"] != 2:
        raise AcceptanceError("objective 1 cost miscounted its runs")
    if obj2 is None or obj2["runs"] != 1:
        raise AcceptanceError("objective 2 cost miscounted its runs")
    if mission is None or mission["runs"] != 3:
        raise AcceptanceError("mission cost miscounted its runs")
    if mission["duration_s"] != 16.5:
        raise AcceptanceError("mission cost miscounted durations")
    evidence.cost = {
        "objective_runs": [obj1["runs"], obj2["runs"]],
        "mission_runs": mission["runs"],
        "mission_duration_s": mission["duration_s"],
        "tokens_unknown_runs": mission["tokens_unknown_runs"],
    }


def _step_spend(config: AcceptanceConfig,
                evidence: AcceptanceEvidence) -> None:
    from company_mandate import load_mandate_file

    mandate = load_mandate_file(
        repo_root() / "projects" / "company_mandate.yaml")
    verdict = check_plan_spend(
        costs=[{"label": "auditor review", "amount": "150.00"}],
        summary="Monthly close with an external review",
        mandate=mandate)
    if not verdict.get("escalate"):
        raise AcceptanceError("declared spend launched without approval")
    if verdict.get("category") != "gasto_nuevo":
        raise AcceptanceError(
            f"spend category drifted: {verdict.get('category')!r}")
    answer = owner_answer_record(
        approve=True, actor_id="owner",
        comment="Approved: the review is budgeted.",
        decided_at="2026-10-13T00:00:00+00:00")
    evidence.spend = {
        "amount": verdict.get("amount"),
        "currency": verdict.get("currency"),
        "category": verdict.get("category"),
        "decision": answer["decision"],
        "launched_after_approval": True,
    }


async def _step_close(evidence: AcceptanceEvidence, tickets: _FakeTickets,
                      objective_env: Any, mission_id: str,
                      made: list[str]) -> None:
    from objectives import missions as missions_mod

    objectives = objective_env.objectives
    for objective_id in made:
        await objectives.patch_objective(objective_id, {
            "status": "closed",
            "delivery": {"verdict": "closed with evidence",
                         "tickets": "hecho with citations"}})
        marked = await missions_mod.record_objective_closed(
            objective_env.missions, mission_id, objective_id)
        if marked is None:
            raise AcceptanceError(f"{objective_id} missed the close mark")
    env = SimpleNamespace(
        missions=objective_env.missions, objectives=objectives,
        tasks=tickets, patch_task=tickets.patch_task,
        notify=_noop_notify, now=lambda: None)
    closed = await missions_mod.close_mission(
        env, mission_id, reason="acceptance met with evidence")
    if not closed.get("ok"):
        raise AcceptanceError(f"mission did not close: {closed!r}"[:200])
    mission = await objective_env.missions.get_mission(mission_id)
    if mission is None or mission.get("status") != "closed":
        raise AcceptanceError("mission doc is not closed")
    ledger = mission.get("objectives") or []
    if any(str(item.get("status") or "") != "closed" for item in ledger):
        raise AcceptanceError("mission ledger kept an open objective")
    evidence.mission["closed"] = True


async def run_acceptance_rehearsal(
    config: AcceptanceConfig,
) -> AcceptanceEvidence:
    """Run the full acceptance recorrido with everything simulated."""
    if shutil.which("git") is None:
        raise AcceptanceError("the rehearsal needs git for tmp remotes")
    evidence = AcceptanceEvidence()
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    objective_env, tickets, made, children = await _step_mission(
        config, run_dir, evidence)
    await _step_convert(config, run_dir, evidence, tickets)
    dev_id = children[made[0]]["dev"]
    fin_id = children[made[0]]["finance"]
    sec_id = children[made[1]]["secretary"]
    await _step_handoff(config, run_dir, evidence, tickets, dev_id, fin_id)
    await _step_verification(evidence, tickets, fin_id)
    await _step_lock(config, evidence, tickets, sec_id)
    await _step_crystallization(config, run_dir, evidence, tickets)
    await _step_playbook(config, run_dir, evidence, tickets, made[0])
    await _step_no_aplica(config, run_dir, evidence, tickets, made[0],
                          objective_env.objectives)
    _step_opencode(evidence, run_dir)
    _step_cost(evidence)
    _step_spend(config, evidence)
    await _step_close(evidence, tickets, objective_env,
                      str(evidence.mission["mission_id"]), made)
    catalog = load_catalog()
    for role in ("dev", "finance", "secretary"):
        if role not in catalog.agents:
            raise AcceptanceError(f"catalog lost role {role}")
    return evidence


def _cli_main(argv: Sequence[str] | None = None) -> int:
    """Manual re-run: one rehearsal over tmp, evidence JSON on stdout."""
    parser = argparse.ArgumentParser(
        description="Acceptance rehearsal without tokens (tmp only)")
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.run_dir:
            run_dir = args.run_dir
        else:
            tmp = tempfile.TemporaryDirectory(prefix="acceptance-rehearsal-")
            run_dir = tmp.name
        evidence = asyncio.run(run_acceptance_rehearsal(
            AcceptanceConfig(run_dir=run_dir)))
        print(json.dumps(asdict(evidence), indent=2, sort_keys=True,
                         default=str))
        return 0
    except AcceptanceError as exc:
        print(f"rehearsal failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp is not None and not args.keep:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(_cli_main())
