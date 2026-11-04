"""Read-only roles stay in role and never relaunch our own timeout (00443).

Contract fixed here: the executor leads every planner/reviewer prompt with
its role contract (the builder passes untouched), masks the Owner's
user-level instruction files for those roles, and treats its own wall-clock
timeout as final for them; the Tier S planner parks for the Owner instead
of relaunching, and a plan review without observations costs no planner
session.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from orchestration.agent_sandbox import user_instruction_masks  # noqa: E402
from orchestration.live import (  # noqa: E402
    TIMEOUT_RETURNCODE,
    ExecResult,
    ProcessExecutor,
    executor_timed_out,
)
from orchestration.role_contracts import (  # noqa: E402
    READ_ONLY_ROLES,
    is_read_only,
    with_role_contract,
)

OWN_TIMEOUT = ExecResult(
    TIMEOUT_RETURNCODE, 2400.0,
    output_tail="timeout after 2400s: claudej --print",
    usage={"timeout": True})


def _ctx(repo: Path, **over: Any) -> Any:
    base: dict[str, Any] = dict(
        ticket={"task_id": "t", "title": "T", "description": "D",
                "acceptance": ["a1"], "params": {}},
        repo=repo, ref="t", run_id="run", branch="b", base_branch="main",
        base_sha="s", budgets={}, timeouts={}, waiting_cfg={},
        auto_merge=True, budget_exclude=[], gate_specs=[], roles={})
    base.update(over)
    return cycle_mod._Ctx(**base)  # noqa: SLF001


class RoleContractTextTests(unittest.TestCase):
    def test_every_read_only_role_gets_boundaries(self) -> None:
        for role in sorted(READ_ONLY_ROLES):
            with self.subTest(role=role):
                text = with_role_contract(role, "BODY")
                self.assertTrue(text.startswith("ROLE CONTRACT: "))
                self.assertIn("running tests", text)
                self.assertIn("this contract prevails", text)
                self.assertTrue(text.endswith("\n\nBODY"))

    def test_executor_role_names_map_to_contracts(self) -> None:
        self.assertIn("code reviewer",
                      with_role_contract("review:gemini", "x"))
        self.assertIn("plan reviewer",
                      with_role_contract("plan-reviewer", "x"))
        self.assertTrue(is_read_only("review:mimo"))

    def test_builder_and_unknown_roles_pass_untouched(self) -> None:
        for role in ("builder", "dev", "finance"):
            with self.subTest(role=role):
                self.assertEqual(with_role_contract(role, "BODY"), "BODY")
                self.assertFalse(is_read_only(role))

    def test_planner_contract_sets_no_length_target(self) -> None:
        text = with_role_contract("planner", "x")
        self.assertIn("Length follows the ticket, never a target", text)
        self.assertNotIn("lines", text)


class ExecutorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_leads_read_only_prompts_only(self) -> None:
        executor = ProcessExecutor()
        executor._run_once = AsyncMock(  # noqa: SLF001
            return_value=ExecResult(0, 1.0, output_tail="ok"))
        with tempfile.TemporaryDirectory() as tmp:
            await executor.run("planner", "PLAN BODY", Path(tmp), 30)
            await executor.run("builder", "BUILD BODY", Path(tmp), 30)
        planner_prompt = executor._run_once.call_args_list[0].args[1]  # noqa: SLF001
        builder_prompt = executor._run_once.call_args_list[1].args[1]  # noqa: SLF001
        self.assertTrue(planner_prompt.startswith(
            "ROLE CONTRACT: Tier S planner"))
        self.assertTrue(planner_prompt.endswith("PLAN BODY"))
        self.assertEqual(builder_prompt, "BUILD BODY")

    async def test_own_timeout_is_final_for_read_only_roles(self) -> None:
        executor = ProcessExecutor()
        executor._run_once = AsyncMock(return_value=OWN_TIMEOUT)  # noqa: SLF001
        with patch("orchestration.live.asyncio.sleep",
                   new_callable=AsyncMock) as sleep, \
                tempfile.TemporaryDirectory() as tmp:
            result = await executor.run("planner", "p", Path(tmp), 2400)
        executor._run_once.assert_awaited_once()  # noqa: SLF001
        sleep.assert_not_awaited()
        self.assertTrue(executor_timed_out(result))

    async def test_builder_still_resumes_after_own_timeout(self) -> None:
        executor = ProcessExecutor()
        executor._run_once = AsyncMock(side_effect=[  # noqa: SLF001
            OWN_TIMEOUT, ExecResult(0, 1.0, output_tail="done")])
        with patch("orchestration.live.asyncio.sleep",
                   new_callable=AsyncMock) as sleep, \
                tempfile.TemporaryDirectory() as tmp:
            result = await executor.run("builder", "b", Path(tmp), 60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [300])

    async def test_provider_outage_still_retries_read_only_roles(self) -> None:
        executor = ProcessExecutor()
        executor._run_once = AsyncMock(side_effect=[  # noqa: SLF001
            ExecResult(1, 1.0, output_tail="503 service unavailable"),
            ExecResult(0, 1.0, output_tail="ok")])
        with patch("orchestration.live.asyncio.sleep",
                   new_callable=AsyncMock), \
                tempfile.TemporaryDirectory() as tmp:
            result = await executor.run("arbiter", "a", Path(tmp), 600)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(executor._run_once.await_count, 2)  # noqa: SLF001


class UserInstructionMaskTests(unittest.IsolatedAsyncioTestCase):
    def test_masks_shared_real_files_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".claude").mkdir(parents=True)
            shared = home / ".claude" / "CLAUDE.md"
            shared.write_text("owner rules", encoding="utf-8")
            for alias in (".claude-j/CLAUDE.md", ".codex-j/AGENTS.md"):
                (home / alias).parent.mkdir(parents=True)
                (home / alias).symlink_to(shared)
            (home / ".gemini").mkdir()
            (home / ".gemini" / "GEMINI.md").symlink_to(home / "missing.md")
            outside = Path(tmp) / "outside.md"
            outside.write_text("x", encoding="utf-8")
            (home / ".config" / "opencode").mkdir(parents=True)
            (home / ".config" / "opencode" / "AGENTS.md").symlink_to(outside)
            (home / ".qwen").mkdir()
            (home / ".qwen" / "QWEN.md").write_text("q", encoding="utf-8")
            masks = user_instruction_masks(home)
        self.assertEqual(masks, {".claude/CLAUDE.md": b"",
                                 ".qwen/QWEN.md": b""})

    async def _overlays_for(self, role: str) -> Any:
        spec = {"provider": "claude", "account": "claudej", "model": "opus",
                "command": ["true"], "headless_args": ["--print"],
                "prompt_via": "argv"}
        executor = ProcessExecutor(roles={role: spec})
        with patch("orchestration.live.user_instruction_masks",
                   return_value={".claude/CLAUDE.md": b""}), \
                patch("orchestration.live.plan_agent",
                      side_effect=ValueError("stop")) as planned, \
                tempfile.TemporaryDirectory() as tmp:
            result = await executor._run_once(  # noqa: SLF001
                role, "p", Path(tmp), 5)
        self.assertEqual(result.output_tail, "sandbox refused: stop")
        return planned.call_args.kwargs["home_file_overlays"]

    async def test_read_only_roles_run_without_owner_rules(self) -> None:
        self.assertEqual(await self._overlays_for("planner"),
                         {".claude/CLAUDE.md": b""})

    async def test_builder_keeps_user_instructions(self) -> None:
        self.assertIsNone(await self._overlays_for("builder"))


class PlannerCycleTests(unittest.IsolatedAsyncioTestCase):
    def _cycle(self, *results: ExecResult) -> Any:
        executor = MagicMock()
        executor.run = AsyncMock(side_effect=list(results))
        runs = MagicMock()
        runs.patch_run = AsyncMock()
        cycle = cycle_mod.TierSCycle(runs, MagicMock(), MagicMock(), executor)
        cycle._record_session = MagicMock()  # noqa: SLF001
        cycle._park = AsyncMock()  # noqa: SLF001
        return cycle

    async def test_own_timeout_parks_for_owner_without_relaunch(self) -> None:
        cycle = self._cycle(OWN_TIMEOUT)
        with tempfile.TemporaryDirectory() as tmp:
            result = await cycle._planning(  # noqa: SLF001
                _ctx(Path(tmp), timeouts={"planner": 40}))
        self.assertIsNone(result)
        cycle._exec.run.assert_awaited_once()  # noqa: SLF001
        args, kwargs = cycle._park.call_args  # noqa: SLF001
        self.assertEqual(args[1], "needs_owner")
        self.assertIn("40-min budget", args[2])
        self.assertNotIn("resume_in_min", kwargs)

    async def test_review_without_observations_skips_planner(self) -> None:
        cycle = self._cycle(ExecResult(0, 1.0, output_tail="No findings."))
        plan = SimpleNamespace(goal="g", files=[])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(Path(tmp), timeouts={"plan_review": 15})
            await cycle._plan_review(ctx, plan)  # noqa: SLF001
        roles = [c.args[0] for c in cycle._exec.run.call_args_list]  # noqa: SLF001
        self.assertEqual(roles, ["plan_reviewer"])
        cycle._runs.patch_run.assert_awaited_once()  # noqa: SLF001


class CodexHomeTests(unittest.TestCase):
    def test_codexj_gets_per_run_home_with_persistent_auth(self) -> None:
        from orchestration.agent_sandbox import _codex_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            account = home / ".codex-j"
            (account / "plugins").mkdir(parents=True)
            for name in ("config.toml", "auth.json", "installation_id",
                         "models_cache.json", "state_5.sqlite",
                         "memories_1.sqlite", "history.jsonl"):
                (account / name).write_text("x", encoding="utf-8")
            (account / "AGENTS.md").symlink_to(home / "rules.md")
            ops = _codex_state_ops(home, ["codexj", "exec", "-"])
            self.assertEqual(ops[0], ["--tmpfs", str(account)])
            self.assertIn(["--ro-bind", str(account / "config.toml"),
                           str(account / "config.toml")], ops)
            for writable in ("auth.json", "installation_id",
                             "models_cache.json"):
                self.assertIn(["--bind", str(account / writable),
                               str(account / writable)], ops)
            mounted = {Path(op[-1]).name for op in ops[1:]}
            self.assertEqual(mounted, {"config.toml", "plugins", "auth.json",
                                       "installation_id", "models_cache.json"})
            self.assertEqual(_codex_state_ops(home, ["agyj", "--print"]), [])

    def test_codexf_gets_the_same_per_run_home_over_dot_codex(self) -> None:
        # codexj lapsed and codexf took over (TASK-2026-00475).
        from orchestration.agent_sandbox import _codex_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            account = home / ".codex"
            account.mkdir()
            for name in ("config.toml", "auth.json", "installation_id",
                         "models_cache.json", "AGENTS.md"):
                (account / name).write_text("x", encoding="utf-8")
            ops = _codex_state_ops(home, ["/x/.local/bin/codexf", "exec"])
            self.assertEqual(ops[0], ["--tmpfs", str(account)])
            for writable in ("auth.json", "installation_id",
                             "models_cache.json"):
                self.assertIn(["--bind", str(account / writable),
                               str(account / writable)], ops)
            mounted = {Path(op[-1]).name for op in ops[1:]}
            self.assertEqual(mounted, {"config.toml", "auth.json",
                                       "installation_id", "models_cache.json"})

    def test_motor_under_shadowed_home_is_reexposed(self) -> None:
        # The standalone motor lives inside ~/.codex: the per-run tmpfs
        # would hide it and `codex` would fall through to a stale system
        # install (TASK-2026-00477).
        from orchestration.agent_sandbox import _codex_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            account = home / ".codex"
            motor_bin = (account / "packages/standalone/releases/0.166.0/bin")
            motor_bin.mkdir(parents=True)
            (motor_bin / "codex").write_text("x", encoding="utf-8")
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/codex").symlink_to(motor_bin / "codex")
            (account / "config.toml").write_text("x", encoding="utf-8")
            ops = _codex_state_ops(home, ["codexf", "exec"])
            for level in ("packages", "packages/standalone",
                          "packages/standalone/releases",
                          "packages/standalone/releases/0.166.0",
                          "packages/standalone/releases/0.166.0/bin"):
                self.assertIn(["--tmpfs", str(account / level)], ops)
            self.assertIn(["--ro-bind", str(motor_bin), str(motor_bin)], ops)

    def test_motor_outside_shadowed_home_needs_no_ops(self) -> None:
        # codexj's motor stays visible (its account dir is not ~/.codex),
        # so no motor mounts are emitted (TASK-2026-00477).
        from orchestration.agent_sandbox import _codex_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            account = home / ".codex-j"
            account.mkdir()
            motor_bin = home / ".codex/packages/standalone/bin"
            motor_bin.mkdir(parents=True)
            (motor_bin / "codex").write_text("x", encoding="utf-8")
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/codex").symlink_to(motor_bin / "codex")
            (account / "config.toml").write_text("x", encoding="utf-8")
            ops = _codex_state_ops(home, ["codexj", "exec", "-"])
            self.assertNotIn(["--tmpfs", str(home / ".codex/packages")], ops)
            self.assertFalse([op for op in ops
                              if op[0] == "--ro-bind"
                              and "packages" in op[-1]])
