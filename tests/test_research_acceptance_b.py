"""Aceptacion B (offline) del research recurrente: suscripciones sin duplicacion (TASK-2026-00070).

Cubre las demostraciones del ticket que **no** necesitan red ni encender el servicio vivo:
un "despliegue" completo (servicio, coordinator, motor de vigilancia, API de suscripciones)
sobre Mongo real con **tiempo virtual**, reinicios simulados creando otro despliegue sobre la
misma base, y fallos inyectados en cada punto del tick. El tiempo solo avanza cuando el test
lo mueve: el timer real duerme sin tope y se despierta por evento o vencimiento exacto.

No prueba: reinicio del servicio desplegado, observacion contra una fuente publica real ni
activacion de las flags en produccion. Eso exige evidencia real y autorizacion del dueño; los
fixtures de aqui no prueban conectividad ni calidad de ninguna fuente (TASK-2026-00070 queda
abierto hasta entonces).
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))
import research_support as support
from research.acquisition.base import ProviderDisabledError
from research.policies import Principal, RESEARCH_RUN
from research.scheduler import STALE_RUNNING_SECONDS
from research.service import ResearchDependencies, ResearchService
from research.subscriptions import ResearchSubscriptions, SubscriptionRequest
from research.watch import SourcePage
from research.workflows import ResearchScheduleRuntime, principal_resolver_for
from settings import Settings
from workflow_service import WorkflowService

PROJECT = "jardal-openceo"
OWNER, PEER = "usr_acc_owner", "usr_acc_peer"
CFG = {"research": {"enabled": True, "allowed_modes": ["quick"], "classification_ceiling": "public",
        "paid_budget_usd": "0.00",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False}}}
START = datetime(2027, 3, 13, 12, 0, tzinfo=timezone.utc)  # el dia antes del cambio de hora de EE. UU.
HOUR = 3600


def utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def settings(**kw):
    kw.setdefault("RESEARCH_ENABLED", True)
    kw.setdefault("RESEARCH_SCHEDULER_ENABLED", True)
    kw.setdefault("RESEARCH_SCHEDULER_BACKOFF_SECONDS", 0)  # reintento inmediato en tiempo virtual
    kw.setdefault("RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER", 20)
    return Settings(_env_file=None, DEEPSEEK_API_KEY="", **kw)


class VirtualTime:
    """Reloj y timer virtuales: el tiempo solo avanza cuando el test lo mueve."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.parked = asyncio.Event()
        self.timeouts: list[float | None] = []

    def __call__(self) -> datetime:
        return self.now

    async def waiter(self, event: asyncio.Event, timeout: float | None) -> bool:
        self.timeouts.append(timeout)
        if not event.is_set():
            # Solo cuenta como "quieto" si de verdad va a esperar: un evento ya activo hace que
            # el bucle vuelva a drenar antes de dormir.
            self.parked.set()
        await event.wait()
        return True

    async def settle(self) -> None:
        await asyncio.wait_for(self.parked.wait(), 10)
        await asyncio.sleep(0.05)

    async def move_to(self, scheduler, target: datetime) -> None:
        self.now = target
        self.parked.clear()
        scheduler.wake()
        await self.settle()


class FeedServer(support.ScriptedAdapter):
    """Fuentes sinteticas con validadores: sirve ``items`` por URL y responde 304 si no cambian."""

    def __init__(self, vt: VirtualTime) -> None:
        super().__init__()
        self.vt = vt
        self.feeds: dict[str, list] = {}
        self.versions: dict[str, int] = {}
        self.log: list[tuple[datetime, str, dict]] = []
        self.fail_next: Exception | None = None
        self.degrade: tuple | None = None
        self.on_fetch = None

    def publish(self, url: str, *items) -> None:
        self.feeds.setdefault(url, []).extend(items)
        self.versions[url] = self.versions.get(url, 0) + 1

    def times(self, url: str) -> list[datetime]:
        return [at for at, called, _ in self.log if called == url]

    async def fetch_page(self, source, *, page_token, conditional, scope, deadline):
        url = source["url"]
        self.log.append((self.vt.now, url, dict(conditional or {})))
        if self.on_fetch is not None:
            await self.on_fetch(url)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if self.degrade is not None:
            return SourcePage(degradations=self.degrade)
        version = self.versions.get(url, 0)
        etag = f'"v{version}"'
        if conditional and conditional.get("etag") == etag:
            return SourcePage(not_modified=True, etag=etag, content_hash=f"h{version}")
        return SourcePage(items=list(self.feeds.get(url, [])), etag=etag, content_hash=f"h{version}")


