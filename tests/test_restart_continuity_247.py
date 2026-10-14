"""Restart continuity (TASK-2026-00247).

Recovery of interrupted tickets at boot, the close checkpoint that survives
the restart of our own deploy, and the explicit night-cycle target. Fakes
only, 0 tokens: an in-memory repo stands in for Mongo, a scripted fetch
for the ticket API, and tmp files for the checkpoint.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from orchestration.close_checkpoint import (  # noqa: E402
    CloseCheckpoint,
    clear_close_checkpoint,
    complete_close_once,
    load_close_checkpoint,
    save_close_checkpoint,
)
from orchestration.night_cycle import (  # noqa: E402
    NIGHT_EPIC_DEFAULT,
    NIGHT_ORDER_DEFAULT,
    NIGHT_PROJECT_DEFAULT,
    NightTarget,
    TransientFetchError,
    select_next_eligible,
)
from orchestration.restart_recovery import (  # noqa: E402
    MAX_RECOVERY_ATTEMPTS,
    decide,
    recover_interrupted,
)
from state import TaskStatus  # noqa: E402

_NOW = datetime(2026, 10, 14, 6, 0, 0, tzinfo=timezone.utc)


def _ticket(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "status": TaskStatus.INTERRUPTED.value,
        "coding_tool": "tier_b",
        "autorun": True,
        "waiting_reason": None,
        "resume_at": None,
        "qa_review_state": None,
        "params": {
            "ticket_outputs": {"report": "done"},
            "engine": "muse-code",
        },
    }
    doc.update(overrides)
    return doc


class FakeRepo:
    """Dict-backed repo with the recovery surface (conditional rewrite)."""

    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        self.tasks = {t["task_id"]: t for t in (tasks or [])}
        self.writes = 0

    async def list_tasks_by_status(
        self, status: Any, limit: int = 200
    ) -> list[dict[str, Any]]:
        want = getattr(status, "value", status)
        return [
            dict(t) for t in self.tasks.values()
            if str(t.get("status") or "") == str(want)
        ][:limit]

    async def apply_restart_recovery(
        self,
        task_id: str,
        *,
        status: str,
        waiting_reason: str | None,
        recovery_meta: dict[str, Any],
    ) -> bool:
        doc = self.tasks.get(str(task_id))
        if doc is None or doc.get("status") != TaskStatus.INTERRUPTED.value:
            return False
        params = dict(doc.get("params") or {})
        params["_recovery"] = dict(recovery_meta)
        doc.update({
            "status": status,
            "waiting_reason": waiting_reason,
            "resume_at": None,
            "params": params,
        })
        self.writes += 1
        return True


class DecideTests(unittest.TestCase):
    def test_interrupted_requeues_with_attempt_one(self) -> None:
        decision = decide(_ticket("t1"), now=_NOW)
        self.assertEqual(decision.action, "requeue")
        self.assertEqual(decision.status, TaskStatus.QUEUED.value)
        assert decision.recovery_meta is not None
        self.assertEqual(decision.recovery_meta["attempt"], 1)
        self.assertEqual(decision.recovery_meta["from"], "interrupted")

    def test_non_interrupted_never_moves(self) -> None:
        for status in ("queued", "waiting", "completed", "cancelled",
                       "failed", "in_progress"):
            with self.subTest(status=status):
                decision = decide(_ticket("t1", status=status))
                self.assertEqual(decision.action, "skip")
                self.assertEqual(decision.reason, "not_interrupted")

    def test_tier_s_stays_manual(self) -> None:
        decision = decide(_ticket("t1", coding_tool="tier_s"))
        self.assertEqual((decision.action, decision.reason),
                         ("skip", "tier_s_manual"))

    def test_autorun_off_skips_and_missing_allows(self) -> None:
        self.assertEqual(decide(_ticket("t1", autorun=False)).reason,
                         "autorun_off")
        doc = _ticket("t1")
        del doc["autorun"]
        self.assertEqual(decide(doc).action, "requeue")

    def test_live_process_skips(self) -> None:
        decision = decide(_ticket("t1"), is_alive=lambda tid: tid == "t1")
        self.assertEqual((decision.action, decision.reason),
                         ("skip", "process_alive"))

    def test_human_holds_park_in_waiting_with_markers(self) -> None:
        cases = [
            {"secret_block": {"secret": "X"}},
            {"owner_question_id": "q1"},
        ]
        for params in cases:
            with self.subTest(params=params):
                base = dict(_ticket("t1")["params"])
                base.update(params)
                decision = decide(_ticket("t1", params=base))
                self.assertEqual(decision.action, "to_waiting")
                self.assertTrue(decision.reason.startswith(
                    "human_wait_preserved"))
        decision = decide(_ticket("t1", qa_review_state="pending"))
        self.assertEqual(decision.action, "to_waiting")

    def test_exhausted_attempts_park_for_manual_relaunch(self) -> None:
        params = dict(_ticket("t1")["params"])
        params["_recovery"] = {"attempt": MAX_RECOVERY_ATTEMPTS}
        decision = decide(_ticket("t1", params=params))
        self.assertEqual((decision.action, decision.reason),
                         ("to_waiting", "recovery_exhausted"))

    def test_open_blockers_still_requeue_for_dispatcher_parking(self) -> None:
        decision = decide(_ticket("t1", blocked_by=["other"]))
        self.assertEqual(decision.action, "requeue")


class RecoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_boot_pass_applies_once(self) -> None:
        repo = FakeRepo([
            _ticket("run-1"),
            _ticket("run-2", autorun=False),
            _ticket("run-3", coding_tool="tier_s"),
            _ticket("run-4", params={"secret_block": {"secret": "X"}}),
            _ticket("done-1", status="completed"),
        ])
        result = await recover_interrupted(repo)
        self.assertEqual(result["requeued"], ["run-1"])
        self.assertEqual(result["to_waiting"], ["run-4"])
        self.assertEqual(result["skipped"].get("autorun_off"), 1)
        self.assertEqual(result["skipped"].get("tier_s_manual"), 1)
        again = await recover_interrupted(repo)
        self.assertEqual(again["requeued"], [])
        self.assertEqual(again["to_waiting"], [])
        self.assertEqual(repo.writes, 2)

    async def test_requeue_preserves_params_and_bumps_attempt(self) -> None:
        repo = FakeRepo([_ticket("run-1")])
        await recover_interrupted(repo)
        doc = repo.tasks["run-1"]
        self.assertEqual(doc["status"], "queued")
        self.assertEqual(doc["params"]["ticket_outputs"], {"report": "done"})
        self.assertEqual(doc["params"]["engine"], "muse-code")
        self.assertEqual(doc["params"]["_recovery"]["attempt"], 1)
        doc["status"] = "interrupted"
        await recover_interrupted(repo)
        self.assertEqual(repo.tasks["run-1"]["params"]["_recovery"]["attempt"], 2)

    async def test_concurrent_cancel_wins_over_recovery(self) -> None:
        repo = FakeRepo([_ticket("run-1")])
        real = repo.apply_restart_recovery

        async def cancel_first(task_id: str, **kwargs: Any) -> bool:
            repo.tasks[task_id]["status"] = "cancelled"
            return await real(task_id, **kwargs)

        repo.apply_restart_recovery = cancel_first  # type: ignore[method-assign]
        result = await recover_interrupted(repo)
        self.assertEqual(result["requeued"], [])
        self.assertEqual(result["skipped"].get("already_moved"), 1)
        self.assertEqual(repo.tasks["run-1"]["status"], "cancelled")

    async def test_unreadable_list_reports_without_raising(self) -> None:
        class BrokenRepo(FakeRepo):
            async def list_tasks_by_status(
                self, status: Any, limit: int = 200
            ) -> list[dict[str, Any]]:
                raise RuntimeError("mongo down")

        result = await recover_interrupted(BrokenRepo())
        self.assertEqual(result["skipped"], {"list_failed": 1})


class CheckpointTests(unittest.TestCase):
    def test_roundtrip_and_missing(self) -> None:
        with self.subTest("missing reads None"):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "close_checkpoint.json"
                self.assertIsNone(load_close_checkpoint(path))
                self.assertFalse(clear_close_checkpoint(path))
                checkpoint = CloseCheckpoint(
                    task_id="t1", ticket="TASK-2026-00247",
                    published={"commit": "abc"}, verified=["tests"],
                    pending={"smoke": True, "ticket_close": True},
                )
                save_close_checkpoint(path, checkpoint)
                back = load_close_checkpoint(path)
                assert back is not None
                self.assertEqual(back.ticket, "TASK-2026-00247")
                self.assertEqual(back.published, {"commit": "abc"})
                self.assertTrue(back.created_at)
                self.assertTrue(clear_close_checkpoint(path))
                self.assertIsNone(load_close_checkpoint(path))

    def test_corrupt_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "close_checkpoint.json"
            path.write_text("{nope", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_close_checkpoint(path)

    def test_complete_once_never_repeats(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "close_checkpoint.json"
            save_close_checkpoint(path, CloseCheckpoint(ticket="T1"))
            calls: list[str] = []
            first = complete_close_once(path, lambda c: calls.append(c.ticket))
            second = complete_close_once(path, lambda c: calls.append(c.ticket))
            self.assertEqual((first["completed"], second["completed"]),
                             (True, False))
            self.assertEqual(calls, ["T1"])

    def test_failing_complete_keeps_checkpoint(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "close_checkpoint.json"
            save_close_checkpoint(path, CloseCheckpoint(ticket="T1"))

            def _boom(checkpoint: CloseCheckpoint) -> None:
                raise RuntimeError("smoke down")

            with self.assertRaises(RuntimeError):
                complete_close_once(path, _boom)
            assert load_close_checkpoint(path) is not None


def _child(name: str, status: str,
            depends_on: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "depends_on": depends_on or []}


class NightCycleTests(unittest.TestCase):
    def test_defaults_pin_epic(self) -> None:
        self.assertEqual(NIGHT_PROJECT_DEFAULT, "jardal-openceo")
        self.assertEqual(NIGHT_EPIC_DEFAULT, "TASK-2026-00245")
        self.assertEqual(NIGHT_ORDER_DEFAULT,
                         ("TASK-2026-00247", "TASK-2026-00244", "TASK-2026-00246"))

    def test_order_and_dependencies(self) -> None:
        children = [
            _child("TASK-2026-00246", "Open", ["TASK-2026-00244"]),
            _child("TASK-2026-00247", "Working"),
            _child("TASK-2026-00244", "Open"),
        ]
        self.assertEqual(select_next_eligible(children)["name"],
                         "TASK-2026-00247")
        children[1]["status"] = "Completed"
        self.assertEqual(select_next_eligible(children)["name"],
                         "TASK-2026-00244")
        children[2]["status"] = "Completed"
        self.assertEqual(select_next_eligible(children)["name"],
                         "TASK-2026-00246")

    def test_closed_in_either_vocabulary_is_skipped(self) -> None:
        children = [
            _child("TASK-2026-00247", "completed"),
            _child("TASK-2026-00244", "Cancelled"),
            _child("TASK-2026-00246", "Open", ["TASK-2026-00244"]),
        ]
        self.assertEqual(select_next_eligible(children)["name"],
                         "TASK-2026-00246")
        for child in children:
            child["status"] = "Completed"
        self.assertIsNone(select_next_eligible(children))

    def test_transient_outage_preserves_target_then_resumes(self) -> None:
        target = NightTarget()
        children = [_child("TASK-2026-00247", "Working")]
        calls = {"n": 0}

        def fetch() -> list[dict[str, Any]]:
            calls["n"] += 1
            if calls["n"] == 1:
                return children
            if calls["n"] == 2:
                raise TransientFetchError("api down")
            return children

        first = target.refresh(fetch)
        self.assertEqual((first["stale"], first["done"]), (False, False))
        self.assertEqual(first["pick"]["name"], "TASK-2026-00247")
        during = target.refresh(fetch)
        self.assertTrue(during["stale"])
        self.assertEqual(during["pick"]["name"], "TASK-2026-00247")
        self.assertEqual((target.project, target.epic),
                         (NIGHT_PROJECT_DEFAULT, NIGHT_EPIC_DEFAULT))
        after = target.refresh(fetch)
        self.assertEqual((after["stale"], after["done"]), (False, False))
        self.assertEqual(after["pick"]["name"], "TASK-2026-00247")

    def test_done_when_nothing_eligible(self) -> None:
        target = NightTarget()
        result = target.refresh(
            lambda: [_child("TASK-2026-00247", "Completed")])
        self.assertTrue(result["done"])
        self.assertIsNone(result["pick"])


class CloseFlowTests(unittest.TestCase):
    def test_publish_restart_close_next_without_repeats(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "close_checkpoint.json"
            save_close_checkpoint(path, CloseCheckpoint(
                task_id="t-247", ticket="TASK-2026-00247",
                published={"merged": True, "commit": "abc"},
                verified=["tests", "gate"],
                pending={"smoke": True, "ticket_close": True},
            ))
            effects: list[str] = []

            def _close(checkpoint: CloseCheckpoint) -> None:
                effects.append(f"smoke:{checkpoint.ticket}")
                effects.append(f"close:{checkpoint.ticket}")

            children = [
                _child("TASK-2026-00247", "Completed"),
                _child("TASK-2026-00244", "Open"),
                _child("TASK-2026-00246", "Open", ["TASK-2026-00244"]),
            ]
            first = complete_close_once(path, _close)
            nxt = select_next_eligible(children)
            second = complete_close_once(path, _close)
            self.assertTrue(first["completed"])
            self.assertFalse(second["completed"])
            self.assertEqual(effects, ["smoke:TASK-2026-00247",
                                       "close:TASK-2026-00247"])
            assert nxt is not None
            self.assertEqual(nxt["name"], "TASK-2026-00244")


if __name__ == "__main__":
    unittest.main()
