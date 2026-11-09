"""Managed research sessions with model switches (TASK-2026-00307).

Offline proof with a simulated OpenCode server: the session starts
on the best group A target (§11.2), falls back across native models
(§11.3) and over to group B (§11.4) without a global HTTP alias,
refuses incompatible switches (§11.5), and keeps history and
checkpoints across switches without duplicating external-effect
tools (§11.8). The client speaks the switchModel contract verified
without generation on opencode v2.0.22. No network, no CLI, no live
model calls: the short real managed session lives in
test_model_routing_research_managed_live.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import research_wiring as wiring  # noqa: E402
from model_routing.availability import (  # noqa: E402
    AvailabilityStore,
    Candidate,
)
from model_routing.models import (  # noqa: E402
    CatalogDocument,
    RankingsDocument,
)

FAKE_SECRETS = {
    "FREE_ROUTER_OPENROUTER_API_KEY": "TEST-ONLY-FAKE-OPENROUTER",
}


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
        "source": "managed-test",
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
        "generator": "managed-test",
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
        "generator": "managed-test",
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


NATIVE_ALPHA = ("org/alpha", "alpha", "opencode/alpha-free", "opencode")
NATIVE_BETA = ("org/beta", "beta", "opencode/beta-free", "opencode")
EXTERNAL_GAMMA = ("acme/gamma", "gamma", "openrouter/acme/gamma:free",
                  "openrouter")
EXTERNAL_DELTA = ("acme/delta", "delta", "openrouter/acme/delta:free",
                  "openrouter")


def _standard_order():
    return [
        _rank_entry(1, *NATIVE_ALPHA, 0.90),
        _rank_entry(2, *NATIVE_BETA, 0.88),
        _rank_entry(3, *EXTERNAL_GAMMA, 0.85),
        _rank_entry(4, *EXTERNAL_DELTA, 0.83),
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
        _catalog_entry("delta", "acme/delta", "Delta Free", _deployment(
            "openrouter/acme/delta:free", "openrouter", "external_free",
            "openrouter", "acme/delta:free")),
    ]


def _snapshot(order):
    document = RankingsDocument.model_validate(_rankings_doc(order))
    return wiring.SessionSnapshot(
        document=document, profile=document.profiles["free-research"],
        file_sha256="sha256:test", catalog_snapshot="catalog-test",
        generated_at=datetime.now(timezone.utc).isoformat())


def _catalog(entries):
    return CatalogDocument.model_validate(_catalog_doc(entries))


def _store(tmp_path: Path) -> AvailabilityStore:
    return AvailabilityStore(tmp_path / "availability.sqlite")


class FakeApi:
    """In-memory OpenCode session server speaking the verified contract."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sessions: dict[str, dict] = {}
        self.counter = 0
        self.get_override: dict[str, str] = {}
        self.vanish_once: set[str] = set()

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
        self.sessions[session_id] = {"model": None}
        return wiring.ApiRun(
            args=tuple(argv), returncode=0, stderr="",
            stdout=json.dumps({"data": {"id": session_id, "cost": 0,
                                        "tokens": {"input": 0, "output": 0,
                                                   "reasoning": 0}}}))

    def _switched(self, argv, path, body):
        session_id = path.removeprefix("/api/session/").removesuffix("/model")
        if not isinstance(body, dict) or not isinstance(
                body.get("model"), dict):
            return self._failed(argv, 400, "InvalidRequestError",
                                "model object is required")
        model = body["model"]
        if not model.get("providerID") or not model.get("id"):
            return self._failed(argv, 400, "InvalidRequestError",
                                "Missing key")
        if session_id in self.vanish_once:
            self.vanish_once.discard(session_id)
            del self.sessions[session_id]
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        self.sessions[session_id]["model"] = {
            "providerID": model["providerID"], "id": model["id"]}
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
                      "tokens": {"input": 3, "output": 7, "reasoning": 0,
                                 "cache": {"read": 0, "write": 0}}}
        if model is not None:
            data["model"] = dict(model)
        return wiring.ApiRun(args=tuple(argv), returncode=0, stderr="",
                             stdout=json.dumps({"data": data}))

    def _deleted(self, argv, session_id):
        if session_id not in self.sessions:
            return self._failed(argv, 404, "SessionNotFoundError",
                                f"Session not found: {session_id}")
        del self.sessions[session_id]
        return wiring.ApiRun(args=tuple(argv), returncode=0,
                             stdout="", stderr="")

    @staticmethod
    def _failed(argv, status, tag, message):
        reason = {400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found"}.get(status, "Error")
        return wiring.ApiRun(
            args=tuple(argv), returncode=1,
            stdout=json.dumps({"_tag": tag, "message": message}),
            stderr=f"HTTP {status} {reason}\n")


def _ok(text="answer", tools=(), results=()):
    return wiring.ProviderTurn(
        text=text, complete=True,
        executed_tools=tuple(tools), tool_results=tuple(results))


def _fail(error_text, stage="before_response", text="", tools=(), results=()):
    return wiring.ProviderTurn(
        text=text, complete=False, executed_tools=tuple(tools),
        tool_results=tuple(results), error_text=error_text,
        failure_stage=stage)


def _targets(tmp_path, store, credentials="__fake__", **kwargs):
    if credentials == "__fake__":
        credentials = dict(FAKE_SECRETS)
    return wiring.native_research_targets(
        _snapshot(_standard_order()), _catalog(_standard_entries()),
        store, credentials=credentials, run_id="r-m", **kwargs)


def _managed(tmp_path, store, api, targets, **kwargs):
    params = {"sleeper": lambda _s: None,
              "checkpoints": tmp_path / "ckpt"}
    params.update(kwargs)
    return wiring.ManagedResearchSession(
        research_id="r-m", candidates=targets, store=store,
        session_client=wiring.SessionClient(runner=api),
        project_dir=tmp_path / "proj", **params)


def test_switch_request_matches_verified_contract(tmp_path):
    api = FakeApi()
    client = wiring.SessionClient(runner=api)
    project = tmp_path / "proj"
    session_id = client.create_session(project, title="probe")
    assert session_id.startswith("ses_")
    client.switch_model(project, session_id, "opencode", "alpha-free")
    assert api.sessions[session_id]["model"] == {
        "providerID": "opencode", "id": "alpha-free"}
    switch = api.calls[-1]
    assert switch[:5] == ["opencode", "api", "--standalone", "POST",
                          f"/api/session/{session_id}/model"]
    assert json.loads(switch[switch.index("-d") + 1]) == {
        "model": {"providerID": "opencode", "id": "alpha-free"}}
    info = client.get_session(project, session_id)
    assert info.opencode_model == "opencode/alpha-free"
    assert (info.input_tokens, info.output_tokens) == (3, 7)
    client.delete_session(project, session_id)
    assert session_id not in api.sessions


def test_client_maps_verified_error_shapes(tmp_path):
    def scripted(responses):
        calls: list[list[str]] = []

        def run(argv, cwd, timeout):
            calls.append(list(argv))
            return responses.pop(0)

        return run, calls

    shapes = [
        (wiring.ApiRun((), 1, json.dumps(
            {"_tag": "SessionNotFoundError", "message": "gone"}),
            "HTTP 404 Not Found\n"), "not_found"),
        (wiring.ApiRun((), 1, json.dumps(
            {"_tag": "InvalidRequestError", "message": "bad"}),
            "HTTP 400 Bad Request\n"), "invalid_request"),
        (wiring.ApiRun((), 1, json.dumps(
            {"_tag": "UnauthorizedError", "message": "denied"}),
            "HTTP 401 Unauthorized\n"), "unauthorized"),
    ]
    for finished, kind in shapes:
        run, _calls = scripted([finished])
        client = wiring.SessionClient(runner=run)
        try:
            client.get_session(tmp_path, "ses_fake0001")
        except wiring.SessionError as exc:
            assert exc.kind == kind, kind
        else:
            raise AssertionError(f"{kind} did not raise")


def test_client_rejects_non_session_ids_without_a_call(tmp_path):
    api = FakeApi()
    client = wiring.SessionClient(runner=api)
    for bad in ("", "proj-1", "http://127.0.0.1:4096/api/session/ses_x"):
        try:
            client.get_session(tmp_path, bad)
        except wiring.SessionError as exc:
            assert exc.kind == "invalid_request"
        else:
            raise AssertionError(f"{bad!r} was accepted")
    assert api.calls == []


def test_delete_missing_session_is_already_gone(tmp_path):
    api = FakeApi()
    client = wiring.SessionClient(runner=api)
    client.delete_session(tmp_path, "ses_fake9999")


def test_fresh_session_reports_unset_model(tmp_path):
    api = FakeApi()
    client = wiring.SessionClient(runner=api)
    session_id = client.create_session(tmp_path)
    info = client.get_session(tmp_path, session_id)
    assert info.opencode_model == ""
    assert info.cost == 0


def test_targets_order_group_a_then_b_with_config_refs(tmp_path):
    store = _store(tmp_path)
    try:
        targets = _targets(tmp_path, store)
        assert [(item.provider_id, item.model_id) for item in targets] == [
            ("opencode", "alpha-free"), ("opencode", "beta-free"),
            ("openrouter", "acme/gamma:free"),
            ("openrouter", "acme/delta:free")]
        assert [item.is_external for item in targets] == [
            False, False, True, True]
        gamma = targets[2]
        assert gamma.route_type == "external_free"
        assert gamma.model_arg() == "openrouter/acme/gamma:free"
        assert gamma.credential_env == "FREE_ROUTER_OPENROUTER_API_KEY"
        assert gamma.provider_config is not None
        api_key = gamma.provider_config["openrouter"]["settings"]["apiKey"]
        assert api_key == "{env:FREE_ROUTER_OPENROUTER_API_KEY}"
        assert "TEST-ONLY-FAKE" not in json.dumps(
            gamma.provider_config)
    finally:
        store.close()


def test_group_b_drops_without_credential_and_audits(tmp_path):
    store = _store(tmp_path)
    try:
        targets = _targets(tmp_path, store, credentials={})
        assert [item.model_arg() for item in targets] == [
            "opencode/alpha-free", "opencode/beta-free"]
        events = store.audit_since("r-m", 0.0)
        assert [event for _ts, event, _d in events].count(
            "research_group_b_uncredentialed") == 2
    finally:
        store.close()


def test_instance_allowlist_covers_group_a_only(tmp_path):
    store = _store(tmp_path)
    try:
        targets = _targets(
            tmp_path, store,
            instance_models={("opencode", "alpha-free")})
        assert [item.model_arg() for item in targets] == [
            "opencode/alpha-free", "openrouter/acme/gamma:free",
            "openrouter/acme/delta:free"]
    finally:
        store.close()


def test_empty_selection_refuses_targets(tmp_path):
    store = _store(tmp_path)
    try:
        snapshot = _snapshot([])
        catalog = _catalog([])
        try:
            wiring.native_research_targets(
                snapshot, catalog, store, run_id="r-m")
        except wiring.ResearchRoutingError:
            pass
        else:
            raise AssertionError("empty selection served targets")
    finally:
        store.close()


def _switch_details(store):
    return [detail for _ts, event, detail in store.audit_since("r-m", 0.0)
            if event == "research_model_switch"]


def test_11_2_starts_on_best_group_a(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        outcome = session.run_turn("first", lambda _req: _ok("alpha says hi"))
        assert outcome.model_effective == "opencode/alpha-free"
        assert outcome.rotations == 0
        assert session.managed_model == "opencode/alpha-free"
        assert session.managed_session_id is not None
        owned = api.sessions[session.managed_session_id]["model"]
        assert owned == {"providerID": "opencode", "id": "alpha-free"}
        (detail,) = _switch_details(store)
        assert session.managed_session_id in detail
        assert "free-research" in detail
        assert "opencode/alpha-free" in detail
        assert "reason=initial" in detail
        assert "attempt=1" in detail and "result=ok" in detail
    finally:
        session.close()
        store.close()


def test_11_3_native_fallback_needs_no_manual_step(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        def invoke(request):
            if request.model == "opencode/alpha-free":
                return _fail("429 too many requests (injected)")
            return _ok("beta takes over")

        outcome = session.run_turn("tired quota", invoke)
        assert outcome.model_effective == "opencode/beta-free"
        assert outcome.rotations == 1
        assert session.managed_model == "opencode/beta-free"
        details = _switch_details(store)
        assert len(details) == 2
        assert "reason=initial" in details[0]
        assert "reason=rotation after rate_limited" in details[1]
        assert "opencode/beta-free" in details[1]
    finally:
        session.close()
        store.close()


def test_11_4_crosses_to_group_b_through_project_config(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        def invoke(request):
            if request.model.startswith("opencode/"):
                return _fail("429 too many requests (injected)")
            return _ok("gamma serves from group B")

        outcome = session.run_turn("native pool dry", invoke)
        assert outcome.model_effective == "openrouter/acme/gamma:free"
        assert outcome.rotations == 2
        config_path = tmp_path / "proj" / "opencode.json"
        written = json.loads(config_path.read_text(encoding="utf-8"))
        assert written["providers"]["openrouter"]["settings"]["apiKey"] == (
            "{env:FREE_ROUTER_OPENROUTER_API_KEY}")
        assert "TEST-ONLY-FAKE" not in config_path.read_text(
            encoding="utf-8")
        details = _switch_details(store)
        assert len(details) == 3
        assert "openrouter/acme/gamma:free" in details[2]
        assert "reason=rotation after rate_limited" in details[2]
    finally:
        session.close()
        store.close()


def test_11_4_merges_group_b_into_existing_project_config(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        project = tmp_path / "proj"
        project.mkdir(parents=True, exist_ok=True)
        (project / "opencode.json").write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "providers": {"keep": {"settings": {}}},
        }), encoding="utf-8")

        def invoke(request):
            if request.model.startswith("opencode/"):
                return _fail("429 too many requests (injected)")
            return _ok("gamma serves from group B")

        outcome = session.run_turn("native pool dry", invoke)
        assert outcome.model_effective == "openrouter/acme/gamma:free"
        written = json.loads(
            (project / "opencode.json").read_text(encoding="utf-8"))
        assert written["providers"]["keep"] == {"settings": {}}
        assert "openrouter" in written["providers"]
    finally:
        session.close()
        store.close()


def test_11_5_incompatible_switch_blocks_resumably(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    targets = _targets(tmp_path, store)
    entries = _standard_entries()
    entries[2]["capabilities"]["tools"] = False
    drifted = _catalog(entries)
    session = _managed(tmp_path, store, api, targets, catalog=drifted)
    saved_id: str | None = None
    try:
        session.run_turn("first", lambda _req: _ok("alpha ok"))
        saved_id = session.managed_session_id

        def invoke(request):
            if request.model.startswith("opencode/"):
                return _fail("429 too many requests (injected)")
            return _ok("unreachable")

        try:
            session.run_turn("drifted pool", invoke)
        except wiring.SessionBlocked as blocked:
            assert blocked.code == wiring.BLOCKED_NO_ELIGIBLE_MODEL
            assert "incompatible" in str(blocked)
        else:
            raise AssertionError("incompatible switch went through")
        assert session.managed_model == "opencode/beta-free"
        saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-m")
        assert saved is not None and saved.turn == 1
        resumed = wiring.ResearchSession.resume(tmp_path / "ckpt", "r-m")
        assert resumed.turn == 1
        assert saved_id is not None and saved_id.startswith("ses_")
    finally:
        session.close()
        store.close()


def test_11_8_history_survives_switches_without_tool_repeats(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    executions: list[str] = []
    try:
        first = session.run_turn(
            "read the sky", lambda _req: _ok(
                "sky is blue",
                tools=[wiring.ToolCall(id="call_sky", name="read")],
                results=[("call_sky", "blue")]),
            validated=["sky-blue"])
        assert first.turn == 1

        def flaky(request):
            executions.append(request.model)
            if len(executions) == 1:
                return _fail("provider exploded mid-stream",
                             stage="partial_text", text="the beantwortet ")
            return _ok("the answer is green")

        second = session.run_turn("and the grass", flaky,
                                  validated=["grass-green"])
        assert second.turn == 2
        assert second.resumed is True
        assert executions == ["opencode/alpha-free"] * 2
        saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-m")
        assert saved is not None and saved.turn == 2
        assert saved.validated_results == ("sky-blue", "grass-green")
        assert [tool.id for tool in saved.applied_tools] == ["call_sky"]
        assert session.managed_model == "opencode/alpha-free"
        assert len(_switch_details(store)) == 1
    finally:
        session.close()
        store.close()


def test_11_8_tool_without_result_still_fails_safe(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        try:
            session.run_turn(
                "shady tool", lambda _req: _ok(
                    "trust me",
                    tools=[wiring.ToolCall(id="call_x", name="write")]))
        except wiring.SessionBlocked as blocked:
            assert blocked.code == wiring.BLOCKED_TOOL_CONFLICT
        else:
            raise AssertionError("resultless tool was accepted")
    finally:
        session.close()
        store.close()


def test_vanished_session_recreates_once_under_supervision(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        session.run_turn("first", lambda _req: _ok("alpha ok"))
        first_id = session.managed_session_id
        assert first_id is not None
        api.vanish_once.add(first_id)

        def invoke(request):
            if request.model == "opencode/alpha-free":
                return _fail("429 too many requests (injected)")
            return _ok("beta after recreate")

        outcome = session.run_turn("server forgot us", invoke)
        assert outcome.model_effective == "opencode/beta-free"
        assert session.managed_session_id != first_id
        events = store.audit_since("r-m", 0.0)
        recreates = [detail for _ts, event, detail in events
                     if event == "research_session_recreated"]
        assert len(recreates) == 1
        assert first_id in recreates[0]
        assert "reason=session_not_found" in recreates[0]
    finally:
        session.close()
        store.close()


def test_unconfirmed_switch_blocks_instead_of_drifting(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        session.run_turn("first", lambda _req: _ok("alpha ok"))
        assert session.managed_session_id is not None
        api.get_override[session.managed_session_id] = "rogue/other"

        def invoke(request):
            if request.model == "opencode/alpha-free":
                return _fail("429 too many requests (injected)")
            return _ok("unreachable")

        try:
            session.run_turn("lying server", invoke)
        except wiring.SessionBlocked as blocked:
            assert blocked.code == wiring.BLOCKED_PROVIDER
            assert "rogue/other" in str(blocked)
        else:
            raise AssertionError("unconfirmed switch was trusted")
        assert session.managed_model == "opencode/alpha-free"
        saved = wiring.load_checkpoint(tmp_path / "ckpt", "r-m")
        assert saved is not None and saved.turn == 1
    finally:
        session.close()
        store.close()


def test_managed_session_refuses_legacy_and_static_shapes(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    try:
        legacy = [Candidate("opencode/a-free", "fam-a", "canon-a",
                            "opencode", 0.9)]
        try:
            _managed(tmp_path, store, api, legacy)
        except wiring.ResearchRoutingError:
            pass
        else:
            raise AssertionError("legacy candidate was managed")
        try:
            _managed(tmp_path, store, api, [])
        except wiring.ResearchRoutingError:
            pass
        else:
            raise AssertionError("empty order was managed")
        try:
            _managed(tmp_path, store, api, _targets(tmp_path, store),
                      static_model="opencode/static-free")
        except wiring.ResearchRoutingError:
            pass
        else:
            raise AssertionError("static fallback was managed")
    finally:
        store.close()


def test_close_deletes_the_owned_session_once(tmp_path):
    store = _store(tmp_path)
    api = FakeApi()
    session = _managed(tmp_path, store, api, _targets(tmp_path, store))
    try:
        session.run_turn("first", lambda _req: _ok("hi"))
        owned = session.managed_session_id
        assert owned in api.sessions
        session.close()
        assert owned not in api.sessions
        session.close()
        deletes = [call for call in api.calls if call[3] == "DELETE"]
        assert len(deletes) == 1
    finally:
        store.close()


def _text_event(text):
    return {"type": "text", "sessionID": "ses_fake0001",
            "part": {"type": "text", "text": text}}


def _tool_event(call_id, name, output):
    return {"type": "tool_use", "sessionID": "ses_fake0001",
            "part": {"id": call_id, "tool": name,
                     "state": {"status": "completed", "output": output}}}


def _finish_event(cost=0):
    return {"type": "step_finish", "sessionID": "ses_fake0001",
            "part": {"type": "step-finish", "cost": cost}}


def test_run_managed_turn_uses_session_without_model_flag(tmp_path):
    seen: list[list[str]] = []

    def run(argv, cwd, timeout):
        seen.append(list(argv))
        lines = "\n".join(json.dumps(event) for event in [
            _text_event("Localizo tu nota y la leo."),
            _tool_event("call_1", "read", "nota.txt"),
            _finish_event(0)])
        return wiring.ApiRun(args=tuple(argv), returncode=0,
                             stdout=lines, stderr="")

    turn = wiring.run_managed_turn(
        session_id="ses_fake0001", project_dir=tmp_path,
        prompt="read the note", runner=run)
    assert turn.complete is True
    assert "nota" in turn.text
    assert turn.executed_tools == (
        wiring.ToolCall(id="call_1", name="read"),)
    assert turn.tool_results == (("call_1", "nota.txt"),)
    (argv,) = seen
    assert argv[:-1] == ["opencode", "run", "--standalone", "--session",
                        "ses_fake0001", "--format", "json"]
    assert "--model" not in argv
    assert argv[-1] == "read the note"


def test_run_managed_turn_billed_cost_fails_as_billing(tmp_path):
    def run(argv, cwd, timeout):
        lines = "\n".join(json.dumps(event) for event in [
            _text_event("paid words"), _finish_event(0.5)])
        return wiring.ApiRun(args=tuple(argv), returncode=0,
                             stdout=lines, stderr="")

    turn = wiring.run_managed_turn(
        session_id="ses_fake0001", project_dir=tmp_path,
        prompt="expensive", runner=run)
    assert turn.complete is False
    assert "402" in turn.error_text


def test_parse_managed_events_keeps_partial_failures_partial(tmp_path):
    del tmp_path
    turn = wiring.parse_managed_events([
        _text_event("valid prefix"),
        {"type": "error", "sessionID": "ses_fake0001",
         "part": {"type": "error"},
         "error": {"type": "provider.stream", "message": "cut",
                   "status": 503}},
    ])
    assert turn.complete is False
    assert turn.failure_stage == "partial_text"
    assert turn.text == "valid prefix"
    assert "status=503" in turn.error_text


def test_parse_managed_events_reconciles_completed_effects_after_failed_probes(tmp_path):
    probes = [
        ("webfetch", "egress blocked"),
        ("skill", "skills disabled"),
        ("question", "non-interactive worker"),
        ("research_read", "unknown tool"),
    ]
    events = [
        {"type": "tool_use", "part": {"id": f"probe_{name}", "tool": name,
         "state": {"status": "error", "error": error}}}
        for name, error in probes
    ]
    events += [
        _tool_event("read_1", "research-broker_research_read", "source one"),
        _tool_event("read_2", "research-broker_research_read", {"text": "source two"}),
        _text_event("The sources establish a fact."),
    ]
    attempt = wiring.parse_managed_events(events)
    assert attempt.complete is True
    for name, error in probes:
        assert name in attempt.error_text
        assert error in attempt.error_text
    assert attempt.executed_tools == (
        wiring.ToolCall(id="read_1", name="research-broker_research_read"),
        wiring.ToolCall(id="read_2", name="research-broker_research_read"),
    )
    assert attempt.tool_results == (("read_1", "source one"), ("read_2", '{"text": "source two"}'))
    session = wiring.ResearchSession(
        research_id="mixed-tools", checkpoints=tmp_path, static_model="opencode/test-free")
    try:
        outcome = session.run_turn("Read the sources", lambda _request: attempt)
        assert outcome.turn == 1
        assert outcome.rotations == 0 and outcome.resumed is False
        assert set(session.ledger) == {"read_1", "read_2"}
        saved = wiring.load_checkpoint(tmp_path, "mixed-tools")
        assert saved.applied_tools == tuple(session.ledger.values())
    finally:
        session.close()


@pytest.mark.parametrize("state", [
    {"status": "error", "error": {"message": "probe rejected"}},
    {"status": "running", "output": "uncommitted output"},
    {"status": "pending"},
    {},
    None,
])
def test_parse_managed_events_keeps_incomplete_tools_in_diagnostics(state):
    attempt = wiring.parse_managed_events([
        {"type": "tool_use", "part": {"id": "probe", "tool": "webfetch", "state": state}},
        _text_event("An answer without that probe."),
    ])
    assert attempt.complete is True
    assert attempt.executed_tools == attempt.tool_results == ()
    assert "webfetch" in attempt.error_text
    assert (state.get("status", "unknown") if state else "unknown") in attempt.error_text
    if state and state.get("error"):
        assert "probe rejected" in attempt.error_text


@pytest.mark.parametrize("with_text", [False, True])
def test_failed_probe_diagnostics_preserve_provider_failures(with_text):
    attempt = wiring.parse_managed_events([
        {"type": "tool_use", "part": {"id": "probe", "tool": "webfetch",
         "state": {"status": "error", "error": "egress blocked"}}},
        *([_text_event("valid prefix")] if with_text else []),
        {"type": "error", "error": {"message": "stream cut", "status": 503}},
    ])
    assert attempt.complete is False
    assert attempt.executed_tools == attempt.tool_results == ()
    assert attempt.failure_stage == ("partial_text" if with_text else "before_response")
    assert "status=503" in attempt.error_text
    assert "webfetch" in attempt.error_text and "egress blocked" in attempt.error_text


def test_failed_probe_without_text_still_requires_a_response():
    attempt = wiring.parse_managed_events([
        {"type": "tool_use", "part": {"id": "probe", "tool": "skill",
         "state": {"status": "error", "error": "skills disabled"}}},
    ])
    assert attempt.complete is False
    assert attempt.failure_stage == "before_response"
    assert "no text" in attempt.error_text and "skills disabled" in attempt.error_text


def test_failed_probe_diagnostics_redact_credentials_and_bound_error_detail():
    attempt = wiring.parse_managed_events([
        {"type": "tool_use", "part": {"id": "probe", "tool": "webfetch",
         "state": {"status": "error", "error": "Bearer syntheticProbeSecret " + "detail " * 100}}},
        _text_event("An answer without that probe."),
    ])
    assert attempt.complete is True
    assert "webfetch" in attempt.error_text
    assert "syntheticProbeSecret" not in attempt.error_text
    assert len(attempt.error_text) <= 300


def test_completed_tool_without_result_still_blocks_reconciliation(tmp_path):
    attempt = wiring.parse_managed_events([
        _tool_event("read_1", "research-broker_research_read", None),
        _text_event("Trust me."),
    ])
    session = wiring.ResearchSession(
        research_id="missing-result", checkpoints=tmp_path, static_model="opencode/test-free")
    try:
        with pytest.raises(wiring.SessionBlocked) as blocked:
            session.run_turn("Read the source", lambda _request: attempt)
        assert blocked.value.code == wiring.BLOCKED_TOOL_CONFLICT
        assert session.ledger == {}
    finally:
        session.close()
