"""Role skills in git: standard format, own skills only, planner index.

TASK-2026-00203. Each agent owns versioned skills under
``skills/<role>/<skill>/SKILL.md`` (Agent Skills standard plus an OpenCEO
header: status, creation ticket, date). Role profiles load only their own
skills plus the Muse engine skills they use (``create-skill`` everywhere,
``git`` for dev); the catalog index lists each agent's skills for the
planner, and a suggested plan skill must name a real one (active or draft).

No network, no LLM. The ``muse skills validate`` gate shells to the pinned
binary (skipped when it is missing); the echo acceptance lives in the 00197
smoke test, which asserts each role loads exactly its allowed skills.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
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
    ENGINE_SKILLS,
    CatalogError,
    check_skill_links,
    check_skill_names_unique,
    discover_role_skills,
    load_all_role_skills,
    load_catalog,
    load_role_skills,
    parse_role_skill,
    skills_index_for_planner,
    suggestable_skills,
    sync_skill_links,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _agents_section, _planner_skills  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    DevTier,
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)


def write_skill(parent: Path, name: str, *,
                role: str = "dev",
                description: str = "Does role work.",
                status: str = "borrador",
                ticket: str = "TASK-2026-00203",
                day: str = "2026-10-12",
                body: str = "# Skill\n",
                extra_header: str = "") -> Path:
    """Write ``parent/<name>/SKILL.md`` and return the skill dir."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True)
    header = (f"name: {name}\ndescription: {description}\nstatus: {status}\n"
              f"ticket: {ticket}\ndate: {day}\n{extra_header}")
    (skill_dir / "SKILL.md").write_text(
        f"---\n{header}---\n\n{body}", encoding="utf-8")
    assert role  # role is the directory name, kept for readability
    return skill_dir


def write_catalog(root: Path, cards: dict[str, dict[str, Any]],
                  profiles: dict[str, dict[str, Any]] | None = None) -> Path:
    """Minimal catalog tree (dev + owner) with optional profiles."""
    base = {"type": "ai", "engine": "muse-code", "profile": None,
            "boundary_tools": [], "inputs": [], "outputs": [],
            "enabled": True, "disabled_reason": None,
            "decided_by_ticket": None}
    catalog = root / "catalog"
    agents = catalog / "agents"
    agents.mkdir(parents=True)
    entries = []
    for name, overrides in cards.items():
        card = dict(base, name=name, skills_dir=f"skills/{name}",
                    skills=[])
        card.update(overrides)
        (agents / f"{name}.yaml").write_text(
            yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
        entries.append({"name": name, "summary": f"{name} summary.",
                        "card": f"agents/{name}.yaml"})
    (catalog / "index.yaml").write_text(
        yaml.safe_dump({"version": 1, "agents": entries}, sort_keys=False),
        encoding="utf-8")
    (catalog / "staff.yaml").write_text(
        yaml.safe_dump({"roster": []}, sort_keys=False), encoding="utf-8")
    if profiles:
        prof_dir = catalog / "profiles"
        prof_dir.mkdir()
        for name, spec in profiles.items():
            (prof_dir / f"{name}.yaml").write_text(
                yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return catalog


def link_skill(root: Path, role: str, name: str) -> None:
    """Expose a fixture skill as muse would see it (project symlink)."""
    exposed = root / ".codex" / "skills"
    exposed.mkdir(parents=True, exist_ok=True)
    (exposed / name).symlink_to(f"../../skills/{role}/{name}")


def dev_profile(skills: list[str]) -> dict[str, Any]:
    return {"name": "dev", "agent": "dev", "engine": "muse-code",
            "card": {"who": "w", "delivers": ["d"], "never": ["n"]},
            "rules": "AGENTS.md", "skills": skills,
            "output_schema": "openceo-core/objectives/agent_result.schema.json"}


def make_ticket(target: str, **overrides: Any) -> PlannedTicket:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Do work", "target_agent": target,
        "work_type": "general", "goal": "g", "why_now": "now",
        "acceptance_criteria": ["done"], "expected_artifacts": ["report"],
    }
    if target == "dev":
        fields.update(coding_tool=DevTier.TIER_C, tier_reason="simple")
    fields.update(overrides)
    return PlannedTicket(**fields)


def validate_skill(target: str, skill: str | None,
                   skills: dict[str, list[str]] | None) -> list[str]:
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[make_ticket(target, skill=skill)]
                           if skill else [make_ticket(target)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents={target: {"enabled": True}}, open_titles=[],
            task_exists=_exists, skills=skills)

    return asyncio.run(_run())


class SkillFrontmatterTest(unittest.TestCase):
    def test_valid_borrador_and_activa_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "triage", role="dev", status="borrador")
            write_skill(root, "deploy", role="dev", status="activa")
            borrador = parse_role_skill(root / "triage" / "SKILL.md", "dev")
            activa = parse_role_skill(root / "deploy" / "SKILL.md", "dev")
        self.assertEqual("triage", borrador.name)
        self.assertEqual("borrador", borrador.status)
        self.assertEqual("TASK-2026-00203", borrador.ticket)
        self.assertEqual(date(2026, 10, 12), borrador.date)
        self.assertEqual("Does role work.", borrador.description)
        self.assertEqual("activa", activa.status)

    def test_missing_file_names_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CatalogError, "no-such"):
                parse_role_skill(Path(tmp) / "no-such" / "SKILL.md", "dev")

    def test_name_must_match_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "triage", extra_header="name: other\n")
            with self.assertRaisesRegex(CatalogError, "must match"):
                parse_role_skill(root / "triage" / "SKILL.md", "dev")

    def test_bad_header_fields_rejected(self) -> None:
        cases = [
            ("status: shipped\n", "status"),
            ("ticket: nope\n", "ticket"),
            ("date: 12-10-2026\n", "date"),
            ("description: ''\n", "description"),
        ]
        for extra, marker in cases:
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "triage").mkdir()
                    (root / "triage" / "SKILL.md").write_text(
                        "---\nname: triage\ndescription: d\nstatus: borrador\n"
                        f"ticket: TASK-2026-00203\ndate: 2026-10-12\n{extra}"
                        "---\n\n# t\n", encoding="utf-8")
                    with self.assertRaisesRegex(CatalogError, marker):
                        parse_role_skill(root / "triage" / "SKILL.md", "dev")

    def test_missing_frontmatter_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# No header\n",
                                                encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "frontmatter"):
                parse_role_skill(skill_dir / "SKILL.md", "dev")


