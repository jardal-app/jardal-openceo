from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.kimi_session_manager import KimiSessionManager


class KimiSessionManagerTests(unittest.TestCase):
    def test_respects_kimi_share_dir_env_and_writes_session(self) -> None:
        previous = os.environ.get("KIMI_SHARE_DIR")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["KIMI_SHARE_DIR"] = tmpdir
                manager = KimiSessionManager()
                result = manager.create_qa_session_with_prompt(
                    work_dir="/tmp/example-repo",
                    custom_title="QA #999",
                    context_prompt="Contexto de prueba",
                )

                session_dir = Path(result["session_dir"])
                self.assertTrue(session_dir.exists())
                self.assertTrue((session_dir / "state.json").exists())
                self.assertTrue((session_dir / "context.jsonl").exists())
                self.assertTrue((session_dir / "wire.jsonl").exists())
                self.assertTrue(str(session_dir).startswith(tmpdir))
        finally:
            if previous is None:
                os.environ.pop("KIMI_SHARE_DIR", None)
            else:
                os.environ["KIMI_SHARE_DIR"] = previous
