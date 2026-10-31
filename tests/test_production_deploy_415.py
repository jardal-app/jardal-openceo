"""Post-merge production step (TASK-2026-00415).

Contract fixed here:

- After an auto-merge to the base branch, the engine publishes the
  base to origin (push only when ahead) and applies the project's
  ``dev.deploy`` mode: ``self`` (origin/main self-deploys, nothing to
  run), ``command`` (run the configured cmd with timeout) or ``none``
  (no production target; delivery to origin/main plus a receipt).
- The step never raises and never fails the ticket close: failures
  land in the receipt as ``production_pending`` with the cause, and
  the next close retries the push (idempotent).
- Objective branches never reach the step: call sites fire it only
  on merges to the base branch.

Fake runners for every branch plus one real local-git round trip
(tmp repo + bare origin, no network, no tokens).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.production_deploy import (  # noqa: E402
    normalize_deploy,
    run_production_step,
    validate_deploy_config,
)
from config_loader import ConfigLoader  # noqa: E402
from settings import Settings  # noqa: E402


def _proc(args, code=0, out="", err=""):
    return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


class FakeRunner:
    """Scripted (cmd, shell) -> CompletedProcess with a call log."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, cmd, *, shell, cwd, timeout):
        key = (cmd if shell else tuple(cmd), shell)
        self.calls.append(key)
        if key not in self.script:
            return _proc(cmd, 1, "", f"unscripted: {key}")
        out = self.script[key]
        if isinstance(out, Exception):
            raise out
        if isinstance(out, list):
            return out.pop(0)
        return out


SHA = "0123456789abcdef0123456789abcdef01234567"
GIT = ["git"]
REV = (tuple(GIT + ["rev-parse", "HEAD"]), False)
ORIGIN = (tuple(GIT + ["remote", "get-url", "origin"]), False)
AHEAD = (tuple(GIT + ["rev-list", "--count", "origin/main..main"]), False)
PUSH = (tuple(GIT + ["push", "origin", "main"]), False)
PULL = (tuple(GIT + ["pull", "--ff-only", "origin", "main"]), False)


def _git_script():
    return {
        REV: _proc(GIT, 0, SHA + "\n"),
        ORIGIN: _proc(GIT, 0, "x\n"),
        AHEAD: _proc(GIT, 0, "0\n"),
        PUSH: _proc(GIT, 0, ""),
        PULL: _proc(GIT, 0, ""),
    }


class NormalizeTests(unittest.TestCase):
    def test_absent_is_none(self):
        for raw in (None, {}, {"mode": None}):
            plan = normalize_deploy(raw)
            self.assertEqual(plan["mode"], "none")
            self.assertEqual(plan["timeout_minutes"], 10)

    def test_unknown_mode_fails_closed_to_none(self):
        plan = normalize_deploy({"mode": "rocket"})
        self.assertEqual(plan["mode"], "none")

    def test_bad_timeout_falls_back(self):
        for bad in (0, -3, True, "10", None):
            plan = normalize_deploy({"mode": "self", "timeout_minutes": bad})
            self.assertEqual(plan["timeout_minutes"], 10)

    def test_command_plan_kept_verbatim(self):
        plan = normalize_deploy({"mode": "command", "cmd": "make live", "timeout_minutes": 3})
        self.assertEqual(plan, {"mode": "command", "cmd": "make live", "timeout_minutes": 3})


class ValidateTests(unittest.TestCase):
    def test_absent_and_none_valid(self):
        self.assertEqual(validate_deploy_config({}), [])
        self.assertEqual(validate_deploy_config({"deploy": None}), [])

    def test_valid_modes(self):
        for raw in ({"mode": "none"}, {"mode": "self"},
                    {"mode": "command", "cmd": "make live"}):
            self.assertEqual(validate_deploy_config({"deploy": raw}), [], raw)

    def test_bad_shapes(self):
        self.assertEqual(len(validate_deploy_config({"deploy": []})), 1)
        self.assertTrue(any("mode" in e for e in
                            validate_deploy_config({"deploy": {"mode": "rocket"}})))
        self.assertTrue(any("cmd" in e for e in
                            validate_deploy_config({"deploy": {"mode": "command"}})))
        self.assertTrue(any("cmd" in e for e in
                            validate_deploy_config({"deploy": {"mode": "command", "cmd": "  "}})))
        self.assertTrue(any("timeout" in e for e in
                            validate_deploy_config({"deploy": {"timeout_minutes": 0}})))


