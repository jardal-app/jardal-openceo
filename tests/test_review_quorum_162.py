"""Review-quorum hardening (TASK-2026-00162).

The F7 pilot (TASK-2026-00122, E2) parked on ``review_quorum`` with 7
exit-0 reviewers: qwen-glm went 0/5 valid on a one-line prose preamble,
qwen-max timed out, and every park re-ran planning + build (~1900 s) in
a fresh run. This suite pins the fix with scripted executors and
doubles only: zero tokens, zero network, real git worktrees and (for
the orphan-run reconcile) real Mongo in an isolated database, skipped
when Mongo is down.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev import tier_s_contract as contract_mod  # noqa: E402
from agents.dev.tier_s.cycle import TierSCycle  # noqa: E402
from agents.dev.tier_s.gates import GateSpec  # noqa: E402
from agents.dev.tier_s.models import (  # noqa: E402
    BUDGETS_DEFAULTS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
)
from agents.dev.tier_s.stores import (  # noqa: E402
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    objective_scratch,
    run_final_review,
    scratch_root,
)
from orchestration import findings as findings_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from orchestration.live import TIMEOUT_RETURNCODE, ExecResult  # noqa: E402
from orchestration.review import (  # noqa: E402
    InMemoryReviewStore,
    InMemorySlotStore,
)
from state import TaskStatus  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, check=True)


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@e.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


CLEAN_REPORT = "No findings.\nREVIEW_DONE_ticket_1\n"


class ParseReport162Tests(unittest.TestCase):
    """Tolerant to prose, strict on findings (fail-closed)."""

    def test_preamble_and_epilogue_prose_are_ignored(self) -> None:
        text = (
            "Verification complete. The change is sound. One finding below.\n"
            "\n"
            "[WARN] [bug] tracked.txt:2 — off by one → bound the index\n"
            "> audited line\n"
            "The fix is a one-liner.\n"
            "Thanks for reading this far.\n"
            "REVIEW_DONE_ticket_1\n"
        )
        findings = findings_mod.parse_report(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "WARN")
        self.assertEqual(findings[0].category, "bug")
        self.assertEqual(findings[0].quotes, ["audited line"])

    def test_fence_markers_and_any_done_trailer_are_skipped(self) -> None:
        text = (
            "```\n"
            "Review notes in a fenced block.\n"
            "~~~\n"
            "No findings.\n"
            "REVIEW_DONE_anything_at_all\n"
        )
        self.assertEqual(findings_mod.parse_report(text), [])

    def test_header_like_line_must_be_complete(self) -> None:
        for bad in (
            "[WARN] [bug] oops no anchor here\n",
            "[WARN] totally broken line\n",
            "- [CRITICAL] [security] missing the arrow\n",
        ):
            with self.subTest(line=bad.strip()):
                with self.assertRaises(findings_mod.ReportMalformed):
                    findings_mod.parse_report(
                        "[INFO] [test] a.py — fine → ship\n" + bad)

    def test_line_anchor_without_quote_is_malformed(self) -> None:
        with self.assertRaises(findings_mod.ReportMalformed):
            findings_mod.parse_report(
                "[WARN] [bug] tracked.txt:2 — no quote → reject\n")

    def test_prose_only_report_is_malformed(self) -> None:
        for bad in ("", "   \n", "Looks good to me.\nAll fine.\n",
                    "```\ncode\n```\nREVIEW_DONE_ticket_1\n"):
            with self.subTest(report=bad[:20]):
                with self.assertRaises(findings_mod.ReportMalformed):
                    findings_mod.parse_report(bad)

    def test_dressing_around_header_anchor_and_quote(self) -> None:
        text = (
            "- **[WARN]** **[bug]** `tracked.txt:2` — off by one → bound it\n"
            "> `audited line`\n"
        )
        findings = findings_mod.parse_report(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].anchors[0].path, "tracked.txt")
        self.assertEqual(findings[0].anchors[0].line, 2)
        self.assertEqual(findings[0].quotes, ["audited line"])

    def test_anchor_wrap_never_strips_underscores(self) -> None:
        findings = findings_mod.parse_report(
            "[WARN] [bug] `__init__.py:2` — typo → fix\n"
            "> x\n")
        self.assertEqual(findings[0].anchors[0].path, "__init__.py")

    def test_explanation_keeps_five_lines_and_drops_the_rest(self) -> None:
        lines = "\n".join(f"explanation line {n}" for n in range(1, 8))
        findings = findings_mod.parse_report(
            "[WARN] [bug] a.py — p → f\n"
            f"{lines}\n")
        self.assertEqual(
            findings[0].explanation.splitlines(),
            [f"explanation line {n}" for n in range(1, 6)])

    def test_warning_and_uppercase_category_are_normalized(self) -> None:
        findings = findings_mod.parse_report(
            "[WARNING] [BUG] a.py — p → f\n")
        self.assertEqual(findings[0].severity, "WARN")
        self.assertEqual(findings[0].category, "bug")

    def test_no_findings_tolerates_dressing(self) -> None:
        for clean in ("No findings.\n", "**No findings.**\n",
                      "no findings\n", "1. `No findings.`\n",
                      "- No findings.\n"):
            with self.subTest(report=clean.strip()):
                self.assertEqual(findings_mod.parse_report(clean), [])

    def test_no_findings_after_findings_is_prose_not_fatal(self) -> None:
        # Deliberate tolerance change: the exact marker used to void the
        # whole report here ("No findings mixed with findings").
        findings = findings_mod.parse_report(
            "[WARN] [bug] a.py — p → f\n"
            "No findings.\n")
        self.assertEqual(len(findings), 1)


class PilotReport162Tests(unittest.TestCase):
    """The pilot failure mode, replayed on a synthetic repo (0 tokens).

    Shape of the real ``.openceo/REVIEW-qwen-glm-*.md`` pilot reports (prose
    preamble, §16.4 findings, ``REVIEW_DONE_*`` trailer), with anchors and
    quotes adapted to the scratch repo so ``verify_report`` runs for real.
    """

    def test_pilot_shaped_report_parses_and_verifies(self) -> None:
        repo = _scratch_repo(self)
        (repo / "tracked.txt").write_text(
            "before\naudited line\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "change")
        base = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        text = (
            "Verification complete. The change is sound — one finding on "
            "the new line itself.\n"
            "\n"
            "[WARN] [bug] tracked.txt:2 — the appended line has no trailing "
            "context → append a comment\n"
            "> audited line\n"
            "Flagged because this ticket defines the audited surface.\n"
            "\n"
            "REVIEW_DONE_ticket_1\n"
        )
        raw = findings_mod.parse_report(text)
        self.assertEqual(len(raw), 1)
        verdicts = findings_mod.verify_report(
            raw, repo, head, base, "r-deep", "deepseek", ["ship"])
        self.assertTrue(verdicts[0].verified)
        self.assertEqual(verdicts[0].scope, "ticket")
        self.assertEqual(verdicts[0].destination, "findings")


class LegacyReviewContract162Tests(unittest.TestCase):
    """The retired SUGGEST vocabulary rejected INFO-only reports."""

    def test_valid_review_accepts_info(self) -> None:
        self.assertTrue(contract_mod.valid_review("[INFO] [test] a — p → f\n"))

    def test_review_quorum_counts_two_info_families(self) -> None:
        self.assertTrue(contract_mod.review_quorum(
            {"a": "[INFO] [test] x — p → f\n",
             "b": "[INFO] [test] y — p → f\n"},
            {"a": "deepseek", "b": "qwen"}))


def _pool(*specs: tuple[str, str, int],
          extras: tuple[str, ...] = ()) -> list[review_mod.ReviewSlot]:
    return review_mod.parse_pool([
        {"label": label, "family": family, "priority": priority,
         "extra": label in extras}
        for label, family, priority in specs
    ])


class _ScriptedRound:
    """Fake ``run_one``: scripted (valid, kind, reason) per label/attempt."""

    def __init__(self, script: dict[tuple[str, int], tuple[bool, str, str]],
                 pool: list[review_mod.ReviewSlot]) -> None:
        self._script = script
        self._by_label = {slot.label: slot for slot in pool}
        self.calls: list[tuple[str, int]] = []

    async def __call__(
        self, slot: review_mod.ReviewSlot, attempt: int,
        previous: Any | None,
    ) -> dict[str, Any]:
        self.calls.append((slot.label, attempt))
        valid, kind, reason = self._script.get(
            (slot.label, attempt), (False, "slot_error", "unscripted"))
        outcome = review_mod.new_outcome(slot, attempt)
        outcome.update(valid=valid, kind="valid" if valid else kind,
                       reason="" if valid else reason)
        return outcome


class QuorumRound162Tests(unittest.IsolatedAsyncioTestCase):
    """One round machinery for both engines: relief, then one retry."""

    async def test_wave1_quorum_runs_no_wave2(self) -> None:
        pool = _pool(("a", "fa", 10), ("b", "fb", 20), ("x", "muse", 30),
                     extras=("x",))
        store = InMemorySlotStore()
        plan = await review_mod.select_round(pool, store, 2)
        script = _ScriptedRound(
            {("a", 1): (True, "", ""), ("b", 1): (True, "", ""),
             ("x", 1): (True, "", "")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertTrue(result.met)
        self.assertEqual(sorted(script.calls),
                         [("a", 1), ("b", 1), ("x", 1)])
        self.assertEqual(
            sorted(label for label, ok in result.valid.items() if ok),
            ["a", "b", "x"])

    async def test_relief_uses_an_untried_family_first(self) -> None:
        pool = _pool(("a", "fa", 10), ("b", "fb", 20), ("c", "fc", 30))
        store = InMemorySlotStore()
        plan = await review_mod.select_round(pool, store, 2)
        self.assertEqual([slot.label for slot in plan.reviewers], ["a", "b"])
        script = _ScriptedRound(
            {("a", 1): (True, "", ""), ("b", 1): (False, "timeout", "900s"),
             ("c", 1): (True, "", ""), ("b", 2): (True, "", "")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertTrue(result.met)
        # Short of quorum by one: wave 2 runs shortfall + 1 = 2 slots —
        # the untried family first, then the retriable failure.
        self.assertIn(("c", 1), script.calls)
        self.assertIn(("b", 2), script.calls)
        self.assertNotIn(("a", 2), script.calls)

    async def test_retry_runs_once_and_only_for_retriable_kinds(self) -> None:
        pool = _pool(("a", "fa", 10), ("b", "fb", 20))
        store = InMemorySlotStore()
        plan = await review_mod.select_round(pool, store, 2)
        script = _ScriptedRound(
            {("a", 1): (False, "malformed", "bad header"),
             ("b", 1): (True, "", ""),
             ("a", 2): (False, "malformed", "still bad")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertFalse(result.met)
        self.assertEqual(script.calls.count(("a", 1)), 1)
        self.assertEqual(script.calls.count(("a", 2)), 1)
        self.assertNotIn(("a", 3), script.calls)
        self.assertNotIn(("b", 2), script.calls)

    async def test_cooling_down_disabled_and_write_attempt_never_retry(
        self,
    ) -> None:
        pool = _pool(("a", "fa", 10), ("b", "fb", 20), ("c", "fc", 30))
        store = InMemorySlotStore()
        await store.set_slot("a", {"status": "cooling_down",
                                   "until": "2999-01-01T00:00:00+00:00"})
        plan = await review_mod.select_round(pool, store, 2)
        self.assertEqual([slot.label for slot in plan.reviewers], ["b", "c"])
        script = _ScriptedRound(
            {("b", 1): (False, "write_attempt", "write_attempt"),
             ("c", 1): (True, "", "")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertFalse(result.met)
        self.assertEqual(script.calls, [("b", 1), ("c", 1)])

    async def test_extras_never_count_toward_quorum(self) -> None:
        pool = _pool(("a", "fa", 10), ("x", "muse", 20), extras=("x",))
        store = InMemorySlotStore()
        plan = await review_mod.select_round(pool, store, 2)
        script = _ScriptedRound(
            {("a", 1): (True, "", ""), ("x", 1): (True, "", "")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertFalse(result.met)
        # Both valid, nothing retriable, nobody untried: no wave 2.
        self.assertEqual(sorted(script.calls), [("a", 1), ("x", 1)])

    async def test_summary_reports_every_attempt(self) -> None:
        pool = _pool(("a", "fa", 10), ("b", "fb", 20))
        store = InMemorySlotStore()
        plan = await review_mod.select_round(pool, store, 2)
        script = _ScriptedRound(
            {("a", 1): (False, "timeout", "slow"),
             ("b", 1): (True, "", ""),
             ("a", 2): (True, "", "")}, pool)
        result = await review_mod.run_quorum_round(
            pool, store, plan, script)

        self.assertTrue(result.met)
        self.assertEqual(result.summary(),
                         "a#1 timeout (slow); b#1 valid; a#2 valid")


class SlotTimeout162Tests(unittest.TestCase):
    def test_slot_override_default_and_timeout_retry_factor(self) -> None:
        default_slot = review_mod.ReviewSlot(label="a", family="fa")
        self.assertEqual(
            review_mod.slot_timeout_s(default_slot, 15), 900)
        self.assertEqual(
            review_mod.slot_timeout_s(default_slot, None), 900)
        self.assertEqual(
            review_mod.slot_timeout_s(default_slot, "broken"), 900)
        timed = review_mod.ReviewSlot(
            label="b", family="fb", timeout_minutes=10)
        self.assertEqual(review_mod.slot_timeout_s(timed, 15), 600)
        self.assertEqual(
            review_mod.slot_timeout_s(
                timed, 15, {"kind": "timeout"}), 900)
        self.assertEqual(
            review_mod.slot_timeout_s(
                timed, 15, {"kind": "malformed"}), 600)


class ReviewerPrompt162Tests(unittest.TestCase):
    def test_output_contract_first_and_retry_note_leads(self) -> None:
        prompt = review_mod.reviewer_prompt("r1", "REQ.md", "ticket", 1)
        self.assertIn("The first line is a finding header or `No findings.`",
                      prompt)
        retry = review_mod.retry_note({"kind": "timeout", "reason": "900s"})
        self.assertIn("timed out", retry)
        prompt = review_mod.reviewer_prompt(
            "r1", "REQ.md", "ticket", 1, retry=retry)
        self.assertTrue(prompt.startswith(retry))
        self.assertEqual(review_mod.retry_note(None), "")
        malformed = review_mod.retry_note(
            {"kind": "malformed", "reason": "bad header"})
        self.assertIn("was rejected", malformed)
        self.assertIn("bad header", malformed)


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

INFO_REPORT = (
    "[INFO] [test] tracked.txt:1@base — stale header comment → reword it\n"
    "> before\n"
    "It predates the ticket.\n"
    "REVIEW_DONE_ticket_1\n"
)


class _QuorumExecutor:
    """Scripted planner/builder plus queued reviewer outputs (0 tokens)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cleaned: list[str] = []
        # role -> [(returncode, output_tail), ...], consumed in order.
        self.scripts: dict[str, list[tuple[int, str]]] = {}

    def review_always(self, returncode: int, output: str) -> None:
        for label in ("r-deep", "r-qwen", "r-glm"):
            self.scripts[f"review:{label}"] = [(returncode, output)] * 12

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del timeout_s
        self.calls.append(role)
        tier_dir = Path(workdir) / ".openceo"
        tier_dir.mkdir(exist_ok=True)
        if role == "planner" and "PLAN_RESPONSE.md is incomplete" not in prompt and "Answer each plan observation" not in prompt and "Your PLAN.md is invalid" not in prompt:
            (tier_dir / "PLAN.md").write_text(PLAN_MD, encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_READY\n", encoding="utf-8")
            return ExecResult(0, 1.0, signal="PLAN_READY", model="opus",
                              slot="claudej")
        if role == "planner":
            (tier_dir / "PLAN_RESPONSE.md").write_text(
                "O1: ACEPTADA — diff kept to one line\n", encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_RESPONDED\n",
                                             encoding="utf-8")
            return ExecResult(0, 0.5, signal="PLAN_RESPONDED", model="opus",
                              slot="claudej")
        if role == "plan_reviewer":
            (tier_dir / "PLAN_REVIEW.md").write_text(
                PLAN_REVIEW_MD, encoding="utf-8")
            return ExecResult(0, 0.5, model="gpt-6.1-sol", slot="codexj")
        if role == "builder":
            with open(Path(workdir) / "tracked.txt", "a",
                      encoding="utf-8") as fh:
                fh.write("audited line\n")
            (tier_dir / "HANDOFF.md").write_text(HANDOFF_MD, encoding="utf-8")
            (tier_dir / "status").write_text("BUILD_DONE\n", encoding="utf-8")
            return ExecResult(0, 2.0, signal="BUILD_DONE",
                              model="muse-spark-1.3-contributor", slot="muse")
        if role.startswith("review:"):
            queue = self.scripts.get(role, [(0, CLEAN_REPORT)])
            code, output = queue[min(len(queue) - 1, sum(
                1 for call in self.calls if call == role) - 1)]
            return ExecResult(code, 3.0, output_tail=output,
                              usage={"timeout": True} if code != 0 else None)
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role: str) -> None:
        self.cleaned.append(role)


