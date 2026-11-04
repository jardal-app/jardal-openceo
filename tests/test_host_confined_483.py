"""Host-confined builders and the live-checkout guard (TASK-2026-00483).

``dev.isolation: host`` builders see the whole host (toolchain, Docker,
caches) yet keep their role overlays, die with their parent and cannot
gain privileges; simple tiers never branch over someone else's
uncommitted work. Real bwrap and git in temp dirs; no models, no network,
no Docker.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.session import DevSessionManager
from orchestration import agent_sandbox
from orchestration.agent_sandbox import plan_host_agent
from orchestration.live import ProcessExecutor
from research.runtimes.base import IsolationUnavailable
from state import CodingTool, TaskStatus

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None,
                                 reason="bwrap is not installed")
BASE_ENV = {"PATH": "/usr/bin:/bin"}


def _bwrap_available(value: bool):
    return patch.object(agent_sandbox.BubblewrapBackend, "available",
                        new_callable=PropertyMock, return_value=value)


def _run(plan) -> subprocess.CompletedProcess[str]:
    return subprocess.run(plan.argv, cwd=plan.cwd, env=plan.env,
                          capture_output=True, text=True, timeout=30)


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())


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


class TestPlanHostAgent:
    def test_shares_the_host_root_and_keeps_the_env_allowlist(
            self, tmp_path: Path) -> None:
        with _bwrap_available(True):
            plan = plan_host_agent(
                argv=["/bin/true"], workspace=tmp_path,
                base={**BASE_ENV, "OPENCEO_API_KEY": "secret"},
                overlay={"X_RUN": "1"})
        assert plan.argv[:6] == ["bwrap", "--die-with-parent",
                                 "--new-session", "--dev-bind", "/", "/"]
        assert plan.argv[-4:] == ["--chdir", str(tmp_path), "--", "/bin/true"]
        assert "OPENCEO_API_KEY" not in plan.env
        assert plan.env["X_RUN"] == "1"
        assert plan.env["GIT_TERMINAL_PROMPT"] == "0"
        assert plan.filesystem_isolated is False

    def test_refuses_without_bwrap(self, tmp_path: Path) -> None:
        with _bwrap_available(False), pytest.raises(IsolationUnavailable):
            plan_host_agent(argv=["/bin/true"], workspace=tmp_path,
                            base=BASE_ENV)

    def test_overlay_must_shadow_an_existing_home_file(
            self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        with _bwrap_available(True):
            with pytest.raises(ValueError, match="missing on the host"):
                plan_host_agent(
                    argv=["/bin/true"], workspace=work, base=BASE_ENV,
                    home=home,
                    home_file_overlays={".config/muse/settings.json": b"{}"})
            with pytest.raises(ValueError, match="escapes the home dir"):
                plan_host_agent(
                    argv=["/bin/true"], workspace=work, base=BASE_ENV,
                    home=home, home_file_overlays={"../x": b""})
        assert not (work / ".openceo").exists()

    def test_only_bind_ops_are_accepted(self, tmp_path: Path) -> None:
        with _bwrap_available(True), pytest.raises(
                ValueError, match="unsupported host-mode op"):
            plan_host_agent(argv=["/bin/true"], workspace=tmp_path,
                            base=BASE_ENV, extra_ops=[["--tmpfs", "/x"]])


@needs_bwrap
class TestHostConfinedRun:
    def test_role_overlay_shadows_the_global_config_only_inside(
            self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        settings = home / ".config" / "muse" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("global", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        plan = plan_host_agent(
            argv=["/bin/cat", str(settings)], workspace=work, base=BASE_ENV,
            home=home,
            home_file_overlays={".config/muse/settings.json": b"role-box"})
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "role-box"
        assert settings.read_text(encoding="utf-8") == "global"

    def test_host_files_stay_visible_and_the_workspace_writable(
            self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("host", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        plan = plan_host_agent(
            argv=["/bin/sh", "-c", f"cat {outside} && echo built > out.txt"],
            workspace=work, base=BASE_ENV)
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "host"
        assert (work / "out.txt").read_text(encoding="utf-8").strip() == "built"

    def test_no_privilege_can_be_gained(self, tmp_path: Path) -> None:
        plan = plan_host_agent(
            argv=["/bin/grep", "NoNewPrivs", "/proc/self/status"],
            workspace=tmp_path, base=BASE_ENV)
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["NoNewPrivs:", "1"]

    @pytest.mark.skipif(shutil.which("sudo") is None, reason="no sudo")
    def test_sudo_refuses_inside(self, tmp_path: Path) -> None:
        plan = plan_host_agent(argv=["sudo", "-n", "true"],
                               workspace=tmp_path, base=BASE_ENV)
        assert _run(plan).returncode != 0


class TestHostWiring:
    def test_live_builder_runs_host_confined_with_its_trace_binds(
            self, tmp_path: Path) -> None:
        executor = ProcessExecutor(
            roles={"builder": {"command": ["/bin/echo"], "provider": "claude",
                               "prompt_via": "argv"}},
            host_mode=True)
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "confined"],
                               cwd=str(tmp_path), env=dict(BASE_ENV))
        trace_ops = [["--bind", str(tmp_path / "trace"), "/x/projects"]]
        with patch("orchestration.live.session_trace.prepare",
                   return_value=(tmp_path / "trace", trace_ops)), \
             patch("orchestration.live.plan_host_agent",
                   return_value=plan) as planned, \
             patch("orchestration.live.plan_agent",
                   side_effect=AssertionError("sandbox fence used")):
            result = asyncio.run(executor.run("builder", "x", tmp_path, 30))
        assert result.returncode == 0
        assert result.output_tail == "confined"
        assert planned.call_args.kwargs["extra_ops"] == trace_ops
        assert planned.call_args.kwargs["workspace"] == tmp_path

    def test_simple_tier_host_run_keeps_the_role_overlays(
            self, tmp_path: Path) -> None:
        overlays = {".config/muse/settings.json": b"{}"}
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "ok"],
                               cwd=str(tmp_path), env=dict(BASE_ENV))
        with patch("orchestration.agent_sandbox.plan_host_agent",
                   return_value=plan) as planned, \
             patch("orchestration.agent_sandbox.plan_agent",
                   side_effect=AssertionError("sandbox fence used")):
            result = asyncio.run(_manager()._run_process_detailed(
                ["/bin/echo"], cwd=str(tmp_path), env={"A": "1"},
                sandbox=False, home_file_overlays=overlays))
        assert result.ok is True and result.stdout == "ok"
        assert planned.call_args.kwargs["home_file_overlays"] == overlays
        assert planned.call_args.kwargs["overlay"] == {"A": "1"}


class TestLiveCheckoutGuard:
    def test_clean_tree_and_untracked_scratch_are_free(self, repo: Path) -> None:
        assert DevSessionManager._foreign_changes_branch(repo) == ""
        (repo / ".openceo").mkdir()
        (repo / ".openceo" / "x").write_text("scratch", encoding="utf-8")
        assert DevSessionManager._foreign_changes_branch(repo) == ""

    def test_owner_edits_hold_the_ticket_and_stay_put(self, repo: Path) -> None:
        (repo / "a.txt").write_text("owner wip\n", encoding="utf-8")
        assert DevSessionManager._foreign_changes_branch(repo) == "main"
        out = _manager()._open_simple_branch(
            {"task_id": "t-1", "params": {"x": 1}}, {"dev": {}},
            CodingTool.TIER_C, repo)
        assert out.status == TaskStatus.WAITING
        assert out.waiting_reason.startswith("repo_busy: ")
        assert "uncommitted changes on main" in out.waiting_reason
        assert out.resume_at is not None
        assert out.params == {"x": 1}
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert (repo / "a.txt").read_text(encoding="utf-8") == "owner wip\n"

    def test_an_interrupted_ticket_branch_keeps_its_resume_path(
            self, repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "tier/tier_c-1-abc123")
        (repo / "a.txt").write_text("builder partial\n", encoding="utf-8")
        assert DevSessionManager._foreign_changes_branch(repo) == ""
