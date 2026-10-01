"""Aceptación de la Entrega A (TASK-2026-00067).

Este archivo es la evidencia **verificable** de lo que sí puede aceptarse en un
entorno sin CLI de OpenCode, sin backend de aislamiento y sin claves de proveedor, y
separa con claridad lo que queda bloqueado:

* **Composición real**: repositorio Mongo real, ledger real y almacén de evidencia
  real (SQLite/FTS5). Solo la red y el LLM son fakes, y se declaran como tales.
* **Aislamiento**: la evidencia de un owner no es visible para otro, ni siquiera en el
  mismo proyecto.
* **Fallos integrados deterministas**: cuota, presupuesto cero, precio subcentavo,
  reserva concurrente por el último saldo, cancelación coherente y recuperación tras
  reinicio.
* **Regresión del transporte real**: el bug que solo aparecía con el transporte de
  httpx real (y que los fakes no veían) queda cubierto sin red.

Lo que **no** se puede afirmar aquí y por eso mantiene abierta la aceptación: el
circuito real modelo → tool MCP del broker → lectura real → síntesis citada, y el
smoke tras reinicio del servicio. El smoke de red es opt-in
(`RESEARCH_ACCEPTANCE_SMOKE=1`) y se identifica como tal.
"""

from __future__ import annotations

import os
import sys
import tempfile
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

import httpx  # noqa: E402

import research_support as support  # noqa: E402
from research.acquisition.base import (  # noqa: E402
    DiscoveryItem,
    DiscoveryResult,
    ProviderQuotaExhausted,
)
from research.budget import ResearchBudgetLedger  # noqa: E402
from research.contracts import (  # noqa: E402
    Classification,
    ResearchJobRequest,
    ResearchJobStatus,
)
from research.evidence import EvidenceScope, EvidenceVault  # noqa: E402
from research.policies import (  # noqa: E402
    RESEARCH_READ,
    RESEARCH_RUN,
    EgressGuard,
    PinnedEgressTransport,
    Principal,
)
from research.runtimes.base import RuntimeOutcome  # noqa: E402
from research.service import ResearchDependencies, ResearchService  # noqa: E402
from research.wiring import ProjectEvidence  # noqa: E402
from settings import Settings  # noqa: E402

OWNER = "usr_acceptance"
PROJECT = "jardal-openceo"

SMOKE = os.environ.get("RESEARCH_ACCEPTANCE_SMOKE") == "1"


def _principal(owner: str = OWNER, projects=(PROJECT,)) -> Principal:
    return Principal(
        user_id=owner,
        permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
        allowed_projects=frozenset(projects),
    )


def _cfg(**overrides: Any) -> dict[str, Any]:
    research = {
        "enabled": True,
        "allowed_modes": ["quick"],
        "classification_ceiling": "public",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False},
        "paid_budget_usd": "0.00",
        "limits": {"quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120}},
        "allowed_account_refs": [],
    }
    research.update(overrides)
    return {"research": research}


class _Discovery:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self._fail = fail

    async def __call__(self, **kwargs: Any) -> DiscoveryResult:
        self.calls += 1
        if self._fail:
            raise ProviderQuotaExhausted("sin saldo", provider="brave")
        return DiscoveryResult(
            provider="brave",
            items=tuple(
                DiscoveryItem(url=f"https://example.org/p{i}", provider="brave", rank=i)
                for i in range(1, 4)
            ),
        )


