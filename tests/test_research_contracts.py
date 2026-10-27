"""Contratos y persistencia de research (TASK-2026-00058).

Lo importante que se fija aqui:

* la identidad del job vive en ``research_jobs`` y **nunca** toca ``tasks`` ni el
  observador de Jardal ERP;
* la idempotencia es un indice **parcial** scoped por proyecto/owner/proposito;
* un terminal es estable y no existe el estado ``interrupted``;
* el dinero se serializa como cadena decimal y lo desconocido es ``None``.
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

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from pydantic import ValidationError  # noqa: E402

import research_support as support  # noqa: E402
from research.contracts import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ClaimKind,
    Classification,
    CoverageReason,
    EvidenceSupport,
    ResearchClaim,
    ResearchCoverageGap,
    ResearchEvidence,
    ResearchJobRequest,
    ResearchJobResult,
    ResearchJobStatus,
    ResearchTransitionError,
    ResearchUsage,
    can_transition,
    freshness_state,
    interrupted_recovery,
    most_restrictive,
    result_reusable,
)
from research.money import format_micro  # noqa: E402


class RequestValidationTests(unittest.TestCase):
    def test_blank_fields_are_rejected(self) -> None:
        for field in ("project_key", "purpose", "question"):
            with self.assertRaises(ValidationError):
                support.request(**{field: "   "})

    def test_unknown_fields_are_rejected_instead_of_ignored(self) -> None:
        with self.assertRaises(ValidationError):
            support.request(owner_override="otro")

    def test_limits_cannot_be_negative(self) -> None:
        with self.assertRaises(ValidationError):
            support.request(limits={"search_requests": -1})

    def test_blank_idempotency_key_means_absent(self) -> None:
        self.assertIsNone(support.request(idempotency_key="   ").idempotency_key)

    def test_fingerprint_ignores_owner_and_key_but_not_question(self) -> None:
        base = support.request(idempotency_key="k1", owner_id="a")
        same = support.request(idempotency_key="otra", owner_id="b")
        different = support.request(idempotency_key="k1", question="Otra pregunta")
        self.assertEqual(base.fingerprint(), same.fingerprint())
        self.assertNotEqual(base.fingerprint(), different.fingerprint())

    def test_server_derived_owner_is_only_a_proposal(self) -> None:
        # El contrato acepta owner_id para trazabilidad; no es autorizacion. La
        # derivacion real la hace la politica desde el principal autenticado.
        req = support.request(owner_id="usuario_falsificado")
        self.assertEqual(req.owner_id, "usuario_falsificado")


class MoneySerializationTests(unittest.TestCase):
    def test_unknown_money_is_null_not_zero(self) -> None:
        payload = ResearchUsage().to_payload()
        self.assertIsNone(payload["estimated_usd"])
        self.assertIsNone(payload["reconciled_usd"])

    def test_subcent_estimate_is_preserved(self) -> None:
        payload = ResearchUsage(estimated_micro_usd=2_500).to_payload()
        self.assertEqual(payload["estimated_usd"], "0.002500")

    def test_uncertain_usage_reports_reconciled_as_null(self) -> None:
        payload = ResearchUsage(reconciled_micro_usd=2_500, uncertain=True).to_payload()
        self.assertIsNone(payload["reconciled_usd"])

    def test_usage_round_trips_through_payload(self) -> None:
        original = ResearchUsage(
            search_requests=3,
            pages_read=4,
            model_calls=2,
            estimated_micro_usd=2_500,
            reconciled_micro_usd=2_500,
        )
        restored = ResearchUsage.from_payload(original.to_payload())
        self.assertEqual(restored.estimated_micro_usd, 2_500)
        self.assertEqual(restored.reconciled_micro_usd, 2_500)
        self.assertFalse(restored.uncertain)


class StatusMachineTests(unittest.TestCase):
    def test_there_is_no_seventh_interrupted_state(self) -> None:
        self.assertNotIn("interrupted", {s.value for s in ResearchJobStatus})

    def test_terminal_states_are_stable(self) -> None:
        for status in TERMINAL_STATUSES:
            self.assertEqual(ALLOWED_TRANSITIONS[status], frozenset({status}))
            self.assertTrue(can_transition(status, status))
        self.assertFalse(can_transition(ResearchJobStatus.COMPLETED, ResearchJobStatus.RUNNING))
        self.assertFalse(can_transition(ResearchJobStatus.CANCELLED, ResearchJobStatus.QUEUED))

    def test_queued_can_run_or_cancel_but_not_jump_to_completed(self) -> None:
        self.assertTrue(can_transition(ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING))
        self.assertTrue(can_transition(ResearchJobStatus.QUEUED, ResearchJobStatus.CANCELLED))
        self.assertFalse(can_transition(ResearchJobStatus.QUEUED, ResearchJobStatus.COMPLETED))

    def test_orphan_running_becomes_partial_or_failed_without_relaunching(self) -> None:
        self.assertEqual(
            interrupted_recovery(True), (ResearchJobStatus.PARTIAL, "runtime_interrupted")
        )
        self.assertEqual(
            interrupted_recovery(False), (ResearchJobStatus.FAILED, "runtime_interrupted")
        )


class ResultMechanicsTests(unittest.TestCase):
    def _result(self, **overrides) -> ResearchJobResult:
        evidence = ResearchEvidence(
            id="e1",
            url="https://example.org/source",
            access_method="http",
            observed_at=datetime.now(timezone.utc),
        )
        payload = {
            "research_id": "res_1",
            "status": "completed",
            "project_key": "jardal-openceo",
            "classification": "public",
            "summary": "Sintesis",
            "claims": [
                ResearchClaim(
                    id="c1",
                    statement="Afirmacion",
                    kind=ClaimKind.FACT,
                    evidence_ids=["e1"],
                    support=[EvidenceSupport(location="parrafo 2")],
                )
            ],
            "evidence": [evidence],
        }
        payload.update(overrides)
        return ResearchJobResult(**payload)

    def test_a_valid_result_has_no_mechanical_issues(self) -> None:
        self.assertEqual(self._result().mechanical_issues(), [])

    def test_result_cannot_be_queued_or_running(self) -> None:
        for status in ("queued", "running"):
            with self.assertRaises(ValidationError):
                self._result(status=status)

    def test_invented_evidence_reference_is_flagged(self) -> None:
        result = self._result(
            claims=[
                ResearchClaim(
                    id="c1",
                    statement="Afirmacion",
                    kind=ClaimKind.FACT,
                    evidence_ids=["e404"],
                    support=[EvidenceSupport(location="x")],
                )
            ]
        )
        self.assertIn("claim_evidencia_inexistente:c1:e404", result.mechanical_issues())

    def test_a_citation_without_support_is_flagged(self) -> None:
        result = self._result(
            claims=[
                ResearchClaim(
                    id="c1",
                    statement="Afirmacion",
                    kind=ClaimKind.FACT,
                    evidence_ids=["e1"],
                )
            ]
        )
        self.assertIn("claim_soporte_vacio:c1", result.mechanical_issues())

    def test_a_fact_without_evidence_is_flagged(self) -> None:
        result = self._result(
            claims=[ResearchClaim(id="c1", statement="Afirmacion", kind=ClaimKind.FACT)]
        )
        self.assertIn("claim_sin_evidencia:c1", result.mechanical_issues())

    def test_support_needs_location_or_extract(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceSupport()

    def test_coverage_gaps_are_causes_not_missing_news(self) -> None:
        gap = ResearchCoverageGap(source="x.com", reason=CoverageReason.LOGIN_REQUIRED)
        self.assertEqual(gap.reason, CoverageReason.LOGIN_REQUIRED)
        self.assertEqual(gap.detail, None)

    def test_most_restrictive_wins(self) -> None:
        self.assertEqual(
            most_restrictive(Classification.PUBLIC, Classification.PRIVATE),
            Classification.PRIVATE,
        )


class FreshnessTests(unittest.TestCase):
    def test_old_result_is_stale_and_not_reused(self) -> None:
        now = datetime.now(timezone.utc)
        observed = now - timedelta(hours=200)
        self.assertEqual(freshness_state(observed, max_age_hours=168, now=now), "stale")
        self.assertFalse(result_reusable(observed, max_age_hours=168, now=now))

    def test_missing_observation_cannot_be_called_fresh(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertEqual(freshness_state(None, max_age_hours=168, now=now), "unknown")
        self.assertFalse(result_reusable(None, max_age_hours=168, now=now))

    def test_no_window_requested_does_not_block_reuse(self) -> None:
        self.assertTrue(result_reusable(None, max_age_hours=None))


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo no disponible en este entorno")
class ResearchJobPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Persistencia real: indice parcial, deduplicacion, CAS y cero tickets."""

    async def asyncSetUp(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self.client = AsyncIOMotorClient(support.MONGO_URL, serverSelectionTimeoutMS=1000)
        await self.client.drop_database(support.TEST_DB)
        self.repo = support.build_repo()
        self.repo._client = self.client  # se reutiliza el cliente del test
        await self.repo.ensure_indexes()
        self.observer_calls: list[dict] = []

        async def observer(task):
            self.observer_calls.append(task or {})

        self.repo.set_status_observer(observer)

    async def asyncTearDown(self) -> None:
        await self.client.drop_database(support.TEST_DB)
        self.client.close()

    async def test_two_requests_without_key_do_not_collide(self) -> None:
        first, created_first = await self.repo.create_research_job(support.job_doc())
        second, created_second = await self.repo.create_research_job(support.job_doc())
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(first["research_id"], second["research_id"])

    async def test_same_key_concurrently_deduplicates(self) -> None:
        req = support.request(idempotency_key="interview:1")
        first, second = await asyncio.gather(
            self.repo.create_research_job(support.job_doc(req)),
            self.repo.create_research_job(support.job_doc(req)),
        )
        created_flags = [created for _, created in (first, second)]
        self.assertEqual(created_flags.count(True), 1)
        self.assertEqual(first[0]["research_id"], second[0]["research_id"])
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 1)

    async def test_same_key_with_a_different_payload_is_a_conflict(self) -> None:
        original = support.request(idempotency_key="interview:2")
        await self.repo.create_research_job(support.job_doc(original, research_id="res_original"))
        changed = support.request(idempotency_key="interview:2", question="Otra pregunta")
        with self.assertRaises(Exception) as ctx:
            await self.repo.create_research_job(support.job_doc(changed))
        self.assertEqual(type(ctx.exception).__name__, "ResearchIdempotencyConflict")

    async def test_scope_separates_owner_project_and_purpose(self) -> None:
        req = support.request(idempotency_key="k", project_key="jardal-openceo")
        await self.repo.create_research_job(support.job_doc(req, owner_id="owner-a"))
        other_owner = await self.repo.create_research_job(
            support.job_doc(req, owner_id="owner-b")
        )
        other_project = await self.repo.create_research_job(
            support.job_doc(
                support.request(idempotency_key="k", project_key="otro-proyecto"),
                owner_id="owner-a",
            )
        )
        other_purpose = await self.repo.create_research_job(
            support.job_doc(
                support.request(idempotency_key="k", purpose="otro-proposito"),
                owner_id="owner-a",
            )
        )
        self.assertTrue(other_owner[1])
        self.assertTrue(other_project[1])
        self.assertTrue(other_purpose[1])
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 4)

    async def test_documents_without_key_do_not_break_the_partial_index(self) -> None:
        for _ in range(3):
            await self.repo.create_research_job(support.job_doc())
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 3)
        info = await self.repo.research_jobs.index_information()
        spec = info["idx_research_jobs_idempotency"]
        self.assertTrue(spec.get("unique"))
        self.assertIn("partialFilterExpression", spec)

    async def test_research_never_writes_tasks_or_notifies_business(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.RUNNING)
        await self.repo.transition_research_job(
            doc["research_id"], ResearchJobStatus.COMPLETED, updates={"finished_at": None}
        )
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)
        self.assertEqual(self.observer_calls, [])

    async def test_terminal_transition_is_idempotent_but_never_reopens(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.RUNNING)
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.COMPLETED)
        same = await self.repo.transition_research_job(
            doc["research_id"], ResearchJobStatus.COMPLETED
        )
        self.assertEqual(same["status"], "completed")
        with self.assertRaises(ResearchTransitionError):
            await self.repo.transition_research_job(
                doc["research_id"], ResearchJobStatus.RUNNING
            )

    async def test_transition_on_an_unknown_job_returns_none(self) -> None:
        self.assertIsNone(
            await self.repo.transition_research_job("res_inexistente", ResearchJobStatus.RUNNING)
        )

    async def test_patch_research_job_cannot_change_status(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        updated = await self.repo.patch_research_job(
            doc["research_id"], {"status": "completed", "failure_reason": "x"}
        )
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["failure_reason"], "x")

    async def test_claim_takes_a_job_once(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        claimed = await self.repo.claim_next_research_job(lease_owner="w1", lease_seconds=60)
        again = await self.repo.claim_next_research_job(lease_owner="w2", lease_seconds=60)
        self.assertEqual(claimed["research_id"], doc["research_id"])
        self.assertIsNone(again)
        self.assertEqual(claimed["attempts"], 1)

    async def test_expired_lease_is_listed_for_recovery(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        await self.repo.claim_next_research_job(
            lease_owner="w1", lease_seconds=60, now=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        expired = await self.repo.list_research_jobs_with_expired_lease(
            datetime.now(timezone.utc)
        )
        self.assertEqual([row["research_id"] for row in expired], [doc["research_id"]])

    async def test_heartbeat_only_renews_our_lease(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        await self.repo.claim_next_research_job(lease_owner="w1", lease_seconds=60)
        self.assertTrue(
            await self.repo.patch_research_job_heartbeat(
                doc["research_id"], lease_owner="w1", lease_seconds=120
            )
        )
        self.assertFalse(
            await self.repo.patch_research_job_heartbeat(
                doc["research_id"], lease_owner="w2", lease_seconds=120
            )
        )

    async def test_no_ttl_can_expire_a_live_job_or_its_idempotency_identity(self) -> None:
        # Unico TTL permitido (TASK-2026-00069): sobre ``expires_at``, un campo que solo
        # escribe la transicion a un estado terminal. Ningun otro indice (en especial el de
        # idempotencia y el de la cola) puede expirar documentos.
        info = await self.repo.research_jobs.index_information()
        ttl = {name: spec for name, spec in info.items() if "expireAfterSeconds" in spec}
        self.assertEqual({name: spec["key"] for name, spec in ttl.items()},
                         {"idx_research_jobs_ttl": [("expires_at", 1)]})
        self.assertEqual(ttl["idx_research_jobs_ttl"]["expireAfterSeconds"], 0)

    async def test_only_terminal_jobs_carry_the_ttl_field(self) -> None:
        live, _ = await self.repo.create_research_job(support.job_doc())
        self.assertNotIn("expires_at", live)
        running = await self.repo.transition_research_job(live["research_id"], ResearchJobStatus.RUNNING)
        self.assertNotIn("expires_at", running)
        done = await self.repo.transition_research_job(live["research_id"], ResearchJobStatus.COMPLETED)
        retention = timedelta(days=self.repo._settings.RESEARCH_JOB_RETENTION_DAYS)
        self.assertEqual(done["expires_at"] - done["finished_at"], retention)
        # cancelar y fallar desde la cola tambien son terminales
        for target in (ResearchJobStatus.CANCELLED, ResearchJobStatus.FAILED):
            doc, _ = await self.repo.create_research_job(support.job_doc())
            ended = await self.repo.transition_research_job(doc["research_id"], target)
            self.assertIn("expires_at", ended, target)

    async def test_retention_zero_disables_the_job_ttl(self) -> None:
        self.repo._settings.RESEARCH_JOB_RETENTION_DAYS = 0
        doc, _ = await self.repo.create_research_job(support.job_doc())
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.RUNNING)
        done = await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.COMPLETED)
        self.assertNotIn("expires_at", done)

    async def test_the_idempotency_index_survives_the_ttl_of_a_terminal_job(self) -> None:
        # Mongo borra el job terminal, no su dedup de ocurrencias (esa vive en eventos): la
        # clave idempotente vuelve a estar libre, que es el limite documentado de la retencion.
        req = support.request(idempotency_key="k-ttl")
        doc, _ = await self.repo.create_research_job(support.job_doc(req))
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.RUNNING)
        await self.repo.transition_research_job(doc["research_id"], ResearchJobStatus.COMPLETED)
        await self.repo.research_jobs.delete_one({"research_id": doc["research_id"]})
        again, created = await self.repo.create_research_job(support.job_doc(req))
        self.assertTrue(created)
        self.assertNotEqual(again["research_id"], doc["research_id"])

    async def test_patch_research_job_cannot_change_status(self) -> None:
        doc, _ = await self.repo.create_research_job(support.job_doc())
        updated = await self.repo.patch_research_job(
            doc["research_id"], {"status": "completed", "failure_reason": "x"}
        )
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["failure_reason"], "x")


if __name__ == "__main__":
    unittest.main()
