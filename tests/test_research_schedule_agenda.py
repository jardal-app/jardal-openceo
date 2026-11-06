"""Agenda research del kernel de schedule global: contrato con dobles (ticket #564).

Solo logica/contrato con dobles y reloj virtual; ninguna llamada a modelos vivos
(orden del Owner 2026-10-14). La propiedad que se demuestra, ademas del contrato
``next_due``/``claim_due``/``run``/``settle``/``recover_at_boot``, es que la
migracion conserva cadencia, backoff y admision del scheduler viejo, y que la
recuperacion por ``boot_id`` libera una toma joven de otro arranque (D3 de
TASK-2026-00441), cosa que el umbral por edad no hacia.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from research.schedule_agenda import ResearchScheduleAgenda  # noqa: E402
from research.scheduler import (  # noqa: E402
    ResearchScheduleRequest,
    ResearchScheduleStatus,
    build_schedule_document,
)
from schedule_kernel import Outcome, ScheduleKernel  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class _VirtualWaiter:
    """Como el del oraculo: con presupuesto agotado espera al evento de verdad."""

    def __init__(self, clock: _Clock, budget: int = 0) -> None:
        self.clock = clock
        self.budget = budget
        self.calls: list[float | None] = []
        self._timeouts = 0

    async def __call__(self, event: asyncio.Event, timeout: float | None) -> bool:
        self.calls.append(timeout)
        if timeout is None or self._timeouts >= self.budget:
            await event.wait()
            return True
        self._timeouts += 1
        self.clock.now = self.clock.now + timedelta(seconds=timeout)
        return False


class _FakeRepo:
    """Repo en memoria con la semantica de Mongo que la Agenda necesita.

    Espejo del ``_FakeRepo`` del oraculo mas ``claimed_boot_id`` y la
    recuperacion por arranque; no demuestra atomicidad (eso lo cubre el oraculo
    contra Mongo real), solo el contrato de la Agenda.
    """

    def __init__(self) -> None:
        self.schedules: dict[str, dict] = {}
        self.claim_calls = 0
        self.next_due_calls = 0

    async def get_research_schedule(self, schedule_id: str) -> dict | None:
        row = self.schedules.get(schedule_id)
        return dict(row) if row else None

    async def claim_next_research_schedule(
        self, *, lease_owner: str, lease_seconds: int, now: datetime,
        boot_id: str | None = None,
    ) -> dict | None:
        self.claim_calls += 1
        due = [
            r for r in self.schedules.values()
            if r.get("status") == "active" and r.get("run_state") == "idle"
            and _aware(r["next_run_at"]) <= now
        ]
        if not due:
            return None
        due.sort(key=lambda r: _aware(r["next_run_at"]))
        row = due[0]
        row["run_state"] = "running"
        row["lease_owner"] = lease_owner
        row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        row["scheduled_for"] = row.get("scheduled_for") or row["next_run_at"]
        row["fencing_token"] = int(row.get("fencing_token") or 0) + 1
        if boot_id is not None:
            row["claimed_boot_id"] = boot_id
        return dict(row)

    async def patch_research_schedule(
        self, schedule_id: str, updates: dict, *,
        lease_owner: str | None = None, fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        row = self.schedules.get(schedule_id)
        if row is None:
            return None
        if lease_owner is not None and (
            row.get("lease_owner") != lease_owner or row.get("run_state") != "running"
        ):
            return None
        if fencing_token is not None and int(row.get("fencing_token") or 0) != int(fencing_token):
            return None
        row.update(updates)
        return dict(row)

    async def next_research_schedule_due_at(self) -> datetime | None:
        self.next_due_calls += 1
        active = [
            r for r in self.schedules.values()
            if r.get("status") == "active" and r.get("run_state") == "idle"
        ]
        if not active:
            return None
        return min(_aware(r["next_run_at"]) for r in active)

    async def recover_research_schedules_for_boot(
        self, boot_id: str, *, now: datetime | None = None
    ) -> int:
        recovered = 0
        for row in self.schedules.values():
            if row.get("run_state") != "running":
                continue
            if row.get("claimed_boot_id") == boot_id:
                continue
            row["run_state"] = "idle"
            row["lease_owner"] = None
            row["lease_expires_at"] = None
            row["claimed_boot_id"] = None
            row["pending_reconcile"] = True
            row["last_error"] = "recovered_after_restart"
            if row.get("status") == "active" and _aware(row["next_run_at"]) <= now:
                row["next_run_at"] = now
            recovered += 1
        return recovered

    async def expire_research_schedules(self, now: datetime) -> int:
        count = 0
        for row in self.schedules.values():
            expires_at = row.get("expires_at")
            if row.get("status") == "active" and expires_at and _aware(expires_at) <= now:
                row["status"] = "expired"
                count += 1
        return count


def _seed(repo: _FakeRepo, *, run_at: datetime, **overrides) -> dict:
    payload: dict = {"workflow": "research.watch_tick", "interval_seconds": 1800}
    payload.update(overrides)
    doc = build_schedule_document(
        project_key="p", owner_id="o", purpose="watch",
        spec=ResearchScheduleRequest(**payload), dedupe_key=None, now=NOW,
    )
    doc["next_run_at"] = run_at
    repo.schedules[doc["schedule_id"]] = doc
    return doc


def _agenda(repo, handler, clock, **kw) -> ResearchScheduleAgenda:
    return ResearchScheduleAgenda(repo=repo, handler=handler, now_fn=clock, **kw)


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.handled: list[dict] = []

    async def _handler(self, schedule: dict) -> dict:
        self.handled.append(schedule)
        return {"ok": True}

    async def _recovered_agenda(self, **kw) -> ResearchScheduleAgenda:
        agenda = _agenda(self.repo, self._handler, self.clock, **kw)
        await agenda.recover_at_boot("boot-1")
        return agenda

    async def test_next_due_es_una_consulta_al_vencimiento_reclamable(self) -> None:
        _seed(self.repo, run_at=NOW + timedelta(seconds=300))
        _seed(self.repo, run_at=NOW + timedelta(seconds=60))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=60))
        self.assertEqual(self.repo.next_due_calls, 1)

    async def test_next_due_none_cuando_no_hay_nada_reclamable(self) -> None:
        agenda = await self._recovered_agenda()
        self.assertIsNone(await agenda.next_due())

    async def test_claim_due_toma_hasta_el_limite_y_sella_el_boot(self) -> None:
        first = _seed(self.repo, run_at=NOW - timedelta(seconds=10))
        second = _seed(self.repo, run_at=NOW - timedelta(seconds=5))
        _seed(self.repo, run_at=NOW + timedelta(hours=1))
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 2)
        self.assertEqual([c.item_id for c in claims],
                         [first["schedule_id"], second["schedule_id"]])
        for claim in claims:
            self.assertEqual(claim.boot_id, "boot-1")
            self.assertEqual(claim.fencing_token, 1)
            stored = await self.repo.get_research_schedule(claim.item_id)
            self.assertEqual(
                (stored["run_state"], stored["claimed_boot_id"]), ("running", "boot-1"))
        # El futuro no se toca y un tercer reclamo no encuentra nada vencido.
        self.assertEqual(await agenda.claim_due(NOW, 2), [])

    async def test_claim_due_sin_boot_no_reclama_nada(self) -> None:
        _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        agenda = _agenda(self.repo, self._handler, self.clock)
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        self.assertEqual(self.repo.claim_calls, 0)

    async def test_run_ok_envuelve_el_resultado_del_handler(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        self.assertEqual((outcome.ok, outcome.result), (True, {"ok": True}))
        self.assertEqual(self.handled[0]["schedule_id"], doc["schedule_id"])

    async def test_run_que_falla_devuelve_ok_false_con_el_texto_del_error(self) -> None:
        async def failing(_schedule: dict) -> dict:
            raise RuntimeError("boom")

        _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        agenda = _agenda(self.repo, failing, self.clock)
        await agenda.recover_at_boot("boot-1")
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        self.assertEqual((outcome.ok, outcome.error), (False, "boom"))

    async def test_settle_ok_cierra_con_proxima_ocurrencia_futura(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(minutes=5))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        await agenda.settle(claim, outcome)
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(
            (stored["run_state"], stored["attempts"], stored["last_error"],
             stored["pending_reconcile"]),
            ("idle", 0, None, False))
        self.assertEqual(stored["last_result"], {"ok": True})
        self.assertEqual(stored["execution_key"], f"{doc['schedule_id']}:20261001T115500.000000Z")
        self.assertIsNone(stored["scheduled_for"])
        self.assertGreater(_aware(stored["next_run_at"]), NOW)

    async def test_settle_ok_con_skipped_tambien_cierra_la_ocurrencia(self) -> None:
        """Un tick omitido por admision avanza la cadencia, no reintenta."""

        async def skipped(_schedule: dict) -> dict:
            return {"skipped": "schedule_inactive"}

        doc = _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        agenda = _agenda(self.repo, skipped, self.clock)
        await agenda.recover_at_boot("boot-1")
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual((stored["run_state"], stored["attempts"]), ("idle", 0))
        self.assertEqual(stored["last_result"], {"skipped": "schedule_inactive"})
        self.assertGreater(_aware(stored["next_run_at"]), NOW)

    async def test_settle_err_reintenta_con_backoff_y_luego_retoma_cadencia(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(seconds=1), max_attempts=2)
        agenda = await self._recovered_agenda(backoff_base=60)
        [first] = await agenda.claim_due(NOW, 1)
        await agenda.settle(first, Outcome(ok=False, error="boom"))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(
            (stored["run_state"], stored["attempts"], stored["last_error"]),
            ("idle", 1, "boom"))
        self.assertEqual(_aware(stored["next_run_at"]), NOW + timedelta(seconds=60))
        # Segundo fallo: agota los intentos y vuelve a su cadencia.
        [second] = await agenda.claim_due(NOW + timedelta(seconds=60), 1)
        await agenda.settle(second, Outcome(ok=False, error="boom"))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual((stored["attempts"], stored["scheduled_for"]), (0, None))
        self.assertGreater(_aware(stored["next_run_at"]), NOW + timedelta(seconds=60))

    async def test_settle_con_fencing_viejo_se_ignora(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        # Otro ejecutor tomo de nuevo: el token ya no es el del claim.
        self.repo.schedules[doc["schedule_id"]]["fencing_token"] = 99
        await agenda.settle(claim, Outcome(ok=True, result={"ok": True}))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(stored["run_state"], "running")
        self.assertIsNone(stored["last_result"])

    async def test_overlap_skip_y_merge_se_contabilizan(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(seconds=1),
                    overlap_policy="merge")
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        # La ejecucion tarda mas que el intervalo: 3 h para intervalo de 30 min.
        self.clock.now = NOW + timedelta(hours=3)
        await agenda.settle(claim, Outcome(ok=True, result={"ok": True}))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual((stored["skipped_runs"], stored["merged_runs"]), (0, 6))

    async def test_recover_libera_una_toma_joven_de_otro_arranque(self) -> None:
        """D3: 5 s de edad se libera igual; por edad (900 s) quedaba perdida."""
        doc = _seed(self.repo, run_at=NOW - timedelta(hours=1))
        doc["run_state"] = "running"
        doc["lease_owner"] = "dead-coordinator"
        doc["lease_expires_at"] = NOW + timedelta(seconds=115)
        doc["claimed_boot_id"] = "boot-old"
        doc["scheduled_for"] = doc["next_run_at"]
        agenda = _agenda(self.repo, self._handler, self.clock)
        recovered = await agenda.recover_at_boot("boot-new")
        self.assertEqual(recovered, 1)
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(
            (stored["run_state"], stored["pending_reconcile"], stored["last_error"]),
            ("idle", True, "recovered_after_restart"))
        # Un solo catch-up: se ejecuta ahora, no una rafaga.
        self.assertEqual(_aware(stored["next_run_at"]), NOW)
        # Y las tomas de este arranque no se tocan.
        [claim] = await agenda.claim_due(NOW, 1)
        self.assertEqual(await agenda.recover_at_boot("boot-new"), 0)
        self.assertEqual(claim.boot_id, "boot-new")

    async def test_recover_libera_tomas_viejas_sin_boot_y_expira(self) -> None:
        legacy = _seed(self.repo, run_at=NOW - timedelta(seconds=1))
        legacy["run_state"] = "running"
        legacy["lease_owner"] = "old-loop"
        legacy["lease_expires_at"] = NOW - timedelta(hours=1)
        expired = _seed(self.repo, run_at=NOW + timedelta(hours=1),
                        expires_at=NOW - timedelta(minutes=1))
        agenda = _agenda(self.repo, self._handler, self.clock)
        self.assertEqual(await agenda.recover_at_boot("boot-new"), 1)
        self.assertEqual(
            (await self.repo.get_research_schedule(legacy["schedule_id"]))["run_state"],
            "idle")
        self.assertEqual(
            (await self.repo.get_research_schedule(expired["schedule_id"]))["status"],
            ResearchScheduleStatus.EXPIRED.value)


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """La Agenda sirve bajo el kernel real: una pasada vencida corre una vez."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.handled: list[dict] = []

    async def _handler(self, schedule: dict) -> dict:
        self.handled.append(schedule)
        return {"ok": True}

    async def test_vencido_corre_una_vez_y_la_espera_apunta_al_futuro(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(minutes=5))
        waiter = _VirtualWaiter(self.clock)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = ResearchScheduleAgenda(repo=self.repo, handler=self._handler,
                                        now_fn=self.clock)
        kernel.register(agenda)
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.handled), 1)
            stored = await self.repo.get_research_schedule(doc["schedule_id"])
            self.assertGreater(_aware(stored["next_run_at"]), self.clock())
            # None mientras la toma esta en vuelo; 1800 s tras liquidar.
            self.assertEqual(waiter.calls, [None, 1800.0])
            self.assertEqual(kernel.status()["agendas"]["research"]["recovered"], 0)
        finally:
            await kernel.stop()

    async def test_toma_joven_de_otro_boot_se_recupera_y_dispara_una_vez(self) -> None:
        doc = _seed(self.repo, run_at=NOW - timedelta(hours=1))
        doc["run_state"] = "running"
        doc["lease_owner"] = "dead-coordinator"
        doc["lease_expires_at"] = NOW + timedelta(seconds=115)
        doc["claimed_boot_id"] = "boot-old"
        doc["scheduled_for"] = doc["next_run_at"]
        waiter = _VirtualWaiter(self.clock)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = ResearchScheduleAgenda(repo=self.repo, handler=self._handler,
                                        now_fn=self.clock)
        kernel.register(agenda)
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.handled), 1)
            self.assertTrue(self.handled[0]["pending_reconcile"])
            stored = await self.repo.get_research_schedule(doc["schedule_id"])
            self.assertFalse(stored["pending_reconcile"])
            self.assertGreater(_aware(stored["next_run_at"]), self.clock())
            status = kernel.status()["agendas"]["research"]
            self.assertEqual(status["recovered"], 1)
        finally:
            await kernel.stop()


