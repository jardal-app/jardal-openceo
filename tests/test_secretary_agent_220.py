"""Secretary as a catalog agent: send-email skill, migrated notes (TASK-2026-00220).

Secretary leaves its fixed legacy session and graph node: the catalog card
is enabled with its ticket + email box, the enviar-correo recipe guides
drafting and the locked boundary tool sends. The Mongo notes collection
stops being a third memory: its tools leave the registry and stored notes
migrate (personal to Horizon capture, company to the vault or the named
ticket), verified with counts. No tokens.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    load_catalog,
    load_role_profile,
    load_role_skills,
)
from config_loader import _planner_agents_from_catalog  # noqa: E402
from graph import (  # noqa: E402
    WIRED_AGENTS,
    OpenCEOGraph,
    resolve_agent_gate,
)
from mcp_identity import (  # noqa: E402
    McpDenied,
    TicketGrant,
    TokenIssuer,
    TokenKind,
    box_for_agent,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _planner_skills  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)
from orchestration.generic_agent import (  # noqa: E402
    GenericAgentWiring,
    run_generic_agent,
)
from orchestration.live import ExecResult  # noqa: E402
from orchestration.notes_migration import (  # noqa: E402
    classify_note,
    migrate_notes,
    note_has_secret,
    ticket_tag_target,
    write_company_note,
)
from settings import Settings  # noqa: E402
from state import TaskStatus  # noqa: E402
from workflow_service import (  # noqa: E402
    MissingWorkflowToolError,
    WorkflowToolRegistry,
)

SKILL_DIR = REPO / "skills" / "secretary" / "enviar-correo"

SECRETARY_BOX = frozenset({
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_add_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_send_email",
})

MIRROR = {"_mirrors": {"erpnext": {"name": "TASK-2026-00220"}}}


def _validate_skill_live(target: str, skill: str | None,
                         skills: dict[str, list[str]] | None) -> list[str]:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Send the invoice email",
        "target_agent": target, "work_type": "secretary", "goal": "g",
        "why_now": "now", "acceptance_criteria": ["sent"],
        "expected_artifacts": ["sent email"],
    }
    if skill is not None:
        fields["skill"] = skill
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[PlannedTicket(**fields)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents={target: {"enabled": True}}, open_titles=[],
            task_exists=_exists, skills=skills)

    return asyncio.run(_run())


class SecretaryCardTest(unittest.TestCase):
    def test_secretary_enabled_with_box_and_skill(self) -> None:
        card = load_catalog().agents["secretary"]
        self.assertTrue(card.enabled)
        self.assertIsNone(card.disabled_reason)
        self.assertIsNone(card.decided_by_ticket)
        self.assertEqual(SECRETARY_BOX, set(card.boundary_tools))
        self.assertEqual(["enviar-correo"], card.skills)

    def test_profile_loads_card_skill(self) -> None:
        profile = load_role_profile("secretary")
        self.assertEqual({"create-skill", "enviar-correo"},
                         set(profile.skills))

    def test_index_summary_dropped_disabled(self) -> None:
        import yaml
        entries = yaml.safe_load(
            (REPO / "catalog" / "index.yaml").read_text(encoding="utf-8"))
        summary = next(item["summary"] for item in entries["agents"]
                       if item["name"] == "secretary")
        self.assertNotIn("Disabled", summary)


class SkillOnDiskTest(unittest.TestCase):
    def test_skill_parses_activa(self) -> None:
        found = load_role_skills("secretary", "skills/secretary")
        self.assertEqual(["enviar-correo"],
                         [skill.name for skill in found])
        skill = found[0]
        self.assertEqual("activa", skill.status)
        self.assertEqual("TASK-2026-00220", skill.ticket)

    def test_exposure_symlink_resolves_to_source(self) -> None:
        link = REPO / ".codex" / "skills" / "enviar-correo"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(SKILL_DIR),
                         os.path.realpath(link))

    def test_recipe_sends_through_the_locked_tool(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertIn("openceo_send_email", flat)
        self.assertIn("bloqueado", flat)

    def test_recipe_stays_small(self) -> None:
        size = (SKILL_DIR / "SKILL.md").stat().st_size
        self.assertLess(size, 4096, "the role prompt budget is shared")


class PlannerSkillLiveTest(unittest.TestCase):
    def test_planner_sees_secretary_skill(self) -> None:
        block, mapping = _planner_skills()
        self.assertIn("enviar-correo", block)
        self.assertEqual(["enviar-correo"], mapping["secretary"])

    def test_suggested_skill_validated_live(self) -> None:
        _block, mapping = _planner_skills()
        live = {name: list(names) for name, names in mapping.items()}
        self.assertEqual(
            [], _validate_skill_live("secretary", "enviar-correo", live))
        errors = _validate_skill_live("secretary", "frobnicate", live)
        self.assertTrue(any("frobnicate" in line for line in errors),
                        errors)

    def test_registry_enables_secretary(self) -> None:
        registry = _planner_agents_from_catalog(load_catalog())
        self.assertEqual({"enabled": True}, registry["secretary"])


class GateLiveTest(unittest.TestCase):
    def test_hint_proceeds(self) -> None:
        decision = resolve_agent_gate("secretary", {}, None,
                                      load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("secretary", decision.hint)

    def test_planner_target_proceeds(self) -> None:
        decision = resolve_agent_gate(
            None, {"target_agent": "secretary"}, None, load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("secretary", decision.hint)


class SecretaryBoxTest(unittest.TestCase):
    def _grant(self, tools: frozenset[str]) -> TicketGrant:
        return TicketGrant(kind=TokenKind.AGENT, agent="secretary",
                           ticket="TASK-2026-00220",
                           project="jardal-openceo", tools=tools)

    def test_box_comes_from_the_live_card(self) -> None:
        card = load_catalog().agents["secretary"]
        self.assertEqual(SECRETARY_BOX, box_for_agent(card))

    def test_send_binds_own_ticket_and_project(self) -> None:
        grant = self._grant(SECRETARY_BOX)
        grant.check("openceo_send_email", {
            "to": ["a@jardal.app"], "subject": "s", "body_text": "b",
            "ticket": "TASK-2026-00220", "project": "jardal-openceo"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_send_email", {
                "to": ["a@jardal.app"], "subject": "s", "body_text": "b",
                "ticket": "TASK-2026-00001", "project": "jardal-openceo"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_send_email", {
                "to": ["a@jardal.app"], "subject": "s", "body_text": "b"})

    def test_missing_tools_still_refused(self) -> None:
        grant = self._grant(frozenset({"openceo_get_ticket"}))
        with self.assertRaises(McpDenied):
            grant.check("openceo_send_email", {
                "to": ["a@jardal.app"], "subject": "s", "body_text": "b",
                "ticket": "TASK-2026-00220", "project": "jardal-openceo"})

    def test_ticket_tools_stay_ticket_bound(self) -> None:
        grant = self._grant(SECRETARY_BOX)
        grant.check("openceo_add_ticket_comment",
                    {"ticket": "TASK-2026-00220"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_add_ticket_comment",
                        {"ticket": "TASK-2026-00001"})

    def test_secretary_badge_mints(self) -> None:
        class _Store:
            async def save(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _mint() -> str:
            issuer = TokenIssuer(_Store(), load_catalog().agents)  # type: ignore[arg-type]
            return await issuer.issue_agent(
                "secretary", "TASK-2026-00220", "jardal-openceo")

        badge = asyncio.run(_mint())
        self.assertTrue(badge)


def _scripted_factory(recorded: list[dict[str, Any]],
                      stdout: str):
    result = ExecResult(returncode=0, elapsed_s=1.0,
                        output_tail=stdout, usage={})

    class _Scripted:
        async def run(self, role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            recorded.append({"role": role, "prompt": prompt,
                             "workdir": workdir})
            return result

        async def cleanup(self, role: str) -> None:
            recorded.append({"cleanup": role})

    def _factory(role: str, spec: dict[str, Any],
                 provider: Any) -> _Scripted:
        recorded.append({"factory_role": role, "spec": dict(spec)})
        return _Scripted()

    return _factory


def _badge_recorder(calls: list[dict[str, str]]) -> Any:
    class _Provider:
        async def mint(self, role: str) -> str:
            return "badge-scripted"

        def mcp_servers(self, badge: str) -> dict[str, Any]:
            return {}

        async def revoke(self, badge: str) -> None:
            return None

    def _factory(agent: str, ticket: str, project: str) -> _Provider:
        calls.append({"agent": agent, "ticket": ticket,
                      "project": project})
        return _Provider()

    return _factory


_HECHO = ("sent the email\n```agent-result\n"
          '{"resultado": "hecho", "motivo": "email sent"}\n```\n')


def _live_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "8d5cc2b8-8f1e-4a2a-9c3e-7f6a5b4c3d21",
        "ticket_number": 9002, "project": "jardal-openceo",
        "title": "Send the invoice email", "description": "spec",
        "source": "test",
        "params": dict(MIRROR),
    }
    task.update(overrides)
    return task


class GenericRunLiveTest(unittest.TestCase):
    def test_secretary_run_stages_mounts_and_badges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict[str, Any]] = []
            badge_calls: list[dict[str, str]] = []
            wiring = GenericAgentWiring(
                settings=Settings(),
                repo=None,
                executor_factory=_scripted_factory(recorded, _HECHO),
                badge_provider_factory=_badge_recorder(badge_calls),
                work_base_dir=root / "agent-work",
                catalog=load_catalog())
            task = _live_task(params={**MIRROR, "skill": "enviar-correo"})
            out = asyncio.run(run_generic_agent("secretary", task,
                                                wiring))
            self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
            self.assertIn("hecho", out["result_comment"])
            link = (root / "agent-work"
                    / "8d5cc2b8-8f1e-4a2a-9c3e-7f6a5b4c3d21"
                    / ".codex" / "skills" / "enviar-correo")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(SKILL_DIR),
                             os.path.realpath(link))
            built = [call for call in recorded if "factory_role" in call]
            self.assertEqual(1, len(built))
            spec = built[0]["spec"]
            self.assertEqual([str(REPO)], spec["extra_read_roots"])
            self.assertNotIn("extra_write_roots", spec)
            runs = [call for call in recorded if "prompt" in call]
            self.assertIn("enviar-correo", runs[0]["prompt"])
            self.assertEqual("TASK-2026-00220", badge_calls[0]["ticket"])


class GraphRemovalTest(unittest.TestCase):
    def test_wired_agents_are_the_two_nodes(self) -> None:
        # TASK-2026-00221 retired the finance node.
        self.assertEqual({"dev", "pm"}, set(WIRED_AGENTS))

    def test_secretary_routes_to_the_generic_node(self) -> None:
        graph = OpenCEOGraph(None, None, None, None)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "secretary"}))

    def test_secretary_hint_waits_without_wiring(self) -> None:
        loader = mock.MagicMock()
        loader.load_project.return_value = {}
        graph = OpenCEOGraph(None, loader, None)
        out = asyncio.run(graph.execute(
            _live_task(), agent_hint="secretary"))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])

    def test_secretary_failed_keeps_no_legacy_verdict(self) -> None:
        out = OpenCEOGraph._attach_legacy_agent_result(
            {"params": {}},
            {"status": TaskStatus.FAILED.value,
             "selected_agent": "secretary",
             "error": "boom", "params": {}})
        self.assertNotIn("agent_result", out["params"])


class LockInAgentPathTest(unittest.TestCase):
    def test_agent_box_holds_send_and_nothing_else(self) -> None:
        card = load_catalog().agents["secretary"]
        grant = TicketGrant(kind=TokenKind.AGENT, agent="secretary",
                            ticket="TASK-2026-00220",
                            project="jardal-openceo",
                            tools=box_for_agent(card))
        grant.check("openceo_send_email", {
            "to": ["a@jardal.app"], "subject": "s", "body_text": "b",
            "ticket": "TASK-2026-00220", "project": "jardal-openceo"})
        for foreign in ("manage_dns_record",
                        "openceo_register_expense",
                        "openceo_transition_ticket"):
            with self.subTest(tool=foreign), self.assertRaises(McpDenied):
                grant.check(foreign, {"ticket": "TASK-2026-00220",
                                      "project": "jardal-openceo"})


class NotesRegistryTest(unittest.TestCase):
    def _registry(self) -> WorkflowToolRegistry:
        return WorkflowToolRegistry(
            settings=Settings(_env_file=None))  # type: ignore[call-arg]

    def test_notes_tools_are_unknown(self) -> None:
        described = self._registry().describe()
        self.assertNotIn("secretary.save_note", described)
        self.assertNotIn("secretary.query_notes", described)

    def test_notes_tools_raise_on_call(self) -> None:
        registry = self._registry()
        for name in ("secretary.save_note", "secretary.query_notes"):
            with self.subTest(tool=name):
                with self.assertRaises(MissingWorkflowToolError):
                    asyncio.run(registry.call(name, {}, {}))

    def test_email_tools_stay(self) -> None:
        described = self._registry().describe()
        self.assertIn("secretary.send_email", described)
        self.assertIn("secretary.list_sender_accounts", described)


class _FakeNotes:
    """Motor-shaped notes collection: find + delete_one only."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = {doc["note_id"]: dict(doc) for doc in docs}

    def find(self, _query: dict[str, Any]) -> Any:
        docs = list(self.docs.values())

        class _Cursor:
            def __aiter__(self) -> Any:
                async def _gen() -> Any:
                    for doc in docs:
                        yield dict(doc)
                return _gen()

        return _Cursor()

    async def delete_one(self, filt: dict[str, Any]) -> Any:
        self.docs.pop(filt.get("note_id", ""), None)
        return mock.MagicMock(deleted_count=1)


