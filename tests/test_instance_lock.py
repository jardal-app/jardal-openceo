"""Single-instance lock: a second holder fails while the first holds it."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from instance_lock import LOCK_FILENAME, acquire, lock_path_for, stamp


class InstanceLockTests(unittest.TestCase):
    def test_second_acquire_fails_until_first_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / LOCK_FILENAME
            first = acquire(path)
            self.assertIsNotNone(first)
            assert first is not None
            self.assertIsNone(acquire(path))
            first.close()
            second = acquire(path)
            self.assertIsNotNone(second)
            assert second is not None
            second.close()

    def test_lock_path_lives_in_runtime_dir(self) -> None:
        self.assertEqual(
            lock_path_for("/repo/openceo-core"),
            Path("/repo/.runtime") / LOCK_FILENAME,
        )

    def test_stamp_records_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / LOCK_FILENAME
            handle = acquire(path)
            self.assertIsNotNone(handle)
            assert handle is not None
            try:
                stamp(handle, 12345)
                self.assertEqual(path.read_text(encoding="utf-8"), "12345")
            finally:
                handle.close()


if __name__ == "__main__":
    unittest.main()