class _Reader:
    """Lector fake: declara su procedencia para que la evidencia sea trazable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, *, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(url)
        return {
            "url": url,
            "text": f"Texto observado en {url} (lector fake, sin red)",
            "observed_at": datetime.now(timezone.utc),
            "provider": "http-fake",
        }


class _Runtime:
    """Runtime fake: la ruta LLM real no existe en este entorno y se declara."""

    def __init__(self, *, report: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self._report = report

    async def run(self, request: Any) -> RuntimeOutcome:
        self.calls += 1
        import json

        report = self._report or {
            "summary": "Sintesis local (runtime fake, sin LLM)",
            "claims": [
                {
                    "id": "c1",
                    "statement": "En example.org/p1 se observo un texto",
                    "kind": "fact",
                    "evidence_ids": ["e1"],
                    "support": [{"extract": "Texto observado en https://example.org/p1"}],
                }
            ],
        }
        return RuntimeOutcome(
            text=json.dumps(report).replace("\"e1\"", f"\"{request.research_id}-e1\""),
            session_id="ses_fake",
        )


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo no disponible en este entorno")
class AcceptanceATests(unittest.IsolatedAsyncioTestCase):
    """Circuito compuesto con repositorio, ledger y almacén reales."""

    async def asyncSetUp(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self.tmp = tempfile.TemporaryDirectory()
        self.client = AsyncIOMotorClient(support.MONGO_URL, serverSelectionTimeoutMS=1000)
        await self.client.drop_database(support.TEST_DB)
        self.repo = support.build_repo()
        self.repo._client = self.client  # noqa: SLF001
        await self.repo.ensure_indexes()
        self.vault = EvidenceVault(root=self.tmp.name)
        self.ledger = ResearchBudgetLedger(
            accounts=self.repo.research_budget_accounts, usage=self.repo.research_usage
        )
        self.discovery = _Discovery()
        self.reader = _Reader()
        self.runtime = _Runtime()
        self.service = ResearchService(
            settings=Settings(
                _env_file=None,
                RESEARCH_ENABLED=True,
                RESEARCH_EVIDENCE_DIR=self.tmp.name,
                RESEARCH_WORKSPACE_DIR=str(Path(self.tmp.name) / "ws"),
            ),
            deps=ResearchDependencies(
                repo=self.repo,
                ledger=self.ledger,
                evidence=ProjectEvidence(self.vault),
                runtime=self.runtime,
                discover=self.discovery,
                read=self.reader,
            ),
        )

    async def asyncTearDown(self) -> None:
        self.vault.close()
        await self.client.drop_database(support.TEST_DB)
        self.client.close()
        self.tmp.cleanup()

    async def _submit(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "project_key": PROJECT,
            "purpose": "brief",
            "mode": "quick",
            "question": "Pregunta de aceptacion",
        }
        payload.update(overrides)
        return await self.service.submit(
            ResearchJobRequest(**payload), principal=_principal(), project_cfg=_cfg()
        )

    # ---------------------------------------------------- circuito compuesto

    async def test_the_composed_cycle_persists_real_evidence_and_cites_it(self) -> None:
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.COMPLETED.value)
        result = document["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["claims"]), 1)
        scoped_id = f"{document['research_id']}-e1"
        self.assertEqual(result["claims"][0]["evidence_ids"], [scoped_id])

        # La evidencia existe de verdad en el almacen, no solo en el informe.
        repository = self.vault.project(PROJECT)
        stored = await repository.aget(
            EvidenceScope(project_key=PROJECT, owner_id=OWNER), scoped_id
        )
        self.assertIsNotNone(stored)
        self.assertIn("example.org", stored.evidence.url)
        self.assertTrue(stored.content_hash)
        self.assertIn("Texto observado", stored.content or "")

    async def test_the_evidence_index_is_queryable_with_real_fts(self) -> None:
        await self._submit()
        document = await self.service.run_once()
        repository = self.vault.project(PROJECT)
        hits = await repository.asearch(
            EvidenceScope(project_key=PROJECT, owner_id=OWNER), "observado"
        )
        self.assertTrue(hits)
        self.assertEqual(hits[0].evidence.id, f"{document['research_id']}-e1")

    async def test_evidence_of_another_owner_is_not_visible(self) -> None:
        await self._submit()
        document = await self.service.run_once()
        repository = self.vault.project(PROJECT)
        foreign = await repository.aget(
            EvidenceScope(project_key=PROJECT, owner_id="usr_ajeno"),
            f"{document['research_id']}-e1",
        )
        self.assertIsNone(foreign)
        hits = await repository.asearch(
            EvidenceScope(project_key=PROJECT, owner_id="usr_ajeno"), "observado"
        )
        self.assertEqual(hits, [])

    async def test_two_jobs_of_the_same_project_do_not_collide_on_evidence(self) -> None:
        """Regresion del defecto hallado al correr el circuito por la API.

        El almacen de evidencias es **por proyecto** y el servicio usaba ids `e1`, `e2`...
        solo unicos dentro del job: un segundo job del mismo proyecto (otro owner)
        fallaba con `EvidenceConflict`. Los ids llevan ahora el `research_id`.
        """
        first = await self.service.submit(
            ResearchJobRequest(
                project_key=PROJECT, purpose="brief", mode="quick", question="Primera"
            ),
            principal=_principal(owner=OWNER),
            project_cfg=_cfg(),
        )
        second = await self.service.submit(
            ResearchJobRequest(
                project_key=PROJECT, purpose="brief", mode="quick", question="Segunda"
            ),
            principal=_principal(owner="usr_segundo"),
            project_cfg=_cfg(),
        )
        first_done = await self.service.run_once()
        second_done = await self.service.run_once()
        self.assertEqual(first_done["status"], ResearchJobStatus.COMPLETED.value)
        self.assertEqual(second_done["status"], ResearchJobStatus.COMPLETED.value)
        first_ids = {row["id"] for row in first_done["result"]["evidence"]}
        second_ids = {row["id"] for row in second_done["result"]["evidence"]}
        self.assertTrue(first_ids and second_ids)
        self.assertEqual(first_ids & second_ids, set())
        self.assertTrue(
            all(row["id"].startswith(first["research_id"]) for row in first_done["result"]["evidence"])
        )

    async def test_research_does_not_create_business_tickets(self) -> None:
        await self._submit()
        await self.service.run_once()
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)

    # --------------------------------------------------------- fallos deterministas

    async def test_an_exhausted_quota_leaves_a_partial_with_its_cause(self) -> None:
        self.service._deps.discover = _Discovery(fail=True)  # noqa: SLF001
        await self._submit()
        document = await self.service.run_once()
        # Sin candidatos no hay lectura ni sintesis: fallo tecnico honesto.
        self.assertEqual(document["status"], ResearchJobStatus.FAILED.value)
        self.assertIn("quota_exhausted", str(document.get("coverage_gaps")))

    async def test_a_report_with_foreign_evidence_stays_partial(self) -> None:
        self.runtime._report = {  # noqa: SLF001
            "summary": "Sintesis",
            "claims": [
                {
                    "id": "c1",
                    "statement": "Afirmacion con cita ajena",
                    "kind": "fact",
                    "evidence_ids": ["e9"],
                    "support": [{"extract": "ajeno"}],
                }
            ],
        }
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.PARTIAL.value)
        self.assertIn("no pertenece a la evidencia de este job", str(document["coverage_gaps"]))
        self.assertNotEqual(document["result"]["status"], "completed")

    async def test_firecrawl_disabled_does_not_block_the_route(self) -> None:
        # El adaptador opcional esta deshabilitado y el ciclo funciona igual.
        await self._submit()
        document = await self.service.run_once()
        self.assertEqual(document["status"], ResearchJobStatus.COMPLETED.value)

    async def test_zero_paid_budget_records_zero_paid_calls(self) -> None:
        await self._submit()
        await self.service.run_once()
        self.assertEqual(await self.repo.research_usage.count_documents({}), 0)
        snapshot = await self.ledger.snapshot(provider="muse")
        self.assertEqual(snapshot["spent_usd"], "0.000000")
        self.assertEqual(snapshot["live_reservations"], 0)

    async def test_subcent_reservation_and_concurrent_last_balance(self) -> None:
        import asyncio

        first, second = await asyncio.gather(
            self.ledger.reserve(
                provider="muse",
                amount_micro_usd=2_500,
                operation_id="op-1",
                project_key=PROJECT,
                research_id="res_1",
                ceiling_micro_usd=2_500,
            ),
            self.ledger.reserve(
                provider="muse",
                amount_micro_usd=2_500,
                operation_id="op-2",
                project_key=PROJECT,
                research_id="res_2",
                ceiling_micro_usd=2_500,
            ),
        )
        accepted = [d for d in (first, second) if d.accepted]
        self.assertEqual(len(accepted), 1, "el ultimo saldo solo se puede reservar una vez")
        # Subcentavo: $0,0025 no se redondea a 0.
        snapshot = await self.ledger.snapshot(provider="muse")
        self.assertEqual(snapshot["reserved_usd"], "0.002500")

    async def test_cancel_after_finish_keeps_the_terminal_state(self) -> None:
        await self._submit()
        finished = await self.service.run_once()
        self.assertEqual(finished["status"], ResearchJobStatus.COMPLETED.value)
        again = await self.service.cancel(finished["research_id"])
        self.assertEqual(again["status"], ResearchJobStatus.COMPLETED.value)

    async def test_restart_recovers_an_orphan_without_an_interrupted_state(self) -> None:
        document = await self._submit()
        await self.repo.claim_next_research_job(
            lease_owner="caido", lease_seconds=1, now=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        recovered = await self.service.recover_interrupted()
        self.assertEqual(recovered[0]["failure_reason"], "runtime_interrupted")
        self.assertIn(
            recovered[0]["status"],
            {ResearchJobStatus.PARTIAL.value, ResearchJobStatus.FAILED.value},
        )
        self.assertNotEqual(recovered[0]["status"], "interrupted")
        self.assertEqual(document["status"], ResearchJobStatus.QUEUED.value)


class PinnedRequestRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Regresión del transporte real, sin red.

    El bug: pasar el `stream` original a `httpx.Request` producia un objeto que el
    transporte real de httpx rechazaba con un assert. Los tests con transporte interno
    falso no podian verlo, y aparecio al ejercitar la lectura real.
    """

    async def test_the_pinned_request_carries_a_valid_async_stream(self) -> None:
        async def resolver(host: str) -> list[str]:
            return ["93.184.216.34"]

        guard = EgressGuard(resolver=resolver)
        transport = PinnedEgressTransport(guard)  # transporte interno REAL
        try:
            destination = await guard.validate("https://example.org/x?q=1")
            request = httpx.Request(
                "GET", "https://example.org/x?q=1", headers={"accept": "text/html"}
            )
            pinned = await transport._build_pinned(request, destination)  # noqa: SLF001
            self.assertIsInstance(pinned.stream, httpx.AsyncByteStream)
            self.assertEqual(pinned.url.host, "93.184.216.34")
            self.assertEqual(pinned.headers["host"], "example.org")
            self.assertEqual(pinned.extensions.get("sni_hostname"), "example.org")
        finally:
            await transport.aclose()


@unittest.skipUnless(SMOKE, "smoke de red opt-in: RESEARCH_ACCEPTANCE_SMOKE=1")
class RealNetworkSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Lectura REAL de una fuente publica conocida, a traves del guard de egress."""

    async def test_a_real_public_page_is_read_with_provenance(self) -> None:
        from research.policies import GuardedFetcher

        guard = EgressGuard()
        fetcher = GuardedFetcher(guard=guard)
        page = await fetcher.fetch("https://example.com/")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.content_hash)
        self.assertIsNotNone(page.observed_at)
        self.assertTrue(fetcher.transport.connections)
        # El destino efectivo es una IP publica validada, con Host original.
        self.assertEqual(fetcher.transport.connections[0]["host"], "example.com")


if __name__ == "__main__":
    unittest.main()
