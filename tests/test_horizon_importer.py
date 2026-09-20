"""Importacion del contexto personal de Horizon.

Lo que se prueba aqui no es un parser generico sino tres propiedades que la
especificacion fija y que serian caras de descubrir en produccion:

1. La importacion es idempotente por version: releer el README no duplica ni
   sobrescribe el progreso (secciones 9.3 y D-016).
2. Un dato desconocido es ``None``, **nunca cero** (seccion 2).
3. "Minutos grabados" y "minutos de audio util" son medidas distintas: el
   hito no puede arrancar con los 18 grabados como si fueran utiles.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from db.mongo import MongoRepository
from horizon import importer
from horizon.models import ObjectiveStatus, Uncertainty
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class PolicyExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = importer.read_source(importer.default_source_path())
        cls.declared = importer.extract_policy_block(cls.text)

    def test_specification_source_exists(self) -> None:
        self.assertTrue(importer.default_source_path().exists())

    def test_policy_block_is_found_and_versioned(self) -> None:
        self.assertTrue(self.declared)
        self.assertEqual(self.declared.get("plan_id"), "horizon")
        self.assertEqual(self.declared.get("plan_version"), "2.1")

    def test_declared_income_and_reference_exchange_rate(self) -> None:
        income = self.declared["money"]["reported_monthly_income"]
        self.assertEqual(income["remote_bob"], 6200)
        self.assertEqual(income["teaching_bob"], 3800)
        self.assertEqual(income["total_bob"], 10000)
        self.assertEqual(self.declared["money"]["reference_fx"]["bob_per_usd"], 11.6)

    def test_declared_obligations_and_tithe(self) -> None:
        money = self.declared["money"]
        self.assertEqual(money["contract_refund_target_usd"], 1000)
        self.assertEqual(money["tithe_rate"], 0.10)
        self.assertEqual(money["new_trading_capital_usd"], 0)
        self.assertEqual(money["lottery_monthly_budget_usd"], 0)

    def test_voice_target_and_recorded_are_distinct_from_usable(self) -> None:
        voice = self.declared["voice"]
        self.assertEqual(voice["target_audio_minutes"], 180)
        self.assertEqual(voice["recorded_audio_minutes_reported"], 18)
        self.assertIsNone(voice["usable_audio_minutes_confirmed"])

    def test_backlog_ids_are_read(self) -> None:
        backlog = importer.extract_backlog(self.text)
        for key in ("VOZ-001", "VOZ-002", "FIN-001", "EMP-001", "SAL-001", "CLI-001"):
            with self.subTest(key=key):
                self.assertIn(key, backlog)


class UnknownIsNotZeroTests(unittest.TestCase):
    def test_usable_audio_starts_unknown_and_target_is_declared(self) -> None:
        text = importer.read_source(importer.default_source_path())
        declared = importer.extract_policy_block(text)
        objectives = importer.build_objectives("usr_test", declared, {})
        growth = next(o for o in objectives if o.source_key == "growth_employment")
        voz = next(m for m in growth.milestones if m.source_key == "VOZ-001")

        # Desconocido: sin valor, no cero.
        self.assertIsNone(voz.base.value)
        self.assertEqual(voz.base.uncertainty, Uncertainty.UNKNOWN)
        self.assertEqual(voz.target.value, 180)

    def test_recorded_minutes_never_seed_the_usable_metric(self) -> None:
        text = importer.read_source(importer.default_source_path())
        declared = importer.extract_policy_block(text)
        objectives = importer.build_objectives("usr_test", declared, {})
        growth = next(o for o in objectives if o.source_key == "growth_employment")
        voz = next(m for m in growth.milestones if m.source_key == "VOZ-001")
        self.assertNotEqual(voz.base.value, 18)

    def test_expenses_and_bank_balance_stay_unknown(self) -> None:
        profile = importer.build_profile(
            "usr_test",
            importer.extract_policy_block(importer.read_source(importer.default_source_path())),
        )
        unresolved = profile.declared_context.unresolved
        self.assertIn("essential_monthly_expenses", unresolved)
        self.assertIn("bank_outstanding_balance", unresolved)
        self.assertIn("usable_audio_minutes_confirmed", unresolved)


class MissionAndParkedObjectivesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = importer.read_source(importer.default_source_path())
        cls.declared = importer.extract_policy_block(cls.text)
        cls.objectives = importer.build_objectives("usr_test", cls.declared, {})

    def test_mission_is_registered(self) -> None:
        profile = importer.build_profile("usr_test", self.declared)
        self.assertIn("trabajo digno", profile.mission.statement)

    def test_exactly_one_active_growth_goal(self) -> None:
        active_growth = [
            o
            for o in self.objectives
            if o.kind.value == "growth"
            and o.status == ObjectiveStatus.ACTIVE
            and o.horizon.value != "someday"
        ]
        self.assertEqual(len(active_growth), 1)
        self.assertEqual(active_growth[0].source_key, "growth_employment")

    def test_new_ideas_are_parked_with_a_review_condition(self) -> None:
        parked = [o for o in self.objectives if o.status == ObjectiveStatus.PARKED]
        self.assertGreaterEqual(len(parked), 4)
        for objective in parked:
            with self.subTest(objective=objective.source_key):
                self.assertEqual(objective.horizon.value, "someday")
                self.assertTrue(objective.parked_review_condition)

    def test_parked_objectives_carry_no_actions(self) -> None:
        parked_keys = {o.source_key for o in self.objectives if o.status == ObjectiveStatus.PARKED}
        actions = importer.build_initial_actions("usr_test", self.objectives)
        for action in actions:
            objective = next(
                o for o in self.objectives if o.objective_id == action["objective_id"]
            )
            with self.subTest(action=action["title"]):
                self.assertNotIn(objective.source_key, parked_keys)


class ImportIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.service = HorizonService(_settings(), self.repo)

    async def test_first_import_creates_profile_objectives_and_actions(self) -> None:
        result = await self.service.import_context("usr_owner")
        self.assertTrue(result["imported"])
        self.assertEqual(result["reason"], "created")
        self.assertGreater(result["objectives_created"], 0)
        self.assertGreater(result["actions_created"], 0)

        profile = await self.repo.get_horizon_profile("usr_owner")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["declared_context"]["plan_version"], "2.1")

    async def test_reimport_same_version_changes_nothing(self) -> None:
        first = await self.service.import_context("usr_owner")
        objectives_before = await self.repo.list_horizon_objectives("usr_owner")

        second = await self.service.import_context("usr_owner")

        self.assertFalse(second["imported"])
        self.assertEqual(second["reason"], "already_current")
        objectives_after = await self.repo.list_horizon_objectives("usr_owner")
        self.assertEqual(
            [o["objective_id"] for o in objectives_before],
            [o["objective_id"] for o in objectives_after],
        )

    async def test_forced_reimport_preserves_progress(self) -> None:
        await self.service.import_context("usr_owner")
        growth = await self.repo.get_horizon_objective_by_source_key(
            "usr_owner", "growth_employment"
        )
        voz = next(m for m in growth["milestones"] if m["source_key"] == "VOZ-001")
        objective_id = growth["objective_id"]

        # El usuario registra audio util y evidencia.
        await self.repo.update_horizon_milestone(
            user_id="usr_owner",
            objective_id=objective_id,
            milestone_id=voz["milestone_id"],
            updates={
                "base": {
                    "value": 25,
                    "uncertainty": "declared",
                    "source": "user",
                    "as_of": None,
                },
                "evidence": ["hev_1"],
                "status": "active",
            },
        )

        await self.service.import_context("usr_owner", force=True)

        growth_after = await self.repo.get_horizon_objective_by_source_key(
            "usr_owner", "growth_employment"
        )
        voz_after = next(m for m in growth_after["milestones"] if m["source_key"] == "VOZ-001")
        self.assertEqual(growth_after["objective_id"], objective_id)
        self.assertEqual(voz_after["milestone_id"], voz["milestone_id"])
        self.assertEqual(voz_after["base"]["value"], 25)
        self.assertEqual(voz_after["evidence"], ["hev_1"])

    async def test_import_does_not_duplicate_actions_on_force(self) -> None:
        await self.service.import_context("usr_owner")
        before = await self.repo.count_horizon_actions("usr_owner")
        await self.service.import_context("usr_owner", force=True)
        after = await self.repo.count_horizon_actions("usr_owner")
        self.assertEqual(before, after)

    async def test_missing_source_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await self.service.import_context("usr_owner", source_path="/nope/README.md")


class UpsertOperatorContractTests(unittest.IsolatedAsyncioTestCase):
    """Mongo rechaza el mismo campo en ``$set`` y ``$setOnInsert``.

    ``model_dump`` incluye ``created_at``, asi que sin quitarlo del ``$set`` el
    upsert falla con "would create a conflict at 'created_at'". Este test no
    necesita Mongo: comprueba los operadores que se envian.
    """

    async def test_profile_upsert_does_not_put_created_at_in_set(self) -> None:
        repo = _CapturingProfileRepo()
        await repo.upsert_horizon_profile(
            {
                "user_id": "usr_x",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "mission": {"statement": "m"},
            }
        )
        update = repo.collection.last_update
        self.assertNotIn("created_at", update["$set"])
        self.assertIn("created_at", update["$setOnInsert"])
        self.assertIn("updated_at", update["$set"])

    async def test_objective_upsert_does_not_put_created_at_in_set(self) -> None:
        repo = _CapturingProfileRepo()
        repo.collection.return_none = True
        await repo.upsert_horizon_objective(
            {
                "user_id": "usr_x",
                "source_key": "growth_employment",
                "created_at": datetime.now(timezone.utc),
                "objective_id": "hob_1",
            }
        )
        update = repo.collection.last_update
        self.assertNotIn("created_at", update["$set"])
        self.assertIn("created_at", update["$setOnInsert"])


class _CapturingCollection:
    def __init__(self) -> None:
        self.last_update: dict | None = None
        self.last_key: dict | None = None
        self.return_none = False

    async def update_one(self, key, update, upsert=False):  # noqa: ANN001
        self.last_key = key
        self.last_update = update

    async def find_one(self, key, **kwargs):  # noqa: ANN001
        return None


class _CapturingProfileRepo(MongoRepository):
    def __init__(self) -> None:
        self.collection = _CapturingCollection()

    @property
    def horizon_profile(self):  # noqa: ANN201
        return self.collection

    @property
    def horizon_objectives(self):  # noqa: ANN201
        return self.collection


if __name__ == "__main__":
    unittest.main()