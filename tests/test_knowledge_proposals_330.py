"""Knowledge proposal format + role instruction (TASK-2026-00330, D7).

At close the agent reviews learnings and leaves proposals in its ticket
folder, never the vault; a valid one is ready for the publisher (2.4);
schema errors, off-project targets, unknown actions/classes and missing
evidence are rejected with the recorded reason, never blocking close.

Synthetic ticket dirs under tmp dirs only: never the real vault, never
other projects. No tokens. The ticket client is simulated (a stub
comment sink plus plain dict configs), mirroring the acceptance wording.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration import knowledge_proposals as kp  # noqa: E402
from orchestration.agent_sandbox import ticket_dir  # noqa: E402

PROJECT = "projects/org/active"
TICKET = "TASK-2026-00997"

NOTE = ("---\ntype: practice\ntitle: Retries\n---\n\n# Retries\n\n"
        "Retry idempotent sends once.\n")


def _proposal(**overrides: object) -> dict:
    base: dict = {
        "target": f"{PROJECT}/practices/retries.md",
        "action": "create",
        "class": "descriptive",
        "base_version": None,
        "evidence": [{"ticket": TICKET}],
        "content": NOTE,
    }
    base.update(overrides)
    return base


class CollectProposalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str = "retries.yaml",
               **overrides: object) -> Path:
        folder = ticket_dir(TICKET, base_dir=self.base) \
            / kp.PROPOSALS_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(yaml.safe_dump(_proposal(**overrides)),
                        encoding="utf-8")
        return path

    def _collect(self, task_id: str = TICKET):
        return kp.collect_proposals(task_id, PROJECT, base_dir=self.base)

    def test_valid_proposal_ready_for_publisher(self) -> None:
        self._write()
        valid, rejected = self._collect()
        self.assertEqual(rejected, {})
        self.assertEqual(len(valid), 1)
        first = valid[0]
        self.assertEqual(first.target,
                         f"{PROJECT}/practices/retries.md")
        self.assertEqual(first.action, "create")
        self.assertEqual(first.proposal_class, "descriptive")
        self.assertIsNone(first.base_version)
        self.assertEqual(first.evidence, [{"ticket": TICKET}])
        self.assertIn("type: practice", first.content)

    def test_update_with_base_version_collected(self) -> None:
        self._write(action="update", base_version="abc123",
                    **{"class": "normative"})
        valid, rejected = self._collect()
        self.assertEqual(rejected, {})
        self.assertEqual(valid[0].action, "update")
        self.assertEqual(valid[0].proposal_class, "normative")
        self.assertEqual(valid[0].base_version, "abc123")

    def test_invalid_schema_rejected_with_reason(self) -> None:
        self._write(content="no frontmatter here, just prose")
        self._write("blank.yaml", target="   ")
        valid, rejected = self._collect()
        self.assertEqual(valid, [])
        self.assertEqual(len(rejected), 2)
        for reason in rejected.values():
            self.assertTrue(reason.strip())

    def test_off_project_target_rejected(self) -> None:
        self._write(target="projects/org/other/practices/notes.md")
        self._write("escape.yaml", target=f"{PROJECT}/../../etc/x.md")
        self._write("abs.yaml", target="/etc/passwd.md")
        valid, rejected = self._collect()
        self.assertEqual(valid, [])
        self.assertEqual(len(rejected), 3)
        for reason in rejected.values():
            self.assertIn("target", reason)

    def test_unknown_action_or_class_rejected(self) -> None:
        self._write("action.yaml", action="delete")
        self._write("class.yaml", **{"class": "opinion"})
        valid, rejected = self._collect()
        self.assertEqual(valid, [])
        self.assertEqual(len(rejected), 2)

    def test_missing_evidence_rejected(self) -> None:
        self._write(evidence=[])
        self._write("blank.yaml", evidence=[{"ticket": "", "commit": "",
                                             "path": ""}])
        valid, rejected = self._collect()
        self.assertEqual(valid, [])
        self.assertEqual(len(rejected), 2)
        for reason in rejected.values():
            self.assertIn("evidence", reason)

    def test_reject_never_blocks_close(self) -> None:
        folder = ticket_dir(TICKET, base_dir=self.base) \
            / kp.PROPOSALS_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "garbage.yaml").write_text("{{{\n", encoding="utf-8")
        (folder / "link.yaml").symlink_to(folder / "garbage.yaml")
        (folder / ".hidden.yaml").write_text("x: 1\n", encoding="utf-8")
        valid, rejected = self._collect()
        self.assertEqual(valid, [])
        self.assertIn("garbage.yaml", rejected)
        self.assertNotIn("link.yaml", rejected)
        self.assertNotIn(".hidden.yaml", rejected)

    def test_missing_folder_is_noop(self) -> None:
        self.assertEqual(self._collect("TASK-2026-00996"), ([], {}))

    def test_has_proposals(self) -> None:
        self.assertFalse(kp.has_proposals(TICKET, base_dir=self.base))
        self._write()
        self.assertTrue(kp.has_proposals(TICKET, base_dir=self.base))

    def test_commit_and_path_evidence_accepted(self) -> None:
        self._write(evidence=[{"commit": "deadbee"}, {"path": "a/b.py"}])
        valid, rejected = self._collect()
        self.assertEqual(rejected, {})
        self.assertEqual(valid[0].evidence,
                         [{"commit": "deadbee"}, {"path": "a/b.py"}])


VAULT_CONFIG = {"dev": {"vault_project": {"dir": "/vault",
                                                "project": PROJECT}}}


class StubTicketClient:
    """Simulated ticket client: records comments, changes no state."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    async def comment(self, task_id: str, text: str) -> bool:
        self.comments.append((task_id, text))
        return True


class CloseReviewInstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manager(self):  # type: ignore[no-untyped-def]
        from agents.dev.session import DevSessionManager
        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def _write(self, name: str = "retries.yaml",
               **overrides: object) -> Path:
        folder = ticket_dir(TICKET, base_dir=self.base) \
            / kp.PROPOSALS_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(yaml.safe_dump(_proposal(**overrides)),
                        encoding="utf-8")
        return path

    def test_simulated_close_collects_and_records_reasons(self) -> None:
        prefix = kp.knowledge_target_prefix(VAULT_CONFIG)
        self.assertEqual(prefix, PROJECT)
        ticket = {"task_id": "t", "ticket_number": 997,
                  "title": "t", "description": "d",
                  "project": "org/active"}
        prompt = self._manager()._build_coding_prompt(ticket, VAULT_CONFIG)
        self.assertIn(kp.PROPOSALS_DIRNAME, prompt)
        self.assertIn("agent-work/t", prompt)
        self.assertIn(PROJECT, prompt)
        self._write()
        self._write("broken.yaml", action="delete",
                    content="no frontmatter here, just prose")
        client = StubTicketClient()
        valid, rejected = asyncio.run(kp.collect_and_comment(
            TICKET, project_prefix=prefix, base_dir=self.base,
            comment=client.comment))
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0].name, "retries")
        self.assertEqual(list(rejected), ["broken.yaml"])
        self.assertEqual(len(client.comments), 1)
        task_id, text = client.comments[0]
        self.assertEqual(task_id, TICKET)
        self.assertIn("retries", text)
        self.assertIn("broken.yaml", text)
        self.assertIn(rejected["broken.yaml"], text)

    def test_simulated_close_without_proposals_stays_silent(self) -> None:
        client = StubTicketClient()
        valid, rejected = asyncio.run(kp.collect_and_comment(
            TICKET, project_prefix=PROJECT, base_dir=self.base,
            comment=client.comment))
        self.assertEqual((valid, rejected), ([], {}))
        self.assertEqual(client.comments, [])

    def test_tier_c_prompt_points_at_ticket_folder_not_vault(self) -> None:
        ticket = {"task_id": "t", "title": "t", "description": "d"}
        prompt = self._manager()._build_coding_prompt(ticket, VAULT_CONFIG)
        self.assertIn("agent-work/t", prompt)
        self.assertIn("never into the vault", prompt)
        self.assertIn(PROJECT, prompt)

    def test_tier_c_prompt_omitted_without_vault_mapping(self) -> None:
        ticket = {"task_id": "t", "title": "t", "description": "d"}
        prompt = self._manager()._build_coding_prompt(ticket, {"dev": {}})
        self.assertNotIn(kp.PROPOSALS_DIRNAME, prompt)

    def test_tier_s_builder_prompt_asks_review(self) -> None:
        from types import SimpleNamespace
        from agents.dev.tier_s import cycle as cycle_mod
        from agents.dev.tier_s.plan import Plan, PlannedFile
        cycle = cycle_mod.TierSCycle.__new__(cycle_mod.TierSCycle)
        ctx = SimpleNamespace(
            ticket={"task_id": "t", "title": "t", "description": "d",
                    "acceptance": ["a"]},
            repo=self.base, ref="T-1",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
            vault_index="", knowledge_prefix=PROJECT)
        plan = Plan(
            ticket="t", objective="", goal="g", acceptance=(),
            files=(PlannedFile(path="a.py", operation="modify",
                               reason="r"),),
            contracts=(), decisions=(), steps=("s1",),
            estimate_files=1, estimate_lines=10, out_of_scope=())
        prompt = cycle._builder_prompt(ctx, plan)
        self.assertIn(kp.PROPOSALS_DIRNAME, prompt)
        self.assertIn("agent-work/t", prompt)
        self.assertIn(PROJECT, prompt)

    def test_tier_s_builder_prompt_omitted_without_vault(self) -> None:
        from types import SimpleNamespace
        from agents.dev.tier_s import cycle as cycle_mod
        from agents.dev.tier_s.plan import Plan, PlannedFile
        cycle = cycle_mod.TierSCycle.__new__(cycle_mod.TierSCycle)
        ctx = SimpleNamespace(
            ticket={"task_id": "t", "title": "t", "description": "d",
                    "acceptance": ["a"]},
            repo=self.base, ref="T-1",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
            vault_index="", knowledge_prefix="")
        plan = Plan(
            ticket="t", objective="", goal="g", acceptance=(),
            files=(PlannedFile(path="a.py", operation="modify",
                               reason="r"),),
            contracts=(), decisions=(), steps=("s1",),
            estimate_files=1, estimate_lines=10, out_of_scope=())
        prompt = cycle._builder_prompt(ctx, plan)
        self.assertNotIn(kp.PROPOSALS_DIRNAME, prompt)

    def test_generic_agent_prompt_asks_review(self) -> None:
        from catalog import load_role_profile
        from orchestration import generic_agent
        profile = load_role_profile("dev")
        task = {"task_id": "t", "ticket_number": 1, "title": "t",
                "description": "d", "params": {}}
        prompt = generic_agent.build_agent_prompt(profile, task)
        self.assertIn(kp.PROPOSALS_DIRNAME, prompt)

    def test_dev_profile_orders_search_before_propose(self) -> None:
        from catalog import load_role_profile
        profile = load_role_profile("dev")
        blob = "; ".join(profile.card.delivers)
        self.assertIn("ticket folder", blob)
        self.assertIn("search", blob)
        self.assertIn("existing note", blob)


class ProposalRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _folder(self) -> Path:
        folder = ticket_dir(TICKET, base_dir=self.base) \
            / kp.PROPOSALS_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def test_undecodable_file_rejected_without_raising(self) -> None:
        folder = self._folder()
        (folder / "binary.yaml").write_bytes(b"\xff\xfe\x00bad\x80utf8")
        valid, rejected = kp.collect_proposals(
            TICKET, PROJECT, base_dir=self.base)
        self.assertEqual(valid, [])
        self.assertIn("binary.yaml", rejected)
        self.assertIn("unreadable", rejected["binary.yaml"])

    def test_missing_project_prefix_rejects_without_raising(self) -> None:
        folder = self._folder()
        (folder / "retries.yaml").write_text(
            yaml.safe_dump(_proposal()), encoding="utf-8")
        for project in (None, "", "  "):
            valid, rejected = kp.collect_proposals(
                TICKET, project, base_dir=self.base)
            self.assertEqual(valid, [])
            self.assertIn("no project folder", rejected["retries.yaml"])

    def test_oversized_file_rejected_without_reading(self) -> None:
        folder = self._folder()
        (folder / "huge.yaml").write_text(
            "x" * (kp.MAX_PROPOSAL_BYTES + 1), encoding="utf-8")
        valid, rejected = kp.collect_proposals(
            TICKET, PROJECT, base_dir=self.base)
        self.assertEqual(valid, [])
        self.assertIn("exceeds", rejected["huge.yaml"])

    def test_schema_reason_hides_note_content(self) -> None:
        folder = self._folder()
        secret = "SECRET-ABCDEF-12345-do-not-echo"
        (folder / "leaky.yaml").write_text(
            yaml.safe_dump(_proposal(action="delete",
                                     content=NOTE + secret + "\n")),
            encoding="utf-8")
        valid, rejected = kp.collect_proposals(
            TICKET, PROJECT, base_dir=self.base)
        self.assertEqual(valid, [])
        reason = rejected["leaky.yaml"]
        self.assertIn("invalid schema", reason)
        self.assertNotIn(secret, reason)
        self.assertNotIn("SECRET-ABCDEF", reason)

    def test_target_prefix_derived_from_project_config(self) -> None:
        self.assertEqual(
            kp.knowledge_target_prefix(VAULT_CONFIG), PROJECT)
        self.assertEqual(
            kp.knowledge_target_prefix(
                {"dev": {"vault_project": {"project": "  a/b/ "}}}),
            "a/b")
        self.assertEqual(kp.knowledge_target_prefix({"dev": {}}), "")
        self.assertEqual(kp.knowledge_target_prefix(None), "")

    def test_prompt_for_points_at_real_ticket_folder(self) -> None:
        prompt = kp.proposal_prompt_for("t", project_prefix=PROJECT)
        self.assertIn("agent-work/t", prompt)
        self.assertIn(kp.PROPOSALS_DIRNAME, prompt)
        self.assertIn(PROJECT, prompt)
        fallback = kp.proposal_prompt_for("not a valid id!")
        self.assertEqual(fallback, kp.KNOWLEDGE_PROPOSAL_PROMPT)

    def test_close_comment_names_valid_and_reasons(self) -> None:
        self.assertIsNone(kp.format_close_comment([], {}))
        text = kp.format_close_comment([], {"a.yaml": "bad"})
        assert text is not None
        self.assertIn("0 valid", text)
        self.assertIn("a.yaml", text)
        self.assertIn("bad", text)


if __name__ == "__main__":
    unittest.main()
