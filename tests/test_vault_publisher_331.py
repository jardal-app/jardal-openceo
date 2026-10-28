"""Vault publisher at ticket close (TASK-2026-00331, D7/D8).

The close of a ticket invokes the publisher with its folder proposals:
a valid descriptive one publishes (commit, no push); a changed base
version or an uncommitted note defers with the proposal kept; backup
and publisher never overlap (shared lock); unknown evidence, foreign
projects and secrets reject with the recorded reason; normative
content queues for Owner confirmation (2.5).

Synthetic git repos, vaults and ticket dirs under tmp dirs only: never
the real vault, never other projects. The real vault_check.py is
copied into the synthetic vault layout so link/frontmatter validation
runs for real. The ticket client is simulated. No tokens.
"""

from __future__ import annotations

import asyncio
import fcntl
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration import vault_publisher as vp  # noqa: E402
from orchestration.agent_sandbox import ticket_dir  # noqa: E402

PROJECT = "projects/org/active"
TICKET = "TASK-2026-00997"
TICKET_REF = "TASK-2026-00331"

REAL_CHECKER = Path(
    "/home/develop/development/Jardal/jardal-knowledge/.agents/skills"
    "/llm-vault-links/scripts/vault_check.py")

HUB = ("---\ntype: hub\nproject: active\ndate: 2026-10-28\n---\n\n"
       "# Active\n\nProject hub.\n")

NOTE = ("---\ntype: practice\nproject: active\ndate: 2026-10-28\n---\n\n"
        "# Retries\n\nRetry idempotent sends once.\n")


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


