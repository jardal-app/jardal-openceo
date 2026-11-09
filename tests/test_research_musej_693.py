"""Exhaustion backstop with scripted turns and a sandboxed receipt-only CLI."""
import asyncio
import json
import shutil
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from model_routing import research_wiring as wiring
from model_routing.availability import AvailabilityStore
from model_routing.paths import catalog_path
from research.runtimes.base import IsolationUnavailable, RuntimeOutcome
from research.runtimes.isolation import BubblewrapBackend
from research.runtimes.opencode import OpenCodeRuntime, _default_spawn
from research.wiring import _provider_status
from settings import Settings
from test_research_deep_free_662 import build, draft, verify
from test_research_free_profile_648 import setup as free_setup, fail, report
from test_research_opencode import _FakeBackend, _request


STUB = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
assert sys.argv[1] == "exec"
assert sys.argv[sys.argv.index("--model") + 1] == "muse-spark-1.3-contributor"
assert "--json" in sys.argv
if __CHECK_BOUNDARY__:
    assert not Path(__CANARY__).exists()
assert "DEEPSEEK_API_KEY" not in os.environ
assert os.environ["MUSE_NO_AUTO_UPDATE"] == "1"
assert Path(os.environ["MUSE_AUTH_PATH"]).is_file()
assert (Path(os.environ["XDG_CONFIG_HOME"]) / "muse/auth.json").is_file()
config = json.loads((Path(os.environ["XDG_CONFIG_HOME"]) / "muse/settings.json").read_text())
assert set(config["mcpServers"]) == {"research-broker"}
assert config["run"]["toolset"] == []
prompt = sys.argv[-1]
try:
    payload = json.JSONDecoder().raw_decode(prompt)[0]
except ValueError:
    payload = {}
if payload.get("task") == "verifier":
    body = {"verdicts": [{"claim_digest": c["claim_digest"], "verdict": "supported",
        "evidence_id": c["evidence_ids"][0], "extract": "source fact", "reason": "receipt"}
        for c in payload["claims"]]}
else:
    body = {"summary": "Source establishes a fact", "claims": [{"id": "c1",
        "statement": "source fact", "kind": "fact", "evidence_ids": ["res_profile-e1"],
        "support": [{"extract": "source fact"}]}]}
print(json.dumps({"payload_type": "run.terminal.__TERMINAL__", "payload": {
    "terminal": "__TERMINAL__", "text": json.dumps(body), "error": "synthetic subscription failure"}}))
