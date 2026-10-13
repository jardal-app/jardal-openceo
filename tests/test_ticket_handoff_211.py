"""Ticket handoff: declared skill I/O, outputs on close, delivery on launch.

TASK-2026-00211. The planner freezes INPUTS as text before the previous
ticket ran, so every stage but dev (which reads the repo) guesses. Each
skill declares its inputs/outputs; on close the ticket's files land as
private ERPNext attachments and its data in the execution record; on
launch the executor delivers the predecessors' real outputs.

No network, no LLM: dict-backed repos, scripted executors and a fake
ERPNext attach.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    AgentCard,
    Catalog,
    CatalogError,
    IndexEntry,
    RoleProfile,
    StaffRoster,
    load_role_skills,
    parse_role_skill,
)
from orchestration import ticket_handoff as handoff  # noqa: E402
from orchestration.generic_agent import (  # noqa: E402
    GenericAgentWiring,
    build_agent_prompt,
    run_generic_agent,
)
from orchestration.live import ExecResult  # noqa: E402


def write_skill_md(skill_dir: Path, name: str, header_extra: str = "") -> Path:
    skill_dir.mkdir(parents=True)
    target = skill_dir / "SKILL.md"
    target.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Test skill {name}.\n"
        "status: activa\n"
        "ticket: TASK-2026-00211\n"
        "date: 2026-10-13\n"
        f"{header_extra}"
        "---\n\n# Test\n",
        encoding="utf-8",
    )
    return target


class SkillDeclarationTests(unittest.TestCase):
    def test_inputs_outputs_default_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = write_skill_md(Path(tmp) / "probe", "probe")
            skill = parse_role_skill(target, "operations")
        self.assertEqual(skill.inputs, [])
        self.assertEqual(skill.outputs, [])

    def test_inputs_outputs_parsed_from_frontmatter(self) -> None:
        extra = "inputs:\n  - project_key\n  - display_name\noutputs:\n  - project_key\n"
        with tempfile.TemporaryDirectory() as tmp:
            target = write_skill_md(Path(tmp) / "probe", "probe", extra)
            skill = parse_role_skill(target, "operations")
        self.assertEqual(skill.inputs, ["project_key", "display_name"])
        self.assertEqual(skill.outputs, ["project_key"])

    def test_blank_and_duplicate_entries_rejected(self) -> None:
        for bad in ("inputs:\n  - '  '\n", "inputs:\n  - a\n  - a\n",
                    "inputs: project_key\n", "outputs:\n  - 42\n"):
            with tempfile.TemporaryDirectory() as tmp:
                target = write_skill_md(Path(tmp) / "probe", "probe", bad)
                with self.assertRaises(CatalogError, msg=bad):
                    parse_role_skill(target, "operations")

    def test_shipped_skills_declare_inputs_and_outputs(self) -> None:
        for role, names in {
                "operations": ["crear-proyecto"],
                "secretary": ["enviar-correo"],
                "research": ["investigar"],
                "finance": ["consultar-resumen", "pago-elfec",
                            "registrar-gasto", "registrar-ingreso",
                            "regla-10"],
        }.items():
            found = {skill.name: skill
                     for skill in load_role_skills(role, f"skills/{role}",
                                                   REPO)}
            for name in names:
                with self.subTest(skill=name):
                    self.assertTrue(found[name].inputs)
                    self.assertTrue(found[name].outputs)


class CollectOutputsTests(unittest.TestCase):
    def test_file_and_text_outputs_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp)
            (ticket_dir / "brief.md").write_text("hello", encoding="utf-8")
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": ["brief.md", "journal entry JE-1"],
                       "evidencia": "ERPNext JE-1"}
            record = handoff.collect_ticket_outputs(verdict, ticket_dir)
        self.assertEqual(len(record["files"]), 1)
        entry = record["files"][0]
        self.assertEqual(entry["name"], "brief.md")
        self.assertEqual(entry["size"], 5)
        self.assertTrue(entry["sha256"])
        self.assertIsNone(entry["skipped"])
        self.assertEqual(record["texts"], ["journal entry JE-1"])
        self.assertEqual(record["evidence"], "ERPNext JE-1")

    def test_path_escape_stays_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp)
            (ticket_dir / "secret.txt").write_text("x", encoding="utf-8")
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": ["../secret.txt", "/etc/hostname",
                                       str(ticket_dir / "secret.txt")]}
            record = handoff.collect_ticket_outputs(verdict, ticket_dir)
        self.assertEqual(record["files"], [])
        self.assertEqual(len(record["texts"]), 3)

    def test_symlink_and_hidden_and_upstream_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp)
            outside = ticket_dir / "real.txt"
            outside.write_text("x", encoding="utf-8")
            (ticket_dir / "link.txt").symlink_to(outside)
            hidden = ticket_dir / ".codex" / "skills"
            hidden.mkdir(parents=True)
            (hidden / "s.md").write_text("x", encoding="utf-8")
            staged = ticket_dir / "upstream" / "task-a"
            staged.mkdir(parents=True)
            (staged / "a.md").write_text("x", encoding="utf-8")
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": ["link.txt", ".codex/skills/s.md",
                                       "upstream/task-a/a.md"]}
            record = handoff.collect_ticket_outputs(verdict, ticket_dir)
        self.assertEqual(record["files"], [])
        self.assertEqual(record["texts"], [])

    def test_oversize_file_skipped_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp)
            (ticket_dir / "big.bin").write_bytes(
                b"z" * (handoff.MAX_OUTPUT_FILE_BYTES + 1))
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": ["big.bin"]}
            record = handoff.collect_ticket_outputs(verdict, ticket_dir)
        self.assertEqual(len(record["files"]), 1)
        self.assertIsNotNone(record["files"][0]["skipped"])

    def test_file_cap_marks_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp)
            names = []
            for index in range(handoff.MAX_OUTPUT_FILES + 3):
                name = f"f{index}.txt"
                (ticket_dir / name).write_text("x", encoding="utf-8")
                names.append(name)
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": names}
            record = handoff.collect_ticket_outputs(verdict, ticket_dir)
        kept = [entry for entry in record["files"]
                if entry["skipped"] is None]
        self.assertEqual(len(kept), handoff.MAX_OUTPUT_FILES)

    def test_missing_dir_yields_texts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone"
            verdict = {"resultado": "hecho", "motivo": "done",
                       "entregables": ["a.txt"]}
            record = handoff.collect_ticket_outputs(verdict, missing)
        self.assertEqual(record["files"], [])
        self.assertEqual(record["texts"], ["a.txt"])
        self.assertTrue(record["notes"])

    def test_declared_outputs_travel_in_the_record(self) -> None:
        verdict = {"resultado": "hecho", "motivo": "done",
                   "entregables": []}
        record = handoff.collect_ticket_outputs(
            verdict, None, declared_outputs=["journal_entry"])
        self.assertEqual(record["declared"], ["journal_entry"])


class FakeRepo:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.rows.get(task_id)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        self.patches.append((task_id, updates))
        row = self.rows.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)


def completed_row(task_id: str, outputs: dict[str, Any] | None,
                  **over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if outputs is not None:
        params[handoff.TICKET_OUTPUTS_KEY] = outputs
    row: dict[str, Any] = {"task_id": task_id, "title": f"title {task_id}",
                           "status": "completed", "params": params}
    row.update(over)
    return row


class PublishOutputsTests(unittest.TestCase):
    def test_attach_and_record(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                ticket_dir = Path(tmp)
                (ticket_dir / "brief.md").write_text("hello",
                                                     encoding="utf-8")
                attach = AsyncMock(return_value={
                    "ok": True, "duplicate": False,
                    "attachment": {"name": "FILE-1"}})
                verdict = {"resultado": "hecho", "motivo": "done",
                           "entregables": ["brief.md"], "evidencia": ""}
                record = await handoff.publish_ticket_outputs(
                    agent="research", skill="investigar", verdict=verdict,
                    ticket_folder=ticket_dir, mirror="TASK-1", attach=attach)
            self.assertEqual(record["agent"], "research")
            self.assertEqual(record["skill"], "investigar")
            self.assertEqual(record["verdict"], "hecho")
            self.assertTrue(record["published_at"])
            entry = record["files"][0]
            self.assertFalse(entry["attachment"]["duplicate"])
            attach.assert_awaited_once()
            _, kwargs = attach.await_args
            self.assertEqual(kwargs["file_name"], "brief.md")
            self.assertTrue(kwargs["private"])
            self.assertEqual(base64.b64decode(kwargs["content_base64"]),
                             b"hello")
        asyncio.run(go())

    def test_no_mirror_lists_files_without_attachments(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                ticket_dir = Path(tmp)
                (ticket_dir / "brief.md").write_text("hello",
                                                     encoding="utf-8")
                attach = AsyncMock()
                verdict = {"resultado": "hecho", "motivo": "done",
                           "entregables": ["brief.md"], "evidencia": ""}
                record = await handoff.publish_ticket_outputs(
                    agent="research", skill=None, verdict=verdict,
                    ticket_folder=ticket_dir, mirror=None, attach=attach)
            self.assertIsNone(record["files"][0]["attachment"])
            attach.assert_not_awaited()
        asyncio.run(go())

    def test_attach_failure_never_raises(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                ticket_dir = Path(tmp)
                (ticket_dir / "brief.md").write_text("hello",
                                                     encoding="utf-8")
                attach = AsyncMock(side_effect=RuntimeError("erp down"))
                verdict = {"resultado": "hecho", "motivo": "done",
                           "entregables": ["brief.md"], "evidencia": ""}
                record = await handoff.publish_ticket_outputs(
                    agent="research", skill=None, verdict=verdict,
                    ticket_folder=ticket_dir, mirror="TASK-1", attach=attach)
            self.assertIn("erp down",
                          record["files"][0]["skipped"])
        asyncio.run(go())


def stored_outputs(*, texts: list[str] | None = None,
                   files: list[dict[str, Any]] | None = None,
                   evidence: str = "") -> dict[str, Any]:
    return {"agent": "research", "skill": "investigar", "verdict": "hecho",
            "declared": [], "texts": texts or [], "evidence": evidence,
            "files": files or [], "notes": []}


class ResolveUpstreamTests(unittest.TestCase):
    def test_completed_predecessor_resolves(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                repo = FakeRepo({
                    "task-a": completed_row(
                        "task-a", stored_outputs(texts=["row 1"])),
                })
                resolved = await handoff.resolve_upstream_outputs(
                    repo, ["task-a"], base)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0]["task_id"], "task-a")
            self.assertEqual(resolved[0]["texts"], ["row 1"])
            self.assertIsNone(resolved[0]["skipped"])
        asyncio.run(go())

    def test_failed_missing_and_empty_predecessors_skipped(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                repo = FakeRepo({
                    "task-f": completed_row("task-f", stored_outputs(),
                                            status="failed"),
                    "task-e": completed_row("task-e", None),
                })
                resolved = await handoff.resolve_upstream_outputs(
                    repo, ["task-f", "task-e", "task-gone"], Path(tmp))
            reasons = {entry["task_id"]: entry["skipped"]
                       for entry in resolved}
            self.assertTrue(reasons["task-f"])
            self.assertTrue(reasons["task-e"])
            self.assertTrue(reasons["task-gone"])
        asyncio.run(go())

    def test_no_blockers_resolves_empty(self) -> None:
        async def go() -> None:
            resolved = await handoff.resolve_upstream_outputs(
                FakeRepo({}), [])
            self.assertEqual(resolved, [])
        asyncio.run(go())


class RenderAndStageTests(unittest.TestCase):
    def test_empty_renders_empty(self) -> None:
        self.assertEqual(handoff.render_upstream_section([]), "")

    def test_render_lists_texts_files_and_skips(self) -> None:
        resolved = [{
            "task_id": "task-a", "title": "brief", "verdict": "hecho",
            "skill": "investigar", "declared": [], "texts": ["row 1"],
            "evidence": "JE-1",
            "files": [{"name": "brief.md", "sha256": "abc", "size": 5,
                       "attachment": None, "skipped": None}],
            "source_dir": None, "skipped": None,
        }, {
            "task_id": "task-f", "title": "", "verdict": "", "skill": None,
            "declared": [], "texts": [], "evidence": "", "files": [],
            "source_dir": None, "skipped": "status failed, no outputs",
        }]
        text = handoff.render_upstream_section(
            resolved, staged={("task-a", "brief.md")})
        self.assertIn("task-a", text)
        self.assertIn("row 1", text)
        self.assertIn("JE-1", text)
        self.assertIn("upstream/task-a/brief.md", text)
        self.assertIn("task-f", text)
        self.assertIn("no outputs", text)

    def test_stage_copies_verified_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            upstream_dir = base / "task-a-dir"
            upstream_dir.mkdir()
            (upstream_dir / "brief.md").write_text("hello", encoding="utf-8")
            import hashlib
            digest = hashlib.sha256(b"hello").hexdigest()
            resolved = [{
                "task_id": "task-a", "title": "", "verdict": "hecho",
                "skill": None, "declared": [], "texts": [],
                "evidence": "",
                "files": [{"name": "brief.md", "sha256": digest, "size": 5,
                           "attachment": None, "skipped": None}],
                "source_dir": str(upstream_dir), "skipped": None,
            }]
            ticket_dir = base / "task-b-dir"
            ticket_dir.mkdir()
            staged = handoff.stage_upstream_files(resolved, ticket_dir)
            target = ticket_dir / "upstream" / "task-a" / "brief.md"
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            self.assertEqual(staged, [("task-a", "brief.md")])

    def test_stage_skips_tampered_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            upstream_dir = base / "task-a-dir"
            upstream_dir.mkdir()
            (upstream_dir / "brief.md").write_text("changed",
                                                   encoding="utf-8")
            resolved = [{
                "task_id": "task-a", "title": "", "verdict": "hecho",
                "skill": None, "declared": [], "texts": [],
                "evidence": "",
                "files": [{"name": "brief.md", "sha256": "deadbeef",
                           "size": 5, "attachment": None,
                           "skipped": None}],
                "source_dir": str(upstream_dir), "skipped": None,
            }]
            ticket_dir = base / "task-b-dir"
            ticket_dir.mkdir()
            staged = handoff.stage_upstream_files(resolved, ticket_dir)
            self.assertEqual(staged, [])
            self.assertFalse(
                (ticket_dir / "upstream" / "task-a" / "brief.md").exists())


def tiny_catalog_here(specs: dict[str, dict[str, Any]]) -> Catalog:
    entries = [
        IndexEntry(name=name, summary=f"{name} does things, not others.",
                   card=f"agents/{name}.yaml")
        for name in specs
    ]
    agents = {}
    for name, spec in specs.items():
        agents[name] = AgentCard(
            name=name, type="ai", engine="muse-code",
            profile=spec.get("profile", f"profiles/{name}.yaml"),
            boundary_tools=[], skills_dir=f"skills/{name}", skills=[],
            inputs=[], outputs=[], enabled=True)
    return Catalog(version=1, entries=entries, agents=agents,
                   staff=StaffRoster())


def tiny_profile_here(name: str = "operations") -> RoleProfile:
    return RoleProfile(
        name=name, agent=name, engine="muse-code",
        card={"who": f"{name} test agent.",
              "delivers": ["test output"], "never": ["change states"]},
        rules="AGENTS.md", skills=["create-skill"],
        output_schema="openceo-core/objectives/agent_result.schema.json")


class _FakeBadgeProvider:
    async def mint(self, role: str) -> str:
        del role
        return "badge-test"

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        del badge
        return {}

    async def revoke(self, badge: str) -> None:
        del badge


def scripted_factory(result: ExecResult,
                     calls: list[dict[str, Any]]):
    class _Scripted:
        async def run(self, role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            calls.append({"role": role, "prompt": prompt,
                          "workdir": workdir})
            return result

        async def cleanup(self, role: str) -> None:
            del role

    def factory(role: str, spec: dict[str, Any],
                provider: Any) -> _Scripted:
        del role, spec, provider
        return _Scripted()

    def badge_factory(agent: str, ticket: str,
                      project: str) -> _FakeBadgeProvider:
        del agent, ticket, project
        return _FakeBadgeProvider()

    return factory, badge_factory


def hecho_with(outputs: list[str], motivo: str = "done") -> str:
    import json
    payload = json.dumps({"resultado": "hecho", "motivo": motivo,
                          "entregables": outputs, "evidencia": ""})
    return f"did it\n```agent-result\n{payload}\n```\n"


def handoff_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": task_id, "ticket_number": 211,
        "project": "jardal-openceo", "title": f"probe {task_id}",
        "description": "Do the thing.", "source": "api", "params": {},
    }
    task.update(overrides)
    return task


class PromptSectionTests(unittest.TestCase):
    def test_build_prompt_carries_upstream_section(self) -> None:
        task = handoff_task("task-b")
        plain = build_agent_prompt(tiny_profile_here(), task)
        section = "ENTRADAS DE TICKETS ANTERIORES\n- x"
        with_section = build_agent_prompt(
            tiny_profile_here(), task, upstream_section=section)
        self.assertNotIn("ENTRADAS", plain)
        self.assertIn(section, with_section)

    def test_empty_section_leaves_prompt_untouched(self) -> None:
        task = handoff_task("task-b")
        self.assertEqual(
            build_agent_prompt(tiny_profile_here(), task),
            build_agent_prompt(tiny_profile_here(), task,
                               upstream_section=""))


class GenericAgentHandoffTests(unittest.TestCase):
    def test_llm_run_receives_predecessor_outputs(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = FakeRepo({
                    "task-a": completed_row(
                        "task-a", stored_outputs(texts=["row 1"])),
                    "task-b": {"task_id": "task-b", "blocked_by": ["task-a"]},
                })
                calls: list[dict[str, Any]] = []
                factory, badge_factory = scripted_factory(
                    ExecResult(returncode=0, elapsed_s=1.0,
                               output_tail=hecho_with([])), calls)
                from settings import Settings
                wiring = GenericAgentWiring(
                    settings=Settings(DEEPSEEK_API_KEY="",
                                      PROJECTS_DIR="projects",
                                      DEFAULT_PROJECT="jardal-openceo"),
                    repo=repo, executor_factory=factory,
                    badge_provider_factory=badge_factory,
                    repo_root=root,
                    work_base_dir=root / "agent-work",
                    catalog=tiny_catalog_here({"operations": {}}))
                task = handoff_task("task-b", blocked_by=["task-a"])
                out = await run_generic_agent("operations", task, wiring)
                self.assertEqual("completed", out["status"])
                prompts = [call["prompt"] for call in calls
                           if "prompt" in call]
                self.assertEqual(1, len(prompts))
                self.assertIn("row 1", prompts[0])
                self.assertIn("task-a", prompts[0])
        asyncio.run(go())

    def test_no_blockers_leaves_prompt_untouched(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls: list[dict[str, Any]] = []
                factory, badge_factory = scripted_factory(
                    ExecResult(returncode=0, elapsed_s=1.0,
                               output_tail=hecho_with([])), calls)
                from settings import Settings
                wiring = GenericAgentWiring(
                    settings=Settings(DEEPSEEK_API_KEY="",
                                      PROJECTS_DIR="projects",
                                      DEFAULT_PROJECT="jardal-openceo"),
                    repo=FakeRepo({}), executor_factory=factory,
                    badge_provider_factory=badge_factory,
                    repo_root=root,
                    work_base_dir=root / "agent-work",
                    catalog=tiny_catalog_here({"operations": {}}))
                out = await run_generic_agent(
                    "operations", handoff_task("task-solo"), wiring)
                self.assertEqual("completed", out["status"])
                prompts = [call["prompt"] for call in calls
                           if "prompt" in call]
                self.assertNotIn("ENTRADAS", prompts[0])
        asyncio.run(go())


class ChainedTicketsAcceptanceTests(unittest.TestCase):
    def test_first_output_reaches_second(self) -> None:
        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = root / "agent-work"
                repo = FakeRepo({
                    "task-b": {"task_id": "task-b", "title": "second",
                               "blocked_by": ["task-a"]},
                })
                from settings import Settings
                settings = Settings(DEEPSEEK_API_KEY="",
                                    PROJECTS_DIR="projects",
                                    DEFAULT_PROJECT="jardal-openceo")
                catalog = tiny_catalog_here({"operations": {}})

                first_calls: list[dict[str, Any]] = []
                first_factory, first_badges = scripted_factory(
                    ExecResult(returncode=0, elapsed_s=1.0,
                               output_tail=hecho_with(["brief.md"])),
                    first_calls)
                first_wiring = GenericAgentWiring(
                    settings=settings, repo=repo,
                    executor_factory=first_factory,
                    badge_provider_factory=first_badges,
                    repo_root=root, work_base_dir=base, catalog=catalog)
                first = await run_generic_agent(
                    "operations", handoff_task("task-a"), first_wiring)
                self.assertEqual("completed", first["status"])
                verdict = first["params"]["agent_result"]

                from orchestration.agent_sandbox import ticket_dir
                folder_a = ticket_dir("task-a", base_dir=base)
                (folder_a / "brief.md").write_text("brief body",
                                                   encoding="utf-8")
                attach = AsyncMock(return_value={
                    "ok": True, "duplicate": False,
                    "attachment": {"name": "FILE-1"}})
                record = await handoff.publish_ticket_outputs(
                    agent="operations", skill=None, verdict=verdict,
                    ticket_folder=folder_a, mirror="TASK-A", attach=attach)
                repo.rows["task-a"] = completed_row(
                    "task-a", record, title="first")

                second_calls: list[dict[str, Any]] = []
                second_factory, second_badges = scripted_factory(
                    ExecResult(returncode=0, elapsed_s=1.0,
                               output_tail=hecho_with([])),
                    second_calls)
                second_wiring = GenericAgentWiring(
                    settings=settings, repo=repo,
                    executor_factory=second_factory,
                    badge_provider_factory=second_badges,
                    repo_root=root, work_base_dir=base, catalog=catalog)
                second = await run_generic_agent(
                    "operations",
                    handoff_task("task-b", blocked_by=["task-a"]),
                    second_wiring)
                self.assertEqual("completed", second["status"])
                prompt = second_calls[0]["prompt"]
                self.assertIn("brief.md", prompt)
                staged = (ticket_dir("task-b", base_dir=base)
                          / "upstream" / "task-a" / "brief.md")
                self.assertEqual(staged.read_text(encoding="utf-8"),
                                 "brief body")
        asyncio.run(go())
