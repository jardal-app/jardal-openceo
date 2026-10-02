"""Vigilancia incremental: dedup, cursor, crash, cobertura y descubrimiento (TASK-2026-00069).

Los ticks corren contra Mongo real (eventos, schedules, jobs) y el ``ResearchService`` real,
de modo que la idempotencia de admision que se prueba es la de produccion. Los adaptadores
de fuente se sustituyen por un guion; los parsers de RSS/Greenhouse/Lever se prueban aparte
con un lector HTTP falso (los fixtures no prueban conectividad ni calidad real).
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))
import research_support as support
from research.acquisition.base import (
    DiscoveryItem, DiscoveryResult, ProviderDisabledError, ProviderParseError, ProviderRateLimited,
)
from research.acquisition.http_reader import HttpReader, ReadOutcome
from research.contracts import AccessMethod, ResearchEvidence
from research.execution import ResearchExecutionContext
from research.policies import (
    EgressGuard, GuardedFetcher, PinnedEgressTransport, Principal, ResearchAccessDenied,
    ResearchPolicyError, RESEARCH_RUN,
)
from research.scheduler import ResearchScheduleRequest, build_schedule_document
from research.service import ResearchDependencies, ResearchService
from research.watch import (
    GreenhouseSourceAdapter, ItemFilter, LeverSourceAdapter, RssSourceAdapter, SourcePage,
    SourceSpecError, WatchEngine, WatchItem, WatchStaleClaim, WatchTransientError,
    content_fingerprint, event_key_for, is_transient, item_key_for, normalize_source, normalize_url,
)
from settings import Settings

OWNER = "usr_watch_test"
PROJECT = "jardal-openceo"
PURPOSE = "company_interview_brief"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
CFG = {"research": {"enabled": True, "allowed_modes": ["quick"],
        "classification_ceiling": "public", "paid_budget_usd": "0.00",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False}}}
SOURCE = {"type": "rss", "url": "https://example.org/jobs.xml"}


def settings(**kw):
    return Settings(_env_file=None, DEEPSEEK_API_KEY="", RESEARCH_ENABLED=True, **kw)


def principal(**kw):
    return Principal(user_id=OWNER, permissions=frozenset({RESEARCH_RUN}),
                     allowed_projects=frozenset({PROJECT}), **kw)


item = support.watch_item
ScriptedAdapter = support.ScriptedAdapter


# =========================================================================
# Normalizacion, identidad y filtro (sin red ni Mongo)
# =========================================================================


class NormalizationTests(unittest.TestCase):
    def test_url_identity_drops_noise_but_keeps_material_parts(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.ORG:443/Jobs/1?utm_source=x&b=2&a=1&fbclid=z#frag", strip_tracking=True),
            "https://example.org/Jobs/1?a=1&b=2")
        # un parametro con significado distingue items: no se oculta
        self.assertNotEqual(normalize_url("https://e.org/p?id=1", strip_tracking=True),
                            normalize_url("https://e.org/p?id=2", strip_tracking=True))
        self.assertEqual(normalize_url("http://e.org:80"), "http://e.org/")

    def test_url_rejects_non_http_credentials_and_blank(self):
        for bad in ["", "ftp://e.org/x", "javascript:alert(1)", "https://user:pw@e.org/", "https:///x", "x" * 2100]:
            with self.assertRaises(SourceSpecError):
                normalize_url(bad)

    def test_source_normalization_is_strict(self):
        self.assertEqual(normalize_source({"type": "RSS", "url": "https://E.org/feed?b=1&a=2"}),
                         {"type": "rss", "url": "https://e.org/feed?a=2&b=1"})
        self.assertEqual(normalize_source({"type": "greenhouse", "board": "acme"}),
                         {"type": "greenhouse", "board": "acme"})
        for bad in [{"type": "rss"}, {"type": "x", "url": "https://e.org"}, {"type": "lever", "board": "../etc"},
                    {"type": "lever", "board": "a b"}, {"type": "rss", "url": "https://e.org", "extra": 1},
                    {"type": "greenhouse", "board": "acme", "url": "https://evil.example"}, "rss"]:
            with self.assertRaises(SourceSpecError):
                normalize_source(bad)

    def test_item_identity_prefers_declared_id_and_content_ignores_dates(self):
        self.assertEqual(item_key_for("g-1", "https://a.org/x"), item_key_for(" g-1 ", "https://b.org/y"))
        self.assertEqual(item_key_for(None, "https://a.org/x?utm_a=1"), item_key_for(None, "https://a.org/x"))
        self.assertIsNone(item_key_for(None, None))
        self.assertIsNone(item_key_for(None, "not a url"))
        self.assertEqual(content_fingerprint("Titulo  uno", "r"), content_fingerprint("Titulo uno", "r"))
        self.assertNotEqual(content_fingerprint("Titulo uno", "r"), content_fingerprint("Titulo dos", "r"))

    def test_filter_is_accent_and_case_insensitive_with_exclusions(self):
        flt = ItemFilter.from_params({"include_terms": ["Ingeniería", "data"], "exclude_terms": ["Junior"]})
        self.assertTrue(flt.matches("Senior INGENIERIA de software"))
        self.assertTrue(flt.matches(None, "Equipo de Data"))
        self.assertFalse(flt.matches("Ingeniero junior"))
        self.assertFalse(flt.matches("Contador"))
        self.assertTrue(ItemFilter().matches("cualquier cosa"))
        for bad in [{"include_terms": "texto"}, {"include_terms": [""]}, {"include_terms": ["a"] * 21},
                    {"include_terms": ["x" * 81]}, {"otro": []}, ["lista"]]:
            with self.assertRaises(SourceSpecError):
                ItemFilter.from_params(bad)


# =========================================================================
# Adaptadores de fuente con un lector HTTP falso
# =========================================================================

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Empleos</title>
<item><title>Ingeniero de datos</title><link>https://example.org/jobs/1?utm_source=feed</link>
<guid>job-1</guid><description>Equipo de datos</description><pubDate>Mon, 28 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Contador</title><link>https://example.org/jobs/2</link><description>Finanzas</description></item>
<item><title>Sin enlace</title></item></channel></rss>"""