'''


def cool_fleet():
    with closing(AvailabilityStore()) as store:
        for name in ("a", "b", "c"):
            store.set_cooldown(f"opencode/{name}", "synthetic daily quota", seconds=3600)


def enable(service, repo):
    service._settings.MUSE_CLI_PATH = "musej"
    repo.job.setdefault("provider_status", {})["musej"] = {
        "configured": True, "mounted": True, "verified": None}
    service._deps.ledger = SimpleNamespace(reserve=AsyncMock(side_effect=AssertionError("paid reservation")))
    repo.research_usage = SimpleNamespace(update_one=AsyncMock())


@pytest.fixture
def stub_cli(tmp_path, monkeypatch):
    def mount(service, repo, *, terminal="completed", real_boundary=False):
        if real_boundary:
            if not shutil.which("bwrap"):
                pytest.skip("bwrap unavailable for receipt-only CLI smoke")
            probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--", "/bin/true"],
                                   capture_output=True, timeout=5)
            if probe.returncode:
                pytest.skip("host kernel denies bwrap namespaces; sandbox plan covered with doubles")
        binary_dir = tmp_path / "bin"
        binary_dir.mkdir(exist_ok=True)
        canary = tmp_path / "private-host" / "vault-and-repo.txt"
        canary.parent.mkdir(exist_ok=True)
        canary.write_text("synthetic host-only canary")
        cli = binary_dir / "musej"
        cli.write_text('#!/bin/sh\nexec muse "$@"\n')
        cli.chmod(0o700)
        launcher = binary_dir / "muse"
        launcher.write_text('#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\n'
            'root = Path(__file__).parent\nversion = (root / ".muse-version").read_text().strip()\n'
            'binary = root / ("muse-bin-" + version)\nos.execv(binary, [str(binary), *sys.argv[1:]])\n')
        launcher.chmod(0o700)
        (binary_dir / ".muse-version").write_text("1.0.0-R1")
        binary = binary_dir / "muse-bin-1.0.0-R1"
        binary.write_text(STUB.replace("__CANARY__", repr(str(canary))).replace("__TERMINAL__", terminal)
                       .replace("__CHECK_BOUNDARY__", repr(real_boundary)))
        binary.chmod(0o700)
        auth = tmp_path / "subscription-auth.json"
        auth.write_text("{}")
        worker_path = f"{binary_dir}:/usr/bin:/bin"
        monkeypatch.setenv("PATH", worker_path)
        enable(service, repo)
        spawned, requests = [], []

        async def spawn(argv, *, cwd, env):
            spawned.append((argv, env))
            command = argv if real_boundary else argv[argv.index("--") + 1:]
            return await _default_spawn(command, cwd=cwd, env=env)

        worker = OpenCodeRuntime(settings=service._settings, isolation_backend=BubblewrapBackend(),
            state_dir=tmp_path / "workers", broker_url="http://127.0.0.1:9/research-broker",
            broker_bearer="synthetic-broker-receipt", spawn=spawn,
            base_env={"PATH": worker_path, "HOME": str(tmp_path / "host-home"),
                      "MUSE_AUTH_PATH": str(auth), "DEEPSEEK_API_KEY": "synthetic-denied"})
        free_runtime = service._deps.runtime

        async def dispatch(request):
            requests.append(request)
            return await (worker.run(request) if request.provider == "musej" else free_runtime.run(request))

        service._deps.runtime = SimpleNamespace(run=dispatch, cancel=worker.cancel)
        return spawned, requests
    return mount


@pytest.mark.parametrize("mode", ["quick", "deep"])
def test_cold_fleet_completes_through_stub_and_sandbox_plan_with_zero_ledger(free_setup, stub_cli, mode):
    service, repo, _, reads = build(free_setup, []) if mode == "deep" else free_setup([])
    spawned, requests = stub_cli(service, repo)
    cool_fleet()
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed", done.get("failure_reason")
    assert done["result"]["usage"]["model_calls"] == 2
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    assert done["provider_status"]["musej"]["verified"] is True
    assert all(r.provider == "musej" and not r.allow_session_resume and r.session_id is None for r in requests)
    assert len(spawned) == len(requests) == 2 and len(reads) == 1
    assert all(argv[0] == "bwrap" for argv, _ in spawned)
    for argv, env in spawned:
        prefix = argv[:argv.index("--")]
        sources = [Path(prefix[i + 1]) for i, flag in enumerate(prefix) if flag in {"--bind", "--ro-bind"}]
        assert {"musej", "muse", ".muse-version", "muse-bin-1.0.0-R1"} <= {p.name for p in sources}
        canary = next(p for p in sources if p.name == "musej").parent.parent / "private-host/vault-and-repo.txt"
        assert all(not canary.is_relative_to(source) for source in sources)
    assert all(row["provider"] == "musej" and row["cost_tier"] == "own" and row["micro_usd"] == 0
               for row in done["free_research"]["model_usage"])
    assert wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"]).turn == 2
    service._deps.ledger.reserve.assert_not_awaited()
    assert repo.research_usage.update_one.await_count == 2
    entries = [call.args[1]["$setOnInsert"] for call in repo.research_usage.update_one.await_args_list]
    assert len({row["operation_id"] for row in entries}) == 2
    assert all(row["provider"] == "musej" and row["cost_tier"] == "own"
               and row["charged_micro_usd"] == row["reserved_micro_usd"] == 0 for row in entries)
    assert service.slot_snapshot()["calls_in_use"] == 0


def test_receipt_only_cli_runs_inside_kernel_boundary(free_setup, stub_cli):
    service, repo, _, _ = free_setup([])
    stub_cli(service, repo, real_boundary=True)
    cool_fleet()
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed", done.get("failure_reason")


def test_rotation_exhausts_before_musej_and_preserves_completed_only_effects(free_setup, stub_cli):
    completed = {"type": "tool_use", "part": {"id": "read-receipt", "tool": "research_read",
                 "state": {"status": "completed", "output": "source fact"}}}
    failed = {"type": "tool_use", "part": {"id": "failed-receipt", "tool": "research_read",
              "state": {"status": "error", "error": "synthetic failure"}}}
    service, repo, _, _ = free_setup([fail(429, events=[completed, failed]), fail(429), fail(429)])
    _, requests = stub_cli(service, repo)
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert [r.provider for r in requests] == ["opencode"] * 3 + ["musej"] * 2
    assert "read-receipt=source fact" in requests[3].prompt
    assert "failed-receipt=" not in requests[3].prompt
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
    assert [t.id for t in saved.applied_tools] == ["read-receipt"]
    assert done["result"]["usage"]["model_calls"] == 5


@pytest.mark.parametrize("missing_result", [False, True])
def test_musej_turn_reconciles_completed_only_receipts_and_conflicts(free_setup, missing_result):
    state = {"status": "completed"}
    if not missing_result:
        state["output"] = "source fact"
    events = [{"type": "tool_use", "part": {"id": "own-receipt", "tool": "research_read", "state": state}},
              {"type": "tool_use", "part": {"id": "unsettled", "tool": "research_read",
               "state": {"status": "running", "output": "not completed"}}}]
    service, repo, runtime, _ = free_setup([RuntimeOutcome("outline", None, events=events), report()])
    enable(service, repo)
    cool_fleet()
    done = asyncio.run(service.run_once())
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
    with closing(AvailabilityStore()) as store:
        assert not store.reservations(repo.job["research_id"], saved.session_id)
    if missing_result:
        assert done["status"] == "blocked" and "BLOCKED_TOOL_CONFLICT" in done["failure_reason"]
        assert len(runtime.requests) == 1 and not saved.applied_tools
    else:
        assert done["status"] == "completed"
        assert [tool.id for tool in saved.applied_tools] == ["own-receipt"]
        assert "own-receipt=source fact" in runtime.requests[1].prompt


@pytest.mark.parametrize("state", ["unhealthy", "disabled", "provider-blocked"])
def test_initial_unavailable_fleet_uses_backstop(free_setup, state):
    service, repo, runtime, _ = free_setup([RuntimeOutcome("outline", None), report()])
    enable(service, repo)
    if state == "unhealthy":
        catalog = json.loads(catalog_path().read_text())
        for entry in catalog["entries"]:
            for dep in entry["deployments"]:
                dep["healthy"] = False
        catalog_path().write_text(json.dumps(catalog))
    else:
        with closing(AvailabilityStore()) as store:
            if state == "provider-blocked":
                store.block_provider("opencode", "synthetic quota", seconds=3600)
            else:
                for name in ("a", "b", "c"):
                    store.disable_route(f"opencode/{name}", "synthetic disabled route")
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert [r.provider for r in runtime.requests] == ["musej", "musej"]


def test_transient_rotation_does_not_use_musej(free_setup):
    service, repo, runtime, _ = free_setup([fail(429), RuntimeOutcome("outline", None), report()])
    enable(service, repo)
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/b", "opencode/b"]
    assert "musej_attempts" not in done["free_research"]
    assert done["provider_status"]["musej"]["verified"] is None


@pytest.mark.parametrize("disabled", ["kill-switch", "router", "catalog"])
def test_disabled_or_invalid_configuration_blocks_without_backstop(free_setup, disabled):
    service, repo, runtime, _ = free_setup([])
    enable(service, repo)
    cool_fleet()
    if disabled == "kill-switch":
        service._settings.RESEARCH_MUSEJ_FALLBACK_ENABLED = False
    elif disabled == "router":
        service._settings.RESEARCH_FREE_ROUTER_ENABLED = False
    else:
        catalog_path().unlink()
    done = asyncio.run(service.run_once())
    assert done["status"] == "blocked" and "BLOCKED_NO_ELIGIBLE_MODEL" in done["failure_reason"]
    assert not runtime.requests


def test_musej_failure_blocks_with_reason_and_cannot_replay_on_resume(free_setup, stub_cli):
    service, repo, _, _ = free_setup([])
    spawned, _ = stub_cli(service, repo, terminal="failed")
    cool_fleet()

    async def scenario():
        done = await service.run_once()
        assert done["status"] == "blocked"
        assert "musej fallback failed" in done["failure_reason"]
        assert "synthetic subscription failure" in done["failure_reason"]
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    done = asyncio.run(scenario())
    assert done["status"] == "blocked" and "already attempted" in done["failure_reason"]
    assert len(spawned) == 1
    assert done["free_research"]["model_usage"][0]["micro_usd"] == 0


@pytest.mark.parametrize("phase", ["researcher-1", "verifier"])
def test_malformed_deep_backstop_blocks_without_format_retry(free_setup, phase):
    tool = {"type": "tool_use", "part": {"id": "source-receipt", "tool": "research_read",
            "state": {"status": "completed", "output": "source fact"}}}
    malformed = RuntimeOutcome("not a report", None, events=[tool])
    script = [draft, malformed] if phase == "verifier" else [malformed]
    service, repo, runtime, _ = build(free_setup, script)
    enable(service, repo)
    cool_fleet()
    done = asyncio.run(service.run_once())
    assert done["status"] == "blocked" and "malformed musej" in done["failure_reason"]
    assert len(runtime.requests) == len(script)
    assert done["deep_execution"]["model_calls"] == len(script)
    assert done["deep_execution"]["estimated_micro_usd"] == 0
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
    assert saved.partial_text == ""
    assert [tool.id for tool in saved.applied_tools] == ["source-receipt"]

    async def scenario():
        with closing(AvailabilityStore()) as store:
            for name in ("a", "b", "c"):
                store.set_cooldown(f"opencode/{name}", "synthetic recovery", seconds=0, now=0)
        runtime.script.extend([verify] if phase == "verifier" else [draft, verify])
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    assert asyncio.run(scenario())["status"] == "completed"
    resumed_prompt = runtime.requests[len(script)].prompt
    assert "not a report" not in resumed_prompt
    assert "source-receipt=source fact" in resumed_prompt


def test_empty_fleet_resume_preserves_turn_effects_and_partial_text(free_setup):
    from research.runtimes.base import RuntimeUnavailable

    tool = {"type": "tool_use", "part": {"id": "source-receipt", "tool": "research_read",
            "state": {"status": "completed", "output": "source fact"}}}
    service, repo, runtime, reads = free_setup([
        RuntimeOutcome("outline", None, events=[tool]),
        RuntimeUnavailable("synthetic unavailable", outcome=RuntimeOutcome("Kept synthesis prefix", None)),
    ])
    enable(service, repo)
    catalog = json.loads(catalog_path().read_text())
    for entry in catalog["entries"]:
        for dep in entry["deployments"]:
            dep["healthy"] = False
    catalog_path().write_text(json.dumps(catalog))

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked"
        saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
        assert not saved.candidates and saved.rankings_snapshot
        assert saved.turn == 1 and saved.partial_text == "Kept synthesis prefix"
        assert [tool.id for tool in saved.applied_tools] == ["source-receipt"]
        await service.resume(repo.job["research_id"])
        resumed = await service.run_once()
        assert resumed["status"] == "blocked" and "already attempted" in resumed["failure_reason"]
        after = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
        assert after.session_id == saved.session_id
        assert after.turn == saved.turn and after.applied_tools == saved.applied_tools
        assert after.partial_text == saved.partial_text and after.pending == saved.pending

    asyncio.run(scenario())
    assert len(runtime.requests) == 2 and len(reads) == 1


def test_resume_after_router_recovery_initializes_candidates(free_setup):
    service, repo, runtime, _ = free_setup([RuntimeOutcome("outline", None), report()])
    service._settings.RESEARCH_FREE_ROUTER_ENABLED = False

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked" and not runtime.requests
        saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
        assert not saved.candidates and not saved.rankings_snapshot
        service._settings.RESEARCH_FREE_ROUTER_ENABLED = True
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    assert asyncio.run(scenario())["status"] == "completed"
    assert [request.model for request in runtime.requests] == ["opencode/a", "opencode/a"]


def test_usage_storage_failure_blocks_before_musej_invocation(free_setup):
    service, repo, runtime, _ = free_setup([])
    enable(service, repo)
    repo.research_usage.update_one.side_effect = OSError("synthetic storage unavailable")
    cool_fleet()
    done = asyncio.run(service.run_once())
    assert done["status"] == "blocked" and "usage ledger unavailable" in done["failure_reason"]
    assert not runtime.requests


def test_backstop_requires_bwrap_and_status_uses_existing_provider_shape(tmp_path):
    settings = Settings(_env_file=None)
    assert settings.RESEARCH_MUSEJ_FALLBACK_ENABLED is True
    worker = OpenCodeRuntime(settings=settings, isolation_backend=_FakeBackend())
    with pytest.raises(IsolationUnavailable, match="bwrap"):
        asyncio.run(worker.run(_request(tmp_path, provider="musej")))
    kwargs = dict(ledger=None, discover=None, firecrawl=None, deep_model=None)
    assert _provider_status(settings, runtime=None, **kwargs)["musej"] == {
        "configured": True, "mounted": False, "verified": None}
    assert _provider_status(settings, runtime=worker, **kwargs)["musej"] == {
        "configured": True, "mounted": True, "verified": None}
