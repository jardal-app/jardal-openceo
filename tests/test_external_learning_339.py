"""External learning candidates from Jira, Autotask and Teams (TASK-2026-00339, D13). No tokens."""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from external_learning import (  # noqa: E402
    CONCRETE_EVENT_KINDS,
    EXTERNAL_CHANNELS,
    ExternalLearningError,
    authorized_persons,
    build_external_candidate,
    candidate_filename,
    evaluate_external_event,
    is_authorized,
    is_concrete_event,
    queue_external_candidate,
)
from external_send import EXTERNAL_TOOLS  # noqa: E402

PROJECT = "projects/org/active"
TARGET = f"{PROJECT}/practices/review-rules.md"
ORIGIN = "TASK-2026-00939"
AUTHORITIES = ["Ada Owner", "Lead Reviewer"]
RULE = "Toda propuesta normativa cita su evidencia antes de publicarse."


def _event(**overrides: object) -> dict:
    base: dict = {
        "channel": "jira",
        "kind": "ticket_rejected",
        "author": "Ada Owner",
        "source_id": "PROJ-123",
        "body": "Conversacion larga que nunca debe guardarse.",
    }
    base.update(overrides)
    return base


def _candidate(**overrides: object) -> dict:
    base = build_external_candidate(
        channel="jira",
        kind="ticket_rejected",
        author="Ada Owner",
        source_id="PROJ-123",
        rule=RULE,
        project=PROJECT,
        target=TARGET,
        origin_ticket=ORIGIN,
    )
    base.update(overrides)
    return base


class ConcreteEventTests(unittest.TestCase):
    def test_every_concrete_kind_is_learnable_with_authority(self) -> None:
        for kind in CONCRETE_EVENT_KINDS:
            with self.subTest(kind=kind):
                verdict = evaluate_external_event(
                    _event(kind=kind), AUTHORITIES)
                self.assertTrue(verdict["learnable"], verdict)

    def test_generic_events_are_ignored(self) -> None:
        for kind in ("comment", "message", "status_changed",
                     "pr_comment", "reaction", "frobnicate", ""):
            with self.subTest(kind=kind):
                verdict = evaluate_external_event(
                    _event(kind=kind), AUTHORITIES)
                self.assertFalse(verdict["learnable"])
                with self.assertRaises(ExternalLearningError):
                    build_external_candidate(
                        channel="jira", kind=kind or "comment",
                        author="Ada Owner", source_id="PROJ-123",
                        rule=RULE, project=PROJECT, target=TARGET,
                        origin_ticket=ORIGIN)

    def test_channels_match_send_and_read_sets(self) -> None:
        self.assertEqual(tuple(EXTERNAL_CHANNELS), tuple(EXTERNAL_TOOLS))
        from external_read import EXTERNAL_CHANNELS as READ_CHANNELS

        self.assertEqual(tuple(EXTERNAL_CHANNELS), tuple(READ_CHANNELS))


class AuthorityTests(unittest.TestCase):
    def test_unauthorized_authors_are_ignored(self) -> None:
        for author in ("Intruso", "  ", "bot-externo"):
            with self.subTest(author=author):
                verdict = evaluate_external_event(
                    _event(author=author), AUTHORITIES)
                self.assertFalse(verdict["learnable"])
                self.assertIn("authority", verdict["reason"])

    def test_authority_matches_case_insensitive(self) -> None:
        self.assertTrue(is_authorized("ada owner", AUTHORITIES))
        self.assertTrue(is_authorized("  LEAD reviewer ", AUTHORITIES))
        self.assertFalse(is_authorized("", AUTHORITIES))
        self.assertFalse(is_concrete_event("comment"))
        self.assertTrue(is_concrete_event(" Direct_Message "))

    def test_blank_kind_is_not_concrete(self) -> None:
        self.assertFalse(is_concrete_event(""))
        self.assertFalse(is_concrete_event("   "))
        self.assertFalse(is_concrete_event(None))

    def test_authorities_read_from_project_config(self) -> None:
        config = {"dev": {"external_learning": {
            "authorities": ["Ada Owner", "Lead Reviewer", "Ada Owner", "  "]}}}
        self.assertEqual(
            authorized_persons(config), ["Ada Owner", "Lead Reviewer"])
        self.assertEqual(authorized_persons({}), [])
        self.assertEqual(authorized_persons(None), [])
        self.assertEqual(authorized_persons({"dev": None}), [])

    def test_unknown_channel_and_missing_link_fail_closed(self) -> None:
        verdict = evaluate_external_event(_event(channel="sms"), AUTHORITIES)
        self.assertFalse(verdict["learnable"])
        verdict = evaluate_external_event(
            _event(source_id="  "), AUTHORITIES)
        self.assertFalse(verdict["learnable"])
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="sms", kind="ticket_rejected", author="Ada Owner",
                source_id="x", rule=RULE, project=PROJECT,
                target=TARGET, origin_ticket=ORIGIN)


