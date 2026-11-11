"""Attachment text synthesis in the production ERP view (763).

Final-review finding F1 (CRITICAL, REAL): production
``JardalERPWork._attachment_view`` spread the ERP row with
``content_base64`` and never synthesized ``content_text``, while brief
discovery (engine.py) and copy verification (service.py) require
``content_text`` — so in production no brief was ever discovered and the
DEV ticket was never released. The adapter now decodes
``content_base64`` into ``content_text``, fail-soft (missing/invalid
base64 or non-UTF8 bytes omit ``content_text``, never raise), leaving
every existing key untouched.

Targeted logic/contract tests with mocks; no network/models.
"""

from __future__ import annotations

import base64
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from business.work import JardalERPWork  # noqa: E402


_WORK = JardalERPWork(
    SimpleNamespace(JARDAL_ERP_URL="https://erp.example.test"), client=MagicMock())


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        id="att-1", fileName="brief.md", fileSize=5, isPrivate=True,
        sha256Hash="deadbeef", taskId="TASK-1", version=1)
    base.update(overrides)
    return base


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return _WORK._attachment_view(row)


class AttachmentTextSynthesisTests(unittest.TestCase):
    def test_valid_base64_synthesizes_text_and_leaves_keys_untouched(self) -> None:
        text = "Verified: retries need the same idempotency key."
        row = _row(content_base64=base64.b64encode(text.encode()).decode("ascii"))
        snapshot = copy.deepcopy(row)
        view = _view(row)
        self.assertEqual(view["content_text"], text)
        for key, value in snapshot.items():
            self.assertEqual(view[key], value)
        added = set(view) - set(snapshot)
        self.assertLessEqual(
            added, {"file_name", "size", "private", "content_hash",
                    "attached_to", "download_url", "content_text"})
        self.assertEqual(row, snapshot)

    def test_aliases_match_production_shape(self) -> None:
        view = _view(_row(content_base64=base64.b64encode(b"hello").decode("ascii")))
        self.assertEqual(view["file_name"], "brief.md")
        self.assertEqual(view["size"], 5)
        self.assertTrue(view["private"])
        self.assertEqual(view["content_hash"], "deadbeef")
        self.assertEqual(view["attached_to"], "TASK-1")
        self.assertEqual(view["download_url"],
                         "https://erp.example.test/api/v1/attachments/att-1/content")

    def test_missing_content_base64_omits_text(self) -> None:
        for row in (_row(), _row(content_base64=""), _row(content_base64=None)):
            with self.subTest(row=row):
                view = _view(row)
                self.assertNotIn("content_text", view)
                self.assertEqual(view["file_name"], "brief.md")

    def test_invalid_base64_omits_text_and_never_raises(self) -> None:
        view = _view(_row(content_base64="!!!not-base64!!!"))
        self.assertNotIn("content_text", view)
        self.assertEqual(view["content_base64"], "!!!not-base64!!!")

    def test_non_utf8_bytes_omit_text_and_never_raise(self) -> None:
        payload = base64.b64encode(b"\xff\xfe\x00\x01").decode("ascii")
        view = _view(_row(content_base64=payload))
        self.assertNotIn("content_text", view)
        self.assertEqual(view["content_base64"], payload)

    def test_non_ascii_content_base64_omits_text_and_never_raises(self) -> None:
        # R1 F1: b64decode raises plain ValueError for a non-ASCII str.
        view = _view(_row(content_base64="áéíóú-not-base64-áéíóú"))
        self.assertNotIn("content_text", view)

    def test_preexisting_content_text_is_left_untouched(self) -> None:
        view = _view(_row(content_text="kept", content_base64="!!!not-base64!!!"))
        self.assertEqual(view["content_text"], "kept")
        view = _view(_row(content_text="kept"))
        self.assertEqual(view["content_text"], "kept")


if __name__ == "__main__":
    unittest.main()
