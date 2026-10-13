"""Pilot 1 rehearsal recorrido (TASK-2026-00234, scope 1).

One shared rehearsal run over tmp scratch: pedido, simulated planner
and agent, real skill script against doubles, verdict, cost fold and
crystallization. No network, no LLM, no ERPNext, no Cloudflare.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestration.pilot_rehearsal import (
    REHEARSAL_MIRROR,
    RehearsalConfig,
    run_pilot_rehearsal,
)

REPO = Path(__file__).resolve().parents[1]


def _git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(REPO), capture_output=True,
        text=True, timeout=60)
    return result.stdout if result.returncode == 0 else "<git failed>"


@unittest.skipUnless(shutil.which("git"), "the rehearsal needs git")
class PilotRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory(prefix="pilot-rehearsal-234-")
        cls._status_before = _git_status()
        cls.validate_calls: list[Path] = []
        cls.skill_test_calls: list[tuple[str, ...]] = []

        def fake_validate(path: Path) -> tuple[bool, str]:
            cls.validate_calls.append(Path(path))
            return True, "rehearsal fake gate"

        def fake_skill_tests(*args: object, **kwargs: object) -> tuple[bool, str]:
            cls.skill_test_calls.append((str(args), str(sorted(kwargs))))
            return True, "rehearsal fake skill tests"

        config = RehearsalConfig(
            run_dir=cls._tmp.name, validate=fake_validate,
            run_skill_tests=fake_skill_tests)
        cls.evidence = asyncio.run(run_pilot_rehearsal(config))
        cls.run_dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()
        super().tearDownClass()

    def test_planner_decides_operations_with_skill(self) -> None:
        decision = self.evidence.decision
        self.assertEqual(decision["action"], "decided")
        self.assertEqual(decision["target_agent"], "operations")
        self.assertEqual(decision["skill"], "crear-proyecto")
        self.assertEqual(decision["params_skill"], "crear-proyecto")
        self.assertEqual(decision["agent_hint"], "operations")

    def test_run_uses_the_operations_profile(self) -> None:
        spec = self.evidence.run_spec
        self.assertEqual((spec["command"] or [])[0], "musej")
        self.assertEqual(spec["role_profile"], "operations")
        self.assertIn("--output-schema", spec["headless_args"] or [])
        self.assertTrue(spec["schema_exists"])
        self.assertIn("crear-proyecto", spec["staged_skills"])
        self.assertTrue(str(spec["workdir"]).startswith(str(self.run_dir)))
        self.assertGreater(spec["prompt_chars"], 500)

    def test_badge_binds_agent_ticket_box_and_revokes(self) -> None:
        badge = self.evidence.badge
        self.assertEqual(badge["agent"], "operations")
        self.assertEqual(badge["ticket"], REHEARSAL_MIRROR)
        self.assertEqual(badge["box"], [
            "manage_dns_record",
            "openceo_add_ticket_comment",
            "openceo_attach_ticket_file",
            "openceo_ensure_erpnext_project",
            "openceo_get_ticket",
            "openceo_search_tickets",
        ])
        self.assertTrue(badge["box_matches_card"])
        self.assertTrue(badge["revoked"])

    def test_only_in_box_tools_run(self) -> None:
        box = set(self.evidence.badge["box"])
        self.assertTrue(self.evidence.boundary_calls)
        for call in self.evidence.boundary_calls:
            self.assertIn(call["tool"], box)
        tools = [call["tool"] for call in self.evidence.boundary_calls]
        self.assertIn("openceo_ensure_erpnext_project", tools)
        self.assertIn("manage_dns_record", tools)
        denied = [call["tool"] for call in self.evidence.denied_calls]
        self.assertIn("openceo_send_email", denied)

    def test_skill_script_runs_for_real(self) -> None:
        outputs = self.evidence.skill_outputs
        for name in ("roots", "templates", "project-config", "vault-entry"):
            self.assertEqual(outputs[name], "ok", name)
        key = "rehearsal-pilot1"
        self.assertTrue((self.run_dir / "projects" / key / "project.yaml"
                         ).is_file())
        self.assertTrue((self.run_dir / "repos" / key / ".git").is_dir())
        self.assertTrue((self.run_dir / "repos" / key / "README.md").is_file())
        git = self.evidence.git
        self.assertTrue(git["has_git"])
        self.assertEqual(git["remote"], str(self.run_dir / "remote.git"))

    def test_verdict_is_hecho_with_measured_cost(self) -> None:
        verdict = self.evidence.verdict
        self.assertEqual(verdict["resultado"], "hecho")
        self.assertIn("operations: hecho", verdict["comment"])
        cost = self.evidence.cost
        self.assertGreaterEqual(cost["runs"], 1)
        self.assertIsNone(cost["tokens_in"])
        self.assertEqual(cost["tokens_unknown_runs"], cost["runs"])
        self.assertIn("rehearsal-simulated", cost["models"])

    def test_crystallization_gate_commits_and_pushes(self) -> None:
        crystal = self.evidence.crystallization
        self.assertEqual(crystal["drafts"], ["rehearsal-notes"])
        self.assertEqual(crystal["created"], ["rehearsal-notes"])
        self.assertTrue(crystal["commit"])
        self.assertTrue(crystal["pushed"])
        self.assertEqual(len(self.validate_calls), 1)
        self.assertIn("rehearsal-notes", str(self.validate_calls[0]))
        self.assertEqual(self.skill_test_calls, [])

    def test_no_service_secrets_leak(self) -> None:
        secrets = self.evidence.secrets
        self.assertEqual(secrets["env_vars_checked"], 4)
        self.assertEqual(secrets["env_leaked"], [])
        self.assertEqual(secrets["files_leaked"], [])

    def test_dns_delete_still_requires_the_owner(self) -> None:
        self.assertTrue(self.evidence.dns_delete_requires_owner)

    def test_live_tree_untouched(self) -> None:
        self.assertEqual(_git_status(), self._status_before)


if __name__ == "__main__":
    unittest.main()
