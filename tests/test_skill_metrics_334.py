"""Skill version metrics, revert proposals and retirement (TASK-2026-00334).

D9: every version records uses, verified successes, blocked /
not_applicable returns and rework; a regression opens a revert
proposal (never reverts alone); unused or low-success skills retire;
full rewrites are rejected; metrics are visible per version.

No network, no LLM. Git gate tests reuse the fixture bare origin from
test_crystallization_210 (temporary dirs only).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import skills_index_for_planner  # noqa: E402
from orchestration import crystallization as crys  # noqa: E402
from orchestration import skill_metrics as metrics  # noqa: E402
from orchestration.skill_metrics import VersionMetrics  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from test_crystallization_210 import (  # noqa: E402
    _git_ok,
    make_origin,
    ok_tests,
    ok_validate,
    run_config,
    write_draft,
)

TICKET = "TASK-2026-00999"
OTHER = "TASK-2026-00998"


def record(store: dict, role: str, name: str, version: str,
           outcome: str, ticket: str = TICKET,
           count: int = 1):
    for _ in range(count):
        metrics.record_use(store, role=role, name=name, version=version,
                           outcome=outcome, ticket=ticket)
    entry = store["skills"][f"{role}/{name}"]
    return VersionMetrics(**entry["versions"][version])


class RecordTests(unittest.TestCase):
    def test_outcomes_counted_per_version(self) -> None:
        store = metrics.empty_store()
        record(store, "dev", "recipe", "v1", "verified_success", count=3)
        record(store, "dev", "recipe", "v1", "blocked")
        record(store, "dev", "recipe", "v1", "not_applicable")
        record(store, "dev", "recipe", "v1", "rework")
        record(store, "dev", "recipe", "v2", "verified_success", count=2)
        v1 = metrics.latest_version(store, "dev", "other")
        self.assertIsNone(v1)
        lines = metrics.format_metrics_lines(store, "dev", "recipe")
        self.assertEqual(2, len(lines))
        self.assertIn("recipe@v1", lines[0])
        self.assertIn("6 uses", lines[0])
        self.assertIn("recipe@v2", lines[1])
        self.assertIn("2 uses", lines[1])
        summary = metrics.metrics_summary(store)
        self.assertEqual(2, len(summary["dev/recipe"]["versions"]))
        json.dumps(summary)

    def test_unknown_outcome_is_rejected(self) -> None:
        store = metrics.empty_store()
        with self.assertRaises(ValueError):
            metrics.record_use(store, role="dev", name="recipe",
                               version="v1", outcome="hecho",
                               ticket=TICKET)
        self.assertEqual({}, store["skills"])

    def test_store_roundtrip_and_corrupt_reads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill_metrics.json"
            store = metrics.empty_store()
            record(store, "dev", "recipe", "v1", "verified_success")
            metrics.mark_close(store)
            metrics.save_store(store, path)
            loaded = metrics.load_store(path)
            self.assertEqual(1, loaded["closes"])
            current = metrics.latest_version(loaded, "dev", "recipe")
            assert current is not None
            self.assertEqual(1, current.uses)
            path.write_text("{nope", encoding="utf-8")
            self.assertEqual(metrics.empty_store(),
                             metrics.load_store(path))

    def test_version_digest_is_stable_and_content_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "---\nname: s\ndescription: d\n---\n\n# Body\n",
                encoding="utf-8")
            first = metrics.version_of_skill(root)
            self.assertEqual(first, metrics.version_of_skill(root))
            (root / "SKILL.md").write_text(
                "---\nname: s\ndescription: d\n---\n\n# Other\n",
                encoding="utf-8")
            self.assertNotEqual(first, metrics.version_of_skill(root))


class RegressionTests(unittest.TestCase):
    def _versions(self):
        previous = VersionMetrics(version="v1", uses=8,
                                  verified_successes=7)
        current = VersionMetrics(version="v2", uses=6,
                                 verified_successes=2)
        return previous, current

    def test_regression_opens_revert_proposal(self) -> None:
        previous, current = self._versions()
        reason = metrics.regression_decision(previous, current)
        self.assertIsNotNone(reason)
        assert reason is not None
        proposal = metrics.RevertProposal(
            role="dev", name="recipe", from_version="v1",
            to_version="v2", reason=reason, evidence_ticket=TICKET)
        question = metrics.format_revert_question(proposal)
        self.assertIn("dev/recipe", question)
        self.assertIn("approve", question)

    def test_regression_never_reverts_alone(self) -> None:
        store = metrics.empty_store()
        record(store, "dev", "recipe", "v1", "verified_success",
               count=8)
        record(store, "dev", "recipe", "v2", "rework", count=6)
        before = json.dumps(store, sort_keys=True)
        previous = VersionMetrics(
            **store["skills"]["dev/recipe"]["versions"]["v1"])
        current = VersionMetrics(
            **store["skills"]["dev/recipe"]["versions"]["v2"])
        self.assertIsNotNone(
            metrics.regression_decision(previous, current))
        self.assertEqual(before, json.dumps(store, sort_keys=True))
        self.assertEqual(["v1", "v2"],
                         store["skills"]["dev/recipe"]["order"])

    def test_small_drop_or_few_uses_hold(self) -> None:
        previous = VersionMetrics(version="v1", uses=8,
                                  verified_successes=7)
        close = VersionMetrics(version="v2", uses=6,
                               verified_successes=5)
        self.assertIsNone(metrics.regression_decision(previous, close))
        few = VersionMetrics(version="v2", uses=2,
                             verified_successes=0)
        self.assertIsNone(metrics.regression_decision(previous, few))


class RetireTests(unittest.TestCase):
    def test_unused_past_grace_retires(self) -> None:
        store = metrics.empty_store()
        store["skills"]["dev/stale"] = {
            "versions": {"v1": {"version": "v1"}},
            "order": ["v1"], "last_use_close": None}
        store["closes"] = 12
        idle = VersionMetrics(version="v1")
        reason = metrics.retire_decision(idle, 12)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("no recorded use", reason)

    def test_new_skill_is_never_retired(self) -> None:
        self.assertIsNone(metrics.retire_decision(None, 99))
        fresh = VersionMetrics(version="v1")
        self.assertIsNone(metrics.retire_decision(fresh, None))
        self.assertIsNone(metrics.retire_decision(fresh, 3))

    def test_low_success_retires_healthy_stays(self) -> None:
        weak = VersionMetrics(version="v1", uses=6,
                              verified_successes=1)
        reason = metrics.retire_decision(weak, 0)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("low success", reason)
        few = VersionMetrics(version="v1", uses=3,
                             verified_successes=0)
        self.assertIsNone(metrics.retire_decision(few, 0))
        healthy = VersionMetrics(version="v1", uses=6,
                                 verified_successes=5)
        self.assertIsNone(metrics.retire_decision(healthy, 0))


class RewriteTests(unittest.TestCase):
    def test_small_edit_is_not_a_rewrite(self) -> None:
        old = "---\nname: s\ndescription: d\n---\n\n# Skill\n"
        new = "---\nname: s\ndescription: d\n---\n\n# Known improved\n"
        self.assertFalse(metrics.is_full_rewrite(old, new))

    def test_disjoint_body_is_a_rewrite(self) -> None:
        old = ("---\nname: s\ndescription: d\n---\n\n# Deploy recipe\n\n"
               "Freeze the plan before every release.\n\n## Steps\n\n"
               "- Diff the plan against main.\n"
               "- Run the release checklist end to end.\n"
               "- Confirm the owner question before sending.\n"
               "- Record the commit hash in the ticket.\n")
        new = ("---\nname: s\ndescription: d\n---\n\n# Quantum baking\n\n"
               "Preheat the void before every batch.\n\n## Method\n\n"
               "- Fold stardust into the batter gently.\n"
               "- Sing to the comets while it rises.\n"
               "- Serve cold with extra glitter on top.\n"
               "- Tip the void before leaving.\n")
        self.assertTrue(metrics.is_full_rewrite(old, new))

    def test_gate_rejects_full_rewrite_of_activa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(
                drafts, "known",
                body=("# Quantum baking\n\nPreheat the void. Fold stardust "
                      "gently. Serve cold to passing comets with extra "
                      "glitter on top.\n"))
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("known", out.discarded)
            self.assertIn("rewrite", out.discarded["known"])
            self.assertEqual([], out.updated)
            self.assertIsNone(out.commit)

    def test_gate_keeps_small_activa_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "known", body="# Known improved\n")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["known"], out.updated)
            self.assertEqual({}, out.discarded)


class VisibilityTests(unittest.TestCase):
    def test_planner_index_renders_as_before_by_default(self) -> None:
        from catalog import RoleSkill
        skill = RoleSkill(role="dev", name="recipe",
                          description="Does work.", status="activa",
                          ticket="TASK-2026-00001",
                          date=date(2026, 10, 1), source="skills/dev/x")
        plain = skills_index_for_planner({"dev": [skill]})
        self.assertNotIn("[", plain)
        annotated = skills_index_for_planner(
            {"dev": [skill]}, {"dev/recipe": "80% verified over 5 uses"})
        self.assertIn("[80% verified over 5 uses]", annotated)


if __name__ == "__main__":
    unittest.main()