class Deployment:
    """Un proceso del servicio. Crear otro sobre la misma base simula un reinicio."""

    def __init__(self, repo, vt: VirtualTime, feed: FeedServer, *, cfg=None) -> None:
        self.repo, self.vt = repo, vt
        self.cfg = cfg or settings()
        self.model = Mock()  # el runtime del modelo: ningun tick debe tocarlo
        self.service = ResearchService(settings=self.cfg, deps=ResearchDependencies(repo=repo, runtime=self.model))
        self.workflows = WorkflowService(settings=self.cfg, repo=repo, realtime_hub=Mock())
        self.runtime = ResearchScheduleRuntime(
            repo=repo, workflows=self.workflows, service=self.service, settings=self.cfg,
            project_cfg_for=lambda _: CFG, adapters={"rss": feed}, now_fn=vt, waiter=vt.waiter)
        self.subs = ResearchSubscriptions(store=self.runtime.store, repo=repo, settings=self.cfg,
                                          resolve_principal=principal_resolver_for(repo), now_fn=vt)

    @property
    def scheduler(self):
        return self.runtime.scheduler

    async def start(self) -> None:
        self.vt.parked.clear()
        assert await self.runtime.start(), "el coordinator exige ambas flags"
        await self.settle()

    async def stop(self) -> None:
        await self.runtime.stop()

    async def settle(self) -> None:
        await self.vt.settle()
        await self.drain()

    async def drain(self, timeout: float = 10.0) -> None:
        """Quiescencia bajo el kernel (#564): el carril liquida despues del parked.

        Con el bucle aislado, ``settle()`` implicaba tick liquidado (ejecucion
        inline). Con carriles hay que esperar ademas a que no quede nada en
        vuelo ni nada vencido sin reclamar. Cada ronda fuerza una pasada del
        kernel (lectura fresca, sin CACHE) y exige dos rondas quietas seguidas:
        observar ``status`` y el repo en dos lecturas separadas tiene una
        carrera (una toma puede caer en medio), pero no dos veces seguidas.
        Solo arnes de test.
        """
        calm = 0
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if asyncio.get_running_loop().time() > deadline:
                status = self.runtime.kernel.status()["agendas"]["research"]
                due = await self.repo.next_research_schedule_due_at()
                raise AssertionError(
                    f"sin quiescence: in_flight={status['in_flight']} due={due} now={self.vt.now}")
            self.vt.parked.clear()
            self.scheduler.wake()  # delega al kernel: pasada con lectura fresca
            await asyncio.wait_for(self.vt.parked.wait(), timeout)
            status = self.runtime.kernel.status()["agendas"]["research"]
            due = await self.repo.next_research_schedule_due_at()
            if status["in_flight"] == 0 and (due is None or utc(due) > self.vt.now):
                calm += 1
                if calm >= 2:
                    return
            else:
                calm = 0
                await asyncio.sleep(0.01)

    async def run_until(self, limit: datetime) -> None:
        """Mueve el tiempo de vencimiento en vencimiento hasta ``limit`` (inclusive)."""
        while True:
            due = await self.repo.next_research_schedule_due_at()
            if due is None or utc(due) > limit:
                break
            await self.vt.move_to(self.scheduler, max(utc(due), self.vt.now))
            await self.drain()
        self.vt.now = max(self.vt.now, limit)


def principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, permissions=frozenset({RESEARCH_RUN, "read_research"}),
                     allowed_projects=frozenset({PROJECT}))


