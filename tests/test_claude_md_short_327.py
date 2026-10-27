"""Contract: short CLAUDE.md with practices index (TASK-2026-00327, D4).

CLAUDE.md and AGENTS.md stay under 200 lines; per-zone rules live in
`.claude/rules/` with `paths` frontmatter; work rules are indexed, not copied.
No tokens, no subprocesses, no vault reads: only repo files and a synthetic
temp dir for the frontmatter shape.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PRACTICES = (
    "autonomía y escalado",
    "ciclo automático y revisión",
    "comunicación y atribución",
    "validación",
)

RULES = {
    ".claude/rules/horizon.md": "openceo-core/horizon/",
    ".claude/rules/dev-workflows.md": "workflows/",
    ".claude/rules/tickets.md": "openceo-core/task_service.py",
}


class ShortClaudeContractTest(unittest.TestCase):
    def test_claude_md_under_200_lines(self):
        lines = (REPO / "CLAUDE.md").read_text().splitlines()
        self.assertLess(len(lines), 200, f"CLAUDE.md has {len(lines)} lines")

    def test_agents_md_under_200_lines(self):
        lines = (REPO / "AGENTS.md").read_text().splitlines()
        self.assertLess(len(lines), 200, f"AGENTS.md has {len(lines)} lines")

    def test_practices_index_covers_all_areas(self):
        text = (REPO / "CLAUDE.md").read_text().lower()
        for area in PRACTICES:
            self.assertIn(area, text, f"practices index missing: {area}")

    def test_zone_rules_have_paths_frontmatter(self):
        for rule, zone in RULES.items():
            body = (REPO / rule).read_text()
            self.assertIn("paths:", body, rule)
            self.assertIn(zone, body, f"{rule} does not scope {zone}")
            target = (REPO / zone.rstrip("/")).resolve()
            self.assertTrue(target.exists(), f"rule zone missing: {zone}")

    def test_zone_rules_shape_matches_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "rule.md"
            fixture.write_text("---\npaths:\n  - \"some/zone/**\"\n---\n\n# Title\n")
            fixture_head = fixture.read_text().split("---")[1]
            self.assertIn("paths:", fixture_head)
            for rule in RULES:
                body = (REPO / rule).read_text()
                self.assertEqual(body.count("---"), 2, rule)
                rule_head = body.split("---")[1]
                self.assertIn("paths:", rule_head, rule)
                for key in ("paths:",):
                    self.assertIn(key, rule_head, rule)
                    self.assertIn(key, fixture_head, rule)

    def test_combined_import_stays_under_200_lines(self):
        claude = (REPO / "CLAUDE.md").read_text().splitlines()
        agents = (REPO / "AGENTS.md").read_text().splitlines()
        self.assertIn("@AGENTS.md", "\n".join(claude))
        self.assertLess(
            len(claude) + len(agents), 200,
            f"combined {len(claude)} + {len(agents)} lines",
        )

    def test_no_procedure_dumps_in_claude_md(self):
        text = (REPO / "CLAUDE.md").read_text()
        self.assertNotIn("presets:\n  test:", text)
        self.assertNotIn("HZ-001", text)


if __name__ == "__main__":
    unittest.main()
