"""Internal crystallization save step (TASK-2026-00210).

On ticket close the agent leaves drafts under ``skill_drafts/`` in its
ticket folder; the deterministic save gate promotes them to versioned
role skills: everything new enters as a draft, a draft promotes only
from another ticket, at most 10 drafts per role (oldest deleted), only
the agent's own role is touched, every touched skill passes the
normative loader plus ``muse skills validate`` (and its script tests
fenced when scripts changed), and the result commits in a dedicated
worktree over origin/main with the ticket number, pushing at once.

No network, no LLM. Git tests run against a fixture bare origin in a
temporary dir; the real ``muse``/bwrap integration runs only when both
are present on the host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    AgentCard,
    RoleProfile,
    load_role_skills,
)
from orchestration import crystallization as crys  # noqa: E402
from orchestration.crystallization import (  # noqa: E402
    SaveOutcome,
    SaveStepConfig,
)

TICKET = "TASK-2026-00999"
OTHER_TICKET = "TASK-2026-00998"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "crys-test",
    "GIT_AUTHOR_EMAIL": "crys-test@example.invalid",
    "GIT_COMMITTER_NAME": "crys-test",
    "GIT_COMMITTER_EMAIL": "crys-test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", timeout=120,
        env=_GIT_ENV, check=False)


def _git_ok(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    assert proc.returncode == 0, f"git {args}: {proc.stderr[:400]}"
    return proc.stdout


def _skill_md(name: str, description: str = "Does role work.",
              status: str = "borrador",
              ticket: str = "TASK-2026-00001",
              day: str = "2026-10-01", body: str = "# Skill\n") -> str:
    return (f"---\nname: {name}\ndescription: {description}\n"
            f"status: {status}\nticket: {ticket}\ndate: {day}\n---\n\n{body}")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_role(root: Path, role: str, skills: list[str],
               profile_skills: list[str] | None = None,
               profile: str | None = None) -> None:
    """Minimal card (+ optional profile) for one fixture AI agent."""
    card = {
        "name": role, "type": "ai", "engine": "muse-code",
        "profile": (f"profiles/{role}.yaml" if profile is None
                    else profile),
        "boundary_tools": [], "skills_dir": f"skills/{role}",
        "skills": skills, "inputs": ["x"], "outputs": ["y"],
        "enabled": True, "disabled_reason": None,
        "decided_by_ticket": None,
    }
    _write(root / "catalog" / "agents" / f"{role}.yaml",
           "# Agent card (fixture).\n"
           + yaml.safe_dump(card, sort_keys=False))
    if card["profile"] is not None:
        _write(root / "catalog" / str(card["profile"]),
               "# Role profile (fixture).\n" + yaml.safe_dump(
                   {"name": role, "agent": role, "engine": "muse-code",
                    "card": {"who": f"{role} fixture",
                             "delivers": ["fixture work"],
                             "never": ["change ticket states"]},
                    "rules": "AGENTS.md",
                    "skills": (profile_skills if profile_skills is not None
                               else ["create-skill", *skills]),
                    "output_schema": None},
                   sort_keys=False))


def make_origin(root: Path) -> tuple[Path, Path]:
    """Bare origin + seeded clone; returns ``(origin, repo)``."""
    origin = root / "origin.git"
    _git_ok(root, "init", "--bare", "-b", "main", str(origin))
    repo = root / "repo"
    _git_ok(root, "clone", str(origin), str(repo))
    write_role(repo, "alpha", ["known"])
    _write(repo / "skills" / "alpha" / "known" / "SKILL.md",
           _skill_md("known", status="activa"))
    _git_ok(repo, "add", ".")
    _git_ok(repo, "commit", "-m", "seed fixture catalog")
    _git_ok(repo, "push", "origin", "main")
    return origin, repo


def write_draft(drafts: Path, name: str, *,
                description: str = "Drafted work.",
                body: str = "# Draft\n", header: str | None = None,
                scripts: dict[str, str] | None = None) -> Path:
    """One agent draft: SKILL.md (+ optional scripts/) without the stamp."""
    target = drafts / name
    if header is None:
        header = f"name: {name}\ndescription: {description}\n"
    _write(target / "SKILL.md", f"---\n{header}---\n\n{body}")
    for rel, text in (scripts or {}).items():
        _write(target / rel, text)
    return target


def ok_validate(path: Path) -> tuple[bool, str]:
    return True, ""


def fail_validate(path: Path) -> tuple[bool, str]:
    return False, "validate boom"


def ok_tests(**kwargs: Any) -> tuple[bool, str]:
    return True, ""


def fail_tests(**kwargs: Any) -> tuple[bool, str]:
    return False, "red tests"


def run_config(repo: Path, drafts: Path, ticket: str = TICKET,
               agent: str = "alpha", **over: Any) -> SaveStepConfig:
    params: dict[str, Any] = {
        "repo": repo, "agent": agent, "ticket": ticket,
        "drafts_dir": drafts, "lock_path": repo.parent / "test.lock",
        "today": date(2026, 10, 13), "validate": ok_validate,
        "run_skill_tests": ok_tests,
    }
    params.update(over)
    return SaveStepConfig(**params)


def origin_file(origin: Path, ref_path: str) -> str:
    return _git_ok(origin.parent, "--git-dir", str(origin), "show",
                   f"HEAD:{ref_path}")


def origin_log(origin: Path) -> list[str]:
    return _git_ok(origin.parent, "--git-dir", str(origin), "log",
                   "--format=%s", "main").splitlines()


class CollectTests(unittest.TestCase):
    def test_missing_drafts_dir_skips_without_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = crys.run_save_step(run_config(
                root / "repo", root / "ticket" / "skill_drafts"))
            self.assertEqual("no drafts", out.skipped)
            self.assertIsNone(out.comment)
            self.assertIsNone(out.commit)
            self.assertFalse((root / "test.lock").exists())

    def test_bad_ticket_is_a_programmer_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "s")
            with self.assertRaises(ValueError):
                crys.run_save_step(run_config(root / "repo", drafts,
                                              ticket="not-a-task"))

    def test_draft_without_skill_md_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            (drafts / "empty").mkdir(parents=True)
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("empty", out.discarded)
            self.assertIn("SKILL.md", out.discarded["empty"])
            self.assertIsNone(out.commit)
            assert out.comment is not None
            self.assertIn("empty", out.comment)
            self.assertFalse((drafts / "empty").exists())

    def test_bad_name_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "Bad Name")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("Bad Name", out.discarded)
            self.assertIsNone(out.commit)

    def test_blank_description_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "nodesc", description="   ")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("nodesc", out.discarded)

    def test_oversize_draft_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            target = write_draft(drafts, "huge")
            _write(target / "dump.bin", "x" * (crys.MAX_DRAFT_BYTES + 1))
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("huge", out.discarded)
            self.assertIn("too large", out.discarded["huge"])

    def test_symlink_entry_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            drafts.mkdir(parents=True)
            (drafts / "link").symlink_to(drafts)
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual("no drafts", out.skipped)
            self.assertTrue((drafts / "link").is_symlink())


class GateTests(unittest.TestCase):
    def test_new_draft_enters_as_borrador(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["fresh"], out.created)
            self.assertIsNotNone(out.commit)
            stored = origin_file(origin, "skills/alpha/fresh/SKILL.md")
            self.assertIn("status: borrador", stored)
            self.assertIn(f"ticket: {TICKET}", stored)
            self.assertIn("date: 2026-10-13", stored)
            self.assertIn("name: fresh", stored)
            card = yaml.safe_load(origin_file(
                origin, "catalog/agents/alpha.yaml"))
            self.assertEqual(["fresh", "known"], card["skills"])
            profile = yaml.safe_load(origin_file(
                origin, "catalog/profiles/alpha.yaml"))
            self.assertIn("fresh", profile["skills"])
            self.assertFalse((drafts / "fresh").exists())
            assert out.comment is not None
            self.assertIn(TICKET, out.comment)
            self.assertIn("fresh", out.comment)

    def test_agent_header_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "sneaky", header=(
                "name: sneaky\ndescription: Sneaky.\nstatus: activa\n"
                "ticket: TASK-2026-00001\ndate: 2020-01-01\n"))
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["sneaky"], out.created)
            stored = origin_file(origin, "skills/alpha/sneaky/SKILL.md")
            self.assertIn("status: borrador", stored)
            self.assertIn(f"ticket: {TICKET}", stored)

    def test_promotion_only_from_another_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "reuse")
            first = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["reuse"], first.created)
            drafts2 = root / "ticket2" / "skill_drafts"
            write_draft(drafts2, "reuse", body="# Draft v2\n")
            out = crys.run_save_step(run_config(repo, drafts2,
                                                ticket=OTHER_TICKET))
            self.assertEqual(["reuse"], out.promoted)
            stored = origin_file(origin, "skills/alpha/reuse/SKILL.md")
            self.assertIn("status: activa", stored)
            self.assertIn(f"ticket: {TICKET}", stored)
            self.assertIn("Draft v2", stored)

    def test_same_ticket_update_stays_borrador(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "mine")
            crys.run_save_step(run_config(repo, drafts))
            write_draft(drafts, "mine", body="# Draft v2\n")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual([], out.promoted)
            self.assertEqual(["mine"], out.updated)
            stored = origin_file(origin, "skills/alpha/mine/SKILL.md")
            self.assertIn("status: borrador", stored)
            self.assertIn("Draft v2", stored)

    def test_activa_update_keeps_creation_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "known", body="# Known improved\n")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["known"], out.updated)
            stored = origin_file(origin, "skills/alpha/known/SKILL.md")
            self.assertIn("status: activa", stored)
            self.assertIn("ticket: TASK-2026-00001", stored)
            self.assertIn("date: 2026-10-01", stored)
            self.assertIn("Known improved", stored)

    def test_unchanged_draft_commits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "known", description="Does role work.",
                        body="# Skill\n")
            before = origin_log(origin)
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["known"], out.unchanged)
            self.assertIsNone(out.commit)
            self.assertEqual(before, origin_log(origin))
            self.assertFalse((drafts / "known").exists())

    def test_cap_deletes_oldest_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            for pos in range(crys.MAX_DRAFTS_PER_ROLE):
                name = f"d{pos:02d}"
                _write(repo / "skills" / "alpha" / name / "SKILL.md",
                       _skill_md(name, ticket="TASK-2026-00002",
                                 day=f"2026-09-{pos + 1:02d}"))
            card_path = repo / "catalog" / "agents" / "alpha.yaml"
            crys.rewrite_skills_list(
                card_path, ["known", *[f"d{p:02d}" for p in range(10)]])
            profile_path = repo / "catalog" / "profiles" / "alpha.yaml"
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            crys.rewrite_skills_list(
                profile_path,
                [*data["skills"], *[f"d{p:02d}" for p in range(10)]])
            _git_ok(repo, "add", ".")
            _git_ok(repo, "commit", "-m", "seed ten drafts")
            _git_ok(repo, "push", "origin", "main")
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["fresh"], out.created)
            self.assertEqual(["d00"], out.deleted_oldest)
            card = yaml.safe_load(origin_file(
                origin, "catalog/agents/alpha.yaml"))
            self.assertNotIn("d00", card["skills"])
            self.assertIn("fresh", card["skills"])
            profile = yaml.safe_load(origin_file(
                origin, "catalog/profiles/alpha.yaml"))
            self.assertNotIn("d00", profile["skills"])
            proc = _git(origin.parent, "--git-dir", str(origin), "show",
                        "HEAD:skills/alpha/d00/SKILL.md")
            self.assertNotEqual(0, proc.returncode)
            assert out.comment is not None
            self.assertIn("d00", out.comment)

    def test_name_owned_by_another_role_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            write_role(repo, "beta", ["clash"])
            _write(repo / "skills" / "beta" / "clash" / "SKILL.md",
                   _skill_md("clash", status="activa"))
            _git_ok(repo, "add", ".")
            _git_ok(repo, "commit", "-m", "seed beta role")
            _git_ok(repo, "push", "origin", "main")
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "clash")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("clash", out.discarded)
            self.assertIn("beta", out.discarded["clash"])
            self.assertIsNone(out.commit)

    def test_unknown_agent_discards_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "ghost")
            out = crys.run_save_step(run_config(repo, drafts, agent="ghost"))
            self.assertIn("ghost", out.discarded)
            self.assertIsNone(out.commit)
            self.assertFalse((drafts / "ghost").exists())

    def test_red_sibling_role_defers_and_keeps_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            write_role(repo, "beta", ["broken"])
            _write(repo / "skills" / "beta" / "broken" / "SKILL.md",
                   "no frontmatter here\n")
            _git_ok(repo, "add", ".")
            _git_ok(repo, "commit", "-m", "seed red beta role")
            _git_ok(repo, "push", "origin", "main")
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIsNotNone(out.deferred)
            self.assertIsNone(out.commit)
            self.assertTrue((drafts / "fresh" / "SKILL.md").exists())

    def test_failed_validate_discards_only_that_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "good")
            write_draft(drafts, "bad")

            def selective(path: Path) -> tuple[bool, str]:
                return (False, "validate boom") if path.name == "bad" \
                    else (True, "")

            out = crys.run_save_step(run_config(repo, drafts,
                                                validate=selective))
            self.assertEqual(["good"], out.created)
            self.assertIn("bad", out.discarded)
            self.assertIn("boom", out.discarded["bad"])
            proc = _git(origin.parent, "--git-dir", str(origin), "show",
                        "HEAD:skills/alpha/bad/SKILL.md")
            self.assertNotEqual(0, proc.returncode)

    def test_failing_script_tests_discard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "coded", scripts={
                "scripts/helper.py": "VALUE = 1\n",
                "scripts/test_helper.py": "def test_v():\n    assert True\n",
            })
            calls: list[dict[str, Any]] = []

            def spy(**kwargs: Any) -> tuple[bool, str]:
                calls.append(kwargs)
                return False, "red tests"

            out = crys.run_save_step(run_config(repo, drafts,
                                                run_skill_tests=spy))
            self.assertIn("coded", out.discarded)
            self.assertIn("red", out.discarded["coded"])
            self.assertEqual(1, len(calls))
            self.assertIsNone(out.commit)

    def test_tests_run_only_when_scripts_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            scripts = {
                "scripts/helper.py": "VALUE = 1\n",
                "scripts/test_helper.py": "def test_v():\n    assert True\n",
            }
            known = repo / "skills" / "alpha" / "known"
            for rel, text in scripts.items():
                _write(known / rel, text)
            _git_ok(repo, "add", ".")
            _git_ok(repo, "commit", "-m", "seed known scripts")
            _git_ok(repo, "push", "origin", "main")
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "known", description="Does role work.",
                        body="# Skill\n", scripts=dict(scripts))
            calls: list[dict[str, Any]] = []

            def spy(**kwargs: Any) -> tuple[bool, str]:
                calls.append(kwargs)
                return True, ""

            out = crys.run_save_step(run_config(repo, drafts,
                                                run_skill_tests=spy))
            self.assertEqual(["known"], out.unchanged)
            self.assertEqual([], calls)
            write_draft(drafts, "known2", scripts=dict(scripts))
            out2 = crys.run_save_step(run_config(repo, drafts,
                                                 run_skill_tests=spy))
            self.assertEqual(["known2"], out2.created)
            self.assertEqual(1, len(calls))

    def test_script_without_test_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "lonely", scripts={
                "scripts/helper.py": "VALUE = 1\n",
            })
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIn("lonely", out.discarded)
            self.assertIn("test_helper.py", out.discarded["lonely"])

    def test_pycache_and_symlinks_never_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            target = write_draft(drafts, "dirty")
            cache = target / "scripts" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "helper.pyc").write_bytes(b"junk")
            (target / "escape").symlink_to(repo / "catalog")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["dirty"], out.created)
            tree = _git_ok(origin.parent, "--git-dir", str(origin),
                           "ls-tree", "-r", "--name-only", "HEAD")
            self.assertNotIn("__pycache__", tree)
            self.assertNotIn("escape", tree)


class GitTests(unittest.TestCase):
    def test_commit_carries_ticket_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            assert out.commit is not None
            log = origin_log(origin)
            self.assertIn(TICKET, log[0])
            self.assertIn("fresh", log[0])
            head = _git_ok(origin.parent, "--git-dir", str(origin),
                           "rev-parse", "main").strip()
            self.assertEqual(head, out.commit)

    def test_concurrent_closes_do_not_interleave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            outcomes: dict[str, SaveOutcome] = {}
            errors: list[BaseException] = []

            def close(ticket: str, name: str) -> None:
                try:
                    drafts = root / ticket / "skill_drafts"
                    write_draft(drafts, name)
                    outcomes[name] = crys.run_save_step(run_config(
                        repo, drafts, ticket=ticket))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=close,
                                 args=("TASK-2026-00991", "one")),
                threading.Thread(target=close,
                                 args=("TASK-2026-00992", "two")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=300)
            self.assertEqual([], errors)
            self.assertEqual(["one"], outcomes["one"].created)
            self.assertEqual(["two"], outcomes["two"].created)
            card = yaml.safe_load(origin_file(
                origin, "catalog/agents/alpha.yaml"))
            self.assertEqual(["known", "one", "two"], card["skills"])
            self.assertIn("one",
                          origin_file(origin, "skills/alpha/one/SKILL.md"))
            self.assertIn("two",
                          origin_file(origin, "skills/alpha/two/SKILL.md"))

    def test_push_conflict_rebases_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            rival = root / "rival"
            _git_ok(root, "clone", str(origin), str(rival))
            _write(rival / "skills" / "alpha" / "rival" / "SKILL.md",
                   _skill_md("rival", status="activa"))
            _git_ok(rival, "add", ".")
            _git_ok(rival, "commit", "-m", "rival skill")
            _git_ok(rival, "push", "origin", "main")
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertEqual(["fresh"], out.created)
            self.assertIsNotNone(out.commit)
            log = origin_log(origin)
            self.assertEqual(3, len(log))
            self.assertIn(TICKET, log[0])
            self.assertIn("rival skill", log[1])

    def test_clean_rebase_lands_on_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            rival = root / "rival"
            _git_ok(root, "clone", str(origin), str(rival))

            def land_rival() -> None:
                _write(rival / "notes" / "rival.md", "# rival\n")
                _git_ok(rival, "add", ".")
                _git_ok(rival, "commit", "-m", "rival note")
                _git_ok(rival, "push", "origin", "main")

            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts,
                                                pre_push=land_rival))
            self.assertEqual(["fresh"], out.created)
            self.assertIsNotNone(out.commit)
            log = origin_log(origin)
            self.assertEqual(3, len(log))
            self.assertIn(TICKET, log[0])
            self.assertIn("rival note", log[1])

    def test_rebase_conflict_defers_and_keeps_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            rival = root / "rival"
            _git_ok(root, "clone", str(origin), str(rival))

            def land_rival() -> None:
                _write(rival / "skills" / "alpha" / "fresh" / "SKILL.md",
                       _skill_md("fresh", status="activa",
                                 body="# Rival version\n"))
                _git_ok(rival, "add", ".")
                _git_ok(rival, "commit", "-m", "rival claims fresh")
                _git_ok(rival, "push", "origin", "main")

            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts,
                                                pre_push=land_rival))
            self.assertIsNotNone(out.deferred)
            self.assertIn("conflict", out.deferred or "")
            self.assertIsNone(out.commit)
            self.assertEqual({}, out.discarded)
            self.assertEqual([], out.created)
            self.assertTrue((drafts / "fresh" / "SKILL.md").exists())
            assert out.comment is not None
            self.assertIn("conflict", out.comment)
            self.assertEqual(["rival claims fresh", "seed fixture catalog"],
                             origin_log(origin))

    def test_pushed_tree_passes_catalog_skill_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "fresh")
            out = crys.run_save_step(run_config(repo, drafts))
            self.assertIsNotNone(out.commit)
            clone = root / "check"
            _git_ok(root, "clone", str(origin), str(clone))
            card = AgentCard.model_validate(yaml.safe_load(
                (clone / "catalog" / "agents" / "alpha.yaml")
                .read_text(encoding="utf-8")))
            profile = RoleProfile.model_validate(yaml.safe_load(
                (clone / "catalog" / "profiles" / "alpha.yaml")
                .read_text(encoding="utf-8")))
            disk = {skill.name for skill in load_role_skills(
                "alpha", card.skills_dir, clone)}
            self.assertEqual(set(card.skills), disk)
            self.assertEqual([], [skill for skill in card.skills
                                  if skill not in profile.skills])


class SurgeryTests(unittest.TestCase):
    def test_flow_empty_becomes_block_preserving_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.yaml"
            _write(path, "# Agent card (fixture).\nname: a\nskills: []\n"
                   "enabled: true\n")
            crys.rewrite_skills_list(path, ["b-skill", "a-skill"])
            self.assertEqual(
                "# Agent card (fixture).\nname: a\nskills:\n"
                "  - b-skill\n  - a-skill\nenabled: true\n",
                path.read_text(encoding="utf-8"))

    def test_block_items_replaced_comments_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.yaml"
            _write(path, "# top\nskills:\n  - old\n  - older\ninputs: []\n")
            crys.rewrite_skills_list(path, ["new"])
            self.assertEqual("# top\nskills:\n  - new\ninputs: []\n",
                             path.read_text(encoding="utf-8"))

    def test_removal_to_empty_renders_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.yaml"
            _write(path, "skills:\n  - old\ninputs: []\n")
            crys.rewrite_skills_list(path, [])
            self.assertEqual("skills: []\ninputs: []\n",
                             path.read_text(encoding="utf-8"))

    def test_missing_key_inserts_after_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.yaml"
            _write(path, "name: a\nrules: AGENTS.md\noutput_schema: null\n")
            crys.rewrite_skills_list(path, ["s"])
            self.assertEqual("name: a\nrules: AGENTS.md\nskills:\n  - s\n"
                             "output_schema: null\n",
                             path.read_text(encoding="utf-8"))

    def test_result_stays_valid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.yaml"
            _write(path, "name: a\nskills: []\n")
            crys.rewrite_skills_list(path, ["s"])
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(["s"], data["skills"])


class HookTests(unittest.TestCase):
    def test_has_drafts_without_creating_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "work"
            self.assertFalse(crys.has_drafts("tid-1", base_dir=base))
            self.assertFalse(base.exists())
            target = base / "tid-1" / crys.DRAFTS_DIRNAME / "s"
            target.mkdir(parents=True)
            _write(target / "SKILL.md", "x")
            self.assertTrue(crys.has_drafts("tid-1", base_dir=base))

    def test_has_drafts_rejects_bad_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(crys.has_drafts("../x", base_dir=Path(tmp)))

    def test_mirror_ticket_reads_erpnext_name(self) -> None:
        task = {"params": {"_mirrors": {"erpnext": {"name": TICKET}}}}
        self.assertEqual(TICKET, crys.mirror_ticket(task))
        self.assertIsNone(crys.mirror_ticket({"params": {}}))
        self.assertIsNone(crys.mirror_ticket({}))
        weird = {"params": {"_mirrors": {"erpnext": {"name": "nope"}}}}
        self.assertIsNone(crys.mirror_ticket(weird))

    def test_ticket_dir_does_not_create(self) -> None:
        from orchestration.agent_sandbox import ticket_dir
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "work"
            self.assertEqual(base / "abc-1", ticket_dir("abc-1",
                                                       base_dir=base))
            self.assertFalse(base.exists())
            with self.assertRaises(ValueError):
                ticket_dir("../x", base_dir=base)


class PromptTests(unittest.TestCase):
    def test_generic_prompt_carries_draft_instruction(self) -> None:
        from catalog import load_role_profile
        from orchestration.generic_agent import build_agent_prompt
        prompt = build_agent_prompt(
            load_role_profile("operations"),
            {"ticket_number": 1, "task_id": "t", "title": "Do",
             "description": "Work", "params": {}})
        self.assertIn(crys.DRAFTS_DIRNAME, prompt)
        self.assertIn("borrador", prompt)

    def test_executor_cards_carry_draft_line(self) -> None:
        from catalog import load_role_profile
        from orchestration.role_muse import render_card
        for name in ("operations", "finance", "research", "secretary"):
            with self.subTest(role=name):
                self.assertIn("draft", render_card(
                    load_role_profile(name)))

    def test_dev_planner_ceo_cards_untouched(self) -> None:
        from catalog import load_role_profile
        from orchestration.role_muse import render_card
        for name in ("dev", "planner", "ceo"):
            with self.subTest(role=name):
                self.assertNotIn("draft", render_card(
                    load_role_profile(name)))


class RealToolsTests(unittest.TestCase):
    def _require_tools(self) -> None:
        from orchestration.agent_sandbox import sandbox_available
        if shutil.which("muse") is None:
            self.skipTest("no muse binary")
        if not sandbox_available():
            self.skipTest("no bwrap sandbox")

    def test_real_validate_and_fenced_tests_apply(self) -> None:
        self._require_tools()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "coded", scripts={
                "scripts/helper.py": "VALUE = 41\n",
                "scripts/test_helper.py": (
                    "import helper\n\n"
                    "def test_value():\n"
                    "    assert helper.VALUE == 41\n"),
            })
            out = crys.run_save_step(run_config(
                repo, drafts, validate=None, run_skill_tests=None))
            self.assertEqual(["coded"], out.created)
            self.assertIsNotNone(out.commit)

    def test_real_fenced_tests_reject_red_skill(self) -> None:
        self._require_tools()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, repo = make_origin(root)
            drafts = root / "ticket" / "skill_drafts"
            write_draft(drafts, "red", scripts={
                "scripts/helper.py": "VALUE = 41\n",
                "scripts/test_helper.py": (
                    "import helper\n\n"
                    "def test_value():\n"
                    "    assert helper.VALUE == 42\n"),
            })
            out = crys.run_save_step(run_config(
                repo, drafts, validate=None, run_skill_tests=None))
            self.assertIn("red", out.discarded)
            self.assertIsNone(out.commit)


if __name__ == "__main__":
    unittest.main()
