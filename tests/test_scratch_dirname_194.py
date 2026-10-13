"""Contract: the orchestrator scratch is `.openceo/` (TASK-2026-00194).

`.tier-s/` was the scratch of the whole orchestrator (TASK-2026-00136),
not Tier S tickets, so the name confused. Since 00194 the engine,
executors, prompts, tests, .gitignore and docs use `.openceo/`.
STATUS.md keeps its past entries: it is the operational log, history.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives.engine import (  # noqa: E402
    SCRATCH_DIRNAME,
    objective_scratch,
    scratch_root,
)
from orchestration.agent_sandbox import _SCRATCH_DIRNAME  # noqa: E402

OLD = ".tier" + "-s"
NEW = ".openceo"
HISTORY = {"STATUS.md"}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


class ScratchDirnameTests(unittest.TestCase):
    def test_engine_scratch_dirname_is_openceo(self) -> None:
        self.assertEqual(SCRATCH_DIRNAME, NEW)
        repo = Path("/tmp/fake-repo")
        self.assertEqual(scratch_root(repo), repo / NEW)
        self.assertEqual(
            objective_scratch(repo, "OBJ-1"), repo / NEW / "objectives" / "OBJ-1"
        )

    def test_sandbox_scratch_dirname_matches_engine(self) -> None:
        self.assertEqual(_SCRATCH_DIRNAME, SCRATCH_DIRNAME)

    def test_no_old_dirname_outside_history(self) -> None:
        offenders = [
            str(path.relative_to(REPO))
            for path in _tracked_text_files()
            if path.name not in HISTORY
            and path.is_file()
            and OLD in path.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertEqual(offenders, [])

    def test_gitignore_covers_new_dirname(self) -> None:
        text = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(NEW + "/", text)

    def test_docs_name_the_new_scratch(self) -> None:
        for doc in ("AGENTS.md", "docs/ARCHITECTURE_REFERENCE_2026.md"):
            with self.subTest(doc=doc):
                text = (REPO / doc).read_text(encoding="utf-8")
                self.assertIn(NEW, text)


if __name__ == "__main__":
    unittest.main()
