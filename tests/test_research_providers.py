"""Mounted provider policy and native credit limits, with real shared Mongo."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import research_support as support
from research.acquisition.base import BudgetRefusedError, DiscoveryResult
from research.acquisition.firecrawl import FirecrawlClient
from research.provider_credits import ProviderCredits
from research.wiring import _build_discovery, build_research_service
from settings import Settings


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_providers")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_providers")

    async def asyncTearDown(self):
        await self.repo.db.client.drop_database("openceo_test_providers")
        await self.repo.close()

    async def test_two_instances_share_credit_ceiling_and_uncertain_debit(self):
        args = dict(provider="firecrawl", account="test", capacity=1, window="explicit")
        one, two = (ProviderCredits(self.repo.db, **args) for _ in range(2))
        results = await asyncio.gather(one.reserve(1), two.reserve(1), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, str) for r in results), 1)
        self.assertEqual(sum(isinstance(r, BudgetRefusedError) for r in results), 1)
        with self.assertRaises(BudgetRefusedError):
            await one.reserve(1)
        usage = await self.repo.db.research_provider_credit_usage.find_one({})
        self.assertIsNone(usage["actual_credits"])
        self.assertEqual(usage["state"], "uncertain")

    async def test_expired_measured_window_cannot_authorize_new_credit(self):
        quota = ProviderCredits(self.repo.db, provider="firecrawl", account="test", capacity=1000,
                                window="2026-01-01T00:00:00Z")
        with self.assertRaises(BudgetRefusedError):
            await quota.reserve(1)
        self.assertEqual(await quota.accounts.count_documents({}), 0)

    async def test_firecrawl_reserves_native_credit_before_one_v2_request(self):
        quota = ProviderCredits(self.repo.db, provider="firecrawl", account="test", capacity=1, window="explicit")
        requests = []
        async def handle(request):
            self.assertEqual((await quota.accounts.find_one({}))["reserved_credits"], 1)
            requests.append(str(request.url))
            return httpx.Response(200, json={"success": True, "creditsUsed": 1,
                "data": {"markdown": "Example Domain", "metadata": {"sourceURL": "https://example.org"}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            fire = FirecrawlClient(settings=Settings(_env_file=None, RESEARCH_FIRECRAWL_API_KEY="SECRET_CANARY"),
                http_client=client, credit_budget=quota, max_attempts=1)
            outcome = await fire.scrape("https://example.org", research_id="test", project_key="jardal-openceo")
            self.assertEqual(outcome.text, "Example Domain")
            with self.assertRaises(BudgetRefusedError):
                await fire.scrape("https://example.org", research_id="test", project_key="jardal-openceo")
        self.assertEqual(requests, ["https://api.firecrawl.dev/v2/scrape"])
        self.assertNotIn("SECRET_CANARY", str(await quota.usage.find_one({})))

    async def test_firecrawl_own_client_reads_stream_and_keeps_bounded_body(self):
        import gzip
        import json
        quota = ProviderCredits(self.repo.db, provider="firecrawl", account="test", capacity=1, window="explicit")
        body = json.dumps({"success": True, "creditsUsed": 1,
            "data": {"markdown": "Streamed content", "metadata": {"sourceURL": "https://example.org"}}}).encode()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-encoding": "gzip"},
                                                                        content=gzip.compress(body)))
        original = httpx.AsyncClient
        with patch("research.acquisition.firecrawl.httpx.AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            fire = FirecrawlClient(settings=Settings(_env_file=None, RESEARCH_FIRECRAWL_API_KEY="synthetic"),
                                   credit_budget=quota, max_attempts=1)
            outcome = await fire.scrape("https://example.org", research_id="test", project_key="jardal-openceo")
        self.assertEqual(outcome.text, "Streamed content")
        self.assertEqual((await quota.usage.find_one({}))["actual_credits"], 1)

    async def test_http_reader_degradation_keeps_cause_and_is_not_blank_evidence(self):
        from research.acquisition.base import DestinationBlockedError, AcquisitionError
        from research.wiring import _reader_callable
        reader = SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace(
            degradations=(DestinationBlockedError("Demasiadas redirecciones", provider="http").degradation(),))))
        with self.assertRaises(AcquisitionError) as error:
            await _reader_callable(reader)(url="https://example.org", research_id="test", project_key="jardal-openceo")
        self.assertEqual(error.exception.kind, "egress_blocked")

    async def test_discovery_selects_explicit_provider_and_never_falls_back(self):
        job = {"project_key": "jardal-openceo", "classification": "public", "adapters": {"serper": True}}
        repo = SimpleNamespace(get_research_job=AsyncMock(return_value=job))
        serper = SimpleNamespace(search_web=AsyncMock(return_value=DiscoveryResult(provider="serper")))
        brave = SimpleNamespace(search_web=AsyncMock(side_effect=AssertionError("unrequested fallback")))
        settings = Settings(_env_file=None, RESEARCH_BRAVE_API_KEY="brave", RESEARCH_SERPER_API_KEY="serper")
        with patch("research.acquisition.serper.SerperClient", return_value=serper), patch("research.acquisition.brave.BraveClient", return_value=brave):
            discover = _build_discovery(settings, None, repo=repo)
        result = await discover(question="test", research_id="test", project_key="jardal-openceo")
        self.assertEqual(result.provider, "serper")
        job["adapters"] = {}
        result = await discover(question="test", research_id="test", project_key="jardal-openceo")
        self.assertTrue(result.degradations)
        job["adapters"], job["classification"] = {"serper": True}, "restricted"
        await discover(question="private", research_id="test", project_key="jardal-openceo")
        self.assertEqual(serper.search_web.await_count, 1)
        brave.search_web.assert_not_awaited()

    async def test_wired_firecrawl_blocks_private_destinations_before_credit_use(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, RESEARCH_EVIDENCE_DIR=root, RESEARCH_FIRECRAWL_API_KEY="synthetic",
                RESEARCH_FIRECRAWL_ACCOUNT_CREDITS="1", RESEARCH_FIRECRAWL_CREDIT_WINDOW="test")
            service, resources = build_research_service(settings, self.repo)
            job = {**support.job_doc(), "read_provider": "firecrawl", "adapters": {"firecrawl": True}}
            await self.repo.create_research_job(job)
            try:
                with self.assertRaisesRegex(Exception, "Destino no publico"):
                    await service._deps.read(url="http://127.0.0.1/secret", research_id=job["research_id"], project_key="jardal-openceo")
                self.assertEqual(await self.repo.db.research_provider_credits.count_documents({}), 0)
            finally:
                await resources.aclose()
