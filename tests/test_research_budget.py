"""Presupuesto de research: reservas atomicas, reconciliacion y precision.

Los unitarios comprueban las guardas que deben ocurrir **antes** de tocar el saldo.
La integracion corre contra una base Mongo aislada y de verdad: un fake que
presuponga exito no demuestra que dos reservas concurrentes por el ultimo saldo se
serialicen, ni que el indice parcial exista.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from research.budget import (  # noqa: E402
    PaidBudgetDisabled,
    ResearchBudgetLedger,
    account_key,
)
from research.money import (  # noqa: E402
    MoneyFormatError,
    format_micro,
    micro_from_str,
    parse_optional_micro,
)

MONGO_URL = "mongodb://localhost:27017"
TEST_DB = "openceo_test_research_budget"


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


MONGO_AVAILABLE = _mongo_available()


class _ForbiddenAuthority:
    """Autoridad que falla si alguien escribe: prueba que la guarda es previa."""

    async def find_one_and_update(self, *args, **kwargs):  # pragma: no cover - guarda
        raise AssertionError("No debio tocar la autoridad de saldo")

    async def update_one(self, *args, **kwargs):  # pragma: no cover - guarda
        raise AssertionError("No debio tocar la autoridad de saldo")

    async def find(self, *args, **kwargs):  # pragma: no cover - guarda
        raise AssertionError("No debio leer la autoridad de saldo")


class MoneyPrecisionTests(unittest.TestCase):
    """Una tarifa subcentavo no puede redondearse a 0."""

    def test_grounding_reference_price_survives_as_micro_usd(self) -> None:
        # $0.0025 por busqueda (referencia publicada de Meta, 2026-10-01).
        self.assertEqual(micro_from_str("0.0025"), 2500)

    def test_micro_usd_round_trips_with_six_decimals(self) -> None:
        self.assertEqual(format_micro(2500), "0.002500")
        self.assertEqual(format_micro(1), "0.000001")
        self.assertEqual(format_micro(1_000_000), "1.000000")

    def test_unknown_is_none_and_never_zero(self) -> None:
        self.assertIsNone(micro_from_str(""))
        self.assertIsNone(micro_from_str(None))
        self.assertIsNone(parse_optional_micro(None))
        self.assertIsNone(format_micro(None))

    def test_malformed_money_raises_instead_of_degrading_to_zero(self) -> None:
        with self.assertRaises(MoneyFormatError):
            micro_from_str("cinco dolares")

    def test_boolean_is_not_money(self) -> None:
        with self.assertRaises(MoneyFormatError):
            parse_optional_micro(True)


class PaidBudgetGuardTests(unittest.IsolatedAsyncioTestCase):
    """Pago cero significa cero llamadas, no "revisar despues"."""

    async def _reserve(self, **overrides):
        ledger = ResearchBudgetLedger(
            accounts=_ForbiddenAuthority(),
            usage=_ForbiddenAuthority(),
        )
        params = {
            "provider": "muse",
            "amount_micro_usd": 2500,
            "operation_id": "op-1",
            "project_key": "jardal-openceo",
            "research_id": "res-1",
            "ceiling_micro_usd": 0,
        }
        params.update(overrides)
        return await ledger.reserve(**params)

    async def test_zero_ceiling_rejects_paid_operation(self) -> None:
        with self.assertRaises(PaidBudgetDisabled):
            await self._reserve()

    async def test_unknown_ceiling_does_not_authorize_spend(self) -> None:
        with self.assertRaises(PaidBudgetDisabled):
            await self._reserve(ceiling_micro_usd=None)

    async def test_unknown_ceiling_can_be_authorized_explicitly(self) -> None:
        # Solo con autorizacion explicita; el ledger no decide politica por su cuenta.
        with self.assertRaises(AssertionError):
            await self._reserve(ceiling_micro_usd=None, allow_unknown_ceiling=True)

    async def test_negative_reservation_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            await self._reserve(amount_micro_usd=-1, ceiling_micro_usd=1_000_000)


class AccountKeyTests(unittest.TestCase):
    def test_key_is_provider_and_account_not_api_key(self) -> None:
        # Rotar claves del mismo equipo no crea mas saldo: la clave natural no
        # incluye la credencial.
        self.assertEqual(account_key("brave"), "brave:default")
        self.assertEqual(account_key("brave", "team"), "brave:team")


@unittest.skipUnless(MONGO_AVAILABLE, "Mongo no disponible en este entorno")
class BudgetMongoIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Atomicidad real contra Mongo aislado."""

    async def asyncSetUp(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo import ASCENDING

        self.client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=1000)
        self.db = self.client[TEST_DB]
        await self.db.research_budget_accounts.drop()
        await self.db.research_usage.drop()
        self.accounts = self.db["research_budget_accounts"]
        self.usage = self.db["research_usage"]
        await self.usage.create_index([("operation_id", ASCENDING)], unique=True)
        self.ledger = ResearchBudgetLedger(accounts=self.accounts, usage=self.usage)

    async def asyncTearDown(self) -> None:
        await self.client.drop_database(TEST_DB)
        self.client.close()

    async def _reserve(self, **overrides):
        params = {
            "provider": "brave",
            "amount_micro_usd": 5_000,
            "operation_id": "op-a",
            "project_key": "jardal-openceo",
            "research_id": "res-1",
            "ceiling_micro_usd": 100_000,
        }
        params.update(overrides)
        return await self.ledger.reserve(**params)

    async def test_two_concurrent_reservations_for_the_last_balance_only_one_proceeds(
        self,
    ) -> None:
        first, second = await asyncio.gather(
            self._reserve(operation_id="op-1", amount_micro_usd=100_000, ceiling_micro_usd=100_000),
            self._reserve(operation_id="op-2", amount_micro_usd=100_000, ceiling_micro_usd=100_000),
        )
        accepted = [d for d in (first, second) if d.accepted]
        rejected = [d for d in (first, second) if not d.accepted]
        self.assertEqual(len(accepted), 1, "Dos reservas no pueden gastar el mismo saldo")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason, "account_ceiling")

    async def test_project_limit_is_enforced(self) -> None:
        first = await self._reserve(
            operation_id="op-1",
            amount_micro_usd=6_000,
            project_limit_micro_usd=10_000,
        )
        second = await self._reserve(
            operation_id="op-2",
            amount_micro_usd=6_000,
            project_limit_micro_usd=10_000,
        )
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "project_limit")

    async def test_job_limit_is_enforced(self) -> None:
        first = await self._reserve(operation_id="op-1", job_limit_micro_usd=8_000)
        second = await self._reserve(
            operation_id="op-2", amount_micro_usd=5_000, job_limit_micro_usd=8_000
        )
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "job_limit")

    async def test_same_operation_id_is_idempotent(self) -> None:
        first = await self._reserve(operation_id="op-same")
        second = await self._reserve(operation_id="op-same")
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertTrue(second.reused)
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(len(doc["reservations"]), 1)

    async def test_double_commit_charges_once(self) -> None:
        decision = await self._reserve(operation_id="op-c", amount_micro_usd=2_500)
        first = await self.ledger.commit(decision.reservation, charged_micro_usd=2_500)
        second = await self.ledger.commit(decision.reservation, charged_micro_usd=2_500)
        self.assertFalse(first.already_settled)
        self.assertTrue(second.already_settled)
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(doc["spent_micro_usd"], 2_500)
        self.assertEqual(doc["reservations"], [])

    async def test_subcent_cost_is_accounted_exactly(self) -> None:
        decision = await self._reserve(
            provider="muse", operation_id="op-sub", amount_micro_usd=2_500
        )
        await self.ledger.commit(decision.reservation, charged_micro_usd=2_500)
        doc = await self.accounts.find_one({"_id": "muse:default"})
        self.assertEqual(doc["spent_micro_usd"], 2_500)
        self.assertEqual(format_micro(doc["spent_micro_usd"]), "0.002500")

    async def test_double_release_is_idempotent_and_charges_nothing(self) -> None:
        decision = await self._reserve(operation_id="op-r", amount_micro_usd=7_000)
        first = await self.ledger.release(decision.reservation)
        second = await self.ledger.release(decision.reservation)
        self.assertFalse(first.already_settled)
        self.assertTrue(second.already_settled)
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(doc["spent_micro_usd"], 0)
        self.assertEqual(doc["reservations"], [])

    async def test_uncertain_timeout_stays_reserved_and_is_never_zero(self) -> None:
        decision = await self._reserve(operation_id="op-u", amount_micro_usd=2_500, provider="muse")
        settlement = await self.ledger.commit(decision.reservation, charged_micro_usd=None)
        self.assertEqual(settlement.state, "uncertain")
        self.assertIsNone(settlement.charged_micro_usd)
        self.assertFalse(settlement.reconciled)
        snapshot = await self.ledger.snapshot(provider="muse")
        # La reserva sigue viva y el gasto reconciliado no puede afirmarse.
        self.assertEqual(snapshot["reserved_usd"], "0.002500")
        self.assertIsNone(snapshot["reconciled_usd"])
        self.assertEqual(snapshot["uncertain_operations"], ["op-u"])

    async def test_reconcile_settles_usage_recorded_before_a_crash(self) -> None:
        decision = await self._reserve(operation_id="op-crash", amount_micro_usd=5_000)
        # Se simula el crash entre "registrar uso" y "retirar reserva".
        await self.ledger._record_usage(  # noqa: SLF001 - se prueba la reparacion
            decision.reservation, state="committed", charged_micro_usd=5_000
        )
        report = await self.ledger.reconcile(grace_seconds=0)
        self.assertEqual(report.accounts, 1)
        self.assertEqual(len(report.settled), 1)
        self.assertEqual(report.settled[0].state, "committed")
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(doc["spent_micro_usd"], 5_000)
        self.assertEqual(doc["reservations"], [])

    async def test_reconcile_keeps_orphan_uncertain_unless_explicitly_released(self) -> None:
        decision = await self._reserve(operation_id="op-orphan", amount_micro_usd=5_000)
        report = await self.ledger.reconcile(grace_seconds=0)
        self.assertEqual(report.settled, [])
        self.assertEqual(len(report.orphaned), 1)
        self.assertEqual(report.orphaned[0]["resolution"], "uncertain")
        # No se libera por defecto: sin registro de uso no se sabe si el I/O ocurrio
        # y liberar seria contabilizar como gratis un gasto posible.
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(len(doc["reservations"]), 1)
        self.assertEqual(doc["reservations"][0]["state"], "uncertain")

        # Liberacion explicita solo con job terminal declarado.
        released = await self.ledger.reconcile(
            grace_seconds=0, terminal_job_ids=[decision.reservation.research_id], release_orphans=True
        )
        self.assertEqual(len(released.settled), 1)
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(doc["reservations"], [])
        self.assertEqual(doc["spent_micro_usd"], 0)

    async def test_orphan_of_a_running_job_is_not_released_even_when_asked(self) -> None:
        await self._reserve(operation_id="op-running", amount_micro_usd=5_000)
        report = await self.ledger.reconcile(
            grace_seconds=0, terminal_job_ids=["otro-job"], release_orphans=True
        )
        self.assertEqual(report.settled, [])
        self.assertEqual(len(report.orphaned), 1)

    async def test_snapshot_matches_the_ledger(self) -> None:
        await self._reserve(operation_id="op-s", amount_micro_usd=40_000, ceiling_micro_usd=100_000)
        snapshot = await self.ledger.snapshot(provider="brave")
        self.assertEqual(snapshot["ceiling_usd"], "0.100000")
        self.assertEqual(snapshot["spent_usd"], "0.000000")
        self.assertEqual(snapshot["reserved_usd"], "0.040000")
        self.assertEqual(snapshot["available_usd"], "0.060000")
        self.assertEqual(snapshot["reconciled_usd"], "0.000000")

    async def test_injected_failure_keeps_the_reservation_live(self) -> None:
        """Con el libro de uso caido, la reserva no se pierde."""
        decision = await self._reserve(operation_id="op-fail", amount_micro_usd=5_000)

        class _BrokenUsage:
            async def update_one(self, *args, **kwargs):
                raise RuntimeError("libro de uso caido")

        broken = ResearchBudgetLedger(accounts=self.accounts, usage=_BrokenUsage())
        with self.assertRaises(RuntimeError):
            await broken.commit(decision.reservation, charged_micro_usd=5_000)
        doc = await self.accounts.find_one({"_id": "brave:default"})
        self.assertEqual(len(doc["reservations"]), 1)
        self.assertEqual(doc["spent_micro_usd"], 0)

    async def test_reserve_authority_has_no_ttl(self) -> None:
        """Una reserva viva no puede borrarse sola por TTL."""
        await self._reserve(operation_id="op-ttl")
        indexes = await self.accounts.index_information()
        for name, spec in indexes.items():
            self.assertNotIn(
                "expireAfterSeconds",
                spec,
                f"El indice {name} no puede expirar la autoridad de reserva",
            )


if __name__ == "__main__":
    unittest.main()