class StubTicketClient:
    """Simulated ticket client: records comments, changes no state."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    async def comment(self, task_id: str, text: str) -> bool:
        self.comments.append((task_id, text))
        return True


async def _yes(_name: str) -> bool:
    return True


async def _no(_name: str) -> bool:
    return False


async def _unknown(_name: str) -> None:
    return None


class PublisherCase(unittest.TestCase):
    def setUp(self) -> None:
        if not REAL_CHECKER.is_file():
            self.skipTest(f"vault checker not present at {REAL_CHECKER}")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.evidence = self._git_repo("evidence")
        (self.evidence / "base.txt").write_text("baseline\n")
        self._git(self.evidence, "add", "--all")
        self._git(self.evidence, "commit", "--quiet", "-m", "Baseline")
        self.commit = self._git(
            self.evidence, "rev-parse", "HEAD").stdout.strip()
        self.vault_repo = self._git_repo("vaultrepo")
        self.vault_dir = self.vault_repo / "vault"
        project_dir = self.vault_dir / PROJECT
        project_dir.mkdir(parents=True)
        (project_dir / "active.md").write_text(HUB, encoding="utf-8")
        checker_dir = (self.vault_repo / ".agents" / "skills"
                       / "llm-vault-links" / "scripts")
        checker_dir.mkdir(parents=True)
        shutil.copyfile(REAL_CHECKER, checker_dir / "vault_check.py")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Baseline")
        self.base = self.root / "work"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        env = dict(__import__("os").environ, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "--initial-branch=main", str(repo)],
                       check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(repo), "config", "user.name",
                        "Publisher Test"], check=True, capture_output=True,
                       env=env)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "publisher@example.invalid"], check=True,
                       capture_output=True, env=env)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign",
                        "false"], check=True, capture_output=True, env=env)
        return repo

    def _git(self, repo: Path, *args: str):
        return subprocess.run(["git", "-C", str(repo), *args],
                              text=True, capture_output=True, check=True)

    def _config(self, **overrides: object) -> vp.PublishConfig:
        return vp.PublishConfig(
            ticket_ref=TICKET_REF, vault_repo=self.vault_repo,
            vault_dir=self.vault_dir, project=PROJECT,
            evidence_repo=self.evidence, **overrides)  # type: ignore[arg-type]

    def _write(self, name: str = "retries.yaml",
               **overrides: object) -> Path:
        folder = ticket_dir(TICKET, base_dir=self.base) / "knowledge_proposals"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(yaml.safe_dump(_proposal(**overrides)),
                        encoding="utf-8")
        return path

    def _run(self, **overrides: object) -> vp.PublishOutcome:
        client = StubTicketClient()
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_yes, comment=client.comment, **overrides))
        self._client = client
        return outcome

    def _note(self) -> Path:
        return self.vault_dir / PROJECT / "practices/retries.md"

    def _head_subject(self) -> str:
        return self._git(self.vault_repo, "log", "--format=%s", "-1"
                         ).stdout.strip()

    def test_valid_descriptive_publishes(self) -> None:
        self._write()
        outcome = self._run()
        self.assertEqual(outcome.published, ["retries"])
        self.assertEqual(outcome.deferred, {})
        self.assertEqual(outcome.rejected, {})
        self.assertEqual(self._note().read_text(encoding="utf-8"), NOTE)
        self.assertIn(f"[{TICKET_REF}]", self._head_subject())
        proposals = ticket_dir(
            TICKET, base_dir=self.base) / "knowledge_proposals"
        self.assertFalse((proposals / "retries.yaml").exists())
        self.assertEqual(len(self._client.comments), 1)
        self.assertIn("1 published", self._client.comments[0][1])

    def test_commit_scopes_only_published_paths(self) -> None:
        async def _only_ticket(name: str) -> bool:
            return name == TICKET

        self._write()
        self._write("bad.yaml", target=f"{PROJECT}/practices/bad.md",
                    evidence=[{"ticket": "TASK-2026-00000"}])
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_only_ticket, comment=StubTicketClient().comment))
        self.assertEqual(outcome.published, ["retries"])
        self.assertIn("bad", outcome.rejected)
        self.assertIn(f"[{TICKET_REF}]", self._head_subject())
        names = self._git(self.vault_repo, "show", "--name-only",
                          "--format=", "HEAD").stdout
        self.assertIn("practices/retries.md", names)
        self.assertNotIn("bad.md", names)

    def test_commit_and_path_evidence_resolve(self) -> None:
        self._write(evidence=[{"commit": self.commit}])
        self._write("path.yaml", target=f"{PROJECT}/practices/paths.md",
                    evidence=[{"path": "base.txt"}])
        outcome = self._run()
        self.assertEqual(sorted(outcome.published), ["path", "retries"])
        names = self._git(self.vault_repo, "log", "--name-only",
                          "--format=", "-2").stdout
        self.assertIn("practices/paths.md", names)
        self.assertIn("practices/retries.md", names)

    def test_update_with_matching_base_publishes(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Seed note")
        base = self._git(self.vault_repo, "log", "-1", "--format=%H", "--",
                         f"vault/{PROJECT}/practices/retries.md").stdout.strip()
        updated = NOTE + "\nSecond paragraph.\n"
        self._write(action="update", base_version=base,
                    content=updated, **{"class": "descriptive"})
        outcome = self._run()
        self.assertEqual(outcome.published, ["retries"])
        self.assertEqual(note.read_text(encoding="utf-8"), updated)

    def test_changed_base_version_defers_and_keeps(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Seed note")
        head_before = self._git(
            self.vault_repo, "rev-parse", "HEAD").stdout.strip()
        self._write(action="update", base_version="0" * 40,
                    **{"class": "descriptive"})
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.deferred)
        self.assertIn("version changed", outcome.deferred["retries"])
        proposals = ticket_dir(TICKET, base_dir=self.base)
        self.assertTrue(
            (proposals / "knowledge_proposals/retries.yaml").exists())
        self.assertEqual(self._git(
            self.vault_repo, "rev-parse", "HEAD").stdout.strip(), head_before)
        self.assertEqual(note.read_text(encoding="utf-8"), NOTE)

    def test_uncommitted_note_defers(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Seed note")
        note.write_text(NOTE + "Local edit.\n", encoding="utf-8")
        base = self._git(self.vault_repo, "log", "-1", "--format=%H", "--",
                         f"vault/{PROJECT}/practices/retries.md").stdout.strip()
        self._write(action="update", base_version=base,
                    **{"class": "descriptive"})
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.deferred)
        self.assertIn("uncommitted", outcome.deferred["retries"])
        self.assertEqual(note.read_text(encoding="utf-8"), NOTE + "Local edit.\n")

    def test_normative_queues_for_confirmation(self) -> None:
        self._write(**{"class": "normative"})
        outcome = self._run()
        self.assertEqual(outcome.queued, ["retries"])
        self.assertEqual(outcome.published, [])
        self.assertFalse(self._note().exists())
        queue = ticket_dir(
            TICKET, base_dir=self.base) / "knowledge_confirm_queue/retries.yaml"
        self.assertTrue(queue.is_file())
        self.assertIn("2.5", self._client.comments[0][1])
        self.assertIn("sha256", self._client.comments[0][1])

    def test_mandate_touching_descriptive_queues(self) -> None:
        content = NOTE + "\nEvery agent must obey the mandate update.\n"
        self._write(content=content)
        outcome = self._run()
        self.assertEqual(outcome.queued, ["retries"])
        self.assertEqual(outcome.published, [])
        self.assertFalse(self._note().exists())

    def test_unresolving_evidence_rejects(self) -> None:
        self._write(evidence=[{"ticket": "TASK-2026-00000"},
                              {"commit": "0" * 40},
                              {"path": "missing/file.txt"}])
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_no, comment=StubTicketClient().comment))
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.rejected)
        self.assertIn("does not resolve", outcome.rejected["retries"])
        self.assertFalse(self._note().exists())

    def test_unknown_ticket_evidence_defers(self) -> None:
        self._write()
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_unknown, comment=StubTicketClient().comment))
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.deferred)
        proposals = ticket_dir(TICKET, base_dir=self.base)
        self.assertTrue(
            (proposals / "knowledge_proposals/retries.yaml").exists())

    def test_off_project_target_rejected(self) -> None:
        self._write(target="projects/org/other/practices/x.md")
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.rejected)
        self.assertIn("outside the ticket project",
                      self._client.comments[0][1])

    def test_secret_content_rejected(self) -> None:
        content = NOTE + "\nKey: AKIAIOSFODNN7EXAMPLE\n"
        self._write(content=content)
        outcome = self._run()
        self.assertIn("retries", outcome.rejected)
        self.assertIn("secret", outcome.rejected["retries"])
        self.assertFalse(self._note().exists())

    def test_reserved_basename_rejected(self) -> None:
        self._write(target=f"{PROJECT}/CLAUDE.md")
        outcome = self._run()
        self.assertIn("retries", outcome.rejected)
        self.assertIn("reserved", outcome.rejected["retries"])
        self.assertFalse((self.vault_dir / PROJECT / "CLAUDE.md").exists())

    def test_vault_check_failure_rejects(self) -> None:
        bad = ("---\ntype: practice\nproject: wrong\ndate: 2026-10-28\n---\n\n"
               "# Bad\n\nBody.\n")
        self._write(content=bad)
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.rejected)
        self.assertIn("vault check failed", outcome.rejected["retries"])
        self.assertFalse(self._note().exists())

    def test_create_on_existing_note_rejects(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Seed note")
        self._write()
        outcome = self._run()
        self.assertIn("retries", outcome.rejected)
        self.assertIn("already exists", outcome.rejected["retries"])

    def test_busy_lock_defers_and_later_publish_succeeds(self) -> None:
        self._write()
        git_dir = self._git(
            self.vault_repo, "rev-parse", "--git-dir").stdout.strip()
        lock_path = self.vault_repo / git_dir / vp.LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            outcome = asyncio.run(vp.publish_after_close(
                TICKET, config=self._config(), base_dir=self.base,
                ticket_exists=_yes, comment=StubTicketClient().comment))
            self.assertEqual(outcome.published, [])
            self.assertIn("retries", outcome.deferred)
            self.assertIn("busy", outcome.deferred["retries"])
            self.assertFalse(self._note().exists())
        outcome = self._run()
        self.assertEqual(outcome.published, ["retries"])
        self.assertTrue(self._note().is_file())

    def test_empty_close_posts_no_comment(self) -> None:
        client = StubTicketClient()
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_yes, comment=client.comment))
        self.assertEqual((outcome.published, outcome.queued), ([], []))
        self.assertEqual((outcome.deferred, outcome.rejected), ({}, {}))
        self.assertEqual(client.comments, [])
        self.assertIsNone(vp.format_publish_comment(outcome, {}))

    def test_create_over_untracked_note_defers(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("Another session draft.\n", encoding="utf-8")
        self._write()
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.deferred)
        self.assertIn("uncommitted", outcome.deferred["retries"])
        self.assertEqual(note.read_text(encoding="utf-8"),
                         "Another session draft.\n")

    def test_symlink_target_rejected(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside.md"
        outside.write_text("Outside.\n", encoding="utf-8")
        try:
            note.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        self._write()
        outcome = self._run()
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.rejected)
        self.assertIn("symlink", outcome.rejected["retries"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "Outside.\n")

    def test_checker_infra_defers_and_keeps(self) -> None:
        self._write()
        config = self._config(checker=Path("/nonexistent/vault_check.py"))
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=config, base_dir=self.base,
            ticket_exists=_yes, comment=StubTicketClient().comment))
        self.assertEqual(outcome.published, [])
        self.assertIn("retries", outcome.deferred)
        self.assertIn("unavailable", outcome.deferred["retries"])
        proposals = ticket_dir(TICKET, base_dir=self.base)
        self.assertTrue(
            (proposals / "knowledge_proposals/retries.yaml").exists())
        self.assertFalse(self._note().exists())

    def test_identical_update_publishes_idempotent(self) -> None:
        note = self._note()
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(NOTE, encoding="utf-8")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Seed note")
        head_before = self._git(
            self.vault_repo, "rev-parse", "HEAD").stdout.strip()
        base = self._git(self.vault_repo, "log", "-1", "--format=%H", "--",
                         f"vault/{PROJECT}/practices/retries.md").stdout.strip()
        self._write(action="update", base_version=base,
                    **{"class": "descriptive"})
        outcome = self._run()
        self.assertEqual(outcome.published, ["retries"])
        self.assertEqual(self._git(
            self.vault_repo, "rev-parse", "HEAD").stdout.strip(), head_before)
        self.assertEqual(note.read_text(encoding="utf-8"), NOTE)

    def test_normative_mandate_flags_owner_approval(self) -> None:
        content = NOTE + "\nNew mandate obligation for agents.\n"
        self._write(content=content, **{"class": "normative"})
        outcome = self._run()
        self.assertEqual(outcome.queued, ["retries"])
        self.assertIn("express Owner approval",
                      self._client.comments[0][1])

    def test_files_consumed_without_comment_callback(self) -> None:
        self._write()
        self._write("rules.yaml", target=f"{PROJECT}/practices/rules.md",
                    **{"class": "normative"})
        outcome = asyncio.run(vp.publish_after_close(
            TICKET, config=self._config(), base_dir=self.base,
            ticket_exists=_yes, comment=None))
        self.assertEqual(outcome.published, ["retries"])
        self.assertEqual(outcome.queued, ["rules"])
        folder = ticket_dir(TICKET, base_dir=self.base)
        self.assertFalse((folder / "knowledge_proposals/retries.yaml").exists())
        self.assertFalse((folder / "knowledge_proposals/rules.yaml").exists())
        self.assertTrue(
            (folder / "knowledge_confirm_queue/rules.yaml").is_file())

    def test_target_guard_direct(self) -> None:
        self.assertIsNone(vp._target_usable(f"{PROJECT}/practices/a.md", PROJECT))
        self.assertIsNotNone(vp._target_usable("projects/other/x.md", PROJECT))
        self.assertIsNotNone(vp._target_usable("/abs/path.md", PROJECT))
        self.assertIsNotNone(vp._target_usable(f"{PROJECT}/../escape.md", PROJECT))
        self.assertIsNotNone(vp._target_usable(f"{PROJECT}/CLAUDE.md", PROJECT))
        self.assertIsNotNone(vp._target_usable(f"{PROJECT}/note.txt", PROJECT))

    def test_mirror_ticket_returns_name_string(self) -> None:
        from orchestration.crystallization import mirror_ticket
        mirrored = {"params": {"_mirrors": {"business": {"name": TICKET_REF}}}}
        name = mirror_ticket(mirrored)
        self.assertIsInstance(name, str)
        self.assertEqual(name, TICKET_REF)
        self.assertEqual(str(name or ""), TICKET_REF)
        self.assertIsNone(mirror_ticket({"task_id": "x"}))


if __name__ == "__main__":
    unittest.main()
