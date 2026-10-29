"""Capture-time redaction (design v2 section 15.2).

Pure unit tests, no network, no CLI, no LLM (epic TASK-2026-00365).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability.models import SCHEMA_VERSION  # noqa: E402
from reliability.sanitize import (  # noqa: E402
    MAX_MESSAGE_CHARS,
    is_in_app_frame,
    sanitize_frames,
    sanitize_incident,
    sanitize_message,
)


class SanitizeTests(unittest.TestCase):
    def test_truncates_to_500_characters(self) -> None:
        self.assertEqual(len(sanitize_message("x" * 900)), MAX_MESSAGE_CHARS)
        self.assertEqual(MAX_MESSAGE_CHARS, 500)

    def test_redacts_tokens_keys_urls_emails_dsn(self) -> None:
        text = sanitize_message(
            "api_key=sk-secret-value user pass https://u:p@host/x "
            "a@b.com dsn=https://k@sentry/x"
        )
        self.assertNotIn("sk-secret-value", text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("u:p@host", text)
        self.assertIn("https://[CREDS]@host", text)
        self.assertNotIn("a@b.com", text)
        self.assertIn("[EMAIL]", text)

    def test_redacts_absolute_paths_outside_repo(self) -> None:
        text = sanitize_message("failed reading /etc/passwd file")
        self.assertNotIn("/etc/passwd", text)
        self.assertIn("[PATH]", text)

    def test_keeps_repo_paths_when_root_given(self) -> None:
        text = sanitize_message(
            "see /repo/openceo-core/main.py and /etc/passwd", repo_root="/repo")
        self.assertIn("/repo/openceo-core/main.py", text)
        self.assertNotIn("/etc/passwd", text)

    def test_redacts_all_paths_without_root(self) -> None:
        text = sanitize_message("see /repo/openceo-core/main.py")
        self.assertNotIn("/repo/openceo-core/main.py", text)

    def test_in_app_frames_keep_module_function_line(self) -> None:
        frames = [
            {"module": "m", "function": "f", "line": "1",
             "file": "openceo-core/m.py"},
            {"module": "auto", "function": "g", "line": "2",
             "file": "automatico.py"},
        ]
        self.assertTrue(all(is_in_app_frame(frame["file"]) for frame in frames))
        self.assertEqual(
            sanitize_frames(frames), ["m:f:1", "auto:g:2"])

    def test_absolute_motor_paths_are_in_app(self) -> None:
        frames = [{"module": "openceo-core.session", "function": "step",
                   "line": "7",
                   "file": "/home/u/jardal-openceo/openceo-core/session.py"}]
        self.assertTrue(is_in_app_frame(frames[0]["file"]))
        self.assertEqual(sanitize_frames(frames), ["openceo-core.session:step:7"])

    def test_external_frames_collapse_to_package(self) -> None:
        frames = [{"module": "httpx._client", "function": "send",
                   "line": "99", "file": "site-packages/httpx/_client.py"}]
        self.assertFalse(is_in_app_frame(frames[0]["file"]))
        self.assertEqual(sanitize_frames(frames), ["httpx"])

    def test_allowlist_drops_unknown_fields(self) -> None:
        out = sanitize_incident({
            "engine": {"exc_type": "ValueError", "local_vars": {"x": 1},
                       "env": {"HOME": "/root"}, "reason_code": "r"},
            "context": {"message": "boom", "headers": {"a": "b"},
                        "evidence_refs": [f"e{i}" for i in range(30)]},
            "extra": "dropped",
        })
        self.assertNotIn("local_vars", out["engine"])
        self.assertNotIn("env", out["engine"])
        self.assertNotIn("headers", out["context"])
        self.assertNotIn("extra", out)
        self.assertEqual(out["engine"]["exc_type"], "ValueError")
        self.assertEqual(len(out["context"]["evidence_refs"]), 10)

    def test_sanitized_incident_carries_schema_version(self) -> None:
        out = sanitize_incident({"engine": {}, "context": {}})
        self.assertEqual(out["schema_version"], SCHEMA_VERSION)

    def test_redacts_file_urls_and_traversal(self) -> None:
        text = sanitize_message("open file:///home/u/x")
        self.assertNotIn("/home/u/x", text)
        self.assertIn("[PATH]", text)
        text = sanitize_message("x /repo/../etc/passwd", repo_root="/repo")
        self.assertNotIn("etc/passwd", text)
        self.assertIn("[PATH]", text)

    def test_evidence_refs_are_redacted_but_business_ref_is_verbatim(self) -> None:
        out = sanitize_incident({
            "engine": {},
            "context": {"message": "",
                        "business_ref": "TASK-2026-00368",
                        "evidence_refs": ["see a@b.com", "log /etc/passwd"]},
        })
        self.assertEqual(out["context"]["business_ref"], "TASK-2026-00368")
        self.assertIn("[EMAIL]", out["context"]["evidence_refs"][0])
        self.assertIn("[PATH]", out["context"]["evidence_refs"][1])


if __name__ == "__main__":
    unittest.main()