class RuntimeWiringTests(unittest.IsolatedAsyncioTestCase):
    """El runtime arranca el kernel (inyectado o propio), no el bucle aislado."""

    async def test_start_arranca_el_kernel_inyectado_y_liquida_un_vencido(self) -> None:
        from unittest.mock import AsyncMock, Mock

        from research.workflows import ResearchScheduleRuntime
        from settings import Settings

        repo = _FakeRepo()
        clock = _Clock()
        waiter = _VirtualWaiter(clock)
        kernel = ScheduleKernel(now_fn=clock, waiter=waiter)
        workflows = Mock()
        workflows.execute_research_workflow = AsyncMock(return_value={"ok": True})

        async def resolve(owner_id: str):
            return Mock()

        cfg = Settings(_env_file=None, DEEPSEEK_API_KEY="",
                       RESEARCH_ENABLED=True, RESEARCH_SCHEDULER_ENABLED=True)
        runtime = ResearchScheduleRuntime(
            repo=repo, workflows=workflows, service=Mock(), settings=cfg,
            project_cfg_for=lambda _: {}, resolve_principal=resolve, kernel=kernel,
            now_fn=clock)
        self.assertIs(runtime.kernel, kernel)
        doc = _seed(repo, run_at=NOW - timedelta(seconds=1),
                    workflow="research.investigate")
        try:
            self.assertTrue(await runtime.start())
            await asyncio.sleep(0.05)
            workflows.execute_research_workflow.assert_awaited_once()
            stored = await repo.get_research_schedule(doc["schedule_id"])
            self.assertEqual(stored["run_state"], "idle")
            self.assertGreater(_aware(stored["next_run_at"]), clock())
            # El bucle aislado no arranca: compatibilidad, no produccion.
            self.assertIsNone(runtime.scheduler._job)
            # El wake del scheduler viejo delega al kernel sin romperse.
            runtime.scheduler.wake()
        finally:
            await runtime.stop()

    async def test_disabled_no_toca_el_repo_ni_arranca_el_kernel(self) -> None:
        from unittest.mock import Mock

        from research.workflows import ResearchScheduleRuntime
        from settings import Settings

        repo = Mock()
        workflows = Mock()
        cfg = Settings(_env_file=None, DEEPSEEK_API_KEY="",
                       RESEARCH_ENABLED=True, RESEARCH_SCHEDULER_ENABLED=False)
        runtime = ResearchScheduleRuntime(
            repo=repo, workflows=workflows, service=Mock(), settings=cfg,
            project_cfg_for=lambda _: {})
        self.assertFalse(await runtime.start())
        await runtime.stop()
        self.assertEqual(repo.method_calls, [])
        self.assertFalse(runtime.kernel.status()["running"])

    async def test_apagado_el_kernel_compartido_no_ejecuta_los_schedules(self) -> None:
        from unittest.mock import AsyncMock, Mock

        from research.workflows import ResearchScheduleRuntime
        from settings import Settings

        for research_enabled, scheduler_enabled in [(False, False), (False, True), (True, False)]:
            with self.subTest(research=research_enabled, scheduler=scheduler_enabled):
                repo = _FakeRepo()
                clock = _Clock()
                kernel = ScheduleKernel(now_fn=clock, waiter=_VirtualWaiter(clock))
                workflows = Mock()
                workflows.execute_research_workflow = AsyncMock(return_value={"ok": True})
                cfg = Settings(_env_file=None, DEEPSEEK_API_KEY="",
                               RESEARCH_ENABLED=research_enabled,
                               RESEARCH_SCHEDULER_ENABLED=scheduler_enabled)
                runtime = ResearchScheduleRuntime(
                    repo=repo, workflows=workflows, service=Mock(), settings=cfg,
                    project_cfg_for=lambda _: {}, kernel=kernel, now_fn=clock)
                doc = _seed(repo, run_at=NOW - timedelta(seconds=1),
                            workflow="research.investigate")
                try:
                    # main.py arranca siempre el kernel compartido, con research apagado o no.
                    await kernel.start()
                    await asyncio.sleep(0.05)
                    with self.assertNoLogs("schedule_kernel.kernel", level="WARNING"):
                        runtime.store.notify()
                        runtime.scheduler.wake()
                    await asyncio.sleep(0.05)
                    workflows.execute_research_workflow.assert_not_awaited()
                    self.assertEqual((repo.claim_calls, repo.next_due_calls), (0, 0))
                    stored = await repo.get_research_schedule(doc["schedule_id"])
                    self.assertEqual(stored["run_state"], "idle")
                    self.assertNotIn("research", kernel.status()["agendas"])
                finally:
                    await runtime.stop()


if __name__ == "__main__":
    unittest.main()
