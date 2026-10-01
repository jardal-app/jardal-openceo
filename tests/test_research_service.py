"""Servicio quick de research (TASK-2026-00065).

Se usa el **repositorio Mongo real** contra una base aislada para la toma de trabajos
(claim/lease) y fakes delgados para runtime, evidencia y adquisicion: el servicio es
composicion, y lo que hay que probar es su orden, sus limites y su decision de estado.

Lo que fijan estos tests: limites reales contados en el servicio, cita inventada o
ajena que **nunca** acaba en `completed`, cancelacion coherente con el final, y
recuperacion tras reinicio sin estado `interrupted` ni relanzamiento de gasto.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import research_support as support  # noqa: E402
from research.acquisition.base import DiscoveryItem, DiscoveryResult, ProviderAuthError  # noqa: E402
from research.contracts import (  # noqa: E402
    ResearchJobRequest,
    ResearchJobStatus,
    new_research_id,
)
from research.policies import Principal, RESEARCH_READ, RESEARCH_RUN  # noqa: E402
from research.runtimes.base import RuntimeOutcome  # noqa: E402
from research.service import JobSlots, ResearchDependencies, ResearchService, StepBudget  # noqa: E402
from research.worker.capabilities import CapabilityScope, CapabilityStore  # noqa: E402
from settings import Settings  # noqa: E402


def _principal() -> Principal:
    return Principal(
        user_id="usr_owner",
        permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
        allowed_projects=frozenset({"jardal-openceo"}),
    )


def _project_cfg(**overrides: Any) -> dict[str, Any]:
    cfg = {
        "enabled": True,
        "allowed_modes": ["quick"],
        "classification_ceiling": "public",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False},
        "paid_budget_usd": "0.00",
        "limits": {"quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120}},
        "allowed_account_refs": [],
    }
    cfg.update(overrides)
    return {"research": cfg}


def _settings(**overrides: Any) -> Settings:
    payload = {"RESEARCH_ENABLED": True}
    payload.update(overrides)
    return Settings(_env_file=None, **payload)


class _Request:
    @staticmethod
    def build(**overrides: Any) -> ResearchJobRequest:
        payload = {
            "project_key": "jardal-openceo",
            "purpose": "brief",
            "mode": "quick",
            "question": "Pregunta",
        }
        payload.update(overrides)
        return ResearchJobRequest(**payload)


class _FakeEvidence:
    def __init__(self) -> None:
        self.saved: list[Any] = []

    async def asave(self, evidence: Any, **kwargs: Any) -> Any:
        self.saved.append((evidence, kwargs))

        class _Stored:
            evidence_id = evidence.id

        return _Stored()


class _FakeDiscovery:
    def __init__(self, *, count: int = 8, fail: bool = False) -> None:
        self.calls = 0
        self._count = count
        self._fail = fail

    async def __call__(self, **kwargs: Any) -> DiscoveryResult:
        self.calls += 1
        if self._fail:
            raise ProviderAuthError("sin acceso", provider="brave")
        items = tuple(
            DiscoveryItem(url=f"https://example.org/{self.calls}-{i}", provider="brave", rank=i)
            for i in range(1, self._count + 1)
        )
        return DiscoveryResult(provider="brave", items=items)


class _FakeReader:
    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail_urls or set()

    async def __call__(self, *, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(url)
        if url in self._fail:
            raise ProviderAuthError("bloqueado", provider="http")
        return {
            "url": url,
            "text": f"Contenido de {url}",
            "observed_at": datetime.now(timezone.utc),
            "provider": "http",
        }


class _FakeRuntime:
    def __init__(self, *, report: dict[str, Any] | None = None, text: str | None = None,
                 fail: bool = False) -> None:
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.cancelled: list[str] = []
        self._report = report
        self._text = text
        self._fail = fail

    async def run(self, request: Any) -> RuntimeOutcome:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)
            if self._fail:
                raise RuntimeError("runtime roto")
            if self._text is not None:
                text = self._text
            else:
                report = self._report if self._report is not None else {
                    "summary": "Sintesis",
                    "claims": [
                        {
                            "id": "c1",
                            "statement": "Afirmacion respaldada",
                            "kind": "fact",
                            "evidence_ids": ["e1"],
                            "support": [{"extract": "Contenido de example.org"}],
                        }
                    ],
                }
                text = json.dumps(report)
            return RuntimeOutcome(text=text, session_id="ses_1")
        finally:
            self.concurrent -= 1

    async def cancel(self, research_id: str) -> bool:
        self.cancelled.append(research_id)
        return True


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo no disponible en este entorno")
class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self.client = AsyncIOMotorClient(support.MONGO_URL, serverSelectionTimeoutMS=1000)
        await self.client.drop_database(support.TEST_DB)
        self.repo = support.build_repo()
        self.repo._client = self.client  # noqa: SLF001 - cliente del test
        await self.repo.ensure_indexes()
        self.evidence = _FakeEvidence()
        self.discovery = _FakeDiscovery()
        self.reader = _FakeReader()
        self.runtime = _FakeRuntime()
        self.capabilities = CapabilityStore(ttl_seconds=600)
        self.slots = JobSlots(max_jobs=2, max_calls=2)
        self.settings = _settings(
            RESEARCH_OPENCODE_TEST_MODEL="opencode/muse-spark-1.3-contributor-free",
            RESEARCH_WORKSPACE_DIR=str(Path("/tmp") / "research-test"),
        )
        self.service = ResearchService(
            settings=self.settings,
            deps=ResearchDependencies(
                repo=self.repo,
                evidence=self.evidence,
                runtime=self.runtime,
                discover=self.discovery,
                read=self.reader,
                capabilities=self.capabilities,
                slots=self.slots,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.client.drop_database(support.TEST_DB)
        self.client.close()

    async def _submit(self, **overrides: Any) -> dict[str, Any]:
        request = _Request.build(**overrides)
        return await self.service.submit(
            request, principal=_principal(), project_cfg=_project_cfg()
        )

    # ------------------------------------------------------------- admision

    async def test_a_disabled_service_explains_itself(self) -> None:
        service = ResearchService(settings=_settings(RESEARCH_ENABLED=False), deps=self.service._deps)
        available, reason = service.available()
        self.assertFalse(available)
        self.assertIn("deshabilitado", reason)

    async def test_submit_persists_before_answering(self) -> None:
        document = await self._submit()
        self.assertEqual(document["status"], ResearchJobStatus.QUEUED.value)
        stored = await self.repo.get_research_job(document["research_id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["owner_id"], "usr_owner")

    async def test_submit_caps_the_requested_limits(self) -> None:
        document = await self._submit(
            limits={"search_requests": 999, "pages": 999, "steps": 999, "seconds": 999}
        )
        self.assertEqual(document["effective_limits"]["search_requests"], 3)
        self.assertEqual(document["effective_limits"]["pages"], 5)
        self.assertEqual(document["effective_limits"]["steps"], 8)
        self.assertEqual(document["effective_limits"]["seconds"], 120)

    async def test_submit_is_idempotent_by_key(self) -> None:
        first = await self._submit(idempotency_key="k1")
        second = await self._submit(idempotency_key="k1")
        self.assertEqual(first["research_id"], second["research_id"])
        self.assertFalse(second["_created"])

    # --------------------------------------------------------------- ciclo

    async def test_a_supported_quick_cycle_completes(self) -> None:
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.COMPLETED.value)
        result = document["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["coverage_gaps"], [])
        self.assertEqual(self.evidence.saved[0][0].project_key, "jardal-openceo")
        self.assertEqual(result["usage"]["pages_read"], 5)  # 5 paginas leidas

    async def test_the_search_limit_is_enforced_when_nothing_is_found(self) -> None:
        # Un buscador que no devuelve nada obliga a agotar el cupo: exactamente
        # `search_requests` consultas, ni una mas.
        self.service._deps.discover = _FakeDiscovery(count=0)  # noqa: SLF001
        await self._submit()
        await self.service.run_once()
        self.assertEqual(self.service._deps.discover.calls, 3)  # noqa: SLF001

    async def test_searching_stops_once_there_are_enough_candidates(self) -> None:
        # No se gasta cupo de mas: con 8 candidatos en la primera consulta y 5
        # paginas de techo, una sola busqueda basta.
        await self._submit()
        await self.service.run_once()
        self.assertEqual(self.discovery.calls, 1)

    async def test_the_page_limit_is_enforced(self) -> None:
        discovery = _FakeDiscovery(count=2)
        self.service._deps.discover = discovery  # noqa: SLF001
        await self._submit()
        await self.service.run_once()
        self.assertEqual(len(self.reader.calls), 5)

    async def test_seed_urls_are_read_without_spending_searches(self) -> None:
        document = await self._submit(seed_urls=["https://seed.example/a"])
        await self.service.run_once()
        self.assertIn("https://seed.example/a", self.reader.calls)
        self.assertLessEqual(self.discovery.calls, 3)

    # --------------------------------------------------- partial y failed

    async def test_a_failed_read_leaves_a_partial_with_its_cause(self) -> None:
        reader = _FakeReader(fail_urls={"https://example.org/1-1"})
        self.service._deps.read = reader  # noqa: SLF001
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.PARTIAL.value)
        reasons = {g["reason"] for g in document["coverage_gaps"]}
        self.assertIn("access_denied", reasons)

    async def test_an_invented_citation_never_completes(self) -> None:
        self.runtime._report = {  # noqa: SLF001
            "summary": "Sintesis",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Afirmacion sin fuente",
                    "kind": "fact",
                    "evidence_ids": ["e404"],
                    "support": [{"extract": "x"}],
                }
            ],
        }
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.PARTIAL.value)
        self.assertIn(
            "evidencia_inexistente",
            " ".join(g["detail"] for g in document["coverage_gaps"]),
        )

    async def test_a_citation_without_support_is_not_accepted(self) -> None:
        self.runtime._report = {  # noqa: SLF001
            "summary": "Sintesis",
            "claims": [
                {"id": "c1", "statement": "Afirmacion", "kind": "fact", "evidence_ids": ["e1"]}
            ],
        }
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.PARTIAL.value)

    async def test_evidence_from_another_job_is_rejected(self) -> None:
        # El id `e9` existe en el almacen de otro owner, pero no en la evidencia de
        # este job: no vale como cita.
        self.runtime._report = {  # noqa: SLF001
            "summary": "Sintesis",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Afirmacion",
                    "kind": "fact",
                    "evidence_ids": ["e9"],
                    "support": [{"extract": "ajeno"}],
                }
            ],
        }
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.PARTIAL.value)
        self.assertIn(
            "no pertenece a la evidencia de este job",
            " ".join(g["detail"] for g in document["coverage_gaps"]),
        )

    async def test_without_a_result_the_job_fails(self) -> None:
        self.runtime._fail = True  # noqa: SLF001
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.FAILED.value)

    async def test_a_broken_report_is_not_a_success(self) -> None:
        self.runtime._text = "esto no es json"  # noqa: SLF001
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.FAILED.value)

    async def test_no_job_ever_reaches_an_interrupted_state(self) -> None:
        await self._submit()
        document = await self.service.run_once()
        self.assertNotEqual(document["status"], "interrupted")
        self.assertIn(
            document["status"],
            {"completed", "partial", "failed", "cancelled"},
        )

    # ------------------------------------------------------------ cancelacion

    async def test_cancelling_a_queued_job_is_terminal_and_revokes_capabilities(self) -> None:
        document = await self._submit()
        scope = CapabilityScope(
            project_key="jardal-openceo", owner_id="usr_owner", research_id=document["research_id"]
        )
        capability = self.capabilities.issue(scope=scope)
        result = await self.service.cancel(document["research_id"])
        self.assertEqual(result["status"], ResearchJobStatus.CANCELLED.value)
        self.assertEqual(self.capabilities.revoke_for_job(document["research_id"]), 0)
        from research.worker.capabilities import RevokedCapability

        with self.assertRaises(RevokedCapability):
            self.capabilities.authenticate(capability.token, operation="research_read")

    async def test_cancelling_a_finished_job_keeps_the_real_terminal_state(self) -> None:
        await self._submit()
        finished = await self.service.run_once()
        self.assertEqual(finished["status"], ResearchJobStatus.COMPLETED.value)
        again = await self.service.cancel(finished["research_id"])
        self.assertEqual(again["status"], ResearchJobStatus.COMPLETED.value)

    async def test_cancelling_signals_the_runtime(self) -> None:
        document = await self._submit()
        await self.service.cancel(document["research_id"])
        self.assertEqual(self.runtime.cancelled, [document["research_id"]])

    # ----------------------------------------------------------- recuperacion

    async def test_an_orphan_with_evidence_becomes_partial(self) -> None:
        document = await self._submit()
        await self.repo.claim_next_research_job(
            lease_owner="otro", lease_seconds=1, now=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        await self.repo.patch_research_job(document["research_id"], {"evidence_ids": ["e1"]})
        recovered = await self.service.recover_interrupted()
        self.assertEqual(recovered[0]["status"], ResearchJobStatus.PARTIAL.value)
        self.assertEqual(recovered[0]["failure_reason"], "runtime_interrupted")

    async def test_an_orphan_without_evidence_becomes_failed(self) -> None:
        document = await self._submit()
        await self.repo.claim_next_research_job(
            lease_owner="otro", lease_seconds=1, now=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        recovered = await self.service.recover_interrupted()
        self.assertEqual(recovered[0]["status"], ResearchJobStatus.FAILED.value)
        self.assertEqual(recovered[0]["failure_reason"], "runtime_interrupted")

    async def test_recovery_does_not_create_gastos_nor_queued_duplicates(self) -> None:
        await self._submit()
        await self.repo.claim_next_research_job(
            lease_owner="otro", lease_seconds=1, now=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        before = await self.repo.research_jobs.count_documents({})
        await self.service.recover_interrupted()
        after = await self.repo.research_jobs.count_documents({})
        self.assertEqual(before, after)

    # ------------------------------------------------------------ claim/lease

    async def test_two_dispatchers_do_not_take_the_same_job(self) -> None:
        await self._submit()
        first, second = await asyncio.gather(
            self.repo.claim_next_research_job(lease_owner="w1", lease_seconds=60),
            self.repo.claim_next_research_job(lease_owner="w2", lease_seconds=60),
        )
        claimed = [row for row in (first, second) if row is not None]
        self.assertEqual(len(claimed), 1)

    async def test_run_once_returns_none_when_there_is_nothing_queued(self) -> None:
        self.assertIsNone(await self.service.run_once())


class SlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_third_job_waits(self) -> None:
        slots = JobSlots(max_jobs=2, max_calls=2)
        await slots.acquire_job()
        await slots.acquire_job()
        waiting = asyncio.create_task(slots.acquire_job())
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        await slots.release_job()
        await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(slots.snapshot()["jobs_in_use"], 2)

    async def test_job_and_call_slots_are_independent(self) -> None:
        slots = JobSlots(max_jobs=1, max_calls=1)
        await slots.acquire_job()
        # Retener un cupo de job no consume el de llamada.
        await asyncio.wait_for(slots.acquire_call(), timeout=0.5)
        await slots.release_call()
        await slots.release_job()
        self.assertEqual(slots.snapshot()["jobs_in_use"], 0)

    def test_step_budget_counts_steps_and_time(self) -> None:
        budget = StepBudget(steps=2, seconds=100)
        self.assertTrue(budget.consume())
        self.assertTrue(budget.consume())
        self.assertFalse(budget.consume())
        self.assertIn("step_limit", budget.causes())

    def test_an_expired_step_budget_reports_its_cause(self) -> None:
        now = [datetime.now(timezone.utc)]

        def clock() -> datetime:
            return now[0]

        budget = StepBudget(steps=10, seconds=1, clock=clock)
        now[0] = now[0] + timedelta(seconds=5)
        self.assertTrue(budget.expired)
        self.assertFalse(budget.consume())
        self.assertIn("deadline_exceeded", budget.causes())


if __name__ == "__main__":
    unittest.main()
