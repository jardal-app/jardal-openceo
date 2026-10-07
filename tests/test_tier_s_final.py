"""Final test for the Tier S engine (F1+F2) — the ONLY test of record.

Owner order 2026-10-04: no per-phase tests; one final test proving the whole
flow, then live pilots on the new engine. It runs a complete Tier S cycle on
a scratch git repo with scripted executors (zero LLM spend, zero network,
zero Mongo) and real orchestrator gates, and asserts the F1+F2 acceptance:

- F1: every session records its cost; gates run by the orchestrator (before
  any reviewer could see the work); the handoff ``Gates`` section is the
  orchestrator's, never the agent's claim.
- F2: one ticket runs one cycle with one builder session; close integrates,
  mirrors ERP, archives ``.tier-s/``, kills every session and frees the lock.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s.cycle import TierSCycle
from orchestration.live import ExecResult
from agents.dev.tier_s.gates import GateSpec
from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
)
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)

PLAN_MD = """TICKET: TASK-2026-99999
OBJECTIVE: none
GOAL: Append one audited line to tracked.txt
ACCEPTANCE:
- tracked.txt gains exactly one line
- compile gate passes
FILES:
- tracked.txt (modify) — the line goes here
CONTRACTS:
- produces none
DECISIONS:
- D1 [durable: no] one line only — acceptance says so
STEPS:
1. Append the line
2. Write HANDOFF.md
ESTIMATE: files=1 lines=1
OUT_OF_SCOPE:
- anything else
"""

PLAN_REVIEW_MD = """O1 [WARN] PLAN.md:7 — acceptance counts lines strictly → keep the diff to one line
> ACCEPTANCE:
REVIEW_DONE_plan_0
"""

HANDOFF_MD = """HANDOFF TASK-2026-99999
SHA: <orquestador>
Files changed: <orquestador>
Contracts introduced/changed: none
Behavior implemented: appended the audited line
Deviations from plan: none
Discovered work: none
Gotchas: none
Gates: <orquestador>
Findings discarded: none
"""


class ScriptedExecutor:
    """Deterministic stand-in for the three F2 roles (no CLI, no spend)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cleaned: list[str] = []

    async def run(self, role, prompt, workdir, timeout_s):
        del timeout_s
        self.calls.append(role)
        tier_dir = Path(workdir) / ".tier-s"
        tier_dir.mkdir(exist_ok=True)
        if role == "planner" and "PLAN_RESPONSE.md is incomplete" not in prompt and "Answer each plan observation" not in prompt and "Your PLAN.md is invalid" not in prompt:
            (tier_dir / "PLAN.md").write_text(PLAN_MD, encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_READY\n", encoding="utf-8")
            return ExecResult(0, 1.0, signal="PLAN_READY", model="opus",
                              slot="claudej")
        if role == "planner":
            (tier_dir / "PLAN_RESPONSE.md").write_text(
                "O1: ACEPTADA — diff kept to one line\n", encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_RESPONDED\n", encoding="utf-8")
            return ExecResult(0, 0.5, signal="PLAN_RESPONDED", model="opus",
                              slot="claudej")
        if role == "plan_reviewer":
            (tier_dir / "PLAN_REVIEW.md").write_text(
                PLAN_REVIEW_MD, encoding="utf-8")
            return ExecResult(0, 0.5, model="gpt-6.1-sol", slot="codexj")
        if role == "builder":
            with open(Path(workdir) / "tracked.txt", "a", encoding="utf-8") as fh:
                fh.write("audited line\n")
            (tier_dir / "HANDOFF.md").write_text(HANDOFF_MD, encoding="utf-8")
            (tier_dir / "status").write_text("BUILD_DONE\n", encoding="utf-8")
            return ExecResult(0, 2.0, signal="BUILD_DONE",
                              model="muse-spark-1.3-contributor", slot="muse")
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role) -> None:
        self.cleaned.append(role)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=True)
    return proc.stdout.strip()


class TierSFinalTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_cycle_closes_and_frees_everything(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo,
                       check=True)
        (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

        runs = InMemoryRunStore()
        locks = InMemoryLockStore()
        tasks = InMemoryTaskStore()
        fake = ScriptedExecutor()
        notices: list[tuple[str, str]] = []

        async def notify(ref: str, text: str) -> None:
            notices.append((ref, text))

        cycle = TierSCycle(runs, locks, tasks, fake, notify=notify)
        ticket = {
            "task_id": "task-final",
            "ref": "TASK-2026-99999",
            "title": "final probe",
            "description": "append one line",
            "acceptance": ["tracked.txt gains exactly one line"],
            "coding_tool": "tier_s",
            "blocked_by": [],
            "origin": "owner",
        }
        roles = {
            "planner": {"account": "claudej", "model": "opus",
                        "command": ["true"]},
            "plan_reviewer": {"account": "codexj", "model": "gpt-6.1-sol",
                              "command": ["true"]},
            "builder": {"account": "muse",
                        "model": "muse-spark-1.3-contributor",
                        "command": ["true"]},
        }
        cfg = {
            "roles": roles,
            "budgets": dict(BUDGETS_DEFAULTS),
            "timeouts": dict(TIMEOUTS_DEFAULTS),
            "waiting": dict(WAITING_DEFAULTS),
            "auto_merge": True,
            "base_branch": "main",
            "budget_exclude": [],
        }
        specs = [
            GateSpec(name="worktree_sane", cmd="test -f tracked.txt",
                     timeout_minutes=1, required=True),
            GateSpec(name="optional_probe", cmd="test -f tracked.txt",
                     timeout_minutes=1, required=False),
        ]

        outcome = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        # -- close ------------------------------------------------------
        self.assertEqual(outcome.status, "completed")
        self.assertIsNotNone(outcome.commit_sha)
        run = runs.runs[outcome.run_id]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["phase"], "closing")
        self.assertEqual(run["roster"]["builder"]["account"], "muse")
        self.assertTrue(run["base_sha"])
        self.assertEqual(run["head_sha"], outcome.commit_sha)
        self.assertEqual(tasks.statuses["task-final"], "completed")
        # one builder session for the whole ticket (no subplans, no re-rolls)
        self.assertEqual(fake.calls.count("builder"), 1)
        self.assertEqual(
            [c for c in fake.calls if c != "builder"],
            ["planner", "plan_reviewer", "planner"])

        # -- F1: gates ran by the orchestrator at baseline/build/final -----
        phases = [entry["phase"] for entry in run["gates"]]
        for wanted in ("baseline", "build", "final"):
            self.assertIn(wanted, phases)
        self.assertTrue(
            all(entry["status"] == "pass" for entry in run["gates"]))
        # F1: every session recorded its cost
        sessions = run["metrics"]["sessions"]
        self.assertEqual(len(sessions), 4)
        for session in sessions:
            for key in ("role", "slot", "model", "duration_s", "exit_code"):
                self.assertIn(key, session)
        self.assertEqual(
            [s["role"] for s in sessions],
            ["planner", "plan_reviewer", "planner", "builder"])
        # plan review was non-blocking and fully answered
        self.assertEqual(run["plan_review"]["observations"], 1)
        self.assertEqual(run["plan_review"]["accepted"], 1)
        self.assertEqual(run["plan_review"]["no_response"], 0)
        # planned-vs-real sizes
        self.assertEqual(run["metrics"]["files_real"], 1)
        self.assertGreaterEqual(run["metrics"]["lines_real"], 1)

        # -- git: one squash commit on base, wips kept, branch gone ---------
        self.assertEqual(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(
            _git(repo, "log", "-1", "--format=%s"),
            "TASK-2026-99999: final probe")
        self.assertEqual(_git(repo, "diff", "HEAD~1", "HEAD", "--numstat").split(),
                         ["1", "0", "tracked.txt"])
        wip = _git(repo, "rev-parse", f"refs/tier-s/{outcome.run_id}/wip")
        self.assertTrue(wip)
        branches = _git(repo, "branch", "--list", "tier-s/*")
        self.assertEqual(branches, "")

        # -- handoff completed by the orchestrator, then archived -----------
        handoff = (repo / ".tier-s" / "runs" / outcome.run_id / "HANDOFF.md")
        text = handoff.read_text(encoding="utf-8")
        self.assertIn(outcome.commit_sha, text)
        self.assertIn("tracked.txt", text)
        self.assertIn("worktree_sane", text)
        self.assertNotIn("<orquestador>", text)

        # -- nothing stays alive: sessions dead, lock free, ERP told --------
        for role in ("planner", "plan_reviewer", "builder"):
            self.assertIn(role, fake.cleaned)
        self.assertEqual(locks.holders, {})
        self.assertTrue(any("closed" in body for _, body in notices))


if __name__ == "__main__":
    unittest.main()
