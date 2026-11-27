"""Host-mode builders without production Docker or secrets (TASK-2026-00985).

A ``dev.isolation: host`` builder keeps the host toolchain and home but sees
no other checkout, no service env file, no credential store, no agent socket
and no host process, and reaches Docker only through the agents' rootless
daemon. Real bwrap in temp dirs; no models, no network, no Docker calls.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.session import DevSessionManager
from orchestration import agent_sandbox
from orchestration.agent_sandbox import plan_host_agent
from orchestration.live import ProcessExecutor

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None,
                                 reason="bwrap is not installed")
BASE_ENV = {"PATH": "/usr/bin:/bin"}


def _run(plan) -> subprocess.CompletedProcess[str]:
    return subprocess.run(plan.argv, cwd=plan.cwd, env=plan.env,
                          capture_output=True, text=True, timeout=30)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n" if text else "", encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    """A home with a code root holding the builder's repo and its siblings."""
    home = tmp_path / "home"
    repo = home / "development" / "org" / "repo"
    repo.mkdir(parents=True)
    _write(repo / "src.txt", "code")
    _write(repo / ".env", "REPO_SECRET=1")
    _write(repo / "apps" / "api" / ".env.local", "DB=1")
    _write(repo / ".env.example", "DB=<placeholder>")
    _write(repo / ".runtime" / "token", "t")
    sibling = _write(home / "development" / "org" / "other" / "notes.txt",
                     "client data")
    _write(home / "development" / "org" / "other" / ".env", "OTHER=1")
    vault = home / "development" / "org" / "vault" / "projects" / "repo"
    _write(vault / "hub.md", "hub")
    _write(home / "development" / "org" / "vault" / "projects" / "x" / "n.md",
           "other project note")
    _write(home / ".ssh" / "id_ed25519", "PRIVATE")
    _write(home / ".docker" / "config.json", '{"auths": {"r": {}}}')
    _write(home / ".git-credentials", "https://u:p@example.invalid")
    _write(home / ".config" / "tool" / "settings", "visible")
    return SimpleNamespace(home=home, repo=repo, sibling=sibling, vault=vault)


@needs_bwrap
class TestHostModeView:
    def test_only_the_workspace_and_read_roots_survive_the_code_root(
            self, layout: SimpleNamespace) -> None:
        org = layout.repo.parent
        plan = plan_host_agent(
            argv=["/bin/sh", "-c",
                  f"ls {org}; ls {org}/vault/projects; cat {layout.vault}/hub.md;"
                  f" echo built > out.txt; touch {layout.vault}/w 2>/dev/null"
                  " && echo vault-writable || echo vault-ro"],
            workspace=layout.repo, base=BASE_ENV, home=layout.home,
            read_roots=[layout.vault])
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["repo", "vault", "repo", "hub",
                                         "vault-ro"]
        assert (layout.repo / "out.txt").read_text().strip() == "built"
        assert layout.sibling.read_text().strip() == "client data"

    def test_env_files_runtime_and_home_credentials_read_empty(
            self, layout: SimpleNamespace) -> None:
        home, repo = layout.home, layout.repo
        script = "; ".join([
            "wc -c < .env", "wc -c < apps/api/.env.local",
            "cat .env.example", "ls -A .runtime | wc -l",
            f"ls -A {home}/.ssh | wc -l", f"cat {home}/.docker/config.json",
            f"wc -c < {home}/.git-credentials", f"cat {home}/.config/tool/settings",
        ])
        plan = plan_host_agent(argv=["/bin/sh", "-c", script],
                               workspace=repo, base=BASE_ENV, home=home)
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [
            "0", "0", "DB=<placeholder>", "0", "0", "{}", "0", "visible"]
        assert (home / ".ssh" / "id_ed25519").read_text().strip() == "PRIVATE"

    def test_host_processes_are_out_of_sight(self, tmp_path: Path) -> None:
        plan = plan_host_agent(
            argv=["/bin/sh", "-c",
                  f"test -e /proc/{os.getpid()} && echo seen || echo hidden"],
            workspace=tmp_path, base=BASE_ENV)
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "hidden"

    @pytest.mark.skipif(not Path("/run/docker.sock").exists(),
                        reason="no production Docker socket on this host")
    def test_the_production_docker_socket_is_masked(
            self, tmp_path: Path) -> None:
        plan = plan_host_agent(
            argv=["/bin/sh", "-c",
                  "test -S /var/run/docker.sock && echo socket || echo masked"],
            workspace=tmp_path, base=BASE_ENV)
        result = _run(plan)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "masked"