def _cycle_parts() -> tuple[dict[str, Any], dict[str, Any], list[GateSpec]]:
    ticket = {
        "task_id": "task-quorum-162",
        "ref": "TASK-2026-99999",
        "title": "quorum probe",
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
        "reviewers": [
            {"label": "r-deep", "family": "deepseek", "priority": 10},
            {"label": "r-qwen", "family": "qwen", "priority": 20},
            {"label": "r-glm", "family": "glm", "priority": 30},
        ],
        "budgets": dict(BUDGETS_DEFAULTS),
        "timeouts": dict(TIMEOUTS_DEFAULTS),
        "waiting": dict(WAITING_DEFAULTS),
        # Static pool pinned: quorum mechanics, not live routing (00403).
        "review": {"min_available_families": 1, "round1_parallel": 2,
                   "round2_parallel": 2,
                   "free_router": {"enabled": False}},
        "auto_merge": True,
        "base_branch": "main",
        "budget_exclude": [],
    }
    specs = [GateSpec(name="worktree_sane", cmd="test -f tracked.txt",
                      timeout_minutes=1, required=True)]
    return ticket, cfg, specs


def _resume_minutes(resume_at: str | None) -> float:
    assert resume_at
    moment = datetime.fromisoformat(resume_at)
    delta = moment - datetime.now(timezone.utc)
    return delta.total_seconds() / 60.0


