"""Controlled phases, adversarial verifier outputs, shared slots and real ledger."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from pathlib import Path

import httpx
import research_support as support
from research.deep import DeepResearch, claim_digest
from research.deep_model import BudgetedDeepModel, DeepModelError, PhaseOutcome
from research.service import ResearchService, ResearchDependencies, JobSlots, StepBudget
from research.budget import ResearchBudgetLedger


class Model:
    controlled_calls = True
    def __init__(self, *, wrong=False, invent=False):
        self.calls = []
        self.wrong, self.invent = wrong, invent
    async def run_phase(self, *, job, phase, prompt, seconds):
        p = json.loads(prompt)
        self.calls.append(p)
        if phase.startswith("researcher"):
            eid = p["evidence"][0]["id"]
            payload = {"summary": "Result", "claims": [{"id": "c1", "statement": "Revenue is 10",
                        "kind": "fact", "evidence_ids": [eid], "support": [{"extract": "Revenue is 10"}]}]}
        elif phase == "correction":
            payload = p["draft"]
            if self.invent:
                payload["claims"][0]["statement"] = "Invented 900"
                payload["claims"][0]["support"] = [{"extract": "Invented 900"}]
        else:
            payload = {"verdicts": [{"claim_digest": c["claim_digest"],
                "verdict": "contradicted" if self.wrong else "supported", "evidence_id": c["evidence_ids"][0],
                "extract": "Revenue is 10", "reason": "Checked source", "observed_at": "unknown"} for c in p["claims"]]}
        return PhaseOutcome(json.dumps(payload), estimated_micro_usd=1, usage={"test": True})
    async def cancel(self, research_id):
        pass


class DeepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = support.job_doc(support.request(mode="deep"), owner_id="alice")
        self.service = ResearchService(settings=SimpleNamespace(), deps=ResearchDependencies(repo=None))
        self.evidence = {self.job["research_id"] + "-e1": {"text": "Revenue is 10", "url": "https://example.org/source"}}
    async def run_deep(self, model, steps=30):
        gaps = []
        result = await DeepResearch(self.service, model).synthesize(self.job, StepBudget(steps=steps, seconds=10), self.evidence, gaps)
        return result, gaps
    async def test_fresh_verifier_context_and_support(self):
        m = Model()
        result, gaps = await self.run_deep(m)
        self.assertEqual(result.claims[0].verification.value, "supported")
        self.assertEqual(gaps, [])
        self.assertEqual(self.job["deep_execution"]["phases"], ["researcher-1", "verifier"])
        self.assertNotIn("draft", m.calls[1])
        self.assertNotIn("summary", m.calls[1])
        self.assertEqual(self.service.slot_snapshot()["calls_in_use"], 0)
    async def test_false_claim_and_persistent_contradiction(self):
        result, gaps = await self.run_deep(Model(wrong=True))
        self.assertTrue(gaps)
        self.assertEqual(result.claims[0].verification.value, "contradicted")
        self.assertEqual(self.job["deep_execution"]["phases"].count("correction"), 1)
        self.assertTrue(any(x["verdict"] == "contradicted" for x in self.job["deep_execution"]["verification_history"]))
    async def test_correction_invents_fact_but_claim_is_not_supported(self):
        result, gaps = await self.run_deep(Model(wrong=True, invent=True))
        self.assertTrue(gaps)
        self.assertNotEqual(result.claims[0].verification.value, "supported")
    async def test_duplicate_sources_are_not_researcher_votes(self):
        self.evidence["copy"] = dict(self.evidence[next(iter(self.evidence))])
        m = Model()
        await self.run_deep(m)
        self.assertEqual(len(m.calls[0]["evidence"]), 1)
    async def test_limit_and_cli_rejected_before_calls(self):
        m = Model()
        result, gaps = await self.run_deep(m, steps=1)
        self.assertTrue(gaps)
        self.assertEqual(len(m.calls), 1)
        result, gaps = await self.run_deep(SimpleNamespace(controlled_calls=False))
        self.assertIsNone(result)
        self.assertTrue(gaps)
    async def test_three_jobs_share_two_call_slots_without_deadlock(self):
        slots = self.service._deps.slots
        peak = 0
        class Slow(Model):
            async def run_phase(other, **kw):
                nonlocal peak
                peak = max(peak, slots.calls_in_use)
                await asyncio.sleep(.01)
                return await super().run_phase(**kw)
        async def distinct_job(number):
            from copy import deepcopy
            job = deepcopy(self.job)
            job["research_id"] = job["research_id"] + f"-{number}"
            evidence = {job["research_id"] + "-e1": dict(next(iter(self.evidence.values())))}
            result = await DeepResearch(self.service, Slow()).synthesize(job, StepBudget(steps=30, seconds=10), evidence, [])
            self.assertEqual(result.research_id, job["research_id"])
        async with asyncio.timeout(5):
            await asyncio.gather(*(distinct_job(n) for n in range(3)))
        self.assertEqual(peak, 2)
        self.assertEqual(slots.calls_in_use, 0)
    async def test_cancelled_job_does_not_start_verifier(self):
        self.service._cancelled.add(self.job["research_id"])
        m = Model()
        result, gaps = await self.run_deep(m)
        self.assertEqual(m.calls, [])
        self.assertTrue(gaps)


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class ModelLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_deep_ledger")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_deep_ledger")
        self.ledger = ResearchBudgetLedger(accounts=self.repo.db.research_budget_accounts, usage=self.repo.db.research_usage)
        self.settings = SimpleNamespace(RESEARCH_DEEP_API_KEY="SECRET_CANARY", RESEARCH_DEEP_MODEL="explicit",
            RESEARCH_DEEP_INPUT_USD_PER_MILLION="0.10", RESEARCH_DEEP_OUTPUT_USD_PER_MILLION="0.20",
            RESEARCH_DEEP_ACCOUNT_CEILING_USD="0.010000")
        self.job = {**support.job_doc(), "paid_budget_micro_usd": 5000}
    async def asyncTearDown(self):
        await self.repo.db.client.drop_database("openceo_test_deep_ledger")
        await self.repo.close()
    async def test_reserve_before_io_single_request_subcent_usage(self):
        calls = []
        async def handler(request):
            self.assertEqual(await self.repo.db.research_budget_accounts.count_documents({"reservations.0": {"$exists": True}}), 1)
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"ok"}'}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 10}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m = BudgetedDeepModel(self.settings, self.ledger, client=client)
            result = await m.run_phase(job=self.job, phase="researcher", prompt="JSON", seconds=2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])
        self.assertEqual(calls[0]["thinking"], {"type": "disabled"})
        self.assertEqual(result.usage["rate_estimated_micro_usd"], 3)
        self.assertIsNone(result.reconciled_micro_usd)
        self.assertEqual(await self.repo.db.research_usage.count_documents({}), 1)
        self.assertNotIn("SECRET_CANARY", str(await self.repo.db.research_usage.find_one({})))
    async def test_budget_zero_and_restricted_make_zero_calls(self):
        async def forbidden(request):
            self.fail("I/O before approval")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            m = BudgetedDeepModel(self.settings, self.ledger, client=client)
            for overrides in ({"paid_budget_micro_usd": 0}, {"classification": "restricted"}):
                with self.assertRaises(DeepModelError):
                    await m.run_phase(job={**self.job, **overrides}, phase="researcher", prompt="JSON", seconds=2)
    async def test_ambiguous_timeout_keeps_reservation_and_does_not_retry(self):
        calls = []
        async def timeout(request):
            calls.append(1)
            raise httpx.ReadTimeout("SECRET_PROVIDER_BODY")
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            m = BudgetedDeepModel(self.settings, self.ledger, client=client)
            with self.assertRaisesRegex(DeepModelError, "model_unavailable"):
                await m.run_phase(job=self.job, phase="researcher", prompt="JSON", seconds=2)
        self.assertEqual(calls, [1])
        doc = await self.repo.db.research_budget_accounts.find_one({})
        self.assertEqual(doc["reservations"][0]["state"], "uncertain")

    async def test_cancel_inflight_http_stops_without_retry_and_keeps_uncertainty(self):
        entered = asyncio.Event()
        calls = []
        async def blocked(request):
            calls.append(1)
            entered.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
            model = BudgetedDeepModel(self.settings, self.ledger, client=client)
            task = asyncio.create_task(model.run_phase(job=self.job, phase="researcher", prompt="JSON", seconds=5))
            await asyncio.wait_for(entered.wait(), 2)
            await model.cancel(self.job["research_id"])
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, [1])
        self.assertEqual(model.active[self.job["research_id"]], set())
        account = await self.repo.research_budget_accounts.find_one({})
        self.assertEqual(account["reservations"][0]["state"], "uncertain")
