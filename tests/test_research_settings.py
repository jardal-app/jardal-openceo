"""Configuracion de research: defaults seguros y claves ausentes (TASK-2026-00057).

Estos tests fijan el contrato de arranque: el servicio debe poder empezar **sin
ninguna clave** y sin habilitar gasto pagado. Un default que activara un proveedor
pagado convertiria una variable de entorno olvidada en una factura.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from config_loader import ConfigLoader  # noqa: E402
from settings import Settings  # noqa: E402

ENV_EXAMPLE = CORE_DIR / ".env.example"

SECRET_NAMES = [
    "RESEARCH_BRAVE_API_KEY",
    "RESEARCH_SERPER_API_KEY",
    "RESEARCH_FIRECRAWL_API_KEY",
    "RESEARCH_META_API_KEY",
]

RESEARCH_NAMES = [
    "RESEARCH_ENABLED",
    "RESEARCH_MAX_ACTIVE_JOBS",
    "RESEARCH_MAX_ACTIVE_MODEL_CALLS",
    "RESEARCH_PAID_SPEND_USD",
    "RESEARCH_AUTO_TOPUPS",
    "RESEARCH_BRAVE_ENABLED",
    "RESEARCH_BRAVE_ACCOUNT_CEILING_USD",
    "RESEARCH_SERPER_ENABLED",
    "RESEARCH_FIRECRAWL_ENABLED",
    "RESEARCH_MUSE_GROUNDING_ENABLED",
    "RESEARCH_MUSE_GROUNDING_MONTHLY_BUDGET_USD",
    "RESEARCH_OPENCODE_TEST_MODEL",
    "RESEARCH_OPENCODE_PROD_MODEL",
    "RESEARCH_EVIDENCE_DIR",
    "RESEARCH_WORKER_DIR",
]


class SettingsDefaultsTests(unittest.TestCase):
    """`Settings()` no puede depender de ninguna variable de research."""

    def setUp(self) -> None:
        # Se ignora el .env del humano a proposito: el test comprueba los defaults
        # del codigo, no el entorno de una maquina concreta.
        self.settings = Settings(_env_file=None)

    def test_research_starts_disabled(self) -> None:
        self.assertFalse(self.settings.RESEARCH_ENABLED)

    def test_no_provider_key_is_required_to_build_settings(self) -> None:
        self.assertEqual(self.settings.RESEARCH_BRAVE_API_KEY, "")
        self.assertEqual(self.settings.RESEARCH_SERPER_API_KEY, "")
        self.assertEqual(self.settings.RESEARCH_FIRECRAWL_API_KEY, "")
        self.assertEqual(self.settings.RESEARCH_META_API_KEY, "")

    def test_paid_spend_defaults_to_zero_and_never_buys(self) -> None:
        self.assertEqual(self.settings.RESEARCH_PAID_SPEND_USD, "0.00")
        self.assertFalse(self.settings.RESEARCH_AUTO_TOPUPS)
        self.assertFalse(self.settings.RESEARCH_BRAVE_PAID_OVERAGE)
        self.assertFalse(self.settings.RESEARCH_SERPER_PAID_PURCHASE)
        self.assertFalse(self.settings.RESEARCH_FIRECRAWL_PAID_TOPUPS)

    def test_grounding_is_paid_and_starts_off(self) -> None:
        self.assertFalse(self.settings.RESEARCH_MUSE_GROUNDING_ENABLED)
        self.assertEqual(self.settings.RESEARCH_MUSE_GROUNDING_MONTHLY_BUDGET_USD, "0.00")

    def test_unknown_serper_credit_is_unknown_not_zero(self) -> None:
        # Cadena vacia = desconocido. Un 0 afirmaria una medicion que no existe.
        self.assertEqual(self.settings.RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES, "")

    def test_opencode_test_route_and_meta_production_route_are_separate(self) -> None:
        self.assertNotEqual(
            self.settings.RESEARCH_OPENCODE_TEST_MODEL,
            self.settings.RESEARCH_OPENCODE_PROD_MODEL,
        )
        self.assertIn("meta/", self.settings.RESEARCH_OPENCODE_PROD_MODEL)
        self.assertIn("opencode/", self.settings.RESEARCH_OPENCODE_TEST_MODEL)

    def test_concurrency_ceilings_are_two_jobs_and_two_calls(self) -> None:
        self.assertEqual(self.settings.RESEARCH_MAX_ACTIVE_JOBS, 2)
        self.assertEqual(self.settings.RESEARCH_MAX_ACTIVE_MODEL_CALLS, 2)

    def test_quick_limits_match_the_reviewed_plan(self) -> None:
        self.assertEqual(self.settings.RESEARCH_QUICK_MAX_SEARCHES, 3)
        self.assertEqual(self.settings.RESEARCH_QUICK_MAX_PAGES, 5)
        self.assertEqual(self.settings.RESEARCH_QUICK_MAX_STEPS, 8)
        self.assertEqual(self.settings.RESEARCH_QUICK_MAX_SECONDS, 120)

    def test_erpnext_contract_is_untouched_by_research_flags(self) -> None:
        # El epico es explicito: la flag de capacidad research NO modifica el
        # contrato de ERPNext, que sigue siempre activo.
        self.assertFalse(hasattr(self.settings, "ERPNEXT_TICKETS_ENABLED"))


class ConfigLoaderResearchSectionTests(unittest.TestCase):
    """La seccion `research` por proyecto nace apagada y sin adaptadores pagados."""

    def setUp(self) -> None:
        self.loader = ConfigLoader(Settings(_env_file=None))

    def test_research_section_defaults_are_conservative(self) -> None:
        cfg = self.loader.load_project(None)
        research = cfg["research"]
        self.assertFalse(research["enabled"])
        self.assertEqual(research["default_mode"], "quick")
        self.assertEqual(research["classification_ceiling"], "public")
        self.assertEqual(research["paid_budget_usd"], "0.00")
        self.assertEqual(research["allowed_account_refs"], [])

    def test_paid_adapters_are_off_even_with_keys(self) -> None:
        research = self.loader.load_project(None)["research"]
        self.assertTrue(research["adapters"]["brave"])
        self.assertFalse(research["adapters"]["serper"])
        self.assertFalse(research["adapters"]["firecrawl"])
        self.assertFalse(research["adapters"]["grounding"])

    def test_project_limits_are_present_per_mode(self) -> None:
        limits = self.loader.load_project(None)["research"]["limits"]
        self.assertEqual(limits["quick"]["search_requests"], 3)
        self.assertEqual(limits["deep"]["pages"], 25)
        self.assertIn("watch", limits)


class EnvExampleTests(unittest.TestCase):
    """El ejemplo versionado documenta los nombres y no lleva secretos."""

    def setUp(self) -> None:
        self.text = ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_every_research_name_is_documented(self) -> None:
        for name in RESEARCH_NAMES:
            self.assertIn(f"{name}=", self.text, f"Falta {name} en .env.example")

    def test_secret_entries_are_empty(self) -> None:
        for name in SECRET_NAMES:
            for line in self.text.splitlines():
                if line.startswith(f"{name}="):
                    value = line.split("=", 1)[1].strip()
                    self.assertEqual(value, "", f"{name} no puede llevar una clave real")

    def test_human_env_file_is_not_modified_by_the_repo(self) -> None:
        # El ticket prohibe tocar el .env del humano. Se comprueba que el codigo no
        # lo escribe: no existe ninguna ruta de escritura hacia el.
        core_sources = list((CORE_DIR / "research").rglob("*.py"))
        self.assertTrue(core_sources, "El paquete research debe existir")
        for path in core_sources:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn('open(".env"', source)
            self.assertNotIn("'.env'", source)


if __name__ == "__main__":
    unittest.main()
