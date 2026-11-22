"""Sellable UI across OpenCEO (TASK-2026-00896).

Directed contract tests (no live models, no Tier S cycles, no missions):

* A: dev role carries unslop + ui-ux-pro-max (catalog, muse overlay,
  worktree delivery, Claude Skill tool, builder contract);
* C: UI literal-text oracles demote in code; §44, _QUALITY_GUIDE and
  the Tier S planner prompt carry the new rules;
* D: visual verdict parsing/prompt/providers, pixel comparison, fix
  tickets after one round, budget and mission close guards.

The UI gate itself is covered by tests/test_ui_gate_896.py.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO = Path(__file__).resolve().parents[1]
for _path in (REPO / "openceo-core", REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agents.dev.builder_contract import engineering_standard_prompt  # noqa: E402
from agents.dev.tier_oracle import (  # noqa: E402
    demoted_ui_refs,
    freeze_oracle,
    is_ui_literal_test,
    oracle_run_plan,
    validate_ui_tests_config,
)
from catalog import load_catalog, load_role_profile  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import visual_judge as judge_mod  # noqa: E402
from objectives.engine import ObjectiveEnv, run_final_review  # noqa: E402
from orchestration.role_claude import (  # noqa: E402
    CLAUDE_BUILTIN_TOOLS,
    render_allowed_tools,
)
from orchestration.role_muse import render_activation  # noqa: E402
from orchestration.role_skills import (  # noqa: E402
    SKILL_CONTRACT_PATH,
    role_skill_sources,
    stage_role_skills,
)
from test_company_objectives import (  # noqa: E402
    _ExplodingExecutor,
    _child,
    _env_for,
    _mirrored,
    _scratch_repo,
)

POLICY = REPO / "openceo-core" / "objectives" / "routing_policy.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


class DevSkillsCatalogTests(unittest.TestCase):
    def test_card_and_profile_carry_both_ui_skills(self) -> None:
        catalog = load_catalog()
        self.assertEqual(["ui-ux-pro-max", "unslop"],
                         sorted(catalog.agents["dev"].skills))
        profile = load_role_profile("dev")
        self.assertEqual(["create-skill", "git", "ui-ux-pro-max", "unslop"],
                         sorted(profile.skills))

    def test_skill_sources_resolve_on_disk(self) -> None:
        sources = role_skill_sources("dev")
        self.assertEqual({"unslop", "ui-ux-pro-max"}, set(sources))
        for source in sources.values():
            self.assertTrue((source / "SKILL.md").is_file())

    def test_muse_overlay_keeps_ui_skills_and_drops_the_rest(self) -> None:
        profile = load_role_profile("dev")
        off = {
            "bundled": ["bundled://muse-core/skills/git/SKILL.md"],
            "user": ["$HOME/.agents/skills/agent-browser/SKILL.md"],
            "plugin": [],
            "projects": {"/w": [
                {"id": "unslop",
                 "path": ".codex/skills/unslop/SKILL.md"},
                {"id": "ui-ux-pro-max",
                 "path": ".codex/skills/ui-ux-pro-max/SKILL.md"},
                {"id": "notion-api-local",
                 "path": ".codex/skills/notion-api-local/SKILL.md"},
            ]},
        }
        activation = render_activation(off, allowed=list(profile.skills))
        self.assertEqual(
            "off", activation["user"][
                "$HOME/.agents/skills/agent-browser/SKILL.md"])
        self.assertNotIn(
            "bundled://muse-core/skills/git/SKILL.md",
            activation.get("bundled", {}))
        projects = activation.get("projects", {}).get("/w", {})
        self.assertNotIn(".codex/skills/unslop/SKILL.md", projects)
        self.assertNotIn(".codex/skills/ui-ux-pro-max/SKILL.md", projects)
        self.assertEqual("off",
                         projects[".codex/skills/notion-api-local/SKILL.md"])


class RoleSkillsDeliveryTests(unittest.TestCase):
    def test_staging_keeps_git_status_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "t@example.invalid")
            (repo / "tracked.txt").write_text("base\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "base")
            staged = stage_role_skills(repo)
            self.assertEqual(["ui-ux-pro-max", "unslop"], staged)
            for parent in (".codex/skills", ".claude/skills"):
                for skill in ("unslop", "ui-ux-pro-max"):
                    skill_file = repo / parent / skill / "SKILL.md"
                    self.assertTrue(skill_file.is_file(), skill_file)
            status = subprocess.run(
                ["git", "status", "--short"], cwd=repo, check=True,
                capture_output=True, text=True).stdout
            self.assertEqual("", status)
            # Second run is a no-op (fingerprint match), still clean.
            self.assertEqual(staged, stage_role_skills(repo))
            status = subprocess.run(
                ["git", "status", "--short"], cwd=repo, check=True,
                capture_output=True, text=True).stdout
            self.assertEqual("", status)

    def test_staged_copies_are_read_only(self) -> None:
        import os
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            stage_role_skills(repo)
            target = repo / ".codex" / "skills" / "unslop" / "SKILL.md"
            mode = target.stat().st_mode
            self.assertFalse(mode & (stat.S_IWUSR | stat.S_IWGRP
                                     | stat.S_IWOTH))
            self.assertTrue(os.access(target, os.R_OK))

    def test_foreign_paths_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            foreign = repo / ".codex" / "skills" / "unslop"
            foreign.mkdir(parents=True)
            (foreign / "mine.txt").write_text("repo-owned\n")
            staged = stage_role_skills(repo)
            self.assertEqual(
                "repo-owned\n", (foreign / "mine.txt").read_text())
            self.assertFalse((foreign / ".openceo-staged").exists())
            # The skill still lands where it fits.
            self.assertIn("unslop", staged)
            self.assertTrue((repo / ".claude" / "skills" / "unslop"
                             / "SKILL.md").is_file())

    def test_contract_path_matches_staged_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            stage_role_skills(repo)
            for skill in ("unslop", "ui-ux-pro-max"):
                path = SKILL_CONTRACT_PATH.format(skill=skill)
                self.assertTrue((repo / path).is_file(), path)


class ClaudeSkillToolTests(unittest.TestCase):
    def test_skill_is_preapproved(self) -> None:
        self.assertIn("Skill", CLAUDE_BUILTIN_TOOLS)
        self.assertIn("Skill", render_allowed_tools(["openceo_get_ticket"]))


class BuilderContractUiTests(unittest.TestCase):
    def test_contract_carries_the_ui_bullet(self) -> None:
        text = engineering_standard_prompt()
        self.assertIn(".codex/skills/unslop/SKILL.md", text)
        self.assertIn(".codex/skills/ui-ux-pro-max/SKILL.md", text)
        self.assertIn("just do it", text)
        self.assertIn("documento de diseño del repo > unslop > "
                      "ui-ux-pro-max", text)
        self.assertIn("gate ui_gate", text)
        self.assertIn("Nunca uses Playwright", text)
        self.assertIn("agent-browser", text)

    def test_contract_cites_00734_superseded_on_both_points(self) -> None:
        import agents.dev.builder_contract as contract_mod

        self.assertIn(
            "supersedes TASK-2026-00734 on UI skills and the visual verdict",
            (contract_mod.__doc__ or "").replace("\n", " "))

    def test_simple_tier_prompt_carries_the_bullet(self) -> None:
        from agents.dev.session import DevSessionManager

        manager = DevSessionManager(
            SimpleNamespace(DEV_EXECUTION_MODE="cli"), MagicMock(), repo=None)
        prompt = manager._build_coding_prompt(
            {"task_id": "t", "title": "probe", "description": "d",
             "params": {}},
            {"project": {"repos": []}, "dev": {}})
        self.assertIn(".codex/skills/unslop/SKILL.md", prompt)

    def test_demoted_oracles_land_in_the_prompt(self) -> None:
        from agents.dev.session import DevSessionManager

        manager = DevSessionManager(
            SimpleNamespace(DEV_EXECUTION_MODE="cli"), MagicMock(), repo=None)
        prompt = manager._build_coding_prompt(
            {"task_id": "t", "title": "probe", "description": "d",
             "params": {"_ui_oracle_demoted": ["apps/web/test/a.test.tsx"]}},
            {"project": {"repos": []}, "dev": {}})
        self.assertIn("ORÁCULOS UI DEGRADADOS A EVIDENCIA", prompt)
        self.assertIn("apps/web/test/a.test.tsx", prompt)
        self.assertIn("comportamiento", prompt)


class UiOracleDemotionTests(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        (repo / "apps" / "web" / "test").mkdir(parents=True)
        (repo / "apps" / "web" / "e2e").mkdir(parents=True)
        (repo / "apps" / "api").mkdir(parents=True)
        (repo / "apps" / "web" / "test" / "literal.test.tsx").write_text(
            "expect(screen.getByText('Fuente: GET /x')).toBeVisible();\n")
        (repo / "apps" / "web" / "test" / "content.test.tsx").write_text(
            "expect(el).toHaveTextContent('Panel inicial');\n")
        (repo / "apps" / "web" / "e2e" / "contain.spec.ts").write_text(
            "expect(text).toContain('Guardar');\n")
        (repo / "apps" / "web" / "test" / "role.test.tsx").write_text(
            "await screen.getByRole('button', {name: 'Guardar'});\n")
        (repo / "apps" / "web" / "test" / "regex.test.tsx").write_text(
            "screen.getByText(/guard/i);\n")
        (repo / "apps" / "api" / "logic.test.ts").write_text(
            "screen.getByText('Algo');\n")
        return repo

    def test_literal_assertions_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            paths = ["apps/web/test/", "apps/web/e2e/"]
            for name in ("apps/web/test/literal.test.tsx",
                         "apps/web/test/content.test.tsx",
                         "apps/web/e2e/contain.spec.ts"):
                self.assertTrue(is_ui_literal_test(repo, name, paths), name)

    def test_behavior_and_outside_files_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            paths = ["apps/web/test/", "apps/web/e2e/"]
            for name in ("apps/web/test/role.test.tsx",
                         "apps/web/test/regex.test.tsx",
                         "apps/api/logic.test.ts"):
                self.assertFalse(is_ui_literal_test(repo, name, paths), name)
            self.assertFalse(is_ui_literal_test(
                repo, "apps/web/test/literal.test.tsx", None))

    def test_freeze_demotes_ui_refs_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            oracle = {"type": "preexisting_tests", "refs": [
                {"ref": "apps/web/test/literal.test.tsx"},
                {"ref": "apps/web/test/role.test.tsx"},
            ]}
            frozen = freeze_oracle(
                repo, "base", oracle, "t",
                ["apps/web/test/", "apps/web/e2e/"])
            assert frozen is not None
            by_ref = {entry["ref"]: entry for entry in frozen["refs"]}
            self.assertFalse(by_ref["apps/web/test/literal.test.tsx"][
                "resolved"])
            self.assertEqual("ui_literal", by_ref[
                "apps/web/test/literal.test.tsx"]["demoted"])
            self.assertTrue(
                by_ref["apps/web/test/role.test.tsx"]["resolved"])
            self.assertEqual(["apps/web/test/literal.test.tsx"],
                             demoted_ui_refs(frozen))

    def test_run_plan_names_the_demotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            oracle = {"type": "preexisting_tests", "refs": [
                {"ref": "apps/web/test/literal.test.tsx"},
            ]}
            frozen = freeze_oracle(
                repo, "base", oracle, "t", ["apps/web/test/"])
            run, note = oracle_run_plan(
                frozen, {"oracle_cmd": "bun test {refs}"})
            self.assertIsNone(run)
            self.assertIn("demoted to evidence", note)

    def test_ui_tests_config_validation(self) -> None:
        self.assertEqual([], validate_ui_tests_config({}))
        self.assertEqual(
            [], validate_ui_tests_config({"ui_tests": ["apps/web/test/"]}))
        self.assertTrue(validate_ui_tests_config({"ui_tests": []}))
        self.assertTrue(validate_ui_tests_config({"ui_tests": ["../x"]}))


class PolicySection44Tests(unittest.TestCase):
    def test_section_44_carries_the_new_rules(self) -> None:
        text = POLICY.read_text(encoding="utf-8")
        self.assertIn("never qualifies as a frozen", text)
        self.assertIn("verifies it through", text)
        self.assertIn("one visual verdict at objective close", text)
        self.assertIn("pinned role skills first", text)
        self.assertIn("shell first", text)
        self.assertIn("own codemod", text)
        self.assertIn("out of the navigation until it exists", text)
        self.assertIn("proposes one first, once", text)
        self.assertIn("superseding\n  TASK-2026-00734 on UI skills and "
                      "the visual verdict", text)

    def test_quality_guide_carries_the_new_rules(self) -> None:
        prompt = engine_mod._planner_prompt({"anchor": {}}, "", [], "", [])
        self.assertIn("never qualifies as a", prompt)
        self.assertIn("one visual verdict", prompt)
        self.assertIn("Shell first", prompt)
        self.assertIn("00896 supersedes 00734", prompt)

    def test_tier_s_planner_prompt_carries_the_new_rules(self) -> None:
        from agents.dev.tier_s.cycle import TierSCycle, _Ctx
        from agents.dev.tier_s.stores import (
            InMemoryLockStore,
            InMemoryRunStore,
            InMemoryTaskStore,
        )

        cycle = TierSCycle(InMemoryRunStore(), InMemoryLockStore(),
                           InMemoryTaskStore(), MagicMock())
        ctx = _Ctx(
            ticket={"task_id": "t", "ref": "t", "title": "probe",
                    "description": "d", "params": {}},
            repo=REPO, ref="t", run_id="run", branch="b",
            base_branch="main", budgets={}, timeouts={}, waiting_cfg={},
            auto_merge=False, budget_exclude=[], gate_specs=[], roles={})
        prompt = cycle._planner_prompt(ctx)
        self.assertIn("pinned UI skills (unslop", prompt)
        self.assertIn("one ", prompt)
        self.assertIn("visual verdict at objective close", prompt)
        self.assertIn("TASK-2026-00896, superseding TASK-2026-00734", prompt)


class VerdictParsingTests(unittest.TestCase):
    def test_approved_parses(self) -> None:
        verdict = judge_mod.parse_verdict(
            '{"approved": true, "defects": []}')
        self.assertTrue(verdict.approved)
        self.assertEqual([], verdict.defects)

    def test_failed_parses_with_defects(self) -> None:
        verdict = judge_mod.parse_verdict(
            'Intro {"approved": false, "defects": ['
            '{"where": "nav", "what": "sin iconos", "rule": "unslop-3"}'
            ']} tail')
        self.assertFalse(verdict.approved)
        self.assertEqual("nav", verdict.defects[0]["where"])

    def test_defects_capped_at_three(self) -> None:
        defects = ", ".join(
            '{"where": "a", "what": "b", "rule": "c"}' for _ in range(5))
        verdict = judge_mod.parse_verdict(
            f'{{"approved": false, "defects": [{defects}]}}')
        self.assertEqual(3, len(verdict.defects))

    def test_malformed_rejected(self) -> None:
        for text in ("no json here",
                     '{"approved": "yes"}',
                     '{"approved": false, "defects": []}',
                     '{"approved": false, "defects": [{"where": "a"}]}'):
            with self.assertRaises(ValueError, msg=text):
                judge_mod.parse_verdict(text)


class JudgePromptTests(unittest.TestCase):
    def test_prompt_carries_rubric_and_json_contract(self) -> None:
        rubrics = judge_mod.load_rubrics()
        self.assertIn("4px", rubrics["unslop"])
        self.assertIn("touch targets", rubrics["ui_ux_pro_max"])
        prompt = judge_mod.build_judge_prompt(
            "/dashboard", rubrics, "Inter + Azul Jardal")
        self.assertIn("/dashboard", prompt)
        self.assertIn("RÚBRICA UNSLOP", prompt)
        self.assertIn("Inter + Azul Jardal", prompt)
        self.assertIn('"approved"', prompt)
        self.assertIn("Máximo 3 defectos", prompt)

    def test_design_summary_missing_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(judge_mod.design_summary(Path(tmp)))
            (Path(tmp) / "DESIGN.md").write_text("# Doc\nSin sección.\n")
            self.assertIsNone(judge_mod.design_summary(Path(tmp)))

    def test_design_summary_capped_at_30_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = "\n".join(f"línea {index}" for index in range(50))
            (Path(tmp) / "DESIGN.md").write_text(
                f"# Doc\n\n## Resumen para el juez\n{body}\n")
            summary = judge_mod.design_summary(Path(tmp))
            assert summary is not None
            self.assertLessEqual(len(summary.splitlines()), 31)


class JudgeProvidersTests(unittest.TestCase):
    def test_muse_argv_attaches_images(self) -> None:
        argv = judge_mod.judge_argv_muse(["a.png", "b.png"])
        self.assertEqual(["muse", "exec", "--model",
                          judge_mod.MUSE_JUDGE_MODEL,
                          "--reasoning-effort", "max",
                          "--image", "a.png", "--image", "b.png"], argv)

    def test_agy_argv_embeds_captures_and_flags_first(self) -> None:
        self.assertTrue(judge_mod.AGY_IMAGE_VIA_TOOLS)
        argv = judge_mod.judge_argv_agy("prompt", "agyj")
        self.assertEqual("agyj", argv[0])
        self.assertLess(argv.index("--print"), len(argv) - 1)
        self.assertEqual("prompt", argv[-1])
        with self.assertRaises(ValueError):
            judge_mod.judge_argv_agy("prompt", "nope")
        refs = judge_mod.judge_capture_refs(["a.png", "b.png"])
        self.assertIn("@a.png (absolute path a.png)", refs)


class RunVerdictTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_approval_wins_on_first_account(self) -> None:
        seen: list[str] = []

        async def fake_cli(argv, cwd, timeout):
            seen.append(argv[0])
            return (0, '{"approved": true, "defects": []}',
                    {"tokens_in": 4000, "tokens_out": 120})

        record = await judge_mod.run_verdict(
            "/dashboard", ["a.png", "b.png"], "prompt", "/tmp",
            run_cli=fake_cli)
        self.assertEqual("gemini:agyd", record["provider"])
        self.assertTrue(record["approved"])
        self.assertEqual(4000, record["tokens_in"])
        self.assertEqual(120, record["tokens_out"])
        self.assertEqual(["agyd"], seen)

    async def test_busy_account_falls_to_next_then_muse(self) -> None:
        seen: list[str] = []

        async def fake_cli(argv, cwd, timeout):
            seen.append(argv[0])
            if argv[0] in ("agyd", "agyj", "agy2"):
                return (1, "busy", {})
            self.assertIn("--image", argv)
            return (0, '{"approved": true, "defects": []}', {})

        record = await judge_mod.run_verdict(
            "/dashboard", ["a.png"], "prompt", "/tmp",
            run_cli=fake_cli)
        self.assertEqual("muse-review", record["provider"])
        self.assertEqual(["agyd", "agyj", "agy2", "muse"], seen)

    async def test_malformed_output_falls_through_to_error(self) -> None:
        async def fake_cli(argv, cwd, timeout):
            return (0, "not json", {})

        with self.assertRaises(ValueError):
            await judge_mod.run_verdict(
                "/dashboard", ["a.png"], "prompt", "/tmp",
                run_cli=fake_cli)


def _solid_png(path: Path, width: int, height: int,
               rgb: tuple[int, int, int]) -> None:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                    0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PixelCompareTests(unittest.TestCase):
    def test_identical_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            _solid_png(first, 8, 8, (10, 20, 30))
            _solid_png(second, 8, 8, (10, 20, 30))
            self.assertEqual(
                0.0, judge_mod.changed_ratio_png(first, second))

    def test_different_is_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            _solid_png(first, 8, 8, (10, 20, 30))
            _solid_png(second, 8, 8, (200, 20, 30))
            self.assertGreater(
                judge_mod.changed_ratio_png(first, second),
                judge_mod.VISUAL_CHANGE_THRESHOLD)

    def test_size_drift_counts_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            _solid_png(first, 8, 8, (10, 20, 30))
            _solid_png(second, 9, 8, (10, 20, 30))
            self.assertEqual(
                1.0, judge_mod.changed_ratio_png(first, second))

    def test_changed_and_pending_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            head = Path(tmp) / "head"
            base.mkdir()
            head.mkdir()
            _solid_png(base / "dashboard-1440-light.png", 8, 8, (1, 1, 1))
            _solid_png(base / "dashboard-390-dark.png", 8, 8, (1, 1, 1))
            _solid_png(head / "dashboard-1440-light.png", 8, 8, (9, 9, 9))
            _solid_png(head / "dashboard-390-dark.png", 8, 8, (1, 1, 1))
            cfg = {"routes": ["/dashboard"], "widths": [1440, 390],
                   "themes": ["light", "dark"]}
            baseline = {"captures": {
                "/dashboard|1440|light": judge_mod.sha256_file(
                    base / "dashboard-1440-light.png"),
                "/dashboard|390|dark": judge_mod.sha256_file(
                    base / "dashboard-390-dark.png")}}
            self.assertEqual(
                ["/dashboard"],
                judge_mod.changed_views(cfg, baseline, head, base))
            pending = judge_mod.pending_views(["/dashboard"], [])
            self.assertEqual([{"view": "/dashboard", "round": 1}], pending)
            pending = judge_mod.pending_views(["/dashboard"], [{
                "view": "/dashboard", "round": 1, "approved": False,
                "defects": []}])
            self.assertEqual([{"view": "/dashboard", "round": 2}], pending)
            pending = judge_mod.pending_views(["/dashboard"], [{
                "view": "/dashboard", "round": 2, "approved": False,
                "defects": []}])
            self.assertEqual([], pending)
            self.assertEqual(
                ["/dashboard"],
                judge_mod.final_failed_views([{
                    "view": "/dashboard", "round": 2, "approved": False,
                    "defects": []}]))


def _visual_env(test: unittest.TestCase, repo: Path,
                objective: dict, children: list[dict],
                ui_gate: dict | None):
    from test_company_objectives import _plan_result

    tasks = {"task-parent": _mirrored(
        "task-parent", "TASK-1", status="converted",
        objective_id=objective["objective_id"])}
    tasks.update({str(child["task_id"]): child for child in children})
    env, _planner, created, _patches, notices = _env_for(
        test, repo, [_plan_result()], tasks=tasks,
        objectives_cfg={"max_final_reviews": 2})
    env.project_qa = {"ui_gate": ui_gate} if ui_gate else {}
    return env, created, notices


_UI_GATE_MIN = {
    "preview_cmd": "true",
    "url": "http://127.0.0.1:1",
    "routes": ["/dashboard"],
    "widths": [1440, 390],
    "themes": ["light", "dark"],
}


class VisualReviewStepTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, env, **overrides) -> dict:
        doc = {
            "objective_id": "OBJ-V", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
            "final_reviews": 0, "base_sha": "abc123",
        }
        doc.update(overrides)
        await env.objectives.create_objective(doc)
        stored = await env.objectives.get_objective("OBJ-V")
        assert stored is not None
        return stored

    async def test_skips_without_ui_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _created, _notices = _visual_env(
                self, repo, {"objective_id": "OBJ-V"},
                [_child("kid-1", "completed", dev=True)], None)
            objective = await self._objective(env)
            with patch.object(
                    judge_mod, "run_gate_worktree",
                    new=AsyncMock()) as gate:
                result = await engine_mod._visual_review_step(
                    env, objective, [], {"dev_children": 1}, 0)
            self.assertIsNone(result)
            gate.assert_not_awaited()

    async def test_failed_round_one_creates_fix_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, created, _notices = _visual_env(
                self, repo, {"objective_id": "OBJ-V"},
                [_child("kid-1", "completed", dev=True)], _UI_GATE_MIN)
            objective = await self._objective(env)

            async def fake_gate(repo_p, sha, tag, cfg, out_dir, ticket):
                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                color = (5, 5, 5) if tag == "base" else (9, 9, 9)
                _solid_png(out / "dashboard-1440-light.png", 8, 8, color)
                _solid_png(out / "dashboard-390-dark.png", 8, 8, (1, 1, 1))
                return {"verdict": "pass", "results": []}

            async def fake_verdict(view, images, prompt, workdir, **kwargs):
                return {"view": view, "provider": "muse-review",
                        "approved": False,
                        "defects": [{"where": "nav", "what": "sin iconos",
                                     "rule": "unslop-3"}],
                        "tokens_in": 1, "tokens_out": 1,
                        "at": "2026-11-22T00:00:00+00:00"}

            planned = AsyncMock(return_value={"ok": True, "planned": True})
            with patch.object(judge_mod, "run_gate_worktree",
                              new=fake_gate), \
                    patch.object(judge_mod, "run_verdict",
                                 new=fake_verdict), \
                    patch.object(engine_mod, "plan_objective", new=planned), \
                    patch.object(engine_mod, "_integration_head",
                                 return_value="abc123"):
                result = await engine_mod._visual_review_step(
                    env, objective, [], {"dev_children": 1}, 0)
            self.assertEqual({"ok": True, "planned": True}, result)
            self.assertEqual(1, len(created))
            payload = created[0]
            self.assertEqual("visual_judge", payload["origin"])
            self.assertEqual("tier_c", payload["coding_tool"])
            self.assertIn("/dashboard", payload["title"])
            self.assertTrue(payload["autorun"])
            stored = await env.objectives.get_objective("OBJ-V")
            assert stored is not None
            self.assertEqual("active", stored["status"])
            self.assertEqual(1, len(stored["ui_verdicts"]))
            self.assertEqual(1, stored["ui_verdicts"][0]["round"])
            planned.assert_awaited_once()

    async def test_approved_verdicts_continue_to_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, created, _notices = _visual_env(
                self, repo, {"objective_id": "OBJ-V"},
                [_child("kid-1", "completed", dev=True)], _UI_GATE_MIN)
            objective = await self._objective(env)

            async def fake_gate(repo_p, sha, tag, cfg, out_dir, ticket):
                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                color = (5, 5, 5) if tag == "base" else (9, 9, 9)
                _solid_png(out / "dashboard-1440-light.png", 8, 8, color)
                _solid_png(out / "dashboard-390-dark.png", 8, 8, (1, 1, 1))
                return {"verdict": "pass", "results": []}

            async def fake_verdict(view, images, prompt, workdir, **kwargs):
                return {"view": view, "provider": "muse-review",
                        "approved": True, "defects": [],
                        "tokens_in": 1, "tokens_out": 1,
                        "at": "2026-11-22T00:00:00+00:00"}

            with patch.object(judge_mod, "run_gate_worktree",
                              new=fake_gate), \
                    patch.object(judge_mod, "run_verdict",
                                 new=fake_verdict), \
                    patch.object(engine_mod, "_integration_head",
                                 return_value="abc123"):
                result = await engine_mod._visual_review_step(
                    env, objective, [], {"dev_children": 1}, 0)
            self.assertIsNone(result)
            self.assertEqual([], created)
            stored = await env.objectives.get_objective("OBJ-V")
            assert stored is not None
            self.assertTrue(stored["ui_verdicts"][0]["approved"])

    async def test_second_failure_closes_with_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, created, _notices = _visual_env(
                self, repo, {"objective_id": "OBJ-V"},
                [_child("kid-1", "completed", dev=True)], _UI_GATE_MIN)
            objective = await self._objective(env, ui_verdicts=[{
                "view": "/dashboard", "round": 1, "approved": False,
                "defects": [{"where": "a", "what": "b", "rule": "c"}],
                "provider": "muse-review", "tokens_in": 1,
                "tokens_out": 1, "at": "x",
                "captures": ["old", "old"]}],
                ui_baseline={"sha": "abc123", "captures": {}})

            async def fake_gate(repo_p, sha, tag, cfg, out_dir, ticket):
                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                color = (5, 5, 5) if tag == "base" else (9, 9, 9)
                _solid_png(out / "dashboard-1440-light.png", 8, 8, color)
                _solid_png(out / "dashboard-390-dark.png", 8, 8, (1, 1, 1))
                return {"verdict": "pass", "results": []}

            async def fake_verdict(view, images, prompt, workdir, **kwargs):
                return {"view": view, "provider": "muse-review",
                        "approved": False,
                        "defects": [{"where": "a", "what": "b",
                                     "rule": "c"}],
                        "tokens_in": 1, "tokens_out": 1,
                        "at": "2026-11-22T00:00:00+00:00"}

            with patch.object(judge_mod, "run_gate_worktree",
                              new=fake_gate), \
                    patch.object(judge_mod, "run_verdict",
                                 new=fake_verdict), \
                    patch.object(engine_mod, "_integration_head",
                                 return_value="abc123"):
                result = await engine_mod._visual_review_step(
                    env, objective, [], {"dev_children": 1}, 0)
            self.assertIsNone(result)
            self.assertEqual([], created)
            stored = await env.objectives.get_objective("OBJ-V")
            assert stored is not None
            self.assertEqual(2, stored["ui_verdicts"][-1]["round"])
            self.assertEqual(
                ["/dashboard"],
                judge_mod.final_failed_views(stored["ui_verdicts"]))


class BudgetSpentVisualGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_spent_budget_runs_the_judge_first(self) -> None:
        tasks = {"task-parent": _mirrored(
            "task-parent", "TASK-1", status="converted",
            objective_id="OBJ-1")}
        tasks["kid-1"] = _child("kid-1", "completed", dev=True)
        env, _planner, _created, _patches, _notices = _env_for(
            self, _scratch_repo(self), [], tasks=tasks,
            objectives_cfg={"max_final_reviews": 2})
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.project_qa = {"ui_gate": dict(_UI_GATE_MIN)}
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
            "final_reviews": 2,
        })
        visual = AsyncMock(return_value={"ok": True, "action": "visual"})
        with patch.object(engine_mod, "_visual_review_step", new=visual):
            result = await run_final_review(env, "OBJ-1")
        self.assertEqual({"ok": True, "action": "visual"}, result)
        visual.assert_awaited_once()
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertNotEqual("closed", stored["status"])


class MissionVisualGateTests(unittest.IsolatedAsyncioTestCase):
    async def _mission_env(self):
        from objectives.missions import (
            InMemoryMissionStore,
            MissionEnv,
            MissionRejected,
        )
        from objectives.engine import InMemoryObjectiveStore

        missions = InMemoryMissionStore()
        objectives = InMemoryObjectiveStore()
        notices: list[tuple[str, str]] = []

        async def notify(ref: str, text: str) -> None:
            notices.append((ref, text))

        env = MissionEnv(
            missions=missions, objectives=objectives,
            tasks=MagicMock(), create_ticket=AsyncMock(),
            patch_task=AsyncMock(), notify=notify)
        return env, missions, objectives, notices, MissionRejected

    async def test_close_blocked_on_failed_verdict(self) -> None:
        from objectives import missions as missions_mod

        env, missions, objectives, notices, MissionRejected = (
            await self._mission_env())
        await missions.create_mission({
            "mission_id": "MIS-1", "status": "active",
            "goal": "g", "acceptance": ["ship"],
            "conversion_source_task_id": "task-shell",
            "objectives": [{"objective_id": "OBJ-1",
                            "status": "closed"}]})
        await objectives.create_objective({
            "objective_id": "OBJ-1", "status": "closed",
            "delivery": {"ui_verdicts": [{
                "view": "/dashboard", "round": 2, "approved": False,
                "defects": []}]}})
        with self.assertRaises(MissionRejected):
            await missions_mod.close_mission(env, "MIS-1")
        mission = await missions.get_mission("MIS-1")
        assert mission is not None
        self.assertEqual("active", mission["status"])
        self.assertIn("visual_block", mission)
        self.assertTrue(any("visual" in text for _ref, text in notices))

    async def test_close_passes_on_approved_verdict(self) -> None:
        from objectives import missions as missions_mod

        env, missions, objectives, _notices, _rejected = (
            await self._mission_env())
        await missions.create_mission({
            "mission_id": "MIS-1", "status": "active",
            "goal": "g", "acceptance": ["ship"],
            "conversion_source_task_id": "task-shell",
            "objectives": [{"objective_id": "OBJ-1",
                            "status": "closed"}]})
        await objectives.create_objective({
            "objective_id": "OBJ-1", "status": "closed",
            "delivery": {"ui_verdicts": [{
                "view": "/dashboard", "round": 1, "approved": True,
                "defects": []}]}})
        env.tasks.get_task = AsyncMock(return_value=None)
        result = await missions_mod.close_mission(env, "MIS-1")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
