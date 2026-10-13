"""Generic non-dev delivery verification (TASK-2026-00216).

A completed non-dev child verifies when its structured result carries
the outputs its skill declares plus the evidence the skill requires;
a skill may ship a stricter `verify.py`. Missing verifiers still park
as capability_gap, missing evidence as data_gaps (TASK-2026-00175).

No network, no LLM: stub children, fixture skill dirs, scripted runs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for _path in (CORE_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from objectives.delivery_verification import (  # noqa: E402
    SkillSpec,
    generic_agent_verifiers,
    normalize_citation,
    verify_agent_delivery,
)
from objectives.engine import run_final_review  # noqa: E402
from test_company_objectives import (  # noqa: E402
    _ExplodingExecutor,
    _child,
    _env_for,
    _mirrored,
    _plan_result,
    _scratch_repo,
)


def skill_spec(outputs: list[str], evidence: list[str] | None = None,
               source_dir: Path | None = None) -> SkillSpec:
    return SkillSpec(outputs=list(outputs),
                     evidence=list(evidence or []),
                     source_dir=str(source_dir) if source_dir else None)


def fake_resolver(specs: dict[tuple[str, str], SkillSpec]):
    def resolve(agent: str, skill: str) -> SkillSpec | None:
        return specs.get((agent, skill))
    return resolve


def nondev_child(task_id: str, agent: str, skill: str | None,
                 verdict: dict[str, Any] | None,
                 record: dict[str, Any] | None = None) -> dict[str, Any]:
    child = _child(task_id, "completed", dev=False)
    child["params"]["target_agent"] = agent
    if skill is not None:
        child["params"]["skill"] = skill
    if verdict is not None:
        child["params"]["agent_result"] = verdict
    if record is not None:
        child["params"]["ticket_outputs"] = record
    return child


def hecho(entregables: list[str], evidencia: str = "") -> dict[str, Any]:
    return {"resultado": "hecho", "motivo": "done",
            "entregables": entregables, "evidencia": evidencia}


class CitationTests(unittest.TestCase):
    def test_normalize_drops_case_and_separators(self) -> None:
        self.assertEqual(normalize_citation("Journal Entry JE-1"),
                         "journalentryje1")
        self.assertEqual(normalize_citation("research_brief"),
                         normalize_citation("Research Brief"))


class GenericVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_cited_outputs_and_evidence_verify(self) -> None:
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], ["journal_entry"])})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["register_expense.journal_entry: JE-1"],
                  "Journal entry JE-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["missing_artifacts"], [])
        self.assertEqual(report["unmet_criteria"], [])

    async def test_missing_output_is_a_missing_artifact(self) -> None:
        resolve = fake_resolver({
            ("research", "investigar"): skill_spec(
                ["research_brief", "research_id"], ["research_id"])})
        child = nondev_child(
            "kid-1", "research", "investigar",
            hecho(["research_id: R-1"], "research_id R-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_artifacts"], ["research_brief"])
        self.assertEqual(report["unmet_criteria"], [])

    async def test_non_hecho_verdict_is_unmet(self) -> None:
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(["journal_entry"])})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            {"resultado": "bloqueado", "motivo": "stuck",
             "entregables": ["journal_entry JE-1"], "evidencia": ""})
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertIn("hecho verdict", report["unmet_criteria"])

    async def test_missing_verdict_is_unmet(self) -> None:
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(["journal_entry"])})
        child = nondev_child("kid-1", "finance", "registrar-gasto", None)
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertIn("hecho verdict", report["unmet_criteria"])

    async def test_unknown_skill_is_unmet(self) -> None:
        resolve = fake_resolver({})
        child = nondev_child("kid-1", "finance", "nope",
                             hecho(["x"], "y"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertIn("known skill", report["unmet_criteria"])

    async def test_skill_without_declared_outputs_is_unmet(self) -> None:
        resolve = fake_resolver({
            ("finance", "vague"): skill_spec([])})
        child = nondev_child("kid-1", "finance", "vague",
                             hecho(["something"], "somewhere"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertIn("declared outputs", report["unmet_criteria"])

    async def test_missing_evidence_is_unmet(self) -> None:
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], ["journal_entry"])})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["register_expense.journal_entry: JE-1"], "trust me"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_artifacts"], [])
        self.assertEqual(report["unmet_criteria"],
                         ["evidence: journal_entry"])

    async def test_handoff_record_supplements_the_verdict(self) -> None:
        resolve = fake_resolver({
            ("research", "investigar"): skill_spec(
                ["research_brief"], ["research_id"])})
        child = nondev_child(
            "kid-1", "research", "investigar",
            hecho([], ""),
            record={"texts": ["research_brief attached"],
                    "files": [{"name": "brief.md"}],
                    "evidence": "research_id R-1"})
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertTrue(report["ok"], report)


class SkillVerifyHookTests(unittest.IsolatedAsyncioTestCase):
    def _skill_dir(self, body: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "verify.py").write_text(body, encoding="utf-8")
        return root

    async def test_stricter_skill_verifier_merges_its_gaps(self) -> None:
        root = self._skill_dir(
            "def verify(child, outputs):\n"
            "    return {'ok': False, 'missing_artifacts': ['audit_seal'],\n"
            "            'unmet_criteria': []}\n")
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], [], source_dir=root)})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["journal_entry JE-1"], "JE-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_artifacts"], ["audit_seal"])

    async def test_passing_skill_verifier_keeps_ok(self) -> None:
        root = self._skill_dir(
            "async def verify(child, outputs):\n"
            "    return {'ok': True}\n")
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], [], source_dir=root)})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["journal_entry JE-1"], "JE-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertTrue(report["ok"], report)

    async def test_broken_skill_verifier_is_a_gap_not_a_crash(self) -> None:
        root = self._skill_dir("def verify(:\n  broken\n")
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], [], source_dir=root)})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["journal_entry JE-1"], "JE-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("skill verifier" in item
                for item in report["unmet_criteria"]))

    async def test_skill_verifier_without_verify_is_a_gap(self) -> None:
        root = self._skill_dir("VALUE = 1\n")
        resolve = fake_resolver({
            ("finance", "registrar-gasto"): skill_spec(
                ["journal_entry"], [], source_dir=root)})
        child = nondev_child(
            "kid-1", "finance", "registrar-gasto",
            hecho(["journal_entry JE-1"], "JE-1"))
        report = await verify_agent_delivery(child, resolve_skill=resolve)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("skill verifier" in item
                for item in report["unmet_criteria"]))


class VerifierRegistrationTests(unittest.TestCase):
    def test_enabled_ai_agents_get_the_generic_verifier(self) -> None:
        verifiers = generic_agent_verifiers()
        for name in ("operations", "secretary", "finance", "research"):
            with self.subTest(agent=name):
                self.assertIn(name, verifiers)
        self.assertNotIn("owner", verifiers)
        self.assertNotIn("pm", verifiers)


class FinalReviewAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, env: Any) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })

    def _parent(self) -> dict[str, Any]:
        return _mirrored("task-parent", "TASK-1", status="converted",
                         objective_id="OBJ-1")

    async def test_full_evidence_closes(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": nondev_child(
                     "kid-1", "research", "investigar",
                     hecho(["research_brief: brief body",
                            "research_id: R-1"],
                           "research_id R-1 brief_path brief.md"))}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = generic_agent_verifiers()
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")
        self.assertEqual(result["action"], "closed")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "closed")
        self.assertEqual(stored["delivery"]["evidence"],
                         {"verified": {"kid-1": "research"}})

    async def test_missing_output_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": nondev_child(
                     "kid-1", "research", "investigar",
                     hecho(["research_id: R-1"],
                           "research_id R-1 brief_path brief.md"))}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = generic_agent_verifiers()
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("missing artifact: research_brief",
                      result["detail"])

    async def test_unknown_domain_parks_capability_gap(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": nondev_child(
                     "kid-1", "legacy", "old-skill",
                     hecho(["did it"], "somewhere"))}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = generic_agent_verifiers()
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capability_gap")
        self.assertIn("legacy", result["detail"])


class PromptDeclarationTests(unittest.TestCase):
    def _profile(self) -> Any:
        from catalog import RoleProfile
        return RoleProfile(
            name="finance", agent="finance", engine="muse-code",
            card={"who": "finance test agent.",
                  "delivers": ["test output"],
                  "never": ["change states"]},
            rules="AGENTS.md", skills=["create-skill"],
            output_schema=(
                "openceo-core/objectives/agent_result.schema.json"))

    def test_suggested_skill_lists_declared_outputs(self) -> None:
        from orchestration.generic_agent import build_agent_prompt
        task = {"task_id": "t", "ticket_number": 1, "title": "t",
                "description": "d", "params": {}}
        plain = build_agent_prompt(self._profile(), task, skill="s")
        declared = build_agent_prompt(
            self._profile(), task, skill="s",
            skill_outputs=["journal_entry"],
            skill_evidence=["journal_entry"])
        self.assertIn("Suggested skill: s", plain)
        self.assertNotIn("journal_entry", plain)
        self.assertIn("journal_entry", declared)
        self.assertIn("entregables", declared)
        self.assertIn("evidencia", declared)

    def test_llm_run_passes_skill_declarations(self) -> None:
        import asyncio
        from catalog import AgentCard, Catalog, IndexEntry, StaffRoster
        from orchestration.generic_agent import (
            GenericAgentWiring, run_generic_agent)
        from orchestration.live import ExecResult
        from settings import Settings

        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill_dir = root / "skills" / "finance" / "probe"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: probe\ndescription: Test probe.\n"
                    "status: activa\nticket: TASK-2026-00216\n"
                    "date: 2026-10-13\n"
                    "outputs:\n  - probe_output\n"
                    "evidence:\n  - probe_proof\n---\n\n# Test\n",
                    encoding="utf-8")
                prompts: list[str] = []

                class _Scripted:
                    async def run(self, role: str, prompt: str,
                                  workdir: Path,
                                  timeout_s: int) -> ExecResult:
                        prompts.append(prompt)
                        return ExecResult(
                            returncode=0, elapsed_s=1.0,
                            output_tail="did it\n```agent-result\n"
                            '{"resultado": "hecho", '
                            '"motivo": "done"}\n```\n')

                    async def cleanup(self, role: str) -> None:
                        del role

                class _Badges:
                    async def mint(self, role: str) -> str:
                        del role
                        return "badge-test"

                    def mcp_servers(self, badge: str) -> dict[str, Any]:
                        del badge
                        return {}

                    async def revoke(self, badge: str) -> None:
                        del badge

                catalog = Catalog(
                    version=1,
                    entries=[IndexEntry(
                        name="finance",
                        summary="finance does things, not others.",
                        card="agents/finance.yaml")],
                    agents={"finance": AgentCard(
                        name="finance", type="ai", engine="muse-code",
                        profile="profiles/finance.yaml",
                        boundary_tools=[],
                        skills_dir="skills/finance", skills=[],
                        inputs=[], outputs=[], enabled=True)},
                    staff=StaffRoster())
                wiring = GenericAgentWiring(
                    settings=Settings(
                        DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                        DEFAULT_PROJECT="jardal-openceo"),
                    repo=None,
                    executor_factory=lambda r, s, p: _Scripted(),
                    badge_provider_factory=lambda a, t, p: _Badges(),
                    repo_root=root,
                    work_base_dir=root / "agent-work",
                    catalog=catalog)
                out = await run_generic_agent(
                    "finance",
                    {"task_id": "task-216", "ticket_number": 216,
                     "project": "p", "title": "probe",
                     "description": "Do it.", "source": "api",
                     "params": {"skill": "probe"}},
                    wiring)
                self.assertEqual("completed", out["status"])
                self.assertIn("probe_output", prompts[0])
                self.assertIn("probe_proof", prompts[0])
        asyncio.run(go())


class ExactVerdictTests(unittest.TestCase):
    def test_exact_run_cites_workflow_outputs(self) -> None:
        import asyncio
        from catalog import AgentCard, Catalog, IndexEntry, StaffRoster
        from orchestration.generic_agent import (
            GenericAgentWiring, run_generic_agent)
        from settings import Settings
        from workflow_service import WorkflowExecutionOutcome

        async def go() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill_dir = root / "skills" / "finance" / "probe"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: probe\ndescription: Test probe.\n"
                    "status: activa\nticket: TASK-2026-00216\n"
                    "date: 2026-10-13\n---\n\n# Test\n",
                    encoding="utf-8")
                (skill_dir / "workflow.yaml").write_text(
                    "workflow: finance.probe\nsteps: []\n",
                    encoding="utf-8")

                class _FakeWorkflows:
                    def parse_workflow_text(self, raw: str,
                                            source: str) -> dict[str, Any]:
                        del raw, source
                        return {"workflow": "finance.probe"}

                    async def execute_task_workflow(
                            self, task: dict[str, Any],
                            workflow: dict[str, Any]):
                        del task, workflow
                        return WorkflowExecutionOutcome(
                            selected_agent="finance",
                            status="completed", steps=[],
                            last_response_summary="summary here",
                            params={})

                    async def latest_workflow_outputs(
                            self, task_id: str) -> dict[str, Any]:
                        del task_id
                        return {"register_expense": {
                            "ok": True, "journal_entry": "JE-1"}}

                def _forbidden(role: str, spec: Any,
                               provider: Any) -> Any:
                    raise AssertionError("exact runs never invoke the LLM")

                catalog = Catalog(
                    version=1,
                    entries=[IndexEntry(
                        name="finance",
                        summary="finance does things, not others.",
                        card="agents/finance.yaml")],
                    agents={"finance": AgentCard(
                        name="finance", type="ai", engine="muse-code",
                        profile="profiles/finance.yaml",
                        boundary_tools=[],
                        skills_dir="skills/finance", skills=[],
                        inputs=[], outputs=[], enabled=True)},
                    staff=StaffRoster())
                wiring = GenericAgentWiring(
                    settings=Settings(
                        DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                        DEFAULT_PROJECT="jardal-openceo"),
                    repo=None, workflow_service=_FakeWorkflows(),
                    executor_factory=_forbidden,
                    badge_provider_factory=lambda a, t, p: None,
                    repo_root=root,
                    work_base_dir=root / "agent-work",
                    catalog=catalog)
                out = await run_generic_agent(
                    "finance",
                    {"task_id": "task-216", "ticket_number": 216,
                     "project": "p", "title": "probe",
                     "description": "Do it.", "source": "api",
                     "params": {"skill": "probe"}},
                    wiring)
                self.assertEqual("completed", out["status"])
                verdict = out["params"]["agent_result"]
                self.assertEqual("hecho", verdict["resultado"])
                self.assertTrue(
                    any("journal_entry" in str(item)
                        for item in verdict["entregables"]),
                    verdict)
                self.assertIn("journal_entry", verdict["evidencia"])
        asyncio.run(go())


class SkillEvidenceDeclarationTests(unittest.TestCase):
    def test_shipped_skills_declare_required_evidence(self) -> None:
        from catalog import load_role_skills
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
                                                   TESTS_DIR.parent)}
            for name in names:
                with self.subTest(skill=name):
                    self.assertTrue(found[name].evidence)
