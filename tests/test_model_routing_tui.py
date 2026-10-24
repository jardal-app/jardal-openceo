"""Interactive launcher over managed OpenCode sessions (TASK-2026-00308).

Offline proof with a simulated OpenCode server: the launcher
prepares a managed session on the profile's best target and prints
the attach command (§11.9 setup), the controller observes a
simulated failure in that session through the messages API, and the
next turn falls back to the next eligible target without overriding
a manual switch. Message shapes come from the v2.0.22 contract
verified with zero tokens on 2026-10-24 (failure ``idle/failed``
markers, ``model-switched`` drift records). No network, no CLI, no
live model calls: the short real launcher session lives in
test_model_routing_tui_live.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import research_wiring as wiring  # noqa: E402
from model_routing import tui as launcher  # noqa: E402
from model_routing.availability import AvailabilityStore  # noqa: E402
from model_routing.cli import main as cli_main  # noqa: E402
from model_routing.models import (  # noqa: E402
    CatalogDocument,
    RankingsDocument,
)

FAKE_SECRETS = {
    "FREE_ROUTER_OPENROUTER_API_KEY": "TEST-ONLY-FAKE-OPENROUTER",
}

CANARY = "tui-canary-118-never-in-audit"


def _deployment(deployment_id, provider, route, provider_id, model_id):
    allowed = ("opencode_only" if route == "opencode_native_free"
               else "configured_opencode_or_gateway")
    return {
        "deployment_id": deployment_id, "provider": provider,
        "route_type": route, "allowed_client": allowed,
        "provider_id": provider_id, "model_id": model_id,
        "gateway_model_id": model_id,
        "free_offered": True, "free_verified": True, "healthy": True,
        "input_price_usd": 0.0, "output_price_usd": 0.0,
        "approved_confidentiality": ["public"],
        "source": "tui-test",
    }


def _catalog_entry(family, canonical, display, deployment, *, tools=True,
                   context=131072):
    return {
        "family_id": family, "canonical_model_id": canonical,
        "display_name": display,
        "capabilities": {"tools": tools, "streaming": False,
                         "context_tokens": context},
        "deployments": [deployment],
    }


def _catalog_doc(entries):
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "tui-test",
        "entries": entries,
    }


def _rank_entry(rank, canonical, family, deployment_id, provider, quality):
    return {
        "rank": rank, "canonical_model_id": canonical,
        "family_id": family,
        "primary": {"deployment_id": deployment_id, "provider": provider},
        "backups": [],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"agentic": quality},
        "quality_sources": ["synthetic-fixture"],
    }


def _rankings_doc(order):
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "tui-test",
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


def _standard_order():
    return [
        _rank_entry(1, "org/alpha", "alpha", "opencode/alpha-free",
                    "opencode", 0.90),
        _rank_entry(2, "org/beta", "beta", "opencode/beta-free",
                    "opencode", 0.88),
        _rank_entry(3, "acme/gamma", "gamma",
                    "openrouter/acme/gamma:free", "openrouter", 0.85),
    ]


def _standard_entries():
    return [
        _catalog_entry("alpha", "org/alpha", "Alpha Free", _deployment(
            "opencode/alpha-free", "opencode", "opencode_native_free",
            "opencode", "alpha-free")),
        _catalog_entry("beta", "org/beta", "Beta Free", _deployment(
            "opencode/beta-free", "opencode", "opencode_native_free",
            "opencode", "beta-free")),
        _catalog_entry("gamma", "acme/gamma", "Gamma Free", _deployment(
            "openrouter/acme/gamma:free", "openrouter", "external_free",
            "openrouter", "acme/gamma:free")),
    ]


def _snapshot(order):
    document = RankingsDocument.model_validate(_rankings_doc(order))
    return wiring.SessionSnapshot(
        document=document, profile=document.profiles["free-research"],
        file_sha256="sha256:test", catalog_snapshot="catalog-test",
        generated_at=datetime.now(timezone.utc).isoformat())


def _catalog(entries):
    return CatalogDocument.model_validate(_catalog_doc(entries))


def _targets(tmp_path, *, credentials=None):
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        return wiring.native_research_targets(
            _snapshot(_standard_order()), _catalog(_standard_entries()),
            store, credentials=dict(credentials or {}),
            run_id="r-tui-test")
    finally:
        store.close()


def _message(message_id, msg_type, *, outcome="", model=None, text="",
             created=1792865860299):
    payload: dict = {"id": message_id, "time": {"created": created},
                     "type": msg_type}
    if outcome:
        payload["outcome"] = outcome
    if model is not None:
        payload["model"] = model
    if text:
        payload["text"] = text
    return payload


class FakeApi:
    """In-memory OpenCode session server with message history."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sessions: dict[str, dict] = {}
        self.counter = 0
        self.get_override: dict[str, str] = {}

    def __call__(self, argv: list[str], cwd: str, timeout: float):
        self.calls.append(list(argv))
        assert argv[1:3] == ["api", "--standalone"], argv
        assert "--server" not in argv, argv
        method, path = argv[3], argv[4]
        body = json.loads(argv[argv.index("-d") + 1]) if "-d" in argv else None
        if method == "POST" and path == "/api/session":
            return self._created(argv)
        if path.startswith("/api/session/") and path.endswith("/model"):
            return self._switched(argv, path, body)
        if path.startswith("/api/session/") and path.endswith("/message"):
            session_id = path.removeprefix("/api/session/").removesuffix(
                "/message")
            return self._messaged(argv, session_id)
        if path.startswith("/api/session/"):
            session_id = path.removeprefix("/api/session/")
            if method == "GET":
                return self._got(argv, session_id)
            if method == "DELETE":
                return self._deleted(argv, session_id)
        raise AssertionError(f"unexpected call: {argv}")

    def _created(self, argv):
        self.counter += 1
        session_id = f"ses_fake{self.counter:04d}"
        self.sessions[session_id] = {"model": None, "messages": []}
        return wiring.ApiRun(
            args=tuple(argv), returncode=0, stderr="",
            stdout=json.dumps({"data": {"id": session_id}}))

    def _switched(self, argv, path, body):
        session_id = path.removeprefix("/api/session/").removesuffix("/model")
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        model = (body or {}).get("model") or {}
        if not model.get("providerID") or not model.get("id"):
            return self._failed(argv, 400, "InvalidRequestError",
                                "Missing key")
        self.sessions[session_id]["model"] = {
            "providerID": model["providerID"], "id": model["id"]}
        self.sessions[session_id]["messages"].insert(0, _message(
            f"msg_switch{len(self.calls):04d}", "model-switched",
            model={"providerID": model["providerID"], "id": model["id"]}))
        return wiring.ApiRun(args=tuple(argv), returncode=0,
                             stdout="", stderr="")

    def _got(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        model = self.sessions[session_id]["model"]
        if session_id in self.get_override:
            provider_id, model_id = self.get_override[session_id].split(
                "/", 1)
            model = {"providerID": provider_id, "id": model_id}
        data: dict = {"id": session_id, "cost": 0,
                      "tokens": {"input": 3, "output": 7}}
        if model is not None:
            data["model"] = dict(model)
        return wiring.ApiRun(args=tuple(argv), returncode=0, stderr="",
                             stdout=json.dumps({"data": data}))

    def _messaged(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        return wiring.ApiRun(
            args=tuple(argv), returncode=0, stderr="",
            stdout=json.dumps(
                {"data": list(self.sessions[session_id]["messages"])}))

    def _deleted(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        del self.sessions[session_id]
        return wiring.ApiRun(args=tuple(argv), returncode=0,
                             stdout="", stderr="")

    @staticmethod
    def _failed(argv, status, tag, detail):
        return wiring.ApiRun(
            args=tuple(argv), returncode=1,
            stdout=json.dumps({"_tag": tag, "message": detail}),
            stderr=f"HTTP {status} {tag}\n")


def _client(fake: FakeApi) -> wiring.SessionClient:
    return wiring.SessionClient(runner=fake, timeout=5.0)


def test_prepare_sets_profile_model_and_attach_argv(tmp_path):
    fake = FakeApi()
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=_client(fake), project_dir=project, targets=targets)
    assert plan.profile == "free-research"
    assert plan.target.model_arg() == "opencode/alpha-free"
    assert plan.argv == ("opencode", "--session", plan.session_id,
                        str(project))
    assert "free-research" in plan.audit
    assert "opencode/alpha-free" in plan.audit
    methods = [(argv[3], argv[4]) for argv in fake.calls]
    assert methods[0] == ("POST", "/api/session")
    assert methods[1][0] == "POST" and methods[1][1].endswith("/model")
    assert methods[2][0] == "GET"


def test_prepare_refuses_without_targets(tmp_path):
    fake = FakeApi()
    with pytest.raises(wiring.ResearchRoutingError):
        launcher.prepare_tui_session(
            client=_client(fake), project_dir=tmp_path, targets=[])
    assert fake.calls == []


def test_prepare_refuses_missing_project_dir(tmp_path):
    fake = FakeApi()
    targets = _targets(tmp_path)
    with pytest.raises(wiring.ResearchRoutingError):
        launcher.prepare_tui_session(
            client=_client(fake), project_dir=tmp_path / "absent",
            targets=targets)
    assert fake.calls == []


def test_prepare_refuses_unverified_switch(tmp_path):
    fake = FakeApi()
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    fake.get_override["ses_fake0001"] = "opencode/beta-free"
    with pytest.raises(wiring.ResearchRoutingError):
        launcher.prepare_tui_session(
            client=_client(fake), project_dir=project, targets=targets)


def test_prepare_merges_group_b_config_with_refs_only(tmp_path):
    fake = FakeApi()
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path, credentials=FAKE_SECRETS)
    external = [item for item in targets if item.is_external]
    assert external, "group B target expected with fake credentials"
    plan = launcher.prepare_tui_session(
        client=_client(fake), project_dir=project, targets=external)
    assert plan.target.is_external
    payload = json.loads((project / "opencode.json").read_text("utf-8"))
    text = json.dumps(payload)
    assert "{env:FREE_ROUTER_OPENROUTER_API_KEY}" in text
    assert "TEST-ONLY-FAKE-OPENROUTER" not in text
    assert CANARY not in text


def test_list_messages_returns_newest_first(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    session_id = client.create_session(tmp_path)
    fake.sessions[session_id]["messages"] = [
        _message("msg_new", "idle", outcome="succeeded"),
        _message("msg_old", "user", text="hi"),
    ]
    seen = client.list_messages(tmp_path, session_id)
    assert [item["id"] for item in seen] == ["msg_new", "msg_old"]


def test_list_messages_rejects_bad_shape(tmp_path):
    def broken(argv, cwd, timeout):
        return wiring.ApiRun(args=tuple(argv), returncode=0, stderr="",
                             stdout=json.dumps({"data": {"not": "a list"}}))

    client = wiring.SessionClient(runner=broken, timeout=5.0)
    with pytest.raises(wiring.SessionError):
        client.list_messages(tmp_path, "ses_fake0001")


def _failed_session(fake, client, project, session_id):
    fake.sessions[session_id]["messages"] = [
        _message("msg_idle_failed", "idle", outcome="failed"),
        _message("msg_user", "user",
                 text=f"Reply with exactly: {CANARY}"),
        _message("msg_switched", "model-switched",
                 model={"providerID": "opencode", "id": "alpha-free"}),
    ]


def test_observe_finds_verified_failure_marker(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    assert seen.failure is not None
    assert seen.failure.message_id == "msg_idle_failed"
    assert seen.model == "opencode/alpha-free"
    assert seen.watermark == "msg_idle_failed"
    assert CANARY not in repr(seen)


def test_observe_healthy_session_has_no_failure(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    fake.sessions[plan.session_id]["messages"] = [
        _message("msg_idle_ok", "idle", outcome="succeeded"),
        _message("msg_user", "user", text="hi"),
    ]
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    assert seen.failure is None
    assert seen.watermark == "msg_idle_ok"


def test_observe_error_type_counts_as_failure(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    fake.sessions[plan.session_id]["messages"] = [
        _message("msg_err", "error"),
    ]
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    assert seen.failure is not None
    assert seen.failure.message_id == "msg_err"


def test_observe_watermark_skips_seen_markers(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id,
        after="msg_idle_failed")
    assert seen.failure is None
    assert seen.failures_skipped == 1
    assert seen.watermark == "msg_idle_failed"


def test_observe_unknown_watermark_considers_everything(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id,
        after="msg_gone")
    assert seen.watermark_unknown is True
    assert seen.failure is not None


def test_observe_reports_model_drift(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    fake.sessions[plan.session_id]["messages"] = [
        _message("msg_tui_switch", "model-switched",
                 model={"providerID": "opencode", "id": "beta-free"}),
        _message("msg_idle_ok", "idle", outcome="succeeded"),
    ]
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    assert seen.failure is None
    assert seen.drift == (("msg_tui_switch", "opencode/beta-free"),)


def test_fallback_applies_next_eligible_target(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        moved = launcher.apply_next_turn_fallback(
            client=client, project_dir=project, observation=seen,
            targets=targets, store=store, run_id="r-tui-fallback")
    finally:
        store.close()
    assert moved.applied is True
    assert moved.previous_model == "opencode/alpha-free"
    assert moved.new_model == "opencode/beta-free"
    assert "msg_idle_failed" in moved.audit
    assert CANARY not in moved.audit
    info = client.get_session(project, plan.session_id)
    assert info.opencode_model == "opencode/beta-free"


def test_fallback_is_noop_without_failure(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    fake.sessions[plan.session_id]["messages"] = [
        _message("msg_idle_ok", "idle", outcome="succeeded"),
    ]
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    calls_before = len(fake.calls)
    moved = launcher.apply_next_turn_fallback(
        client=client, project_dir=project, observation=seen,
        targets=targets)
    assert moved.applied is False
    assert moved.reason == "no-failure-observed"
    assert len(fake.calls) == calls_before


def test_fallback_blocks_without_spare_target(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=[targets[0]])
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    calls_before = len(fake.calls)
    moved = launcher.apply_next_turn_fallback(
        client=client, project_dir=project, observation=seen,
        targets=[targets[0]])
    assert moved.applied is False
    assert moved.reason == "no-eligible-target"
    assert len(fake.calls) == calls_before


def test_fallback_refuses_override_after_manual_switch(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    fake.sessions[plan.session_id]["messages"] = [
        _message("msg_manual", "model-switched",
                 model={"providerID": "opencode", "id": "beta-free"}),
        _message("msg_idle_failed", "idle", outcome="failed"),
    ]
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    calls_before = len(fake.calls)
    moved = launcher.apply_next_turn_fallback(
        client=client, project_dir=project, observation=seen,
        targets=targets)
    assert moved.applied is False
    assert moved.reason == "model-changed-since-failure"
    assert len(fake.calls) == calls_before


def test_11_9_launcher_session_failure_observed_and_routed(tmp_path):
    fake = FakeApi()
    client = _client(fake)
    project = tmp_path / "proj"
    project.mkdir()
    targets = _targets(tmp_path)
    plan = launcher.prepare_tui_session(
        client=client, project_dir=project, targets=targets)
    assert plan.argv[:2] == ("opencode", "--session")
    _failed_session(fake, client, project, plan.session_id)
    seen = launcher.observe_tui_session(
        client=client, project_dir=project, session_id=plan.session_id)
    assert seen.failure is not None
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        moved = launcher.apply_next_turn_fallback(
            client=client, project_dir=project, observation=seen,
            targets=targets, store=store, run_id="r-tui-11-9")
        events = [event for _ts, event, _detail
                  in store.audit_since("r-tui-11-9", 0.0)]
    finally:
        store.close()
    assert moved.applied is True
    assert client.get_session(project, plan.session_id).opencode_model == \
        "opencode/beta-free"
    assert "tui_next_turn_fallback" in events


def test_cli_tui_launch_rejects_missing_snapshot(tmp_path, capsys):
    config = tmp_path / "config"
    config.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    code = cli_main(["tui-launch", "--project-dir", str(project),
                     "--config-dir", str(config),
                     "--state-dir", str(state), "--print-only"])
    assert code == 1
    assert "no published ranking" in capsys.readouterr().out


def test_cli_tui_launch_rejects_missing_project_dir(tmp_path, capsys):
    config = tmp_path / "config"
    config.mkdir()
    code = cli_main(["tui-launch", "--project-dir",
                     str(tmp_path / "absent"),
                     "--config-dir", str(config), "--print-only"])
    assert code == 2
    assert "project dir" in capsys.readouterr().out


def test_runbook_documents_launcher_limitation():
    repo = Path(__file__).resolve().parent.parent
    text = (repo / "docs" / "model_routing" / "RUNBOOK.md").read_text(
        encoding="utf-8")
    assert "opencode-free-research" in text
    assert "hand-opened" in text
    assert "next turn" in text
