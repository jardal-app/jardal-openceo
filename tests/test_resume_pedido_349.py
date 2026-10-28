"""Resume helper for a terminally failed pedido (TASK-2026-00349, F4).

Contract fixed here: ``resume_failed_pedido`` requeues a FAILED
pedido to ``queued`` (so the dispatcher admits it), resets the planner
bookkeeping (``planner_attempts``/``planner_last_invalid``) and clears
the error/waiting markers, then advances the CEO trigger to now so the
next review replans it. Refusals (unknown ticket, decided work,
non-failed status, missing trigger) and ``dry_run`` write nothing.

No network, no CLI, no LLM. Repositories are faked.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ops.resume_pedido import resume_failed_pedido  # noqa: E402
from state import TaskStatus  # noqa: E402


def _failed_pedido(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "pedido-344", "project": "proj", "agent": "ceo",
        "source": "api", "title": "Pedido Shop necesita checkout",
        "description": "Descripcion", "attachments": [],
        "priority": "high", "status": TaskStatus.FAILED.value,
        "steps": [],
        "params": {
            "planner_attempts": 3,
            "planner_last_invalid": "{bad json}",
            "shop": "keep-me",
        },
        "error": "planner failed 3x: materiality",
        "waiting_reason": None,
        "resume_at": None,
    }
    task.update(overrides)
    return task


_MISSING: Any = object()


class _FakeRepo:
    def __init__(self, task: dict[str, Any] | None,
                 trigger: Any = _MISSING) -> None:
        self.task = dict(task) if task else None
        if trigger is _MISSING:
            trigger = {
                "next_scheduled_at": datetime(2026, 11, 4,
                                              tzinfo=timezone.utc),
                "pending_reasons": [],
            }
        self.trigger = dict(trigger) if trigger is not None else None
        self.patches: list = []
        self.saves: list = []

    async def get_task(self, task_id: str):
        if self.task is None or task_id != self.task["task_id"]:
            return None
        return dict(self.task)

    async def patch_task(self, task_id: str, updates: dict):
        self.patches.append((task_id, dict(updates)))
        assert self.task is not None
        self.task.update(updates)
        return dict(self.task)

    async def get_ceo_trigger(self):
        return dict(self.trigger) if self.trigger is not None else None

    async def save_ceo_trigger(self, doc: dict) -> None:
        self.saves.append(dict(doc))
        self.trigger = dict(doc)


_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class ResumeFailedPedidoTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_resets_planner_and_advances_trigger(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        report = await resume_failed_pedido(repo, "pedido-344", now=_NOW)

        self.assertTrue(report["ok"])
        self.assertEqual(report["cleared_params"],
                         ["planner_attempts", "planner_last_invalid"])
        self.assertIsNone(report["verified_attempts"])
        self.assertEqual(repo.task["params"], {"shop": "keep-me"})
        self.assertEqual(repo.task["status"], TaskStatus.QUEUED.value)
        self.assertEqual(report["verified_status"], TaskStatus.QUEUED.value)
        self.assertIsNone(repo.task["error"])
        self.assertEqual(repo.saves[-1]["next_scheduled_at"], _NOW)

    async def test_dry_run_writes_nothing(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        report = await resume_failed_pedido(repo, "pedido-344", now=_NOW,
                                            dry_run=True)

        self.assertTrue(report["ok"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.saves, [])

    async def test_refuses_non_failed_without_writes(self) -> None:
        repo = _FakeRepo(_failed_pedido(status=TaskStatus.WAITING.value))

        report = await resume_failed_pedido(repo, "pedido-344", now=_NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "not_failed")
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.saves, [])

    async def test_refuses_decided_ticket_without_writes(self) -> None:
        repo = _FakeRepo(_failed_pedido(coding_tool="tier_b"))

        report = await resume_failed_pedido(repo, "pedido-344", now=_NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "not_a_pedido")
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.saves, [])

    async def test_refuses_unknown_ticket(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        report = await resume_failed_pedido(repo, "missing", now=_NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "task_not_found")
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.saves, [])

    async def test_refuses_missing_trigger_without_writes(self) -> None:
        repo = _FakeRepo(_failed_pedido(), trigger=None)

        report = await resume_failed_pedido(repo, "pedido-344", now=_NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "trigger_missing")
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.saves, [])


if __name__ == "__main__":
    unittest.main()
