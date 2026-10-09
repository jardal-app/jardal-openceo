"""The orchestrator RUNS the frozen oracle (TASK-2026-00178, policy §24).

Before this ticket the frozen oracle was only hash-compared: a builder
could break the cited tests without touching them and still read
"oracle ok". Contract:

- ``oracle_run_plan``: executable oracle types run through the project's
  ``dev.oracle_cmd`` (refs shell-quoted); static artifacts, missing refs
  and a missing template yield a visible "hash-protected only" note.
- ``validate_oracle_config`` shapes ``dev.oracle_cmd`` /
  ``dev.oracle_timeout_minutes``.
- ``_finish_simple_tool`` appends the oracle as a required post-build
  gate with the gates' retries; a retry that edits the oracle is a
  material failure; the session's params survive whole.
- The Tier C chain link that ran a ticket is recorded (policy §25/§32).

Real git repos and real (token-free) shell gates. No network, no CLI,
no LLM.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    CodingStepResult,
    DevExecutionOutput,
    DevSessionManager,
    _SimpleBranch,
)
from agents.dev.tier_oracle import (  # noqa: E402
    DEFAULT_ORACLE_TIMEOUT_MINUTES,
    EXECUTABLE_ORACLE_TYPES,
    freeze_oracle,
    oracle_run_plan,
    validate_oracle_config,
)
from config_loader import ConfigLoader  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    blank_tier_c_telemetry,
    record_tier_c,
)
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402

_PROJECTS = Path(__file__).resolve().parents[1] / "projects"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", check=True)
    return proc.stdout.strip()


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@e.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_oracle.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _oracle(oracle_type: str = "preexisting_tests",
            refs: Any = ("tests/test_oracle.py",)) -> dict[str, Any]:
    return {"type": oracle_type, "refs": [{"ref": ref} for ref in refs]}


def _manager() -> DevSessionManager:
    manager = DevSessionManager(Settings(), MagicMock())
    manager._task_service = None
    manager._rate_monitor = None
    manager._repo = None
    return manager


def _dev_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "project": {"repos": [{"branch_base": "main"}]},
        "dev": {"tier_s": {"budgets": {"gate_retries": 0}}},
    }
    config["dev"].update(overrides)
    return config


def _loose_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-o1", "ref": "o1", "title": "Oracle work",
        "coding_tool": "tier_c", "project": "proj",
        "params": {"oracle": _oracle(), "tier_reason": "specified work",
                   "work_type": "implementation"},
    }
    task.update(overrides)
    return task


def _open_work_branch(repo: Path) -> str:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "work")
    return base


def _write_handoff(repo: Path) -> None:
    folder = repo / ".tier-s"
    folder.mkdir(exist_ok=True)
    (folder / "HANDOFF.md").write_text(
        "# Handoff\nSHA:\nFiles changed:\nGates:\nDone.\n", encoding="utf-8")


def _frozen(refs: Any = None, oracle_type: str = "preexisting_tests",
            ) -> dict[str, Any]:
    refs = refs if refs is not None else [
        {"ref": "tests/test_oracle.py", "path": "tests/test_oracle.py",
         "sha256": "x", "resolved": True}]
    return {"oracle_type": oracle_type, "task_ref": "o1",
            "base_sha": "abc", "frozen_at": "now", "refs": refs}


class OracleRunPlanTests(unittest.TestCase):
    def test_nothing_frozen_says_nothing(self) -> None:
        self.assertEqual(oracle_run_plan(None, {"oracle_cmd": "t {refs}"}),
                         (None, ""))

    def test_executable_types_run_through_the_project_command(self) -> None:
        for oracle_type in sorted(EXECUTABLE_ORACLE_TYPES):
            with self.subTest(oracle_type=oracle_type):
                run, note = oracle_run_plan(
                    _frozen(oracle_type=oracle_type),
                    {"oracle_cmd": "pytest {refs} -q"})
                assert run is not None
                self.assertEqual(note, "")
                self.assertEqual(run.cmd, "pytest tests/test_oracle.py -q")
                self.assertEqual(run.refs, ("tests/test_oracle.py",))
                self.assertEqual(run.timeout_minutes,
                                 DEFAULT_ORACLE_TIMEOUT_MINUTES)

    def test_static_artifacts_are_hash_protected_only(self) -> None:
        run, note = oracle_run_plan(
            _frozen(oracle_type="contract_or_schema"),
            {"oracle_cmd": "pytest {refs}"})
        self.assertIsNone(run)
        self.assertIn("hash-protected only", note)
        self.assertIn("contract_or_schema", note)

    def test_missing_command_is_a_visible_limit_not_silence(self) -> None:
        for dev_cfg in (None, {}, {"oracle_cmd": ""}, {"oracle_cmd": "  "}):
            with self.subTest(dev_cfg=dev_cfg):
                run, note = oracle_run_plan(_frozen(), dev_cfg)
                self.assertIsNone(run)
                self.assertIn("dev.oracle_cmd is not configured", note)

    def test_unresolved_refs_cannot_run(self) -> None:
        run, note = oracle_run_plan(
            _frozen(refs=[{"ref": "spec text", "resolved": False}]),
            {"oracle_cmd": "pytest {refs}"})
        self.assertIsNone(run)
        self.assertIn("no file-backed refs", note)

    def test_only_resolved_refs_are_passed_deduplicated_with_nodes(self) -> None:
        run, _ = oracle_run_plan(
            _frozen(refs=[
                {"ref": "tests/a.py::test_one", "path": "tests/a.py",
                 "sha256": "x", "resolved": True},
                {"ref": "tests/a.py::test_one", "path": "tests/a.py",
                 "sha256": "x", "resolved": True},
                {"ref": "tests/gone.py", "resolved": False},
                {"ref": "tests/b.py", "path": "tests/b.py",
                 "sha256": "y", "resolved": True},
            ]),
            {"oracle_cmd": "pytest {refs}"})
        assert run is not None
        self.assertEqual(run.refs, ("tests/a.py::test_one", "tests/b.py"))
        self.assertEqual(run.cmd, "pytest tests/a.py::test_one tests/b.py")

    def test_refs_are_shell_quoted_so_a_ref_is_never_a_command(self) -> None:
        run, _ = oracle_run_plan(
            _frozen(refs=[{"ref": "tests/a.py::x; touch /tmp/pwned",
                           "path": "tests/a.py", "sha256": "x",
                           "resolved": True}]),
            {"oracle_cmd": "pytest {refs}"})
        assert run is not None
        self.assertEqual(run.cmd,
                         "pytest 'tests/a.py::x; touch /tmp/pwned'")

    def test_braces_in_the_template_survive(self) -> None:
        run, _ = oracle_run_plan(
            _frozen(), {"oracle_cmd": "runner --opts '{\"a\": 1}' {refs}"})
        assert run is not None
        self.assertEqual(
            run.cmd, "runner --opts '{\"a\": 1}' tests/test_oracle.py")

    def test_timeout_comes_from_config_when_valid(self) -> None:
        run, _ = oracle_run_plan(
            _frozen(), {"oracle_cmd": "t {refs}", "oracle_timeout_minutes": 25})
        assert run is not None
        self.assertEqual(run.timeout_minutes, 25)
        for bad in (0, -3, "10", True, 1.5, None):
            with self.subTest(bad=bad):
                run, _ = oracle_run_plan(
                    _frozen(),
                    {"oracle_cmd": "t {refs}", "oracle_timeout_minutes": bad})
                assert run is not None
                self.assertEqual(run.timeout_minutes,
                                 DEFAULT_ORACLE_TIMEOUT_MINUTES)

    def test_unreadable_record_is_not_executed(self) -> None:
        run, note = oracle_run_plan("garbage", {"oracle_cmd": "t {refs}"})
        self.assertIsNone(run)
        self.assertIn("unreadable", note)


class OracleConfigValidationTests(unittest.TestCase):
    def test_absent_or_null_is_valid(self) -> None:
        self.assertEqual(validate_oracle_config(None), [])
        self.assertEqual(validate_oracle_config({}), [])
        self.assertEqual(validate_oracle_config(
            {"oracle_cmd": None, "oracle_timeout_minutes": None}), [])

    def test_a_good_block_is_valid(self) -> None:
        self.assertEqual(validate_oracle_config(
            {"oracle_cmd": "pytest {refs} -q", "oracle_timeout_minutes": 15}),
            [])

    def test_command_needs_the_refs_placeholder(self) -> None:
        errors = validate_oracle_config({"oracle_cmd": "pytest tests/"})
        self.assertEqual(errors, ["dev.oracle_cmd must contain the {refs} placeholder"])

    def test_command_must_be_a_non_empty_string(self) -> None:
        for bad in ("", "   ", 5, ["pytest", "{refs}"], True):
            with self.subTest(bad=bad):
                self.assertEqual(
                    validate_oracle_config({"oracle_cmd": bad}),
                    ["dev.oracle_cmd must be a non-empty string"])

    def test_timeout_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, "10", True, 2.5):
            with self.subTest(bad=bad):
                self.assertEqual(
                    validate_oracle_config({"oracle_timeout_minutes": bad}),
                    ["dev.oracle_timeout_minutes must be a positive integer"])

    def test_shipped_project_config_is_valid_and_wired(self) -> None:
        import yaml
        cfg = yaml.safe_load(
            (_PROJECTS / "jardal-openceo" / "project.yaml").read_text(
                encoding="utf-8"))["dev"]
        self.assertEqual(validate_oracle_config(cfg), [])
        self.assertIn("{refs}", cfg["oracle_cmd"])
        self.assertIn("pytest", cfg["oracle_cmd"])

    def test_validate_project_reports_a_bad_block(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "demo"
            folder.mkdir()
            (folder / "project.yaml").write_text(
                "dev:\n  oracle_cmd: pytest tests/\n"
                "  oracle_timeout_minutes: 0\n", encoding="utf-8")
            loader = ConfigLoader.__new__(ConfigLoader)
            loader._resolve_projects_root = lambda: Path(root)  # type: ignore[method-assign]
            errors = loader.validate_project("demo")
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(any("{refs}" in err for err in errors))
        self.assertTrue(any("oracle_timeout_minutes" in err for err in errors))
        self.assertTrue(all(err.startswith("demo/project.yaml:") for err in errors))


class OracleGateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Real git + real shell gates through ``_finish_simple_tool``."""

    #: Exits 0 only when handed exactly the frozen ref.
    _ARGS_CHECK = ("python3 -c \"import sys; "
                   "sys.exit(0 if sys.argv[1:] == ['tests/test_oracle.py'] "
                   "else 3)\" {refs}")

    def _prepared(self, *, edit: str = "VALUE = 2\n") -> tuple[Path, str, dict]:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        frozen = freeze_oracle(repo, base, _oracle(), "o1")
        assert frozen is not None
        (repo / "app.py").write_text(edit, encoding="utf-8")
        _write_handoff(repo)
        return repo, base, frozen

    async def _finish(self, repo: Path, base: str, frozen: dict,
                      config: dict, task: dict | None = None,
                      manager: DevSessionManager | None = None,
                      ) -> DevExecutionOutput:
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="o1", oracle_frozen=frozen)
        return await (manager or _manager())._finish_simple_tool(
            task or _loose_task(), config, CodingTool.TIER_C,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)

    @staticmethod
    def _gates_detail(output: DevExecutionOutput) -> str:
        return " | ".join(step["detail"] for step in output.steps
                          if step["step"] == "gates")

    async def test_passing_oracle_runs_with_the_frozen_refs_and_closes(self) -> None:
        repo, base, frozen = self._prepared()
        output = await self._finish(
            repo, base, frozen, _dev_config(oracle_cmd=self._ARGS_CHECK))
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        self.assertIn("oracle=pass", self._gates_detail(output))
        self.assertEqual(
            _git(repo, "show", "main:app.py"), "VALUE = 2")

    async def test_failing_oracle_is_a_material_failure(self) -> None:
        repo, base, frozen = self._prepared()
        output = await self._finish(
            repo, base, frozen, _dev_config(oracle_cmd="false {refs}"))
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("oracle=fail", self._gates_detail(output))
        self.assertIn("gates failed after 0 retries: oracle",
                      output.last_response_summary or "")
        # Nothing merged: main still carries the base content.
        self.assertEqual(_git(repo, "show", "main:app.py"), "VALUE = 1")

    async def test_oracle_failure_is_the_builders_to_fix_on_retry(self) -> None:
        repo, base, frozen = self._prepared(edit="VALUE = 1\n")
        # The builder only changes the file the oracle checks on retry.
        oracle_cmd = 'grep -q "VALUE = 2" app.py && echo {refs}'
        (repo / "notes.txt").write_text("x\n", encoding="utf-8")
        manager = _manager()

        async def fix_the_code(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        followup = AsyncMock(side_effect=fix_the_code)
        with patch.object(manager, "_simple_followup", new=followup):
            output = await self._finish(
                repo, base, frozen,
                _dev_config(oracle_cmd=oracle_cmd,
                            tier_s={"budgets": {"gate_retries": 1}}),
                manager=manager)
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        followup.assert_awaited_once()
        prompt = followup.await_args.args[-1]
        self.assertIn("[oracle]", prompt)
        self.assertIn("Never edit the frozen oracle", prompt)

    async def test_retry_that_edits_the_oracle_is_caught(self) -> None:
        repo, base, frozen = self._prepared(edit="VALUE = 1\n")
        (repo / "notes.txt").write_text("x\n", encoding="utf-8")
        manager = _manager()

        async def weaken_the_oracle(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            (repo / "tests" / "test_oracle.py").write_text(
                "def test_value():\n    assert True  # weakened\n",
                encoding="utf-8")

        with patch.object(manager, "_simple_followup",
                          new=AsyncMock(side_effect=weaken_the_oracle)):
            output = await self._finish(
                repo, base, frozen,
                _dev_config(oracle_cmd="false {refs}",
                            tier_s={"budgets": {"gate_retries": 2}}),
                manager=manager)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("oracle modified", output.error or "")
        kinds = [step["step"] for step in output.steps]
        self.assertIn("oracle", kinds)
        # Failed on the recheck right after the first retry turn, not after
        # burning the second retry.
        self.assertEqual(self._gates_detail(output).count("oracle=fail"), 1)

    async def test_without_a_command_the_limit_is_visible(self) -> None:
        repo, base, frozen = self._prepared()
        output = await self._finish(repo, base, frozen, _dev_config())
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        (skipped,) = [step for step in output.steps
                      if step["step"] == "oracle_run"]
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("dev.oracle_cmd is not configured", skipped["detail"])
        self.assertNotIn("oracle=", self._gates_detail(output))

    async def test_tamper_message_names_where_the_oracle_came_from(self) -> None:
        for assigned_by, expected in (
                ("loose_classifier/v3", "frozen from the ticket text"),
                (None, "declared by the planner")):
            with self.subTest(origin=expected):
                repo, base, frozen = self._prepared()
                (repo / "tests" / "test_oracle.py").write_text(
                    "def test_value():\n    assert False\n", encoding="utf-8")
                task = _loose_task()
                if assigned_by:
                    task["params"] = {**task["params"],
                                      "tier_assigned_by": assigned_by}
                output = await self._finish(
                    repo, base, frozen, _dev_config(), task=task)
                self.assertEqual(output.status, TaskStatus.FAILED)
                self.assertIn(expected, output.error or "")

    async def test_session_params_stay_whole_without_review_links(self) -> None:
        # output.params is None when no review link changed: the frozen
        # oracle must be added to the FULL task params, not replace them.
        repo, base, frozen = self._prepared()
        task = _loose_task()
        task["params"] = {**task["params"], "why_not_c": None,
                          "tier_assigned_by": "loose_classifier/v3"}
        output = await self._finish(
            repo, base, frozen, _dev_config(), task=task)
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        assert output.params is not None
        for key in ("oracle", "tier_reason", "work_type", "tier_assigned_by",
                    "oracle_frozen"):
            self.assertIn(key, output.params, key)

    async def test_material_failure_keeps_what_the_run_added(self) -> None:
        repo, base, frozen = self._prepared()
        output_in = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            params={**_loose_task()["params"], "executor_link": "dsh"})
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="o1", oracle_frozen=frozen)
        output = await _manager()._finish_simple_tool(
            _loose_task(), _dev_config(oracle_cmd="false {refs}"),
            CodingTool.TIER_C, output_in, branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        assert output.params is not None
        self.assertEqual(output.params["executor_link"], "dsh")
        self.assertIn("oracle_frozen", output.params)
        self.assertIn("material_failure", output.params)


class OracleBaselineTests(unittest.IsolatedAsyncioTestCase):
    """A preexisting oracle must be green on the base to judge the work.

    Red there it only stays hash-protected (visible note): parking would
    stall the autonomy and failing it after the build would discard the
    work. Other types are red at baseline by design and are never probed.
    """

    def _open(self, repo: Path, task: dict[str, Any], **dev: Any):
        return _manager()._open_simple_branch(
            task, _dev_config(**dev), CodingTool.TIER_C, repo)

    async def test_green_preexisting_oracle_will_run_later(self) -> None:
        repo = _scratch_repo(self)
        opened = self._open(repo, _loose_task(), oracle_cmd="true {refs}")
        assert isinstance(opened, _SimpleBranch)
        self.assertIsNotNone(opened.oracle_frozen)
        self.assertEqual(opened.oracle_baseline_red, "")

    async def test_red_preexisting_oracle_is_recorded_not_parked(self) -> None:
        repo = _scratch_repo(self)
        opened = self._open(repo, _loose_task(), oracle_cmd="false {refs}")
        assert isinstance(opened, _SimpleBranch)   # not a parked output
        self.assertIn("fail", opened.oracle_baseline_red)
        self.assertIn("exit 1", opened.oracle_baseline_red)

    async def test_reproductions_and_specs_are_not_probed_at_baseline(self) -> None:
        repo = _scratch_repo(self)
        for oracle_type in ("bug_reproduction", "separate_ticket_tests",
                            "planner_executable_spec"):
            with self.subTest(oracle_type=oracle_type):
                task = _loose_task()
                task["params"] = {**task["params"],
                                  "oracle": _oracle(oracle_type)}
                opened = self._open(repo, task, oracle_cmd="false {refs}")
                assert isinstance(opened, _SimpleBranch)
                self.assertEqual(opened.oracle_baseline_red, "")
                _git(repo, "checkout", "-q", "main")
                _git(repo, "branch", "-D", opened.branch)

    async def test_without_a_command_nothing_is_probed(self) -> None:
        repo = _scratch_repo(self)
        opened = self._open(repo, _loose_task())
        assert isinstance(opened, _SimpleBranch)
        self.assertEqual(opened.oracle_baseline_red, "")

    async def test_red_baseline_skips_the_gate_and_says_so(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        frozen = freeze_oracle(repo, base, _oracle(), "o1")
        assert frozen is not None
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _write_handoff(repo)
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="o1", oracle_frozen=frozen,
                               oracle_baseline_red="fail (exit 1)")
        # `false {refs}` would fail the ticket if the gate ran.
        output = await _manager()._finish_simple_tool(
            _loose_task(), _dev_config(oracle_cmd="false {refs}"),
            CodingTool.TIER_C,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        (skipped,) = [step for step in output.steps
                      if step["step"] == "oracle_run"]
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("already red on the base branch", skipped["detail"])
        self.assertIn("hash-protected only", skipped["detail"])


class ExecutorLinkTests(unittest.IsolatedAsyncioTestCase):
    """The chain is availability, not selection: the link is recorded."""

    async def test_link_is_persisted_in_whole_params(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s",
            executor_link="dsh"))
        config = {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]}}
        output = await manager._run_simple_tool(
            _loose_task(), config, CodingTool.TIER_C, {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        assert output.params is not None
        self.assertEqual(output.params["executor_link"], "dsh")
        self.assertEqual(output.params["tier_reason"], "specified work")
        self.assertIn("oracle", output.params)

    async def test_a_failed_run_still_names_the_link(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "failed"}, prompt="p", summary="",
            error="boom", executor_link="claudep"))
        config = {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]}}
        output = await manager._run_simple_tool(
            _loose_task(), config, CodingTool.TIER_C, {})
        self.assertEqual(output.status, TaskStatus.FAILED)
        assert output.params is not None
        self.assertEqual(output.params["executor_link"], "claudep")

    async def test_tiers_without_a_chain_carry_no_link(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        config = {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]}}
        output = await manager._run_simple_tool(
            _loose_task(coding_tool="tier_b"), config, CodingTool.TIER_B, {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertNotIn("executor_link", output.params or {})

    def test_executor_link_reads_the_chain_label(self) -> None:
        from agents.dev.session import TierExecutor

        def executor(label: str) -> TierExecutor:
            return TierExecutor(argv=["x"], prompt_via="argv", cwd=None,
                                env={}, label=label)

        self.assertEqual(
            DevSessionManager._executor_link(executor("tier_c:muse")), "muse")
        self.assertEqual(
            DevSessionManager._executor_link(executor("tier_c:dsh")), "dsh")
        self.assertIsNone(DevSessionManager._executor_link(executor("tier_a")))
        self.assertIsNone(DevSessionManager._executor_link(executor("tier_b")))

    async def test_telemetry_counts_links_and_stamps_the_open_record(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1", "material_failures": [],
            "planner_telemetry": {"tier_c": blank_tier_c_telemetry()},
        })
        await record_tier_c(store, "OBJ-1", "started", task_id="t1")
        await record_tier_c(store, "OBJ-1", "executor", task_id="t1",
                            executor_link="muse")
        await record_tier_c(store, "OBJ-1", "started", task_id="t2")
        await record_tier_c(store, "OBJ-1", "executor", task_id="t2",
                            executor_link="dsh")
        await record_tier_c(store, "OBJ-1", "started", task_id="t3")
        await record_tier_c(store, "OBJ-1", "executor", task_id="t3",
                            executor_link="muse")
        await record_tier_c(store, "OBJ-1", "closed", task_id="t1")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        tier_c = objective["planner_telemetry"]["tier_c"]
        self.assertEqual(tier_c["by_link"], {"muse": 2, "dsh": 1})
        links = {r["task_id"]: r["executor_link"]
                 for r in tier_c["c_ticket_records"]}
        self.assertEqual(links, {"t1": "muse", "t2": "dsh", "t3": "muse"})
        closed = [r for r in tier_c["c_ticket_records"]
                  if r["task_id"] == "t1"]
        self.assertEqual(closed[0]["outcome"], "closed")
        self.assertEqual(closed[0]["executor_link"], "muse")

    async def test_legacy_telemetry_without_by_link_is_tolerated(self) -> None:
        store = InMemoryObjectiveStore()
        legacy = blank_tier_c_telemetry()
        del legacy["by_link"]
        await store.create_objective({
            "objective_id": "OBJ-1", "material_failures": [],
            "planner_telemetry": {"tier_c": legacy},
        })
        await record_tier_c(store, "OBJ-1", "started", task_id="t1")
        await record_tier_c(store, "OBJ-1", "executor", task_id="t1",
                            executor_link="claudep")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        self.assertEqual(
            objective["planner_telemetry"]["tier_c"]["by_link"],
            {"claudep": 1})

    def test_blank_block_declares_by_link(self) -> None:
        self.assertEqual(blank_tier_c_telemetry()["by_link"], {})


if __name__ == "__main__":
    unittest.main()