class LoadRoleSkillsTest(unittest.TestCase):
    def test_missing_dir_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                [], load_role_skills("dev", "skills/dev", Path(tmp)))

    def test_skills_sorted_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "skills" / "dev"
            write_skill(role_dir, "zebra")
            write_skill(role_dir, "apple")
            names = [skill.name
                     for skill in load_role_skills("dev", "skills/dev", root)]
        self.assertEqual(["apple", "zebra"], names)

    def test_every_problem_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "skills" / "dev"
            (role_dir / "empty-dir").mkdir(parents=True)
            write_skill(role_dir, "bad", status="shipped")
            try:
                load_role_skills("dev", "skills/dev", root)
            except CatalogError as exc:
                self.assertIn("bad", str(exc))
                self.assertIn("empty-dir", str(exc))
            else:
                self.fail("expected CatalogError")

    def test_stray_files_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "skills" / "dev"
            role_dir.mkdir(parents=True)
            (role_dir / "notes.txt").write_text("scratch", encoding="utf-8")
            self.assertEqual(
                [], load_role_skills("dev", "skills/dev", root))

    def test_script_without_test_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = write_skill(root / "skills" / "dev", "triage")
            scripts = skill_dir / "scripts"
            scripts.mkdir()
            (scripts / "check.py").write_text("print('ok')\n",
                                              encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "test_check"):
                load_role_skills("dev", "skills/dev", root)

    def test_script_with_sibling_test_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = write_skill(root / "skills" / "dev", "triage")
            scripts = skill_dir / "scripts"
            scripts.mkdir()
            (scripts / "check.py").write_text("print('ok')\n",
                                              encoding="utf-8")
            (scripts / "test_check.py").write_text("def test_x(): pass\n",
                                                   encoding="utf-8")
            (skill_dir / "references").mkdir()
            (skill_dir / "references" / "notes.md").write_text(
                "ref\n", encoding="utf-8")
            skills = load_role_skills("dev", "skills/dev", root)
        self.assertEqual(["triage"], [skill.name for skill in skills])


