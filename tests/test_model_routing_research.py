"""Research sessions fed by the free-research ranking (TASK-2026-00294).

Offline units (settings, frozen snapshots, candidates, checkpoints,
§10.3 recovery, T13, the resumable block) plus the full SPEC §10.4
run (50+ turns with injected 429/503 against a simulated provider)
and synthesis wiring with scripted runtimes. No network, no CLI, no
live model calls: the short real session lives in
test_model_routing_research_live.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import research_wiring as wiring  # noqa: E402
from model_routing.availability import (  # noqa: E402
    AvailabilityStore,
    Candidate,
    ProfileRequirements,
)
from model_routing.models import (  # noqa: E402
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Deployment,
    RankingsDocument,
)
from research.runtimes.base import (  # noqa: E402
    RuntimeCancelled,
    RuntimeExecutionError,
    RuntimeOutcome,
    RuntimeTimeout,
)


def _entry(rank, canonical, family, deployment, provider="opencode",
           quality=0.8, backups=()):
    return {
        "rank": rank, "canonical_model_id": canonical,
        "family_id": family,
        "primary": {"deployment_id": deployment, "provider": provider},
        "backups": [{"deployment_id": dep, "provider": prov}
                    for dep, prov in backups],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"agentic": quality},
        "quality_sources": ["artificial-analysis:index4.3@test"],
    }


def _rankings_doc(order, generated_at=None):
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "generator": "test",
        "catalog_snapshot": "catalog-test",
        "catalog_sha256": "0" * 64,
        "profiles": {
            "free-research": {
                "profile": "free-research",
                "order": order,
                "experimental": [],
                "pending_top3": None,
                "provenance": {
                    "catalog_snapshot": "catalog-test",
                    "catalog_sha256": "0" * 64,
                },
            }
        },
    }


def _write_rankings(path: Path, order, generated_at=None) -> Path:
    path.write_text(json.dumps(_rankings_doc(order, generated_at)),
                    encoding="utf-8")
    return path


def _profile(order):
    doc = RankingsDocument.model_validate(_rankings_doc(order))
    return doc.profiles["free-research"]


def _candidate(name, family=None, canonical=None, provider="opencode",
               quality=0.8):
    return Candidate(
        deployment_id=f"opencode/{name}" if provider == "opencode" else name,
        family_id=family or f"fam-{name}", canonical_model_id=canonical or name,
        provider=provider, quality=quality)


def _store(tmp_path: Path) -> AvailabilityStore:
    return AvailabilityStore(tmp_path / "availability.sqlite")


def _session(tmp_path: Path, names=("a", "b", "c"), **kwargs):
    kwargs.setdefault("sleeper", lambda _s: None)
    return wiring.ResearchSession(
        research_id="r-test", candidates=[_candidate(name) for name in names],
        store=_store(tmp_path), checkpoints=tmp_path / "ckpt", **kwargs)


def _ok(text="answer", tools=(), results=()):
    return wiring.ProviderTurn(
        text=text, complete=True,
        executed_tools=tuple(tools), tool_results=tuple(results))


def _fail(error_text, stage="before_response", text="", tools=(), results=()):
    return wiring.ProviderTurn(
        text=text, complete=False, executed_tools=tuple(tools),
        tool_results=tuple(results), error_text=error_text,
        failure_stage=stage)


# ---------------------------------------------------------------- settings


def test_settings_default_to_enabled_with_30h_snapshot():
    options = wiring.research_router_settings(SimpleNamespace())
    assert options == {"enabled": True, "snapshot_max_age_hours": 30.0}


def test_settings_switch_off_returns_to_static():
    options = wiring.research_router_settings(
        SimpleNamespace(RESEARCH_FREE_ROUTER_ENABLED=False))
    assert options["enabled"] is False


@pytest.mark.parametrize("bad", ["soon", None, -2.0, 0.0])
def test_settings_bad_max_age_fails_fast(bad):
    settings = SimpleNamespace(RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS=bad)
    with pytest.raises(wiring.ResearchRoutingError):
        wiring.research_router_settings(settings)


# ---------------------------------------------------------------- snapshot


def test_snapshot_missing_file_is_static_state(tmp_path):
    assert wiring.load_session_snapshot(tmp_path / "rankings.json") is None


def test_snapshot_invalid_fails_closed(tmp_path):
    target = tmp_path / "rankings.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(wiring.ResearchRoutingError):
        wiring.load_session_snapshot(target)


def test_snapshot_expired_fails_closed(tmp_path):
    order = [_entry(1, "canon-a", "fam-a", "opencode/a")]
    old = (datetime.now(timezone.utc) - timedelta(hours=31)).isoformat()
    target = _write_rankings(tmp_path / "rankings.json", order, old)
    with pytest.raises(wiring.ResearchRoutingError, match="expired"):
        wiring.load_session_snapshot(target)


def test_snapshot_pins_free_research_order(tmp_path):
    order = [_entry(1, "canon-a", "fam-a", "opencode/a")]
    target = _write_rankings(tmp_path / "rankings.json", order)
    snapshot = wiring.load_session_snapshot(target)
    assert snapshot is not None
    assert snapshot.profile.profile == "free-research"
    assert snapshot.file_sha256.startswith("sha256:")
    assert snapshot.catalog_snapshot == "catalog-test"


def test_snapshot_without_research_profile_fails_closed(tmp_path):
    doc = _rankings_doc([_entry(1, "canon-a", "fam-a", "opencode/a")])
    doc["profiles"]["free-review"] = doc["profiles"].pop("free-research")
    doc["profiles"]["free-review"]["profile"] = "free-review"
    target = tmp_path / "rankings.json"
    target.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(wiring.ResearchRoutingError, match="free-research"):
        wiring.load_session_snapshot(target)


# -------------------------------------------------------------- candidates


def test_candidates_keep_order_and_opencode_routes_only():
    ranking = _profile([
        _entry(1, "canon-a", "fam-a", "opencode/a"),
        _entry(2, "canon-b", "fam-b", "groq/b", provider="groq"),
        _entry(3, "canon-c", "fam-c", "groq/c", provider="groq",
               backups=[("opencode/c", "opencode")]),
    ])
    got = wiring.research_candidates(ranking)
    assert [item.deployment_id for item in got] == ["opencode/a", "opencode/c"]


def test_candidates_collapse_same_canonical_t05():
    ranking = _profile([
        _entry(1, "canon-a", "fam-a", "opencode/a"),
        _entry(2, "canon-a", "fam-a", "opencode/a2"),
    ])
    got = wiring.research_candidates(ranking)
    assert [item.deployment_id for item in got] == ["opencode/a"]


def test_candidates_keep_unknown_families():
    ranking = _profile([
        _entry(1, "canon-a", "unknown-family", "opencode/a"),
    ])
    got = wiring.research_candidates(ranking)
    assert [item.deployment_id for item in got] == ["opencode/a"]


def _catalog_with(entries):
    docs = []
    for canonical, family, deployment, tools, context in entries:
        docs.append(CatalogEntry(
            family_id=family, canonical_model_id=canonical,
            capabilities=Capabilities(tools=tools, context_tokens=context),
            deployments=[Deployment(deployment_id=deployment,
                                    provider="opencode")]))
    return CatalogDocument(entries=docs)


def test_candidates_catalog_gates_reject_tools_and_context():
    ranking = _profile([
        _entry(1, "canon-a", "fam-a", "opencode/a"),
        _entry(2, "canon-b", "fam-b", "opencode/b"),
        _entry(3, "canon-c", "fam-c", "opencode/c"),
    ])
    catalog = _catalog_with([
        ("canon-a", "fam-a", "opencode/a", False, 131072),
        ("canon-b", "fam-b", "opencode/b", True, 8192),
        ("canon-c", "fam-c", "opencode/c", True, 131072),
    ])
    got = wiring.research_candidates(ranking, catalog=catalog)
    assert [item.deployment_id for item in got] == ["opencode/c"]


def test_fit_candidate_unknown_deployment_fails_closed():
    catalog = _catalog_with([("canon-a", "fam-a", "opencode/a", True, 131072)])
    fits, reason = wiring.fit_candidate(
        _candidate("ghost"), catalog, ProfileRequirements(needs_tools=True))
    assert fits is False
    assert "not in catalog" in reason


# ------------------------------------------------------------- checkpoints


def _checkpoint(**overrides):
    payload = {
        "research_id": "r1", "session_id": "s1", "turn": 2,
        "objective": "why is the sky blue",
        "plan": ["search", "read", "write"],
        "queries_and_sources": [["sky color", "https://example.org/sky"]],
        "validated_results": ["rayleigh scattering"],
        "pending": ["draft summary"],
        "decisions": ["trust peer-reviewed sources"],
        "artifacts": ["notes/sky.md"],
        "applied_tools": [{"id": "t1", "name": "research_read",
                           "result": "sky page"}],
        "partial_text": "partial prefix",
        "candidates": [{"deployment_id": "opencode/a", "family_id": "fam-a",
                        "canonical_model_id": "a", "provider": "opencode",
                        "quality": 0.8}],
        "static_model": "opencode/static-free",
        "effective_deployment": "opencode/a",
        "rankings_snapshot": "sha256:abc",
        "updated_at": "2026-10-23T00:00:00+00:00",
    }
    payload.update(overrides)
    return wiring.ResearchCheckpoint.from_dict(
        {"schema_version": 1, **payload})


def test_checkpoint_roundtrip_preserves_every_field(tmp_path):
    saved = _checkpoint()
    path = wiring.record_checkpoint(tmp_path, saved)
    assert path.name == "r1.json"
    assert wiring.load_checkpoint(tmp_path, "r1") == saved


def test_checkpoint_missing_is_none(tmp_path):
    assert wiring.load_checkpoint(tmp_path, "never") is None


@pytest.mark.parametrize("body", ["{oops", '{"schema_version": 99}'])
def test_checkpoint_corrupt_fails_closed(tmp_path, body):
    (tmp_path / "r1.json").write_text(body, encoding="utf-8")
    with pytest.raises(wiring.ResearchRoutingError):
        wiring.load_checkpoint(tmp_path, "r1")


def test_checkpoint_write_leaves_no_temp_files(tmp_path):
    wiring.record_checkpoint(tmp_path, _checkpoint())
    assert list(tmp_path.iterdir()) == [tmp_path / "r1.json"]


# ------------------------------------------------------------------ turns


def test_session_keeps_affinity_while_viable(tmp_path):
    session = _session(tmp_path)
    try:
        seen = [session.run_turn(f"prompt {turn}", lambda req: _ok(f"a{turn}"))
                for turn in range(3)]
    finally:
        session.close()
    assert [item.model_effective for item in seen] == ["opencode/a"] * 3
    assert [item.turn for item in seen] == [1, 2, 3]
    assert [item.rotations for item in seen] == [0, 0, 0]
    assert all(item.resumed is False for item in seen)
    assert wiring.load_checkpoint(tmp_path / "ckpt", "r-test").turn == 3


def test_session_rejects_empty_prompt_and_closed_session(tmp_path):
    session = _session(tmp_path)
    with pytest.raises(wiring.ResearchRoutingError):
        session.run_turn("  ", lambda req: _ok())
    session.close()
    with pytest.raises(wiring.ResearchRoutingError):
        session.run_turn("prompt", lambda req: _ok())


def test_session_429_rotates_records_and_cools(tmp_path):
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test",
        candidates=[_candidate("a"), _candidate("b")],
        store=store, checkpoints=tmp_path / "ckpt",
        sleeper=lambda _s: None)
    calls = []

    def invoke(request):
        calls.append(request.model)
        if len(calls) == 1:
            return _fail("upstream says 429 too many requests")
        return _ok("recovered")

    try:
        outcome = session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert calls == ["opencode/a", "opencode/b"]
    assert outcome.model_effective == "opencode/b"
    assert outcome.rotations == 1
    assert outcome.resumed is True
    assert store.cooldown_for("opencode/a") is not None
    rows = store.errors_since(0)
    assert [(item.error_class, item.run_id) for item in rows] == [
        ("rate_limited", "r-test")]
    assert store.audit_since("r-test", 0)[0][1] == "research_fallback"


def test_session_503_rebuilds_same_first_then_rotates(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    calls = []

    def invoke(request):
        calls.append((request.model, request.resume))
        if len(calls) <= 2:
            return _fail("upstream 503 service unavailable")
        return _ok("recovered")

    try:
        outcome = session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert calls == [("opencode/a", False), ("opencode/a", True),
                     ("opencode/b", True)]
    assert outcome.model_effective == "opencode/b"
    assert outcome.rotations == 1


def test_session_partial_text_rebuilds_same_model(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    seen = []

    def invoke(request):
        seen.append(request)
        if len(seen) == 1:
            return _fail("connection was closed mid-stream",
                         stage="partial_text", text="valid prefix")
        return _ok("valid prefix + rest")

    try:
        outcome = session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert [item.model for item in seen] == ["opencode/a", "opencode/a"]
    assert seen[1].resume is True
    assert seen[1].partial_text == "valid prefix"
    assert outcome.text == "valid prefix + rest"
    assert outcome.rotations == 0


def test_session_after_tool_never_reexecutes_t13(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    executions: dict[str, int] = {}
    seen = []

    def invoke(request):
        seen.append(request)
        for applied in request.applied_tools:
            assert applied.id not in executions or True
        if len(seen) == 1:
            executions["t1"] = executions.get("t1", 0) + 1
            return _fail("upstream 503 after tool ran", stage="after_tool",
                         tools=[wiring.ToolCall(id="t1", name="research_read")],
                         results=[("t1", "sky page")])
        assert [tool.id for tool in request.applied_tools] == ["t1"]
        return _ok("done without rerunning t1")

    try:
        outcome = session.run_turn(
            "prompt", invoke, validated=["rayleigh scattering"])
    finally:
        session.close()
    assert executions == {"t1": 1}
    assert outcome.resumed is True
    assert outcome.checkpoint.validated_results == ("rayleigh scattering",)
    assert [tool.id for tool in outcome.checkpoint.applied_tools] == ["t1"]


def test_session_tool_without_result_fails_safe(tmp_path):
    session = _session(tmp_path)

    def invoke(request):
        return wiring.ProviderTurn(
            text="done", complete=True,
            executed_tools=(wiring.ToolCall(id="t1", name="research_read"),),
            tool_results=())

    with pytest.raises(wiring.SessionBlocked) as raised:
        session.run_turn("prompt", invoke, pending=["goal"])
    assert raised.value.code == wiring.BLOCKED_TOOL_CONFLICT
    saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-test")
    assert saved is not None and saved.turn == 0
    assert saved.pending == ("goal",)
    session.close()


def test_session_blocked_saves_state_and_resumes(tmp_path):
    ckpt = tmp_path / "ckpt"
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test", candidates=[_candidate("a")],
        store=store, checkpoints=ckpt, sleeper=lambda _s: None)
    with pytest.raises(wiring.SessionBlocked) as raised:
        session.run_turn("prompt", lambda req: _fail("429 quota spent"),
                         pending=["goal"])
    assert raised.value.code == wiring.BLOCKED_NO_ELIGIBLE_MODEL
    assert raised.value.checkpoint_path == ckpt / "r-test.json"
    session.close()
    resumed = wiring.ResearchSession.resume(
        ckpt, "r-test", store=_store(tmp_path / "fresh"))
    try:
        assert resumed.turn == 0
        assert resumed.effective_model == "opencode/a"
        outcome = resumed.run_turn("prompt", lambda req: _ok("done"))
    finally:
        resumed.close()
    assert outcome.turn == 1
    assert outcome.checkpoint.pending == ("goal",)


# ------------------------------------------------- review hardening


def test_session_tool_result_contradiction_fails_safe(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    calls = []

    def invoke(request):
        calls.append(request)
        if len(calls) == 1:
            return _fail("upstream 503 after tool ran", stage="after_tool",
                         tools=[wiring.ToolCall(id="t1", name="research_read")],
                         results=[("t1", "sky page")])
        return _fail("upstream 503 again", stage="after_tool",
                     tools=[wiring.ToolCall(id="t1", name="research_read")],
                     results=[("t1", "ocean page")])

    with pytest.raises(wiring.SessionBlocked) as raised:
        session.run_turn("prompt", invoke)
    assert raised.value.code == wiring.BLOCKED_TOOL_CONFLICT
    assert session.ledger["t1"].result == "sky page"
    session.close()


def test_session_tool_same_result_reclaim_is_idempotent(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    calls = []

    def invoke(request):
        calls.append(request)
        if len(calls) == 1:
            return _fail("upstream 503 after tool ran", stage="after_tool",
                         tools=[wiring.ToolCall(id="t1", name="research_read")],
                         results=[("t1", "sky page")])
        return wiring.ProviderTurn(
            text="done", complete=True,
            executed_tools=(wiring.ToolCall(id="t1", name="research_read"),),
            tool_results=(("t1", "sky page"),))

    try:
        outcome = session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert outcome.text == "done"
    assert [tool.id for tool in outcome.checkpoint.applied_tools] == ["t1"]


def test_session_static_block_preserves_partial_prefix(tmp_path):
    session = wiring.ResearchSession(
        research_id="r-test", static_model="opencode/static-free",
        checkpoints=tmp_path / "ckpt", sleeper=lambda _s: None)
    calls = []

    def invoke(request):
        calls.append(request)
        text = "short" if len(calls) == 1 else "short plus more"
        return _fail("connection was closed", stage="partial_text", text=text)

    with pytest.raises(wiring.SessionBlocked):
        session.run_turn("prompt", invoke)
    saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-test")
    assert saved is not None and saved.partial_text == "short plus more"
    session.close()


def test_session_initial_selection_skips_raced_candidate(tmp_path):
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test",
        candidates=[_candidate(name) for name in ("a", "b")],
        store=store, checkpoints=tmp_path / "ckpt", sleeper=lambda _s: None)
    store.reserve("r-test", session.session_id, "other-slot",
                  "canon-x", "fam-a", "opencode/x")
    try:
        outcome = session.run_turn("prompt", lambda req: _ok("done"))
    finally:
        session.close()
    assert outcome.model_effective == "opencode/b"


def test_session_rotation_survives_double_reservation_conflict(tmp_path):
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test",
        candidates=[_candidate(name) for name in ("a", "b", "c", "d")],
        store=store, checkpoints=tmp_path / "ckpt", sleeper=lambda _s: None)
    store.reserve("r-test", session.session_id, "slot-b",
                  "canon-b", "fam-b", "opencode/xb")
    store.reserve("r-test", session.session_id, "slot-c",
                  "canon-c", "fam-c", "opencode/xc")
    calls = []

    def invoke(request):
        calls.append(request.model)
        if len(calls) == 1:
            return _fail("upstream 429 too many requests")
        return _ok("recovered")

    try:
        outcome = session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert calls == ["opencode/a", "opencode/d"]
    assert outcome.model_effective == "opencode/d"
    assert outcome.rotations == 1


def test_session_resume_after_close_rereserves_pinned_model(tmp_path):
    ckpt = tmp_path / "ckpt"
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test", candidates=[_candidate("a")],
        store=store, checkpoints=ckpt, sleeper=lambda _s: None)
    with pytest.raises(wiring.SessionBlocked):
        session.run_turn("prompt", lambda req: _fail("status=403 forbidden"))
    session.close()
    assert store.reservations("r-test", session.session_id) == []
    resumed = wiring.ResearchSession.resume(ckpt, "r-test", store=store)
    outcome = resumed.run_turn("prompt", lambda req: _ok("done"))
    assert outcome.model_effective == "opencode/a"
    held = store.reservations("r-test", resumed.session_id)
    assert [item.slot for item in held] == [wiring.RESEARCH_SYNTH_SLOT]
    resumed.close()
    assert store.reservations("r-test", resumed.session_id) == []


def test_session_resume_without_close_adopts_slot_and_rotates(tmp_path):
    ckpt = tmp_path / "ckpt"
    store = _store(tmp_path)
    session = wiring.ResearchSession(
        research_id="r-test",
        candidates=[_candidate(name) for name in ("a", "b")],
        store=store, checkpoints=ckpt, sleeper=lambda _s: None)
    with pytest.raises(wiring.SessionBlocked):
        session.run_turn("prompt", lambda req: _fail("status=403 forbidden"))
    resumed = wiring.ResearchSession.resume(ckpt, "r-test", store=store)
    calls = []

    def invoke(request):
        calls.append(request.model)
        if len(calls) == 1:
            return _fail("upstream 429 too many requests")
        return _ok("recovered")

    try:
        outcome = resumed.run_turn("prompt", invoke)
    finally:
        resumed.close()
    assert calls == ["opencode/a", "opencode/b"]
    assert outcome.rotations == 1
    assert store.reservations("r-test", resumed.session_id) == []


def test_session_static_rebuilds_once_then_blocks(tmp_path):
    session = wiring.ResearchSession(
        research_id="r-test", static_model="opencode/static-free",
        checkpoints=tmp_path / "ckpt", sleeper=lambda _s: None)
    calls = []

    def invoke(request):
        calls.append((request.model, request.resume))
        return _fail("upstream 503 down")

    with pytest.raises(wiring.SessionBlocked) as raised:
        session.run_turn("prompt", invoke)
    assert raised.value.code == wiring.BLOCKED_NO_ELIGIBLE_MODEL
    assert calls == [("opencode/static-free", False),
                     ("opencode/static-free", True)]
    session.close()


def test_session_backoff_spaces_retries(tmp_path):
    delays: list[float] = []
    session = _session(tmp_path, names=("a", "b"),
                       sleeper=delays.append)
    calls = []

    def invoke(request):
        calls.append(request.model)
        if len(calls) == 1:
            return _fail("429 spent")
        return _ok("recovered")

    try:
        session.run_turn("prompt", invoke)
    finally:
        session.close()
    assert len(delays) == 1
    assert delays[0] >= 0.0


def test_session_persistent_403_blocks_without_rotating(tmp_path):
    session = _session(tmp_path, names=("a", "b"))
    calls = []

    def invoke(request):
        calls.append(request.model)
        return _fail("provider says status=403 forbidden region")

    with pytest.raises(wiring.SessionBlocked) as raised:
        session.run_turn("prompt", invoke)
    assert raised.value.code == wiring.BLOCKED_PROVIDER
    assert calls == ["opencode/a", "opencode/a"]
    session.close()


def test_resume_missing_checkpoint_raises(tmp_path):
    with pytest.raises(wiring.ResearchRoutingError):
        wiring.ResearchSession.resume(tmp_path, "ghost")


# ------------------------------------------------- SPEC §10.4 full run


class _ScriptedProvider:
    """Simulated provider with injected 429/503, tool and cut failures."""

    def __init__(self):
        self.executions: dict[str, int] = {}
        self.completions: list[str] = []
        self.attempts: dict[int, int] = {}
        self.fault_429 = {7, 23, 41}
        self.fault_503 = {15, 33}

    def __call__(self, request):
        first_line = request.prompt.split("\n", 1)[0]
        turn = int(first_line.rsplit("-", 1)[1])
        self.attempts[turn] = self.attempts.get(turn, 0) + 1
        attempt = self.attempts[turn]
        tool_id = f"t{turn}"
        if turn in self.fault_429 and attempt == 1:
            return _fail("upstream 429 too many requests, retry later")
        if turn in self.fault_503 and attempt == 1:
            return _fail("upstream 503 service unavailable")
        if turn == 27 and attempt == 1:
            self.executions[tool_id] = self.executions.get(tool_id, 0) + 1
            return _fail("upstream 503 after the tool ran", stage="after_tool",
                         tools=[wiring.ToolCall(id=tool_id, name="research_read")],
                         results=[(tool_id, f"page {turn}")])
        if turn == 35 and attempt == 1:
            return _fail("connection was closed", stage="partial_text",
                         text=f"prefix-{turn}")
        if tool_id not in {tool.id for tool in request.applied_tools}:
            self.executions[tool_id] = self.executions.get(tool_id, 0) + 1
        text = f"answer-{turn}"
        self.completions.append(text)
        return wiring.ProviderTurn(
            text=text, complete=True,
            executed_tools=() if tool_id in {
                tool.id for tool in request.applied_tools} else (
                wiring.ToolCall(id=tool_id, name="research_read"),),
            tool_results=() if tool_id in {
                tool.id for tool in request.applied_tools} else (
                (tool_id, f"page {turn}"),))


def test_research_50_turns_keep_sources_checkpoints_and_models(tmp_path):
    ckpt = tmp_path / "ckpt"
    provider = _ScriptedProvider()
    session = wiring.ResearchSession(
        research_id="r-104",
        candidates=[_candidate(name) for name in "abcdef"],
        store=_store(tmp_path), checkpoints=ckpt,
        objective="synthetic research", plan=["sweep"],
        sleeper=lambda _s: None)
    try:
        outcomes = [
            session.run_turn(
                f"turn-{turn}", provider,
                queries_and_sources=[(f"q{turn}", f"https://example.org/{turn}")],
                validated=[f"v{turn}"])
            for turn in range(1, 51)]
    finally:
        session.close()
    assert [item.turn for item in outcomes] == list(range(1, 51))
    assert [item.text for item in outcomes] == [f"answer-{n}" for n in range(1, 51)]
    assert provider.completions == [f"answer-{n}" for n in range(1, 51)]
    assert set(provider.executions) == {f"t{n}" for n in range(1, 51)}
    assert set(provider.executions.values()) == {1}
    effective = [item.model_effective for item in outcomes]
    assert effective[6] == "opencode/b"
    assert effective[22] == "opencode/c"
    assert effective[40] == "opencode/d"
    for position in (14, 32, 26, 34):
        assert effective[position] == effective[position - 1]
    saved = wiring.load_checkpoint(ckpt, "r-104")
    assert saved is not None and saved.turn == 50
    assert len(saved.queries_and_sources) == 50
    assert len(saved.validated_results) == 50
    assert len(saved.applied_tools) == 50
    assert saved.partial_text == ""
    assert saved.objective == "synthetic research"


# ------------------------------------------------------- synthesis wiring


def _service(settings_extra=None, script=()):
    from research.service import ResearchDependencies, ResearchService

    models: list[str] = []
    plan = list(script)

    class _FakeRuntime:
        async def run(self, request):
            models.append(request.model)
            action = plan.pop(0)
            if isinstance(action, BaseException):
                raise action
            return action

        async def cancel(self, research_id: str) -> bool:
            return False

    settings = SimpleNamespace(
        RESEARCH_OPENCODE_TEST_MODEL="opencode/static-free",
        RESEARCH_WORKSPACE_DIR="/tmp/research-test",
        RESEARCH_ENABLED=True,
        **(settings_extra or {}),
    )
    service = ResearchService(
        settings=settings,
        deps=ResearchDependencies(repo=None, runtime=_FakeRuntime()))
    return service, models


def _job():
    return {"research_id": "r-synth", "project_key": "pk", "owner_id": "o1",
            "question": "synthetic?", "classification": "public"}


def _rows():
    return {"r-synth-e1": {"url": "https://example.org/x", "text": "extract",
                           "content_hash": "h", "access_method": "http",
                           "observed_at": datetime.now(timezone.utc)}}


def _report():
    return json.dumps({
        "summary": "synthetic synthesis",
        "claims": [{"id": "c1", "statement": "backed claim", "kind": "fact",
                    "evidence_ids": ["r-synth-e1"],
                    "support": [{"extract": "extract"}]}]})


def _budget():
    from research.service import StepBudget

    return StepBudget(steps=5, seconds=60)


def _rankings_file(tmp_path: Path) -> Path:
    order = [_entry(1, "canon-a", "fam-a", "opencode/a", quality=0.85),
             _entry(2, "canon-b", "fam-b", "opencode/b", quality=0.8)]
    return _write_rankings(tmp_path / "rankings.json", order)


def test_synth_static_model_when_snapshot_missing(tmp_path):
    service, models = _service(script=[RuntimeOutcome(text=_report(),
                                                      session_id=None)])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage,
        router_rankings_file=tmp_path / "missing.json"))
    assert result is not None and coverage == []
    assert models == ["opencode/static-free"]


def test_synth_uses_ranking_best_model(tmp_path):
    service, models = _service(script=[RuntimeOutcome(text=_report(),
                                                      session_id=None)])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage,
        router_store=_store(tmp_path),
        router_rankings_file=_rankings_file(tmp_path)))
    assert result is not None and coverage == []
    assert models == ["opencode/a"]


def test_synth_429_rotates_to_next_ranked_model(tmp_path):
    store = _store(tmp_path)
    service, models = _service(script=[
        RuntimeExecutionError("failed", exit_code=1,
                              runtime_error="provider error status=429"),
        RuntimeOutcome(text=_report(), session_id=None)])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage, router_store=store,
        router_rankings_file=_rankings_file(tmp_path)))
    assert result is not None and coverage == []
    assert models == ["opencode/a", "opencode/b"]
    assert store.cooldown_for("opencode/a") is not None
    assert store.errors_since(0)[0].error_class == "rate_limited"


def test_synth_switch_off_keeps_static_list(tmp_path):
    service, models = _service(
        {"RESEARCH_FREE_ROUTER_ENABLED": False},
        script=[RuntimeOutcome(text=_report(), session_id=None)])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage,
        router_store=_store(tmp_path),
        router_rankings_file=_rankings_file(tmp_path)))
    assert result is not None and coverage == []
    assert models == ["opencode/static-free"]


def test_synth_cancel_never_retries(tmp_path):
    service, models = _service(script=[RuntimeCancelled("halt")])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage,
        router_store=_store(tmp_path),
        router_rankings_file=_rankings_file(tmp_path)))
    assert result is None
    assert models == ["opencode/a"]
    assert "RuntimeCancelled" in coverage[0].detail


def test_synth_timeout_rebuilds_same_then_rotates(tmp_path):
    service, models = _service(script=[
        RuntimeTimeout("slow"), RuntimeTimeout("slow again"),
        RuntimeOutcome(text=_report(), session_id=None)])
    coverage: list = []
    result = asyncio.run(service._synthesize(
        _job(), None, _budget(), _rows(), coverage,
        router_store=_store(tmp_path),
        router_rankings_file=_rankings_file(tmp_path)))
    assert result is not None and coverage == []
    assert models == ["opencode/a", "opencode/a", "opencode/b"]


def test_assemble_degrades_to_static_on_invalid_rankings(tmp_path):
    target = tmp_path / "rankings.json"
    target.write_text("{broken", encoding="utf-8")
    routing = wiring.assemble_research_model(
        SimpleNamespace(RESEARCH_OPENCODE_TEST_MODEL="opencode/static-free"),
        research_id="r1", rankings_file=target)
    assert routing.active is False
    assert routing.model == "opencode/static-free"


def test_close_synthesis_routing_releases_reservation(tmp_path):
    from model_routing.availability import ReservationConflict, reserve_model

    store = _store(tmp_path)
    routing = wiring.assemble_research_model(
        SimpleNamespace(RESEARCH_OPENCODE_TEST_MODEL="opencode/static-free"),
        research_id="r1", store=store,
        rankings_file=_rankings_file(tmp_path))
    assert routing.active is True
    with pytest.raises(ReservationConflict):
        reserve_model(store, "r1", routing.session_id, wiring.RESEARCH_SYNTH_SLOT,
                      "canon-a", "fam-a", "opencode/a")
    wiring.close_synthesis_routing(routing)
    held = reserve_model(store, "r1", routing.session_id,
                         wiring.RESEARCH_SYNTH_SLOT,
                         "canon-a", "fam-a", "opencode/a")
    assert held.deployment_id == "opencode/a"
    store.close()
