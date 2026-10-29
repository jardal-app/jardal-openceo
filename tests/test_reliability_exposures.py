"""Reliability exposures: per-day counters (TASK-2026-00384, v2 §11.1/§13.3).

Targeted tests with fakes: one focused test per counting point plus the
never-throw contract. No network, no CLI, no LLM. Reviewer execution is
stubbed at the live-CLI boundary (the Owner rule forbids live calls);
the exposure hook fires in real code before that boundary.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from reliability import exposures as exposures_mod  # noqa: E402
from reliability.exposures import (  # noqa: E402
    build_exposure_update,
    ensure_exposure_indexes,
    exposure_day_id,
    normalize_project,
    record_exposure,
)
from state import CodingTool, TaskStatus  # noqa: E402


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n")
    (tmp_path / ".gitignore").write_text(".openceo/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


class FakeExposures:
    """Async dict-backed upsert collection for exposure counters."""

    def __init__(self, fail: bool = False) -> None:
        self.docs: dict[str, dict] = {}
        self.indexes: list[tuple] = []
        self.fail = fail

    async def update_one(self, filt: dict, update: dict,
                         upsert: bool = False) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("mongo down")
        key = filt["_id"]
        doc = self.docs.setdefault(key, {"_id": key, "count": 0})
        doc["count"] += int(update.get("$inc", {}).get("count", 0))
        doc.update(update.get("$set", {}))
        for field, value in update.get("$setOnInsert", {}).items():
            doc.setdefault(field, value)
        return SimpleNamespace(matched_count=1)

    async def create_index(self, keys, **options) -> str:
        if self.fail:
            raise RuntimeError("mongo down")
        name = "ttl" if options.get("expireAfterSeconds") == 0 else "idx"
        self.indexes.append((list(keys), dict(options)))
        return name


class FakeIncidents:
    """Minimal async incidents collection for capture paths."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict) -> int:
        return 0


def exposure_store(fail: bool = False, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        db={"reliability_incidents": FakeIncidents(),
            "reliability_exposures": FakeExposures(fail=fail)},
        get_task=AsyncMock(return_value=None),
        patch_task=AsyncMock(return_value={}),
        get_agent_status=AsyncMock(return_value=None),
        set_agent_status=AsyncMock(return_value=None),
        **extra)


def exposures_of(store: SimpleNamespace) -> FakeExposures:
    return store.db["reliability_exposures"]


# ------------------------- unit: id and update -------------------------

def test_day_id_format_and_unknown_project() -> None:
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    assert exposure_day_id("gates", "jardal-openceo", now) == \
        "gates|jardal-openceo|2026-10-29"
    assert normalize_project("") == "unknown"
    assert normalize_project(None) == "unknown"
    assert exposure_day_id("review", None, now) == \
        "review|unknown|2026-10-29"


@run_async
async def test_record_counts_and_reuses_the_day_doc() -> None:
    fake = FakeExposures()
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    assert await record_exposure(fake, "dispatcher", project_key="p1",
                                 now=now) is True
    assert await record_exposure(fake, "dispatcher", project_key="p1",
                                 now=now) is True
    assert len(fake.docs) == 1
    doc = next(iter(fake.docs.values()))
    assert doc["count"] == 2
    assert doc["component"] == "dispatcher"
    assert doc["project"] == "p1"
    assert doc["day"] == "2026-10-29"
    assert doc["schema_version"] == 1
    assert doc["created_at"] <= doc["updated_at"]
    assert doc["expire_at"] - doc["created_at"] == timedelta(days=180)


@run_async
async def test_record_splits_days() -> None:
    fake = FakeExposures()
    day1 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    assert await record_exposure(fake, "core_process", now=day1) is True
    assert await record_exposure(fake, "core_process", now=day2) is True
    assert len(fake.docs) == 2


@run_async
async def test_record_rejects_unknown_component() -> None:
    fake = FakeExposures()
    assert await record_exposure(fake, "nope", now=None) is False
    assert fake.docs == {}


@run_async
async def test_record_skips_none_and_mocks() -> None:
    assert await record_exposure(None, "gates") is False
    assert await record_exposure(MagicMock(), "gates") is False


@run_async
async def test_broken_store_fails_open() -> None:
    assert await record_exposure(
        FakeExposures(fail=True), "business_sync") is False