class CatalogSkillsCrossCheckTest(unittest.TestCase):
    def test_card_lists_what_is_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            base = write_catalog(root, {"dev": {"skills": ["triage"]},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            catalog = load_catalog(base)
        self.assertEqual(["triage"], catalog.agents["dev"].skills)

    def test_card_skill_missing_on_disk_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = write_catalog(root, {"dev": {"skills": ["ghost"]},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            with self.assertRaisesRegex(CatalogError, "ghost"):
                load_catalog(base)

    def test_disk_skill_missing_on_card_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "orphan")
            base = write_catalog(root, {"dev": {"skills": []},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            with self.assertRaisesRegex(CatalogError, "orphan"):
                load_catalog(base)

    def test_human_card_exempt_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = write_catalog(root, {"dev": {"skills": []},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            catalog = load_catalog(base)
        self.assertEqual(4, len(catalog.agents["owner"].skills))

    def test_profile_may_allow_engine_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            base = write_catalog(
                root, {"dev": {"skills": ["triage"],
                               "profile": "profiles/dev.yaml"}},
                {"dev": dev_profile(["triage", *ENGINE_SKILLS])})
            catalog = load_catalog(base)
        self.assertEqual(["triage"], catalog.agents["dev"].skills)

    def test_profile_foreign_skill_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = write_catalog(
                root, {"dev": {"skills": [],
                               "profile": "profiles/dev.yaml"}},
                {"dev": dev_profile(["nope"])})
            with self.assertRaisesRegex(CatalogError, "not on the card"):
                load_catalog(base)

    def test_card_skill_missing_from_profile_rejected(self) -> None:
        """Review 00005/F2: the profile must load every card skill."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            base = write_catalog(
                root, {"dev": {"skills": ["triage"],
                               "profile": "profiles/dev.yaml"}},
                {"dev": dev_profile(["create-skill"])})
            with self.assertRaisesRegex(CatalogError, "not loaded"):
                load_catalog(base)

    def test_unlinked_profile_limited_to_engine_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = write_catalog(
                root, {"dev": {"skills": []}},
                {"planner": {"name": "planner", "agent": "planner",
                             "engine": "muse-code",
                             "card": {"who": "w", "delivers": ["d"],
                                      "never": ["n"]},
                             "rules": "AGENTS.md",
                             "skills": ["create-skill"]}})
            load_catalog(base)
            prof = base / "profiles" / "planner.yaml"
            prof.write_text(prof.read_text(encoding="utf-8").replace(
                "create-skill", "frobnicate"), encoding="utf-8")
            with self.assertRaisesRegex(CatalogError, "engine skill"):
                load_catalog(base)

    def test_engine_skill_name_refused_for_role_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "git")
            base = write_catalog(root, {"dev": {"skills": ["git"]},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            with self.assertRaisesRegex(CatalogError, "engine skill"):
                load_catalog(base)

    def test_duplicate_skill_name_across_roles_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            write_skill(root / "skills" / "ops", "triage")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root),
                        "ops": load_role_skills("ops", "skills/ops", root)}
            problems = check_skill_names_unique(by_agent)
        self.assertEqual(1, len(problems))
        self.assertIn("triage", problems[0])


class SkillLinksTest(unittest.TestCase):
    """Every role skill is exposed once via ``.codex/skills/`` (a symlink).

    Muse discovers project skills only there: the symlink is the exposure,
    ``skills/<role>/`` stays the single source. Legacy real directories
    (``notion-api-local``) are not role skills and stay untouched.
    """

    def _by_agent(self, root: Path, role: str = "dev") -> dict[str, Any]:
        return {role: load_role_skills(role, f"skills/{role}", root)}

    def test_no_skills_no_links_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], check_skill_links({}, Path(tmp)))

    def test_correct_symlink_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            self.assertEqual(
                [], check_skill_links(self._by_agent(root), root))

    def test_missing_symlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            problems = check_skill_links(self._by_agent(root), root)
        self.assertEqual(1, len(problems))
        self.assertIn("triage", problems[0])

    def test_dangling_symlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            exposed = root / ".codex" / "skills"
            exposed.mkdir(parents=True)
            (exposed / "triage").symlink_to("../../skills/dev/gone")
            problems = check_skill_links(self._by_agent(root), root)
        self.assertTrue(problems)
        self.assertIn("triage", problems[0])

    def test_symlink_pointing_elsewhere_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            (root / "stray").mkdir()
            (root / ".codex" / "skills" / "stray").symlink_to(
                "../../stray")
            problems = check_skill_links(self._by_agent(root), root)
        self.assertEqual(1, len(problems))
        self.assertIn("stray", problems[0])

    def test_legacy_real_directory_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / ".codex" / "skills" / "notion-api-local"
            legacy.mkdir(parents=True)
            (legacy / "SKILL.md").write_text("# legacy\n", encoding="utf-8")
            self.assertEqual(
                [], check_skill_links(self._by_agent(root), root))


class SyncSkillLinksTest(unittest.TestCase):
    """Review 00005/F1: exposure links are local state, recreated by sync."""

    def test_creates_missing_link_with_relative_target(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root)}
            created = sync_skill_links(by_agent, root)
            link = root / ".codex" / "skills" / "triage"
            self.assertEqual([".codex/skills/triage"], created)
            self.assertTrue(link.is_symlink())
            self.assertFalse(Path(os.readlink(link)).is_absolute())
            self.assertEqual([], check_skill_links(by_agent, root))

    def test_leaves_existing_link_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root)}
            self.assertEqual([], sync_skill_links(by_agent, root))
            self.assertEqual([], check_skill_links(by_agent, root))

    def test_leaves_dangling_link_for_the_checker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            exposed = root / ".codex" / "skills"
            exposed.mkdir(parents=True)
            (exposed / "triage").symlink_to("../../skills/dev/gone")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root)}
            self.assertEqual([], sync_skill_links(by_agent, root))
            self.assertTrue(check_skill_links(by_agent, root))

    def test_empty_map_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([], sync_skill_links({}, root))
            self.assertFalse((root / ".codex").exists())


class DiscoverRoleSkillsTest(unittest.TestCase):
    """Review 00006/W2: recovery scan without loading the catalog."""

    def test_discovers_skills_and_recovers_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            write_skill(root / "skills" / "operations", "probe")
            found = discover_role_skills(root)
            self.assertEqual({"dev", "operations"}, set(found))
            self.assertEqual(["triage"],
                             [skill.name for skill in found["dev"]])
            created = sync_skill_links(found, root)
            self.assertEqual({".codex/skills/triage", ".codex/skills/probe"},
                             set(created))
            self.assertEqual([], check_skill_links(found, root))
            self.assertEqual([], sync_skill_links(found, root))

    def test_missing_skills_dir_discovers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, discover_role_skills(Path(tmp)))

    def test_skips_stray_files_and_hidden_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            (root / "skills" / "README.md").write_text("docs\n",
                                                      encoding="utf-8")
            (root / "skills" / ".hidden").mkdir()
            self.assertEqual({}, discover_role_skills(root))


class SkillsIndexTest(unittest.TestCase):
    def test_empty_index_is_empty(self) -> None:
        self.assertEqual("", skills_index_for_planner({}))
        self.assertEqual("", skills_index_for_planner({"dev": []}))

    def test_lists_name_description_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage",
                        description="Sorts inbox findings.",
                        status="activa")
            write_skill(root / "skills" / "dev", "spike",
                        description="Throwaway probe.", status="borrador")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root)}
            index = skills_index_for_planner(by_agent)
        self.assertIn("triage", index)
        self.assertIn("Sorts inbox findings.", index)
        self.assertIn("activa", index)
        self.assertIn("spike", index)
        self.assertIn("borrador", index)

    def test_agents_without_skills_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            by_agent = {"dev": load_role_skills("dev", "skills/dev", root),
                        "ops": []}
            index = skills_index_for_planner(by_agent)
        self.assertIn("dev", index)
        self.assertNotIn("ops", index)


class SuggestSkillValidationTest(unittest.TestCase):
    SKILLS = {"dev": ["triage"], "owner": ["aprobar"]}

    def test_unknown_skill_rejected(self) -> None:
        errors = validate_skill("dev", "frobnicate", self.SKILLS)
        self.assertTrue(any("frobnicate" in line and "dev" in line
                            for line in errors),
                        errors)

    def test_known_skill_accepted(self) -> None:
        self.assertEqual([], validate_skill("dev", "triage", self.SKILLS))

    def test_no_suggestion_needs_no_skills(self) -> None:
        self.assertEqual([], validate_skill("dev", None, self.SKILLS))
        self.assertEqual([], validate_skill("dev", None, None))

    def test_unknown_agent_skipped(self) -> None:
        self.assertEqual([], validate_skill("Ana", "triage", self.SKILLS))

    def test_missing_map_skipped_for_compatibility(self) -> None:
        self.assertEqual([], validate_skill("dev", "triage", None))
        self.assertEqual([], validate_skill("dev", "frobnicate", None))

    def test_owner_skill_validated_against_card(self) -> None:
        self.assertEqual([], validate_skill("owner", "aprobar", self.SKILLS))
        errors = validate_skill("owner", "frobnicate", self.SKILLS)
        self.assertTrue(any("frobnicate" in line for line in errors), errors)

    def test_suggestable_skills_come_from_disk_and_owner_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root / "skills" / "dev", "triage")
            link_skill(root, "dev", "triage")
            base = write_catalog(root, {"dev": {"skills": ["triage"]},
                                        "owner": {"type": "human",
                                                  "engine": None,
                                                  "skills": ["decidir",
                                                             "aprobar",
                                                             "responder",
                                                             "hacer"]}})
            catalog = load_catalog(base)
            by_agent = load_all_role_skills(catalog, root)
            mapping = suggestable_skills(catalog, by_agent)
        self.assertEqual(["triage"], mapping["dev"])
        self.assertEqual(["decidir", "aprobar", "responder", "hacer"],
                         mapping["owner"])


class PlannerSkillsLoadTest(unittest.TestCase):
    """Review 00005/F3: fresh load per cycle, logged fail-open."""

    def test_loads_real_catalog_with_first_skill(self) -> None:
        block, mapping = _planner_skills()
        self.assertIn("crear-proyecto", block)
        self.assertEqual(["crear-proyecto"], mapping["operations"])
        self.assertEqual([], mapping["dev"])
        self.assertEqual(["decidir", "aprobar", "responder", "hacer"],
                         mapping["owner"])

    def test_load_failure_warns_and_skips(self) -> None:
        from unittest import mock
        from catalog import CatalogError
        with mock.patch("objectives.engine.load_catalog",
                        side_effect=CatalogError("boom")):
            with self.assertLogs("objectives.engine", level="WARNING") as logs:
                self.assertEqual(("", {}), _planner_skills())
        self.assertTrue(any("skipping skill check" in line
                            for line in logs.output))


class AgentsSectionTest(unittest.TestCase):
    def test_without_skills_unchanged(self) -> None:
        section = _agents_section({"dev": {"enabled": True}})
        self.assertIn("REGISTERED AGENTS", section)
        self.assertNotIn("SKILL", section)

    def test_skills_block_appended(self) -> None:
        section = _agents_section({"dev": {"enabled": True}},
                                  skills_md="- dev: triage — sorts.")
        self.assertIn("REGISTERED AGENTS", section)
        self.assertIn("triage", section)


class RealRepoTest(unittest.TestCase):
    """The shipped tree honors the contract (first skill live)."""

    def test_real_catalog_loads(self) -> None:
        catalog = load_catalog()
        self.assertIn("dev", catalog.agents)

    def test_real_profiles_allow_engine_skills(self) -> None:
        from catalog import load_role_profile
        for name in ("dev", "planner", "ceo", "operations", "secretary",
                     "finance", "research"):
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                self.assertIn("create-skill", profile.skills)
        dev = load_role_profile("dev")
        self.assertIn("git", dev.skills)

    def test_real_skills_index_has_first_skill(self) -> None:
        catalog = load_catalog()
        by_agent = load_all_role_skills(catalog)
        # TASK-2026-00220 added the second shipped skill.
        self.assertEqual({"operations": ["crear-proyecto"],
                          "secretary": ["enviar-correo"]},
                         {key: [skill.name for skill in value]
                          for key, value in by_agent.items() if value})
        self.assertIn("crear-proyecto",
                      skills_index_for_planner(by_agent))

    def test_real_skills_pass_muse_validate(self) -> None:
        import os
        import subprocess
        binary = shutil.which("muse")
        if binary is None:
            self.skipTest("no muse binary")
        env = {**os.environ, "MUSE_NO_AUTO_UPDATE": "1"}
        found = sorted((REPO / "skills").glob("*/")) if (
            REPO / "skills").is_dir() else []
        for role_dir in found:
            for skill_dir in sorted(role_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.is_symlink():
                    continue
                with self.subTest(skill=str(skill_dir.relative_to(REPO))):
                    proc = subprocess.run(
                        [binary, "skills", "validate", str(skill_dir)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, errors="replace", timeout=120, env=env)
                    self.assertEqual(
                        0, proc.returncode,
                        f"muse skills validate failed: {proc.stdout[:500]}")

    def test_validate_gate_is_live(self) -> None:
        import os
        import subprocess
        binary = shutil.which("muse")
        if binary is None:
            self.skipTest("no muse binary")
        env = {**os.environ, "MUSE_NO_AUTO_UPDATE": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = write_skill(root, "gate-good")
            proc = subprocess.run(
                [binary, "skills", "validate", str(good)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=120, env=env)
            self.assertEqual(0, proc.returncode, proc.stdout[:300])
            bad = root / "gate-bad"
            bad.mkdir()
            (bad / "SKILL.md").write_text("# No header\n", encoding="utf-8")
            proc = subprocess.run(
                [binary, "skills", "validate", str(bad)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=120, env=env)
            self.assertNotEqual(0, proc.returncode)


if __name__ == "__main__":
    unittest.main()