class FakeReader:
    def __init__(self, body: bytes | None = None, *, not_modified=False, content_hash="h1",
                 etag='"v1"', degradations=()):
        self.body, self.not_modified, self.content_hash = body, not_modified, content_hash
        self.etag, self.degradations = etag, degradations
        self.calls: list[dict] = []

    async def read(self, url, **kw):
        self.calls.append({"url": url, **kw})
        evidence = None if self.degradations else ResearchEvidence(
            id="e1", url=url, access_method=AccessMethod.HTTP, content_hash=self.content_hash)
        return ReadOutcome(url=url, final_url=url, access_method=kw.get("access_method") or AccessMethod.HTTP,
                           evidence=evidence, body=self.body or b"", text=(self.body or b"").decode(),
                           not_modified=self.not_modified, content_hash=self.content_hash,
                           etag=self.etag, degradations=tuple(self.degradations))


SCOPE = {"research_id": "watch-x", "project_key": PROJECT, "owner_id": OWNER}


class SourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rss_items_use_declared_id_normalized_url_and_skip_entries_without_link(self):
        reader = FakeReader(RSS.encode())
        page = await RssSourceAdapter(reader).fetch_page(
            SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual([i.title for i in page.items], ["Ingeniero de datos", "Contador"])
        self.assertEqual(page.items[0].item_key, item_key_for("job-1", None))
        self.assertEqual(page.items[1].item_key, item_key_for(None, "https://example.org/jobs/2"))
        self.assertEqual((page.etag, page.content_hash, page.truncated), ('"v1"', "h1", False))
        self.assertEqual(reader.calls[0]["access_method"], AccessMethod.RSS)

    async def test_rss_sends_validators_and_reports_304_and_identical_body(self):
        reader = FakeReader(not_modified=True)
        page = await RssSourceAdapter(reader).fetch_page(
            SOURCE, page_token=None, scope=SCOPE, deadline=None,
            conditional={"etag": '"v1"', "last_modified": "Mon", "content_hash": "h1"})
        self.assertTrue(page.not_modified)
        previous = reader.calls[0]["previous"]
        self.assertEqual((previous.etag, previous.last_modified), ('"v1"', "Mon"))
        # el servidor ignora los validadores pero el cuerpo es identico: tampoco hay cambios
        same = FakeReader(RSS.encode(), content_hash="h1")
        page = await RssSourceAdapter(same).fetch_page(
            SOURCE, page_token=None, scope=SCOPE, deadline=None, conditional={"content_hash": "h1"})
        self.assertTrue(page.not_modified)
        self.assertEqual(page.items, [])

    async def test_rss_without_validators_does_not_build_a_previous_reading(self):
        reader = FakeReader(RSS.encode())
        await RssSourceAdapter(reader).fetch_page(
            SOURCE, page_token=None, scope=SCOPE, deadline=None, conditional={"etag": None})
        self.assertIsNone(reader.calls[0]["previous"])

    async def test_rss_truncation_is_declared_and_bad_xml_degrades(self):
        many = "<rss><channel>" + "".join(
            f"<item><title>T{n}</title><link>https://example.org/{n}</link></item>" for n in range(5)
        ) + "</channel></rss>"
        page = await RssSourceAdapter(FakeReader(many.encode()), max_items=3).fetch_page(
            SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual(len(page.items), 3)
        self.assertTrue(page.truncated)
        bad = await RssSourceAdapter(FakeReader(b"<rss><channel>")).fetch_page(
            SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual(bad.items, [])
        self.assertTrue(bad.degradations)
        doctype = await RssSourceAdapter(FakeReader(b'<!DOCTYPE x [<!ENTITY a "b">]><rss/>')).fetch_page(
            SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertTrue(doctype.degradations)

    async def test_reader_degradation_is_passed_through_without_items(self):
        degraded = ProviderParseError("x", provider="rss").degradation()
        page = await RssSourceAdapter(FakeReader(degradations=[degraded])).fetch_page(
            SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual((page.items, page.degradations), ([], (degraded,)))

    async def test_greenhouse_parses_jobs_and_uses_fixed_host(self):
        body = json.dumps({"jobs": [
            {"id": 11, "title": "Backend", "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
             "location": {"name": "Remoto"}, "updated_at": "2026-09-30T10:00:00-04:00"},
            {"id": 12, "title": "Sin url"}, "basura"], "meta": {"total": 3}}).encode()
        reader = FakeReader(body)
        page = await GreenhouseSourceAdapter(reader).fetch_page(
            {"type": "greenhouse", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual(reader.calls[0]["url"], "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        self.assertEqual([(i.title, i.summary) for i in page.items], [("Backend", "Remoto")])
        self.assertEqual(reader.calls[0]["access_method"], AccessMethod.API)

    async def test_greenhouse_publication_date_is_first_published_never_updated_at(self):
        # Real payloads carry both; `updated_at` moves on every edit of the post.
        body = json.dumps({"jobs": [
            {"id": 11, "title": "Backend", "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
             "first_published": "2026-09-01T09:00:00-04:00", "updated_at": "2026-09-30T10:00:00-04:00"},
            {"id": 12, "title": "Solo editado", "absolute_url": "https://boards.greenhouse.io/acme/jobs/12",
             "updated_at": "2026-09-30T10:00:00-04:00"}]}).encode()
        page = await GreenhouseSourceAdapter(FakeReader(body)).fetch_page(
            {"type": "greenhouse", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        published = {i.title: i.published_at for i in page.items}
        self.assertEqual(published["Backend"].isoformat(), "2026-09-01T09:00:00-04:00")
        self.assertIsNone(published["Solo editado"])

    async def test_greenhouse_content_change_changes_the_fingerprint_not_the_identity(self):
        def parse(title):
            body = json.dumps({"jobs": [{"id": 11, "title": title,
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/11", "updated_at": "2026-09-30T10:00:00Z"}]})
            return GreenhouseSourceAdapter(FakeReader(body.encode()))
        src = {"type": "greenhouse", "board": "acme"}
        first = (await parse("Backend").fetch_page(src, page_token=None, conditional=None, scope=SCOPE, deadline=None)).items[0]
        second = (await parse("Backend Senior").fetch_page(src, page_token=None, conditional=None, scope=SCOPE, deadline=None)).items[0]
        self.assertEqual(first.item_key, second.item_key)
        self.assertNotEqual(first.content_fp, second.content_fp)

    async def test_greenhouse_wrong_shape_degrades(self):
        page = await GreenhouseSourceAdapter(FakeReader(b'{"otra": 1}')).fetch_page(
            {"type": "greenhouse", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertTrue(page.degradations)
        page = await GreenhouseSourceAdapter(FakeReader(b"no json")).fetch_page(
            {"type": "greenhouse", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertTrue(page.degradations)

    async def test_lever_parses_postings_and_always_asks_for_an_explicit_page(self):
        body = json.dumps([
            {"id": "abc", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc",
             "createdAt": 1790000000000,
             "categories": {"location": "La Paz", "team": "Datos", "department": "Tech", "commitment": "Full"}},
            {"id": "x"}, 5]).encode()
        reader = FakeReader(body)
        page = await LeverSourceAdapter(reader).fetch_page(
            {"type": "lever", "board": "acme"}, page_token=None, scope=SCOPE, deadline=None,
            conditional={"etag": '"v1"', "content_hash": "h1"})
        self.assertEqual(reader.calls[0]["url"], "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=0")
        self.assertIsNone(reader.calls[0]["previous"])  # sin validadores: un 304 ocultaria paginas
        self.assertEqual([i.title for i in page.items], ["Data Engineer"])
        self.assertEqual(page.items[0].summary, "La Paz Datos Tech Full")
        self.assertEqual(page.items[0].published_at.tzinfo, timezone.utc)
        self.assertIsNone(page.next_page)  # menos de una pagina llena: no hay mas
        bad = await LeverSourceAdapter(FakeReader(b'{"no": "lista"}')).fetch_page(
            {"type": "lever", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertTrue(bad.degradations)

    async def test_lever_follows_skip_while_the_page_is_full_and_rejects_a_bad_continuation(self):
        rows = [{"id": f"p{n}", "text": f"T{n}", "hostedUrl": f"https://jobs.lever.co/acme/p{n}"} for n in range(100)]
        reader = FakeReader(json.dumps(rows).encode())
        adapter = LeverSourceAdapter(reader)
        src = {"type": "lever", "board": "acme"}
        page = await adapter.fetch_page(src, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual((len(page.items), page.next_page), (100, "100"))
        again = await adapter.fetch_page(src, page_token="100", conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual(again.next_page, "200")
        self.assertTrue(reader.calls[1]["url"].endswith("limit=100&skip=100"))
        for bad in ("abc", "-5", "999999999", "1; DROP"):
            degraded = await adapter.fetch_page(src, page_token=bad, conditional=None, scope=SCOPE, deadline=None)
            self.assertTrue(degraded.degradations, bad)
        self.assertEqual(len(reader.calls), 2)  # una continuacion invalida no llega a la red


# =========================================================================
# Adaptadores sobre el lector real (guard de egress + HttpReader), transporte simulado
# =========================================================================


class _InnerTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    async def handle_async_request(self, request):
        self.requests.append(request)
        status, headers, body = self._handler(request)
        return httpx.Response(status, headers=headers, content=body, request=request)


def real_reader(handler, hosts=None):
    mapping = hosts or {"example.org": ["93.184.216.34"], "boards-api.greenhouse.io": ["93.184.216.35"],
                        "api.lever.co": ["93.184.216.36"]}

    async def resolve(host):
        return mapping.get(host, [])

    inner = _InnerTransport(handler)
    guard = EgressGuard(resolver=resolve)
    fetcher = GuardedFetcher(guard=guard, transport=PinnedEgressTransport(guard, inner=inner))
    return HttpReader(fetcher=fetcher, max_attempts=1), inner


class RealReaderTests(unittest.IsolatedAsyncioTestCase):
    FEED = RSS.encode()

    async def test_rss_conditional_request_sends_validators_and_a_304_means_no_parse(self):
        calls = []

        def handler(request):
            calls.append(dict(request.headers))
            if request.headers.get("if-none-match") == '"v1"':
                return 304, {"etag": '"v1"'}, b""
            return 200, {"content-type": "application/rss+xml", "etag": '"v1"',
                         "last-modified": "Mon, 28 Sep 2026 10:00:00 GMT"}, self.FEED

        reader, inner = real_reader(handler)
        adapter = RssSourceAdapter(reader)
        first = await adapter.fetch_page(SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual((len(first.items), first.etag, first.not_modified), (2, '"v1"', False))
        self.assertNotIn("if-none-match", calls[0])
        second = await adapter.fetch_page(
            SOURCE, page_token=None, scope=SCOPE, deadline=None,
            conditional={"etag": first.etag, "last_modified": first.last_modified, "content_hash": first.content_hash})
        self.assertEqual(calls[1]["if-none-match"], '"v1"')
        self.assertTrue(second.not_modified)
        self.assertEqual((second.items, second.etag), ([], '"v1"'))

    async def test_server_that_ignores_validators_but_serves_the_same_body_is_unchanged(self):
        reader, _ = real_reader(lambda r: (200, {"content-type": "application/rss+xml"}, self.FEED))
        adapter = RssSourceAdapter(reader)
        first = await adapter.fetch_page(SOURCE, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        again = await adapter.fetch_page(SOURCE, page_token=None, scope=SCOPE, deadline=None,
                                         conditional={"content_hash": first.content_hash})
        self.assertTrue(again.not_modified)

    async def test_greenhouse_and_lever_hit_their_fixed_hosts_over_json(self):
        gh = json.dumps({"jobs": [{"id": 1, "title": "Backend", "absolute_url": "https://boards.greenhouse.io/a/jobs/1"}]})
        lever = json.dumps([{"id": "z", "text": "Data", "hostedUrl": "https://jobs.lever.co/a/z"}])

        def handler(request):
            # el transporte conecta a la IP validada y conserva el host en la cabecera Host
            body = gh if request.headers["host"] == "boards-api.greenhouse.io" else lever
            return 200, {"content-type": "application/json"}, body.encode()

        reader, inner = real_reader(handler)
        g = await GreenhouseSourceAdapter(reader).fetch_page(
            {"type": "greenhouse", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        l = await LeverSourceAdapter(reader).fetch_page(
            {"type": "lever", "board": "acme"}, page_token=None, conditional=None, scope=SCOPE, deadline=None)
        self.assertEqual([i.title for i in g.items], ["Backend"])
        self.assertEqual([i.title for i in l.items], ["Data"])
        seen = [(r.headers["host"], r.url.raw_path.decode().rstrip("?")) for r in inner.requests]
        self.assertEqual(seen, [("boards-api.greenhouse.io", "/v1/boards/acme/jobs"),
                                ("api.lever.co", "/v0/postings/acme?mode=json&limit=100&skip=0")])

    async def test_private_destination_is_blocked_and_is_not_a_transient_failure(self):
        reader, inner = real_reader(lambda r: (200, {}, b""), hosts={"example.org": ["10.0.0.5"]})
        page = await RssSourceAdapter(reader).fetch_page(SOURCE, page_token=None, conditional=None,
                                                         scope=SCOPE, deadline=None)
        self.assertEqual(inner.requests, [])  # no se conecto a la red interna
        self.assertEqual([d.reason for d in page.degradations], ["egress_blocked"])

    async def test_rate_limit_and_server_errors_are_classified_as_transient(self):
        for status, headers in ((429, {"retry-after": "120"}), (503, {})):
            reader, _ = real_reader(lambda r, s=status, h=headers: (s, h, b""))
            page = await RssSourceAdapter(reader).fetch_page(SOURCE, page_token=None, conditional=None,
                                                             scope=SCOPE, deadline=None)
            self.assertTrue(page.degradations, status)
            self.assertTrue(all(is_transient(d) for d in page.degradations), (status, page.degradations))
        for status in (403, 404):
            reader, _ = real_reader(lambda r, s=status: (s, {}, b""))
            page = await RssSourceAdapter(reader).fetch_page(SOURCE, page_token=None, conditional=None,
                                                             scope=SCOPE, deadline=None)
            self.assertTrue(page.degradations and not any(is_transient(d) for d in page.degradations), status)


# =========================================================================
# Motor de ticks contra Mongo real
# =========================================================================


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    """Fixture compartido: Mongo aislado, servicio real, reloj controlado y un schedule."""

    DB = "openceo_test_research_engine"
    WORKFLOW = "research.watch_tick"
    PARAMS: dict = {}

    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(support.MONGO_URL)
        await self.client.drop_database(self.DB)
        self.repo = support.build_repo(self.DB)
        self.repo._client = self.client
        await self.repo.ensure_indexes()
        self.cfg = settings(RESEARCH_WATCH_MAX_ITEMS_PER_TICK=5)
        self.service = ResearchService(settings=self.cfg, deps=ResearchDependencies(repo=self.repo))
        self.adapter = ScriptedAdapter()
        self.discover = AsyncMock()
        self.clock = [NOW]
        self.engine = self.make_engine()
        self.wake = Mock()
        doc = build_schedule_document(
            project_key=PROJECT, owner_id=OWNER, purpose=PURPOSE,
            spec=ResearchScheduleRequest(workflow=self.WORKFLOW, interval_seconds=3600, params=self.PARAMS),
            dedupe_key=None, now=NOW - timedelta(hours=1))
        await self.repo.create_research_schedule(doc)
        self.sid = doc["schedule_id"]

    def make_engine(self, cfg=None, **kw):
        return WatchEngine(repo=self.repo, settings=cfg or self.cfg, service=self.service,
                           adapters={"rss": self.adapter}, discover=self.discover,
                           now_fn=lambda: self.clock[0], **kw)

    async def asyncTearDown(self):
        await self.client.drop_database(self.DB)
        self.client.close()

    async def claimed(self):
        await self.repo.patch_research_schedule(self.sid, {
            "run_state": "idle", "lease_owner": None, "lease_expires_at": None,
            "next_run_at": self.clock[0] - timedelta(seconds=1)})
        return await self.repo.claim_next_research_schedule(
            lease_owner="t", lease_seconds=120, now=self.clock[0])

    def execution(self, schedule, *, key=None, cfg=CFG, who=None):
        return ResearchExecutionContext(
            project_key=PROJECT, owner_id=OWNER, purpose=PURPOSE, principal=who or principal(),
            project_cfg=cfg, execution_key=key or f"{self.sid}:{self.clock[0].timestamp()}",
            on_admitted=self.wake, schedule=schedule)

    async def tick(self, params=None, **kw):
        schedule = await self.claimed()
        return await self.engine.watch_tick(self.execution(schedule, **kw), params or self.PARAMS)

    async def events(self, **query):
        return [e async for e in self.repo.research_watch_events.find({"subscription_id": self.sid, **query})]

    async def jobs(self):
        return [j async for j in self.repo.research_jobs.find({})]

    def advance(self, **delta):
        self.clock[0] = self.clock[0] + timedelta(**delta)



@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required for watch engine integration")
class WatchEngineTests(EngineTestCase):
    DB = "openceo_test_research_watch"
    PARAMS = {"source": SOURCE, "question": "Que cambio en las vacantes?",
              "filter": {"include_terms": ["datos"]}}

    # ------------------------------------------------------------ linea base y deteccion

    async def test_first_pass_is_a_baseline_and_admits_nothing(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1"), item("b", "Datos 2")], etag='"e1"', content_hash="h1")]
        result = await self.tick()
        self.assertEqual((result["status"], result["baseline"], result["inserted"]["baseline"]), ("ok", True, 2))
        self.assertIsNone(result["admitted"])
        self.assertEqual(await self.jobs(), [])
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertTrue(cursor["initialized"])
        self.assertEqual((cursor["etag"], cursor["content_hash"], cursor["coverage"]), ('"e1"', "h1", "complete"))
        self.wake.assert_not_called()

    async def test_new_relevant_item_admits_one_job_with_its_url_as_seed(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], etag='"e1"', content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1"), item("b", "Analista de datos")], etag='"e2"', content_hash="h2")]
        result = await self.tick()
        self.assertEqual((result["inserted"]["new"], result["backlog"]), (1, 0))
        jobs = await self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["seed_urls"], ["https://example.org/jobs/b"])
        self.assertEqual((jobs[0]["owner_id"], jobs[0]["project_key"], jobs[0]["mode"]), (OWNER, PROJECT, "quick"))
        self.assertTrue(jobs[0]["idempotency_key"].startswith(f"watch:{self.sid}:"))
        admitted = await self.events(status="admitted")
        self.assertEqual([e["research_id"] for e in admitted], [jobs[0]["research_id"]])
        self.wake.assert_called_once_with(jobs[0]["research_id"])
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)

    async def test_no_relevant_change_means_no_job_and_no_model_work(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        # mismo item + uno nuevo que NO pasa el filtro: se registra para no reevaluarlo
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1"), item("c", "Contador")], content_hash="h2")]
        result = await self.tick()
        self.assertEqual((result["inserted"]["filtered"], result["inserted"]["new"]), (1, 0))
        self.assertIsNone(result["admitted"])
        self.assertEqual(await self.jobs(), [])
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1"), item("c", "Contador")], content_hash="h3")]
        result = await self.tick()
        self.assertEqual(sum(result["inserted"].values()), 0)
        self.assertEqual(await self.jobs(), [])
        self.wake.assert_not_called()

    async def test_material_change_emits_an_updated_event_but_a_date_only_change_does_not(self):
        base = item("a", "Datos 1", summary="Remoto")
        self.adapter.queue = [SourcePage(items=[base], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        # solo cambia la fecha de publicacion: misma huella, mismo evento, sin ruido
        same = WatchItem(**{**base.__dict__, "published_at": NOW})
        self.adapter.queue = [SourcePage(items=[same], content_hash="h2")]
        self.assertEqual(sum((await self.tick())["inserted"].values()), 0)
        self.advance(hours=1)
        edited = item("a", "Datos 1 Senior", summary="Remoto")
        self.adapter.queue = [SourcePage(items=[edited], content_hash="h3")]
        result = await self.tick()
        self.assertEqual(result["inserted"]["updated"], 1)
        self.assertEqual(len(await self.jobs()), 1)
        self.assertEqual(len(await self.events()), 2)

    async def test_backfill_flag_treats_existing_items_as_new(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], content_hash="h1")]
        result = await self.tick({**self.PARAMS, "backfill": True})
        self.assertEqual((result["baseline"], result["inserted"]["new"]), (False, 1))
        self.assertEqual(len(await self.jobs()), 1)

    async def test_duplicates_inside_one_source_page_count_once(self):
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("n", "Datos x"), item("n", "Datos x")], content_hash="h2")]
        result = await self.tick()
        self.assertEqual(result["inserted"]["new"], 1)

    async def test_event_documents_carry_scope_ttl_and_no_third_party_summary(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1", summary="resumen largo de tercero")], content_hash="h1")]
        await self.tick()
        [event] = await self.events()
        self.assertEqual((event["owner_id"], event["project_key"]), (OWNER, PROJECT))
        self.assertNotIn("summary", event)
        self.assertGreater(event["expires_at"].replace(tzinfo=timezone.utc), NOW + timedelta(days=89))
        indexes = await self.repo.research_watch_events.index_information()
        self.assertEqual(indexes["idx_research_watch_events_ttl"]["expireAfterSeconds"], 0)

    async def test_ttl_slides_for_items_that_stay_listed(self):
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], content_hash="h1")]
        await self.tick()
        self.advance(days=60)  # queda < mitad de la retencion (90 d)
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], content_hash="h2")]
        await self.tick()
        [event] = await self.events()
        self.assertGreater(event["expires_at"].replace(tzinfo=timezone.utc), NOW + timedelta(days=149))

    # ------------------------------------------------------------ crash y replay

    async def test_crash_before_cursor_commit_loses_nothing_and_admits_once(self):
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        original = self.engine._commit_cursor
        self.engine._commit_cursor = AsyncMock(side_effect=RuntimeError("crash tras insertar eventos"))
        with self.assertRaises(RuntimeError):
            await self.tick()
        self.assertEqual(len(await self.events(status="pending")), 1)
        self.assertEqual(await self.jobs(), [])
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual(cursor["content_hash"], "h1")  # el cursor NO avanzo
        self.engine._commit_cursor = original
        self.advance(minutes=5)
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        result = await self.tick()
        self.assertEqual(sum(result["inserted"].values()), 0)  # ya estaba persistido
        self.assertEqual(result["admitted"]["events"], 1)
        self.assertEqual(len(await self.jobs()), 1)
        self.assertEqual(len(await self.events()), 2)

    async def test_crash_after_admission_replays_to_the_same_job(self):
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        real_mark = self.repo.mark_research_watch_events
        self.repo.mark_research_watch_events = AsyncMock(side_effect=RuntimeError("crash tras admitir"))
        with self.assertRaises(RuntimeError):
            await self.tick()
        self.repo.mark_research_watch_events = real_mark
        self.assertEqual(len(await self.jobs()), 1)
        self.assertEqual(len(await self.events(status="pending")), 1)  # sigue pending
        first_job = (await self.jobs())[0]["research_id"]
        self.advance(minutes=5)
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        result = await self.tick()
        self.assertEqual((result["admitted"]["research_id"], result["admitted"]["created"]), (first_job, False))
        self.assertEqual(len(await self.jobs()), 1)
        self.assertEqual(len(await self.events(status="admitted")), 1)

    async def test_stale_claim_stops_before_cursor_and_admission(self):
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        schedule = await self.claimed()
        await self.repo.patch_research_schedule(self.sid, {"fencing_token": schedule["fencing_token"] + 5})
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        result = await self.engine.watch_tick(self.execution(schedule), self.PARAMS)
        self.assertEqual(result["skipped"], "stale_claim")
        self.assertEqual(await self.jobs(), [])
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual(cursor["content_hash"], "h1")

    # ------------------------------------------------------------ cobertura y paginas

    async def test_page_limit_keeps_a_continuation_and_resumes_without_skipping(self):
        cfg = settings(RESEARCH_WATCH_MAX_PAGES_PER_TICK=2, RESEARCH_WATCH_MAX_ITEMS_PER_TICK=10)
        self.engine = self.make_engine(cfg)
        self.adapter.queue = [SourcePage(items=[item("seed")], content_hash="h0")]
        await self.tick()  # linea base
        self.advance(hours=1)
        pages = {None: SourcePage(items=[item("p1", "Datos 1")], next_page="t2", content_hash="h1"),
                 "t2": SourcePage(items=[item("p2", "Datos 2")], next_page="t3"),
                 "t3": SourcePage(items=[item("p3", "Datos 3")], next_page="t4"),
                 "t4": SourcePage(items=[item("p4", "Datos 4")])}
        self.adapter.pages = pages
        first = await self.tick()
        self.assertEqual((first["coverage"], first["coverage_reason"]), ("incomplete", "page_limit"))
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual(cursor["continuation"], {"page_token": "t3"})
        self.assertEqual(cursor["content_hash"], "h0")  # validadores sin avanzar
        self.assertEqual([c["page_token"] for c in self.adapter.calls[-2:]], [None, "t2"])
        self.advance(hours=1)
        second = await self.tick()
        self.assertEqual([c["page_token"] for c in self.adapter.calls[-2:]], ["t3", "t4"])
        self.assertEqual((second["coverage"], second["coverage_reason"]), ("complete", None))
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertIsNone(cursor["continuation"])
        urls = sorted(e["url"] for e in await self.events() if e["status"] in {"pending", "admitted"})
        self.assertEqual(urls, [f"https://example.org/jobs/p{n}" for n in (1, 2, 3, 4)])

    async def test_item_cap_declares_incomplete_and_the_next_tick_finishes_the_rest(self):
        cfg = settings(RESEARCH_WATCH_MAX_ITEMS_PER_SOURCE=2, RESEARCH_WATCH_MAX_ITEMS_PER_TICK=10)
        self.engine = self.make_engine(cfg)
        self.adapter.queue = [SourcePage(items=[item("seed")], content_hash="h0")]
        await self.tick()
        self.advance(hours=1)
        everything = [item(f"n{n}", f"Datos {n}") for n in range(5)]
        self.adapter.queue = [SourcePage(items=everything, content_hash="h1")]
        first = await self.tick()
        self.assertEqual((first["coverage_reason"], first["inserted"]["new"]), ("item_cap", 2))
        self.assertEqual((await self.repo.get_research_schedule(self.sid))["cursor"]["content_hash"], "h0")
        for hours in (1, 1):
            self.advance(hours=hours)
            self.adapter.queue = [SourcePage(items=everything, content_hash="h1")]
            last = await self.tick()
        self.assertEqual(last["coverage"], "complete")
        self.assertEqual(len([e for e in await self.events() if e["item_key"] != item("seed").item_key]), 5)

    async def test_adapter_truncation_is_declared_but_does_not_stall_the_subscription(self):
        # Recortar en el adaptador es determinista: retener el cursor dejaria la suscripcion en
        # linea base para siempre y no admitiria nunca nada. Se avanza y se declara.
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1")], etag='"e1"', content_hash="h1", truncated=True)]
        first = await self.tick()
        self.assertEqual((first["coverage"], first["coverage_reason"]), ("incomplete", "source_truncated"))
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual((cursor["initialized"], cursor["etag"], cursor["content_hash"]), (True, '"e1"', "h1"))
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a", "Datos 1"), item("b", "Datos nuevos")],
                                         content_hash="h2", truncated=True)]
        second = await self.tick()
        self.assertEqual((second["baseline"], second["inserted"]["new"]), (False, 1))
        self.assertEqual(len(await self.jobs()), 1)  # un item nuevo si llega a admitirse

    async def test_pending_events_without_a_url_are_dropped_not_investigated_blindly(self):
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        await self.repo.insert_research_watch_events([{
            "subscription_id": self.sid, "owner_id": OWNER, "project_key": PROJECT, "event_key": "sin-url",
            "item_key": "sin-url", "content_fp": "f", "kind": "new", "status": "pending", "title": "x",
            "url": None, "first_seen_at": NOW}])
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        result = await self.tick()
        self.assertIsNone(result["admitted"])
        self.assertEqual(await self.jobs(), [])
        self.assertEqual(len(await self.events(status="dropped")), 1)

    async def test_backlog_beyond_one_batch_is_admitted_on_later_ticks_oldest_first(self):
        cfg = settings(RESEARCH_WATCH_MAX_ITEMS_PER_TICK=2)
        self.engine = self.make_engine(cfg)
        self.adapter.queue = [SourcePage(items=[item("seed")], content_hash="h0")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item(f"n{n}", f"Datos {n}") for n in range(5)], content_hash="h1")]
        first = await self.tick()
        self.assertEqual((first["admitted"]["events"], first["backlog"]), (2, 3))
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[], not_modified=True, content_hash="h1")]
        second = await self.tick()
        self.assertEqual((second["admitted"]["events"], second["backlog"]), (2, 1))
        self.assertEqual(len(await self.jobs()), 2)

    # ------------------------------------------------------------ fallos de fuente

    async def test_not_modified_keeps_validators_and_sends_them_next_time(self):
        self.adapter.queue = [SourcePage(items=[item("a")], etag='"e1"', last_modified="Mon", content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(not_modified=True, etag='"e1"', last_modified="Mon", content_hash="h1")]
        result = await self.tick()
        self.assertTrue(result["not_modified"])
        self.assertEqual(self.adapter.calls[-1]["conditional"],
                         {"etag": '"e1"', "last_modified": "Mon", "content_hash": "h1"})
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual((cursor["etag"], cursor["content_hash"]), ('"e1"', "h1"))
        self.assertEqual(await self.jobs(), [])

    async def test_permanent_source_failure_is_recorded_and_does_not_advance(self):
        self.adapter.queue = [SourcePage(items=[item("a")], etag='"e1"', content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        blocked = ProviderDisabledError("bloqueado", provider="rss").degradation()
        self.adapter.queue = [SourcePage(degradations=(blocked,))]
        result = await self.tick()
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["coverage_gaps"])
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual((cursor["consecutive_failures"], cursor["etag"], cursor["content_hash"]), (1, '"e1"', "h1"))
        self.assertEqual(await self.jobs(), [])

    async def test_transient_source_failure_raises_for_scheduler_backoff(self):
        limited = ProviderRateLimited("429", provider="rss").degradation()
        self.adapter.queue = [SourcePage(degradations=(limited,))]
        with self.assertRaises(WatchTransientError):
            await self.tick()
        self.assertEqual((await self.repo.get_research_schedule(self.sid))["cursor"]["consecutive_failures"], 1)

    async def test_missing_adapter_and_invalid_spec_are_reported_not_raised(self):
        engine = WatchEngine(repo=self.repo, settings=self.cfg, service=self.service, adapters={},
                             now_fn=lambda: self.clock[0])
        schedule = await self.claimed()
        result = await engine.watch_tick(self.execution(schedule), self.PARAMS)
        self.assertEqual(result["skipped"], "adapter_unavailable")
        for params in [{**self.PARAMS, "source": {"type": "rss", "url": "ftp://x"}},
                       {**self.PARAMS, "question": " "}, {**self.PARAMS, "filter": {"include_terms": "x"}}]:
            result = await self.engine.watch_tick(self.execution(schedule), params)
            self.assertEqual(result["status"], "invalid_spec")

    # ------------------------------------------------------------ autoridad

    async def test_revoked_authority_raises_before_touching_the_source(self):
        schedule = await self.claimed()
        cases = [(principal().__class__(user_id=OWNER, permissions=frozenset(), allowed_projects=frozenset({PROJECT})),
                  CFG, ResearchAccessDenied),
                 (principal().__class__(user_id=OWNER, permissions=frozenset({RESEARCH_RUN}), allowed_projects=frozenset()),
                  CFG, ResearchAccessDenied),
                 (principal(), {"research": {"enabled": False}}, ResearchPolicyError)]
        for who, cfg, error in cases:
            with self.assertRaises(error):
                await self.engine.watch_tick(self.execution(schedule, who=who, cfg=cfg), self.PARAMS)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(await self.jobs(), [])

    async def test_tick_never_calls_the_model_runtime_or_business_task_apis(self):
        runtime = Mock()
        self.service._deps.runtime = runtime
        self.repo.create_task = AsyncMock(side_effect=AssertionError("business task created"))
        self.repo.patch_task = AsyncMock(side_effect=AssertionError("business task patched"))
        self.adapter.queue = [SourcePage(items=[item("a")], content_hash="h1")]
        await self.tick()
        self.advance(hours=1)
        self.adapter.queue = [SourcePage(items=[item("a"), item("b", "Datos nuevos")], content_hash="h2")]
        await self.tick()
        self.assertEqual(runtime.method_calls, [])  # la admision no ejecuta al modelo
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)


# =========================================================================
# Descubrimiento con cupo propio
# =========================================================================


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required for discovery integration")
class DiscoveryTickTests(EngineTestCase):
    DB = "openceo_test_research_discovery"
    WORKFLOW = "research.discovery_tick"
    PARAMS = {"question": "Nuevas fuentes sobre Empresa Ejemplo", "filter": {"exclude_terms": ["spam"]}}

    def results(self, *urls, degradations=()):
        return DiscoveryResult(provider="brave", items=tuple(
            DiscoveryItem(url=u, provider="brave", rank=n) for n, u in enumerate(urls, 1)),
            degradations=tuple(degradations))

    async def dtick(self, params=None, **kw):
        schedule = await self.claimed()
        return await self.engine.discovery_tick(self.execution(schedule, **kw), params or self.PARAMS)

    async def test_first_run_is_a_baseline_and_stores_only_hashes(self):
        self.discover.return_value = self.results("https://a.example/1?utm_source=x", "https://b.example/2")
        result = await self.dtick()
        self.assertEqual((result["baseline"], result["recorded"], result["admitted"]), (True, 2, None))
        self.assertEqual(await self.jobs(), [])
        events = await self.events()
        self.assertTrue(all(e["url"] is None and e["status"] == "baseline" for e in events))
        self.assertTrue(all("a.example" not in json.dumps(e, default=str) for e in events))
        kwargs = self.discover.await_args.kwargs
        self.assertEqual((kwargs["project_key"], kwargs["question"]), (PROJECT, self.PARAMS["question"]))
        self.assertTrue(kwargs["research_id"].startswith("disc-"))

    async def test_new_url_admits_a_job_and_known_urls_do_not_repeat(self):
        self.discover.return_value = self.results("https://a.example/1")
        await self.dtick()
        self.advance(days=1)
        self.discover.return_value = self.results("https://a.example/1?utm_campaign=z", "https://c.example/3")
        result = await self.dtick()
        self.assertEqual((result["admitted"]["events"], result["overflow"]), (1, 0))
        [job] = await self.jobs()
        self.assertEqual(job["seed_urls"], ["https://c.example/3"])
        self.assertTrue(job["idempotency_key"].startswith(f"disc:{self.sid}:"))
        self.advance(days=1)
        self.discover.return_value = self.results("https://a.example/1", "https://c.example/3")
        again = await self.dtick()
        self.assertIsNone(again["admitted"])
        self.assertEqual(len(await self.jobs()), 1)

    async def test_include_terms_never_filter_url_only_results(self):
        # Un resultado del buscador solo trae la URL: comparar include_terms contra ella
        # descartaria fuentes pertinentes. Solo exclude_terms se aplica.
        params = {**self.PARAMS, "filter": {"include_terms": ["ingeniero"], "exclude_terms": ["spam"]}}
        self.discover.return_value = self.results("https://a.example/careers/1")
        await self.dtick(params)
        self.advance(days=1)
        self.discover.return_value = self.results("https://a.example/careers/1", "https://b.example/careers/2",
                                                  "https://spam.example/x")
        result = await self.dtick(params)
        self.assertEqual(result["admitted"]["events"], 1)
        [job] = await self.jobs()
        self.assertEqual(job["seed_urls"], ["https://b.example/careers/2"])
        self.assertEqual(len(await self.events(status="filtered")), 1)  # el excluido

    async def test_daily_cap_is_independent_and_blocks_before_calling_the_search(self):
        self.discover.return_value = self.results("https://a.example/1")
        await self.dtick()
        await self.dtick()
        capped = await self.dtick()  # cupo por defecto: 2 al dia
        self.assertEqual((capped["skipped"], capped["searches_today"], capped["cap"]), ("daily_search_cap", 2, 2))
        self.assertEqual(self.discover.await_count, 2)
        self.advance(days=1)
        await self.dtick()
        self.assertEqual(self.discover.await_count, 3)

    async def test_per_subscription_cap_can_only_lower_the_server_cap(self):
        self.discover.return_value = self.results("https://a.example/1")
        params = {**self.PARAMS, "max_searches_per_day": 1}
        await self.dtick(params)
        self.assertEqual((await self.dtick(params))["skipped"], "daily_search_cap")
        # pedir 99 no sube el techo del servidor
        self.advance(days=1)
        high = {**self.PARAMS, "max_searches_per_day": 99}
        await self.dtick(high)
        await self.dtick(high)
        self.assertEqual((await self.dtick(high))["skipped"], "daily_search_cap")

    async def test_absent_params_are_missing_data_not_zero_or_invalid(self):
        # el templating YAML convierte un parametro ausente en "": no es 0 ni rompe el tick
        self.discover.return_value = self.results("https://a.example/1")
        params = {**self.PARAMS, "max_searches_per_day": "", "count": "", "backfill": ""}
        result = await self.dtick(params)
        self.assertEqual((result["status"], result["cap"], result["baseline"]), ("ok", 2, True))
        self.assertEqual(self.discover.await_args.kwargs["count"], 10)

    async def test_the_search_slot_is_consumed_before_the_call_so_a_crash_cannot_repeat_it(self):
        self.discover.side_effect = RuntimeError("crash durante la busqueda")
        with self.assertRaises(RuntimeError):
            await self.dtick()
        cursor = (await self.repo.get_research_schedule(self.sid))["cursor"]
        self.assertEqual(cursor["search_window"]["count"], 1)

    async def test_disabled_adapter_or_missing_search_skips_without_spending(self):
        cfg = {"research": {**CFG["research"], "adapters": {"brave": False}}}
        result = await self.dtick(cfg=cfg)
        self.assertEqual(result["skipped"], "adapter_disabled")
        engine = WatchEngine(repo=self.repo, settings=self.cfg, service=self.service, adapters={},
                             discover=None, now_fn=lambda: self.clock[0])
        schedule = await self.claimed()
        result = await engine.discovery_tick(self.execution(schedule), self.PARAMS)
        self.assertEqual(result["skipped"], "discovery_unavailable")
        self.discover.assert_not_awaited()
        self.assertEqual((await self.repo.get_research_schedule(self.sid))["cursor"], {})

    async def test_quota_or_budget_degradation_is_declared_not_retried_or_replaced(self):
        limited = ProviderRateLimited("429", provider="brave").degradation()
        self.discover.return_value = self.results(degradations=[limited])
        result = await self.dtick()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(await self.jobs(), [])
        self.assertEqual(self.discover.await_count, 1)

    async def test_revoked_authority_raises_before_searching(self):
        schedule = await self.claimed()
        with self.assertRaises(ResearchAccessDenied):
            await self.engine.discovery_tick(
                self.execution(schedule, who=Principal(user_id=OWNER)), self.PARAMS)
        self.discover.assert_not_awaited()

    async def test_crash_after_admission_before_recording_repeats_to_the_same_job(self):
        self.discover.return_value = self.results("https://a.example/1")
        await self.dtick()
        self.advance(days=1)
        self.discover.return_value = self.results("https://a.example/1", "https://c.example/3")
        real = self.repo.insert_research_watch_events
        self.repo.insert_research_watch_events = AsyncMock(side_effect=RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            await self.dtick()
        self.repo.insert_research_watch_events = real
        self.assertEqual(len(await self.jobs()), 1)
        self.advance(days=1)
        await self.dtick()
        self.assertEqual(len(await self.jobs()), 1)  # mismos resultados => misma clave => mismo job
        self.assertEqual(len([e for e in await self.events() if e["status"] == "admitted"]), 1)

    async def test_overflow_beyond_one_batch_is_not_recorded_so_it_can_surface_again(self):
        cfg = settings(RESEARCH_WATCH_MAX_ITEMS_PER_TICK=2, RESEARCH_DISCOVERY_DAILY_SEARCH_CAP=5)
        self.engine = self.make_engine(cfg)
        self.discover.return_value = self.results("https://a.example/0")
        await self.dtick()
        self.advance(days=1)
        self.discover.return_value = self.results(*[f"https://n{n}.example/" for n in range(5)])
        result = await self.dtick()
        self.assertEqual((result["admitted"]["events"], result["overflow"]), (2, 3))
        self.assertEqual(len(await self.events(status="admitted")), 2)
        self.assertEqual(len(await self.events()), 3)  # 1 baseline + 2 admitidos


if __name__ == "__main__":
    unittest.main()
