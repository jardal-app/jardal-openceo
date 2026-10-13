"""Acceptance rehearsal without tokens (TASK-2026-00235).

The TASK-2026-00224 recorrido with the CEO, the planner, the agents
and the Owner simulated and everything else real: mission, convert,
handoff, verification, lock, crystallization, playbook, no_aplica,
OpenCode, cost and spend. Touches tmp only.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration.acceptance_rehearsal import (  # noqa: E402
    AcceptanceConfig,
    run_acceptance_rehearsal,
)


class AcceptanceRehearsalTests(unittest.TestCase):
    def test_full_recorrido(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="acceptance-rehearsal-") as run_dir:
            evidence = asyncio.run(run_acceptance_rehearsal(
                AcceptanceConfig(run_dir=run_dir)))
        with self.subTest(section="mission"):
            self.assertTrue(evidence.mission["mission_id"])
            self.assertEqual(len(evidence.mission["objectives"]), 2)
            self.assertTrue(evidence.mission["closed"])
        with self.subTest(section="convert"):
            self.assertEqual(evidence.convert["action"], "convert")
        with self.subTest(section="handoff"):
            self.assertEqual(evidence.handoff["files"],
                             ["reconciliation.csv"])
            self.assertEqual(evidence.handoff["attachments"], 1)
        with self.subTest(section="verification"):
            self.assertEqual(evidence.verification["verdict"], "hecho")
        with self.subTest(section="lock"):
            self.assertEqual(evidence.lock["decision"], "approved")
            self.assertEqual(evidence.lock["executed_tool"],
                             "openceo_send_email")
        with self.subTest(section="crystallization"):
            self.assertEqual(evidence.crystallization["promoted"],
                             ["acceptance-notes"])
            self.assertEqual(evidence.crystallization["updated"],
                             ["acceptance-notes"])
            self.assertIn("enviar-correo",
                          evidence.crystallization["cross_role_refused"])
            self.assertTrue(evidence.crystallization["pushed"])
        with self.subTest(section="playbook"):
            self.assertEqual(evidence.playbook["decision"], "approved")
            self.assertTrue(evidence.playbook["loaded"])
        with self.subTest(section="no_aplica"):
            self.assertEqual(evidence.no_aplica["verdict"], "no_aplica")
            self.assertEqual(evidence.no_aplica["reassigned_to"], "dev")
        with self.subTest(section="opencode"):
            self.assertEqual(evidence.opencode["argv"],
                             ["--agent", "finance"])
            self.assertIn("registrar-gasto",
                          evidence.opencode["entry_skills"])
            self.assertTrue(evidence.opencode["version"])
        with self.subTest(section="cost"):
            self.assertEqual(evidence.cost["mission_runs"], 3)
            self.assertEqual(evidence.cost["objective_runs"], [2, 1])
        with self.subTest(section="spend"):
            self.assertEqual(evidence.spend["category"], "gasto_nuevo")
            self.assertEqual(evidence.spend["decision"], "approved")


if __name__ == "__main__":
    unittest.main()
