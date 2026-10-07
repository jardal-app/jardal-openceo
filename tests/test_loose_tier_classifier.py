"""Automatic tier assignment for loose tickets (TASK-2026-00137).

Contract for ``agents.dev.loose_tier``: S → A → C → B order over title +
description + attachment names (Spanish and English), mandatory Tier S
justification, C oracle extraction, safe ``tier_b`` default, the
``session.run`` dispatch hook (loose classifies, objective child without
tier still waits), ``objective_id`` passthrough in ``graph._dev_node``,
and the ``routing_missing`` ERP comment split.
No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.loose_tier import (  # noqa: E402
    ASSIGNED_BY,
    classify_loose_tier,
)
from agents.dev.session import (  # noqa: E402
    CodingStepResult,
    DevSessionManager,
    _merge_loose_params,
)
from graph import OpenCEOGraph  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402


def _decision(title: str, description: str = "",
              attachments: list[str] | None = None):
    return classify_loose_tier(title, description, attachments or [])


class TierSSignalsTests(unittest.TestCase):
    def test_high_stakes_signals_select_tier_s(self) -> None:
        cases = [
            # (title, description) — Spanish and English per signal class.
            ("Cambiar el flujo de autenticación OAuth", ""),
            ("Fix session handling on the auth boundary", ""),
            ("Control de acceso por roles en el panel", ""),
            ("Revisar posible inyección SQL en el buscador", ""),
            ("Patch XSS in comment rendering", ""),
            ("Corregir el cálculo del pago con tarjeta", ""),
            ("Payment totals are wrong for partial refunds", ""),
            ("La facturación duplica cargos", ""),
            ("Money movement audit for transfers", ""),
            ("Migración destructiva de la tabla de usuarios", ""),
            ("Drop table legacy_users after backfill", ""),
            ("Borrado masivo de sesiones caducadas", ""),
            ("Data integrity rules for ledger entries", ""),
            ("Viola el invariante de saldos", ""),
            ("Distributed cache invalidation", ""),
            ("Invalidación distribuida de caché", ""),
            ("Cross-repo compatibility with the mobile API", ""),
            ("Backward compatibility for the v1 webhook", ""),
            ("Incidente en producción por causa desconocida", ""),
            ("Outage with unknown cause in the API", ""),
        ]
        for title, description in cases:
            with self.subTest(title=title):
                decision = _decision(title, description)
                self.assertEqual(decision.tier, CodingTool.TIER_S, title)
                self.assertTrue((decision.why_consensus_is_needed or "").strip(),
                                title)
                self.assertTrue((decision.why_single_session_is_insufficient or "").strip(),
                                title)
                self.assertTrue(decision.signals, title)
                self.assertIn("19", decision.reason, title)

    def test_incident_pair_needs_both_halves(self) -> None:
        incident_only = _decision("Incidente en producción por disco lleno", "")
        self.assertEqual(incident_only.tier, CodingTool.TIER_B)
        unknown_only = _decision("Error de causa desconocida en el pie", "")
        self.assertEqual(unknown_only.tier, CodingTool.TIER_A)


class TierSNegativeTests(unittest.TestCase):
    def test_priority_size_or_quota_alone_never_select_s(self) -> None:
        cases = [
            "P1: fix typo in the footer link",
            "Critical: update the landing headline",
            "Many files change in this docs reorganization",
            "Long implementation of the settings screens",
            "Use premium quota for this migration",
        ]
        for title in cases:
            with self.subTest(title=title):
                decision = _decision(title)
                self.assertEqual(decision.tier, CodingTool.TIER_B, title)

    def test_word_boundaries_do_not_misfire(self) -> None:
        cases = [
            "Update author docs and credits",  # not "auth"
            "Balanceo de carga del servicio",  # not money "balance"
            "Transfer files between backup buckets",  # not money transfer
            "Redesign the dashboard cards",  # "design" needs architecture pair
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(_decision(title).tier, CodingTool.TIER_B, title)


class TierASignalsTests(unittest.TestCase):
    def test_discovery_dominance_selects_tier_a(self) -> None:
        cases = [
            ("Error de causa desconocida al exportar", ""),
            ("Unknown cause for the nightly failure", ""),
            ("Buscar el root cause del reinicio", ""),
            ("La causa raíz del pico de memoria", ""),
            ("No se sabe por qué falla solo en prod", ""),
            ("Fallo intermitente en el checkout", ""),
            ("Intermittent timeout in the worker", ""),
            ("Flaky test in the suite", ""),
            ("Evidencia contradictoria en los logs", ""),
            ("Contradictory reports from two regions", ""),
            ("Legacy sin documentar en listados", "Hay que entender el código heredado"),
            ("Undocumented legacy listing module", ""),
            ("Diseñar el mecanismo de reintentos", ""),
            ("Design the retry architecture", ""),
            ("Definir la arquitectura de colas", ""),
            ("Build roto tras la actualización", ""),
            ("Broken pipeline in the release branch", ""),
            ("Pipeline rota del despliegue", ""),
            ("Coherencia entre sistemas rota", ""),
            ("Cross-system sync is broken", ""),
        ]
        for title, description in cases:
            with self.subTest(title=title):
                decision = _decision(title, description)
                self.assertEqual(decision.tier, CodingTool.TIER_A, title)
                self.assertIn("21", decision.reason, title)

    def test_architecture_pair_needs_verb_and_noun(self) -> None:
        self.assertEqual(
            _decision("Implementar según el design doc adjunto").tier,
            CodingTool.TIER_B)
        self.assertEqual(
            _decision("Documentar la arquitectura actual").tier,
            CodingTool.TIER_B)

    def test_bare_investigar_is_not_discovery_dominant(self) -> None:
        decision = _decision("Investigar y corregir el bug del listado", "")
        self.assertEqual(decision.tier, CodingTool.TIER_B)


class TierCTests(unittest.TestCase):
    def test_qualifying_oracle_selects_tier_c(self) -> None:
        decision = _decision(
            "Fix off-by-one in pagination",
            "Covered by tests/test_pager.py::test_pages; small reversible change.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_C)
        assert decision.oracle is not None
        self.assertEqual(decision.oracle["type"], "preexisting_tests")
        self.assertEqual(decision.oracle["refs"],
                         [{"ref": "tests/test_pager.py::test_pages"}])
        self.assertIn("25", decision.reason)

    def test_spanish_oracle_selects_tier_c(self) -> None:
        decision = _decision(
            "Corregir el orden de la lista",
            "Cubierto por tests/test_lista.py; cambio acotado.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_C)
        assert decision.oracle is not None
        self.assertEqual(decision.oracle["refs"], [{"ref": "tests/test_lista.py"}])

    def test_builder_tests_alone_never_qualify(self) -> None:
        cases = [
            ("Add tests for the new checkout flow", "Write new tests; small change."),
            ("Escribir tests para el módulo", "Agregar tests nuevos, cambio acotado."),
            ("Cover the new endpoint", "Create tests/test_new.py with fresh cases."),
        ]
        for title, description in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    _decision(title, description).tier, CodingTool.TIER_B, title)

    def test_conjunction_requires_claim_paths_and_bounded(self) -> None:
        # Paths without a coverage claim: could be builder work.
        self.assertEqual(
            _decision("Update tests", "Extend tests/test_x.py for the new flow.").tier,
            CodingTool.TIER_B)
        # Claim without extractable paths: not freezable.
        self.assertEqual(
            _decision("Fix footer link", "Covered by existing tests; small fix.").tier,
            CodingTool.TIER_B)
        # Claim + paths without boundedness.
        self.assertEqual(
            _decision("Rework listing",
                      "Covered by tests/test_list.py; large rework.").tier,
            CodingTool.TIER_B)

    def test_s_and_a_win_over_oracle_evidence(self) -> None:
        decision = _decision(
            "Fix payment rounding",
            "Covered by tests/test_pay.py; small change.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_S)
        decision = _decision(
            "Root cause unknown in export",
            "Covered by tests/test_export.py; small change.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_A)


class TierBDefaultTests(unittest.TestCase):
    def test_safe_default(self) -> None:
        self.assertEqual(_decision("", "").tier, CodingTool.TIER_B)
        self.assertEqual(
            _decision("Add dark mode toggle", "New setting with preview.").tier,
            CodingTool.TIER_B)
        self.assertEqual(
            _decision("Refactor semántico del módulo de notificaciones",
                      "Reordenar sin cambiar comportamiento.").tier,
            CodingTool.TIER_B)

    def test_critico_alone_is_not_tier_s(self) -> None:
        self.assertEqual(
            _decision("Cambio crítico en el pie de página", "").tier,
            CodingTool.TIER_B)
        self.assertEqual(
            _decision("Cambio crítico en el flujo de pago", "").tier,
            CodingTool.TIER_S)


class AdversarialTextsTests(unittest.TestCase):
    def test_tier_tokens_in_text_are_not_honored(self) -> None:
        # Tickets ABOUT tiers (e.g. autonomy roadmap) must not self-trigger:
        # the coding_tool field is the explicit channel.
        decision = _decision(
            "[Autonomía/F3] Retirar tiers numéricos legacy (tier1-4)",
            "Retirar enum, selector LLM, param MCP y default auto. "
            "Tiers tier_s/a/b/c quedan como único routing.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_B)
        decision = _decision(
            "Clasificación automática para no parkar en routing_missing",
            "Tiers solo per-task, sin cuenta/modelo en el ticket, "
            "sin degradaciones silenciosas por cuota.",
        )
        self.assertEqual(decision.tier, CodingTool.TIER_B)

    def test_determinism(self) -> None:
        first = _decision("Fix payment rounding", "Covered by tests/test_pay.py.")
        second = _decision("Fix payment rounding", "Covered by tests/test_pay.py.")
        self.assertEqual(first, second)


class LooseParamsTests(unittest.TestCase):
    def test_merge_reattaches_dropped_params(self) -> None:
        enriched = {"tier_reason": "r", "tier_assigned_by": ASSIGNED_BY}
        self.assertEqual(_merge_loose_params(enriched, None), enriched)
        downstream = {"tier_reason": "r", "review_links": ["u"]}
        self.assertEqual(_merge_loose_params(enriched, downstream),
                         {"tier_reason": "r", "tier_assigned_by": ASSIGNED_BY,
                          "review_links": ["u"]})

    def test_merge_downstream_wins_and_empty_passes_through(self) -> None:
        enriched = {"tier_reason": "old"}
        self.assertEqual(
            _merge_loose_params(enriched, {"tier_reason": "new"}),
            {"tier_reason": "new"})
        self.assertIsNone(_merge_loose_params(None, None))
        self.assertIsNone(_merge_loose_params({}, None))


class SessionDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> DevSessionManager:
        settings = MagicMock()
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=[])
        return DevSessionManager(settings, loader, repo=None)

    def _task(self, **overrides):
        task = {"task_id": "task-1", "title": "t", "description": "d"}
        task.update(overrides)
        return task

    async def test_loose_ticket_without_tier_is_classified(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            self._task(title="Refactor semántico del módulo",
                       description="Reordenar sin cambiar comportamiento."),
            {},
        )
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        assert output.params is not None
        self.assertIn("tier_b", output.params["tier_reason"])
        self.assertEqual(output.params["tier_assigned_by"], ASSIGNED_BY)
        self.assertEqual(output.params["tier_signals"], [])
        manager._step_code.assert_awaited_once()

    async def test_loose_signal_selects_tier_at_dispatch(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            self._task(title="Error de causa desconocida al exportar"),
            {},
        )
        self.assertEqual(output.coding_tool, CodingTool.TIER_A)
        assert output.params is not None
        self.assertIn("a_unknown", output.params["tier_signals"][0])

    async def test_explicit_field_beats_classifier(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            self._task(title="Corregir el cálculo del pago",
                       coding_tool="tier_c"),
            {},
        )
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)

    async def test_objective_child_without_tier_still_waits(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock()
        output = await manager.run(self._task(objective_id="OBJ-1"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("routing_missing"))
        manager._step_code.assert_not_called()


class GraphObjectiveIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_node_forwards_objective_id(self) -> None:
        captured: dict = {}

        class FakeDevSession:
            async def run(self, task: dict, project_config: dict):
                captured["task"] = task
                output = MagicMock()
                output.status = TaskStatus.COMPLETED
                output.steps = []
                output.coding_tool = CodingTool.TIER_B
                output.coding_session_id = None
                output.coding_session_title = None
                output.last_prompt_sent = None
                output.last_response_summary = None
                output.waiting_reason = None
                output.resume_at = None
                output.error = None
                output.modified_files = []
                output.params = None
                return output

        graph = OpenCEOGraph.__new__(OpenCEOGraph)
        graph._dev_session = FakeDevSession()
        await graph._dev_node({
            "task_id": "task-1",
            "title": "t",
            "description": "d",
            "objective_id": "OBJ-9",
            "project_config": {},
        })
        self.assertEqual(captured["task"]["objective_id"], "OBJ-9")


class RoutingWaitCommentTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, adapter):
        service = TaskService.__new__(TaskService)
        service._erpnext = adapter
        service._repo = MagicMock()
        service._repo.patch_task = AsyncMock()
        return service

    def _task(self, **overrides):
        task = {"task_id": "task-1",
                "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}}}
        task.update(overrides)
        return task

    async def test_objective_child_comment_names_planner_gap(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        await TaskService._comment_wait_on_erp(
            self._service(adapter),
            self._task(objective_id="OBJ-1"), "routing_missing: no value")
        args = adapter.add_comment.await_args.args
        self.assertEqual(args[0], "TASK-1")
        self.assertIn("objetivo", args[1])
        self.assertIn("sueltos", args[1])
        self.assertIn("coding_tool", args[1])

    async def test_loose_wait_keeps_legacy_text(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        await TaskService._comment_wait_on_erp(
            self._service(adapter), self._task(), "routing_missing: no value")
        args = adapter.add_comment.await_args.args
        self.assertIn("opción por defecto", args[1])


if __name__ == "__main__":
    unittest.main()