class _FakeHorizon:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.seen: set[tuple[str, str]] = set()

    async def capture(self, user_id: str, text: str, *,
                      channel: Any, origin: str | None,
                      dedupe: bool = True) -> dict[str, Any]:
        key = (user_id, text)
        if dedupe and key in self.seen:
            return {"captured": False, "duplicate": True}
        self.seen.add(key)
        self.calls.append({"user_id": user_id, "text": text,
                           "channel": channel, "origin": origin,
                           "dedupe": dedupe})
        return {"captured": True, "duplicate": False}


class _FakeTickets:
    def __init__(self, existing: list[str]) -> None:
        self.existing = set(existing)
        self.comments: list[tuple[str, str]] = []

    async def get_ticket(self, identifier: str) -> dict[str, Any] | None:
        if identifier in self.existing:
            return {"name": identifier}
        return None

    async def add_comment(self, identifier: str,
                          content: str) -> dict[str, Any]:
        self.comments.append((identifier, content))
        return {"ok": True}


def _note(note_id: str, content: str, **fields: Any) -> dict[str, Any]:
    note: dict[str, Any] = {"note_id": note_id, "content": content,
                            "project": None, "tags": [], "user": "admin"}
    note.update(fields)
    return note


class NotesMigrationTest(unittest.TestCase):
    def _run(self, docs: list[dict[str, Any]], vault: Path,
             mappings: dict[str, str] | None = None,
             existing: list[str] | None = None,
             dry_run: bool = False) -> tuple[dict[str, Any], _FakeHorizon,
                                             _FakeTickets, _FakeNotes]:
        notes = _FakeNotes(docs)
        horizon = _FakeHorizon()
        tickets = _FakeTickets(existing or [])
        result = asyncio.run(migrate_notes(
            notes, horizon=horizon, tickets=tickets,
            vault_root=vault, mappings=mappings or {}, dry_run=dry_run))
        return result, horizon, tickets, notes

    def test_classify_note_by_project(self) -> None:
        self.assertEqual("personal", classify_note(_note("a", "x")))
        self.assertEqual("personal",
                         classify_note(_note("a", "x", project="  ")))
        self.assertEqual("company",
                         classify_note(_note("a", "x", project="demo")))

    def test_ticket_tag_target(self) -> None:
        self.assertEqual(
            "TASK-2026-00001",
            ticket_tag_target(_note("a", "x", tags=["ticket:TASK-2026-00001"])))
        self.assertIsNone(ticket_tag_target(_note("a", "x")))
        self.assertIsNone(ticket_tag_target(
            _note("a", "x", tags=["ticket:nope", "other"])))

    def test_secret_markers(self) -> None:
        self.assertTrue(note_has_secret(
            _note("a", "key: sk-abcdefghijklmnop")))
        self.assertTrue(note_has_secret(
            _note("a", "x Bearer abc")))
        self.assertFalse(note_has_secret(_note("a", "buy milk")))
        self.assertFalse(note_has_secret(
            _note("a", "finish the task-list today")))

    def test_personal_note_goes_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, horizon, _tickets, notes = self._run(
                [_note("n1", "buy milk", user="owner")], Path(tmp))
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["personal"])
        self.assertEqual([], result["refused"])
        self.assertEqual([], result["errors"])
        self.assertEqual(1, len(horizon.calls))
        call = horizon.calls[0]
        self.assertEqual("owner", call["user_id"])
        self.assertEqual("buy milk", call["text"])
        self.assertEqual("notes-migration:n1", call["origin"])
        self.assertTrue(call["dedupe"])
        self.assertEqual({}, notes.docs)

    def test_company_note_goes_to_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result, _h, _t, notes = self._run(
                [_note("abcdef01", "decide the homepage copy",
                       project="demo", tags=["web"])],
                vault, mappings={"demo": "projects/jardal/demo"})
            self.assertEqual(1, result["company_vault"])
            self.assertEqual([], result["refused"])
            target = (vault / "projects" / "jardal" / "demo"
                      / "nota-abcdef01.md")
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn("migrated_from: notes/abcdef01", text)
            self.assertIn("decide the homepage copy", text)
            self.assertIn("TASK-2026-00220", text)
            self.assertEqual({}, notes.docs)

    def test_vault_rerun_counts_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            mappings = {"demo": "projects/jardal/demo"}
            doc = _note("abcdef02", "same content", project="demo")
            first, _h, _t, _n = self._run([doc], vault, mappings=mappings)
            self.assertEqual(1, first["company_vault"])
            target = (vault / "projects" / "jardal" / "demo"
                      / "nota-abcdef02.md")
            before = target.read_text(encoding="utf-8")
            second, _h2, _t2, notes2 = self._run(
                [doc], vault, mappings=mappings)
            self.assertEqual(1, second["duplicates"])
            self.assertEqual(0, second["company_vault"])
            self.assertEqual(before, target.read_text(encoding="utf-8"))
            self.assertEqual({}, notes2.docs)

    def test_vault_slug_collision_refuses_and_keeps(self) -> None:
        # Review 00015/mimo-1: same slug prefix, different note: a
        # collision is not a proven duplicate; the note must stay.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            mappings = {"demo": "projects/jardal/demo"}
            first_id = "0123456789abXX-aaaa"
            second_id = "0123456789abYY-bbbb"
            first, _h, _t, _n = self._run(
                [_note(first_id, "first note text", project="demo")],
                vault, mappings=mappings)
            self.assertEqual(1, first["company_vault"])
            result, _h2, _t2, notes2 = self._run(
                [_note(second_id, "second note text", project="demo")],
                vault, mappings=mappings)
            self.assertEqual(
                [{"note_id": second_id, "reason": "slug_collision",
                  "target": "vault"}],
                result["refused"])
            self.assertEqual(0, result["duplicates"])
            self.assertIn(second_id, notes2.docs)
            target = (vault / "projects" / "jardal" / "demo"
                      / "nota-0123456789ab.md")
            self.assertIn("first note text",
                          target.read_text(encoding="utf-8"))

    def test_overlong_personal_note_refuses_and_keeps(self) -> None:
        # Review 00015/mimo-2: Horizon capture truncates at its text
        # limit; a longer note must refuse instead of losing its tail.
        with tempfile.TemporaryDirectory() as tmp:
            result, horizon, _t, notes = self._run(
                [_note("n9", "x" * 2001)], Path(tmp))
        self.assertEqual(
            [{"note_id": "n9", "reason": "overlong",
              "target": "personal"}],
            result["refused"])
        self.assertEqual(0, result["personal"])
        self.assertEqual([], horizon.calls)
        self.assertIn("n9", notes.docs)

    def test_personal_note_at_sink_limit_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, horizon, _t, notes = self._run(
                [_note("n10", "x" * 2000)], Path(tmp))
        self.assertEqual(1, result["personal"])
        self.assertEqual(1, len(horizon.calls))
        self.assertEqual({}, notes.docs)

    def test_company_note_with_ticket_tag_goes_to_the_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _h, tickets, notes = self._run(
                [_note("n3", "follow up with the client", project="demo",
                       tags=["ticket:TASK-2026-00001"])],
                Path(tmp), existing=["TASK-2026-00001"])
        self.assertEqual(1, result["company_ticket"])
        self.assertEqual(1, len(tickets.comments))
        ticket, content = tickets.comments[0]
        self.assertEqual("TASK-2026-00001", ticket)
        self.assertIn("<!-- notes-migration:n3 -->", content)
        self.assertIn("follow up with the client", content)
        self.assertEqual({}, notes.docs)

    def test_missing_ticket_refuses_and_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _h, tickets, notes = self._run(
                [_note("n4", "follow up", project="demo",
                       tags=["ticket:TASK-2026-00001"])],
                Path(tmp))
        self.assertEqual(
            [{"note_id": "n4", "reason": "missing_ticket",
              "target": "ticket"}],
            result["refused"])
        self.assertEqual([], tickets.comments)
        self.assertIn("n4", notes.docs)

    def test_missing_mapping_refuses_and_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _h, _t, notes = self._run(
                [_note("n5", "company idea", project="demo")], Path(tmp))
        self.assertEqual(
            [{"note_id": "n5", "reason": "mapping_missing",
              "target": "vault"}],
            result["refused"])
        self.assertIn("n5", notes.docs)

    def test_secret_note_refuses_and_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result, horizon, tickets, notes = self._run(
                [_note("n6", "token sk-abcdefghijklmnop here",
                       project="demo")],
                vault, mappings={"demo": "projects/jardal/demo"})
        self.assertEqual(
            [{"note_id": "n6", "reason": "secrets_suspect",
              "target": "vault"}],
            result["refused"])
        self.assertEqual([], horizon.calls)
        self.assertEqual([], tickets.comments)
        self.assertEqual([], list(vault.rglob("*.md")))
        self.assertIn("n6", notes.docs)

    def test_empty_note_refuses_and_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _h, _t, notes = self._run(
                [_note("n7", "   ")], Path(tmp))
        self.assertEqual(
            [{"note_id": "n7", "reason": "empty", "target": "personal"}],
            result["refused"])
        self.assertIn("n7", notes.docs)

    def test_note_without_id_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes = _FakeNotes([])
            notes.docs[""] = {"content": "orphan"}
            horizon, tickets = _FakeHorizon(), _FakeTickets([])
            result = asyncio.run(migrate_notes(
                notes, horizon=horizon, tickets=tickets,
                vault_root=Path(tmp), mappings={}, dry_run=False))
        self.assertEqual(1, result["total"])
        self.assertEqual([{"note_id": "", "reason": "missing_id"}],
                         result["errors"])

    def test_sink_failure_is_an_error_and_keeps(self) -> None:
        class _Boom:
            async def capture(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            notes = _FakeNotes([_note("n8", "idea")])
            result = asyncio.run(migrate_notes(
                notes, horizon=_Boom(), tickets=_FakeTickets([]),
                vault_root=Path(tmp), mappings={}, dry_run=False))
        self.assertEqual([{"note_id": "n8", "reason": "capture_failed"}],
                         result["errors"])
        self.assertIn("n8", notes.docs)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            docs = [_note("d1", "idea"),
                    _note("d2", "company idea", project="demo")]
            result, horizon, tickets, notes = self._run(
                docs, vault, mappings={"demo": "projects/jardal/demo"},
                dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(2, result["total"])
        self.assertEqual(1, result["personal"])
        self.assertEqual(1, result["company_vault"])
        self.assertEqual([], horizon.calls)
        self.assertEqual([], tickets.comments)
        self.assertEqual([], list(vault.rglob("*.md")))
        self.assertEqual({"d1", "d2"}, set(notes.docs))

    def test_counts_carry_no_content(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            result, _h, _t, _n = self._run(
                [_note("c1", "strictly private words here",
                       project="demo")],
                Path(tmp))
        blob = _json.dumps(result)
        self.assertNotIn("strictly private words here", blob)

    def test_empty_collection_counts_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _h, _t, _n = self._run([], Path(tmp))
        self.assertEqual(0, result["total"])
        self.assertEqual([], result["refused"])
        self.assertEqual([], result["errors"])


class VaultWriterTest(unittest.TestCase):
    def test_rejects_bad_mapping(self) -> None:
        from orchestration.notes_migration import NoteMigrationError

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NoteMigrationError):
                write_company_note(Path(tmp), "shared/evil", "nota-x",
                                   project="demo", date="2026-10-13",
                                   tags=[], note_id="x", content="c")

    def test_rejects_symlinked_root(self) -> None:
        from orchestration.notes_migration import NoteMigrationError

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with self.assertRaises(NoteMigrationError):
                write_company_note(link, "projects/jardal/demo",
                                   "nota-x", project="demo",
                                   date="2026-10-13", tags=[], note_id="x",
                                   content="c")

    def test_existing_file_is_a_duplicate(self) -> None:
        from orchestration.notes_migration import NoteDuplicateError

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            kwargs: dict[str, Any] = {"project": "demo",
                                      "date": "2026-10-13", "tags": [],
                                      "note_id": "dup01abc",
                                      "content": "first"}
            first = write_company_note(vault, "projects/jardal/demo",
                                       "nota-dup01abc", **kwargs)
            self.assertTrue((vault / first).is_file())
            with self.assertRaises(NoteDuplicateError):
                write_company_note(vault, "projects/jardal/demo",
                                   "nota-dup01abc", **kwargs)

    def test_existing_file_of_another_note_is_a_collision(self) -> None:
        from orchestration.notes_migration import NoteRefusedError

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            write_company_note(vault, "projects/jardal/demo", "nota-same",
                               project="demo", date="2026-10-13",
                               tags=[], note_id="first-note",
                               content="first")
            with self.assertRaises(NoteRefusedError) as ctx:
                write_company_note(vault, "projects/jardal/demo",
                                   "nota-same", project="demo",
                                   date="2026-10-13", tags=[],
                                   note_id="second-note",
                                   content="second")
            self.assertEqual("slug_collision", ctx.exception.reason)


if __name__ == "__main__":
    unittest.main()
