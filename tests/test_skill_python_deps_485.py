"""Python dependencies of agent skills (TASK-2026-00485).

Fenced agents cannot pip-install onto the host (PEP 668, read-only
system), so a skill script declares its packages in a PEP 723 header and
runs with ``uv run``; the save step tests it with those packages in the
same fence. No models, no network: the fence and subprocess are patched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from catalog import load_role_profile
from orchestration import crystallization as crystal
from orchestration.crystallization import (
    CRYSTALLIZATION_PROMPT,
    fenced_skill_tests,
    script_dependencies,
)
from orchestration.generic_agent import PYTHON_PACKAGES_PROMPT, build_agent_prompt

HEADER = ('# /// script\n# requires-python = ">=3.11"\n'
          '# dependencies = [\n#   "openpyxl>=3.1",\n#   "tinydb",\n# ]\n'
          '# ///\n')
SKILL_REL = "skills/finance/excel"


def _skill(root: Path, script: str = HEADER + "import openpyxl\n") -> Path:
    skill = root / SKILL_REL
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "build.py").write_text(script, encoding="utf-8")
    (skill / "scripts" / "test_build.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    return skill


class TestScriptDependencies:
    def test_reads_the_header_and_skips_test_files(self, tmp_path: Path) -> None:
        skill = _skill(tmp_path)
        (skill / "scripts" / "test_extra.py").write_text(
            HEADER.replace("tinydb", "ignored-in-tests"), encoding="utf-8")
        assert script_dependencies(skill) == ["openpyxl>=3.1", "tinydb"]

    def test_no_header_means_no_dependencies(self, tmp_path: Path) -> None:
        assert script_dependencies(_skill(tmp_path, "import json\n")) == []
        assert script_dependencies(tmp_path / "missing") == []

    @pytest.mark.parametrize("script,reason", [
        (HEADER + "import json\n" + HEADER, "more than one PEP 723 block"),
        (HEADER + HEADER, "invalid PEP 723"),
        ("# /// script\n# dependencies = [\n# ///\n", "invalid PEP 723"),
        ('# /// script\n# dependencies = "openpyxl"\n# ///\n',
         "must be a list"),
        ('# /// script\n# dependencies = ["--index-url=http://x"]\n# ///\n',
         "invalid dependency"),
    ])
    def test_malformed_metadata_is_refused(
            self, tmp_path: Path, script: str, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            script_dependencies(_skill(tmp_path, script))


def _fenced(tmp_path: Path, script: str, uv: str | None = "/usr/bin/uv"):
    _skill(tmp_path, script)
    calls: list[dict] = []

    def plan(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(argv=["/bin/true"], cwd=str(tmp_path), env={})

    done = subprocess.CompletedProcess(["/bin/true"], 0, "", "")
    with patch.object(crystal, "plan_agent", side_effect=plan), \
         patch("orchestration.crystallization.shutil.which",
               return_value=uv), \
         patch("orchestration.crystallization.subprocess.run",
               return_value=done):
        ok, detail = fenced_skill_tests(skill_rel=SKILL_REL,
                                        worktree=tmp_path)
    return ok, detail, calls


class TestFencedSkillTests:
    def test_declared_packages_reach_the_fenced_run_through_uv(
            self, tmp_path: Path) -> None:
        ok, _, calls = _fenced(tmp_path, HEADER + "import openpyxl\n")
        assert ok is True
        argv = calls[0]["argv"]
        assert argv[:4] == ["/usr/bin/uv", "run", "--no-project", "--quiet"]
        assert argv[argv.index("pytest") - 1] == "--with"
        assert ["--with", "openpyxl>=3.1", "--with", "tinydb"] == \
            argv[argv.index("openpyxl>=3.1") - 1:argv.index("tinydb") + 1]
        assert argv[argv.index("python"):argv.index("python") + 4] == [
            "python", "-B", "-m", "pytest"]

    def test_scripts_without_header_keep_the_service_interpreter(
            self, tmp_path: Path) -> None:
        ok, _, calls = _fenced(tmp_path, "import json\n")
        assert ok is True
        assert calls[0]["argv"][:4] == [sys.executable, "-B", "-m", "pytest"]

    def test_missing_uv_refuses_instead_of_failing_on_imports(
            self, tmp_path: Path) -> None:
        ok, detail, calls = _fenced(tmp_path, HEADER, uv=None)
        assert ok is False and "uv is missing" in detail
        assert calls == []

    def test_malformed_metadata_discards_with_its_reason(
            self, tmp_path: Path) -> None:
        ok, detail, calls = _fenced(
            tmp_path, '# /// script\n# dependencies = "x"\n# ///\n')
        assert ok is False
        assert detail.startswith("invalid script dependencies:")
        assert calls == []


def test_agents_are_told_how_to_get_packages() -> None:
    prompt = build_agent_prompt(
        load_role_profile("research"),
        {"task_id": "t-1", "ticket_number": 1, "title": "x",
         "description": "d"})
    assert PYTHON_PACKAGES_PROMPT in prompt
    assert "uv run --with" in prompt and "not a blocker" in prompt
    assert "PEP 723" in CRYSTALLIZATION_PROMPT
    assert "uv run" in CRYSTALLIZATION_PROMPT
