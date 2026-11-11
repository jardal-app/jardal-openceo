"""Brief-attach failure resume with simulated services; no network/models."""

from __future__ import annotations

import base64
import copy
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "openceo-core"))
sys.path.insert(0, str(ROOT / "tests"))

from objectives import engine as engine_mod  # noqa: E402
from objectives.planner_models import PlannerResult  # noqa: E402
from objectives.service import ObjectiveErpAdapter  # noqa: E402
from test_single_planner_204 import _RecordedPlanner  # noqa: E402


def _dev(**extra: Any) -> dict[str, Any]:
    base = dict(local_ref="dev", title="Implement safe retries", target_agent="dev",
                work_type="implementation", goal="Implement retries", why_now="Safe delivery",
                acceptance_criteria=["Retries preserve idempotency"],
                expected_artifacts=["implementation"], coding_tool="tier_s",
                tier_reason="Consensus protects idempotency",
                why_consensus_is_needed="Different models challenge failure modes",
                why_single_session_is_insufficient="One model can miss duplicate charges")
    base.update(extra)
    return base


def _plan(*tickets: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = dict(objective_status="active", state_summary="Next justified work",
                  tickets=list(tickets), roadmap_remaining=[])
    result.update(extra)
    return result


class _Files:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.fail_copy = False

    async def thread_of(self, source: str, **_: Any) -> dict[str, Any]:
        return {"attachments": [copy.deepcopy(item) for item in self.files.values()
                                if item["attached_to"] == source]}

    async def get_attachment(self, key: str, **_: Any) -> dict[str, Any]:
        return {"ok": True, "attachment": copy.deepcopy(self.files[key])}

    async def attach_file(self, target: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail_copy:
            raise RuntimeError("simulated storage failure")
        key = "copy-" + target
        item = dict(id=key, file_name=kwargs["file_name"], private=kwargs["private"],
                    attached_to=target, content_text=base64.b64decode(
                        kwargs["content_base64"]).decode("utf-8"))
        self.files[key] = item
        return {"ok": True, "attachment": copy.deepcopy(item),
                "ticket": {"name": target}}


class BriefAttachResumeTests(unittest.IsolatedAsyncioTestCase):
    def _env(self, *plans: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        tasks: dict[str, dict[str, Any]] = {}
        created: list[dict[str, Any]] = []
        by_source: dict[str, dict[str, Any]] = {}
        files = _Files()
        planner = _RecordedPlanner(repo, list(plans))

        async def create(payload: dict[str, Any]) -> dict[str, Any]:
            # Production create_task is idempotent per (source, source_id).
            key = f"{payload.get('source')}:{payload.get('source_id')}"
            if key in by_source:
                return copy.deepcopy(by_source[key])
            row = dict(copy.deepcopy(payload), task_id=f"child-{len(created) + 1}",
                       status="queued")
            params = dict(row.get("params") or {})
            params["_mirrors"] = {
                "erpnext": {"name": f"TASK-{row['task_id']}".upper()}}
            row["params"] = params
            created.append(copy.deepcopy(row))
            by_source[key] = row
            tasks[row["task_id"]] = row
            return copy.deepcopy(row)

        async def patch(task_id: str, changes: dict[str, Any]) -> None:
            tasks[task_id].update(copy.deepcopy(changes))

        env = engine_mod.ObjectiveEnv(
            objectives=engine_mod.InMemoryObjectiveStore(),
            tasks=engine_mod.InMemoryTaskLister(tasks), create_ticket=create,
            patch_task=patch, executor=planner, quota=MagicMock(), slots=MagicMock(),
            reviews=None, inbox=None, notify=AsyncMock(), repo=repo,
            erp=ObjectiveErpAdapter(files), dispatcher_wake=MagicMock(),
            planner_cfg={"agents": {"dev": {"enabled": True},
                                    "research": {"enabled": True}}})
        return env, planner, created, files

    async def _objective(self, env: Any) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-754", "project": "jardal-openceo", "status": "active",
            "anchor": {"goal": "Safe retries", "acceptance": ["Idempotent"]},
            "capsule": {}, "tickets_log": []})

    def _completed_brief(self, env: Any, files: _Files) -> None:
        env.tasks.tasks["research"] = dict(
            task_id="research", objective_id="OBJ-754", status="completed",
            title="Verify provider", agent_hint="research",
            params={"_mirrors": {"erpnext": {"name": "TASK-RESEARCH"}}})
        files.files["brief-754"] = dict(
            id="brief-754", attached_to="TASK-RESEARCH", private=True,
            file_name="brief.md", content_text="Verified: same idempotency key.")

    async def test_attach_failure_records_pending_with_run_tag(self) -> None:
        decision = dict(needed=True, reason="Use verified retry contract",
                        brief_ids=["brief-754"])
        env, _, created, files = self._env(_plan(_dev(research=decision)))
        await self._objective(env)
        self._completed_brief(env, files)
        files.fail_copy = True
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            await engine_mod.plan_objective(env, "OBJ-754")
        ticket = env.tasks.tasks["child-1"]
        self.assertEqual(ticket["status"], "waiting")
        self.assertEqual(ticket["waiting_reason"],
                         engine_mod.BRIEF_ATTACH_WAITING_REASON)
        self.assertFalse(ticket["autorun"])
        env.dispatcher_wake.assert_not_called()
        pending = (await env.objectives.get_objective("OBJ-754") or {}).get(
            engine_mod.BRIEF_ATTACH_PENDING_FIELD)
        self.assertEqual(len(pending), 1)
        entry = pending[0]
        self.assertEqual(entry["task_id"], "child-1")
        self.assertEqual(entry["key"], "dev")
        self.assertEqual(entry["brief_ids"], ["brief-754"])
        self.assertRegex(entry["run_tag"], r"^[0-9a-f]{8}$")
        datetime.fromisoformat(entry["held_at"])
        self.assertTrue(created[0]["source_id"].endswith(":" + entry["run_tag"]))

    async def test_next_cycle_heals_held_ticket_on_same_ticket(self) -> None:
        decision = dict(needed=True, reason="Use verified retry contract",
                        brief_ids=["brief-754"])
        env, _, created, files = self._env(
            _plan(_dev(research=decision)),
            _plan(_dev(local_ref="docs", title="Document safe retries")))
        await self._objective(env)
        self._completed_brief(env, files)
        files.fail_copy = True
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            await engine_mod.plan_objective(env, "OBJ-754")
        tag = (await env.objectives.get_objective("OBJ-754") or {})[
            engine_mod.BRIEF_ATTACH_PENDING_FIELD][0]["run_tag"]
        files.fail_copy = False
        out = await engine_mod.plan_objective(env, "OBJ-754")
        self.assertTrue(out["ok"], out)
        healed = env.tasks.tasks["child-1"]
        self.assertEqual(healed["status"], "queued")
        self.assertIsNone(healed["waiting_reason"])
        self.assertTrue(healed["autorun"])
        copied = files.files["copy-TASK-CHILD-1"]
        self.assertTrue(copied["private"])
        self.assertEqual(copied["content_text"],
                         files.files["brief-754"]["content_text"])
        objective = await env.objectives.get_objective("OBJ-754") or {}
        self.assertIsNone(objective.get(engine_mod.BRIEF_ATTACH_PENDING_FIELD))
        self.assertEqual([row["task_id"] for row in created], ["child-1", "child-2"])
        # Reconciled: the new plan is new work, so it mints a fresh tag.
        fresh = created[1]["source_id"].rsplit(":", 1)[-1]
        self.assertRegex(fresh, r"^[0-9a-f]{8}$")
        self.assertNotEqual(fresh, tag)
        env.dispatcher_wake.assert_called_once_with()

    async def test_unhealed_hold_mints_fresh_tag_until_reconciled(self) -> None:
        decision = dict(needed=True, reason="Use verified retry contract",
                        brief_ids=["brief-754"])
        env, _, created, files = self._env(
            _plan(_dev(research=decision)),
            _plan(_dev(title="Document safe retries")),
            _plan(_dev(local_ref="more", title="More work")))
        await self._objective(env)
        self._completed_brief(env, files)
        files.fail_copy = True
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            await engine_mod.plan_objective(env, "OBJ-754")
        tag = (await env.objectives.get_objective("OBJ-754") or {})[
            engine_mod.BRIEF_ATTACH_PENDING_FIELD][0]["run_tag"]
        # ERP still down: the hold survives but the new plan mints a fresh
        # tag, so the reused local_ref "dev" cannot collide with child-1.
        out = await engine_mod.plan_objective(env, "OBJ-754")
        self.assertTrue(out["ok"], out)
        self.assertEqual(env.tasks.tasks["child-1"]["status"], "waiting")
        pending = (await env.objectives.get_objective("OBJ-754") or {})[
            engine_mod.BRIEF_ATTACH_PENDING_FIELD]
        self.assertEqual([entry["task_id"] for entry in pending], ["child-1"])
        self.assertEqual(created[1]["task_id"], "child-2")
        fresh_hold = created[1]["source_id"].rsplit(":", 1)[-1]
        self.assertRegex(fresh_hold, r"^[0-9a-f]{8}$")
        self.assertNotEqual(fresh_hold, tag)
        env.dispatcher_wake.assert_not_called()
        # ERP back: the hold heals and the next plan mints a fresh tag.
        files.fail_copy = False
        out = await engine_mod.plan_objective(env, "OBJ-754")
        self.assertTrue(out["ok"], out)
        self.assertEqual(env.tasks.tasks["child-1"]["status"], "queued")
        objective = await env.objectives.get_objective("OBJ-754") or {}
        self.assertIsNone(objective.get(engine_mod.BRIEF_ATTACH_PENDING_FIELD))
        self.assertEqual([row["task_id"] for row in created],
                         ["child-1", "child-2", "child-3"])
        fresh = created[2]["source_id"].rsplit(":", 1)[-1]
        self.assertNotEqual(fresh, tag)
        env.dispatcher_wake.assert_called_once_with()

    async def test_missing_brief_fails_before_creating_anything(self) -> None:
        env, _, _, _ = self._env()
        create = AsyncMock(return_value=None)
        env.create_ticket = create
        result = PlannerResult.model_validate(_plan(
            _dev(local_ref="plain", title="Plain work"),
            _dev(local_ref="needy", title="Needy work", research=dict(
                needed=True, reason="Use the brief", brief_ids=["ghost-brief"]))))
        with self.assertRaises(engine_mod.ObjectiveErpError):
            await engine_mod._create_planned_tickets(
                env, {"objective_id": "OBJ-754", "project": "proj",
                      "erp_group_ref": ""}, result, research_briefs={})
        create.assert_not_called()

    async def test_heal_reconciles_gone_terminal_and_released_tickets(self) -> None:
        env, _, _, _ = self._env()
        erp = AsyncMock()
        env.erp = erp
        env.tasks.tasks["held"] = dict(
            task_id="held", status="waiting",
            waiting_reason=engine_mod.BRIEF_ATTACH_WAITING_REASON,
            params={"_mirrors": {"erpnext": {"name": "TASK-HELD"}}})
        env.tasks.tasks["terminal"] = dict(
            task_id="terminal", status="completed",
            waiting_reason=engine_mod.BRIEF_ATTACH_WAITING_REASON, params={})
        env.tasks.tasks["released"] = dict(
            task_id="released", status="queued", waiting_reason=None, params={})
        pending = [
            {"task_id": "held", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
            {"task_id": "missing", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
            {"task_id": "terminal", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
            {"task_id": "released", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
        ]
        briefs = {"brief-754": {"id": "brief-754", "content_text": "Verified."}}
        remaining = await engine_mod._heal_pending_brief_attaches(
            env, pending, briefs)
        self.assertEqual(remaining, [])
        erp.attach_research_briefs.assert_awaited_once_with(
            "TASK-HELD", [briefs["brief-754"]])
        healed = env.tasks.tasks["held"]
        self.assertEqual(healed["status"], "queued")
        self.assertIsNone(healed["waiting_reason"])
        self.assertTrue(healed["autorun"])
        env.dispatcher_wake.assert_called_once_with()

    async def test_heal_keeps_unhealable_entries_without_blocking(self) -> None:
        env, _, _, _ = self._env()
        erp = AsyncMock()
        erp.attach_research_briefs.side_effect = RuntimeError("ERP down")
        env.erp = erp
        env.tasks.tasks["held"] = dict(
            task_id="held", status="waiting",
            waiting_reason=engine_mod.BRIEF_ATTACH_WAITING_REASON,
            params={"_mirrors": {"erpnext": {"name": "TASK-HELD"}}})
        env.tasks.tasks["no-mirror"] = dict(
            task_id="no-mirror", status="waiting",
            waiting_reason=engine_mod.BRIEF_ATTACH_WAITING_REASON, params={})
        pending = [
            {"task_id": "held", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
            {"task_id": "no-mirror", "key": "dev", "brief_ids": ["brief-754"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
            {"task_id": "held", "key": "dev", "brief_ids": ["ghost-brief"],
             "run_tag": "abc123", "held_at": "2026-11-11T00:00:00+00:00"},
        ]
        briefs = {"brief-754": {"id": "brief-754", "content_text": "Verified."}}
        remaining = await engine_mod._heal_pending_brief_attaches(
            env, pending, briefs)
        self.assertEqual(len(remaining), 3)
        self.assertEqual(env.tasks.tasks["held"]["status"], "waiting")
        env.dispatcher_wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