class CandidateContentTests(unittest.TestCase):
    def test_candidate_is_normative_with_link_and_no_conversation(self) -> None:
        import inspect

        candidate = build_external_candidate(
            channel="teams", kind="direct_message", author="Lead Reviewer",
            source_id="msg-9", rule=RULE, project=PROJECT,
            target=TARGET, origin_ticket=ORIGIN)
        self.assertEqual(candidate["class"], "normative")
        self.assertIn("teams:msg-9", candidate["content"])
        self.assertIn(RULE, candidate["content"])
        self.assertIn(ORIGIN, candidate["content"])
        self.assertEqual(candidate["evidence"], [{"ticket": ORIGIN}])
        params = inspect.signature(build_external_candidate).parameters
        for forbidden in ("body", "conversation", "text", "message"):
            self.assertNotIn(forbidden, params)

    def test_frontmatter_project_matches_project_dir(self) -> None:
        import yaml as _yaml

        candidate = build_external_candidate(
            channel="jira", kind="ticket_rejected", author="Ada Owner",
            source_id="PROJ-123", rule=RULE, project=PROJECT,
            target=TARGET, origin_ticket=ORIGIN)
        head = candidate["content"].split("---\n")[1]
        frontmatter = _yaml.safe_load(head)
        self.assertEqual(frontmatter["type"], "practice")
        self.assertEqual(frontmatter["project"], Path(PROJECT).name)

    def test_target_outside_practices_fails_closed(self) -> None:
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="jira", kind="ticket_reopened", author="Ada Owner",
                source_id="PROJ-1", rule=RULE, project=PROJECT,
                target=f"{PROJECT}/notes/fuera-de-practices.md",
                origin_ticket=ORIGIN)

    def test_blank_rule_target_origin_and_action_fail_closed(self) -> None:
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="jira", kind="ticket_reopened", author="Ada Owner",
                source_id="PROJ-1", rule="  ", project=PROJECT,
                target=TARGET, origin_ticket=ORIGIN)
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="jira", kind="ticket_reopened", author="Ada Owner",
                source_id="PROJ-1", rule=RULE, project="other/proj",
                target=TARGET, origin_ticket=ORIGIN)
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="jira", kind="ticket_reopened", author="Ada Owner",
                source_id="PROJ-1", rule=RULE, project=PROJECT,
                target=TARGET, origin_ticket="  ")
        with self.assertRaises(ExternalLearningError):
            build_external_candidate(
                channel="jira", kind="ticket_reopened", author="Ada Owner",
                source_id="PROJ-1", rule=RULE, project=PROJECT,
                target=TARGET, origin_ticket=ORIGIN, action="delete")

    def test_filename_is_stable_and_yaml(self) -> None:
        first = candidate_filename("jira", "PROJ-123", "ticket_rejected")
        self.assertEqual(
            first, candidate_filename("jira", "PROJ-123", "ticket_rejected"))
        self.assertTrue(first.endswith(".yaml"))
        self.assertNotIn("/", first)


class ConfirmationQueueTests(unittest.TestCase):
    def test_queued_candidate_reaches_block_confirmation(self) -> None:
        from orchestration import knowledge_confirm as kc

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            candidate = _candidate()
            name = candidate_filename("jira", "PROJ-123", "ticket_rejected")
            path = queue_external_candidate(
                candidate, origin=ORIGIN, base_dir=base, filename=name)
            self.assertTrue(path.is_file())
            pending = kc.collect_pending_confirmations(
                base, project_prefix=PROJECT)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].origin, ORIGIN)
            self.assertEqual(pending[0].target, TARGET)

    def test_foreign_project_prefix_does_not_group(self) -> None:
        from orchestration import knowledge_confirm as kc

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue_external_candidate(
                _candidate(), origin=ORIGIN, base_dir=base,
                filename=candidate_filename(
                    "jira", "PROJ-123", "ticket_rejected"))
            pending = kc.collect_pending_confirmations(
                base, project_prefix="projects/org/other")
            self.assertEqual(pending, [])

    def test_invalid_candidate_never_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = _candidate()
            bad["content"] = "sin frontmatter"
            with self.assertRaises(Exception):
                queue_external_candidate(
                    bad, origin=ORIGIN, base_dir=Path(tmp),
                    filename="bad.yaml")

    def test_queued_file_resolves_for_apply(self) -> None:
        from orchestration import knowledge_confirm as kc

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            name = candidate_filename("jira", "PROJ-123", "ticket_rejected")
            queue_external_candidate(
                _candidate(), origin=ORIGIN, base_dir=base, filename=name)
            pending = kc.collect_pending_confirmations(
                base, project_prefix=PROJECT)
            self.assertEqual(len(pending), 1)
            resolved = kc._queued_path(base, pending[0].origin, pending[0].name)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertTrue(resolved.is_file())
            self.assertEqual(resolved.name, name)


if __name__ == "__main__":
    unittest.main()
