"""Contract: repo paths cited in CLAUDE.md and normative docs exist (TASK-2026-00140).

Every agent reads CLAUDE.md, so a citation to a file that is not there sends
work down a dead end. This test scans the backticked spans of CLAUDE.md plus
the normative docs and fails on any cited repo path that resolves to nothing.

Deliberately narrow so it stays cheap and stable:
- STATUS.md is history and is never scanned.
- Only multi-segment spans whose last segment has a dot (``a/b.py``) or that
  end in ``/`` (dirs) count as path citations. Single-segment spans
  (``main.py``), tier shorthands (``tier_s/a/b/c``) and method/intent
  shorthands are ignored: too ambiguous to resolve.
- URLs, absolute paths, ``{placeholders}``, ``<params>`` and spans with
  whitespace or ``*`` are not path citations.
- ``docs/work/`` and ``docs/reviews/`` are temporal gitignored trees: their
  presence in a checkout means nothing, so citations there are skipped.
- ``decisions/``, ``vault/``, ``jardal-knowledge/`` and ``projects/jardal/``
  live in the engineering-memory vault, outside this repo.
- Docs use rooted shorthands (``horizon/cockpit.py``, ``ceo/x.yaml``,
  ``orchestration/inbox.py``); each span is tried against the repo root plus the
  known shorthand roots before it is reported missing.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DOCS = [
    "CLAUDE.md",
    "docs/ARCHITECTURE_REFERENCE_2026.md",
    "openceo-core/objectives/routing_policy.md",
    "docs/decision-2026-10-05-planner-objetivos-muse.md",
]

#: Prefixes that never resolve inside this repo (temporal trees, vault).
EXCLUDED_PREFIXES = (
    "docs/work/",
    "docs/reviews/",
    "decisions/",
    "vault/",
    "jardal-knowledge/",
    "projects/jardal/",
)

#: Roots tried in order for shorthand citations ("" = repo root).
SHORTHAND_ROOTS = (
    "",
    "openceo-core/",
    "workflows/",
    "openceo-core/agents/dev/",
)

_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINE_REF_RE = re.compile(r":\d+.*$")


def cited_path(span: str) -> str | None:
    """Return the repo-relative path a backticked span cites, or None."""
    s = span.strip()
    if "/" not in s:
        return None
    if "://" in s or s.startswith(("file:", "/", "~")):
        return None
    if any(c in s for c in " \t<>*{}"):
        return None
    tail = s.rsplit("/", 1)[1]
    if not s.endswith("/") and "." not in tail:
        return None
    path = _LINE_REF_RE.sub("", s).rstrip("/").rstrip(".,:)")
    if not path or path.startswith("."):
        return None
    if path.startswith(EXCLUDED_PREFIXES):
        return None
    return path


def resolve(repo: Path, path: str) -> Path | None:
    """Resolve a cited path against the repo root and shorthand roots."""
    for root in SHORTHAND_ROOTS:
        candidate = repo / root / path
        if candidate.exists():
            return candidate
    return None


def scan_doc(repo: Path, doc: str) -> list[str]:
    """Return ``doc:line: span`` entries for citations that resolve nowhere."""
    missing: list[str] = []
    for lineno, line in enumerate((repo / doc).read_text().splitlines(), 1):
        for match in _SPAN_RE.finditer(line):
            path = cited_path(match.group(1))
            if path is not None and resolve(repo, path) is None:
                missing.append(f"{doc}:{lineno}: `{match.group(1).strip()}`")
    return missing


class DocsPathContractTest(unittest.TestCase):
    def test_normative_docs_exist(self):
        for doc in DOCS:
            self.assertTrue((REPO / doc).is_file(), f"normative doc gone: {doc}")

    def test_no_dead_cited_paths(self):
        missing: list[str] = []
        for doc in DOCS:
            missing.extend(scan_doc(REPO, doc))
        self.assertEqual([], missing,
                         "cited repo paths that do not exist:\n" + "\n".join(missing))


class CitedPathClassifierTest(unittest.TestCase):
    """The classifier itself: accept real citations, skip non-paths."""

    def test_full_paths_accepted(self):
        self.assertEqual("openceo-core/objectives/engine.py",
                         cited_path("openceo-core/objectives/engine.py"))
        self.assertEqual("openceo-core/ops",
                         cited_path("openceo-core/ops/"))

    def test_non_paths_skipped(self):
        for span in ("main.py", "tier_s/a/b/c",
                     "HorizonService.cockpit/company_status",
                     "horizon_brief/decisions/decide",
                     "projects/{name}/project.yaml",
                     "projects/<key>/project.yaml",
                     "objective/<id>",
                     "https://erp.jardal.app/app/task",
                     "/home/develop/.local/bin/musej",
                     "ops.* tools",
                     "a b/c"):
            self.assertIsNone(cited_path(span), span)

    def test_outside_repo_skipped(self):
        for span in ("decisions/2026-10-06-objetivo-unico-ceo-autonomo",
                     "vault/README.md",
                     "jardal-knowledge/vault/",
                     "projects/jardal/jardal-openceo",
                     "docs/work/TASK-2026-00056-investigator-elite.md"):
            self.assertIsNone(cited_path(span), span)

    def test_line_refs_stripped(self):
        self.assertEqual("openceo-core/objectives/engine.py",
                         cited_path("openceo-core/objectives/engine.py:12"))


class ShorthandRootsTest(unittest.TestCase):
    def test_shorthands_resolve(self):
        cases = {
            "horizon/cockpit.py": "openceo-core/horizon/cockpit.py",
            "tools/erpnext_tickets.py": "openceo-core/tools/erpnext_tickets.py",
            "db/mongo.py": "openceo-core/db/mongo.py",
            # TASK-2026-00119 added openceo-core/ceo/: bare "ceo" is
            # ambiguous (and never a real citation — cited_path needs
            # a slash), so each side pins a realistic span instead.
            "ceo/horizon_daily.yaml": "workflows/ceo/horizon_daily.yaml",
            "ceo/review.py": "openceo-core/ceo/review.py",
            # TASK-2026-00141 moved the review machinery out of tier_s.
            "tier_s/cycle.py": "openceo-core/agents/dev/tier_s/cycle.py",
            "orchestration/inbox.py": "openceo-core/orchestration/inbox.py",
        }
        for span, expected in cases.items():
            with self.subTest(span=span):
                found = resolve(REPO, span)
                self.assertIsNotNone(found, span)
                assert found is not None
                self.assertEqual(REPO / expected, found)

    def test_dead_path_resolves_nowhere(self):
        self.assertIsNone(resolve(REPO, "openceo-core/nope/missing.py"))


class ScanNotVacuousTest(unittest.TestCase):
    """Control: a doc with a dead citation must fail the scan."""

    def test_dead_citation_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "CLAUDE.md").write_text(
                "See `openceo-core/nope/missing.py` and `main.py`.\n")
            missing = scan_doc(repo, "CLAUDE.md")
        self.assertEqual(["CLAUDE.md:1: `openceo-core/nope/missing.py`"], missing)

    def test_clean_doc_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "CLAUDE.md").write_text(
                "See `main.py`, `tier_s/a/b/c` and `projects/{n}/project.yaml`.\n")
            self.assertEqual([], scan_doc(repo, "CLAUDE.md"))


if __name__ == "__main__":
    unittest.main()
