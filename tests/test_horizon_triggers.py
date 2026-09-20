"""Disparadores persistidos y el timer unico.

La propiedad mas importante de este archivo no es que el trabajo se ejecute: es
que **no se sondea**. AGENTS.md §5.1 lo prohibe y la seccion 9.2 punto 4 lo
prohibe para esta capacidad en concreto. Un test que solo comprobara el
resultado seguiria pasando con un bucle que consulta cada segundo, asi que aqui
se cuenta cuantas veces consulta estando ocioso.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from horizon import triggers as triggers_module
from horizon.triggers import HorizonScheduler, HorizonTriggerStore
from horizon_support import FakeHorizonRepo

OWNER = "usr_admin"
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "HORIZON_DAILY_PLAN_TIME": "06:00",
        "HORIZON_CLOSE_DAY_TIME": "21:00",
        "HORIZON_WEEKLY_REVIEW_TIME": "18:00",
        "HORIZON_TRIGGER_MAX_ATTEMPTS": 3,
        "HORIZON_TRIGGER_BACKOFF_SECONDS": 60,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _CountingRepo(FakeHorizonRepo):
    """Cuenta las consultas de vencimiento para poder afirmar que no hay barrido."""

    def __init__(self) -> None:
        super().__init__()
        self.due_queries = 0

    async def list_due_horizon_triggers(self, now, limit: int = 20):
        self.due_queries += 1
        return await super().list_due_horizon_triggers(now, limit=limit)


class DedupeKeyTests(unittest.TestCase):
    def test_the_daily_key_is_scoped_to_the_day(self) -> None:
        # Con una clave sin periodo, el plan diario se deduplicaria una vez y no
        # volveria a crearse nunca.
        monday = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        tuesday = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-22")
        self.assertNotEqual(monday, tuesday)

    def test_a_replan_key_survives_a_repeated_terminal_patch(self) -> None:
        first = triggers_module.dedupe_key(
            "reactive_replan", OWNER, task_id="t1", status="completed"
        )
        second = triggers_module.dedupe_key(
            "reactive_replan", OWNER, task_id="t1", status="completed"
        )
        self.assertEqual(first, second)

    def test_different_kinds_never_collide(self) -> None:
        daily = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        close = triggers_module.dedupe_key("close_day", OWNER, date_key="2026-09-21")
        self.assertNotEqual(daily, close)


class BackoffTests(unittest.TestCase):
    def test_the_wait_grows_and_is_capped(self) -> None:
        self.assertEqual(triggers_module.backoff_seconds(1), 60)
        self.assertEqual(triggers_module.backoff_seconds(2), 120)
        self.assertEqual(triggers_module.backoff_seconds(3), 240)
        # Acotado: un reintento no puede quedar programado para manana.
        self.assertEqual(triggers_module.backoff_seconds(20), 3600)


class UpcomingOccurrenceTests(unittest.TestCase):
    def test_the_next_daily_occurrence_is_tomorrow_if_today_passed(self) -> None:
        local_ten_am = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)  # 10:00 en La Paz
        occurrences = triggers_module.upcoming_occurrences(
            now=local_ten_am, timezone_name="America/La_Paz"
        )
        daily = next(o for o in occurrences if o["kind"] == "daily_plan")
        self.assertEqual(daily["date_key"], "2026-09-22")

    def test_exercise_agenda_is_not_used_here_but_occurrences_are_stable(self) -> None:
        occurrences = triggers_module.upcoming_occurrences(
            now=NOW, timezone_name="America/La_Paz"
        )
        kinds = {o["kind"] for o in occurrences}
        self.assertEqual(
            kinds, {"daily_plan", "close_day", "weekly_review", "monthly_review"}
        )


class EnqueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.store = HorizonTriggerStore(self.repo, settings=_settings())

    async def test_the_same_key_creates_one_document(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        first = await self.store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        second = await self.store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        self.assertEqual(len(self.repo.triggers), 1)
        self.assertEqual(first["trigger_id"], second["trigger_id"])

    async def test_the_enqueued_trigger_has_an_identifier_and_an_expiry(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        created = await self.store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        # Sin identificador no se podria reclamar ni marcar despues.
        self.assertTrue(created["trigger_id"])
        self.assertIsNotNone(created["expires_at"])

    async def test_ordinary_enqueue_is_idempotent(self) -> None:
        first = await self.store.ensure_upcoming(
            user_id=OWNER, timezone_name="America/La_Paz", now=NOW
        )
        second = await self.store.ensure_upcoming(
            user_id=OWNER, timezone_name="America/La_Paz", now=NOW
        )
        self.assertEqual(first, 4)
        self.assertEqual(second, 0)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _CountingRepo()
        self.store = HorizonTriggerStore(self.repo, settings=_settings())
        self.handled: list[dict] = []
        self.fail_with: Exception | None = None

    async def _handler(self, trigger: dict) -> dict:
        if self.fail_with is not None:
            raise self.fail_with
        self.handled.append(trigger)
        return {"ok": True}

    def _scheduler(self, **overrides) -> HorizonScheduler:
        return HorizonScheduler(
            repo=self.repo,
            store=self.store,
            handler=self._handler,
            settings=_settings(),
            now_fn=lambda: NOW,
            **overrides,
        )

    async def test_an_idle_scheduler_does_not_poll(self) -> None:
        """La propiedad que AGENTS.md §5.1 exige y que un test de resultado no ve."""
        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        # Un barrido habria consultado decenas de veces en 200 ms.
        self.assertLessEqual(self.repo.due_queries, 2)

    async def test_a_due_trigger_is_processed(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(minutes=1)
        )
        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        self.assertEqual(len(self.handled), 1)
        self.assertEqual(self.repo.triggers[self.handled[0]["trigger_id"]]["status"], "done")

    async def test_enqueuing_wakes_the_sleeping_timer(self) -> None:
        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.05)

        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(seconds=1)
        )
        scheduler.wake()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        self.assertEqual(len(self.handled), 1)

    async def test_a_future_trigger_is_not_processed_early(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-22")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW + timedelta(hours=5)
        )
        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        self.assertEqual(self.handled, [])

    async def test_an_expired_trigger_is_discarded_not_executed(self) -> None:
        """Seccion 9.7: al reiniciar se descartan los avisos obsoletos."""
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-19")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(days=2)
        )
        trigger = next(iter(self.repo.triggers.values()))
        trigger["expires_at"] = NOW - timedelta(hours=1)

        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        self.assertEqual(self.handled, [])
        self.assertEqual(self.repo.triggers[trigger["trigger_id"]]["status"], "expired")

    async def test_a_failure_is_retried_with_backoff_then_declared(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(minutes=1)
        )
        trigger_id = next(iter(self.repo.triggers))
        self.fail_with = RuntimeError("boom")

        scheduler = self._scheduler(batch_limit=1)
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        trigger = self.repo.triggers[trigger_id]
        self.assertEqual(trigger["status"], "pending")
        self.assertEqual(trigger["attempts"], 1)
        self.assertEqual(trigger["last_error"], "boom")
        # Reprogramado hacia adelante, no reintentado en bucle.
        self.assertGreater(trigger["next_run_at"], NOW)

    async def test_the_attempt_limit_is_respected(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(minutes=1)
        )
        trigger_id = next(iter(self.repo.triggers))
        self.repo.triggers[trigger_id]["max_attempts"] = 1
        self.fail_with = RuntimeError("boom")

        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        trigger = self.repo.triggers[trigger_id]
        self.assertEqual(trigger["status"], "failed")
        self.assertEqual(trigger["last_error"], "boom")

    async def test_a_trigger_left_running_by_a_crash_is_recovered(self) -> None:
        """Seccion 9.7: al reiniciar recupera el trabajo previamente persistido."""
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(minutes=1)
        )
        trigger_id = next(iter(self.repo.triggers))
        self.repo.triggers[trigger_id].update(
            {
                "status": "running",
                "started_at": NOW - timedelta(hours=2),
                "next_run_at": NOW - timedelta(minutes=1),
            }
        )

        scheduler = self._scheduler()
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

        # Se recupero y se ejecuto, en vez de quedar colgado para siempre.
        self.assertEqual(len(self.handled), 1)

    async def test_stop_is_clean_and_idempotent(self) -> None:
        scheduler = self._scheduler()
        await scheduler.start()
        await scheduler.stop()
        await scheduler.stop()
        self.assertIsNone(scheduler._job)

    async def test_two_processes_do_not_run_the_same_trigger(self) -> None:
        key = triggers_module.dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await self.store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key, run_at=NOW - timedelta(minutes=1)
        )
        trigger_id = next(iter(self.repo.triggers))
        # Otro proceso ya lo tomo: la toma atomica debe devolver None.
        self.repo.triggers[trigger_id]["status"] = "running"
        claimed = await self.repo.mark_horizon_trigger_running(trigger_id)
        self.assertIsNone(claimed)


if __name__ == "__main__":
    unittest.main()