@run_async
async def test_ensure_exposure_indexes_ttl() -> None:
    fake = FakeExposures()
    assert await ensure_exposure_indexes(fake) == 2
    ttl = [spec for spec in fake.indexes
           if spec[1].get("expireAfterSeconds") == 0]
    assert len(ttl) == 1
    assert ttl[0][0] == [("expire_at", 1)]


@run_async
async def test_ensure_exposure_indexes_fails_open() -> None:
    assert await ensure_exposure_indexes(None) == 0
    assert await ensure_exposure_indexes(FakeExposures(fail=True)) == 0


def test_build_update_matches_socket_shape() -> None:
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    day_id, update = build_exposure_update("runner", "p1", now)
    assert day_id == "runner|p1|2026-10-29"
    assert update["$inc"] == {"count": 1}
    assert update["$set"]["component"] == "runner"


def test_record_converts_non_utc_zone_to_utc_day() -> None:
    from reliability.exposures import _as_utc

    eastern = timezone(timedelta(hours=-5))
    moment = datetime(2026, 10, 29, 20, 0, tzinfo=eastern)
    assert _as_utc(moment) == datetime(2026, 10, 30, 1, 0,
                                      tzinfo=timezone.utc)
    assert exposure_day_id("core_process", "p1", _as_utc(moment)) == \
        "core_process|p1|2026-10-30"


@run_async
async def test_core_process_hook_shape_counts_one() -> None:
    store = exposure_store()
    assert await record_exposure(store, "core_process") is True
    docs = exposures_of(store).docs
    assert len(docs) == 1
    assert next(iter(docs.values()))["component"] == "core_process"


# ------------------------- points: one count per pass -------------------------

@run_async
async def test_dev_session_attempt_counts_one() -> None:
    from agents.dev.session import DevSessionManager

    store = exposure_store()
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="mock"),
                            MagicMock(), repo=None)
    dev._repo = store
    with patch.object(DevSessionManager, "_step_code",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await dev._run_simple_tool(
                {"task_id": "t-exp-1", "project": "p1", "params": {}},
                {}, CodingTool.TIER_B, {})
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("dev_session", "p1", 1)


@run_async
async def test_dev_session_success_counts_one() -> None:
    from agents.dev.session import DevSessionManager

    store = exposure_store()
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="mock"),
                            MagicMock(), repo=None)
    dev._repo = store
    output = await dev._run_simple_tool(
        {"task_id": "t-exp-ok", "project": "p1", "params": {}},
        {}, CodingTool.TIER_B, {})
    assert output.status == TaskStatus.COMPLETED
    docs = exposures_of(store).docs
    assert [(doc["component"], doc["count"])
            for doc in docs.values()] == [("dev_session", 1)]


@run_async
async def test_dev_session_attempt_fails_open_on_broken_store() -> None:
    from agents.dev.session import DevSessionManager

    store = exposure_store(fail=True)
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="mock"),
                            MagicMock(), repo=None)
    dev._repo = store
    with patch.object(DevSessionManager, "_step_code",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await dev._run_simple_tool(
                {"task_id": "t-exp-2", "params": {}},
                {}, CodingTool.TIER_B, {})
    assert exposures_of(store).docs == {}


@run_async
async def test_dev_session_baseline_counts_gates(repo) -> None:
    from agents.dev.session import CodingStepResult, DevSessionManager

    store = exposure_store()
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="cli"),
                            MagicMock(), repo=None)
    dev._repo = store
    config = {"project": {"repos": [{"local_path": str(repo)}]}, "dev": {}}
    waiting = CodingStepResult(
        step={"name": "code", "status": "waiting"}, prompt="", summary="",
        waiting_reason="scheduled_wait: probe",
        resume_at=datetime.now(timezone.utc) + timedelta(hours=2))
    with patch.object(DevSessionManager, "_step_code",
                      new=AsyncMock(return_value=waiting)):
        output = await dev._run_simple_tool(
            {"task_id": "t-exp-b", "project": "p1", "params": {}},
            config, CodingTool.TIER_B, {})
    assert output.status == TaskStatus.WAITING
    docs = exposures_of(store).docs
    assert sorted(doc["component"] for doc in docs.values()) == \
        ["dev_session", "gates"]
    assert all(doc["count"] == 1 for doc in docs.values())


