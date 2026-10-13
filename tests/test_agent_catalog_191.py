"""Single agent catalog: format, loader and validation (TASK-2026-00191).

The catalog (``catalog/`` in git) is the only source of who exists in the
company and what each one can do. The loader (``openceo-core/catalog.py``)
validates with Pydantic and fails closed: an invalid catalog refuses to
load, and the service refuses to start (``main.py`` lifespan gate).

No network, no CLI, no LLM.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    CATALOG_VERSION,
    INDEX_SUMMARY_MAX_CHARS,
    OWNER_SKILLS,
    Catalog,
    CatalogError,
    check_catalog,
    catalog_fingerprint,
    load_catalog,
    short_index,
)

DEV_CARD: dict[str, Any] = {
    "name": "dev",
    "type": "ai",
    "engine": "muse-code",
    "profile": None,
    "boundary_tools": [],
    "skills_dir": "skills/dev",
    "skills": [],
    "inputs": ["ticket with repo and acceptance criteria"],
    "outputs": ["branch changes", "hecho/bloqueado/no_aplica result"],
    "enabled": True,
    "disabled_reason": None,
    "decided_by_ticket": None,
}

OWNER_CARD: dict[str, Any] = {
    "name": "owner",
    "type": "human",
    "engine": None,
    "profile": None,
    "boundary_tools": [],
    "skills_dir": "skills/owner",
    "skills": ["decidir", "aprobar", "responder", "hacer"],
    "inputs": ["escalation with a nameable human action"],
    "outputs": ["decision"],
    "enabled": True,
    "disabled_reason": None,
    "decided_by_ticket": None,
}

PM_CARD: dict[str, Any] = {
    "name": "pm",
    "type": "ai",
    "engine": "muse-code",
    "profile": None,
    "boundary_tools": [],
    "skills_dir": "skills/pm",
    "skills": [],
    "inputs": [],
    "outputs": [],
    "enabled": False,
    "disabled_reason": "retired: ticket creation moves to the single planner",
    "decided_by_ticket": "TASK-2026-00209",
}

SUMMARIES = {
    "dev": "Writes, deploys and maintains repos. Never decides business work.",
    "owner": "Decides, approves, answers and does. Never receives plans.",
    "pm": "Extracted tickets from interviews. Retired, never runs.",
}


def write_catalog(root: Path, cards: dict[str, dict[str, Any]],
                  summaries: dict[str, str] | None = None,
                  staff: dict[str, Any] | None = None,
                  version: Any = 1) -> Path:
    """Write a catalog tree under ``root`` and return its path."""
    catalog = root / "catalog"
    agents = catalog / "agents"
    agents.mkdir(parents=True)
    entries = []
    for name, card in cards.items():
        (agents / f"{name}.yaml").write_text(
            yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
        entries.append({
            "name": name,
            "summary": (summaries or SUMMARIES)[name],
            "card": f"agents/{name}.yaml",
        })
    (catalog / "index.yaml").write_text(
        yaml.safe_dump({"version": version, "agents": entries},
                       sort_keys=False),
        encoding="utf-8")
    (catalog / "staff.yaml").write_text(
        yaml.safe_dump({"roster": []} if staff is None else staff,
                       sort_keys=False),
        encoding="utf-8")
    return catalog


def valid_cards() -> dict[str, dict[str, Any]]:
    return {"dev": dict(DEV_CARD), "owner": dict(OWNER_CARD),
            "pm": dict(PM_CARD)}


class RealCatalogTest(unittest.TestCase):
    """The shipped catalog loads; the enabled set grows per migration."""

    def test_loads(self):
        catalog = load_catalog()
        self.assertEqual(CATALOG_VERSION, catalog.version)
        self.assertEqual(
            {"dev", "owner", "operations", "secretary", "finance",
             "research", "pm", "marketing", "sales", "media"},
            set(catalog.agents))

    def test_enabled_set(self):
        catalog = load_catalog()
        enabled = {name for name, card in catalog.agents.items()
                   if card.enabled}
        # TASK-2026-00220 enabled secretary, TASK-2026-00221 finance,
        # TASK-2026-00222 research.
        self.assertEqual({"dev", "owner", "operations", "secretary",
                          "finance", "research"},
                         enabled)

    def test_owner_card(self):
        owner = load_catalog().agents["owner"]
        self.assertEqual("human", owner.type.value)
        self.assertIsNone(owner.engine)
        self.assertEqual(set(OWNER_SKILLS), set(owner.skills))

    def test_disabled_cards_name_reason_and_ticket(self):
        catalog = load_catalog()
        for name, card in catalog.agents.items():
            if card.enabled:
                continue
            with self.subTest(agent=name):
                self.assertTrue(card.disabled_reason)
                self.assertRegex(card.decided_by_ticket or "",
                                 r"^TASK-2026-\d+$")

    def test_ceo_and_planner_are_not_executors(self):
        catalog = load_catalog()
        self.assertNotIn("ceo", catalog.agents)
        self.assertNotIn("planner", catalog.agents)

    def test_staff_roster_empty(self):
        self.assertEqual([], load_catalog().staff.roster)

    def test_short_index_is_few_lines(self):
        rendered = short_index(load_catalog()).splitlines()
        self.assertEqual(10, len(rendered))
        for line in rendered:
            name, _, summary = line.partition(": ")
            self.assertTrue(name and summary)
            self.assertLessEqual(len(summary), INDEX_SUMMARY_MAX_CHARS)

    def test_fingerprint_is_stable_short_hex(self):
        sha = catalog_fingerprint(load_catalog())
        self.assertRegex(sha, r"^[0-9a-f]{12}$")
        self.assertEqual(sha, catalog_fingerprint(load_catalog()))

    def test_check_catalog_ok_on_real_tree(self):
        status = check_catalog()
        self.assertTrue(status.ok, status.problem)
        self.assertEqual(10, status.agents)
        self.assertRegex(status.sha, r"^[0-9a-f]{12}$")


class LoaderValidationTest(unittest.TestCase):
    """Every invalid shape fails closed with the file named."""

    def load_tmp(self, cards: dict[str, dict[str, Any]],
                 **kwargs: Any) -> Catalog:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_catalog(Path(tmp), cards, **kwargs)
            return load_catalog(root)

    def assert_invalid(self, cards: dict[str, dict[str, Any]],
                       needle: str, **kwargs: Any) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_catalog(Path(tmp), cards, **kwargs)
            with self.assertRaises(CatalogError) as ctx:
                load_catalog(root)
        self.assertIn(needle, str(ctx.exception))

    def test_valid_fixture_loads(self):
        catalog = self.load_tmp(valid_cards())
        self.assertEqual(["dev", "owner", "pm"], list(catalog.agents))

    def test_missing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError) as ctx:
                load_catalog(Path(tmp) / "catalog")
        self.assertIn("index.yaml", str(ctx.exception))

    def test_bad_version(self):
        self.assert_invalid(valid_cards(), "version", version=99)

    def test_missing_card_file(self):
        cards = valid_cards()
        del cards["pm"]
        with tempfile.TemporaryDirectory() as tmp:
            root = write_catalog(Path(tmp), cards)
            index = yaml.safe_load(
                (root / "index.yaml").read_text(encoding="utf-8"))
            index["agents"].append({"name": "pm", "summary": "x",
                                   "card": "agents/pm.yaml"})
            (root / "index.yaml").write_text(yaml.safe_dump(index),
                                             encoding="utf-8")
            with self.assertRaises(CatalogError) as ctx:
                load_catalog(root)
        self.assertIn("agents/pm.yaml", str(ctx.exception))

    def test_orphan_card_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_catalog(Path(tmp), valid_cards())
            (root / "agents" / "ghost.yaml").write_text(
                yaml.safe_dump(dict(PM_CARD, name="ghost")),
                encoding="utf-8")
            with self.assertRaises(CatalogError) as ctx:
                load_catalog(root)
        self.assertIn("ghost.yaml", str(ctx.exception))

    def test_card_name_must_match_index_and_filename(self):
        cards = valid_cards()
        cards["dev"]["name"] = "someone-else"
        self.assert_invalid(cards, "agents/dev.yaml")

    def test_disabled_needs_reason_and_ticket(self):
        cards = valid_cards()
        cards["pm"]["disabled_reason"] = None
        self.assert_invalid(cards, "disabled_reason")
        cards = valid_cards()
        cards["pm"]["decided_by_ticket"] = None
        self.assert_invalid(cards, "decided_by_ticket")

    def test_enabled_forbids_reason_and_ticket(self):
        cards = valid_cards()
        cards["dev"]["disabled_reason"] = "no reason to be here"
        self.assert_invalid(cards, "disabled_reason")
        cards = valid_cards()
        cards["dev"]["decided_by_ticket"] = "TASK-2026-00191"
        self.assert_invalid(cards, "decided_by_ticket")

    def test_ticket_pattern(self):
        cards = valid_cards()
        cards["pm"]["decided_by_ticket"] = "not-a-ticket"
        self.assert_invalid(cards, "decided_by_ticket")

    def test_engine_rules(self):
        cards = valid_cards()
        cards["dev"]["engine"] = None
        self.assert_invalid(cards, "engine")
        cards = valid_cards()
        cards["owner"]["engine"] = "muse-code"
        self.assert_invalid(cards, "engine")

    def test_owner_skills_exact(self):
        cards = valid_cards()
        cards["owner"]["skills"] = ["decidir", "aprobar", "responder"]
        self.assert_invalid(cards, "skills")
        cards = valid_cards()
        cards["owner"]["type"] = "ai"
        self.assert_invalid(cards, "owner")

    def test_reserved_names_rejected(self):
        cards = valid_cards()
        cards["ceo"] = dict(DEV_CARD, name="ceo")
        summaries = dict(SUMMARIES)
        summaries["ceo"] = "Reserved, never an executor."
        self.assert_invalid(cards, "ceo", summaries=summaries)

    def test_summary_single_line_bounded(self):
        cards = valid_cards()
        summaries = dict(SUMMARIES)
        summaries["dev"] = "two\nlines"
        self.assert_invalid(cards, "summary", summaries=summaries)
        summaries["dev"] = "x" * (INDEX_SUMMARY_MAX_CHARS + 1)
        self.assert_invalid(cards, "summary", summaries=summaries)

    def test_skills_dir_must_be_relative(self):
        cards = valid_cards()
        cards["dev"]["skills_dir"] = "/abs/path"
        self.assert_invalid(cards, "skills_dir")
        cards = valid_cards()
        cards["dev"]["skills_dir"] = "../escape"
        self.assert_invalid(cards, "skills_dir")

    def test_boundary_tools_unique_nonempty(self):
        cards = valid_cards()
        cards["dev"]["boundary_tools"] = ["tickets.comment",
                                          "tickets.comment"]
        self.assert_invalid(cards, "boundary_tools")

    def test_staff_dates_validated(self):
        cards = valid_cards()
        staff = {"roster": [{"name": "A", "role": "qa",
                             "projects": ["jardal-openceo"],
                             "active_from": "2026-10-20",
                             "active_until": "2026-10-11"}]}
        self.assert_invalid(cards, "staff.yaml", staff=staff)

    def test_broken_yaml_names_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_catalog(Path(tmp), valid_cards())
            (root / "agents" / "dev.yaml").write_text(
                "name: [unclosed\n", encoding="utf-8")
            with self.assertRaises(CatalogError) as ctx:
                load_catalog(root)
        self.assertIn("agents/dev.yaml", str(ctx.exception))

    def test_check_catalog_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = check_catalog(Path(tmp) / "catalog")
        self.assertFalse(status.ok)
        self.assertIn("index.yaml", status.problem)


class FingerprintTest(unittest.TestCase):
    def test_same_content_same_sha(self):
        with tempfile.TemporaryDirectory() as tmp1, \
                tempfile.TemporaryDirectory() as tmp2:
            sha1 = catalog_fingerprint(
                load_catalog(write_catalog(Path(tmp1), valid_cards())))
            sha2 = catalog_fingerprint(
                load_catalog(write_catalog(Path(tmp2), valid_cards())))
        self.assertEqual(sha1, sha2)

    def test_changed_content_changes_sha(self):
        with tempfile.TemporaryDirectory() as tmp1, \
                tempfile.TemporaryDirectory() as tmp2:
            before = catalog_fingerprint(
                load_catalog(write_catalog(Path(tmp1), valid_cards())))
            cards = valid_cards()
            cards["dev"]["outputs"] = ["something else"]
            after = catalog_fingerprint(
                load_catalog(write_catalog(Path(tmp2), cards)))
        self.assertNotEqual(before, after)


class ShortIndexTest(unittest.TestCase):
    def test_renders_name_summary_lines_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = load_catalog(
                write_catalog(Path(tmp), valid_cards()))
        self.assertEqual(
            [f"{name}: {SUMMARIES[name]}"
             for name in ("dev", "owner", "pm")],
            short_index(catalog).splitlines())


if __name__ == "__main__":
    unittest.main()
