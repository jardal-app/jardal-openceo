"""Suscripciones research: servicio, API HTTP y fachada MCP (TASK-2026-00069).

El servicio corre contra Mongo real (schedules, eventos). La API se prueba sobre ASGI con el
middleware y el JWT del proyecto, y la fachada MCP sobre un transporte MCP HTTP real, igual
que la fachada de jobs: invocar solo la funcion del handler no demostraria autorizacion.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))
import httpx
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import research_support as support
from research.consumers import ConsumerRegistry
from research.mcp_facade import FACADE_TOOLS, build_research_mcp_facade
from research.policies import Principal, ResearchAccessDenied, ResearchPolicyError
from research.router import build_research_router
from research.scheduler import ResearchScheduleRequest, ResearchScheduleStore, build_schedule_document
from research.service import ResearchDependencies, ResearchService
from research.subscriptions import (
    ResearchSubscriptions, SubscriptionConflict, SubscriptionLimitExceeded, SubscriptionNotFound,
    SubscriptionRequest, SubscriptionUpdate, SubscriptionValidationError, public_subscription,
)
from research.workflows import principal_resolver_for
from settings import Settings
from test_research_facade import USERS, _FakeAuthDB, _client, _cookie

PROJECT = "jardal-openceo"
CFG = {"research": {"enabled": True, "allowed_modes": ["quick"], "classification_ceiling": "public",
        "paid_budget_usd": "0.00",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False}}}
CFG_NO_SEARCH = {"research": {**CFG["research"], "adapters": {"brave": False}}}
SOURCE = {"type": "rss", "url": "https://example.org/jobs.xml"}


def settings(**kw):
    kw.setdefault("RESEARCH_ENABLED", True)
    return Settings(_env_file=None, DEEPSEEK_API_KEY="", **kw)


def principal(user_id="usr_owner", *, run=True, projects=(PROJECT,), admin=False):
    perms = {"read_research"} | ({"run_research"} if run else set())
    return Principal(user_id=user_id, permissions=frozenset(perms), allowed_projects=frozenset(projects),
                     is_admin=admin)


def watch(**kw):
    payload = {"project_key": PROJECT, "purpose": "brief", "question": "Que cambio?", "source": SOURCE,
               "interval_seconds": 3600}
    payload.update(kw)
    return SubscriptionRequest(**payload)


def discovery(**kw):
    payload = {"project_key": PROJECT, "purpose": "brief", "question": "Fuentes nuevas", "kind": "discovery",
               "interval_seconds": 86400}
    payload.update(kw)
    return SubscriptionRequest(**payload)


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required for subscription integration")
class SubscriptionFixture(unittest.IsolatedAsyncioTestCase):
    DB = "openceo_test_research_subscriptions"
    CAP = 10

    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(support.MONGO_URL)
        await self.client.drop_database(self.DB)
        self.repo = support.build_repo(self.DB)
        self.repo._client = self.client
        await self.repo.ensure_indexes()
        for user in USERS:
            await self.repo.db.users.insert_one(dict(user))
        self.cfg = settings(RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER=self.CAP)
        self.store = ResearchScheduleStore(self.repo, settings=self.cfg)
        self.subs = ResearchSubscriptions(store=self.store, repo=self.repo, settings=self.cfg,
                                          resolve_principal=principal_resolver_for(self.repo))
        self.owner = principal()

    async def asyncTearDown(self):
        await self.client.drop_database(self.DB)
        self.client.close()

    async def raw(self, subscription_id):
        return await self.repo.get_research_schedule(subscription_id)

    async def event(self, subscription_id, key, status="pending"):
        await self.repo.insert_research_watch_events([{
            "subscription_id": subscription_id, "owner_id": "usr_owner", "project_key": PROJECT,
            "event_key": key, "item_key": key, "content_fp": "f", "kind": "new", "status": status,
            "title": key, "url": f"https://example.org/{key}", "first_seen_at": datetime.now(timezone.utc)}])


class SubscriptionServiceTests(SubscriptionFixture):
    # ------------------------------------------------------------ crear

    async def test_watch_subscription_is_normalized_persisted_and_publicly_whitelisted(self):
        view, created = await self.subs.subscribe(
            watch(source={"type": "RSS", "url": "HTTPS://Example.ORG:443/jobs.xml?b=1&a=2"},
                  filter={"include_terms": ["Datos", "Ingeniería"], "exclude_terms": ["Junior"]}),
            principal=self.owner, project_cfg=CFG, created_by_consumer="jobhunter")
        self.assertTrue(created)
        self.assertEqual(view["source"], {"type": "rss", "url": "https://example.org/jobs.xml?a=2&b=1"})
        self.assertEqual(view["filter"], {"include_terms": ["datos", "ingenieria"], "exclude_terms": ["junior"]})
        self.assertEqual((view["kind"], view["status"], view["backfill"]), ("watch", "active", False))
        self.assertEqual(view["cadence"], {"interval_seconds": 3600, "daily_at": None, "timezone": "UTC"})
        for hidden in ("lease_owner", "fencing_token", "dedupe_key", "run_state", "owner_id", "params"):
            self.assertNotIn(hidden, view)
        stored = await self.raw(view["subscription_id"])
        self.assertEqual((stored["workflow"], stored["owner_id"], stored["created_by_consumer"]),
                         ("research.watch_tick", "usr_owner", "jobhunter"))
        self.assertEqual(stored["kind"], "watch")

    async def test_subscribing_never_spends_or_reads_the_network(self):
        self.repo.research_jobs.insert_one = None  # cualquier admision de job reventaria
        await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        self.assertEqual(await self.repo.research_watch_events.count_documents({}), 0)

    async def test_same_identity_and_spec_is_idempotent_and_other_spec_conflicts(self):
        first, created = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        again, created_again = await self.subs.subscribe(
            watch(filter={"include_terms": []}), principal=self.owner, project_cfg=CFG)
        self.assertEqual((created, created_again, again["subscription_id"]), (True, False, first["subscription_id"]))
        with self.assertRaises(SubscriptionConflict):
            await self.subs.subscribe(watch(interval_seconds=7200), principal=self.owner, project_cfg=CFG)
        with self.assertRaises(SubscriptionConflict):
            await self.subs.subscribe(watch(question="Otra pregunta"), principal=self.owner, project_cfg=CFG)

    async def test_filter_term_order_is_not_a_different_spec(self):
        first, _ = await self.subs.subscribe(
            watch(filter={"include_terms": ["b", "a"]}), principal=self.owner, project_cfg=CFG)
        again, created = await self.subs.subscribe(
            watch(filter={"include_terms": ["a", "b"]}), principal=self.owner, project_cfg=CFG)
        self.assertEqual((created, again["subscription_id"]), (False, first["subscription_id"]))

    async def test_idempotency_key_opens_a_distinct_subscription_for_the_same_source(self):
        one, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        two, created = await self.subs.subscribe(
            watch(interval_seconds=7200, idempotency_key="cadencia-lenta"), principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)
        self.assertNotEqual(one["subscription_id"], two["subscription_id"])

    async def test_two_owners_with_the_same_source_are_independent(self):
        other = principal("usr_other", projects=("otro-proyecto",))
        cfg = CFG
        mine, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=cfg)
        theirs, created = await self.subs.subscribe(
            watch(project_key="otro-proyecto"), principal=other, project_cfg=cfg)
        self.assertTrue(created)
        self.assertNotEqual(mine["subscription_id"], theirs["subscription_id"])
        # mismo proyecto y fuente pero otro owner: tambien independiente
        peer = principal("usr_peer")
        again, created = await self.subs.subscribe(watch(), principal=peer, project_cfg=cfg)
        self.assertTrue(created)
        self.assertEqual((await self.raw(again["subscription_id"]))["owner_id"], "usr_peer")

    async def test_discovery_subscription_has_its_own_cadence_floor_and_cap_params(self):
        view, created = await self.subs.subscribe(
            discovery(max_searches_per_day=1, count=5), principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)
        self.assertEqual((view["kind"], view["max_searches_per_day"], view["count"]), ("discovery", 1, 5))
        self.assertNotIn("source", view)
        stored = await self.raw(view["subscription_id"])
        self.assertEqual(stored["workflow"], "research.discovery_tick")
        with self.assertRaises(SubscriptionValidationError):  # 6 h minimo para descubrir
            await self.subs.subscribe(discovery(question="otra", interval_seconds=3600),
                                      principal=self.owner, project_cfg=CFG)

    async def test_discovery_rejects_include_terms_but_allows_exclusions(self):
        with self.assertRaises(SubscriptionValidationError):
            await self.subs.subscribe(discovery(filter={"include_terms": ["ingeniero"]}),
                                      principal=self.owner, project_cfg=CFG)
        view, _ = await self.subs.subscribe(discovery(filter={"exclude_terms": ["spam"]}),
                                            principal=self.owner, project_cfg=CFG)
        with self.assertRaises(SubscriptionValidationError):
            await self.subs.update(view["subscription_id"], SubscriptionUpdate(filter={"include_terms": ["x"]}),
                                   principal=self.owner, project_cfg_for=lambda _: CFG)
        ok = await self.subs.update(view["subscription_id"], SubscriptionUpdate(filter={"exclude_terms": ["a", "b"]}),
                                    principal=self.owner, project_cfg_for=lambda _: CFG)
        self.assertEqual(ok["filter"]["exclude_terms"], ["a", "b"])
        self.assertNotIn("reopened_events", ok)

    async def test_subscribing_again_after_cancel_or_expiry_creates_a_new_subscription(self):
        first, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        await self.subs.cancel(first["subscription_id"], principal=self.owner)
        second, created = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)
        self.assertNotEqual(first["subscription_id"], second["subscription_id"])
        self.assertEqual(second["status"], "active")
        self.assertEqual((await self.subs.get(first["subscription_id"], principal=self.owner))["status"], "cancelled")
        # lo mismo al expirar
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        await self.repo.patch_research_schedule(second["subscription_id"], {"expires_at": past})
        self.assertEqual(await self.repo.expire_research_schedules(datetime.now(timezone.utc)), 1)
        third, created = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)
        self.assertEqual(third["status"], "active")
        # y un cambio de spec tras cancelar ya no es un 409 contra el fantasma
        await self.subs.cancel(third["subscription_id"], principal=self.owner)
        again, created = await self.subs.subscribe(watch(interval_seconds=7200), principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)

    async def test_a_late_pause_cannot_resurrect_a_cancelled_subscription(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        await self.subs.cancel(sid, principal=self.owner)
        # un tick en vuelo que se deniega y pausa despues de cancelar (la comprobacion previa
        # del servicio ya paso): la escritura no puede pisar el estado terminal
        self.assertIsNone(await self.store.pause(sid, reason="access_revoked"))
        self.assertIsNone(await self.store.resume(sid))
        stored = await self.raw(sid)
        self.assertEqual((stored["status"], stored["status_reason"]), ("cancelled", None))
        # y el servicio traduce la carrera a un conflicto en lugar de devolver un estado falso
        real_get = self.store.get

        async def stale_get(subscription_id):
            doc = await real_get(subscription_id)
            return dict(doc, status="paused") if doc else doc

        self.store.get = stale_get
        for call in (lambda: self.subs.pause(sid, principal=self.owner),
                     lambda: self.subs.resume(sid, principal=self.owner, project_cfg_for=lambda _: CFG)):
            with self.assertRaises(SubscriptionConflict):
                await call()

    async def test_broadening_a_filter_reopens_the_filtered_events_it_now_matches(self):
        view, _ = await self.subs.subscribe(watch(filter={"include_terms": ["datos"]}),
                                            principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        for key, title, status in [("a", "Analista de Python", "filtered"), ("b", "Contador", "filtered"),
                                   ("c", "Python senior", "baseline"), ("d", "Python admitido", "admitted")]:
            await self.repo.insert_research_watch_events([{
                "subscription_id": sid, "owner_id": "usr_owner", "project_key": PROJECT, "event_key": key,
                "item_key": key, "content_fp": "f", "kind": "new", "status": status, "title": title,
                "url": f"https://example.org/{key}", "first_seen_at": datetime.now(timezone.utc)}])
        updated = await self.subs.update(sid, SubscriptionUpdate(filter={"include_terms": ["datos", "python"]}),
                                         principal=self.owner, project_cfg_for=lambda _: CFG)
        self.assertEqual(updated["reopened_events"], 1)
        statuses = {e["event_key"]: e["status"] for e in await self.subs.events(sid, principal=self.owner)}
        # solo el filtrado que ahora coincide; la linea base y lo ya admitido no se tocan
        self.assertEqual(statuses, {"a": "pending", "b": "filtered", "c": "baseline", "d": "admitted"})
        unchanged = await self.subs.update(sid, SubscriptionUpdate(question="Otra"), principal=self.owner,
                                           project_cfg_for=lambda _: CFG)
        self.assertNotIn("reopened_events", unchanged)

    async def test_validation_errors(self):
        bad = [watch(source=None), watch(source={"type": "rss", "url": "ftp://x"}),
               watch(source={"type": "lever", "board": "../x"}),
               watch(interval_seconds=None), watch(interval_seconds=3600, daily_at="08:00"),
               watch(interval_seconds=60), watch(interval_seconds=None, daily_at="25:99"),
               watch(timezone="No/Existe"), watch(filter={"include_terms": "x"}),
               watch(max_searches_per_day=2), watch(count=3),
               discovery(source=SOURCE), discovery(max_searches_per_day=0), discovery(count=99)]
        for request in bad:
            with self.assertRaises(SubscriptionValidationError, msg=request.model_dump()):
                await self.subs.subscribe(request, principal=self.owner, project_cfg=CFG)
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)
        for kwargs in [{"question": "  "}, {"question": "x" * 1001}, {"purpose": ""}, {"purpose": "p" * 121},
                       {"project_key": ""}, {"extra": 1}]:
            with self.assertRaises(ValueError):
                watch(**kwargs)

    async def test_daily_at_is_valid_below_the_interval_floor(self):
        view, _ = await self.subs.subscribe(
            watch(interval_seconds=None, daily_at="08:30", timezone="America/La_Paz"),
            principal=self.owner, project_cfg=CFG)
        self.assertEqual(view["cadence"], {"interval_seconds": None, "daily_at": "08:30", "timezone": "America/La_Paz"})

    async def test_authority_is_required_to_subscribe(self):
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.subscribe(watch(), principal=principal(run=False), project_cfg=CFG)
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.subscribe(watch(), principal=principal(projects=("otro",)), project_cfg=CFG)
        with self.assertRaises(ResearchPolicyError):
            await self.subs.subscribe(watch(), principal=self.owner, project_cfg={"research": {"enabled": False}})
        with self.assertRaises(ResearchPolicyError):  # descubrir exige buscador habilitado: sin gasto no aprobado
            await self.subs.subscribe(discovery(), principal=self.owner, project_cfg=CFG_NO_SEARCH)
        # vigilar una fuente publica no necesita buscador
        _, created = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG_NO_SEARCH)
        self.assertTrue(created)
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 0)

    # ------------------------------------------------------------ tope

    async def test_active_subscription_cap_per_owner(self):
        self.subs._settings = settings(RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER=2)
        a, _ = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/1"}),
                                         principal=self.owner, project_cfg=CFG)
        await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/2"}),
                                  principal=self.owner, project_cfg=CFG)
        with self.assertRaises(SubscriptionLimitExceeded):
            await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/3"}),
                                      principal=self.owner, project_cfg=CFG)
        # repetir una existente no cuenta como una nueva
        _, created = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/1"}),
                                               principal=self.owner, project_cfg=CFG)
        self.assertFalse(created)
        # pausar libera cupo; reanudar lo vuelve a exigir
        await self.subs.pause(a["subscription_id"], principal=self.owner)
        c, created = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/3"}),
                                               principal=self.owner, project_cfg=CFG)
        self.assertTrue(created)
        with self.assertRaises(SubscriptionLimitExceeded):
            await self.subs.resume(a["subscription_id"], principal=self.owner, project_cfg_for=lambda _: CFG)
        # el tope es por owner: otro owner no lo consume
        _, created = await self.subs.subscribe(watch(), principal=principal("usr_peer"), project_cfg=CFG)
        self.assertTrue(created)
        self.subs._settings = settings(RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER=0)
        with self.assertRaises(SubscriptionLimitExceeded):
            await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/9"}),
                                      principal=self.owner, project_cfg=CFG)

    # ------------------------------------------------------------ leer

    async def test_get_list_and_events_are_owner_scoped_with_404_for_foreign(self):
        mine, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        theirs, _ = await self.subs.subscribe(
            watch(project_key="otro-proyecto"), principal=principal("usr_other", projects=("otro-proyecto",)),
            project_cfg=CFG)
        await self.event(mine["subscription_id"], "e1")
        stranger = principal("usr_other", projects=("otro-proyecto",))
        for call in (lambda: self.subs.get(mine["subscription_id"], principal=stranger),
                     lambda: self.subs.events(mine["subscription_id"], principal=stranger),
                     lambda: self.subs.pause(mine["subscription_id"], principal=stranger),
                     lambda: self.subs.cancel(mine["subscription_id"], principal=stranger),
                     lambda: self.subs.get("sch_inexistente", principal=self.owner)):
            with self.assertRaises(SubscriptionNotFound):
                await call()
        self.assertEqual([v["subscription_id"] for v in await self.subs.list(principal=self.owner)],
                         [mine["subscription_id"]])
        self.assertEqual(await self.subs.list(principal=self.owner, project_key="otro-proyecto"), [])
        events = await self.subs.events(mine["subscription_id"], principal=self.owner)
        self.assertEqual([e["event_key"] for e in events], ["e1"])
        self.assertEqual(set(events[0]), {"event_key", "kind", "status", "title", "url", "published_at",
                                          "first_seen_at", "research_id"})
        # un owner con el proyecto retirado ya no ve la suscripcion
        revoked = principal(projects=())
        with self.assertRaises(SubscriptionNotFound):
            await self.subs.get(mine["subscription_id"], principal=revoked)
        self.assertEqual(await self.subs.list(principal=revoked), [])

    async def test_a_peer_in_the_same_project_cannot_touch_my_subscription(self):
        # El proyecto coincide a proposito: lo unico que separa a los dos es el owner.
        mine, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        sid = mine["subscription_id"]
        await self.event(sid, "e1")
        peer = principal("usr_peer")
        calls = (lambda: self.subs.get(sid, principal=peer), lambda: self.subs.events(sid, principal=peer),
                 lambda: self.subs.pause(sid, principal=peer), lambda: self.subs.cancel(sid, principal=peer),
                 lambda: self.subs.resume(sid, principal=peer, project_cfg_for=lambda _: CFG),
                 lambda: self.subs.update(sid, SubscriptionUpdate(question="x"), principal=peer,
                                          project_cfg_for=lambda _: CFG))
        for call in calls:
            with self.assertRaises(SubscriptionNotFound):
                await call()
        self.assertEqual(await self.subs.list(principal=peer), [])
        self.assertEqual((await self.raw(sid))["status"], "active")
        self.assertEqual((await self.raw(sid))["params"]["question"], "Que cambio?")

    async def test_a_non_subscription_schedule_is_invisible(self):
        doc = build_schedule_document(project_key=PROJECT, owner_id="usr_owner", purpose="brief",
            spec=ResearchScheduleRequest(workflow="research.investigate", interval_seconds=3600,
                                         params={"question": "q"}), dedupe_key=None)
        await self.repo.create_research_schedule(doc)
        with self.assertRaises(SubscriptionNotFound):
            await self.subs.get(doc["schedule_id"], principal=self.owner)
        with self.assertRaises(SubscriptionNotFound):
            await self.subs.cancel(doc["schedule_id"], principal=self.owner)
        self.assertEqual(await self.subs.list(principal=self.owner), [])

    async def test_view_reports_cursor_coverage_and_last_result(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        now = datetime.now(timezone.utc)
        await self.repo.patch_research_schedule(view["subscription_id"], {
            "cursor": {"coverage": "incomplete", "coverage_reason": "page_limit", "last_checked_at": now,
                       "consecutive_failures": 2, "etag": '"x"', "continuation": {"page_token": "t"}},
            "last_result": {"status": "ok", "admitted": None}, "last_run_at": now})
        view = await self.subs.get(view["subscription_id"], principal=self.owner)
        self.assertEqual((view["coverage"], view["coverage_reason"], view["consecutive_failures"]),
                         ("incomplete", "page_limit", 2))
        self.assertTrue(view["last_checked_at"].startswith(now.strftime("%Y-%m-%dT%H:%M")))
        self.assertNotIn("etag", str(view))
        self.assertNotIn("continuation", str(view))

    # ------------------------------------------------------------ editar

    async def test_update_changes_spec_recomputes_the_due_time_and_keeps_identity(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        before = (await self.raw(sid))["dedupe_key"]
        updated = await self.subs.update(
            sid, SubscriptionUpdate(question="Nueva pregunta", filter={"include_terms": ["Datos"]},
                                    interval_seconds=1800, backfill=True),
            principal=self.owner, project_cfg_for=lambda _: CFG)
        self.assertEqual((updated["question"], updated["filter"]["include_terms"], updated["backfill"]),
                         ("Nueva pregunta", ["datos"], True))
        self.assertEqual(updated["cadence"]["interval_seconds"], 1800)
        stored = await self.raw(sid)
        self.assertEqual(stored["dedupe_key"], before)
        self.assertEqual(stored["params"]["source"], view["source"])  # la fuente no cambia
        self.assertLess(stored["next_run_at"].replace(tzinfo=timezone.utc),
                        datetime.now(timezone.utc) + timedelta(minutes=31))
        cleared = await self.subs.update(sid, SubscriptionUpdate(filter={}), principal=self.owner,
                                         project_cfg_for=lambda _: CFG)
        self.assertEqual(cleared["filter"], {"include_terms": [], "exclude_terms": []})
        switched = await self.subs.update(sid, SubscriptionUpdate(daily_at="09:00"), principal=self.owner,
                                          project_cfg_for=lambda _: CFG)
        self.assertEqual((switched["cadence"]["daily_at"], switched["cadence"]["interval_seconds"]), ("09:00", None))
        noop = await self.subs.update(sid, SubscriptionUpdate(), principal=self.owner, project_cfg_for=lambda _: CFG)
        self.assertEqual(noop["subscription_id"], sid)

    async def test_update_rejects_identity_fields_bad_values_and_terminal_state(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        for forbidden in ({"source": SOURCE}, {"purpose": "x"}, {"project_key": "x"}, {"owner_id": "x"},
                          {"status": "active"}, {"kind": "discovery"}):
            with self.assertRaises(ValueError):
                SubscriptionUpdate(**forbidden)
        for bad in (SubscriptionUpdate(question=" "), SubscriptionUpdate(interval_seconds=120),
                    SubscriptionUpdate(filter={"include_terms": [""]}), SubscriptionUpdate(timezone="No/Existe"),
                    SubscriptionUpdate(max_searches_per_day=3)):
            with self.assertRaises(SubscriptionValidationError):
                await self.subs.update(sid, bad, principal=self.owner, project_cfg_for=lambda _: CFG)
        await self.subs.cancel(sid, principal=self.owner)
        with self.assertRaises(SubscriptionConflict):
            await self.subs.update(sid, SubscriptionUpdate(question="x"), principal=self.owner,
                                   project_cfg_for=lambda _: CFG)

    async def test_update_requires_current_authority(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.update(view["subscription_id"], SubscriptionUpdate(question="x"),
                                   principal=principal(run=False), project_cfg_for=lambda _: CFG)
        with self.assertRaises(ResearchPolicyError):
            await self.subs.update(view["subscription_id"], SubscriptionUpdate(question="x"),
                                   principal=self.owner, project_cfg_for=lambda _: {"research": {"enabled": False}})

    async def test_discovery_update_can_change_its_own_cap_and_clear_it(self):
        view, _ = await self.subs.subscribe(discovery(max_searches_per_day=2), principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        up = await self.subs.update(sid, SubscriptionUpdate(max_searches_per_day=1, count=4), principal=self.owner,
                                    project_cfg_for=lambda _: CFG)
        self.assertEqual((up["max_searches_per_day"], up["count"]), (1, 4))
        with self.assertRaises(SubscriptionValidationError):
            await self.subs.update(sid, SubscriptionUpdate(interval_seconds=3600), principal=self.owner,
                                   project_cfg_for=lambda _: CFG)

    # ------------------------------------------------------------ pausar / reanudar / cancelar

    async def test_pause_resume_cancel_lifecycle(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        sid = view["subscription_id"]
        paused = await self.subs.pause(sid, principal=self.owner)
        self.assertEqual((paused["status"], paused["status_reason"]), ("paused", None))
        again = await self.subs.pause(sid, principal=self.owner)
        self.assertEqual(again["status"], "paused")
        resumed = await self.subs.resume(sid, principal=self.owner, project_cfg_for=lambda _: CFG)
        self.assertEqual(resumed["status"], "active")
        await self.event(sid, "pend", "pending")
        await self.event(sid, "done", "admitted")
        cancelled = await self.subs.cancel(sid, principal=self.owner)
        self.assertEqual(cancelled["status"], "cancelled")
        statuses = {e["event_key"]: e["status"] for e in await self.subs.events(sid, principal=self.owner)}
        self.assertEqual(statuses, {"pend": "dropped", "done": "admitted"})  # lo admitido no se toca
        self.assertEqual((await self.subs.cancel(sid, principal=self.owner))["status"], "cancelled")
        for call in (self.subs.pause(sid, principal=self.owner),
                     self.subs.resume(sid, principal=self.owner, project_cfg_for=lambda _: CFG)):
            with self.assertRaises(SubscriptionConflict):
                await call

    async def test_resume_reauthorizes_so_a_revoked_owner_cannot_reactivate(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        await self.subs.pause(view["subscription_id"], principal=self.owner)
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.resume(view["subscription_id"], principal=principal(run=False), project_cfg_for=lambda _: CFG)
        with self.assertRaises(ResearchPolicyError):
            await self.subs.resume(view["subscription_id"], principal=self.owner,
                                   project_cfg_for=lambda _: {"research": {"enabled": False}})
        self.assertEqual((await self.raw(view["subscription_id"]))["status"], "paused")

    async def test_stopping_never_needs_authority(self):
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        no_run = principal(run=False)
        self.assertEqual((await self.subs.pause(view["subscription_id"], principal=no_run))["status"], "paused")
        self.assertEqual((await self.subs.cancel(view["subscription_id"], principal=no_run))["status"], "cancelled")

    async def test_pausing_and_cancelling_wake_the_timer(self):
        woken = []
        self.store.bind_waker(lambda: woken.append(1))
        view, _ = await self.subs.subscribe(watch(), principal=self.owner, project_cfg=CFG)
        await self.subs.update(view["subscription_id"], SubscriptionUpdate(interval_seconds=1800),
                               principal=self.owner, project_cfg_for=lambda _: CFG)
        await self.subs.pause(view["subscription_id"], principal=self.owner)
        await self.subs.cancel(view["subscription_id"], principal=self.owner)
        self.assertEqual(len(woken), 4)  # crear, editar, pausar, cancelar

    # ------------------------------------------------------------ consumidores

    async def test_principal_for_consumer_is_the_account_capped_by_the_grant(self):
        got = await self.subs.principal_for_consumer("usr_owner", [PROJECT, "otro-proyecto"])
        self.assertEqual((got.allowed_projects, got.is_admin), (frozenset({PROJECT}), False))
        self.assertIn("run_research", got.permissions)
        # cuenta con dos proyectos, grant con uno: gana el menor de los dos
        await self.repo.db.users.update_one({"user_id": "usr_owner"},
                                            {"$set": {"allowed_projects": [PROJECT, "otro-proyecto"]}})
        capped = await self.subs.principal_for_consumer("usr_owner", [PROJECT])
        self.assertEqual(capped.allowed_projects, frozenset({PROJECT}))
        wild = await self.subs.principal_for_consumer("usr_owner", ["*"])
        self.assertEqual(wild.allowed_projects, frozenset({PROJECT, "otro-proyecto"}))
        await self.repo.db.users.update_one({"user_id": "usr_owner"}, {"$set": {"is_admin": True}})
        admin = await self.subs.principal_for_consumer("usr_owner", [PROJECT])
        self.assertEqual((admin.allowed_projects, admin.is_admin), (frozenset({PROJECT}), False))
        await self.repo.db.users.update_one({"user_id": "usr_owner"}, {"$set": {"is_active": False}})
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.principal_for_consumer("usr_owner", [PROJECT])
        with self.assertRaises(ResearchAccessDenied):
            await self.subs.principal_for_consumer("usr_inexistente", [PROJECT])

    async def test_revoking_a_consumer_pauses_only_its_active_subscriptions(self):
        a, _ = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/a"}),
                                         principal=self.owner, project_cfg=CFG, created_by_consumer="jobhunter")
        b, _ = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/b"}),
                                         principal=self.owner, project_cfg=CFG, created_by_consumer="otro")
        c, _ = await self.subs.subscribe(watch(source={"type": "rss", "url": "https://e.org/c"}),
                                         principal=self.owner, project_cfg=CFG)
        self.assertEqual(await self.subs.pause_for_consumer("jobhunter"), 1)
        statuses = {s["subscription_id"]: (s["status"], s["status_reason"]) for s in await self.subs.list(principal=self.owner)}
        self.assertEqual(statuses[a["subscription_id"]], ("paused", "grant_revoked"))
        self.assertEqual(statuses[b["subscription_id"]][0], "active")
        self.assertEqual(statuses[c["subscription_id"]][0], "active")
        self.assertEqual(await self.subs.pause_for_consumer("jobhunter"), 0)


# =========================================================================
# API HTTP sobre ASGI con sesion real
# =========================================================================


class HttpSubscriptionTests(SubscriptionFixture):
    def app(self, *, subscriptions=True, service_enabled=True):
        service = ResearchService(settings=settings(RESEARCH_ENABLED=service_enabled),
                                  deps=ResearchDependencies(repo=self.repo))
        app = FastAPI()
        app.state.auth_db = _FakeAuthDB(USERS)
        app.include_router(build_research_router(
            get_service=lambda request: service,
            project_cfg_for=lambda request, key: CFG,
            get_subscriptions=(lambda request: self.subs) if subscriptions else None))
        return app

    def body(self, **kw):
        payload = {"project_key": PROJECT, "purpose": "brief", "question": "Que cambio?", "source": SOURCE,
                   "interval_seconds": 3600}
        payload.update(kw)
        return payload

    async def test_authentication_and_permissions(self):
        async with _client(self.app()) as client:
            self.assertEqual((await client.post("/api/research/subscriptions", json=self.body())).status_code, 401)
            self.assertEqual((await client.get("/api/research/subscriptions")).status_code, 401)
            reader = _cookie("usr_reader")
            self.assertEqual((await client.post("/api/research/subscriptions", json=self.body(),
                                                cookies=reader)).status_code, 403)
            self.assertEqual((await client.get("/api/research/subscriptions", cookies=reader)).status_code, 200)
            self.assertEqual((await client.post("/api/research/subscriptions/x/resume", cookies=reader)).status_code, 403)
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_create_returns_201_then_200_and_never_leaks_internals(self):
        owner = _cookie("usr_owner")
        async with _client(self.app()) as client:
            first = await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)
            again = await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)
            keyed = await client.post("/api/research/subscriptions", json=self.body(interval_seconds=7200),
                                      headers={"Idempotency-Key": "lenta"}, cookies=owner)
            conflict = await client.post("/api/research/subscriptions", json=self.body(interval_seconds=7200),
                                         cookies=owner)
        self.assertEqual((first.status_code, again.status_code, keyed.status_code, conflict.status_code),
                         (201, 200, 201, 409))
        payload = first.json()
        self.assertTrue(payload["created"])
        self.assertFalse(again.json()["created"])
        self.assertEqual(first.headers["location"], f"/api/research/subscriptions/{payload['subscription_id']}")
        for hidden in ("lease_owner", "fencing_token", "dedupe_key", "params", "owner_id"):
            self.assertNotIn(hidden, payload)
        self.assertEqual((await self.raw(payload["subscription_id"]))["owner_id"], "usr_owner")

    async def test_owner_comes_from_the_session_not_the_payload(self):
        async with _client(self.app()) as client:
            response = await client.post("/api/research/subscriptions", json=self.body(owner_id="usr_otro"),
                                         cookies=_cookie("usr_owner"))
        self.assertEqual(response.status_code, 422)  # el campo no existe en el contrato
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_error_mapping(self):
        owner = _cookie("usr_owner")
        async with _client(self.app()) as client:
            for body, code in [(self.body(source={"type": "rss", "url": "ftp://x"}), 422),
                               (self.body(source=None), 422), (self.body(interval_seconds=10), 422),
                               (self.body(project_key="otro-proyecto"), 403),
                               (self.body(project_key="../../etc"), 404), (self.body(project_key=""), 422)]:
                response = await client.post("/api/research/subscriptions", json=body, cookies=owner)
                self.assertEqual(response.status_code, code, body)
        self.subs._settings = settings(RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER=1)
        async with _client(self.app()) as client:
            await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)
            limited = await client.post("/api/research/subscriptions", cookies=owner,
                json=self.body(source={"type": "rss", "url": "https://e.org/otro"}))
        self.assertEqual(limited.status_code, 429)

    async def test_foreign_and_unknown_subscriptions_are_404_on_every_route(self):
        async with _client(self.app()) as client:
            created = await client.post("/api/research/subscriptions", json=self.body(), cookies=_cookie("usr_owner"))
            sid = created.json()["subscription_id"]
            other = _cookie("usr_other")
            for method, path, kwargs in [
                ("get", f"/api/research/subscriptions/{sid}", {}),
                ("get", f"/api/research/subscriptions/{sid}/events", {}),
                ("patch", f"/api/research/subscriptions/{sid}", {"json": {"question": "x"}}),
                ("post", f"/api/research/subscriptions/{sid}/pause", {}),
                ("post", f"/api/research/subscriptions/{sid}/resume", {}),
                ("post", f"/api/research/subscriptions/{sid}/cancel", {}),
                ("get", "/api/research/subscriptions/sch_nope", {}),
            ]:
                response = await getattr(client, method)(path, cookies=other, **kwargs)
                self.assertEqual(response.status_code, 404, (method, path))
            listed = await client.get("/api/research/subscriptions", cookies=other)
        self.assertEqual(listed.json(), {"subscriptions": []})
        self.assertEqual((await self.raw(sid))["status"], "active")

    async def test_a_reader_of_the_same_project_cannot_read_another_owners_subscription(self):
        async with _client(self.app()) as client:
            sid = (await client.post("/api/research/subscriptions", json=self.body(),
                                     cookies=_cookie("usr_owner"))).json()["subscription_id"]
            reader = _cookie("usr_reader")  # mismo proyecto, permiso de lectura, otro owner
            for path in (f"/api/research/subscriptions/{sid}", f"/api/research/subscriptions/{sid}/events"):
                self.assertEqual((await client.get(path, cookies=reader)).status_code, 404, path)
            self.assertEqual((await client.get("/api/research/subscriptions", cookies=reader)).json(),
                             {"subscriptions": []})

    async def test_full_lifecycle_over_http(self):
        owner = _cookie("usr_owner")
        async with _client(self.app()) as client:
            sid = (await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)).json()["subscription_id"]
            patched = await client.patch(f"/api/research/subscriptions/{sid}", cookies=owner,
                json={"question": "Nueva", "interval_seconds": 1800, "filter": {"include_terms": ["Datos"]}})
            self.assertEqual((patched.status_code, patched.json()["question"]), (200, "Nueva"))
            self.assertEqual((await client.patch(f"/api/research/subscriptions/{sid}", cookies=owner,
                                                 json={"source": SOURCE})).status_code, 422)
            self.assertEqual((await client.patch(f"/api/research/subscriptions/{sid}", cookies=owner,
                                                 json={"interval_seconds": 60})).status_code, 422)
            paused = await client.post(f"/api/research/subscriptions/{sid}/pause", cookies=owner)
            self.assertEqual(paused.json()["status"], "paused")
            listed = await client.get("/api/research/subscriptions?status=paused&project_key=" + PROJECT, cookies=owner)
            self.assertEqual([s["subscription_id"] for s in listed.json()["subscriptions"]], [sid])
            self.assertEqual((await client.get("/api/research/subscriptions?status=active", cookies=owner)).json(),
                             {"subscriptions": []})
            resumed = await client.post(f"/api/research/subscriptions/{sid}/resume", cookies=owner)
            self.assertEqual(resumed.json()["status"], "active")
            await self.event(sid, "e1")
            events = await client.get(f"/api/research/subscriptions/{sid}/events?status=pending", cookies=owner)
            self.assertEqual([e["event_key"] for e in events.json()["events"]], ["e1"])
            cancelled = await client.post(f"/api/research/subscriptions/{sid}/cancel", cookies=owner)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            for path in ("pause", "resume"):
                self.assertEqual((await client.post(f"/api/research/subscriptions/{sid}/{path}",
                                                    cookies=owner)).status_code, 409)
            self.assertEqual((await client.patch(f"/api/research/subscriptions/{sid}", cookies=owner,
                                                 json={"question": "x"})).status_code, 409)

    async def test_an_owner_who_lost_run_research_can_still_stop_but_not_edit_or_resume(self):
        owner = _cookie("usr_owner")
        async with _client(self.app()) as client:
            sid = (await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)).json()["subscription_id"]
        reduced = [dict(u, all_permissions=["read_research"]) if u["user_id"] == "usr_owner" else u for u in USERS]
        app = self.app()
        app.state.auth_db = _FakeAuthDB(reduced)
        async with _client(app) as client:
            self.assertEqual((await client.post(f"/api/research/subscriptions/{sid}/pause", cookies=owner)).status_code, 200)
            for method, path, kwargs in [("patch", f"/api/research/subscriptions/{sid}", {"json": {"question": "x"}}),
                                         ("post", f"/api/research/subscriptions/{sid}/resume", {})]:
                self.assertEqual((await getattr(client, method)(path, cookies=owner, **kwargs)).status_code, 403)
            self.assertEqual((await client.post(f"/api/research/subscriptions/{sid}/cancel", cookies=owner)).status_code, 200)
        self.assertEqual((await self.raw(sid))["status"], "cancelled")

    async def test_unavailable_when_not_mounted_or_research_is_off(self):
        owner = _cookie("usr_owner")
        async with _client(self.app(subscriptions=False)) as client:
            self.assertEqual((await client.post("/api/research/subscriptions", json=self.body(),
                                                cookies=owner)).status_code, 503)
        async with _client(self.app(service_enabled=False)) as client:
            response = await client.post("/api/research/subscriptions", json=self.body(), cookies=owner)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)


# =========================================================================
# Fachada MCP sobre transporte real
# =========================================================================


class McpSubscriptionTests(SubscriptionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.registry = ConsumerRegistry(ttl_seconds=600)
        self.ops = ("submit", "get", "cancel", "subscribe", "subscriptions", "manage_subscription")
        self.token = self.registry.register(consumer_id="jobhunter", owner_id="usr_owner",
                                            projects=[PROJECT], operations=self.ops)
        self.service = ResearchService(settings=self.cfg, deps=ResearchDependencies(repo=self.repo))
        self.server = build_research_mcp_facade(
            consumers=self.registry, get_service=lambda: self.service, get_subscriptions=lambda: self.subs,
            project_cfg_for=lambda key: CFG)
        self.mcp_app = self.server.http_app(path="/")
        self.parent = FastAPI(lifespan=self.mcp_app.lifespan)
        self.parent.mount("/mcp", self.mcp_app)

    def transport(self, token):
        return StreamableHttpTransport(
            url="http://testserver/mcp/", headers={"Authorization": f"Bearer {token}"} if token else {},
            httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=self.parent), **kw))

    async def call(self, token, tool, **args):
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with Client(self.transport(token)) as client:
                return (await client.call_tool(tool, args)).data

    def args(self, **kw):
        payload = {"project_key": PROJECT, "purpose": "brief", "question": "Que cambio?", "source": SOURCE,
                   "interval_seconds": 3600}
        payload.update(kw)
        return payload

    async def test_catalog_has_the_subscription_tools_and_no_owner_argument(self):
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with Client(self.transport(self.token)) as client:
                tools = await client.list_tools()
        self.assertEqual({t.name for t in tools}, set(FACADE_TOOLS))
        self.assertEqual(len(FACADE_TOOLS), 10)
        for tool in tools:
            props = set(((tool.inputSchema or {}).get("properties") or {}))
            self.assertFalse({"owner_id", "session_id", "consumer_id"} & props, tool.name)

    async def test_default_grants_do_not_include_subscriptions(self):
        plain = self.registry.register(consumer_id="basic", owner_id="usr_owner", projects=[PROJECT])
        data = await self.call(plain, "openceo_research_subscribe", **self.args())
        self.assertEqual((data["ok"], data["error"]["reason"]), (False, "forbidden"))
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_unauthenticated_and_revoked_credentials_do_not_authorize(self):
        for token in (None, "inventado"):
            data = await self.call(token, "openceo_research_subscribe", **self.args())
            self.assertEqual((data["ok"], data["error"]["reason"]), (False, "unauthenticated"))
        self.registry.revoke(self.token, reason="rotada")
        data = await self.call(self.token, "openceo_research_subscription_list")
        self.assertEqual(data["error"]["reason"], "unauthenticated")
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_subscribe_get_list_events_update_and_control(self):
        data = await self.call(self.token, "openceo_research_subscribe", **self.args())
        self.assertTrue(data["ok"] and data["created"], data)
        sid = data["subscription"]["subscription_id"]
        stored = await self.raw(sid)
        self.assertEqual((stored["owner_id"], stored["created_by_consumer"]), ("usr_owner", "jobhunter"))
        again = await self.call(self.token, "openceo_research_subscribe", **self.args())
        self.assertEqual((again["ok"], again["created"]), (True, False))
        conflict = await self.call(self.token, "openceo_research_subscribe", **self.args(interval_seconds=7200))
        self.assertEqual((conflict["ok"], conflict["error"]["reason"], conflict["error"]["status"]),
                         (False, "conflict", 409))
        got = await self.call(self.token, "openceo_research_subscription_get", subscription_id=sid)
        self.assertEqual(got["subscription"]["subscription_id"], sid)
        listed = await self.call(self.token, "openceo_research_subscription_list", project_key=PROJECT)
        self.assertEqual([s["subscription_id"] for s in listed["subscriptions"]], [sid])
        await self.event(sid, "e1")
        events = await self.call(self.token, "openceo_research_subscription_events", subscription_id=sid)
        self.assertEqual([e["event_key"] for e in events["events"]], ["e1"])
        updated = await self.call(self.token, "openceo_research_subscription_update", subscription_id=sid,
                                  question="Nueva", interval_seconds=1800)
        self.assertEqual((updated["subscription"]["question"], updated["subscription"]["cadence"]["interval_seconds"]),
                         ("Nueva", 1800))
        for action, status in (("pause", "paused"), ("resume", "active"), ("cancel", "cancelled")):
            controlled = await self.call(self.token, "openceo_research_subscription_control",
                                         subscription_id=sid, action=action)
            self.assertEqual(controlled["subscription"]["status"], status)
        bad = await self.call(self.token, "openceo_research_subscription_control", subscription_id=sid, action="borrar")
        self.assertEqual(bad["error"]["reason"], "validation_error")
        done = await self.call(self.token, "openceo_research_subscription_control", subscription_id=sid, action="resume")
        self.assertEqual(done["error"]["reason"], "conflict")

    async def test_validation_and_authority_errors_are_typed(self):
        data = await self.call(self.token, "openceo_research_subscribe", **self.args(source={"type": "rss", "url": "ftp://x"}))
        self.assertEqual((data["error"]["reason"], data["error"]["status"]), ("validation_error", 422))
        data = await self.call(self.token, "openceo_research_subscribe", **self.args(kind="otro"))
        self.assertEqual(data["error"]["reason"], "validation_error")
        data = await self.call(self.token, "openceo_research_subscribe", **self.args(project_key="otro-proyecto"))
        self.assertEqual(data["error"]["reason"], "forbidden")  # fuera del grant
        data = await self.call(self.token, "openceo_research_subscribe", **self.args(project_key="../../etc"))
        self.assertEqual(data["error"]["reason"], "forbidden")
        # grant amplio pero la cuenta no tiene el proyecto: no se inventan permisos
        wide = self.registry.register(consumer_id="wide", owner_id="usr_owner", projects=["otro-proyecto"],
                                      operations=self.ops)
        data = await self.call(wide, "openceo_research_subscribe", **self.args(project_key="otro-proyecto"))
        self.assertEqual(data["error"]["reason"], "forbidden")
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_owner_of_the_grant_without_run_permission_cannot_subscribe(self):
        reader = self.registry.register(consumer_id="reader", owner_id="usr_reader", projects=[PROJECT],
                                        operations=self.ops)
        data = await self.call(reader, "openceo_research_subscribe", **self.args())
        self.assertEqual(data["error"]["reason"], "forbidden")
        self.assertEqual(await self.repo.research_schedules.count_documents({}), 0)

    async def test_other_owners_and_projects_outside_the_grant_see_not_found(self):
        sid = (await self.call(self.token, "openceo_research_subscribe", **self.args()))["subscription"]["subscription_id"]
        stranger = self.registry.register(consumer_id="trading", owner_id="usr_other", projects=["otro-proyecto"],
                                          operations=self.ops)
        for tool, extra in (("openceo_research_subscription_get", {}), ("openceo_research_subscription_events", {}),
                            ("openceo_research_subscription_update", {"question": "x"}),
                            ("openceo_research_subscription_control", {"action": "cancel"})):
            data = await self.call(stranger, tool, subscription_id=sid, **extra)
            self.assertEqual(data["error"]["reason"], "not_found", tool)
        self.assertEqual((await self.call(stranger, "openceo_research_subscription_list"))["subscriptions"], [])
        # mismo owner pero grant de otro proyecto: tampoco la ve
        elsewhere = self.registry.register(consumer_id="elsewhere", owner_id="usr_owner", projects=["otro-proyecto"],
                                           operations=self.ops)
        self.assertEqual((await self.call(elsewhere, "openceo_research_subscription_get",
                                          subscription_id=sid))["error"]["reason"], "not_found")
        self.assertEqual((await self.call(elsewhere, "openceo_research_subscription_list"))["subscriptions"], [])
        self.assertEqual((await self.raw(sid))["status"], "active")

    async def test_another_account_in_the_same_project_sees_not_found(self):
        sid = (await self.call(self.token, "openceo_research_subscribe", **self.args()))["subscription"]["subscription_id"]
        peer = self.registry.register(consumer_id="peer", owner_id="usr_reader", projects=[PROJECT],
                                      operations=self.ops)
        for tool, extra in (("openceo_research_subscription_get", {}), ("openceo_research_subscription_events", {}),
                            ("openceo_research_subscription_update", {"question": "x"}),
                            ("openceo_research_subscription_control", {"action": "cancel"})):
            data = await self.call(peer, tool, subscription_id=sid, **extra)
            self.assertEqual(data["error"]["reason"], "not_found", tool)
        self.assertEqual((await self.call(peer, "openceo_research_subscription_list"))["subscriptions"], [])
        self.assertEqual((await self.raw(sid))["status"], "active")

    async def test_read_only_grant_cannot_manage_and_stopping_survives_a_deactivated_account(self):
        sid = (await self.call(self.token, "openceo_research_subscribe", **self.args()))["subscription"]["subscription_id"]
        reader = self.registry.register(consumer_id="dash", owner_id="usr_owner", projects=[PROJECT],
                                        operations=("subscriptions",))
        data = await self.call(reader, "openceo_research_subscription_control", subscription_id=sid, action="cancel")
        self.assertEqual(data["error"]["reason"], "forbidden")
        self.assertTrue((await self.call(reader, "openceo_research_subscription_get", subscription_id=sid))["ok"])
        await self.repo.db.users.update_one({"user_id": "usr_owner"}, {"$set": {"is_active": False}})
        # sin cuenta activa no se crea ni se reanuda... pero siempre se puede detener
        self.assertEqual((await self.call(self.token, "openceo_research_subscribe",
            **self.args(source={"type": "rss", "url": "https://e.org/n"})))["error"]["reason"], "forbidden")
        self.assertEqual((await self.call(self.token, "openceo_research_subscription_control",
                                          subscription_id=sid, action="pause"))["subscription"]["status"], "paused")
        self.assertEqual((await self.call(self.token, "openceo_research_subscription_control",
                                          subscription_id=sid, action="resume"))["error"]["reason"], "forbidden")
        self.assertEqual((await self.call(self.token, "openceo_research_subscription_control",
                                          subscription_id=sid, action="cancel"))["subscription"]["status"], "cancelled")

    async def test_revoking_the_consumer_pauses_its_subscriptions_immediately(self):
        tasks = []
        self.registry.add_revocation_listener(
            lambda consumer_id, reason: tasks.append(asyncio.ensure_future(
                self.subs.pause_for_consumer(consumer_id, "grant_revoked"))))
        sid = (await self.call(self.token, "openceo_research_subscribe", **self.args()))["subscription"]["subscription_id"]
        self.registry.revoke_consumer("jobhunter", reason="rotada")
        await asyncio.gather(*tasks)
        stored = await self.raw(sid)
        self.assertEqual((stored["status"], stored["status_reason"]), ("paused", "grant_revoked"))

    async def test_a_consumer_cannot_stay_logged_in_after_revocation(self):
        sid = (await self.call(self.token, "openceo_research_subscribe", **self.args()))["subscription"]["subscription_id"]
        self.registry.revoke_consumer("jobhunter")
        data = await self.call(self.token, "openceo_research_subscription_control", subscription_id=sid, action="cancel")
        self.assertEqual(data["error"]["reason"], "unauthenticated")
        self.assertEqual((await self.raw(sid))["status"], "active")

    async def test_unmounted_subscriptions_are_unavailable(self):
        server = build_research_mcp_facade(consumers=self.registry, get_service=lambda: self.service,
                                           project_cfg_for=lambda key: CFG)
        app = server.http_app(path="/")
        parent = FastAPI(lifespan=app.lifespan)
        parent.mount("/mcp", app)
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/", headers={"Authorization": f"Bearer {self.token}"},
            httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=parent), **kw))
        async with app.router.lifespan_context(parent):
            async with Client(transport) as client:
                data = (await client.call_tool("openceo_research_subscribe", self.args())).data
        self.assertEqual((data["ok"], data["error"]["status"]), (False, 503))


class ConsumerRegistryRevocationTests(unittest.TestCase):
    def test_listeners_are_told_when_a_consumer_loses_its_last_credential_and_cannot_block_it(self):
        registry = ConsumerRegistry(ttl_seconds=600)
        heard = []
        registry.add_revocation_listener(lambda cid, reason: heard.append((cid, reason)))
        registry.add_revocation_listener(lambda cid, reason: 1 / 0)  # un oyente roto no impide revocar
        first = registry.register(consumer_id="c1", owner_id="o", projects=["p"])
        second = registry.register(consumer_id="c1", owner_id="o", projects=["p"])
        # rotar: revocar la vieja mientras la nueva sigue viva NO detiene al consumidor
        registry.revoke(first, reason="rotada")
        self.assertEqual(heard, [])
        registry.revoke(first, reason="otra vez")  # idempotente
        self.assertEqual(heard, [])
        registry.revoke(second, reason="fin")
        self.assertEqual(heard, [("c1", "fin")])
        registry.revoke(second, reason="de nuevo")
        self.assertEqual(len(heard), 1)  # ya estaba revocada: no se vuelve a avisar
        self.assertEqual(registry.revoke_consumer("c1"), 0)
        self.assertEqual(registry.revoke_consumer("nadie"), 0)
        self.assertEqual(len(heard), 1)

    def test_revoke_consumer_and_expired_sibling_tokens(self):
        clock = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
        registry = ConsumerRegistry(clock=lambda: clock[0], ttl_seconds=10)
        heard = []
        registry.add_revocation_listener(lambda cid, reason: heard.append(cid))
        old = registry.register(consumer_id="c", owner_id="o", projects=["p"], ttl_seconds=10)
        clock[0] = clock[0] + timedelta(seconds=60)  # la primera vencio
        live = registry.register(consumer_id="c", owner_id="o", projects=["p"], ttl_seconds=600)
        registry.revoke(live, reason="fin")
        self.assertEqual(heard, ["c"])  # la vencida no cuenta como credencial viva
        again = registry.register(consumer_id="d", owner_id="o", projects=["p"])
        registry.register(consumer_id="d", owner_id="o", projects=["p"])
        self.assertEqual(registry.revoke_consumer("d"), 2)
        self.assertEqual(heard, ["c", "d"])
        self.assertFalse(registry.revoke(old + "x"))

    def test_default_operations_exclude_subscription_grants_and_unknown_ones_are_refused(self):
        registry = ConsumerRegistry()
        token = registry.register(consumer_id="c", owner_id="o", projects=["p"])
        grant = registry.authenticate(token)
        self.assertEqual(set(grant.operations), {"submit", "get", "cancel"})
        wide = registry.authenticate(registry.register(
            consumer_id="w", owner_id="o", projects=["p"], operations=("subscribe", "subscriptions", "manage_subscription")))
        self.assertTrue(wide.allows("subscribe", project_key="p"))
        with self.assertRaises(ValueError):
            registry.register(consumer_id="x", owner_id="o", projects=["p"], operations=("subscribe_all",))


if __name__ == "__main__":
    unittest.main()