@run_async
async def test_tier_s_run_counts_one(repo) -> None:
    from agents.dev.tier_s.cycle import CycleOutcome, TierSCycle
    from agents.dev.tier_s.stores import (
        InMemoryRunStore,
        InMemoryTaskStore,
    )
    from test_tier_s_brake_376 import make_ctx, make_cycle

    store = exposure_store()
    cycle = make_cycle(InMemoryRunStore(), InMemoryTaskStore(), None,
                       incidents=store)
    outcome = CycleOutcome(status="completed", run_id="run-exp")
    with patch.object(TierSCycle, "_run_guarded",
                      new=AsyncMock(return_value=outcome)):
        result = await cycle.run(
            {"task_id": "t", "ref": "t", "project": "p1", "params": {}},
            repo, {})
    assert result.status == "completed"
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["count"]) == ("tier_s", 1)
    ctx = make_ctx(repo, {})
    assert await cycle._reliability_boundary_inner(
        ctx, "config", "checkpoint has no valid plan", None) is None
    assert len(exposures_of(store).docs) == 1


@run_async
async def test_generic_agent_attempt_counts_one(tmp_path) -> None:
    from orchestration.generic_agent import run_generic_agent
    from test_generic_agent_205 import (
        generic_task,
        hecho_stdout,
        scripted_executor,
        tiny_catalog,
        wiring_with,
    )

    from orchestration.live import ExecResult

    store = exposure_store()
    factory, _calls = scripted_executor(
        ExecResult(0, 1, output_tail=hecho_stdout("listo")))
    wiring = wiring_with(
        tmp_path, tiny_catalog({"operations": {"enabled": True}}), factory,
        repo=store)
    out = await run_generic_agent("operations", generic_task(), wiring)
    assert out["status"] == "completed"
    docs = exposures_of(store).docs
    assert len(docs) == 1
    assert next(iter(docs.values()))["component"] == "generic_agent"


@run_async
async def test_dispatcher_launch_counts_one() -> None:
    from collections import Counter

    from dispatcher import Dispatcher

    store = exposure_store()
    service = SimpleNamespace(
        _task_allows_autorun=lambda task: True,
        blockers_open=AsyncMock(return_value=False),
        patch_task=AsyncMock(return_value={}))
    dispatcher = Dispatcher(repo=store, settings=SimpleNamespace(),
                            service=service,
                            launcher=AsyncMock(return_value=None))
    store.claim_queued_ticket = AsyncMock(
        return_value={"task_id": "t-d-1"})
    outcome = await dispatcher._admit_and_launch(
        {"task_id": "t-d-1", "project": "p1", "coding_tool": "tier_b"},
        [], Counter(), Counter(), datetime.now(timezone.utc), {})
    assert outcome == "launched"
    await asyncio.sleep(0)
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("dispatcher", "p1", 1)


@run_async
async def test_dispatcher_requeue_counts_one() -> None:
    from dispatcher import Dispatcher

    store = exposure_store()
    service = SimpleNamespace(
        _task_allows_autorun=lambda task: True,
        blockers_open=AsyncMock(return_value=False),
        patch_task=AsyncMock(return_value={}))
    dispatcher = Dispatcher(repo=store, settings=SimpleNamespace(),
                            service=service,
                            launcher=AsyncMock(return_value=None))
    store.due_waiting_tasks = AsyncMock(return_value=[
        {"task_id": "t-d-2", "project": "p2", "params": {}}])
    assert await dispatcher._requeue_due_waiting(
        datetime.now(timezone.utc)) == 1
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("dispatcher", "p2", 1)


@run_async
async def test_business_transition_counts_one() -> None:
    from business.work import JardalERPWork

    row_open = {
        "id": "11111111-1111-1111-1111-111111111111",
        "businessReference": "TASK-2026-00384",
        "projectKey": "jardal-openceo",
        "title": "probe",
        "status": "Open",
        "version": 3,
    }

    async def fake_request(method, path, body=None, key=None, params=None):
        if method == "GET":
            return {"task": dict(row_open)}
        assert method == "POST"
        return {"task": {**row_open, "status": "Working"}}

    client = SimpleNamespace(request=fake_request)
    adapter = JardalERPWork.__new__(JardalERPWork)
    adapter.client = client
    from zoneinfo import ZoneInfo

    adapter._zone = ZoneInfo("UTC")
    adapter.on_violation = None
    seen: list[dict] = []
    adapter.on_business_transition = seen.append
    result = await adapter.transition_status("TASK-2026-00384", "Working")
    assert result["ok"] is True
    assert seen == [{"ticket": "TASK-2026-00384", "target": "Working"}]


