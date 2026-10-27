"""Native OpenCode contract: no external inference, v2 routes, instance ids (TASK-2026-00304). Offline only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from model_routing import availability as router_availability
from model_routing import errors as router_errors
from model_routing import external as router_external
from model_routing import live as router_live
from model_routing import models as router_models
from model_routing import probes as router_probes
from model_routing import publish as router_publish
from model_routing.models import (
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Deployment,
    ProfileRanking,
    RankedDeployment,
    RankedEntry,
    RankingProvenance,
    RankingsDocument,
)

CORE = Path(__file__).resolve().parents[1] / "openceo-core"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _run(returncode=0, stdout="", stderr="", argv=()):
    return router_probes.NativeRun(
        args=tuple(argv), returncode=returncode, stdout=stdout, stderr=stderr
    )


def _events(*events) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _text_event(text):
    return {"type": "text", "part": {"text": text}}


def _step_finish(cost=0):
    return {"type": "step_finish", "part": {"cost": cost}}


# Package scan -----------------------------------------------------------------


def test_package_carries_no_opencode_inference_url():
    package = CORE / "model_routing"
    allowed = (
        "sources.py",
        'return _fetch("opencode", "https://opencode.ai/inference/v1/models", headers, get)',
    )
    hits = []
    for path in sorted(package.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "opencode.ai/inference" not in line:
                continue
            if path.name == allowed[0] and allowed[1] in line:
                continue
            hits.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not hits, "external OpenCode inference reference:\n" + "\n".join(hits)
    assert allowed[1] in (package / allowed[0]).read_text(encoding="utf-8")


# Native probes ----------------------------------------------------------------


def test_run_probe_routes_opencode_to_the_native_cli(isolated_home):
    seen: list = []

    def _runner(argv, _cwd, _timeout):
        seen.append(argv)
        return _run(stdout=_events(_text_event("ok"), _step_finish()))

    result, raw = router_probes.run_probe(
        "opencode", "chat", "big-pickle", {}, enabled=True, runner=_runner
    )
    assert seen == [
        ["opencode", "run", "--format", "json", "--model", "opencode/big-pickle",
         router_probes.CHAT_PROMPT]
    ]
    assert result.deployment_id == "opencode/big-pickle"
    assert len(raw["events"]) == 2


def test_native_tool_probe_marks_tools_capability(isolated_home):
    stdout = _events(
        {"type": "tool_use", "part": {"id": "call-1", "tool": "read", "input": {}}},
        _text_event("ok"),
        _step_finish(),
    )

    def _runner(argv, _cwd, _timeout):
        assert argv[-1] == router_probes.NATIVE_TOOL_PROMPT
        return _run(stdout=stdout)

    result, _ = router_probes.run_native_probe(
        "opencode", "tool_calls", "big-pickle", enabled=True, runner=_runner
    )
    assert result.capabilities.tools is True


def test_native_probe_standalone_spawns_a_private_server():
    seen: list = []

    def _runner(argv, _cwd, _timeout):
        seen.append(argv)
        return _run(stdout=_events(_text_event("ok"), _step_finish()))

    router_probes.run_native_probe(
        "openrouter", "chat", "example/alpha:free",
        enabled=True, runner=_runner, standalone=True)
    assert seen == [
        ["opencode", "run", "--format", "json", "--model",
         "openrouter/example/alpha:free", "--standalone",
         router_probes.CHAT_PROMPT]
    ]


def test_default_runner_points_pwd_at_the_project(tmp_path):
    finished = router_probes._default_native_runner(
        ["sh", "-c", "printf %s \"$PWD\""], str(tmp_path), 30.0)
    assert finished.returncode == 0
    assert finished.stdout == str(tmp_path)


def test_native_probe_disabled_by_default_makes_no_call():
    def _runner(argv, _cwd, _timeout):
        raise AssertionError("disabled probe must not run the CLI")

    with pytest.raises(router_probes.ProbeError) as exc_info:
        router_probes.run_native_probe("opencode", "chat", "m", runner=_runner)
    assert "disabled" in str(exc_info.value)


def test_native_probe_rejects_streaming_and_second_turn():
    def _runner(argv, _cwd, _timeout):
        raise AssertionError("unsupported kind must not run the CLI")

    for kind in ("streaming", "second_turn"):
        with pytest.raises(router_probes.ProbeError) as exc_info:
            router_probes.run_native_probe(
                "opencode", kind, "m", enabled=True, runner=_runner
            )
        assert "no native form" in str(exc_info.value)


def test_native_quota_error_raises_live_rate_limited():
    recorded = json.loads((FIXTURES / "opencode-free-quota-429.json").read_text(encoding="utf-8"))

    def _runner(argv, _cwd, _timeout):
        return _run(returncode=1, stdout=_events(recorded["event"]))

    with pytest.raises(router_live.LiveRateLimited) as exc_info:
        router_probes.run_native_probe(
            "opencode", "chat", "big-pickle", enabled=True, runner=_runner
        )
    assert exc_info.value.provider == "opencode"


def test_native_string_429_status_raises_live_rate_limited():
    event = {"type": "error", "error": {"type": "provider.busy", "status": "429"}}

    def _runner(argv, _cwd, _timeout):
        return _run(returncode=1, stdout=_events(event))

    with pytest.raises(router_live.LiveRateLimited):
        router_probes.run_native_probe(
            "opencode", "chat", "m", enabled=True, runner=_runner
        )


def test_native_error_without_quota_is_a_plain_probe_error():
    event = {
        "type": "error",
        "error": {"type": "provider.auth", "message": "login expired", "status": 401},
    }

    def _runner(argv, _cwd, _timeout):
        return _run(returncode=1, stdout=_events(event))

    with pytest.raises(router_probes.ProbeError) as exc_info:
        router_probes.run_native_probe(
            "opencode", "chat", "m", enabled=True, runner=_runner
        )
    assert "status 401" in str(exc_info.value)


def test_native_billed_cost_fails_closed():
    def _runner(argv, _cwd, _timeout):
        return _run(stdout=_events(_text_event("ok"), _step_finish(0.02)))

    with pytest.raises(router_probes.ProbeError) as exc_info:
        router_probes.run_native_probe(
            "opencode", "chat", "m", enabled=True, runner=_runner
        )
    assert "non-zero cost" in str(exc_info.value)


# Instance catalog ---------------------------------------------------------------


def test_read_instance_models_uses_the_first_nonempty_listing():
    calls: list = []

    def _runner(argv, cwd, timeout):
        calls.append(argv)
        if argv == ["opencode", "models"]:
            return _run(stdout="")
        return _run(stdout="opencode/big-pickle\nopenrouter/x:free\n")

    catalog = router_probes.read_instance_models(runner=_runner)
    assert catalog.models == frozenset({("opencode", "big-pickle"), ("openrouter", "x:free")})
    assert catalog.source == "opencode models --log-level debug"
    assert [call[:2] for call in calls] == [["opencode", "models"], ["opencode", "models"]]
    assert router_probes.is_instance_target("opencode", "big-pickle", catalog) is True
    assert router_probes.is_instance_target("opencode", "ghost", catalog) is False


def test_read_instance_models_fails_closed_when_every_listing_is_empty():
    def _runner(argv, _cwd, _timeout):
        return _run(stdout="\n")

    with pytest.raises(router_probes.InstanceError):
        router_probes.read_instance_models(runner=_runner)


# Error classification -----------------------------------------------------------


def test_external_opencode_403_is_route_not_authorized():
    classified = router_errors.classify_http(
        403, detail="FreeTierError", provider="opencode", client="external"
    )
    assert classified.error_class == "route_not_authorized"
    assert classified.action == "block_route"
    assert classified.retryable is False


def test_bare_and_native_403_keep_the_forbidden_meaning():
    assert router_errors.classify_http(403).error_class == "forbidden"
    assert router_errors.classify_http(403, detail="x").action == "inspect"
    native = router_errors.classify_http(403, provider="opencode", client="native")
    assert (native.error_class, native.action) == ("forbidden", "inspect")


def test_probe_403_marks_opencode_route_not_authorized():
    opencode = router_probes.ProbeError.from_http("opencode", "chat", 403, "https://x.test")
    assert opencode.error_class == "route_not_authorized"
    assert opencode.retryable is False
    groq = router_probes.ProbeError.from_http("groq", "chat", 403, "https://x.test")
    assert groq.error_class is None


def test_recorded_quota_error_blocks_the_whole_native_provider(tmp_path):
    recorded = json.loads((FIXTURES / "opencode-free-quota-429.json").read_text(encoding="utf-8"))
    event = recorded["event"]
    body = event["error"]["response"]["body"]
    classified = router_errors.classify_text(f"{event['error']['type']} {body}")
    assert classified.error_class == "daily_quota"
    assert classified.action == "disable_provider"
    store = router_availability.AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        store.record_error(
            provider="opencode", deployment_id="opencode/big-pickle",
            model="opencode/big-pickle", classified=classified, run_id="native-quota",
        )
        blocked = store.provider_blocked("opencode")
        assert blocked is not None
        assert "daily_quota" in blocked["reason"]
    finally:
        store.close()


# Catalog schema v2 --------------------------------------------------------------


def _v1_document() -> dict:
    return {
        "schema_version": 1,
        "generated_at": None,
        "generator": "v1-test",
        "stale": False,
        "stale_reason": None,
        "entries": [
            {
                "schema_version": 1,
                "family_id": "fam",
                "canonical_model_id": "opencode/big-pickle",
                "deployments": [
                    {"deployment_id": "opencode/big-pickle", "provider": "opencode",
                     "free_verified": True, "input_price_usd": 0, "output_price_usd": 0}
                ],
            },
            {
                "schema_version": 1,
                "family_id": "fam",
                "canonical_model_id": "openrouter/v/m:free",
                "deployments": [
                    {"deployment_id": "openrouter/v/m:free", "provider": "openrouter",
                     "free_verified": True, "input_price_usd": 0, "output_price_usd": 0}
                ],
            },
        ],
    }


def test_migrate_catalog_v1_to_v2_pins_each_route():
    migrated = router_models.migrate_catalog_v1_to_v2(_v1_document())
    assert migrated["schema_version"] == router_models.CATALOG_SCHEMA_VERSION == 2
    doc = CatalogDocument.model_validate(migrated)
    native, external = doc.entries[0].deployments[0], doc.entries[1].deployments[0]
    assert (native.route_type, native.allowed_client) == ("opencode_native_free", "opencode_only")
    assert (native.provider_id, native.model_id) == ("opencode", "big-pickle")
    assert (external.route_type, external.allowed_client) == (
        "external_free", "configured_opencode_or_gateway")
    assert (external.provider_id, external.model_id) == ("openrouter", "v/m:free")
    assert native.gateway_model_id == "big-pickle"
    assert external.gateway_model_id == "v/m:free"


def test_migrate_catalog_v1_to_v2_keeps_a_present_gateway_model_id():
    doc = _v1_document()
    doc["entries"][1]["deployments"][0]["gateway_model_id"] = "vendor/alias:free"
    migrated = router_models.migrate_catalog_v1_to_v2(doc)
    validated = CatalogDocument.model_validate(migrated)
    assert validated.entries[1].deployments[0].gateway_model_id == "vendor/alias:free"


def test_migrate_catalog_v1_to_v2_refuses_malformed_input():
    with pytest.raises(ValueError):
        router_models.migrate_catalog_v1_to_v2({"schema_version": 2, "entries": []})
    broken = _v1_document()
    broken["entries"][0]["deployments"][0].pop("provider")
    with pytest.raises(ValueError):
        router_models.migrate_catalog_v1_to_v2(broken)
    broken = _v1_document()
    broken["entries"][0]["deployments"][0]["deployment_id"] = "no-slash"
    with pytest.raises(ValueError):
        router_models.migrate_catalog_v1_to_v2(broken)


def test_v2_deployment_refuses_a_missing_route_field():
    with pytest.raises(ValidationError):
        Deployment(
            deployment_id="opencode/big-pickle", provider="opencode",
            allowed_client="opencode_only", provider_id="opencode", model_id="big-pickle",
        )


def test_load_catalog_doc_migrates_v1_and_rejects_unknown_versions(tmp_path):
    v1_path = tmp_path / "catalog.json"
    v1_path.write_text(json.dumps(_v1_document()), encoding="utf-8")
    doc = router_publish.load_catalog_doc(v1_path)
    assert doc.schema_version == 2
    assert doc.entries[0].deployments[0].route_type == "opencode_native_free"
    v2_broken = _v1_document()
    v2_broken["schema_version"] = 2
    v2_broken["entries"][0]["schema_version"] = 2
    v1_path.write_text(json.dumps(v2_broken), encoding="utf-8")
    with pytest.raises(router_publish.PublishError):
        router_publish.load_catalog_doc(v1_path)
    v1_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(router_publish.PublishError):
        router_publish.load_catalog_doc(v1_path)


# Gateway exclusion --------------------------------------------------------------


def _dep(deployment_id, provider, gateway):
    native = provider == "opencode"
    return Deployment(
        deployment_id=deployment_id,
        provider=provider,
        route_type="opencode_native_free" if native else "external_free",
        allowed_client="opencode_only" if native else "configured_opencode_or_gateway",
        provider_id=provider,
        model_id=deployment_id.split("/", 1)[1],
        gateway_model_id=gateway,
        free_offered=True,
        free_verified=True,
        healthy=True,
        input_price_usd=0.0,
        output_price_usd=0.0,
    )


def test_gateway_plan_and_yaml_never_carry_a_native_opencode_deployment():
    catalog = CatalogDocument(
        generator="native-test",
        entries=[
            CatalogEntry(
                family_id="alpha", canonical_model_id="example/alpha",
                capabilities=Capabilities(tools=True, context_tokens=131072),
                deployments=[
                    _dep("opencode/alpha-free", "opencode", "alpha-free"),
                    _dep("openrouter/example/alpha:free", "openrouter", "example/alpha:free"),
                ],
            ),
        ],
    )
    rankings = RankingsDocument(
        generator="native-test",
        catalog_snapshot="catalog-test",
        catalog_sha256="0" * 64,
        profiles={
            "free-research": ProfileRanking(
                profile="free-research",
                order=[
                    RankedEntry(
                        rank=1, canonical_model_id="example/alpha", family_id="alpha",
                        primary=RankedDeployment(
                            deployment_id="opencode/alpha-free", provider="opencode"),
                        backups=[RankedDeployment(
                            deployment_id="openrouter/example/alpha:free",
                            provider="openrouter")],
                        quality=0.9, coverage=1.0,
                    ),
                ],
                experimental=[],
                pending_top3=None,
                provenance=RankingProvenance(
                    catalog_snapshot="catalog-test", catalog_sha256="0" * 64),
            ),
        },
    )
    plan = router_external.build_gateway_plan(catalog, rankings)
    assert [route.deployment_id for route in plan.native_routes] == ["opencode/alpha-free"]
    assert "opencode/" not in json.dumps(plan.model_list)
    assert "opencode/" not in router_external.render_gateway_yaml(plan)
