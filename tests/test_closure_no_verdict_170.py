"""Closer without verdicts parks the objective (TASK-2026-00170).

Contract fixed here:

- A missing verdict is never FALSO: after the one bounded retry, any
  finding still without a verdict parks the objective as
  ``closer_no_verdict`` with the attempts as evidence (finding ids,
  return codes, closure-file state). Silence, partial coverage,
  malformed output and exit-code/timeout failures all park; nothing
  closes and no partial fix ticket is created.
- An explicit FALSO is a real judgment and still closes; an explicit
  REAL still becomes a fix ticket. A retry that completes the coverage
  lets the close proceed.
- The park preserves the findings: the reviewed SHA does not advance
  (covered by the corrected ``test_stale_closure_...`` in
  ``test_review_quorum_162.py``), so the next final review re-derives
  the same findings.

Simulated executors; no models.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _close_objective,
)
from orchestration.live import TIMEOUT_RETURNCODE, ExecResult  # noqa: E402


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
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _finding(item_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=item_id, severity="MAJOR", category="bug",
                           anchors=[], problem="p", fix="f",
                           families=["f1"])


def _objective_doc(objective_id: str) -> dict[str, Any]:
    return {
        "objective_id": objective_id,
        "project": "proj",
        "status": "active",
        "anchor": {"goal": "g", "acceptance": ["ship"]},
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "tickets_log": [],
        "conversion_source_task_id": "task-parent",
    }


def _env(repo: Path, executor: Any,
         ) -> tuple[ObjectiveEnv, list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        task_id = f"task-child-{counter['n']}"
        created.append({"task_id": task_id, **dict(payload)})
        return {"task_id": task_id}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister({}),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=executor,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=repo,
    )
    return env, created


class _ScriptedCloser:
    """Closer script: per-run (returncode, file text, usage, append)."""

    def __init__(
        self,
        repo: Path,
        script: list[tuple[int, str | None, dict[str, Any] | None, bool]],
    ) -> None:
        self._repo = repo
        self._script = script
        self._target: Path | None = None
        self.runs = 0

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del workdir, timeout_s
        assert role == "closer", role
        step = self._script[min(self.runs, len(self._script) - 1)]
        self.runs += 1
        code, text, usage, append = step
        if text is not None:
            target = self._target
            if target is None:
                for line in prompt.splitlines():
                    if line.startswith("Write ") \
                            and " with one line per finding" in line:
                        rel = line[len("Write "):].split(
                            " with one line")[0]
                        target = self._repo / rel.strip()
                        self._target = target
                        break
            assert target is not None, "closer prompt names no target"
            target.parent.mkdir(parents=True, exist_ok=True)
            if append and target.exists():
                target.write_text(
                    target.read_text(encoding="utf-8") + text,
                    encoding="utf-8")
            else:
                target.write_text(text, encoding="utf-8")
        return ExecResult(code, 1.0, output_tail="", usage=usage)

    async def cleanup(self, role: str) -> None:
        del role


class CloserNoVerdictTests(unittest.IsolatedAsyncioTestCase):
    async def _close(self, script: list[tuple[int, str | None,
                                              dict[str, Any] | None, bool]],
                     findings: list[SimpleNamespace],
                     ) -> tuple[dict[str, Any], list[dict[str, Any]],
                                _ScriptedCloser, ObjectiveEnv]:
        repo = _scratch_repo(self)
        closer = _ScriptedCloser(repo, script)
        env, created = _env(repo, closer)
        await env.objectives.create_objective(_objective_doc("OBJ-1"))
        objective = await env.objectives.get_objective("OBJ-1")
        assert objective is not None
        result = await _close_objective(env, objective, findings, "head")
        return result, created, closer, env

    async def test_silent_closer_parks_with_evidence(self) -> None:
        result, created, closer, env = await self._close(
            [(0, None, None, False)], [_finding(1)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("F1", result["detail"])
        self.assertIn("2 runs", result["detail"])
        self.assertIn("rc=0,rc=0", result["detail"])
        self.assertIn("empty", result["detail"])
        self.assertEqual(closer.runs, 2)
        self.assertEqual(created, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("closer_no_verdict", stored["waiting_reason"])

    async def test_partial_coverage_parks_without_partial_tickets(
        self,
    ) -> None:
        result, created, closer, _env = await self._close(
            [(0, "F1: REAL — a\n", None, False)],  # retry rewrites the same
            [_finding(1), _finding(2)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("F2", result["detail"])
        self.assertNotIn("F1,", result["detail"])
        self.assertEqual(closer.runs, 2)
        # All or nothing: the judged REAL finding waits with the missing
        # one instead of becoming a lone fix ticket.
        self.assertEqual(created, [])

    async def test_malformed_closure_parks(self) -> None:
        result, created, _closer, _env = await self._close(
            [(0, "looks fine to me\nno verdicts here\n", None, False)],
            [_finding(1)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("2 lines, 0 parsed verdicts", result["detail"])
        self.assertEqual(created, [])

    async def test_exit_code_failure_is_recorded(self) -> None:
        result, _created, _closer, _env = await self._close(
            [(1, None, None, False)], [_finding(1)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("rc=1,rc=1", result["detail"])

    async def test_timeout_is_recorded(self) -> None:
        result, _created, _closer, _env = await self._close(
            [(TIMEOUT_RETURNCODE, None, {"timeout": True}, False)],
            [_finding(1)])

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "closer_no_verdict")
        self.assertIn("timeout", result["detail"])

    async def test_explicit_falso_judgment_still_closes(self) -> None:
        result, created, closer, env = await self._close(
            [(0, "F1: FALSO — not reachable\n", None, False)],
            [_finding(1)])

        self.assertEqual(result["action"], "closed")
        self.assertEqual(closer.runs, 1)
        self.assertEqual(created, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "closed")

    async def test_mixed_real_and_falso_creates_one_fix_ticket(
        self,
    ) -> None:
        result, created, _closer, _env = await self._close(
            [(0, "F1: REAL — a\nF2: FALSO — not reachable\n", None, False)],
            [_finding(1), _finding(2)])

        self.assertEqual(result["action"], "fix-tickets")
        self.assertEqual(len(created), 1)
        self.assertIn(":1:", str(created[0].get("source_id")))

    async def test_retry_can_complete_coverage(self) -> None:
        result, created, closer, _env = await self._close(
            [(0, "F1: REAL — a\n", None, False),
             (0, "F2: FALSO — not reachable\n", None, True)],
            [_finding(1), _finding(2)])

        self.assertEqual(result["action"], "fix-tickets")
        self.assertEqual(closer.runs, 2)
        self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