@run_async
async def test_business_sink_records_with_project() -> None:
    from task_service import TaskService

    store = exposure_store()
    self_stub = SimpleNamespace(
        _repo=store,
        find_task_for_business_ref=AsyncMock(
            return_value={"project": "p9"}))
    await TaskService._business_transition_sink(
        self_stub, {"ticket": "TASK-2026-00384", "target": "Working"})
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("business_sync", "p9", 1)


@run_async
async def test_review_round_counts_one(repo) -> None:
    from agents.dev.session import (
        DevExecutionOutput,
        DevSessionManager,
        _SimpleBranch,
    )

    store = exposure_store()
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="cli"),
                            MagicMock(), repo=None)
    dev._repo = store
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("word " * 40)
    base = git(repo, "rev-parse", "HEAD")
    branch = _SimpleBranch(repo, "tier/exp-r", "main", base, "t")
    project = {"review": {
        "reviewers": [{"label": "r1", "family": "codex"}],
        "review": {}}}
    output = DevExecutionOutput(status=TaskStatus.COMPLETED, params={})
    not_met = SimpleNamespace(outcomes=[], met=False, reviewers=[],
                              verdicts=[], summary=lambda: "stub")
    assembly = SimpleNamespace(
        routing=None, raw_pool=[{"label": "r1", "family": "codex"}])
    with patch("model_routing.review_wiring.assemble_review_pool",
               return_value=assembly), \
        patch("orchestration.review.run_quorum_round",
              new=AsyncMock(return_value=not_met)):
        result = await dev._maybe_simple_review(
            {"task_id": "t-exp-r", "project": "p1"}, project,
            CodingTool.TIER_B, output, branch, [], [], {"gate_retries": 0})
    assert result is not None
    assert result.waiting_reason == "review_no_quorum: stub"
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("review", "p1", 1)


@run_async
async def test_gates_execution_counts_one(repo) -> None:
    from agents.dev.session import (
        DevExecutionOutput,
        DevSessionManager,
        _SimpleBranch,
    )

    store = exposure_store()
    dev = DevSessionManager(SimpleNamespace(DEV_EXECUTION_MODE="cli"),
                            MagicMock(), repo=None)
    dev._repo = store
    base = git(repo, "rev-parse", "HEAD")
    branch = _SimpleBranch(repo, "tier/exp-g", "main", base, "t")
    output = DevExecutionOutput(status=TaskStatus.COMPLETED, params={})
    await dev._run_simple_gates(
        {"task_id": "t-exp-g", "project": "p1"}, {}, CodingTool.TIER_B,
        branch, output, [], {"gate_retries": 0})
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["project"], doc["count"]) == \
        ("gates", "p1", 1)


@run_async
async def test_tier_s_round_helpers_count_one_each() -> None:
    from agents.dev.tier_s.cycle import TierSCycle

    store = exposure_store()
    cycle = TierSCycle.__new__(TierSCycle)
    cycle._incidents = store
    ctx = SimpleNamespace(ticket={"task_id": "t", "project": "p1"})
    await cycle._note_review_exposure(ctx)
    await cycle._note_gates_exposure(ctx)
    docs = exposures_of(store).docs
    assert sorted(doc["component"] for doc in docs.values()) == \
        ["gates", "review"]
    assert all(doc["count"] == 1 for doc in docs.values())


@run_async
async def test_ceo_reviews_count_one_each() -> None:
    from ceo.loop import CEOLoop

    store = exposure_store()
    loop = CEOLoop(repo=store, settings=SimpleNamespace(),
                   mandate_fn=lambda: None,
                   review_runner=AsyncMock(return_value={}))
    store.get_ceo_trigger = AsyncMock(return_value={})
    store.save_ceo_trigger = AsyncMock(return_value={})
    trigger: dict = {}
    await loop._run_scheduled(trigger, reasons=[])
    await loop._run_reactive(trigger, reasons=[])
    docs = exposures_of(store).docs
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert (doc["component"], doc["count"]) == ("ceo_loop", 2)