def request(url: str, **kw) -> SubscriptionRequest:
    payload = {"project_key": PROJECT, "purpose": "brief", "question": "Que cambio?",
               "source": {"type": "rss", "url": url}, "interval_seconds": HOUR}
    payload.update(kw)
    return SubscriptionRequest(**payload)


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required for acceptance B")
class AcceptanceBTests(unittest.IsolatedAsyncioTestCase):
    DB = "openceo_test_research_acceptance_b"

    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(support.MONGO_URL)
        await self.client.drop_database(self.DB)
        self.repo = support.build_repo(self.DB)
        self.repo._client = self.client
        await self.repo.ensure_indexes()
        for user in (OWNER, PEER):
            await self.repo.db.users.insert_one({"user_id": user, "username": user, "email": f"{user}@example.org",
                "is_active": True, "all_permissions": [RESEARCH_RUN, "read_research"], "allowed_projects": [PROJECT]})
        self.vt = VirtualTime(START)
        self.feed = FeedServer(self.vt)
        self.dep = Deployment(self.repo, self.vt, self.feed)
        self.repo.create_task = AsyncMock(side_effect=AssertionError("business task created"))
        self.repo.patch_task = AsyncMock(side_effect=AssertionError("business task patched"))
        self.live: list[Deployment] = [self.dep]

    async def asyncTearDown(self):
        for dep in self.live:
            await dep.stop()
        await self.client.drop_database(self.DB)
        self.client.close()

    async def restart(self) -> Deployment:
        """Reinicio: el proceso viejo muere y uno nuevo arranca sobre la misma base."""
        await self.dep.stop()
        self.dep = Deployment(self.repo, self.vt, self.feed)
        self.live.append(self.dep)
        await self.dep.start()
        return self.dep

    async def subscribe(self, url, who=OWNER, **kw):
        view, created = await self.dep.subs.subscribe(request(url, **kw), principal=principal(who), project_cfg=CFG)
        return view["subscription_id"]

    async def doc(self, sid):
        return await self.repo.get_research_schedule(sid)

    async def jobs(self):
        return [j async for j in self.repo.research_jobs.find({})]

    async def events(self, sid=None, **query):
        if sid:
            query["subscription_id"] = sid
        return [e async for e in self.repo.research_watch_events.find(query)]

    def item(self, key, title="Vacante de datos"):
        return support.watch_item(key, title)

    # ------------------------------------------------------------ 1. cadencias y DST

    async def test_30_minute_hourly_and_4_hour_subscriptions_fire_exactly_on_their_anchors(self):
        urls = {1800: "https://example.org/a.xml", HOUR: "https://example.org/b.xml", 4 * HOUR: "https://example.org/c.xml"}
        for seconds, url in urls.items():
            self.feed.publish(url, self.item("seed"))
            await self.subscribe(url, interval_seconds=seconds)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=8))
        for seconds, url in urls.items():
            expected = [START + timedelta(seconds=seconds * k) for k in range(1, 8 * HOUR // seconds + 1)]
            self.assertEqual(self.feed.times(url), expected, f"cadencia de {seconds} s")
        self.assertEqual(await self.jobs(), [])  # linea base: nada que investigar

    async def test_daily_subscription_across_the_spring_forward_gap_runs_at_the_next_valid_time(self):
        url = "https://example.org/dst.xml"
        self.feed.publish(url, self.item("seed"))
        await self.subscribe(url, interval_seconds=None, daily_at="02:30", timezone="America/New_York")
        await self.dep.start()
        await self.dep.run_until(START + timedelta(days=3))
        # 02:30 no existe el 14 de marzo (salto de 02:00 a 03:00): corre a las 03:30 EDT = 07:30 UTC
        self.assertEqual(self.feed.times(url), [
            datetime(2027, 3, 14, 7, 30, tzinfo=timezone.utc),
            datetime(2027, 3, 15, 6, 30, tzinfo=timezone.utc),
            datetime(2027, 3, 16, 6, 30, tzinfo=timezone.utc)])

    async def test_daily_subscription_across_the_fall_back_repeat_runs_once_on_the_first_pass(self):
        self.vt.now = datetime(2027, 11, 6, 12, 0, tzinfo=timezone.utc)
        url = "https://example.org/dst2.xml"
        self.feed.publish(url, self.item("seed"))
        await self.subscribe(url, interval_seconds=None, daily_at="01:30", timezone="America/New_York")
        await self.dep.start()
        await self.dep.run_until(self.vt.now + timedelta(days=2))
        # 01:30 ocurre dos veces el 7 de noviembre: se elige la primera (EDT = 05:30 UTC), una sola vez
        self.assertEqual(self.feed.times(url), [
            datetime(2027, 11, 7, 5, 30, tzinfo=timezone.utc),
            datetime(2027, 11, 8, 6, 30, tzinfo=timezone.utc)])

    # ------------------------------------------------------------ 2. reinicio

    async def test_restart_keeps_identity_due_time_and_cursor_and_duplicates_nothing(self):
        url = "https://example.org/r.xml"
        self.feed.publish(url, self.item("a"), self.item("b"))
        sid = await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))  # linea base
        self.feed.publish(url, self.item("c", "Datos nuevos"))
        await self.dep.run_until(START + timedelta(hours=2))
        before = await self.doc(sid)
        self.assertEqual((len(await self.jobs()), len(await self.events(sid))), (1, 3))

        await self.restart()
        after = await self.doc(sid)
        for field in ("schedule_id", "next_run_at", "cursor", "dedupe_key", "owner_id", "params", "fencing_token"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(utc(after["next_run_at"]), START + timedelta(hours=3))

        # nada cambio: el tick tras el reinicio manda los validadores del cursor y recibe un 304
        await self.dep.run_until(START + timedelta(hours=3))
        self.assertEqual(self.feed.log[-1][2].get("etag"), '"v2"')
        self.assertEqual((len(await self.jobs()), len(await self.events(sid))), (1, 3))
        # y sigue detectando cambios despues del reinicio, sin volver a empezar la linea base
        self.feed.publish(url, self.item("d", "Mas datos"))
        await self.dep.run_until(START + timedelta(hours=4))
        self.assertEqual((len(await self.jobs()), len(await self.events(sid))), (2, 4))
        self.assertEqual(len(await self.events(sid, status="baseline")), 2)

    async def test_a_long_outage_yields_one_catch_up_not_a_burst(self):
        url = "https://example.org/o.xml"
        self.feed.publish(url, self.item("a"))
        sid = await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        await self.dep.stop()
        calls_before = len(self.feed.times(url))
        self.vt.now = START + timedelta(hours=1, days=2)  # 48 intervalos perdidos
        self.dep = Deployment(self.repo, self.vt, self.feed)
        self.live.append(self.dep)
        await self.dep.start()
        self.assertEqual(len(self.feed.times(url)), calls_before + 1)
        after = await self.doc(sid)
        self.assertGreater(utc(after["next_run_at"]), self.vt.now)

    # ------------------------------------------------------------ 3. crash en cada punto

    async def _scenario(self, name: str) -> tuple[str, str]:
        """Suscripcion con linea base hecha y un item nuevo pendiente de detectar."""
        url = f"https://example.org/crash-{name}.xml"
        self.feed.publish(url, self.item("seed"))
        sid = await self.subscribe(url)
        if not self.scheduler_started:
            await self.dep.start()
            self.scheduler_started = True
        return sid, url

    scheduler_started = False

    async def _arm(self, name):
        sid, url = await self._scenario(name)
        await self.dep.run_until(self.vt.now + timedelta(hours=1))  # linea base
        self.feed.publish(url, self.item("nuevo", "Datos nuevos"))
        return sid, url

    async def _assert_exactly_once(self, sid, url):
        events = [e for e in await self.events(sid) if e["status"] != "baseline"]
        self.assertEqual([e["url"] for e in events], ["https://example.org/jobs/nuevo"])  # un evento por item y scope
        jobs = [j for j in await self.jobs() if j["idempotency_key"].startswith(f"watch:{sid}:")]
        self.assertEqual([j["seed_urls"] for j in jobs], [["https://example.org/jobs/nuevo"]])
        self.assertEqual(events[0]["status"], "admitted")

    @staticmethod
    def fail_once(owner, name, snapshot=None):
        """El primer llamado muere (tras tomar una foto del estado); los demas pasan al original."""
        real = getattr(owner, name)
        state = {"crashed": False, "snapshot": None}

        async def wrapper(*args, **kwargs):
            if not state["crashed"]:
                state["crashed"] = True
                if snapshot is not None:
                    state["snapshot"] = await snapshot()
                raise RuntimeError(f"crash en {name}")
            return await real(*args, **kwargs)

        setattr(owner, name, wrapper)
        return state

    async def test_crash_during_acquisition_is_retried_without_duplicates(self):
        sid, url = await self._arm("acq")
        self.feed.fail_next = RuntimeError("la fuente cae a mitad de lectura")
        await self.dep.run_until(self.vt.now + timedelta(hours=1))
        self.assertEqual(len(self.feed.times(url)), 3)  # linea base + intento fallido + reintento
        await self._assert_exactly_once(sid, url)
        doc = await self.doc(sid)
        self.assertEqual((doc["attempts"], doc["last_error"]), (0, None))

    async def test_crash_while_storing_events_is_retried_without_duplicates(self):
        sid, url = await self._arm("store")
        state = self.fail_once(self.repo, "insert_research_watch_events")
        await self.dep.run_until(self.vt.now + timedelta(hours=1))
        self.assertTrue(state["crashed"])
        await self._assert_exactly_once(sid, url)

    async def test_crash_after_events_before_the_cursor_commit_is_retried_without_duplicates(self):
        sid, url = await self._arm("cursor")

        async def snapshot():
            return ((await self.doc(sid))["cursor"]["content_hash"], len(await self.events(sid, status="pending")))

        state = self.fail_once(self.dep.runtime.watch, "_commit_cursor", snapshot)
        await self.dep.run_until(self.vt.now + timedelta(hours=1))
        # al morir, el cursor NO habia avanzado pero el evento ya era durable
        self.assertEqual(state["snapshot"], ("h1", 1))
        await self._assert_exactly_once(sid, url)
        self.assertEqual((await self.doc(sid))["cursor"]["content_hash"], "h2")

    async def test_crash_after_admitting_the_job_reconciles_to_the_same_job(self):
        sid, url = await self._arm("admit")

        async def snapshot():
            return len(await self.jobs())

        state = self.fail_once(self.repo, "mark_research_watch_events", snapshot)
        await self.dep.run_until(self.vt.now + timedelta(hours=1))
        self.assertEqual(state["snapshot"], 1)  # el job ya existia cuando murio
        await self._assert_exactly_once(sid, url)

    async def test_process_death_after_the_claim_is_declared_and_recovered_once(self):
        sid, url = await self._arm("claim")
        due = utc((await self.doc(sid))["next_run_at"])
        await self.dep.stop()
        # el proceso toma la ocurrencia y muere antes de hacer nada
        claimed = await self.repo.claim_next_research_schedule(lease_owner="muerto", lease_seconds=120, now=due)
        self.assertEqual(claimed["run_state"], "running")
        self.vt.now = due + timedelta(seconds=STALE_RUNNING_SECONDS + 600)
        cutoff = self.vt.now - timedelta(seconds=STALE_RUNNING_SECONDS)
        self.assertEqual(await self.repo.recover_stale_research_schedules(cutoff, now=self.vt.now), 1)
        recovered = await self.doc(sid)
        # no se afirma exactly-once de la red: la reentrega se declara para reconciliar
        self.assertEqual((recovered["pending_reconcile"], recovered["last_error"]), (True, "recovered_after_restart"))
        calls = len(self.feed.times(url))
        self.dep = Deployment(self.repo, self.vt, self.feed)
        self.live.append(self.dep)
        await self.dep.start()
        self.assertEqual(len(self.feed.times(url)), calls + 1)  # un solo catch-up
        await self._assert_exactly_once(sid, url)
        final = await self.doc(sid)
        self.assertFalse(final["pending_reconcile"])
        self.assertGreater(utc(final["next_run_at"]), self.vt.now)

    # ------------------------------------------------------------ 4. sin cambios, fuente caida, owners

    async def test_unchanged_source_makes_no_model_or_job_calls(self):
        url = "https://example.org/quiet.xml"
        self.feed.publish(url, self.item("a"))
        sid = await self.subscribe(url)
        submit = AsyncMock(wraps=self.dep.service.submit)
        self.dep.service.submit = submit
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=6))
        self.assertEqual(len(self.feed.times(url)), 6)
        submit.assert_not_awaited()
        self.assertEqual(self.dep.model.method_calls, [])
        self.assertEqual(await self.jobs(), [])
        self.assertTrue((await self.doc(sid))["last_result"]["not_modified"])

    async def test_inaccessible_source_records_its_cause_and_keeps_the_cursor(self):
        url = "https://example.org/down.xml"
        self.feed.publish(url, self.item("a"))
        sid = await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        good = (await self.doc(sid))["cursor"]
        self.feed.degrade = (ProviderDisabledError("403 del origen", provider="rss").degradation(),)
        await self.dep.run_until(START + timedelta(hours=2))
        doc = await self.doc(sid)
        self.assertEqual(doc["last_result"]["status"], "degraded")
        self.assertTrue(doc["last_result"]["coverage_gaps"])
        for field in ("etag", "content_hash", "initialized"):
            self.assertEqual(doc["cursor"][field], good[field], field)
        self.assertEqual(doc["cursor"]["consecutive_failures"], 1)
        self.assertEqual(await self.jobs(), [])
        self.feed.degrade = None  # se recupera sin intervencion y limpia el contador
        await self.dep.run_until(START + timedelta(hours=3))
        self.assertEqual((await self.doc(sid))["cursor"]["consecutive_failures"], 0)

    async def test_two_owners_on_the_same_source_stay_independent(self):
        url = "https://example.org/shared.xml"
        self.feed.publish(url, self.item("a"))
        mine = await self.subscribe(url, OWNER)
        theirs = await self.subscribe(url, PEER)
        self.assertNotEqual(mine, theirs)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        self.feed.publish(url, self.item("b", "Datos nuevos"))
        await self.dep.run_until(START + timedelta(hours=2))
        jobs = await self.jobs()
        self.assertEqual(sorted(j["owner_id"] for j in jobs), [OWNER, PEER])
        for sid, owner in ((mine, OWNER), (theirs, PEER)):
            events = await self.events(sid)
            self.assertEqual({e["owner_id"] for e in events}, {owner})
            self.assertEqual(len(events), 2)
        # cancelar una no detiene a la otra
        await self.dep.subs.cancel(mine, principal=principal(OWNER))
        before = len(self.feed.times(url))
        await self.dep.run_until(START + timedelta(hours=4))
        self.assertEqual(len(self.feed.times(url)), before + 2)  # solo el ticker de PEER
        self.assertEqual((await self.doc(mine))["status"], "cancelled")

    # ------------------------------------------------------------ 5. timer, overlap, revocacion

    async def test_idle_timer_does_not_poll_and_a_new_subscription_wakes_it(self):
        await self.dep.start()
        self.assertEqual(self.vt.timeouts[-1], None)  # sin vencimiento: espera indefinida
        claim = AsyncMock(wraps=self.repo.claim_next_research_schedule)
        due = AsyncMock(wraps=self.repo.next_research_schedule_due_at)
        self.repo.claim_next_research_schedule, self.repo.next_research_schedule_due_at = claim, due
        self.vt.now = START + timedelta(hours=10)  # mas alla del max_sleep legacy de Horizon (3600 s)
        await asyncio.sleep(0.2)
        self.assertEqual((claim.await_count, due.await_count), (0, 0))  # ni una consulta
        # crear desde la superficie real despierta al coordinator con el vencimiento exacto
        self.vt.parked.clear()
        url = "https://example.org/wake.xml"
        self.feed.publish(url, self.item("a"))
        await self.subscribe(url)
        await self.dep.settle()
        self.assertEqual(self.vt.timeouts[-1], HOUR)
        self.assertGreaterEqual(due.await_count, 1)

    async def test_pausing_and_cancelling_stop_future_ticks_and_wake_the_timer(self):
        url_a, url_b = "https://example.org/pa.xml", "https://example.org/pb.xml"
        for url in (url_a, url_b):
            self.feed.publish(url, self.item("a"))
        a, b = await self.subscribe(url_a), await self.subscribe(url_b)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        self.vt.parked.clear()
        await self.dep.subs.pause(a, principal=principal(OWNER))
        await self.dep.subs.cancel(b, principal=principal(OWNER))
        await self.dep.settle()
        self.assertEqual(self.vt.timeouts[-1], None)  # nada activo: vuelve a esperar sin tope
        calls = (len(self.feed.times(url_a)), len(self.feed.times(url_b)))
        await self.dep.run_until(START + timedelta(hours=10))
        self.assertEqual((len(self.feed.times(url_a)), len(self.feed.times(url_b))), calls)
        # reanudar tras una pausa larga ejecuta UN catch-up y retoma su cadencia
        self.vt.parked.clear()
        resumed = await self.dep.subs.resume(a, principal=principal(OWNER), project_cfg_for=lambda _: CFG)
        await self.dep.settle()
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(len(self.feed.times(url_a)), calls[0] + 1)
        self.assertGreater(utc((await self.doc(a))["next_run_at"]), self.vt.now)
        self.assertEqual(len(self.feed.times(url_b)), calls[1])  # la cancelada sigue muerta

    async def test_a_slow_run_is_counted_per_overlap_policy(self):
        for policy, counter in (("skip", "skipped_runs"), ("merge", "merged_runs")):
            url = f"https://example.org/slow-{policy}.xml"
            self.feed.publish(url, self.item("a"))
            schedule, _ = await self.dep.runtime.store.create(
                project_key=PROJECT, owner_id=OWNER, purpose="slow",
                spec={"workflow": "research.watch_tick", "interval_seconds": HOUR, "overlap_policy": policy,
                      "params": {"source": {"type": "rss", "url": url}, "question": "q"}}, now=self.vt.now)
            sid = schedule["schedule_id"]

            async def slow(called, target=url):  # la lectura tarda 2,5 intervalos de tiempo virtual
                if called == target:
                    self.vt.now += timedelta(hours=2, minutes=30)
                    self.feed.on_fetch = None

            if not self.scheduler_started:
                await self.dep.start()
                self.scheduler_started = True
            self.feed.on_fetch = slow
            await self.dep.run_until(self.vt.now + timedelta(hours=1))
            doc = await self.doc(sid)
            self.assertEqual(doc[counter], 2, policy)
            other = "merged_runs" if counter == "skipped_runs" else "skipped_runs"
            self.assertEqual(doc[other], 0, policy)
            self.assertGreater(utc(doc["next_run_at"]), self.vt.now, policy)

    async def test_a_revoked_owner_pauses_the_subscription_and_no_further_tick_runs(self):
        url = "https://example.org/revoked.xml"
        self.feed.publish(url, self.item("a"))
        sid = await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        await self.repo.db.users.update_one({"user_id": OWNER}, {"$set": {"is_active": False}})
        calls = len(self.feed.times(url))
        await self.dep.run_until(START + timedelta(hours=2))
        doc = await self.doc(sid)
        self.assertEqual((doc["status"], doc["status_reason"], doc["attempts"]), ("paused", "access_revoked", 0))
        self.assertEqual(len(self.feed.times(url)), calls)  # ni siquiera toco la fuente
        await self.dep.run_until(START + timedelta(hours=9))
        self.assertEqual(len(self.feed.times(url)), calls)

    # ------------------------------------------------------------ 6. fronteras y retencion

    async def test_ticks_never_create_business_tasks(self):
        url = "https://example.org/tasks.xml"
        self.feed.publish(url, self.item("a"))
        await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        self.feed.publish(url, self.item("b", "Datos nuevos"))
        await self.dep.run_until(START + timedelta(hours=2))
        self.assertEqual(len(await self.jobs()), 1)
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)
        self.repo.create_task.assert_not_called()
        self.repo.patch_task.assert_not_called()

    async def test_job_retention_never_touches_schedules_or_leases_and_dedup_outlives_the_job(self):
        schedules = await self.repo.research_schedules.index_information()
        self.assertFalse([n for n, spec in schedules.items() if "expireAfterSeconds" in spec])
        jobs = await self.repo.research_jobs.index_information()
        self.assertEqual({n: s["key"] for n, s in jobs.items() if "expireAfterSeconds" in s},
                         {"idx_research_jobs_ttl": [("expires_at", 1)]})

        url = "https://example.org/ret.xml"
        self.feed.publish(url, self.item("a"))
        sid = await self.subscribe(url)
        await self.dep.start()
        await self.dep.run_until(START + timedelta(hours=1))
        self.feed.publish(url, self.item("b", "Datos nuevos"))
        await self.dep.run_until(START + timedelta(hours=2))
        [job] = await self.jobs()
        self.assertNotIn("expires_at", job)  # vivo (queued): el TTL no lo toca
        # el job termina y el TTL de Mongo lo retira: la dedup vive en los eventos, no en el job
        await self.repo.transition_research_job(job["research_id"], "running")
        done = await self.repo.transition_research_job(job["research_id"], "completed")
        self.assertIn("expires_at", done)
        await self.repo.research_jobs.delete_one({"research_id": job["research_id"]})
        events_before = await self.events(sid)
        self.feed.publish(url)  # misma fuente, sin items nuevos pero con otra version del cuerpo
        await self.dep.run_until(START + timedelta(hours=4))
        self.assertEqual(await self.jobs(), [])  # no se vuelve a admitir lo ya visto
        self.assertEqual(len(await self.events(sid)), len(events_before))
        self.assertEqual((await self.doc(sid))["status"], "active")


if __name__ == "__main__":
    unittest.main()
