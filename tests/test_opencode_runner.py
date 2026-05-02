from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.opencode_run import (
    build_run_command,
    build_run_shell_command,
    extract_response_sentinel_exit_code,
    parse_run_output,
)


class OpenCodeRunHelperTests(unittest.TestCase):
    def test_build_run_command_uses_run_build_agent_shape(self) -> None:
        command = build_run_command(
            base_command=["/tmp/opencode", "-m", "opencode-go/kimi-k2.6"],
            prompt="Implementa el ticket",
            cwd="/tmp/repo",
            agent_name="build",
        )

        self.assertEqual(
            command,
            [
                "/tmp/opencode",
                "run",
                "--format",
                "json",
                "-m",
                "opencode-go/kimi-k2.6",
                "--dir",
                "/tmp/repo",
                "--agent",
                "build",
                "Implementa el ticket",
            ],
        )

    def test_parse_run_output_extracts_json_error_even_with_zero_exit(self) -> None:
        parsed = parse_run_output(
            '{"type":"error","sessionID":"ses_123","error":{"name":"UnknownError","data":{"message":"Agent not found: \\"lead-m27\\""}}}'
        )

        self.assertEqual(parsed.session_id, "ses_123")
        self.assertEqual(parsed.final_text, "")
        self.assertEqual(parsed.raw_text, "")
        self.assertEqual(parsed.error_text, 'Agent not found: "lead-m27"')

    def test_build_run_shell_command_appends_completion_sentinel(self) -> None:
        shell_command, sentinel = build_run_shell_command(
            base_command=["/tmp/opencode", "-m", "opencode-go/kimi-k2.6"],
            prompt="Implementa el ticket",
            cwd="/tmp/repo",
            title="melanyryr | #30",
            agent_name="build",
        )

        self.assertIn("OPENCEO_OPENCODE_DONE::", sentinel)
        self.assertIn("/tmp/opencode run --format json", shell_command)
        self.assertIn(sentinel, shell_command)
        self.assertIn("printf", shell_command)

    def test_extract_response_sentinel_exit_code_reads_numeric_code(self) -> None:
        sentinel = "OPENCEO_OPENCODE_DONE::abc123"
        content = f'{{"type":"text","part":{{"type":"text","text":"ok"}}}}\\n{sentinel}:7\\n'

        self.assertEqual(extract_response_sentinel_exit_code(content, sentinel), 7)
