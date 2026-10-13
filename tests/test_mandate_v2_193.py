"""Company mandate v2 (TASK-2026-00193): gain/spend principles, optional KPI.

Acceptance with zero tokens: the shipped YAML carries the five Owner
principles; both prompts (CEO strategy, planner core) render them; a
pedido validates without axes when it carries a written justification.
No network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo.models import CEOKpiReading, CEOValidationContext, validate_ceo_decision
from ceo.review import CEOSignals, CEOEnv, _ceo_prompt, _mandate_section
from company_mandate import (
    MANDATE_PRINCIPLES,
    MandateError,
    parse_mandate,
    principles_prompt_section,
)
from objectives import engine as engine_mod
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
)

REPO = Path(__file__).resolve().parents[1]


def _mandate_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "ejes": [],
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
    return data


def _ctx(**overrides: Any) -> CEOValidationContext:
    base: dict[str, Any] = {
        "mandate": parse_mandate(_mandate_dict()),
        "known_projects": ["ops", "shop"],
        "open_titles": [],
        "active_objectives": [],
        "open_escalations": [],
        "kpi_readings": {},
    }
    base.update(overrides)
    return CEOValidationContext(**base)


def _pedido_decision(**spec: Any) -> Any:
    from ceo.models import CEODecision

    body: dict[str, Any] = {
        "project": "shop",
        "title": "Fix checkout",
        "description": "The button fails",
    }
    body.update(spec)
    return CEODecision.model_validate(
        {"action": "crear_pedido", "summary": "Shop needs checkout",
         "pedido": body}
    )


class ShippedMandateTests(unittest.TestCase):
    def test_shipped_yaml_carries_the_five_principles(self) -> None:
        from company_mandate import load_mandate_file

        mandate = load_mandate_file(REPO / "projects" / "company_mandate.yaml")
        self.assertEqual(mandate.version, 1)
        self.assertEqual(list(mandate.principios), list(MANDATE_PRINCIPLES))

    def test_canonical_principles_cover_gain_spend_and_price(self) -> None:
        keywords = ("ganancia", "gasto", "rinda", "reutilizar", "precio")
        self.assertEqual(len(MANDATE_PRINCIPLES), 5)
        for keyword, principle in zip(keywords, MANDATE_PRINCIPLES):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, principle)

    def test_five_categories_and_threshold_stay(self) -> None:
        from company_mandate import load_mandate_file

        mandate = load_mandate_file(REPO / "projects" / "company_mandate.yaml")
        self.assertEqual(
            sorted(mandate.escalar_al_dueno),
            sorted([
                "gasto_nuevo",
                "accion_irreversible",
                "replanificacion_estrategica",
                "cambio_de_mandato",
                "revision_exigida_por_politica",
            ]),
        )
        self.assertEqual(
            str(mandate.presupuesto.gasto_nuevo_sin_preguntar), "0.00"
        )


class PrincipiosSchemaTests(unittest.TestCase):
    def test_absent_principios_default_to_the_five(self) -> None:
        mandate = parse_mandate(_mandate_dict())
        self.assertEqual(list(mandate.principios), list(MANDATE_PRINCIPLES))

    def test_explicit_principios_are_kept(self) -> None:
        mandate = parse_mandate(_mandate_dict(principios=["uno", "dos"]))
        self.assertEqual(list(mandate.principios), ["uno", "dos"])

    def test_blank_duplicate_or_empty_principios_rejected(self) -> None:
        for bad in ([], ["  "], ["uno", "uno"], ["uno", "  uno  "], [3]):
            with self.subTest(bad=bad):
                with self.assertRaises(MandateError):
                    parse_mandate(_mandate_dict(principios=bad))


class PrinciplesSectionTests(unittest.TestCase):
    def test_mandate_principles_are_rendered(self) -> None:
        section = principles_prompt_section(parse_mandate(_mandate_dict()))
        self.assertIn("PRINCIPLES (company mandate)", section)
        for principle in MANDATE_PRINCIPLES:
            self.assertIn(f"- {principle}", section)

    def test_missing_mandate_falls_back_to_defaults(self) -> None:
        section = principles_prompt_section(None)
        self.assertIn("PRINCIPLES (default company principles)", section)
        for principle in MANDATE_PRINCIPLES:
            self.assertIn(f"- {principle}", section)


class PedidoJustificationTests(unittest.TestCase):
    def test_justification_replaces_the_axis(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(justificacion="Checkout pierde ventas."),
            _ctx(),
        )
        self.assertEqual(errors, [])

    def test_neither_axis_nor_justification_is_rejected(self) -> None:
        errors = validate_ceo_decision(_pedido_decision(), _ctx())
        self.assertEqual(len(errors), 1)
        self.assertIn("justificacion", errors[0])

    def test_kpi_ids_without_eje_are_rejected(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(
                kpi_ids=["tickets_abiertos"],
                justificacion="Checkout pierde ventas.",
            ),
            _ctx(),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("without eje_id", errors[0])

    def test_unknown_eje_is_still_rejected(self) -> None:
        errors = validate_ceo_decision(
            _pedido_decision(
                eje_id="nope", justificacion="Checkout pierde ventas."
            ),
            _ctx(),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown eje", errors[0])

    def test_axis_without_declared_kpis_accepts_justification(self) -> None:
        mandate = parse_mandate(_mandate_dict(ejes=[{
            "id": "nuevo", "meta": "Eje sin KPI", "kpis": [],
        }]))
        justified = validate_ceo_decision(
            _pedido_decision(
                eje_id="nuevo", justificacion="Primera señal."
            ),
            _ctx(mandate=mandate),
        )
        self.assertEqual(justified, [])
        bare = validate_ceo_decision(
            _pedido_decision(eje_id="nuevo"), _ctx(mandate=mandate)
        )
        self.assertEqual(len(bare), 1)
        self.assertIn("justificacion", bare[0])

    def test_unmeasured_link_accepts_justification(self) -> None:
        mandate = parse_mandate(_mandate_dict(ejes=[{
            "id": "crecimiento",
            "meta": "Crecer",
            "kpis": [{"id": "aperturas", "fuente": "erpnext.open"}],
        }]))
        errors = validate_ceo_decision(
            _pedido_decision(
                eje_id="crecimiento",
                kpi_ids=["aperturas"],
                justificacion="La fuente aún no mide.",
            ),
            _ctx(mandate=mandate),
        )
        self.assertEqual(errors, [])

    def test_measured_link_needs_no_justification(self) -> None:
        mandate = parse_mandate(_mandate_dict(ejes=[{
            "id": "crecimiento",
            "meta": "Crecer",
            "kpis": [{"id": "aperturas", "fuente": "erpnext.open"}],
        }]))
        errors = validate_ceo_decision(
            _pedido_decision(eje_id="crecimiento", kpi_ids=["aperturas"]),
            _ctx(
                mandate=mandate,
                kpi_readings={
                    "crecimiento:aperturas": CEOKpiReading(
                        value=7, unknown=False
                    )
                },
            ),
        )
        self.assertEqual(errors, [])

    def test_undeclared_kpi_link_is_still_rejected(self) -> None:
        mandate = parse_mandate(_mandate_dict(ejes=[{
            "id": "crecimiento",
            "meta": "Crecer",
            "kpis": [{"id": "aperturas", "fuente": "erpnext.open"}],
        }]))
        errors = validate_ceo_decision(
            _pedido_decision(
                eje_id="crecimiento",
                kpi_ids=["inventado"],
                justificacion="No cuela.",
            ),
            _ctx(mandate=mandate),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("not declared", errors[0])


class CEOPromptPrinciplesTests(unittest.TestCase):
    def test_mandate_section_renders_principles(self) -> None:
        signals = CEOSignals(
            principios=["ganar más", "gastar menos"],
            limits={
                "objetivos_activos": 3,
                "objetivos_activos_por_proyecto": 2,
                "tickets_activos_por_objetivo": 6,
            },
            spend_threshold="0.00",
            spend_currency="USD",
        )
        section = _mandate_section(signals)
        self.assertIn("PRINCIPLES", section)
        self.assertIn("- ganar más", section)
        self.assertIn("- gastar menos", section)

    def test_no_axes_no_longer_blocks_new_work(self) -> None:
        section = _mandate_section(CEOSignals())
        self.assertNotIn("never new work", section)
        self.assertIn("justificacion", section)

    def test_full_prompt_carries_principles_and_justification_rule(self) -> None:
        prompt = _ceo_prompt(
            CEOSignals(principios=list(MANDATE_PRINCIPLES)),
            kind="scheduled",
            reasons=[],
            review_id="ceo-test",
            repo_root=REPO,
            ops_project="ops",
        )
        for principle in MANDATE_PRINCIPLES:
            self.assertIn(principle, prompt)
        self.assertIn("justificacion", prompt)
        self.assertIn("acceptance", prompt)


class PlannerPromptPrinciplesTests(unittest.TestCase):
    def test_objective_prompt_renders_the_guide(self) -> None:
        prompt = engine_mod._planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}},
            "", [], "", [],
            principles_guide="PRINCIPLES (company mandate):\n- ganar más",
        )
        self.assertIn("PRINCIPLES (company mandate)", prompt)
        self.assertIn("- ganar más", prompt)

    def test_objective_prompt_omits_the_empty_guide(self) -> None:
        prompt = engine_mod._planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}},
            "", [], "", [],
        )
        self.assertNotIn("PRINCIPLES", prompt)

    def test_pedido_prompt_renders_the_guide(self) -> None:
        prompt = engine_mod._pedido_prompt(
            {"task_id": "t-1", "title": "Fix", "project": "shop"},
            principles_guide="PRINCIPLES (company mandate):\n- ganar más",
        )
        self.assertIn("PRINCIPLES (company mandate)", prompt)

    def test_pedido_prompt_omits_the_empty_guide(self) -> None:
        prompt = engine_mod._pedido_prompt(
            {"task_id": "t-1", "title": "Fix", "project": "shop"}
        )
        self.assertNotIn("PRINCIPLES", prompt)


class _RecordedCEO:
    def __init__(self, repo: Path, payload: dict[str, Any]) -> None:
        self._repo = repo
        self._payload = payload
        self.prompts: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "ceo_reviewer", role
        self.prompts.append(prompt)
        target = self._repo / ".openceo" / "CEO_DECISION.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self._payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="CEO_READY")

    async def cleanup(self, role: str) -> None:
        del role


class JustificationAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pedido_without_axes_is_created(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            created: list[dict[str, Any]] = []

            async def _empty(*args: Any, **kwargs: Any) -> list[Any]:
                del args, kwargs
                return []

            async def _business() -> dict[str, Any]:
                return {"rows": [], "unknown": False}

            async def _kpi(fuente: str) -> CEOKpiReading:
                del fuente
                return CEOKpiReading(value=None, unknown=True)

            async def _create(payload: dict[str, Any]) -> dict[str, Any]:
                created.append(payload)
                return {"task_id": "task-1"}

            async def _escalate(**kwargs: Any) -> dict[str, Any]:
                del kwargs
                raise AssertionError("no escalation expected")

            async def _publish(record: dict[str, Any]) -> None:
                del record

            ceo = _RecordedCEO(repo, {
                "action": "crear_pedido",
                "summary": "Shop needs checkout",
                "pedido": {
                    "project": "shop",
                    "title": "Fix checkout",
                    "description": "The button fails",
                    "acceptance": ["The button pays."],
                    "justificacion": "Checkout pierde ventas cada día.",
                },
            })
            from ceo.review import run_review

            env = CEOEnv(
                mandate=parse_mandate(_mandate_dict()),
                objectives=InMemoryObjectiveStore(),
                tasks=InMemoryTaskLister(),
                open_titles=_empty,
                open_escalations=_empty,
                recently_closed=_empty,
                blocked_tasks=_empty,
                recent_research=_empty,
                business_open=_business,
                read_kpi=_kpi,
                known_projects=["ops", "shop"],
                ops_project="ops",
                create_ticket=_create,
                escalate=_escalate,
                executor=ceo,
                repo_root=repo,
                publish=_publish,
                ceo_cfg={},
                now=lambda: datetime(
                    2026, 10, 13, 12, 0, tzinfo=timezone.utc),
            )
            outcome = await run_review(env, kind="reactive",
                                       reasons=["shop is down"])
            self.assertEqual(outcome["action"], "pedido_created")
            self.assertEqual(len(created), 1)
            self.assertEqual(
                created[0]["params"].get("justificacion"),
                "Checkout pierde ventas cada día.",
            )
            self.assertNotIn("eje_id", created[0]["params"])
            self.assertIn("PRINCIPLES", ceo.prompts[0])


if __name__ == "__main__":
    unittest.main()
