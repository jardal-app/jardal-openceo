from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.opencode_paths import normalize_opencode_command, resolve_opencode_cli_path


class OpenCodePathResolutionTests(unittest.TestCase):
    def test_resolve_prefers_container_path_when_host_path_missing(self) -> None:
        def fake_exists(path: Path) -> bool:
            return str(path) == "/home/ceouser/.opencode/bin/opencode"

        with patch("agents.dev.opencode_paths.Path.exists", fake_exists), patch(
            "agents.dev.opencode_paths.Path.is_file", fake_exists
        ), patch("agents.dev.opencode_paths.shutil.which", return_value=None):
            resolved = resolve_opencode_cli_path("/home/develop/.opencode/bin/opencode")

        self.assertEqual(resolved, "/home/ceouser/.opencode/bin/opencode")

    def test_normalize_preserves_existing_host_path(self) -> None:
        def fake_exists(path: Path) -> bool:
            return str(path) == "/home/develop/.opencode/bin/opencode"

        with patch("agents.dev.opencode_paths.Path.exists", fake_exists), patch(
            "agents.dev.opencode_paths.Path.is_file", fake_exists
        ), patch("agents.dev.opencode_paths.shutil.which", return_value=None):
            normalized = normalize_opencode_command(
                ["/home/develop/.opencode/bin/opencode", "-m", "opencode-go/kimi-k2.6"]
            )

        self.assertEqual(normalized[0], "/home/develop/.opencode/bin/opencode")
