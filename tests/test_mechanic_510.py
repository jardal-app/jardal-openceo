"""Engine mechanic (TASK-2026-00510): generic stall rule, verdict, actions, loop.

In-memory repo, fake executor writing the verdict file, real git in temp
dirs. No models, no network.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from catalog import load_role_profile  # noqa: E402
from config_loader import ConfigLoader  # noqa: E402
from mcp_identity import READONLY_PRINCIPALS  # noqa: E402
from mechanic.actions import ActionRunner  # noqa: E402
from mechanic.models import (  # noqa: E402
    MechanicAction,
    MechanicVerdict,
    VerdictContext,
    parse_verdict,
    validate_verdict,
)
from mechanic.service import MechanicService, build_prompt, mechanic_config  # noqa: E402
from mechanic.situation import Finding, compile_situation  # noqa: E402
from mechanic.stall import STREAK_KEY, streak_outcome  # noqa: E402
from orchestration.role_contracts import is_read_only  # noqa: E402
from settings import Settings  # noqa: E402
from reliability.deadlines import DeadlineWatch  # noqa: E402
from task_service import TaskService  # noqa: E402

NOW = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)
GAP = timedelta(hours=12)


def _outcome(status="waiting", reason="repo_busy: /r dirty", resume=True,
             params=None, now=NOW, **kw):
    return streak_outcome(
        status=status, reason=reason,
        resume_at=(now + timedelta(minutes=15)) if resume else None,
        params=params or {}, now=now, max_reparks=3, gap=GAP, **kw)


# -- generic stall rule ---------------------------------------------------------

def test_same_wait_reparked_three_times_stalls_and_stops_reparking() -> None:
    params: dict[str, Any] = {}
    for count in (1, 2):
        fields, trigger = _outcome(params=params)
        assert trigger is None and fields[STREAK_KEY]["count"] == count
        params = {STREAK_KEY: fields[STREAK_KEY]}
    fields, trigger = _outcome(params=params)
    assert trigger == "stall"
    assert fields["resume_at"] is None
    assert fields["waiting_reason"].startswith("stalled: 3x repo_busy")


def test_any_reason_counts_the_same_way() -> None:
    params: dict[str, Any] = {}
    for _ in range(3):
        fields, trigger = _outcome(reason="brand_new_wait: never seen", params=params)
        params = {STREAK_KEY: fields[STREAK_KEY]}
    assert trigger == "stall"


def test_new_reason_or_long_gap_restarts_the_count() -> None:
    first, _ = _outcome()
    fields, _ = _outcome(reason="rate_limit: reset", params={STREAK_KEY: first[STREAK_KEY]})
    assert fields[STREAK_KEY]["count"] == 1
    later = NOW + GAP + timedelta(minutes=1)
    fields, _ = _outcome(params={STREAK_KEY: first[STREAK_KEY]}, now=later)
    assert fields[STREAK_KEY]["count"] == 1


def test_wait_without_resume_is_a_give_up_unless_human_or_self_healing() -> None:
    assert _outcome(reason="needs_owner: invalid plan", resume=False)[1] == "gave_up"
    assert _outcome(reason="stalled_no_progress: same_work", resume=False)[1] == "no_progress"
    for reason in ("phase:qa_review", "owner_action: pay", "tool_lock:email",
                   "spend_approval", "owner_escalation: x",
                   "jardal_erp_pending", "mechanic_wait: quota"):
        assert _outcome(reason=reason, resume=False) == ({}, None), reason
    assert _outcome(reason="x", resume=False,
                    params={"owner_question_id": "q1"}) == ({}, None)
    assert _outcome(reason="x", resume=False, qa_state="pending") == ({}, None)


def test_dependency_and_admission_waits_never_trigger() -> None:
    assert _outcome(reason="blocked_by: t9 (in_progress)", resume=False) == ({}, None)
    params: dict[str, Any] = {}
    for _ in range(5):
        fields, trigger = _outcome(reason="quota_reserved", params=params)
        assert trigger is None
        params = {STREAK_KEY: fields[STREAK_KEY]}
    assert params[STREAK_KEY]["count"] == 5


def test_leaving_waiting_clears_the_streak() -> None:
    first, _ = _outcome()
    assert _outcome(status="completed", params={STREAK_KEY: first[STREAK_KEY]}) == (
        {STREAK_KEY: None}, None)
    assert _outcome(status="completed") == ({}, None)


def _service_with_wake():
    service = TaskService.__new__(TaskService)
    service._config_loader = MagicMock()
    service._config_loader.load_project.return_value = {}
    return service


def test_run_outcome_write_merges_streak_into_params_or_dotted_key() -> None:
    service = _service_with_wake()
    updates = {"waiting_reason": "repo_busy: x", "resume_at": NOW,
               "params": {"a": 1}}
    assert service._apply_wait_streak(updates, {}, "waiting") is None
    assert updates["params"]["a"] == 1 and updates["params"][STREAK_KEY]["count"] == 1
    updates = {"waiting_reason": "repo_busy: x", "resume_at": NOW}
    current = {"params": {STREAK_KEY: {"key": "repo_busy", "count": 2,
                                       "since": NOW.isoformat(),
                                       "last_at": datetime.now(timezone.utc).isoformat()}}}
    assert service._apply_wait_streak(updates, current, "waiting") == "stall"
    assert updates[f"params.{STREAK_KEY}"]["count"] == 3
    assert updates["resume_at"] is None


def test_disabled_mechanic_leaves_writes_untouched() -> None:
    service = _service_with_wake()
    service._config_loader.load_project.return_value = {"mechanic": {"enabled": False}}
    updates = {"waiting_reason": "needs_owner: x", "resume_at": None}
    assert service._apply_wait_streak(updates, {}, "waiting") is None
    assert updates == {"waiting_reason": "needs_owner: x", "resume_at": None}


# -- verdict ---------------------------------------------------------------------

CTX = VerdictContext(run_id="MEC-1", task_ids=frozenset({"t1", "t2"}),
                     waiting_ids=frozenset({"t1"}), repos=frozenset({"/repo"}))


def _verdict(*actions: dict) -> MechanicVerdict:
    return parse_verdict(json.dumps({
        "run_id": "MEC-1", "summary": "s", "diagnosis": "d",
        "actions": list(actions)}))


def test_valid_verdict_passes() -> None:
    verdict = _verdict(
        {"kind": "wait", "task_id": "t1", "minutes": 30, "text": "quota"},
        {"kind": "comment", "task_id": "t2", "text": "nota"},
        {"kind": "git_unstage", "repo": "/repo", "paths": [".openceo/H.md"],
         "text": "scratch"},
        {"kind": "fix_ticket", "title": "Planner pierde la línea ESTIMATE",
         "text": "x" * 50})
    assert validate_verdict(verdict, CTX) == []


@pytest.mark.parametrize("action, needle", [
    ({"kind": "resume", "task_id": "zz", "text": "x"}, "findings"),
    ({"kind": "resume", "task_id": "t2", "text": "x"}, "only waiting"),
    ({"kind": "wait", "task_id": "t1", "minutes": 2, "text": "x"}, "minutes"),
    ({"kind": "git_unstage", "repo": "/repo", "paths": ["../etc"], "text": "x"}, "relative"),
    ({"kind": "git_stash", "repo": "/other", "text": "x"}, "situation repos"),
    ({"kind": "fix_ticket", "title": "corto", "text": "x"}, "title"),
    ({"kind": "comment", "task_id": "t1", "text": ""}, "text"),
])
def test_invalid_actions_are_rejected(action: dict, needle: str) -> None:
    errors = validate_verdict(_verdict(action), CTX)
    assert any(needle in error for error in errors), errors


def test_wrong_run_id_and_unknown_fields_are_rejected() -> None:
    bad = parse_verdict(json.dumps({"run_id": "MEC-2", "summary": "s",
                                    "diagnosis": "d", "actions": []}))
    assert any("run_id" in error for error in validate_verdict(bad, CTX))
    with pytest.raises(ValueError):
        parse_verdict(json.dumps({"run_id": "MEC-1", "summary": "s",
                                  "diagnosis": "d", "actions": [],
                                  "extra": 1}))


def test_one_state_action_per_ticket() -> None:
    verdict = _verdict({"kind": "resume", "task_id": "t1", "text": "a"},
                       {"kind": "fail", "task_id": "t1", "text": "b"})
    assert any("one state action" in error for error in validate_verdict(verdict, CTX))


# -- actions -----------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _runner(**overrides) -> tuple[ActionRunner, SimpleNamespace]:
    tasks = SimpleNamespace(patch_task=AsyncMock(return_value={"task_id": "t1"}),
                            comment_on_ticket=AsyncMock(return_value=True))
    kwargs = dict(task_service=tasks,
                  create_ticket=AsyncMock(return_value={"task_id": "fix-1"}),
                  repo_busy=AsyncMock(return_value=False),
                  open_fix_tickets=AsyncMock(return_value=0),
                  wake_dispatcher=MagicMock(),
                  config={"max_open_fix_tickets": 2, "fix_ticket_autorun": True},
                  clock=lambda: NOW)
    kwargs.update(overrides)
    return ActionRunner(**kwargs), SimpleNamespace(tasks=tasks, **kwargs)


def _apply(runner: ActionRunner, **action: Any) -> dict:
    return asyncio.run(runner.apply(MechanicAction(**action), run_id="MEC-1",
                                    finding_ids=["t1"], diagnosis="causa"))


def test_resume_requeues_and_wakes_the_dispatcher() -> None:
    runner, deps = _runner()
    assert _apply(runner, kind="resume", task_id="t1", text="ok")["ok"]
    updates = deps.tasks.patch_task.await_args.args[1]
    assert updates["status"] == "queued" and updates["params"] == {STREAK_KEY: None}
    deps.wake_dispatcher.assert_called_once()


def test_wait_and_escalate_write_their_reasons() -> None:
    runner, deps = _runner()
    _apply(runner, kind="wait", task_id="t1", minutes=30, text="cupo")
    updates = deps.tasks.patch_task.await_args.args[1]
    assert updates["waiting_reason"] == "mechanic_wait: cupo"
    assert updates["resume_at"] == NOW + timedelta(minutes=30)
    _apply(runner, kind="escalate", task_id="t1", text="renovar el token")
    updates = deps.tasks.patch_task.await_args.args[1]
    assert updates["waiting_reason"] == "owner_action: renovar el token"
    assert "renovar el token" in deps.tasks.comment_on_ticket.await_args.args[1]


def test_fix_ticket_payload_and_open_ticket_cap() -> None:
    runner, deps = _runner()
    out = _apply(runner, kind="fix_ticket", title="Planner sin ESTIMATE",
                 text="evidencia " * 10)
    assert out["ok"]
    payload = deps.create_ticket.await_args.args[0]
    assert payload["title"].startswith("[mecánico] ")
    assert payload["coding_tool"] == "tier_c" and payload["autorun"] is True
    assert payload["params"]["mechanic_fix_for"] == ["t1"]
    assert payload["source_id"].startswith("mechanic:")
    capped, _ = _runner(open_fix_tickets=AsyncMock(return_value=2))
    assert not _apply(capped, kind="fix_ticket", title="Planner sin ESTIMATE",
                      text="evidencia " * 10)["ok"]


def test_git_unstage_touches_only_the_index(repo: Path) -> None:
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "-N", "b.txt")
    runner, _ = _runner()
    assert _apply(runner, kind="git_unstage", repo=str(repo), paths=["b.txt"],
                  text="x")["ok"]
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (repo / "b.txt").exists()


def test_git_stash_is_recoverable_and_refused_while_busy(repo: Path) -> None:
    (repo / "a.txt").write_text("wip\n", encoding="utf-8")
    busy, _ = _runner(repo_busy=AsyncMock(return_value=True))
    assert not _apply(busy, kind="git_stash", repo=str(repo), text="x")["ok"]
    runner, _ = _runner()
    assert _apply(runner, kind="git_stash", repo=str(repo), text="x")["ok"]
    assert "openceo-mechanic MEC-1" in _git(repo, "stash", "list")


def test_index_lock_removed_only_when_stale(repo: Path) -> None:
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    runner, _ = _runner()
    assert not _apply(runner, kind="remove_index_lock", repo=str(repo), text="x")["ok"]
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert _apply(runner, kind="remove_index_lock", repo=str(repo), text="x")["ok"]
    assert not lock.exists()


# -- service -------------------------------------------------------------------------

class _Coll:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.state: dict[str, dict] = {}

    async def create_index(self, *_a, **_k):
        return "at_1"

    async def count_documents(self, query: dict) -> int:
        floor = (query.get("at") or {}).get("$gte")
        return sum(1 for doc in self.docs if floor is None or doc["at"] >= floor)

    async def insert_one(self, doc: dict):
        self.docs.append(doc)

    async def find_one(self, query: dict):
        return self.state.get(query["_id"])

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        doc = self.state.setdefault(query["_id"], {"_id": query["_id"]})
        doc.update(update.get("$set", {}))
        for key, spec in (update.get("$addToSet") or {}).items():
            values = spec["$each"] if isinstance(spec, dict) else [spec]
            doc.setdefault(key, [])
            doc[key] += [value for value in values if value not in doc[key]]

    async def delete_one(self, query: dict):
        self.state.pop(query["_id"], None)


class _Cursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def limit(self, _n: int) -> "_Cursor":
        return self

    def __aiter__(self):
        async def gen():
            for row in self.rows:
                yield row
        return gen()


class _Repo:
    def __init__(self, tasks: list[dict]) -> None:
        self.rows = {task["task_id"]: task for task in tasks}
        self.colls: dict[str, _Coll] = {}
        self.db = self
        self.tasks = SimpleNamespace(count_documents=AsyncMock(return_value=0),
                                     find=lambda _q: _Cursor([]))

    def __getitem__(self, name: str) -> _Coll:
        return self.colls.setdefault(name, _Coll())

    async def get_task(self, task_id: str):
        return self.rows.get(task_id)

    async def list_tasks_by_status(self, status, limit=200):
        return [t for t in self.rows.values() if t["status"] == status.value]

    async def patch_task(self, task_id: str, updates: dict):
        task = self.rows[task_id]
        for key, value in updates.items():
            if key.startswith("params."):
                task.setdefault("params", {})[key[7:]] = value
            else:
                task[key] = value
        return task


def _task(task_id="t1", **over) -> dict:
    task = {"task_id": task_id, "status": "waiting", "autorun": True,
            "project": "p", "waiting_reason": "needs_owner: invalid plan",
            "resume_at": None, "updated_at": NOW - timedelta(hours=1),
            "params": {}}
    task.update(over)
    return task


class _Executor:
    def __init__(self, root: Path, payloads: list[dict]) -> None:
        self.root, self.payloads, self.prompts = root, list(payloads), []

    async def run(self, role, prompt, workdir, timeout_s):
        self.prompts.append(prompt)
        payload = self.payloads.pop(0)
        run_id = prompt.split("RUN: ", 1)[1].split()[0]
        (Path(workdir) / ".openceo" / "MECHANIC_VERDICT.json").write_text(
            json.dumps({**payload, "run_id": run_id}), encoding="utf-8")
        return SimpleNamespace(output_tail="ok")

    async def cleanup(self, role):
        return None


def _service(tmp_path: Path, repo: _Repo, payloads: list[dict], **cfg) -> tuple[MechanicService, SimpleNamespace]:
    async def funnel_patch(task_id: str, updates: dict):
        # Mirrors TaskService.patch_task: params merge, None pops a key.
        updates = dict(updates)
        if isinstance(updates.get("params"), dict):
            merged = dict(repo.rows[task_id].get("params") or {})
            for key, value in updates["params"].items():
                merged.pop(key, None) if value is None else merged.__setitem__(key, value)
            updates["params"] = merged
        return await repo.patch_task(task_id, updates)

    tasks = SimpleNamespace(patch_task=AsyncMock(side_effect=funnel_patch),
                            comment_on_ticket=AsyncMock(return_value=True))
    executor = _Executor(tmp_path, payloads)

    async def situation(findings, *, get_task, repo_for_project, release,
                        tier_s_run=None):
        tickets = []
        for finding in findings:
            task = await get_task(finding.task_id)
            tickets.append({"task_id": task["task_id"], "status": task["status"]})
        return {"tickets": tickets, "repos": {"/repo": {}}}

    service = MechanicService(
        repo=repo, task_service=tasks,
        config_fn=lambda: mechanic_config({"mechanic": cfg}),
        executor_fn=lambda: executor, repo_for_project=lambda _p: "/repo",
        create_ticket=AsyncMock(return_value={"task_id": "fix-1"}),
        wake_dispatcher=MagicMock(), clock=lambda: NOW,
        situation_fn=situation, repo_root=tmp_path)
    return service, SimpleNamespace(tasks=tasks, executor=executor)


def test_heartbeat_finds_engine_give_ups_but_not_human_or_legacy_waits(tmp_path: Path) -> None:
    repo = _Repo([
        _task("give_up"),
        _task("fresh", updated_at=NOW - timedelta(minutes=5)),
        _task("human", waiting_reason="phase:qa_review"),
        _task("legacy", autorun=None),
        _task("old", updated_at=NOW - timedelta(days=30)),
        _task("overdue", resume_at=NOW - timedelta(hours=1),
              waiting_reason="repo_busy: x"),
        _task("snoozed", resume_at=NOW + timedelta(hours=1),
              waiting_reason="mechanic_wait: cupo"),
        _task("stalled", waiting_reason="stalled: 3x repo_busy",
              updated_at=NOW - timedelta(minutes=1)),
        _task("dep_fresh", waiting_reason="blocked_by: x (in_progress)",
              updated_at=NOW - timedelta(hours=3)),
        _task("dep_stuck", waiting_reason="blocked_by: x (converted)",
              updated_at=NOW - timedelta(hours=30)),
        _task("quota_long", waiting_reason="quota_reserved",
              resume_at=NOW + timedelta(minutes=10),
              updated_at=NOW - timedelta(minutes=5),
              params={STREAK_KEY: {"key": "quota_reserved", "count": 9,
                                   "since": (NOW - timedelta(hours=26)).isoformat(),
                                   "last_at": NOW.isoformat()}}),
        _task("retrying", waiting_reason="repo_busy: x",
              resume_at=NOW + timedelta(minutes=10)),
    ])
    service, _ = _service(tmp_path, repo, [])
    found = asyncio.run(service.heartbeat_scan(NOW, service.config()))
    assert {(f.task_id, f.trigger) for f in found} == {
        ("give_up", "stale_wait"), ("overdue", "resume_overdue"),
        ("stalled", "stale_wait"), ("dep_stuck", "long_wait"),
        ("quota_long", "long_wait")}


def test_run_applies_verdict_comments_and_records(tmp_path: Path) -> None:
    repo = _Repo([_task("t1")])
    verdict = {"summary": "plan inválido", "diagnosis": "falta ESTIMATE",
               "actions": [{"kind": "wait", "task_id": "t1", "minutes": 60,
                            "text": "espera al fix"},
                           {"kind": "fix_ticket", "title": "Planner sin ESTIMATE",
                            "text": "evidencia " * 10}]}
    service, deps = _service(tmp_path, repo, [verdict])
    service.wake("t1", "gave_up", "needs_owner: invalid plan")
    asyncio.run(service.tick())
    assert repo.rows["t1"]["waiting_reason"] == "mechanic_wait: espera al fix"
    assert repo.rows["t1"]["params"]["mechanic"]["history"]
    record = repo["mechanic_runs"].docs[0]
    assert [o["ok"] for o in record["outcomes"]] == [True, True]
    assert "falta ESTIMATE" in deps.tasks.comment_on_ticket.await_args.args[1]
    assert "needs_owner: invalid plan" in deps.executor.prompts[0]
    assert '"open_fix_tickets": []' in deps.executor.prompts[0]


def test_invalid_verdict_retries_once_then_reports(tmp_path: Path) -> None:
    repo = _Repo([_task("t1")])
    bad = {"summary": "s", "diagnosis": "d",
           "actions": [{"kind": "resume", "task_id": "nope", "text": "x"}]}
    service, deps = _service(tmp_path, repo, [bad, bad])
    service.wake("t1", "gave_up")
    asyncio.run(service.tick())
    assert len(deps.executor.prompts) == 2
    assert "PREVIOUS VERDICT WAS REJECTED" in deps.executor.prompts[1]
    assert "Sin veredicto válido" in deps.tasks.comment_on_ticket.await_args.args[1]
    assert repo.rows["t1"]["status"] == "waiting"


def test_cooldown_and_ticket_budget_escalate_without_a_model_run(tmp_path: Path) -> None:
    recent = [(NOW - timedelta(hours=h)).isoformat() for h in (5, 4, 3)]
    repo = _Repo([_task("cool", params={"mechanic": {"history": [
                      (NOW - timedelta(minutes=10)).isoformat()]}}),
                  _task("spent", params={"mechanic": {"history": recent}})])
    service, deps = _service(tmp_path, repo, [])
    service.wake("cool", "gave_up")
    asyncio.run(service.tick())
    service.wake("spent", "gave_up")
    asyncio.run(service.tick())
    assert deps.executor.prompts == []
    assert repo.rows["cool"]["waiting_reason"] == "needs_owner: invalid plan"
    assert repo.rows["spent"]["waiting_reason"].startswith("owner_action: el mecánico")


def test_hung_attempt_is_diagnosed_without_state_actions(tmp_path: Path) -> None:
    repo = _Repo([_task("t1", status="in_progress"), _task("t2", status="in_progress")])
    verdict = {"summary": "colgado", "diagnosis": "d",
               "actions": [{"kind": "comment", "task_id": "t1", "text": "mirar"}]}
    service, deps = _service(tmp_path, repo, [verdict, verdict])
    service.wake("t1", "deadline_missed", "attempt hung")
    service.wake("t2", "gave_up")
    asyncio.run(service.tick())
    assert len(deps.executor.prompts) == 1
    assert "t1: deadline_missed" in deps.executor.prompts[0]
    assert "t2:" not in deps.executor.prompts[0]
    bad = validate_verdict(_verdict({"kind": "resume", "task_id": "t1", "text": "x"}),
                           VerdictContext(run_id="MEC-1", task_ids=frozenset({"t1"}),
                                          waiting_ids=frozenset(), repos=frozenset()))
    assert any("only waiting" in error for error in bad)


def test_hourly_budget_holds_runs(tmp_path: Path) -> None:
    repo = _Repo([_task("t1")])
    service, deps = _service(tmp_path, repo, [], max_runs_per_hour=1)
    repo["mechanic_runs"].docs.append({"at": NOW - timedelta(minutes=5)})
    service.wake("t1", "gave_up")
    asyncio.run(service.tick())
    assert deps.executor.prompts == []


def test_fix_done_deploys_when_idle_and_resumes_after_boot(tmp_path: Path) -> None:
    repo = _Repo([_task("t1", waiting_reason="mechanic_wait: fix"),
                  _task("fix-1", status="completed",
                        params={"mechanic_fix_for": ["t1"]})])
    spawn = MagicMock(return_value=True)
    service, deps = _service(tmp_path, repo, [])
    service._deploy_ready = AsyncMock(return_value=True)
    service._spawn_restart = spawn
    service.wake("fix-1", "fix_done")
    asyncio.run(service.tick())
    spawn.assert_called_once()
    assert repo["mechanic_state"].state["deploy"]["resume"] == ["t1"]
    asyncio.run(service._resume_after_deploy())
    assert repo.rows["t1"]["status"] == "queued"
    assert "deploy" not in repo["mechanic_state"].state


def test_failed_fix_wakes_a_new_diagnosis(tmp_path: Path) -> None:
    repo = _Repo([_task("t1", updated_at=NOW - timedelta(minutes=1)),
                  _task("fix-1", status="failed", error="gates red",
                        params={"mechanic_fix_for": ["t1"]})])
    verdict = {"summary": "s", "diagnosis": "d",
               "actions": [{"kind": "escalate", "task_id": "t1", "text": "decidir"}]}
    service, deps = _service(tmp_path, repo, [verdict])
    service.wake("fix-1", "fix_done")
    asyncio.run(service.tick())
    asyncio.run(service.tick())
    assert "fix_failed" in deps.executor.prompts[0]
    assert repo.rows["t1"]["waiting_reason"] == "owner_action: decidir"


def test_disabled_mechanic_ignores_wakes(tmp_path: Path) -> None:
    service, deps = _service(tmp_path, _Repo([_task("t1")]), [], enabled=False)
    service.wake("t1", "gave_up")
    asyncio.run(service.tick())
    assert deps.executor.prompts == []


def test_situation_adds_git_and_tier_s_run_facts(repo: Path) -> None:
    task = _task("t1", coding_tool="tier_s", ticket_number=7)

    async def get_task(_id):
        return task

    async def run_of(_id):
        return {"run_id": "r1", "phase": "planning", "rounds": [1, 2],
                "waiting": {"reason": "needs_owner"}, "metrics": {"x": 1}}

    situation = asyncio.run(compile_situation(
        [Finding("t1", "gave_up")], get_task=get_task,
        repo_for_project=lambda _p: str(repo), logs=lambda _keys: ["line"],
        tier_s_run=run_of))
    ticket = situation["tickets"][0]
    assert ticket["tier_s_run"]["phase"] == "planning"
    assert ticket["tier_s_run"]["rounds"] == 2
    assert situation["repos"][str(repo)]["status"][0].startswith("## main")


def test_prompt_carries_findings_actions_and_situation() -> None:
    text = build_prompt("MEC-9", [Finding("t1", "stall", "repo_busy")],
                        {"tickets": [{"task_id": "t1"}], "repos": {}})
    assert "RUN: MEC-9" in text and "t1: stall" in text
    assert "fix_ticket" in text and "MECHANIC_VERDICT.json" in text


# -- wiring -------------------------------------------------------------------------

def test_deadline_watch_wakes_the_mechanic() -> None:
    woken: list[tuple] = []
    watch = DeadlineWatch(MagicMock(), record=AsyncMock(),
                          on_missed=lambda *args: woken.append(args))
    asyncio.run(watch._store(task_id="t1", attempt_id=None, deadline_type="resume",
                             severity=None, message="late", incident_id="i",
                             now=NOW))
    assert woken == [("t1", "deadline_missed", "late")]


def _loader(tmp: Path) -> ConfigLoader:
    return ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(tmp)))


def test_role_profile_contract_and_badge_are_registered(tmp_path: Path) -> None:
    assert load_role_profile("mechanic").name == "mechanic"
    assert is_read_only("mechanic")
    assert "mechanic" in READONLY_PRINCIPALS
    role = _loader(tmp_path).load_project(None)["mechanic"]["role"]
    assert role["role_profile"] == "mechanic" and role["command"][0] == "musej"


def test_projects_cannot_set_the_mechanic(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "project.yaml").write_text(
        "project:\n  name: x\nmechanic:\n  enabled: false\n", encoding="utf-8")
    errors = _loader(tmp_path).validate_project("x")
    assert any("mechanic must not be set" in error for error in errors)
