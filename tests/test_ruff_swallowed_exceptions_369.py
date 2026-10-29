"""Ruff gate contra excepciones tragadas (TASK-2026-00369, §16.6).

El gate exige exactamente BLE001/S110/S112/E722 sobre openceo-core/ y
automatico.py; la línea base pasa y un `except Exception: pass` nuevo lo
hace fallar. Sin red, sin CLI generativas, sin modelos.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import automatico  # noqa: E402

PYTHON = _REPO / ".venv" / "bin" / "python"
RUFF_TOML = _REPO / "ruff.toml"
SCOPE = ["openceo-core/", "automatico.py"]
RULES = {"BLE001", "S110", "S112", "E722"}


def _ruff_available() -> bool:
    code = subprocess.run(
        [str(PYTHON), "-m", "ruff", "--version"],
        capture_output=True, cwd=_REPO).returncode
    return code == 0


def _ruff_check(*targets: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), "-m", "ruff", "check", "--no-cache",
         "--config", str(RUFF_TOML), *targets],
        capture_output=True, text=True, cwd=_REPO)


def _needs_ruff(test: object) -> object:
    return unittest.skipUnless(_ruff_available(), "ruff no instalado")(test)


@_needs_ruff
class RuffConfigTests(unittest.TestCase):
    def test_gate_selects_exactly_the_ticket_rules(self) -> None:
        with RUFF_TOML.open("rb") as handle:
            lint = tomllib.load(handle)["lint"]
        self.assertEqual(set(lint.get("select", [])), RULES)
        self.assertNotIn("extend-select", lint)

    def test_baseline_passes(self) -> None:
        proc = _ruff_check(*SCOPE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_synthetic_swallow_fails_gate(self) -> None:
        # Prueba la selección de reglas con un fichero fuera del ámbito;
        # que el ámbito cubre openceo-core/ y automatico.py lo prueban
        # test_baseline_passes (ámbito real en verde) y los tests de cableado.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "synthetic.py"
            target.write_text("try:\n    pass\nexcept Exception:\n    pass\n",
                              encoding="utf-8")
            proc = _ruff_check(str(target))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BLE001", proc.stdout)

    def test_synthetic_bare_except_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bare.py"
            target.write_text("try:\n    pass\nexcept:\n    pass\n",
                              encoding="utf-8")
            proc = _ruff_check(str(target))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("E722", proc.stdout)

    def test_logged_swallow_passes_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "logged.py"
            target.write_text(
                "import logging\nlogger = logging.getLogger(__name__)\n"
                "try:\n    pass\nexcept Exception:\n"
                '    logger.exception("boom")\n',
                encoding="utf-8")
            proc = _ruff_check(str(target))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class RuffWiringTests(unittest.TestCase):
    def test_dev_dependency_pins_ruff(self) -> None:
        pinned = (_REPO / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertRegex(pinned, r"(?m)^ruff==\d+\.\d+")

    def test_project_yaml_has_required_ruff_gate(self) -> None:
        project = yaml.safe_load(
            (_REPO / "projects" / "jardal-openceo" / "project.yaml")
            .read_text(encoding="utf-8"))
        gates = {gate["name"]: gate for gate in project["dev"]["gates"]}
        gate = gates["ruff_swallowed_exceptions"]
        self.assertTrue(gate["required"])
        self.assertIn("ruff", gate["cmd"])
        for scope in SCOPE:
            self.assertIn(scope, gate["cmd"])

    def test_automatico_verify_ruff_passes_on_clean_tree(self) -> None:
        if not _ruff_available():
            self.skipTest("ruff no instalado")
        ok, _why = automatico.verify_ruff()
        self.assertTrue(ok)

    def test_automatico_verify_ruff_fails_on_new_violation(self) -> None:
        with patch.object(automatico, "sh", return_value=(1, "BLE001 boom")):
            ok, why = automatico.verify_ruff()
        self.assertFalse(ok)
        self.assertIn("ruff", why)

    def test_automatico_verify_ruff_reports_missing_ruff(self) -> None:
        with patch.object(automatico, "sh",
                          return_value=(1, "No module named ruff")):
            ok, why = automatico.verify_ruff()
        self.assertFalse(ok)
        self.assertIn("requirements-dev.txt", why)

    def test_scope_is_single_sourced(self) -> None:
        self.assertEqual(tuple(SCOPE), tuple(automatico.RUFF_SCOPE))


if __name__ == "__main__":
    unittest.main()
