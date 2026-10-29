"""Autonomous company CEO (TASK-2026-00119, F4; TASK-2026-00213).

Acceptance with a simulated Muse run (recorded JSON, zero tokens): a
review that creates nothing when nothing is needed; one that emits a
pedido the simulated single planner then decides; a rejected
duplicate; an escalated cost case. Plus the decision contract, the
deterministic anti-bloat rules, the no-mandate path (zero model
runs), retry/park behavior and the Horizon status merge. No network,
no CLI, no LLM.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo import models as ceo_models
from ceo.loop import CEOLoop  # noqa: F401 - import surface guard.
from ceo.models import (
    CEOAction,
    CEODecision,
    CEOKpiReading,
    CEOValidationContext,
    DUPLICATE_ESCALATION_PREFIX,
    normalize_title,
    parse_ceo_decision,
    validate_ceo_decision,
)
from ceo.review import (
    CEOEnv,
    compile_signals,
    run_review,
)
from ceo.service import CEOService, ceo_wiring_config
from company_mandate import parse_mandate
from config_loader import ConfigLoader
from objectives import engine as engine_mod
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
)
from objectives.pedido import is_pedido
from settings import Settings


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo,
                   check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _mandate(**overrides: Any):
    data: dict[str, Any] = {
        "version": 1,
        "ejes": [
            {
                "id": "crecimiento",
                "meta": "Crecer de forma sostenible",
                "kpis": [
                    {"id": "tickets_abiertos", "fuente": "erpnext.open"},
                    {"id": "objetivos_activos",
                     "fuente": "objectives.active"},
                ],
            },
            {
                "id": "vacio",
                "meta": "Eje sin KPI declarado",
                "kpis": [],
            },
        ],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "0.00",
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            "gasto_nuevo",
            "accion_irreversible",
            "replanificacion_estrategica",
            "cambio_de_mandato",
            "revision_exigida_por_politica",
        ],
    }
    data.update(overrides)
    return parse_mandate(data)


def _ctx(**overrides: Any) -> CEOValidationContext:
    base: dict[str, Any] = {
        "mandate": _mandate(),
        "known_projects": ["ops", "shop"],
        "open_titles": [],
        "active_objectives": [],
        "open_escalations": [],
        "kpi_readings": {
            "crecimiento:tickets_abiertos": CEOKpiReading(
                value=12, unknown=False),
            "crecimiento:objetivos_activos": CEOKpiReading(
                value=1, unknown=False),
        },
    }
    base.update(overrides)
    return CEOValidationContext(**base)


class _RecordedCEO:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, repo: Path, payloads: list[Any]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "ceo_reviewer", role
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        target = self._repo / ".openceo" / "CEO_DECISION.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
        else:
            target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="CEO_READY")

    async def cleanup(self, role: str) -> None:
        del role


class _ExplodingExecutor:
    """Fails the test if any role runs (proves the deterministic path)."""

    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del prompt, workdir, timeout_s
        self.runs.append(role)
        raise AssertionError(f"no executor runs expected, got {role!r}")

    async def cleanup(self, role: str) -> None:
        del role


def _output_target(repo: Path, prompt: str) -> Path:
    """OUTPUT path the prompt orders (the honest planner follows it)."""
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    """Simulated planner for the objective-creation acceptance."""

    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.runs = 0
        self._target = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner", role
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            # Retry prompt: fix the same file in place.
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _dev_ticket(local_ref: str, title: str) -> dict[str, Any]:
    return {
        "local_ref": local_ref,
        "title": title,
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": f"achieve {title}",
        "why_now": "next step",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_b",
        "tier_reason": "bounded work",
        "why_not_c": "a misjudged step ships wrong behavior",
    }


def _make_env(test: unittest.TestCase, repo: Path,
              **overrides: Any) -> tuple[CEOEnv, dict[str, Any]]:
    """CEOEnv with inert fakes; ``overrides`` replaces any field."""
    calls: dict[str, Any] = {
        "created_tickets": [],
        "escalations": [],
        "published": [],
    }

    async def _none(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return None

    async def _empty_list(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        return []

    async def _business() -> dict[str, Any]:
        return {"rows": [], "unknown": False}

    async def _kpi(fuente: str) -> CEOKpiReading:
        del fuente
        return CEOKpiReading(value=None, unknown=True)

    async def _create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        calls["created_tickets"].append(payload)
        return {"task_id": f"task-{len(calls['created_tickets'])}"}

    async def _escalate(**kwargs: Any) -> dict[str, Any]:
        calls["escalations"].append(kwargs)
        return {"task_id": f"esc-{len(calls['escalations'])}"}

    async def _publish(record: dict[str, Any]) -> None:
        calls["published"].append(record)

    fields: dict[str, Any] = {
        "mandate": _mandate(),
        "objectives": InMemoryObjectiveStore(),
        "tasks": InMemoryTaskLister(),
        "open_titles": _empty_list,
        "open_escalations": _empty_list,
        "recently_closed": _empty_list,
        "blocked_tasks": _empty_list,
        "recent_research": _empty_list,
        "business_open": _business,
        "read_kpi": _kpi,
        "known_projects": ["ops", "shop"],
        "ops_project": "ops",
        "create_ticket": _create_ticket,
        "escalate": _escalate,
        "executor": _ExplodingExecutor(),
        "repo_root": repo,
        "publish": _publish,
        "ceo_cfg": {},
        "now": lambda: datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    assert _none is not None  # Placeholder kept explicit.
    return CEOEnv(**fields), calls


def _measured_kpis() -> Any:
    async def _kpi(fuente: str) -> CEOKpiReading:
        if fuente in ("erpnext.open", "objectives.active"):
            return CEOKpiReading(value=3, unknown=False)
        return CEOKpiReading(value=None, unknown=True)

    return _kpi


class DecisionContractTests(unittest.TestCase):
    def test_parse_accepts_all_four_actions(self) -> None:
        bodies = {
            "keep_going": {"action": "keep_going", "summary": "Work suffices"},
            "do_nothing": {"action": "do_nothing", "summary": "Nothing due"},
            "crear_pedido": {
                "action": "crear_pedido", "summary": "Shop needs checkout",
                "pedido": {
                    "project": "shop", "title": "Fix checkout",
                    "description": "The button fails", "eje_id": "crecimiento",
                    "kpi_ids": ["tickets_abiertos"],
                },
            },
            "escalate": {
                "action": "escalate", "summary": "Needs owner",
                "escalation": {
                    "kind": "compra", "summary": "Buy the domain",
                    "amount": "12.00",
                },
            },
        }
        for action, body in bodies.items():
            with self.subTest(action=action):
                decision = parse_ceo_decision(json.dumps(body))
                self.assertEqual(decision.action, CEOAction(action))

    def test_parse_rejects_retired_creation_actions(self) -> None:
        for action in ("create_objective", "create_ticket", "create_project"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError) as raised:
                    parse_ceo_decision(json.dumps(
                        {"action": action, "summary": "Old shape"}))
                self.assertIn("action", str(raised.exception))

    def test_parse_rejects_malformed_output(self) -> None:
        for text in ("", "not json {", "[1, 2]", "null", '"x"'):
            with self.subTest(text=text[:12]):
                with self.assertRaises(ValueError):
                    parse_ceo_decision(text)

    def test_action_spec_mismatch_is_rejected(self) -> None:
        pedido = {
            "project": "shop", "title": "Fix checkout",
            "description": "The button fails", "eje_id": "crecimiento",
            "kpi_ids": ["tickets_abiertos"],
        }
        cases = [
            ({"action": "crear_pedido", "summary": "x"},
             "requires exactly"),
            ({"action": "keep_going", "summary": "x",
              "pedido": pedido}, "carries no spec"),
            ({"action": "do_nothing", "summary": "x",
              "escalation": {"kind": "k", "summary": "s"}},
             "carries no spec"),
            ({"action": "escalate", "summary": "x",
              "pedido": pedido}, "requires exactly"),
            ({"action": "other", "summary": "x"}, "action"),
            ({"action": "keep_going", "summary": "  "},
             "must not be blank"),
        ]
        for body, fragment in cases:
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as raised:
                    parse_ceo_decision(json.dumps(body))
                self.assertIn(fragment, str(raised.exception))

    def test_pedido_project_key_travels_with_display_name(self) -> None:
        base = {
            "project": "ops", "title": "New shop",
            "description": "Open a second shop", "eje_id": "crecimiento",
            "kpi_ids": ["tickets_abiertos"],
        }
        for spec in (
            {**base, "project_key": "tienda"},
            {**base, "display_name": "Tienda"},
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as raised:
                    parse_ceo_decision(json.dumps(
                        {"action": "crear_pedido", "summary": "x",
                         "pedido": spec}))
                self.assertIn("travel together", str(raised.exception))

    def test_pedido_costs_follow_planner_money_rules(self) -> None:
        base = {
            "project": "shop", "title": "Fix checkout",
            "description": "The button fails", "eje_id": "crecimiento",
            "kpi_ids": ["tickets_abiertos"],
        }
        ok = parse_ceo_decision(json.dumps(
            {"action": "crear_pedido", "summary": "x",
             "pedido": {**base, "costs": [
                 {"label": "domain", "amount": "12.00"},
                 {"label": "vendor quote", "amount": None},
             ]}}))
        assert ok.pedido is not None
        self.assertEqual(len(ok.pedido.costs), 2)
        for amount in (10.5, True, "-3.00", "not money"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    parse_ceo_decision(json.dumps(
                        {"action": "crear_pedido", "summary": "x",
                         "pedido": {**base, "costs": [
                             {"label": "bad", "amount": amount}]}}))

    def test_escalation_amount_accepts_json_numbers(self) -> None:
        body = {
            "action": "escalate", "summary": "Needs owner",
            "escalation": {
                "kind": "compra", "summary": "Buy hosting",
                "amount": 10.5,
            },
        }
        decision = parse_ceo_decision(json.dumps(body))
        assert decision.escalation is not None
        self.assertEqual(decision.escalation.amount, "10.5")

    def test_escalation_amount_rejects_non_finite_and_bool(self) -> None:
        for amount in (True, "  "):
            with self.subTest(amount=amount):
                body = {
                    "action": "escalate", "summary": "Needs owner",
                    "escalation": {
                        "kind": "compra", "summary": "Buy hosting",
                        "amount": amount,
                    },
                }
                with self.assertRaises(ValueError):
                    parse_ceo_decision(json.dumps(body))
        with self.assertRaises(ValueError):
            parse_ceo_decision(
                '{"action": "escalate", "summary": "x", '
                '"escalation": {"kind": "k", "summary": "s", '
                '"amount": NaN}}'
            )

    def test_normalize_title_matches_planner_rule(self) -> None:
        self.assertEqual(normalize_title("  Vender Más "), "vender más")
        self.assertEqual(normalize_title(None), "")


def _pedido_decision(**overrides: Any) -> CEODecision:
    spec: dict[str, Any] = {
        "project": "shop",
        "title": "Fix the checkout button",
        "description": "The button double-charges",
        "eje_id": "crecimiento",
        "kpi_ids": ["tickets_abiertos"],
    }
    spec.update(overrides.pop("spec", {}))
    body: dict[str, Any] = {
        "action": "crear_pedido", "summary": "Shop needs checkout",
        "pedido": spec,
    }
    body.update(overrides)
    return parse_ceo_decision(json.dumps(body))


class AntiBloatTests(unittest.TestCase):
    def test_valid_pedido_passes(self) -> None:
        self.assertEqual(validate_ceo_decision(_pedido_decision(), _ctx()), [])

    def test_unknown_project_rejected(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(spec={"project": "nope"}), _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown project", errors[0])

    def test_unknown_eje_rejected(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(spec={"eje_id": "nope"}), _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown eje", errors[0])

    def test_empty_axis_rejects_undeclared_link(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(spec={"eje_id": "vacio", "kpi_ids": ["x"]}),
            _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("not declared", errors[0])

    def test_undeclared_kpi_link_rejected(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(spec={"kpi_ids": ["inventado"]}), _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("not declared", errors[0])

    def test_unmeasured_axis_without_justification_rejected(self) -> None:
        ctx = _ctx(kpi_readings={
            "crecimiento:tickets_abiertos": CEOKpiReading(
                value=None, unknown=True),
        })
        errors = validate_ceo_decision(_pedido_decision(), ctx)
        self.assertEqual(len(errors), 1)
        self.assertIn("no measured KPI", errors[0])
        self.assertIn("justificacion", errors[0])

    def test_missing_reading_needs_justification(self) -> None:
        errors = validate_ceo_decision(_pedido_decision(), _ctx(kpi_readings={}))
        self.assertEqual(len(errors), 1)
        self.assertIn("no measured KPI", errors[0])
        self.assertIn("justificacion", errors[0])

    def test_measured_zero_is_measured_not_unknown(self) -> None:
        ctx = _ctx(kpi_readings={
            "crecimiento:tickets_abiertos": CEOKpiReading(
                value=0, unknown=False),
        })
        self.assertEqual(validate_ceo_decision(_pedido_decision(), ctx), [])

    def test_duplicate_open_title_rejected(self) -> None:
        ctx = _ctx(open_titles=["  FIX the Checkout Button "])
        errors = validate_ceo_decision(_pedido_decision(), ctx)
        self.assertIn("duplicates an open ticket", errors[0])

    def test_duplicate_active_objective_goal_rejected(self) -> None:
        ctx = _ctx(active_objectives=[
            {"objective_id": "OBJ-1", "project": "shop",
             "goal": "FIX the Checkout Button"},
        ])
        errors = validate_ceo_decision(_pedido_decision(), ctx)
        self.assertTrue(
            any("duplicates active objective" in error for error in errors))

    def test_objective_caps_do_not_veto_a_pedido(self) -> None:
        ctx = _ctx(active_objectives=[
            {"objective_id": f"OBJ-{n}", "project": "shop",
             "goal": f"goal {n}"}
            for n in range(3)
        ])
        self.assertEqual(validate_ceo_decision(_pedido_decision(), ctx), [])

    def test_blind_duplicate_views_fail_closed(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(),
            _ctx(objectives_unknown=True, open_titles_unknown=True))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("blind duplicate" in e for e in errors))
        titles_only = validate_ceo_decision(
            _pedido_decision(), _ctx(open_titles_unknown=True))
        self.assertTrue(any("blind duplicate" in e for e in titles_only))
        objectives_only = validate_ceo_decision(
            _pedido_decision(), _ctx(objectives_unknown=True))
        self.assertTrue(
            any("blind duplicate" in e for e in objectives_only))

    def test_no_mandate_rejects_creation(self) -> None:
        errors = validate_ceo_decision(_pedido_decision(), _ctx(mandate=None))
        self.assertEqual(errors, ["crear_pedido: no valid company mandate"])

    def test_keep_going_and_do_nothing_always_validate(self) -> None:
        ctx = _ctx(mandate=None, open_titles_unknown=True,
                   objectives_unknown=True)
        for action in ("keep_going", "do_nothing"):
            with self.subTest(action=action):
                decision = parse_ceo_decision(json.dumps(
                    {"action": action, "summary": "Steady"}))
                self.assertEqual(validate_ceo_decision(decision, ctx), [])

    def test_new_project_pedido_rules(self) -> None:
        def _project(key: str, **spec: Any) -> CEODecision:
            body = {
                "action": "crear_pedido", "summary": "New area",
                "pedido": {
                    "project": "ops", "title": "Open a new shop",
                    "project_key": key, "display_name": "Tienda",
                    "description": "New", "eje_id": "crecimiento",
                    "kpi_ids": ["tickets_abiertos"],
                    **spec,
                },
            }
            return parse_ceo_decision(json.dumps(body))

        ctx = _ctx(ops_project="ops")
        self.assertEqual(validate_ceo_decision(_project("tienda"), ctx), [])
        existing = validate_ceo_decision(_project("ops"), ctx)
        self.assertTrue(any("already exists" in e for e in existing))
        invalid = validate_ceo_decision(_project("BAD KEY!"), ctx)
        self.assertTrue(any("invalid project_key" in e for e in invalid))
        unmeasured = validate_ceo_decision(
            _project("tienda"), _ctx(ops_project="ops", kpi_readings={}))
        self.assertTrue(any("no measured KPI" in e for e in unmeasured))
        elsewhere = validate_ceo_decision(
            _project("tienda", project="shop"), ctx)
        self.assertTrue(any("ops project" in e for e in elsewhere))

    def test_escalation_without_policy_cause_rejected(self) -> None:
        body = {
            "action": "escalate", "summary": "Routine reorder",
            "escalation": {"kind": "orden", "summary": "Reorder the backlog"},
        }
        errors = validate_ceo_decision(parse_ceo_decision(json.dumps(body)), _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("no escalation signal", errors[0])

    def test_escalation_bad_amount_rejected(self) -> None:
        body = {
            "action": "escalate", "summary": "Bad money",
            "escalation": {
                "kind": "compra", "summary": "Buy x", "amount": "-5.00",
            },
        }
        errors = validate_ceo_decision(parse_ceo_decision(json.dumps(body)), _ctx())
        self.assertTrue(any("invalid escalation action" in e for e in errors))

    def test_escalation_with_cause_passes(self) -> None:
        body = {
            "action": "escalate", "summary": "Buy the domain",
            "escalation": {
                "kind": "compra", "summary": "Buy the domain",
                "amount": "12.00",
            },
        }
        self.assertEqual(
            validate_ceo_decision(parse_ceo_decision(json.dumps(body)), _ctx()),
            [])

    def test_duplicate_escalation_flagged_for_exists_outcome(self) -> None:
        body = {
            "action": "escalate", "summary": "Buy the domain",
            "escalation": {
                "kind": "compra", "summary": "Buy the domain",
                "amount": "12.00",
            },
        }
        decision = parse_ceo_decision(json.dumps(body))
        action = ceo_models.OwnerAction(
            kind="compra", summary="Buy the domain", amount="12.00")
        from company_mandate import requires_owner
        human_action = requires_owner(action, _mandate()).human_action
        ctx = _ctx(open_escalations=[
            {"category": "gasto_nuevo", "human_action": human_action}])
        errors = validate_ceo_decision(decision, ctx)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(DUPLICATE_ESCALATION_PREFIX))

    def test_different_escalation_is_not_a_duplicate(self) -> None:
        body = {
            "action": "escalate", "summary": "Buy the domain",
            "escalation": {
                "kind": "compra", "summary": "Buy the domain",
                "amount": "12.00",
            },
        }
        ctx = _ctx(open_escalations=[
            {"category": "gasto_nuevo",
             "human_action": "Decide si autorizas otra cosa."}])
        self.assertEqual(
            validate_ceo_decision(parse_ceo_decision(json.dumps(body)), ctx),
            [])

    def test_escalation_without_mandate_is_allowed(self) -> None:
        body = {
            "action": "escalate", "summary": "No mandate",
            "escalation": {"kind": "ceo_review", "summary": "No mandate"},
        }
        self.assertEqual(
            validate_ceo_decision(
                parse_ceo_decision(json.dumps(body)), _ctx(mandate=None)),
            [])

    def test_blind_escalation_view_still_escalates(self) -> None:
        body = {
            "action": "escalate", "summary": "Buy the domain",
            "escalation": {
                "kind": "compra", "summary": "Buy the domain",
                "amount": "12.00",
            },
        }
        self.assertEqual(
            validate_ceo_decision(
                parse_ceo_decision(json.dumps(body)),
                _ctx(escalations_unknown=True)),
            [])


def _objective_env_for(test: unittest.TestCase, repo: Path,
                       payloads: list[dict[str, Any]],
                       ) -> tuple[ObjectiveEnv, list[dict[str, Any]]]:
    """Real engine env over memory stores + recorded planner (0 tokens)."""
    policy = repo / "policy.md"
    policy.write_text("POLICY for the test planner.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        row: dict[str, Any] = {"task_id": f"task-new-{counter['n']}"}
        if payload.get("origin") == "objective_shell":
            row["params"] = {
                "_mirrors": {"erpnext": {"name": "TASK-2026-00999"}}}
        return row

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=_RecordedPlanner(repo, payloads),  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=None,
        repo=repo,
        objectives_cfg={
            "max_tickets": 20,
            "max_active_tickets_per_objective": 6,
            "max_planning_runs_without_progress": 2,
            "child_autorun": True,
        },
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
        erp=None,
    )
    return env, created


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """The four ticket acceptance cases, all with a simulated model."""

    async def test_review_creates_nothing_when_nothing_is_needed(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "do_nothing",
             "summary": "Open work suffices, no trigger fires"},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis())

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "do_nothing")
        self.assertEqual(calls["created_tickets"], [])
        self.assertEqual(calls["escalations"], [])
        self.assertEqual(len(calls["published"]), 1)
        record = calls["published"][0]
        self.assertEqual(record["kind"], "scheduled")
        self.assertEqual(record["model_runs"], 1)
        self.assertIn("advanced", record["status"])
        self.assertIn("blocked", record["status"])
        self.assertIn("awaiting_owner", record["status"])
        prompt = ceo.prompts[0]
        self.assertIn("CEO_DECISION.json", prompt)
        self.assertIn("crecimiento", prompt)
        self.assertIn(outcome["review_id"], prompt)

    async def test_review_pedido_is_decided_by_the_simulated_planner(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        obj_env, _ = _objective_env_for(self, repo, [
            {"objective_status": "active", "state_summary": "go",
             "tickets": [_dev_ticket("pedido-1", "Fix the checkout")]},
        ])
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": {
                 "project": "shop",
                 "title": "Fix the checkout button",
                 "description": "The button double-charges",
                 "acceptance": ["Single charge"],
                 "eje_id": "crecimiento",
                 "kpi_ids": ["tickets_abiertos"],
             }},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis())

        outcome = await run_review(
            env, kind="reactive", reasons=["KPI tickets_abiertos moved"])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "pedido_created")
        self.assertEqual(len(calls["created_tickets"]), 1)
        payload = calls["created_tickets"][0]
        self.assertTrue(is_pedido(payload))
        self.assertNotIn("workflow", payload["params"])
        self.assertEqual(calls["escalations"], [])
        record = calls["published"][0]
        self.assertEqual(record["outcome"]["action"], "pedido_created")
        # The strategy states the need; the single planner gives it
        # form (simulated run, zero tokens).
        planned = await engine_mod.plan_pedido(obj_env, {
            **payload, "task_id": outcome["task_id"],
        })
        self.assertTrue(planned["ok"])
        self.assertEqual(planned["action"], "decided")
        self.assertEqual(planned["decision"]["target_agent"], "dev")

    async def test_duplicate_creation_is_rejected(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": {
                 "project": "shop",
                 "title": "Fix the checkout button",
                 "description": "It double-charges",
                 "eje_id": "crecimiento",
                 "kpi_ids": ["tickets_abiertos"],
             }},
        ])

        async def _titles(limit: int) -> list[str]:
            del limit
            return ["fix THE checkout BUTTON"]

        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis(),
            open_titles=_titles)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "rejected")
        self.assertIn("duplicates an open ticket", outcome["reason"])
        self.assertEqual(calls["created_tickets"], [])
        self.assertEqual(ceo.runs, 2)  # First invalid, one retry.
        record = calls["published"][0]
        self.assertTrue(
            any("duplicates" in error for error in record["errors"]))

    async def test_duplicate_check_uses_the_full_objective_goal(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        goal = "Expand " + "x" * 400 + " checkout"
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-LONG", "status": "active",
            "project": "shop", "goal": goal,
        })
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": {
                 "project": "shop", "title": goal,
                 "description": "The button double-charges",
                 "eje_id": "crecimiento",
                 "kpi_ids": ["tickets_abiertos"],
             }},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis(),
            objectives=store)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "rejected")
        self.assertIn("duplicates active objective", outcome["reason"])
        self.assertEqual(calls["created_tickets"], [])

    async def test_cost_case_is_escalated(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "escalate", "summary": "Buy the domain",
             "escalation": {
                 "kind": "compra",
                 "summary": "Buy shop.example for the launch",
                 "project": "shop",
                 "amount": "500.00",
             }},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis())

        outcome = await run_review(env, kind="reactive",
                                   reasons=["launch needs a domain"])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "escalated")
        self.assertEqual(outcome["category"], "gasto_nuevo")
        self.assertEqual(len(calls["escalations"]), 1)
        escalation = calls["escalations"][0]
        self.assertEqual(
            escalation["decision"].category.value, "gasto_nuevo")
        self.assertIn("500.00", escalation["decision"].human_action)
        self.assertTrue(
            escalation["source_id"].startswith(
                f"ceo:{outcome['review_id']}:escalation:"))
        self.assertEqual(calls["created_tickets"], [])


class ReviewFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_mandate_escalates_without_model(self) -> None:
        repo = _scratch_repo(self)
        executor = _ExplodingExecutor()
        env, calls = _make_env(self, repo, mandate=None, executor=executor)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "escalated")
        self.assertEqual(outcome["category"], "mandate_unavailable")
        self.assertEqual(executor.runs, [])
        self.assertEqual(len(calls["escalations"]), 1)
        record = calls["published"][0]
        self.assertIsNone(record["decision"])
        self.assertEqual(record["model_runs"], 0)

    async def test_no_mandate_duplicate_yields_exists(self) -> None:
        repo = _scratch_repo(self)
        from company_mandate import OwnerAction, requires_owner
        # The unavailable-mandate text is summary-independent: any open
        # one dedupes the next no-mandate review.
        human_action = requires_owner(
            OwnerAction(kind="ceo_review", summary="probe"), None,
        ).human_action

        async def _open() -> list[dict[str, Any]]:
            return [{
                "category": "mandate_unavailable",
                "human_action": human_action or "",
            }]

        env, calls = _make_env(
            self, repo, mandate=None, executor=_ExplodingExecutor(),
            open_escalations=_open)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "escalation_exists")
        self.assertEqual(calls["escalations"], [])

    async def test_retry_after_invalid_json_succeeds(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            "not json {",
            {"action": "keep_going", "summary": "Steady course"},
        ])
        env, calls = _make_env(self, repo, executor=ceo)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "keep_going")
        self.assertEqual(ceo.runs, 2)
        self.assertIn("invalid CEO_DECISION.json", ceo.prompts[1])
        record = calls["published"][0]
        self.assertEqual(record["model_runs"], 2)
        self.assertEqual(record["errors"], [])

    async def test_persistent_invalid_decision_is_rejected(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, ["nope", "still nope"])
        env, calls = _make_env(self, repo, executor=ceo)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "rejected")
        self.assertEqual(ceo.runs, 2)
        self.assertEqual(calls["created_tickets"], [])
        record = calls["published"][0]
        self.assertTrue(record["errors"])
        self.assertIsNone(record["decision"])

    async def test_executor_crash_becomes_error_outcome(self) -> None:
        repo = _scratch_repo(self)

        class _Crashing:
            async def run(self, *args: Any, **kwargs: Any) -> Any:
                raise TimeoutError("headless hung")

            async def cleanup(self, role: str) -> None:
                del role

        env, calls = _make_env(self, repo, executor=_Crashing())

        outcome = await run_review(env, kind="reactive", reasons=["x"])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "rejected")
        self.assertIn("ceo run failed", outcome["reason"])
        self.assertEqual(len(calls["published"]), 1)

    async def test_missing_decision_keeps_executor_cause(self) -> None:
        repo = _scratch_repo(self)

        class _FailedSilent:
            def __init__(self) -> None:
                self.runs = 0

            async def run(self, role: str, prompt: str, workdir: Path,
                          timeout_s: int) -> Any:
                del role, prompt, workdir, timeout_s
                self.runs += 1
                return SimpleNamespace(
                    returncode=1,
                    output_tail="muse: pinned 1.4.13, installed 1.4.14",
                )

            async def cleanup(self, role: str) -> None:
                del role

        executor = _FailedSilent()
        env, calls = _make_env(self, repo, executor=executor)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "rejected")
        self.assertEqual(executor.runs, 2)
        record = calls["published"][0]
        self.assertIsNone(record["decision"])
        self.assertEqual(len(record["errors"]), 1)
        self.assertIn("no CEO_DECISION.json", record["errors"][0])
        self.assertIn("(1)", record["errors"][0])
        self.assertIn("pinned 1.4.13", record["errors"][0])
        self.assertIn("pinned 1.4.13", outcome["reason"])

    async def test_unknown_kind_is_a_programmer_error(self) -> None:
        repo = _scratch_repo(self)
        env, _ = _make_env(self, repo)
        with self.assertRaises(ValueError):
            await run_review(env, kind="someday", reasons=[])

    async def test_crear_pedido_payload_shape(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": {
                 "project": "shop",
                 "title": "Fix the checkout button",
                 "description": "It double-charges",
                 "acceptance": ["Single charge"],
                 "eje_id": "crecimiento",
                 "kpi_ids": ["tickets_abiertos"],
                 "costs": [{"label": "domain", "amount": "12.00"}],
             }},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis())

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "pedido_created")
        payload = calls["created_tickets"][0]
        self.assertEqual(payload["project"], "shop")
        self.assertEqual(payload["source"], "api")
        self.assertEqual(
            payload["source_id"], f"ceo:{outcome['review_id']}:pedido")
        self.assertEqual(payload["tags"], ["ceo-review"])
        self.assertEqual(payload["params"]["eje_id"], "crecimiento")
        self.assertEqual(
            payload["params"]["costs"],
            [{"label": "domain", "amount": "12.00"}])
        self.assertIn("CEO review", payload["description"])
        self.assertNotIn("coding_tool", payload)
        self.assertNotIn("workflow", payload["params"])
        self.assertTrue(is_pedido(payload))

    async def test_new_project_pedido_payload_shape(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "New area",
             "pedido": {
                 "project": "ops",
                 "title": "Open a new shop",
                 "project_key": "tienda",
                 "display_name": "Tienda",
                 "description": "New shop",
                 "eje_id": "crecimiento",
                 "kpi_ids": ["tickets_abiertos"],
             }},
        ])
        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis())

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "pedido_created")
        payload = calls["created_tickets"][0]
        self.assertEqual(payload["project"], "ops")
        self.assertNotIn("workflow", payload["params"])
        self.assertEqual(payload["params"]["project_key"], "tienda")
        self.assertEqual(payload["params"]["display_name"], "Tienda")
        self.assertEqual(
            payload["source_id"],
            f"ceo:{outcome['review_id']}:pedido")
        self.assertTrue(is_pedido(payload))

    async def test_failed_pedido_creation_is_an_error(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": {
                 "project": "shop", "title": "T", "description": "D",
                 "eje_id": "crecimiento", "kpi_ids": ["tickets_abiertos"],
             }},
        ])

        async def _failing(payload: dict[str, Any]) -> None:
            del payload
            return None

        env, calls = _make_env(
            self, repo, executor=ceo, read_kpi=_measured_kpis(),
            create_ticket=_failing)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["action"], "error")
        self.assertIn("pedido", outcome["reason"])
        self.assertEqual(len(calls["published"]), 1)

    async def test_publish_failure_does_not_fail_the_review(self) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "do_nothing", "summary": "Quiet week"},
        ])

        async def _broken(record: dict[str, Any]) -> None:
            raise ConnectionError("mongo down")

        env, _ = _make_env(self, repo, executor=ceo, publish=_broken)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "do_nothing")

    async def test_duplicate_escalation_maps_to_exists(self) -> None:
        from company_mandate import OwnerAction, requires_owner
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "escalate", "summary": "Buy the domain",
             "escalation": {
                 "kind": "compra", "summary": "Buy the domain",
                 "amount": "12.00",
             }},
        ])
        human_action = requires_owner(
            OwnerAction(
                kind="compra", summary="Buy the domain", amount="12.00"),
            _mandate()).human_action

        async def _open() -> list[dict[str, Any]]:
            return [{
                "category": "gasto_nuevo",
                "human_action": human_action or "",
            }]

        env, calls = _make_env(
            self, repo, executor=ceo, open_escalations=_open)

        outcome = await run_review(env, kind="reactive", reasons=["cost"])

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "escalation_exists")
        self.assertEqual(calls["escalations"], [])
        self.assertEqual(ceo.runs, 1)  # No retry on duplicates.

    async def test_signals_degrade_independently(self) -> None:
        repo = _scratch_repo(self)

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("down")

        env, _ = _make_env(
            self, repo,
            business_open=_boom,
            recently_closed=_boom,
            blocked_tasks=_boom,
            recent_research=_boom,
            open_titles=_boom,
            open_escalations=_boom)
        signals = await compile_signals(env)

        self.assertTrue(signals.business_unknown)
        self.assertTrue(signals.closed_unknown)
        self.assertTrue(signals.blocked_unknown)
        self.assertTrue(signals.research_unknown)
        self.assertTrue(signals.open_titles_unknown)
        self.assertTrue(signals.escalations_unknown)
        self.assertFalse(signals.objectives_unknown)
        self.assertEqual(signals.objectives, [])
        self.assertEqual(len(signals.axes), 2)

    async def test_status_section_reports_advanced_blocked_awaiting(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        ceo = _RecordedCEO(repo, [
            {"action": "keep_going", "summary": "Steady"},
        ])

        async def _closed(limit: int) -> list[dict[str, Any]]:
            del limit
            return [{"task_id": "t1", "erp_name": "TASK-2026-00001",
                     "title": "Done work", "project": "shop"}]

        async def _blocked(limit: int) -> list[dict[str, Any]]:
            del limit
            return [{"task_id": "t2", "erp_name": "TASK-2026-00002",
                     "title": "Stuck work", "project": "shop",
                     "status": "failed", "reason": "builder crashed"}]

        async def _open() -> list[dict[str, Any]]:
            return [{"category": "gasto_nuevo",
                     "human_action": "Decide the spend."}]

        env, calls = _make_env(
            self, repo, executor=ceo,
            recently_closed=_closed, blocked_tasks=_blocked,
            open_escalations=_open)

        outcome = await run_review(env, kind="scheduled", reasons=[])

        self.assertTrue(outcome["ok"])
        status = calls["published"][0]["status"]
        self.assertEqual(status["advanced"][0]["ref"], "TASK-2026-00001")
        self.assertEqual(status["blocked"][0]["reason"], "builder crashed")
        self.assertEqual(
            status["awaiting_owner"][0]["category"], "gasto_nuevo")
        self.assertFalse(status["advanced_unknown"])


class EngineWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_ceo_review_is_a_valid_entry(self) -> None:
        self.assertIn("ceo_review", engine_mod.ENTRIES)

    async def test_unknown_entry_still_rejected(self) -> None:
        repo = _scratch_repo(self)
        obj_env, _ = _objective_env_for(self, repo, [])
        with self.assertRaises(engine_mod.ObjectiveRejected):
            await engine_mod.create_objective(
                obj_env, project="shop", goal="x", entry="ceo_someday")

    async def test_objective_park_wakes_the_ceo(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1", "status": "active",
            "conversion_source_task_id": "t1",
        })
        wakes: list[str] = []
        env = SimpleNamespace(
            objectives=store, notify=None, ceo_wake=wakes.append)
        objective = await store.get_objective("OBJ-1")
        assert objective is not None

        result = await engine_mod._objective_park(
            env, objective, "blocked", "waiting on vendor")

        self.assertEqual(result["reason"], "blocked")
        self.assertEqual(len(wakes), 1)
        self.assertIn("OBJ-1", wakes[0])
        self.assertIn("blocked", wakes[0])

    async def test_objective_close_wakes_the_ceo(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1", "status": "active",
            "project": "shop",
            "conversion_source_task_id": "t1",
            "anchor": {"goal": "Sell", "acceptance": ["a"]},
            "tickets_log": [],
        })
        rows = {"t1": {"task_id": "t1", "status": "converted"}}
        patches: dict[str, dict[str, Any]] = {}
        wakes: list[str] = []

        async def _patch(task_id: str, updates: dict[str, Any]) -> None:
            patches[task_id] = updates
            rows[task_id].update(updates)

        env = SimpleNamespace(
            objectives=store,
            tasks=InMemoryTaskLister(rows),
            patch_task=_patch,
            notify=None,
            ceo_wake=wakes.append,
            auto_merge=True,
            base_branch="main",
            vault=None,
        )
        objective = await store.get_objective("OBJ-1")
        assert objective is not None

        result = await engine_mod._finish_objective(env, objective)

        self.assertTrue(result["ok"])
        self.assertEqual(patches["t1"]["status"], "completed")
        self.assertEqual(len(wakes), 1)
        self.assertIn("OBJ-1", wakes[0])
        self.assertIn("closed", wakes[0])

    async def test_park_without_wake_stays_silent(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1", "status": "active",
            "conversion_source_task_id": "t1",
        })
        env = SimpleNamespace(
            objectives=store, notify=None, ceo_wake=None)
        objective = await store.get_objective("OBJ-1")
        assert objective is not None

        result = await engine_mod._objective_park(
            env, objective, "stalled", "no progress")

        self.assertEqual(result["reason"], "stalled")


class ConfigWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _loader(self) -> ConfigLoader:
        return ConfigLoader(
            Settings(_env_file=None, PROJECTS_DIR=str(self.root)))

    def _write(self, project: str, body: str) -> None:
        target = self.root / project
        target.mkdir(parents=True, exist_ok=True)
        (target / "project.yaml").write_text(body, encoding="utf-8")

    def test_ceo_section_is_rejected_in_project_yaml(self) -> None:
        self._write("proj", "ceo:\n  role: {}\n")
        errors = self._loader().validate_project("proj")
        self.assertEqual(len(errors), 1)
        self.assertIn("ceo", errors[0])
        self.assertIn("global", errors[0])

    def test_global_defaults_carry_ceo_reviewer(self) -> None:
        cfg = self._loader().load_project(None)
        block = cfg["ceo"]
        self.assertEqual(block["engine"], "muse_code")
        self.assertEqual(
            block["profile"], "muse_spark_1_3_contributor")
        role = block["role"]
        self.assertEqual(role["label"], "ceo_reviewer")
        self.assertEqual(role["model"], "muse-spark-1.3-contributor")
        self.assertTrue(role["active"])

    def test_ceo_wiring_config_reads_role_and_timeout(self) -> None:
        wiring = ceo_wiring_config({
            "ceo": {
                "timeout_minutes": 7,
                "role": {"label": "ceo_reviewer", "active": True},
            }
        })
        self.assertEqual(wiring["timeout_minutes"], 7)
        self.assertIn("ceo_reviewer", wiring["roles"])

    def test_ceo_wiring_config_defaults_when_missing(self) -> None:
        self.assertEqual(
            ceo_wiring_config({}),
            {"roles": {}, "timeout_minutes": 15})
        inactive = ceo_wiring_config({
            "ceo": {"role": {"label": "ceo_reviewer", "active": False}}})
        self.assertEqual(inactive["roles"], {})


class ServiceBindingTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, **overrides: Any) -> tuple[CEOService, Any]:
        repo = SimpleNamespace(
            list_active_objectives=self._returns([]),
            list_open_company_titles=self._returns([]),
            list_tasks_needing_human=self._returns([]),
            list_recently_closed_tasks=self._returns([]),
            list_blocked_tasks=self._returns([]),
            list_recent_research_results=self._returns([]),
            save_ceo_review=self._records(),
        )
        config = SimpleNamespace(
            available_projects=lambda: ["ops", "shop"],
            load_project=lambda project: {"ceo": {
                "timeout_minutes": 9, "role": {"active": True}}},
            load_company_mandate=lambda: _mandate(),
        )
        settings = SimpleNamespace(DEFAULT_PROJECT="ops")
        service = CEOService(
            repo=repo,
            task_service=MagicMock(),
            config_loader=config,
            settings=settings,
            company_reader=overrides.pop("company_reader", None),
        )
        return service, repo

    @staticmethod
    def _returns(value: Any):
        async def _fake(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return value

        return _fake

    @staticmethod
    def _records():
        calls: list[Any] = []

        async def _fake(record: Any) -> None:
            calls.append(record)

        _fake.calls = calls  # type: ignore[attr-defined]
        return _fake

    def test_current_mandate_none_when_unreadable(self) -> None:
        service, _ = self._service()

        def _broken() -> Any:
            raise ValueError("bad yaml")

        service._config.load_company_mandate = _broken
        self.assertIsNone(service.current_mandate())

    def test_build_env_wires_timeout_and_projects(self) -> None:
        service, _ = self._service()
        env = service.build_env(_mandate())
        self.assertEqual(env.ceo_cfg["timeout_minutes"], 9)
        self.assertEqual(list(env.known_projects), ["ops", "shop"])
        self.assertEqual(env.ops_project, "ops")
        self.assertFalse(hasattr(env, "create_objective"))
        self.assertTrue(str(env.repo_root).endswith("jardal-openceo"))

    async def test_kpi_vocabulary_measures_and_ignores(self) -> None:
        async def _search(status: Any, limit: int) -> list[dict[str, Any]]:
            del limit
            return [{"name": f"TASK-{n}"} for n in range(len(status) + 1)]

        reader = SimpleNamespace(search_tickets=_search)
        service, repo = self._service(company_reader=reader)
        repo.list_active_objectives = self._returns([{"objective_id": "O1"}])

        active = await service._read_kpi("objectives.active")
        self.assertFalse(active.unknown)
        self.assertEqual(active.value, 1)
        opened = await service._read_kpi("erpnext.open")
        self.assertFalse(opened.unknown)
        self.assertEqual(opened.value, 2)
        finance = await service._read_kpi("erpnext.finance.balance")
        self.assertTrue(finance.unknown)
        self.assertIsNone(finance.value)
        unknown = await service._read_kpi("something.else")
        self.assertTrue(unknown.unknown)

    async def test_kpi_without_reader_is_unknown(self) -> None:
        service, _ = self._service(company_reader=None)
        reading = await service._read_kpi("erpnext.open")
        self.assertTrue(reading.unknown)

    async def test_open_escalations_filters_owner_waits(self) -> None:
        service, repo = self._service()
        repo.list_tasks_needing_human = self._returns([
            {"waiting_reason": "owner_escalation:gasto_nuevo",
             "params": {"escalation": {
                 "category": "gasto_nuevo", "human_action": "Decide."}}},
            {"waiting_reason": "phase:qa_review", "params": {}},
            {"waiting_reason": "owner_escalation:x", "params": {}},
        ])
        rows = await service._open_escalations()
        self.assertEqual(rows, [
            {"category": "gasto_nuevo", "human_action": "Decide."}])


class CockpitMergeTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, latest: Any) -> Any:
        from horizon.service import HorizonService

        async def _empty(*args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def _latest() -> Any:
            return latest

        repo = SimpleNamespace(
            list_active_objectives=_empty,
            list_tasks_by_objective=_empty,
            list_recently_closed_tasks=_empty,
            list_blocked_tasks=_empty,
            latest_ceo_review=_latest,
        )
        return HorizonService(settings=SimpleNamespace(), repo=repo)

    async def test_ceo_section_merges_latest_review(self) -> None:
        service = self._service({
            "review_id": "CEO-1",
            "at": "2026-10-08T12:00:00Z",
            "kind": "scheduled",
            "outcome": {"ok": True, "action": "keep_going"},
            "decision": {"summary": "Steady"},
            "status": {"advanced": [], "blocked": [],
                       "awaiting_owner": []},
        })
        company = await service.company_status()
        assert company["ceo"] is not None
        self.assertEqual(company["ceo"]["review_id"], "CEO-1")
        self.assertEqual(company["ceo"]["action"], "keep_going")
        self.assertEqual(company["ceo"]["summary"], "Steady")

    async def test_ceo_section_none_when_no_review_ran(self) -> None:
        service = self._service(None)
        company = await service.company_status()
        self.assertIsNone(company["ceo"])


class IncidentWakeTests(unittest.TestCase):
    def _service(self) -> tuple[Any, list[str]]:
        from task_service import TaskService

        wakes: list[str] = []
        service = TaskService.__new__(TaskService)
        service.set_ceo_wake(wakes.append)
        return service, wakes

    def test_failed_company_ticket_wakes(self) -> None:
        service, wakes = self._service()
        service._wake_ceo_incident({
            "task_id": "t1", "status": "failed", "title": "Build broke",
            "params": {},
        })
        self.assertEqual(len(wakes), 1)
        self.assertIn("t1", wakes[0])

    def test_escalated_company_ticket_wakes(self) -> None:
        service, wakes = self._service()
        service._wake_ceo_incident({
            "task_id": "t1", "status": "escalated", "title": "Stuck",
            "params": {},
        })
        self.assertEqual(len(wakes), 1)

    def test_completed_and_personal_stay_silent(self) -> None:
        service, wakes = self._service()
        service._wake_ceo_incident({
            "task_id": "t1", "status": "completed", "title": "Done",
            "params": {},
        })
        service._wake_ceo_incident({
            "task_id": "t2", "status": "failed", "title": "Personal",
            "params": {"horizon_scope": "personal"},
        })
        service._wake_ceo_incident(None)
        self.assertEqual(wakes, [])

    def test_missing_wake_stays_silent(self) -> None:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._wake_ceo_incident({
            "task_id": "t1", "status": "failed", "title": "x",
            "params": {},
        })


def _reliability_problem(key: str, **overrides: Any) -> dict[str, Any]:
    """Problem doc shaped like reliability.worker output (no I/O)."""
    doc: dict[str, Any] = {
        "_id": key,
        "scope": "engine",
        "kind": "exception",
        "component": "dev_session",
        "attribution": "engine",
        "severity_max": "high",
        "status": "open",
        "counts": {
            "occurrences": 3,
            "suppressed": 0,
            "distinct_tickets": 2,
            "distinct_projects": 1,
        },
        "ticket": {
            "business_ref": None,
            "source_id": None,
            "type": None,
        },
        "affected_open_tasks": [],
    }
    doc.update(overrides)
    return doc


class ReliabilitySectionTests(unittest.IsolatedAsyncioTestCase):
    """FIABILIDAD section (TASK-2026-00380, design v2 §12)."""

    def _env_with(self, test: unittest.TestCase, repo: Path,
                  problems: list[dict[str, Any]] | None,
                  health: dict[str, Any] | None,
                  **overrides: Any) -> CEOEnv:
        async def _problems() -> list[dict[str, Any]] | None:
            return problems

        async def _health() -> dict[str, Any] | None:
            return health

        fields: dict[str, Any] = {}
        if problems is not None or "problems" in overrides:
            fields["reliability_problems"] = _problems
        if health is not None or "health" in overrides:
            fields["reliability_health"] = _health
        env, _ = _make_env(test, repo, **fields)
        return env

    async def test_top_ten_by_impact_plus_every_storm(self) -> None:
        from ceo.review import compile_reliability

        docs = [
            _reliability_problem(
                f"p{i:02d}", severity_max=severity,
                counts={"occurrences": i, "suppressed": 0,
                        "distinct_tickets": 2, "distinct_projects": 1},
                status="open",
                ticket={"business_ref": "TASK-2026-00001",
                        "source_id": "reliability:problem:p",
                        "type": "fix"},
                affected_open_tasks=["TASK-2026-00002"],
            )
            for i, severity in enumerate(
                ["critical"] + ["high"] * 6 + ["normal"] * 5)
        ]
        docs.append(_reliability_problem(
            "release_regression:abc", kind="release_regression",
            status="open", severity_max="critical"))
        docs.append(_reliability_problem("muted-1", status="muted"))
        docs.append(_reliability_problem("done-1", status="resolved"))
        top, truncated, environment_active = compile_reliability(docs)

        self.assertTrue(truncated)
        self.assertEqual(len(top), 11)
        self.assertEqual(top[0]["problem_key"], "p00")
        self.assertEqual(top[0]["severity"], "critical")
        self.assertEqual(top[-1]["problem_key"], "release_regression:abc")
        self.assertEqual(environment_active, 0)
        for view in top:
            for field in ("scope", "kind", "component", "attribution",
                          "severity", "counts", "status", "ticket",
                          "affected_open_tasks"):
                self.assertIn(field, view)
            self.assertIn(view["status"],
                          {"open", "diagnosed", "ticketed", "regressed"})
        self.assertEqual(top[0]["ticket"]["business_ref"],
                         "TASK-2026-00001")
        self.assertEqual(top[0]["affected_open_tasks"],
                         ["TASK-2026-00002"])

    async def test_environment_active_and_health_null_not_zero(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        docs = [
            _reliability_problem("env-1", attribution="environment",
                                 scope="environment", status="open"),
            _reliability_problem("env-2", attribution="environment",
                                 scope="environment",
                                 status="diagnosed"),
            _reliability_problem("env-old", attribution="environment",
                                 scope="environment", status="resolved"),
            _reliability_problem("eng-1", status="ticketed"),
        ]
        health = {"spool_pending": 3, "spool_discards": 1,
                  "suppressed_incidents": 7}
        env = self._env_with(self, repo, docs, health)
        signals = await compile_signals(env)

        self.assertFalse(signals.reliability_unknown)
        self.assertFalse(signals.reliability_health_unknown)
        self.assertEqual(signals.reliability_environment_active, 2)
        for key in ("capture_failures", "spool_pending", "spool_discards",
                    "suppressed_incidents", "budget_exhausted",
                    "worker_restarts", "shadow_would_stops"):
            self.assertIn(key, signals.reliability_health)
        self.assertIsNone(
            signals.reliability_health["budget_exhausted"])
        self.assertIsNone(
            signals.reliability_health["worker_restarts"])
        self.assertEqual(signals.reliability_health["spool_pending"], 3)

    async def test_unknown_readers_degrade_the_section_only(self) -> None:
        repo = _scratch_repo(self)
        env, _ = _make_env(self, repo)
        signals = await compile_signals(env)

        self.assertTrue(signals.reliability_unknown)
        self.assertTrue(signals.reliability_health_unknown)
        self.assertIsNone(signals.reliability_environment_active)
        self.assertFalse(signals.objectives_unknown)
        ceo = _RecordedCEO(repo, [
            {"action": "do_nothing", "summary": "Nothing due"},
        ])
        env2, _ = _make_env(self, repo, executor=ceo)
        outcome = await run_review(env2, kind="scheduled", reasons=[])
        self.assertTrue(outcome["ok"])
        prompt = ceo.prompts[0]
        self.assertIn("FIABILIDAD", prompt)
        self.assertIn("UNKNOWN", prompt)
        self.assertIn("never approve hunter admission", prompt)

    async def test_failing_readers_stay_unknown_never_empty(self) -> None:
        repo = _scratch_repo(self)

        async def _boom() -> Any:
            raise ConnectionError("mongo down")

        env, _ = _make_env(
            self, repo, reliability_problems=_boom,
            reliability_health=_boom)
        signals = await compile_signals(env)

        self.assertTrue(signals.reliability_unknown)
        self.assertTrue(signals.reliability_health_unknown)
        self.assertEqual(signals.reliability_problems, [])