class TestHostModePlan:
    def test_runtime_masks_cover_agent_sockets(self, tmp_path: Path) -> None:
        uid = os.getuid()
        empty = _write(tmp_path / "empty", "")
        sock = _write(tmp_path / "docker.sock", "")
        (tmp_path / "run" / str(uid)).mkdir(parents=True)
        tmp = tmp_path / "tmp"
        (tmp / f"tmux-{uid}").mkdir(parents=True)
        (tmp / "ssh-abc").mkdir()
        (tmp / "ssh-link").symlink_to(tmp / "ssh-abc")
        ops = agent_sandbox._host_runtime_masks(
            empty, uid, docker_socket=sock, run_user=tmp_path / "run", tmp=tmp)
        assert ops == [
            ["--ro-bind", str(empty), str(sock)],
            ["--perms", "0700", "--tmpfs", str(tmp_path / "run" / str(uid))],
            ["--tmpfs", str(tmp / f"tmux-{uid}")],
            ["--tmpfs", str(tmp / "ssh-abc")],
        ]

    def test_docker_host_targets_the_agent_daemon_or_nothing(
            self, tmp_path: Path) -> None:
        prod = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
        agent = Path("/run/user/4242/docker.sock")
        with patch.object(agent_sandbox, "agent_docker_socket",
                          return_value=agent):
            plan = plan_host_agent(argv=["/bin/true"], workspace=tmp_path,
                                   base=BASE_ENV, overlay=prod)
        assert plan.env["DOCKER_HOST"] == f"unix://{agent}"
        with patch.object(agent_sandbox, "agent_docker_socket",
                          return_value=None):
            plan = plan_host_agent(argv=["/bin/true"], workspace=tmp_path,
                                   base=BASE_ENV, overlay=prod)
        assert "DOCKER_HOST" not in plan.env
        assert any("Docker is unavailable" in note for note in plan.notes)

    def test_code_root_is_a_top_level_home_folder(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        for rel in ("development/org/repo", "solo", ".codex/worktrees/w"):
            (home / rel).mkdir(parents=True)
        root = agent_sandbox._host_code_root
        assert root(home / "development/org/repo", home) == (
            home / "development").resolve()
        assert root(home / "solo", home) is None
        assert root(home / ".codex/worktrees/w", home) is None
        assert root(tmp_path, home) is None


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())


class TestHostModeWiring:
    def test_simple_tier_host_run_forwards_its_read_roots(
            self, tmp_path: Path) -> None:
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "ok"],
                               cwd=str(tmp_path), env=dict(BASE_ENV))
        with patch("orchestration.agent_sandbox.plan_host_agent",
                   return_value=plan) as planned:
            result = asyncio.run(_manager()._run_process_detailed(
                ["/bin/echo"], cwd=str(tmp_path), sandbox=False,
                extra_read_roots=["/vault/projects/p"]))
        assert result.ok is True
        assert planned.call_args.kwargs["read_roots"] == ["/vault/projects/p"]

    def test_live_builder_forwards_its_declared_roots(
            self, tmp_path: Path) -> None:
        executor = ProcessExecutor(
            roles={"builder": {"command": ["/bin/echo"], "prompt_via": "argv",
                               "extra_read_roots": ["/vault/p"]}},
            host_mode=True)
        plan = SimpleNamespace(argv=["/bin/echo", "-n", "x"],
                               cwd=str(tmp_path), env=dict(BASE_ENV))
        with patch("orchestration.live.plan_host_agent",
                   return_value=plan) as planned:
            result = asyncio.run(executor.run("builder", "x", tmp_path, 30))
        assert result.returncode == 0
        assert planned.call_args.kwargs["read_roots"] == ["/vault/p"]
        assert planned.call_args.kwargs["write_roots"] == ()
