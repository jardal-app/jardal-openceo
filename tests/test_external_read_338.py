"""Read-only third-party connectors (TASK-2026-00338, D13). No tokens."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from external_read import (  # noqa: E402
    EXTERNAL_CHANNELS,
    READ_OPERATIONS,
    ExternalReadError,
    check_external_operation,
    external_reference,
    is_read_operation,
    reference_key,
    require_external_read,
    resolve_external_reference,
)

WRITES = (
    "create_issue",
    "update_ticket",
    "add_comment",
    "send_message",
    "delete_message",
    "transition_issue",
    "post",
    "create_task",
)


class ReadAllowlistTests(unittest.TestCase):
    def test_every_listed_read_is_allowed(self) -> None:
        for channel, operations in READ_OPERATIONS.items():
            for operation in operations:
                with self.subTest(channel=channel, operation=operation):
                    self.assertTrue(is_read_operation(channel, operation))
                    decision = check_external_operation(
                        channel=channel, operation=operation)
                    self.assertTrue(decision["allowed"])
                    self.assertEqual(decision["channel"], channel)

    def test_channels_match_4_1_send_tools(self) -> None:
        from external_send import EXTERNAL_TOOLS

        self.assertEqual(tuple(EXTERNAL_CHANNELS), tuple(EXTERNAL_TOOLS))

    def test_operation_names_normalize(self) -> None:
        self.assertTrue(is_read_operation(" Jira ", " Get_Issue "))
        decision = check_external_operation(
            channel="TEAMS", operation=" List_Messages ")
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["operation"], "list_messages")


class WriteGateTests(unittest.TestCase):
    def test_no_write_prospers_outside_4_1_tools(self) -> None:
        for channel in EXTERNAL_CHANNELS:
            for operation in WRITES:
                with self.subTest(channel=channel, operation=operation):
                    self.assertFalse(is_read_operation(channel, operation))
                    decision = check_external_operation(
                        channel=channel, operation=operation)
                    self.assertFalse(decision["allowed"])
                    self.assertIn("tool_external_send", decision["reason"])
                    with self.assertRaises(ExternalReadError):
                        require_external_read(channel, operation)

    def test_unknown_operation_fails_closed(self) -> None:
        decision = check_external_operation(
            channel="jira", operation="frobnicate")
        self.assertFalse(decision["allowed"])
        with self.assertRaises(ExternalReadError):
            require_external_read("jira", "frobnicate")

    def test_unknown_channel_and_blanks_fail_closed(self) -> None:
        with self.assertRaises(ExternalReadError):
            check_external_operation(channel="sms", operation="get_issue")
        with self.assertRaises(ExternalReadError):
            check_external_operation(channel="jira", operation="  ")
        with self.assertRaises(ExternalReadError):
            is_read_operation("", "get_issue")


class ReferenceTests(unittest.TestCase):
    def test_reference_validates_and_keys(self) -> None:
        ref = external_reference("jira", "PROJ-123")
        self.assertEqual(ref, {"source": "jira", "source_id": "PROJ-123"})
        self.assertEqual(reference_key(ref), "jira:PROJ-123")
        self.assertEqual(
            reference_key({"source": " Teams ", "source_id": " msg-9 "}),
            "teams:msg-9",
        )

    def test_reference_rejects_blanks_and_unknowns(self) -> None:
        with self.assertRaises(ExternalReadError):
            external_reference("jira", "   ")
        with self.assertRaises(ExternalReadError):
            external_reference("sms", "x-1")
        with self.assertRaises(ExternalReadError):
            reference_key({"source": "jira"})
        with self.assertRaises(ExternalReadError):
            reference_key("jira:PROJ-123")

    def test_existing_reference_never_duplicates(self) -> None:
        existing = {"task_id": "t-1", "source_id": "PROJ-123"}
        calls: list[dict[str, str]] = []

        def create(ref: dict[str, str]) -> dict[str, str]:
            calls.append(ref)
            raise AssertionError("must not create a duplicate")

        outcome = resolve_external_reference(
            lambda source, sid: existing,
            create,
            source="jira",
            source_id="PROJ-123",
        )
        self.assertFalse(outcome["created"])
        self.assertEqual(outcome["task"], existing)
        self.assertEqual(calls, [])

    def test_missing_reference_creates_exactly_once(self) -> None:
        created: list[dict[str, str]] = []
        outcome = resolve_external_reference(
            lambda source, sid: None,
            lambda ref: created.append(ref) or {"task_id": "t-2", **ref},
            source="autotask",
            source_id="  T-77 ",
        )
        self.assertTrue(outcome["created"])
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0], {"source": "autotask", "source_id": "T-77"})
        self.assertEqual(outcome["task"]["source_id"], "T-77")


if __name__ == "__main__":
    unittest.main()
