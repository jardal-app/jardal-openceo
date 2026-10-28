"""Planner materiality enum + invalid JSON preserved (TASK-2026-00347).

TASK-2026-00344 failed 3/3 in plan_pedido with
``information_gaps.0.materiality: Input should be 'low', 'medium', 'high'
or 'critical'`` and the offending value was lost. The pedido prompt now
names the enum with a valid example, and a twice-invalid
PLANNER_RESULT.json is preserved on the ticket params (and on disk) for
diagnosis.

No network, no CLI, no LLM: the planner runs simulated.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
)
from state import TaskStatus  # noqa: E402


def _output_target(repo: Path, prompt: str) -> Path:
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0
        self._target: Path | None = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload["__raw__"]
                          if "__raw__" in payload
                          else json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _pedido_env(test: unittest.TestCase, repo: Path,
                payloads: list[dict[str, Any]],
                ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                           list[dict[str, Any]], dict[str, dict[str, Any]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: decide the pedido, then stop.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    patches: dict[str, dict[str, Any]] = {}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        created.append(dict(payload))
        return {"task_id": f"task-new-{len(created)}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    planner = _RecordedPlanner(repo, payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister({}),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=repo,
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True},
                       "secretary": {"enabled": True}},
        },
    )
    test.addCleanup(lambda: None)
    return env, planner, created, patches


def _pedido(**overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "task_id": "task-pedido-1",
        "title": "Undecided loose ticket",
        "description": "no agent, no tier, no target",
        "project": "proj",
        "source": "api",
        "agent": "ceo",
        "status": TaskStatus.QUEUED.value,
        "params": {},
    }
    ticket.update(overrides)
    params = dict(ticket.get("params") or {})
    if "params" in overrides and isinstance(overrides["params"], dict):
        params = dict(overrides["params"])
    ticket["params"] = params
    return ticket


def _gaps_result(materiality: str) -> dict[str, Any]:
    return {
        "objective_status": "capability_gap",
        "state_summary": "outgrows one ticket",
        "information_gaps": [{"gap": "missing budget ceiling",
                              "materiality": materiality,
                              "resolution": "ask the owner"}],
        "tickets": [],
        "roadmap_remaining": [],
    }


class PedidoPromptMaterialityTests(unittest.TestCase):
    def test_pedido_prompt_names_enum_with_example(self) -> None:
        prompt = engine_mod._pedido_prompt(_pedido())
        self.assertIn("low|medium|high|critical", prompt)
        self.assertIn('"high"', prompt)
        self.assertNotIn('"information_gaps": [{...}]', prompt)


class ParseMaterialityTests(unittest.TestCase):
    def test_valid_materiality_parses(self) -> None:
        result = planner_mod.parse_planner_result(
            json.dumps(_gaps_result("high")))
        self.assertEqual(result.information_gaps[0].materiality, "high")

    def test_invalid_materiality_names_field(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            planner_mod.parse_planner_result(
                json.dumps(_gaps_result("urgent")))
        self.assertIn("information_gaps.0.materiality", str(ctx.exception))


class PedidoMaterialityTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_materiality_converts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, patches = _pedido_env(
                self, repo, [_gaps_result("high")])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["action"], "convert")
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertNotIn("planner_last_invalid", params)

    async def test_invalid_materiality_is_rejected_and_preserved(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, planner, _created, patches = _pedido_env(
                self, repo, [_gaps_result("urgent")])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "planner_error")
            self.assertIn("materiality", out["detail"])
            self.assertEqual(planner.runs, 2)
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertEqual(params.get("planner_attempts"), 1)
            preserved = params.get("planner_last_invalid") or ""
            self.assertIn("urgent", preserved)
            on_disk = (repo / ".openceo" / "loose" / "task-pedido-1"
                       / "PLANNER_RESULT.json").read_text(encoding="utf-8")
            self.assertIn("urgent", on_disk)

    async def test_stale_invalid_marker_cleared_on_success(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, patches = _pedido_env(
                self, repo, [_gaps_result("medium")])
            out = await engine_mod.plan_pedido(
                env, _pedido(params={"planner_attempts": 1,
                                     "planner_last_invalid": "stale"}))
            self.assertTrue(out["ok"], out)
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertNotIn("planner_last_invalid", params)
            self.assertNotIn("planner_attempts", params)


if __name__ == "__main__":
    unittest.main()