class RunStepTests(unittest.TestCase):
    def test_no_origin_skips_push(self):
        script = _git_script()
        script[ORIGIN] = _proc(GIT, 1, "", "nope")
        runner = FakeRunner(script)
        receipt = run_production_step(Path("/repo"), "main", {"mode": "none"}, run=runner)
        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.pushed)
        self.assertIn("no origin", receipt.detail)
        self.assertNotIn(PUSH, runner.calls)

    def test_clean_tree_needs_no_push(self):
        runner = FakeRunner(_git_script())
        receipt = run_production_step(Path("/repo"), "main", {"mode": "self"}, run=runner)
        self.assertTrue(receipt.ok)
        self.assertFalse(receipt.pushed)
        self.assertIn(SHA[:12], receipt.detail)
        self.assertIn("self-deploys", receipt.detail)
        self.assertNotIn(PUSH, runner.calls)

    def test_ahead_pushes_once(self):
        script = _git_script()
        script[AHEAD] = _proc(GIT, 0, "2\n")
        runner = FakeRunner(script)
        receipt = run_production_step(Path("/repo"), "main", {"mode": "self"}, run=runner)
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.pushed)
        self.assertEqual(runner.calls.count(PUSH), 1)

    def test_push_rejection_retries_after_ff_pull(self):
        script = _git_script()
        script[AHEAD] = _proc(GIT, 0, "1\n")
        script[PUSH] = [_proc(GIT, 1, "", "non-fast-forward"), _proc(GIT, 0, "")]
        runner = FakeRunner(script)
        receipt = run_production_step(Path("/repo"), "main", {"mode": "none"}, run=runner)
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.pushed)
        self.assertEqual(runner.calls.count(PUSH), 2)

    def test_push_failure_parks_without_deploy_cmd(self):
        script = _git_script()
        script[AHEAD] = _proc(GIT, 0, "1\n")
        script[PUSH] = _proc(GIT, 1, "", "denied")
        script[PULL] = _proc(GIT, 1, "", "diverged")
        runner = FakeRunner(script)
        receipt = run_production_step(
            Path("/repo"), "main",
            {"mode": "command", "cmd": "make live", "timeout_minutes": 5},
            run=runner)
        self.assertFalse(receipt.ok)
        self.assertIn("production_pending", receipt.detail)
        self.assertIn("push", receipt.detail)
        self.assertNotIn(("make live", True), runner.calls)

    def test_command_ok_and_failed(self):
        script = _git_script()
        script[("make live", True)] = _proc("make live", 0, "live\n")
        runner = FakeRunner(script)
        receipt = run_production_step(
            Path("/repo"), "main",
            {"mode": "command", "cmd": "make live", "timeout_minutes": 5}, run=runner)
        self.assertTrue(receipt.ok)
        self.assertIn("deploy cmd ok", receipt.detail)

        script = _git_script()
        script[("make live", True)] = _proc("make live", 3, "boom\n")
        runner = FakeRunner(script)
        receipt = run_production_step(
            Path("/repo"), "main",
            {"mode": "command", "cmd": "make live", "timeout_minutes": 5}, run=runner)
        self.assertFalse(receipt.ok)
        self.assertIn("production_pending", receipt.detail)
        self.assertIn("boom", receipt.detail)

    def test_command_timeout_and_misconfigured(self):
        script = _git_script()
        script[("slow", True)] = subprocess.TimeoutExpired("slow", 300)
        runner = FakeRunner(script)
        receipt = run_production_step(
            Path("/repo"), "main",
            {"mode": "command", "cmd": "slow", "timeout_minutes": 5}, run=runner)
        self.assertFalse(receipt.ok)
        self.assertIn("timed out", receipt.detail)

        runner = FakeRunner(_git_script())
        receipt = run_production_step(Path("/repo"), "main", {"mode": "command", "cmd": "  "},
                                      run=runner)
        self.assertFalse(receipt.ok)
        self.assertIn("misconfigured", receipt.detail)

    def test_runner_crash_never_raises(self):
        def boom(*args, **kwargs):
            raise RuntimeError("kaput")

        receipt = run_production_step(Path("/repo"), "main", {"mode": "self"}, run=boom)
        self.assertFalse(receipt.ok)
        self.assertIn("production_pending", receipt.detail)


class RealGitTests(unittest.TestCase):
    def test_push_if_ahead_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def git(*args, cwd):
            return subprocess.run(["git", *args], cwd=cwd, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=60)

        origin = tmp / "origin.git"
        repo = tmp / "repo"
        git("init", "-q", "-b", "main", str(repo), cwd=tmp)
        git("init", "-q", "--bare", str(origin), cwd=tmp)
        (repo / "f.txt").write_text("x", encoding="utf-8")
        git("add", "f.txt", cwd=repo)
        git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "one", cwd=repo)
        git("remote", "add", "origin", str(origin), cwd=repo)

        first = run_production_step(repo, "main", {"mode": "none"})
        self.assertTrue(first.ok)
        self.assertTrue(first.pushed)
        have = git("--git-dir", str(origin), "rev-parse", "main", cwd=tmp)
        self.assertEqual(have.stdout.strip(), first.sha)

        second = run_production_step(repo, "main", {"mode": "none"})
        self.assertTrue(second.ok)
        self.assertFalse(second.pushed)


class LoaderTests(unittest.TestCase):
    def _loader(self, body):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "demo"
        proj.mkdir()
        (proj / "project.yaml").write_text(body, encoding="utf-8")
        return ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(tmp)))

    def test_validate_accepts_and_rejects_deploy(self):
        good = self._loader("dev:\n  deploy:\n    mode: self\n")
        self.assertEqual(good.validate_project("demo"), [])
        bad = self._loader("dev:\n  deploy:\n    mode: rocket\n")
        self.assertTrue(any("deploy" in e for e in bad.validate_project("demo")))
        bad_cmd = self._loader("dev:\n  deploy:\n    mode: command\n")
        self.assertTrue(any("cmd" in e for e in bad_cmd.validate_project("demo")))

    def test_load_merges_deploy_defaults_deep(self):
        loader = self._loader("dev:\n  deploy:\n    mode: self\n")
        dev = loader.load_project("demo")["dev"]
        self.assertEqual(dev["deploy"]["mode"], "self")
        self.assertEqual(dev["deploy"]["timeout_minutes"], 10)
        plain = self._loader("dev:\n  auto_merge: true\n")
        self.assertEqual(plain.load_project("demo")["dev"]["deploy"]["mode"], "none")


if __name__ == "__main__":
    unittest.main()
