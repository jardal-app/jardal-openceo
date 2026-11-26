"""Verdict-wedge parks route to the visual prefix + probe auto-resume (00951).

Root cause of MIS-F57E67A6 stalling ~36h on OBJ-B397EB1A: the visual
verdict for Approvals failed on agent-browser connectivity, the planner
worded the park as free-text ``blocked`` ("depends on Owner restoring
agent-browser connectivity"), and an exhausted blocked park waits for
the Owner signal by design (ticket #654). The wedge cleared on its own
but no trigger re-drove the objective.

Contract after this ticket:
- piece 1: a planner ``blocked`` whose text names judge/verdict infra is
  parked ``visual_judge``/``visual_baseline`` (retries + recovery ticket
  instead of a silent Owner wait); a previous visual prefix sticks
  through vague planner text; only a concrete human-channel action
  (ERP, paste, prod flag, spend, playbook, approval) keeps ``blocked``;
- piece 2: an exhausted blocked park classified as a verdict wedge gets
  a deterministic agent-browser smoke probe (open/snapshot/close
  about:blank: no network, no tokens); a pass replans once through the
  #654 poke, at most one probe per hour and one resume per 24h per
  objective; a re-park lands exhausted-quiet and re-fires the park
  notify channels; every other park never probes.

Logic/contract only, with doubles; no live model calls (Owner order
2026-10-14).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from objectives import engine as engine_mod  # noqa: E402
from objectives import recovery_policy as policy  # noqa: E402
from objectives import wedge_probes as probes  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    ObjectiveStatus,
    PlannerResult,
)
from objectives.service import ObjectiveService  # noqa: E402

NOW = datetime(2026, 11, 26, 22, 30, tzinfo=timezone.utc)

LIVE_SUMMARY = (
    "Both build children are merged on head: gate verdict pass, suite "
    "green per child evidence, DESIGN.md judge summary updated. Park NOT "
    "resolved: no verdict approval")
LIVE_ROADMAP = [
    "Visual verdict for recomposed Approvals re-run on head de60af0; "
    "depends on Owner restoring agent-browser connectivity"]


def _waiting_doc(**overrides: Any) -> dict[str, Any]:
    doc = {
        "objective_id": "OBJ-B397EB1A",
        "project": "jardal-erp",
        "status": "waiting",
        "waiting_reason": f"blocked: {LIVE_SUMMARY}",
        "roadmap_remaining": list(LIVE_ROADMAP),
        "recovery": {"attempts": 2, "due_at": None, "exhausted": True,
                     "ticket_round": 0},
    }
    doc.update(overrides)
    return doc


class VerdictParkClassifierTests(unittest.TestCase):
    def test_live_case_routes_to_visual_judge(self) -> None:
        self.assertEqual(
            policy.classify_verdict_park(
                _waiting_doc(), LIVE_SUMMARY, LIVE_ROADMAP),
            "visual_judge")

    def test_judge_infra_variants_route_to_visual_judge(self) -> None:
        texts = [
            "judge failed for approvals: boom",
            "judge captures missing for overview",
            "no judge available: all gemini accounts busy",
            "visual verdict missing for approvals, gate timed out",
            "sin veredicto aprobado, agent-browser sin conexión",
            "falta aprobación del veredicto; judge quota exhausted",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(
                    policy.classify_verdict_park({}, text, []),
                    "visual_judge", text)

    def test_baseline_only_routes_to_visual_baseline(self) -> None:
        self.assertEqual(
            policy.classify_verdict_park(
                {}, "baseline gate failed on base: no captures", []),
            "visual_baseline")

    def test_baseline_plus_verdict_routes_to_visual_judge(self) -> None:
        self.assertEqual(
            policy.classify_verdict_park(
                {}, "baseline ok, verdict missing, agent-browser down", []),
            "visual_judge")

    def test_human_waits_stay_blocked(self) -> None:
        texts = [
            "Next is Owner-only: prod .env flip RESEARCH_ENABLED=true",
            "waiting on ERP paste of the fiscal credentials",
            "DESIGN.md judge summary needs Owner wording",
            "needs Resumen para el juez from the Owner",
            "waiting Owner verdict on the logo",
            "playbook approval pending from the Owner",
            "verdict missing; paste ERP token to proceed",
            "judge failed for approvals; owner decision needed on copy",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(
                    policy.classify_verdict_park({}, text, []), text)

    def test_previous_visual_prefix_sticks_through_vague_text(self) -> None:
        objective = {"waiting_reason": "visual_judge: ui gate failed"}
        self.assertEqual(
            policy.classify_verdict_park(
                objective, "still failing, needs Owner help", []),
            "visual_judge")

    def test_concrete_human_action_overrides_previous_visual(self) -> None:
        objective = {"waiting_reason": "visual_judge: ui gate failed"}
        self.assertIsNone(policy.classify_verdict_park(
            objective, "judge green now; waiting on ERP paste", []))

    def test_empty_text_never_routes(self) -> None:
        self.assertIsNone(policy.classify_verdict_park({}, "", []))
        self.assertIsNone(policy.classify_verdict_park({}, "", None))


class PlannerBlockedRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _env(self, doc: dict[str, Any]) -> tuple[Any, list]:
        patches: list = []

        async def _patch_objective(objective_id: str, updates: dict):
            patches.append((objective_id, dict(updates)))
            doc.update(updates)
            return dict(doc)

        repo = SimpleNamespace(patch_objective=_patch_objective)
        env = SimpleNamespace(objectives=repo, notify=None, ceo_wake=None)
        return env, patches

    def _result(self, summary: str,
               roadmap: list[str]) -> PlannerResult:
        return PlannerResult(objective_status=ObjectiveStatus.BLOCKED,
                             state_summary=summary,
                             roadmap_remaining=list(roadmap))

    async def test_verdict_blocked_parks_visual_judge(self) -> None:
        doc = {"objective_id": "OBJ-X", "project": "jardal-erp",
               "status": "active"}
        env, _ = await self._env(doc)
        with patch.object(engine_mod, "create_tool_proposal_questions",
                          AsyncMock(return_value=None)), \
                patch.object(engine_mod, "create_playbook_update_questions",
                             AsyncMock(return_value=None)):
            result = await engine_mod._record_and_apply_plan(
                env, doc, "OBJ-X",
                self._result(LIVE_SUMMARY, LIVE_ROADMAP),
                closed_before=2, policy_sha="test")
        self.assertEqual(result["reason"], "visual_judge")
        self.assertTrue(str(doc["waiting_reason"]).startswith(
            "visual_judge:"))
        self.assertEqual(doc["status"], "waiting")

    async def test_human_blocked_parks_blocked(self) -> None:
        doc = {"objective_id": "OBJ-X", "project": "jardal-erp",
               "status": "active"}
        env, _ = await self._env(doc)
        summary = "Next is Owner-only: prod .env flip plus ERP paste"
        with patch.object(engine_mod, "create_tool_proposal_questions",
                          AsyncMock(return_value=None)), \
                patch.object(engine_mod, "create_playbook_update_questions",
                             AsyncMock(return_value=None)):
            result = await engine_mod._record_and_apply_plan(
                env, doc, "OBJ-X", self._result(summary, []),
                closed_before=2, policy_sha="test")
        self.assertEqual(result["reason"], "blocked")
        self.assertTrue(str(doc["waiting_reason"]).startswith("blocked:"))


class ProbeTests(unittest.TestCase):
    def _runner(self, codes: list[int], calls: list) -> Mock:
        def fake(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append((list(argv), dict(kwargs)))
            code = codes[min(len(calls) - 1, len(codes) - 1)]
            return SimpleNamespace(returncode=code, stdout="ok",
                                   stderr="" if code == 0 else "boom")
        return Mock(side_effect=fake)

    def test_healthy_browser_probes_ok(self) -> None:
        calls: list = []
        result = probes.probe_agent_browser(
            runner=self._runner([0, 0, 0], calls))
        self.assertTrue(result["ok"])
        self.assertEqual([argv[1] for argv, _ in calls],
                         ["open", "snapshot", "close"])
        self.assertIn("about:blank", calls[0][0])
        self.assertNotIn("http", " ".join(calls[0][0]))
        session = calls[0][1]["env"]["AGENT_BROWSER_SESSION"]
        self.assertTrue(session.startswith("openceo-probe-"))
        self.assertTrue(all(item[1]["env"]["AGENT_BROWSER_SESSION"]
                            == session for item in calls))

    def test_open_failure_probes_not_ok_and_still_closes(self) -> None:
        calls: list = []
        result = probes.probe_agent_browser(
            runner=self._runner([3, 0, 0], calls))
        self.assertFalse(result["ok"])
        self.assertIn("open exit 3", str(result["reason"]))
        self.assertEqual(calls[-1][0][1], "close")

    def test_snapshot_failure_probes_not_ok(self) -> None:
        calls: list = []
        result = probes.probe_agent_browser(
            runner=self._runner([0, 1, 0], calls))
        self.assertFalse(result["ok"])
        self.assertIn("snapshot", str(result["reason"]))

    def test_timeout_and_missing_cli_probe_not_ok(self) -> None:
        def _timeout(argv: list[str], **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

        result = probes.probe_agent_browser(runner=Mock(
            side_effect=_timeout))
        self.assertFalse(result["ok"])
        self.assertIn("timeout", str(result["reason"]))

        def _missing(argv: list[str], **kwargs: Any) -> Any:
            raise FileNotFoundError("agent-browser")

        result = probes.probe_agent_browser(runner=Mock(side_effect=_missing))
        self.assertFalse(result["ok"])
        self.assertIn("agent-browser", str(result["reason"]))


class BudgetTests(unittest.TestCase):
    def test_fresh_objective_allows_probe_and_resume(self) -> None:
        self.assertTrue(probes.probe_allowed({}, now=NOW))
        self.assertTrue(probes.resume_allowed({}, now=NOW))

    def test_probe_cooldown_and_resume_interval(self) -> None:
        recent = {"auto_resume": {
            "at": (NOW - timedelta(hours=1)).isoformat(),
            "last_probe_at": (NOW - timedelta(minutes=10)).isoformat()}}
        self.assertFalse(probes.probe_allowed(recent, now=NOW))
        self.assertFalse(probes.resume_allowed(recent, now=NOW))
        old = {"auto_resume": {
            "at": (NOW - timedelta(hours=25)).isoformat(),
            "last_probe_at": (NOW - timedelta(hours=2)).isoformat()}}
        self.assertTrue(probes.probe_allowed(old, now=NOW))
        self.assertTrue(probes.resume_allowed(old, now=NOW))

    def test_malformed_record_fails_open(self) -> None:
        broken = {"auto_resume": {"at": "not-a-date", "count": "x",
                                  "last_probe_at": None}}
        self.assertTrue(probes.probe_allowed(broken, now=NOW))
        self.assertTrue(probes.resume_allowed(broken, now=NOW))
        record = probes.record_probe(broken, ok=True, resumed=True, now=NOW)
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["at"], NOW.isoformat())
        self.assertTrue(record["last_probe_ok"])

    def test_record_probe_counts_only_resumes(self) -> None:
        record = probes.record_probe({}, ok=False, resumed=False, now=NOW)
        self.assertFalse(record["last_probe_ok"])
        self.assertNotIn("at", record)
        record = probes.record_probe(
            {"auto_resume": record}, ok=True, resumed=True, now=NOW)
        self.assertEqual(
            (record["count"], record["at"], record["last_probe_ok"]),
            (1, NOW.isoformat(), True))


class ServiceAutoResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self, doc: dict[str, Any]) -> tuple[Any, list]:
        patches: list = []

        async def _get_objective(objective_id: str):
            return dict(doc) if objective_id == doc["objective_id"] else None

        async def _patch_objective(objective_id: str, updates: dict):
            patches.append((objective_id, dict(updates)))
            doc.update(updates)
            return dict(doc)

        async def _find_one(query: dict):
            return None

        async def _list_tasks(objective_id: str):
            return []

        repo = SimpleNamespace(
            get_objective=_get_objective, patch_objective=_patch_objective,
            tasks=SimpleNamespace(find_one=_find_one),
            list_tasks_by_objective=_list_tasks)
        config = SimpleNamespace(has_project=lambda project: True,
                                 load_project=lambda project: {})
        service = ObjectiveService(
            repo, SimpleNamespace(), config, SimpleNamespace())
        return service, patches

    async def test_clear_wedge_replans_through_poke(self) -> None:
        doc = _waiting_doc()
        service, patches = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})), \
                patch.object(service, "plan_objective", AsyncMock(
                    return_value={"ok": True})) as plan:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", doc,
                "blocked parks wait for the Owner signal", now=NOW)
        self.assertTrue(result["resumed"])
        plan.assert_awaited_once_with(
            "OBJ-B397EB1A", source="automatic wedge re-check")
        record = doc["auto_resume"]
        self.assertEqual((record["count"], record["last_probe_ok"]),
                         (1, True))

    async def test_failed_probe_stays_quiet_without_planning(self) -> None:
        doc = _waiting_doc()
        service, _ = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": False,
                                             "reason": "open exit 3"})), \
                patch.object(service, "plan_objective", AsyncMock(
                    return_value={"ok": True})) as plan:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", doc,
                "blocked parks wait for the Owner signal", now=NOW)
        self.assertFalse(result["resumed"])
        plan.assert_not_awaited()
        self.assertFalse(doc["auto_resume"]["last_probe_ok"])

    async def test_human_park_never_probes(self) -> None:
        doc = _waiting_doc(waiting_reason=(
            "blocked: Next is Owner-only: prod .env flip plus ERP paste"),
            roadmap_remaining=[])
        service, patches = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})) as probe, \
                patch.object(service, "plan_objective", AsyncMock(
                    return_value={"ok": True})) as plan:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", doc,
                "blocked parks wait for the Owner signal", now=NOW)
        self.assertFalse(result["resumed"])
        probe.assert_not_called()
        plan.assert_not_awaited()
        self.assertEqual(patches, [])

    async def test_spent_budgets_skip_probe_or_plan(self) -> None:
        cooling = _waiting_doc(auto_resume={
            "last_probe_at": (NOW - timedelta(minutes=10)).isoformat()})
        service, _ = await self._service(cooling)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})) as probe:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", cooling,
                "blocked parks wait for the Owner signal", now=NOW)
        self.assertFalse(result["resumed"])
        probe.assert_not_called()

        spent = _waiting_doc(auto_resume={
            "at": (NOW - timedelta(hours=1)).isoformat(),
            "last_probe_at": (NOW - timedelta(hours=2)).isoformat()})
        service, _ = await self._service(spent)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})) as probe, \
                patch.object(service, "plan_objective", AsyncMock(
                    return_value={"ok": True})) as plan:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", spent,
                "blocked parks wait for the Owner signal", now=NOW)
        self.assertFalse(result["resumed"])
        self.assertEqual(result["reason"], "auto-resume spent")
        probe.assert_called_once()
        plan.assert_not_awaited()

    async def test_other_reasons_skip_immediately(self) -> None:
        doc = _waiting_doc()
        service, _ = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})) as probe:
            result = await service._maybe_probe_auto_resume(
                "OBJ-B397EB1A", doc, "not due", now=NOW)
        self.assertFalse(result["resumed"])
        probe.assert_not_called()

    async def test_lane_returns_auto_resume_on_clear_wedge(self) -> None:
        doc = _waiting_doc()
        service, _ = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": True})), \
                patch.object(service, "plan_objective", AsyncMock(
                    return_value={"ok": True, "action": "replanned"})):
            result = await service.recover_waiting_objective(
                "OBJ-B397EB1A", now=NOW)
        self.assertEqual(result["action"], "auto_resume")
        self.assertTrue(result["ok"])

    async def test_lane_paces_quietly_on_failed_probe(self) -> None:
        doc = _waiting_doc()
        service, _ = await self._service(doc)
        with patch.object(probes, "probe_agent_browser",
                          Mock(return_value={"ok": False, "reason": "down"})):
            result = await service.recover_waiting_objective(
                "OBJ-B397EB1A", now=NOW)
        self.assertEqual(result["action"], "none")
        self.assertEqual(
            datetime.fromisoformat(doc["recovery"]["due_at"]),
            NOW + timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()
