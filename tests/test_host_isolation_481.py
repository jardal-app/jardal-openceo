"""Builders on the host user per project (TASK-2026-00481).

``dev.isolation: host`` is the Owner-ordered exception to the bwrap
fence for projects whose toolchain (Docker, package stores, caches) the
fence cannot provide. The builder runs with the host user; the env still
goes through the ``child_env`` allowlist. Reviewers and read-only roles
keep the fence, and projects without the key stay sandboxed.

No models, no network, no Docker.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.session import DevSessionManager, TierExecutor
from config_loader import ConfigLoader
from orchestration.agent_sandbox import project_isolation
from orchestration.live import ExecResult, ProcessExecutor
from settings import Settings
from state import CodingTool


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())


def _config(isolation: str | None = None) -> dict:
    dev = {
        "tiers": {
            "tier_a": {"account": "claudej"},
            "tier_b": {"account": "codexf"},
            "tier_c": {
                "chain": ["muse"],
                "muse": {"command": ["musej", "exec"]},
            },
        },
        "providers": {
            "claude": {"command": ["claudej"], "cwd": "/tmp/repo", "env": {}},
            "codex": {"command": ["codexf"], "cwd": "/tmp/repo", "env": {}},
            "muse": {"command": ["musej"], "cwd": "/tmp/repo", "env": {}},
        },
    }
    if isolation is not None:
        dev["isolation"] = isolation
    return {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]},
            "dev": dev}


class TestProjectIsolation:
    def test_default_and_values(self) -> None:
        assert project_isolation({}) == "sandbox"
        assert project_isolation({"isolation": "host"}) == "host"
        assert project_isolation({"isolation": " HOST "}) == "host"
        assert project_isolation({"isolation": "sandbox"}) == "sandbox"

    def test_malformed_falls_back_to_sandbox(self) -> None:
        assert project_isolation({"isolation": "docker"}) == "sandbox"
        assert project_isolation({"isolation": 1}) == "sandbox"

    def test_validation_accepts_host_and_rejects_other(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-isolation-") as tmp:
            root = Path(tmp)
            (root / "demo").mkdir()
            (root / "demo" / "project.yaml").write_text(
                "project:\n  name: demo\n  repos: []\n"
                "dev:\n  isolation: host\n", encoding="utf-8")
            loader = ConfigLoader(Settings(PROJECTS_DIR=tmp))
            assert loader.validate_project("demo") == []

            (root / "demo" / "project.yaml").write_text(
                "project:\n  name: demo\n  repos: []\n"
                "dev:\n  isolation: docker\n", encoding="utf-8")
            errors = loader.validate_project("demo")
            assert any("dev.isolation" in error for error in errors)

    def test_production_projects_use_the_declared_fence(self) -> None:
        loader = ConfigLoader(Settings())
        for project in ("jardal-erp", "jardal-openceo"):
            errors = loader.validate_project(project)
            assert errors == []
            assert project_isolation(
                loader.load_project(project)["dev"]) == "host"


class TestTierExecutorIsolation:
    def test_host_project_builders_run_unsandboxed(self) -> None:
        for tool in (CodingTool.TIER_A, CodingTool.TIER_B,
                     CodingTool.TIER_C):
            with patch.object(DevSessionManager, "_command_available",
                              return_value=True):
                executor = asyncio.run(
                    _manager()._resolve_tier_executor(
                        tool, _config("host")))
            assert executor.sandbox is False, tool

    def test_default_project_keeps_the_sandbox(self) -> None:
        with patch.object(DevSessionManager, "_command_available",
                          return_value=True):
            executor = asyncio.run(
                _manager()._resolve_tier_executor(
                    CodingTool.TIER_C, _config()))
        assert executor.sandbox is True

    def test_step_code_forwards_the_executor_fence(self) -> None:
        manager = _manager()
        seen: list[bool] = []

        async def fake_run_process(*args, **kwargs):
            seen.append(kwargs.get("sandbox"))
            return True, "ok", ""

        async def fake_overlays(*args, **kwargs):
            return {}, "badge", {}

        manager._run_process = fake_run_process  # type: ignore[method-assign]
        executor = TierExecutor(
            argv=["codexf", "exec", "--ephemeral", "-"],
            prompt_via="stdin", cwd="/tmp/repo", env={}, label="tier_b",
            sandbox=False)
        with tempfile.TemporaryDirectory(prefix="host-step-") as tmp:
            repo = Path(tmp)
            (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            config = _config("host")
            config["project"]["repos"][0]["local_path"] = str(repo)
            asyncio.run(manager._step_code_once(
                {"task_id": "t", "project": "demo", "title": "x"},
                config, CodingTool.TIER_B, executor=executor))
        assert seen == [False]


class TestHostProcess:
    def test_unsandboxed_run_skips_bwrap(self) -> None:
        manager = _manager()
        with tempfile.TemporaryDirectory(prefix="host-run-") as tmp:
            with patch("orchestration.agent_sandbox.plan_agent",
                       side_effect=AssertionError("plan_agent called")):
                result = asyncio.run(manager._run_process_detailed(
                    ["/bin/sh", "-c", "printf 'host:%s' \"$PWD\""],
                    timeout=30, cwd=tmp, env={}, sandbox=False))
        assert result.ok is True
        assert result.stdout.strip() == f"host:{tmp}"
        assert result.stderr == ""

    def test_process_executor_host_mode_builder_skips_bwrap(self) -> None:
        script = Path(tempfile.mkdtemp(prefix="host-exec-")) / "run.sh"
        script.write_text("#!/bin/sh\nprintf 'cwd:%s\\n' \"$PWD\"\n"
                          "printf 'prompt:%s\\n' \"$1\"\n", encoding="utf-8")
        script.chmod(0o755)
        executor = ProcessExecutor(
            roles={"builder": {"command": [str(script)],
                               "prompt_via": "argv"}},
            host_mode=True)
        with patch("orchestration.live.plan_agent",
                   side_effect=AssertionError("plan_agent called")):
            result = asyncio.run(executor.run(
                "builder", "hola", Path(script.parent), 30))
        assert result.returncode == 0
        assert f"cwd:{script.parent}" in result.output_tail
        assert "prompt:hola" in result.output_tail

    def test_process_executor_keeps_read_only_roles_fenced(self) -> None:
        executor = ProcessExecutor(
            roles={"planner": {"command": ["/bin/echo"],
                               "prompt_via": "argv"}},
            host_mode=True)
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "fenced"],
                               cwd="/tmp", env={"PATH": "/usr/bin:/bin"})
        with tempfile.TemporaryDirectory(prefix="host-planner-") as tmp:
            with patch("orchestration.live.plan_agent",
                       return_value=plan) as planned:
                result = asyncio.run(executor.run(
                    "planner", "x", Path(tmp), 30))
        assert result.returncode == 0
        assert result.output_tail == "fenced"
        planned.assert_called_once()

    def test_process_executor_default_still_fences_builder(self) -> None:
        executor = ProcessExecutor(
            roles={"builder": {"command": ["/bin/echo"],
                               "prompt_via": "argv"}})
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "sandboxed"],
                               cwd="/tmp", env={"PATH": "/usr/bin:/bin"})
        with tempfile.TemporaryDirectory(prefix="sandbox-builder-") as tmp:
            with patch("orchestration.live.plan_agent",
                       return_value=plan) as planned:
                result = asyncio.run(executor.run(
                    "builder", "x", Path(tmp), 30))
        assert result.returncode == 0
        assert result.output_tail == "sandboxed"
        planned.assert_called_once()