class CycleQuorum162Tests(unittest.IsolatedAsyncioTestCase):
    """No-quorum parks checkpoint; the next launch resumes at review."""

    def _cycle(self, fake: _QuorumExecutor,
               ) -> tuple[TierSCycle, InMemoryRunStore, InMemoryReviewStore,
                          list[tuple[str, str]]]:
        runs = InMemoryRunStore()
        reviews = InMemoryReviewStore()
        notices: list[tuple[str, str]] = []

        async def notify(ref: str, text: str) -> None:
            notices.append((ref, text))

        cycle = TierSCycle(runs, InMemoryLockStore(), InMemoryTaskStore(),
                           fake, notify=notify, reviews=reviews)
        return cycle, runs, reviews, notices

    async def test_park_without_quorum_stores_checkpoint(self) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.review_always(TIMEOUT_RETURNCODE, "")
        cycle, runs, reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()

        outcome = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        self.assertEqual(outcome.status, "waiting")
        assert outcome.run_id is not None
        self.assertEqual(outcome.reason, "review_quorum")
        self.assertTrue(outcome.detail.startswith(
            "round 1 without quorum: r-deep#1 timeout"))
        run = runs.runs[outcome.run_id]
        self.assertEqual(run["status"], "waiting")
        checkpoint = run["checkpoint"]
        self.assertEqual(checkpoint["phase"], "review")
        self.assertEqual(checkpoint["branch"], outcome.branch)
        tip = _git(repo, "rev-parse",
                   f"refs/heads/{outcome.branch}").stdout.strip()
        self.assertEqual(checkpoint["head_sha"], tip)
        self.assertEqual(checkpoint["review_parks"], 1)
        self.assertIn("PLAN.md", checkpoint["artifacts"])
        self.assertIn("HANDOFF.md", checkpoint["artifacts"])
        self.assertFalse(run["rounds"][0]["quorum"])
        self.assertEqual(len(run["rounds"][0]["attempts"]), 5)
        # Every attempt persisted with its kind (2 + relief + 2 retries).
        self.assertEqual(len(reviews.docs), 5)
        kinds = sorted(doc["kind"] for doc in reviews.docs)
        self.assertEqual(kinds, ["timeout"] * 5)
        self.assertEqual(sorted(doc["attempt"] for doc in reviews.docs),
                         [1, 1, 1, 2, 2])
        for doc in reviews.docs:
            self.assertIn("elapsed_s", doc)
            self.assertIn("exit_code", doc)
        self.assertAlmostEqual(_resume_minutes(outcome.resume_at), 15,
                               delta=2.0)

    async def test_second_launch_resumes_same_run_without_replanning(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.review_always(TIMEOUT_RETURNCODE, "")
        cycle, runs, _reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()

        first = await cycle.run(ticket, repo, cfg, gate_specs=specs)
        self.assertEqual(first.reason, "review_quorum")
        planner_calls = fake.calls.count("planner")
        builder_calls = fake.calls.count("builder")

        fake.review_always(0, CLEAN_REPORT)
        second = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        self.assertEqual(second.status, "completed")
        self.assertEqual(second.run_id, first.run_id)
        assert second.run_id is not None
        self.assertEqual(runs.runs[second.run_id]["resumes"], 1)
        # Planning, plan review and build ran once, on the first launch.
        self.assertEqual(fake.calls.count("planner"), planner_calls)
        self.assertEqual(fake.calls.count("plan_reviewer"), 1)
        self.assertEqual(fake.calls.count("builder"), builder_calls)
        self.assertEqual(len(runs.runs), 1)

    async def test_backoff_ladder_then_needs_owner(self) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.review_always(TIMEOUT_RETURNCODE, "")
        cycle, runs, _reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()

        outcomes = [await cycle.run(ticket, repo, cfg, gate_specs=specs)
                    for _ in range(4)]

        self.assertEqual([item.run_id for item in outcomes],
                         [outcomes[0].run_id] * 4)
        self.assertEqual([item.reason for item in outcomes],
                         ["review_quorum"] * 3 + ["needs_owner"])
        for outcome, minutes in zip(outcomes[:3], (15, 30, 60)):
            self.assertAlmostEqual(_resume_minutes(outcome.resume_at),
                                   minutes, delta=2.0)
        last = outcomes[3]
        assert last.run_id is not None
        self.assertIsNone(last.resume_at)
        self.assertIn("4 times", last.detail)
        self.assertIn("resumes at review", last.detail)
        self.assertIn("r-deep#1 timeout", last.detail)
        self.assertEqual(runs.runs[last.run_id]["checkpoint"]
                         ["review_parks"], 4)
        # Still one planning + one build across the four launches.
        self.assertEqual(fake.calls.count("planner"), 2)
        self.assertEqual(fake.calls.count("builder"), 1)

    async def test_moved_branch_starts_fresh_and_supersedes(self) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.review_always(TIMEOUT_RETURNCODE, "")
        cycle, runs, _reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()

        first = await cycle.run(ticket, repo, cfg, gate_specs=specs)
        assert first.branch is not None
        _git(repo, "checkout", "-q", first.branch)
        _git(repo, "commit", "-q", "--allow-empty", "-m", "drift")
        _git(repo, "checkout", "-q", "main")

        second = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        assert first.run_id is not None
        assert second.run_id is not None
        self.assertNotEqual(second.run_id, first.run_id)
        self.assertEqual(runs.runs[first.run_id]["status"], "superseded")
        self.assertEqual(runs.runs[first.run_id]["superseded_by"],
                         second.run_id)
        self.assertEqual(second.reason, "review_quorum")
        # A fresh run plans and builds again.
        self.assertEqual(fake.calls.count("planner"), 4)
        self.assertEqual(fake.calls.count("builder"), 2)

    async def test_fresh_run_supersedes_other_open_runs(self) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.review_always(0, CLEAN_REPORT)
        cycle, runs, _reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()
        runs.runs["ts-old-wait"] = {
            "run_id": "ts-old-wait", "task_id": "task-quorum-162",
            "status": "waiting", "active_repo": str(repo)}
        runs.runs["ts-old-run"] = {
            "run_id": "ts-old-run", "task_id": "task-quorum-162",
            "status": "running", "active_repo": str(repo)}
        runs.runs["ts-old-done"] = {
            "run_id": "ts-old-done", "task_id": "task-quorum-162",
            "status": "completed", "active_repo": None}

        outcome = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        self.assertEqual(outcome.status, "completed")
        assert outcome.run_id is not None
        self.assertEqual(runs.runs["ts-old-wait"]["status"], "superseded")
        self.assertEqual(runs.runs["ts-old-run"]["status"], "superseded")
        self.assertIsNone(runs.runs["ts-old-wait"]["active_repo"])
        self.assertIsNone(runs.runs["ts-old-run"]["active_repo"])
        self.assertEqual(runs.runs["ts-old-wait"]["superseded_by"],
                         outcome.run_id)
        self.assertEqual(runs.runs["ts-old-done"]["status"], "completed")

    async def test_review_attempts_carry_kind_and_finding_text(self) -> None:
        repo = _scratch_repo(self)
        fake = _QuorumExecutor()
        fake.scripts["review:r-deep"] = [(0, "just prose, no marker\n"),
                                         (0, CLEAN_REPORT),
                                         (0, CLEAN_REPORT)]
        fake.scripts["review:r-qwen"] = [(0, INFO_REPORT), (0, INFO_REPORT)]
        fake.scripts["review:r-glm"] = [(0, CLEAN_REPORT)] * 4
        cycle, runs, reviews, _notices = self._cycle(fake)
        ticket, cfg, specs = _cycle_parts()

        outcome = await cycle.run(ticket, repo, cfg, gate_specs=specs)

        self.assertEqual(outcome.status, "completed")
        deep = sorted(
            (doc for doc in reviews.docs if doc["slot"] == "r-deep"
             and doc["round"] == 1),
            key=lambda doc: doc["attempt"])
        self.assertEqual([(doc["attempt"], doc["kind"]) for doc in deep],
                         [(1, "malformed"), (2, "valid")])
        info = next(doc for doc in reviews.docs
                    if doc["slot"] == "r-qwen" and doc["round"] == 1)
        self.assertTrue(info["valid"])
        self.assertEqual(len(info["findings"]), 1)
        finding = info["findings"][0]
        self.assertTrue(finding["verified"])
        self.assertEqual(finding["problem"], "stale header comment")
        self.assertEqual(finding["fix"], "reword it")
        self.assertEqual(finding["quotes"], ["before"])
        self.assertIn("predates the ticket", finding["explanation"])
        assert outcome.run_id is not None
        first_round = runs.runs[outcome.run_id]["rounds"][0]
        self.assertTrue(first_round["quorum"])
        self.assertEqual(
            sorted(item["slot"] for item in first_round["attempts"]),
            ["r-deep", "r-deep", "r-glm", "r-qwen"])


class _FinalExecutor:
    """Scripted final-review roles: records worktree heads and prompts."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.heads: dict[str, str] = {}
        self.scripts: dict[str, list[tuple[int, str]]] = {}

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del timeout_s
        self.calls.append(role)
        self.prompts.setdefault(role, prompt)
        if role.startswith("review:"):
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workdir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                check=True)
            self.heads[role] = proc.stdout.strip()
            queue = self.scripts.get(role, [(0, CLEAN_REPORT)])
            index = min(len(queue) - 1, sum(
                1 for call in self.calls if call == role) - 1)
            code, output = queue[index]
            return ExecResult(code, 2.0, output_tail=output,
                              usage={"timeout": True} if code != 0 else None)
        if role == "closer":
            return ExecResult(0, 1.0, output_tail="")
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role: str) -> None:
        del role


def _final_env(repo: Path, tasks: dict[str, dict[str, Any]],
               fake: _FinalExecutor, pool: list[dict[str, Any]],
               ) -> tuple[ObjectiveEnv, InMemoryReviewStore,
                          list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        created.append(dict(payload))
        return {"task_id": f"task-new-{len(created)}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    async def notify(ref: str, text: str) -> None:
        del ref, text

    reviews = InMemoryReviewStore()
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(dict(tasks)),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=fake,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=InMemorySlotStore(),
        reviews=reviews,
        inbox=None,
        notify=notify,
        repo=repo,
        pool=review_mod.parse_pool(pool),
        review_cfg={"free_router": {"enabled": False}},
    )
    return env, reviews, created


def _final_tasks() -> dict[str, dict[str, Any]]:
    return {
        "task-parent": {
            "task_id": "task-parent", "title": "t", "description": "d",
            "status": "converted", "project": "proj", "acceptance": [],
            "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}},
            "objective_id": "OBJ-1"},
        "kid-1": {
            "task_id": "kid-1", "title": "w", "status": "completed",
            "objective_id": "OBJ-1", "project": "proj",
            "acceptance": ["done"],
            "params": {"target_agent": "dev",
                       "expected_artifacts": ["code"]},
            "coding_tool": "tier_b"},
    }


_FINAL_POOL = [
    {"label": "r-deep", "family": "deepseek", "priority": 10},
    {"label": "r-qwen", "family": "qwen", "priority": 20},
    {"label": "r-glm", "family": "glm", "priority": 30},
]


class FinalReview162Tests(unittest.IsolatedAsyncioTestCase):
    """Final review shares the round machinery on the integration ref."""

    async def test_final_review_targets_main_not_the_live_checkout(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        main_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-qb", "side")
        (repo / "side.txt").write_text("side\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "side work")
        side_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(side_tip, main_tip)

        fake = _FinalExecutor()
        env, _reviews, _created = _final_env(
            repo, _final_tasks(), fake, _FINAL_POOL)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "base_sha": main_tip,
            "conversion_source_task_id": "task-parent",
        })
        result = await run_final_review(env, "OBJ-1")

        self.assertEqual(result["action"], "closed")
        self.assertTrue(fake.heads)
        for head in fake.heads.values():
            self.assertEqual(head, main_tip)

    async def test_invalid_final_attempts_are_persisted(self) -> None:
        repo = _scratch_repo(self)
        fake = _FinalExecutor()
        fake.scripts["review:r-deep"] = [(0, "prose, no marker\n")] * 3
        fake.scripts["review:r-qwen"] = [(TIMEOUT_RETURNCODE, "")] * 3
        fake.scripts["review:r-glm"] = [(TIMEOUT_RETURNCODE, "")] * 3
        env, reviews, _created = _final_env(
            repo, _final_tasks(), fake, _FINAL_POOL)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("r-deep#1 malformed", stored["waiting_reason"])
        kinds = sorted(doc["kind"] for doc in reviews.docs)
        self.assertEqual(kinds,
                         ["malformed"] * 2 + ["timeout"] * 4)
        self.assertTrue(all(doc["attempt"] in (1, 2)
                            for doc in reviews.docs))
        self.assertTrue(all(doc["invalid_reason"] for doc in reviews.docs))
        self.assertTrue(all("final_review" in doc for doc in reviews.docs))

    async def test_stale_closure_is_dropped_and_closer_gets_exact_path(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "tracked.txt").write_text(
            "before\nchanged line\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "change")
        report = (
            "[WARN] [bug] tracked.txt:2 — changed line is bare → annotate\n"
            "> changed line\n"
            "REVIEW_DONE_objective_final_0\n"
        )
        fake = _FinalExecutor()
        fake.scripts["review:r-deep"] = [(0, report)]
        fake.scripts["review:r-qwen"] = [(0, report)]
        fake.scripts["review:r-glm"] = [(0, CLEAN_REPORT)]
        env, _reviews, created = _final_env(
            repo, _final_tasks(), fake, _FINAL_POOL)
        stale_root = scratch_root(repo) / "OBJECTIVE_CLOSURE.md"
        stale_root.parent.mkdir(parents=True, exist_ok=True)
        stale_root.write_text("F1: REAL — stale\n", encoding="utf-8")
        stale_own = (objective_scratch(repo, "OBJ-1")
                     / "OBJECTIVE_CLOSURE.md")
        stale_own.parent.mkdir(parents=True, exist_ok=True)
        stale_own.write_text("F1: REAL — stale\n", encoding="utf-8")
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "base_sha": base,
            "conversion_source_task_id": "task-parent",
        })
        result = await run_final_review(env, "OBJ-1")

        # The silent closer leaves no verdicts: with the stale REAL gone,
        # nothing judges the WARN finding, so the objective parks with
        # the attempts as evidence instead of closing (TASK-2026-00170).
        # The old expectation (closed, FALSO by default) hid a
        # pool-confirmed finding behind a provider failure. The legacy
        # global file is neither read nor swept anymore (TASK-2026-00176);
        # only the objective's own stale is dropped.
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("F1", result["detail"])
        self.assertEqual(created, [])
        self.assertEqual(fake.calls.count("closer"), 2)
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("closer_no_verdict", stored["waiting_reason"])
        # The findings survive for the next review, and the reviewed SHA
        # does not advance past an unreviewed head.
        findings_file = (objective_scratch(repo, "OBJ-1")
                         / "OBJECTIVE_FINDINGS.md")
        self.assertTrue(findings_file.exists())
        self.assertIn("changed line is bare",
                      findings_file.read_text(encoding="utf-8"))
        self.assertIsNone(stored.get("last_final_review_sha"))
        self.assertIn(".openceo/objectives/OBJ-1/OBJECTIVE_FINDINGS.md",
                      fake.prompts["closer"])
        self.assertTrue(stale_root.exists())
        self.assertFalse(stale_own.exists())


MONGO_URL = "mongodb://localhost:27017"
TEST_DB = "openceo_test_review_quorum_162"


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:  # noqa: BLE001 - probe returns bool
        return False


@unittest.skipUnless(_mongo_available(), "MongoDB not available")
class OrphanRuns162Tests(unittest.IsolatedAsyncioTestCase):
    """Terminal tasks and boot leave no resumable run behind."""

    async def asyncSetUp(self) -> None:
        from db.mongo import MongoRepository  # noqa: E402
        from settings import Settings  # noqa: E402

        settings = Settings(_env_file=None, MONGODB_URL=MONGO_URL,
                            MONGODB_DATABASE=TEST_DB)
        self.repo = MongoRepository(settings)
        await self.repo.connect()
        await self.repo.tasks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})

    async def asyncTearDown(self) -> None:
        await self.repo.tasks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})
        await self.repo.close()

    async def _task(self, task_id: str, status: str) -> None:
        await self.repo.tasks.insert_one(
            {"task_id": task_id, "status": status})

    async def _run(self, run_id: str, task_id: str, status: str,
                   **extra: Any) -> None:
        await self.repo.tier_s_runs.insert_one(
            {"run_id": run_id, "task_id": task_id, "status": status,
             **extra})

    async def test_terminal_patch_closes_only_waiting_runs(self) -> None:
        await self._task("t-str", "in_progress")
        await self._run("r-wait", "t-str", "waiting",
                        active_repo="/repo-wait")
        await self._run("r-run", "t-str", "running",
                        active_repo="/repo-run")
        await self.repo.patch_task(
            "t-str", {"status": TaskStatus.COMPLETED.value})
        wait = await self.repo.get_tier_s_run("r-wait")
        assert wait is not None
        self.assertEqual(wait["status"], "superseded")
        self.assertIsNone(wait["active_repo"])
        run = await self.repo.get_tier_s_run("r-run")
        assert run is not None
        self.assertEqual(run["status"], "running")

        await self._task("t-enum", "in_progress")
        await self._run("r-enum", "t-enum", "waiting")
        await self.repo.patch_task(
            "t-enum", {"status": TaskStatus.CANCELLED})
        enum_run = await self.repo.get_tier_s_run("r-enum")
        assert enum_run is not None
        self.assertEqual(enum_run["status"], "superseded")

    async def test_nonterminal_patch_leaves_waiting_runs(self) -> None:
        await self._task("t-open", "queued")
        await self._run("r-open", "t-open", "waiting")
        await self.repo.patch_task(
            "t-open", {"status": TaskStatus.IN_PROGRESS.value})
        run = await self.repo.get_tier_s_run("r-open")
        assert run is not None
        self.assertEqual(run["status"], "waiting")

    async def test_boot_closes_live_runs_and_frees_the_repo(self) -> None:
        await self._task("t-live", "in_progress")
        for run_id, status in (("r-a", "running"), ("r-b", "active"),
                               ("r-c", "resuming"), ("r-d", "waiting")):
            await self._run(run_id, "t-live", status,
                            active_repo=f"/repo-{run_id}")
        await self.repo.mark_interrupted_tasks()

        for run_id in ("r-a", "r-b", "r-c"):
            run = await self.repo.get_tier_s_run(run_id)
            assert run is not None
            self.assertEqual(run["status"], "failed")
            self.assertIsNone(run["active_repo"])
        kept = await self.repo.get_tier_s_run("r-d")
        assert kept is not None
        self.assertEqual(kept["status"], "waiting")

    async def test_boot_orphan_pass_closes_runs_of_ended_tasks(self) -> None:
        await self._task("t-done", "completed")
        await self._task("t-open", "in_progress")
        await self._run("r-done", "t-done", "waiting")
        await self._run("r-open", "t-open", "waiting")
        await self._run("r-gone", "t-missing", "waiting")

        closed = await self.repo.close_orphan_tier_s_runs()

        self.assertEqual(closed, 1)
        done = await self.repo.get_tier_s_run("r-done")
        assert done is not None
        self.assertEqual(done["status"], "superseded")
        kept = await self.repo.get_tier_s_run("r-open")
        assert kept is not None
        self.assertEqual(kept["status"], "waiting")
        gone = await self.repo.get_tier_s_run("r-gone")
        assert gone is not None
        self.assertEqual(gone["status"], "waiting")

    async def test_supersede_and_resumable_roundtrip(self) -> None:
        await self._run("r-keep", "t-x", "running")
        await self._run("r-park", "t-x", "waiting",
                        checkpoint={"phase": "review", "branch": "b"})
        await self._run("r-plain", "t-x", "waiting")

        found = await self.repo.resumable_tier_s_run("t-x")
        assert found is not None
        self.assertEqual(found["run_id"], "r-park")

        closed = await self.repo.supersede_tier_s_runs("t-x", "r-new")
        self.assertEqual(closed, 3)
        for run_id in ("r-keep", "r-park", "r-plain"):
            run = await self.repo.get_tier_s_run(run_id)
            assert run is not None
            self.assertEqual(run["status"], "superseded")
            self.assertEqual(run["superseded_by"], "r-new")


if __name__ == "__main__":
    unittest.main()
