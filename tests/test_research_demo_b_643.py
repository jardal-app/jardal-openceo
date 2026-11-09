"""Demo B con el feed autorizado y scheduler recurrente (ticket #643).

Verifica, sin red viva ni modelos, todo lo anterior al flip productivo: la
suscripcion acotada apunta al feed autorizado unico, dos reinicios conservan
identidad/vencimiento/cursor, un tick observado admite trabajo acotado bajo
politica quick-only con gasto cero, la admision por tick respeta su tope y
cancelar detiene la adquisicion. Servicio simulado sobre Mongo real con
tiempo virtual (arres de ``test_research_acceptance_b``); el modelo es un
Mock que ningun tick debe tocar.

No prueba: el feed publico real (la sonda de solo lectura lo registra en
``docs/research-operations.md``) ni el flip de flags + restart en produccion,
paso manual del Owner ya autorizado.
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_research_acceptance_b as acc
from research.contracts import ResearchJobRequest
from research.demo_b import (
    DEMO_B_FEED_URL,
    DEMO_B_INTERVAL_SECONDS,
    demo_b_bounds_problems,
    demo_b_source,
    demo_b_subscription_request,
)
from research.policies import derive_effective_policy
from research.subscriptions import SubscriptionRequest
from research.watch import normalize_source, source_ref
from research.workflows import ResearchScheduleRuntime
from settings import Settings

FEED = "https://github.com/jardal-app/jardal-openceo/releases.atom"
CFG = {"research": {"enabled": True, "allowed_modes": ["quick"], "classification_ceiling": "public",
       "paid_budget_usd": "0.00",
       "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False}}}


def utc(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class DemoBSpecTests(unittest.TestCase):
    def test_feed_is_the_single_authorized_one(self):
        self.assertEqual(DEMO_B_FEED_URL, FEED)
        self.assertEqual(demo_b_source(), {"type": "rss", "url": FEED})
        request = demo_b_subscription_request()
        self.assertIsInstance(request, SubscriptionRequest)
        self.assertEqual(request.source, {"type": "rss", "url": FEED})
        # La identidad normalizada es estable: la misma fuente escrita con
        # puerto por defecto y fragmento colapsa a la misma referencia.
        twin = normalize_source(
            {"type": "rss", "url": "https://github.com:443/jardal-app/jardal-openceo/releases.atom#noctua"})
        self.assertEqual(source_ref(twin), source_ref(demo_b_source()))

    def test_bounds_fit_the_default_server_and_demo_policy(self):
        settings = Settings(_env_file=None)
        self.assertGreaterEqual(DEMO_B_INTERVAL_SECONDS,
                                settings.RESEARCH_SUBSCRIPTIONS_MIN_INTERVAL_SECONDS)
        self.assertEqual(demo_b_bounds_problems(settings, CFG), [])

    def test_bounds_flag_a_misconfigured_deployment(self):
        strict = Settings(_env_file=None, RESEARCH_SUBSCRIPTIONS_MIN_INTERVAL_SECONDS=7200)
        self.assertTrue(any("minimo" in p for p in demo_b_bounds_problems(strict, CFG)))
        internal = {"research": {**CFG["research"], "classification_ceiling": "internal"}}
        self.assertTrue(any("public" in p for p in demo_b_bounds_problems(Settings(_env_file=None), internal)))
        deep_only = {"research": {**CFG["research"], "allowed_modes": ["deep"]}}
        self.assertTrue(any("quick" in p for p in demo_b_bounds_problems(Settings(_env_file=None), deep_only)))
        disabled = {"research": {**CFG["research"], "enabled": False}}
        self.assertTrue(any("habilitado" in p for p in demo_b_bounds_problems(Settings(_env_file=None), disabled)))
        missing = {"research": {k: v for k, v in CFG["research"].items() if k != "enabled"}}
        self.assertTrue(any("habilitado" in p for p in demo_b_bounds_problems(Settings(_env_file=None), missing)))

    def test_scheduler_stays_off_until_both_flags_are_on(self):
        def runtime(**flags):
            settings = Settings(_env_file=None, **flags)
            return ResearchScheduleRuntime(repo=Mock(), workflows=Mock(), service=Mock(),
                                           settings=settings, project_cfg_for=lambda _: CFG)

        off, scheduler_only, service_only = (
            runtime(), runtime(RESEARCH_SCHEDULER_ENABLED=True), runtime(RESEARCH_ENABLED=True))
        for candidate in (off, scheduler_only, service_only):
            self.assertFalse(candidate.enabled)
        import asyncio
        self.assertFalse(asyncio.run(off.start()))
        self.assertTrue(runtime(RESEARCH_ENABLED=True, RESEARCH_SCHEDULER_ENABLED=True).enabled)


@unittest.skipUnless(acc.support.MONGO_AVAILABLE, "Mongo required for Demo B lifecycle")
class DemoBLifecycleTests(unittest.IsolatedAsyncioTestCase):
    DB = "openceo_test_research_demo_b"

    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(acc.support.MONGO_URL)
        await self.client.drop_database(self.DB)
        self.repo = acc.support.build_repo(self.DB)
        self.repo._client = self.client
        await self.repo.ensure_indexes()
        for user in (acc.OWNER, acc.PEER):
            await self.repo.db.users.insert_one(
                {"user_id": user, "username": user, "email": f"{user}@example.org",
                 "is_active": True, "all_permissions": ["run_research", "read_research"],
                 "allowed_projects": [acc.PROJECT]})
        self.vt = acc.VirtualTime(acc.START)
        self.feed = acc.FeedServer(self.vt)
        self.dep = acc.Deployment(self.repo, self.vt, self.feed)
        self.repo.create_task = AsyncMock(side_effect=AssertionError("business task created"))
        self.repo.patch_task = AsyncMock(side_effect=AssertionError("business task patched"))
        self.live = [self.dep]

    async def asyncTearDown(self):
        for dep in self.live:
            await dep.stop()
        await self.client.drop_database(self.DB)
        self.client.close()

    async def restart(self):
        await self.dep.stop()
        self.dep = acc.Deployment(self.repo, self.vt, self.feed)
        self.live.append(self.dep)
        await self.dep.start()
        return self.dep

    async def subscribe_demo_b(self):
        view, created = await self.dep.subs.subscribe(
            demo_b_subscription_request(), principal=acc.principal(acc.OWNER), project_cfg=CFG)
        self.assertTrue(created)
        self.assertEqual(view["source"]["url"], FEED)
        self.assertEqual(view["cadence"]["interval_seconds"], DEMO_B_INTERVAL_SECONDS)
        return view["subscription_id"]

    async def jobs(self):
        return [j async for j in self.repo.research_jobs.find({})]

    async def events(self, sid, **query):
        return [e async for e in self.repo.research_watch_events.find(
            {"subscription_id": sid, **query})]

    def snapshot(self, doc):
        return {f: doc[f] for f in ("schedule_id", "next_run_at", "cursor", "dedupe_key",
                                   "owner_id", "params", "fencing_token")}

    async def test_two_restarts_keep_identity_due_and_cursor_cancel_stops_acquisition(self):
        self.feed.publish(FEED, acc.support.watch_item("v1", "Release v1"))
        sid = await self.subscribe_demo_b()
        await self.dep.start()
        await self.dep.run_until(acc.START + timedelta(hours=1))  # linea base
        self.feed.publish(FEED, acc.support.watch_item("v2", "Release v2"))
        await self.dep.run_until(acc.START + timedelta(hours=2))  # tick observado
        [job] = await self.jobs()
        self.assertEqual(job["mode"], "quick")
        self.assertEqual(job["classification"], "public")
        self.assertEqual(job["seed_urls"], ["https://example.org/jobs/v2"])
        self.assertEqual(job["question"], demo_b_subscription_request().question)
        self.assertEqual(job["paid_budget_micro_usd"], 0)
        policy = derive_effective_policy(
            principal=acc.principal(acc.OWNER),
            request=ResearchJobRequest(project_key=acc.PROJECT, purpose="demo-b",
                                       question=job["question"], mode="quick"),
            project_cfg=CFG, settings=self.dep.cfg)
        self.assertEqual(job["effective_limits"], policy.limits.model_dump())
        self.assertEqual(self.dep.model.method_calls, [])
        before = self.snapshot(await self.repo.get_research_schedule(sid))

        await self.restart()
        self.assertEqual(self.snapshot(await self.repo.get_research_schedule(sid)), before)
        self.assertEqual(utc((await self.repo.get_research_schedule(sid))["next_run_at"]),
                         acc.START + timedelta(hours=3))
        await self.dep.run_until(acc.START + timedelta(hours=3))
        self.assertEqual(self.feed.log[-1][2].get("etag"), '"v2"')  # 304 con validadores
        self.assertEqual(len(await self.jobs()), 1)

        before_second = self.snapshot(await self.repo.get_research_schedule(sid))
        await self.restart()
        self.assertEqual(self.snapshot(await self.repo.get_research_schedule(sid)), before_second)
        self.feed.publish(FEED, acc.support.watch_item("v3", "Release v3"))
        await self.dep.run_until(acc.START + timedelta(hours=4))
        self.assertEqual(len(await self.jobs()), 2)  # sigue detectando tras dos reinicios

        await self.dep.subs.cancel(sid, principal=acc.principal(acc.OWNER))
        calls = len(self.feed.times(FEED))
        await self.dep.run_until(acc.START + timedelta(hours=8))
        self.assertEqual(len(self.feed.times(FEED)), calls)
        self.assertEqual((await self.repo.get_research_schedule(sid))["status"], "cancelled")
        self.assertEqual(await self.events(sid, status="pending"), [])
        self.repo.create_task.assert_not_called()
        self.repo.patch_task.assert_not_called()

    async def test_tick_admission_is_capped_and_reconciles_without_duplicates(self):
        cap = self.dep.cfg.RESEARCH_WATCH_MAX_ITEMS_PER_TICK
        self.feed.publish(FEED, acc.support.watch_item("seed", "Release seed"))
        sid = await self.subscribe_demo_b()
        await self.dep.start()
        await self.dep.run_until(acc.START + timedelta(hours=1))
        self.feed.publish(FEED, *(acc.support.watch_item(f"r{i}", f"Release r{i}") for i in range(8)))
        await self.dep.run_until(acc.START + timedelta(hours=2))
        [first] = await self.jobs()
        self.assertEqual(len(first["seed_urls"]), cap)
        doc = await self.repo.get_research_schedule(sid)
        self.assertEqual(doc["last_result"]["backlog"], 8 - cap)
        await self.dep.run_until(acc.START + timedelta(hours=3))
        jobs = await self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(jobs[1]["seed_urls"]), 8 - cap)
        keys = [e["event_key"] for e in await self.events(sid)]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(await self.events(sid, status="pending"), [])
