"""Servicio de research: admission, ejecucion quick, cancelacion y partial honesto
(TASK-2026-00065).

Composicion
===========
El servicio une contratos, politica, adquisicion, almacen de evidencia y runtime. Nada
de eso se reimplementa aqui: este modulo decide **orden**, **limites** y **estado**.

Reglas que implementa
=====================
* **Persistir antes de responder.** El job existe en ``research_jobs`` antes de que la
  fachada conteste; un reinicio no lo pierde.
* **Cola reactiva, sin barridos.** ``run_once`` toma un job con
  ``find_one_and_update`` (claim atomico con lease) y lo ejecuta. No hay bucle de
  fondo: el despliegue despierta al despachador al admitir o al terminar. Se documenta
  **un unico despachador** por despliegue; la toma atomica evita duplicar trabajo,
  pero no limita la concurrencia global por si sola.
* **Slots separados.** Los jobs y las llamadas al modelo tienen autoridades de cupo
  distintas: el tercer job espera, y un job no retiene un slot de llamada mientras
  espera a nadie. Los techos iniciales son 2 y 2.
* **Limites reales.** Se cuentan llamadas a herramientas, paginas y pasos, no lo que
  diga el prompt. El modelo no puede elevarlos.
* **Validacion mecanica.** Se comprueba esquema, que la evidencia citada exista **y
  pertenezca al scope de este job**, y que cada afirmacion declare soporte. Eso no
  prueba soporte semantico: si el informe no cuadra, el resultado es ``partial``, y
  una cita inventada o ajena **nunca** acaba en ``completed``.
* **Cancelar y terminar no se pisan.** La cancelacion es un CAS; si el job ya es
  terminal, se devuelve el estado real en lugar de reescribirlo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from research.acquisition.base import AcquisitionError
from research.contracts import (
    TERMINAL_STATUSES,
    AccessMethod,
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
    ResearchLimits,
    ResearchUsage,
    interrupted_recovery,
    new_research_id,
    utcnow,
)
from research.money import format_micro
from research.policies import EffectivePolicy, ResearchAccessDenied, derive_effective_policy
from research.worker.capabilities import CapabilityScope, CapabilityStore

logger = logging.getLogger(__name__)

__all__ = [
    "JobSlots",
    "ResearchDependencies",
    "ResearchService",
    "ResearchServiceError",
    "StepBudget",
    "reactive_dispatch_note",
]

LEASE_SECONDS = 120


class ResearchServiceError(RuntimeError):
    """Fallo del servicio que no es un resultado parcial."""


@dataclass
class JobSlots:
    """Cupos separados de jobs y de llamadas al modelo.

    Existen por separado a proposito: si un job retuviera un cupo de llamada mientras
    espera, dos jobs podrian bloquear todo el servicio sin estar llamando a nadie.
    """

    max_jobs: int = 2
    max_calls: int = 2

    def __post_init__(self) -> None:
        self.jobs_in_use = 0
        self.calls_in_use = 0
        self._condition = asyncio.Condition()

    async def acquire_job(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self.jobs_in_use < self.max_jobs)
            self.jobs_in_use += 1

    async def release_job(self) -> None:
        async with self._condition:
            self.jobs_in_use = max(0, self.jobs_in_use - 1)
            self._condition.notify_all()

    async def acquire_call(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self.calls_in_use < self.max_calls)
            self.calls_in_use += 1

    async def release_call(self) -> None:
        async with self._condition:
            self.calls_in_use = max(0, self.calls_in_use - 1)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        return {
            "jobs_in_use": self.jobs_in_use,
            "calls_in_use": self.calls_in_use,
            "max_jobs": self.max_jobs,
            "max_calls": self.max_calls,
        }


class StepBudget:
    """Presupuesto de pasos y tiempo de una investigacion.

    Un "paso" es una llamada a herramienta (descubrir, leer, sintetizar). Se cuenta
    aqui y no en el prompt: el modelo no puede ampliarlo.
    """

    def __init__(self, *, steps: int, seconds: int, clock: Callable[[], datetime] | None = None) -> None:
        self._steps_left = max(0, int(steps))
        self._clock = clock or utcnow
        self._deadline = self._clock() + timedelta(seconds=max(0, int(seconds)))

    @property
    def steps_left(self) -> int:
        return self._steps_left

    @property
    def expired(self) -> bool:
        return self._clock() >= self._deadline

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, (self._deadline - self._clock()).total_seconds())

    def consume(self) -> bool:
        if self.expired or self._steps_left <= 0:
            return False
        self._steps_left -= 1
        return True

    def causes(self) -> list[str]:
        causes: list[str] = []
        if self.expired:
            causes.append("deadline_exceeded")
        if self._steps_left <= 0:
            causes.append("step_limit")
        return causes


@dataclass
class ResearchDependencies:
    """Piezas inyectadas. El servicio no crea clientes ni abre conexiones."""

    repo: Any
    ledger: Any | None = None
    evidence: Any | None = None
    runtime: Any | None = None
    discover: Callable[..., Awaitable[Any]] | None = None
    read: Callable[..., Awaitable[dict[str, Any]]] | None = None
    capabilities: CapabilityStore | None = None
    slots: JobSlots = field(default_factory=JobSlots)


def reactive_dispatch_note() -> str:
    return (
        "Un unico despachador por despliegue: la toma es atomica (find_one_and_update "
        "con lease) y se despierta al admitir o al terminar, sin barridos periodicos."
    )


class ResearchService:
    """Servicio quick de extremo a extremo."""

    def __init__(
        self,
        *,
        settings: Any,
        deps: ResearchDependencies,
        coordinator_id: str = "research-1",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._deps = deps
        self._coordinator = coordinator_id
        self._clock = clock or utcnow
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------ disponibilidad

    def available(self) -> tuple[bool, str]:
        """(disponible, motivo). Un servicio sin claves sigue existiendo para explicar."""
        if not bool(getattr(self._settings, "RESEARCH_ENABLED", False)):
            return False, "research deshabilitado (RESEARCH_ENABLED=false)"
        return True, "ok"

    # ----------------------------------------------------------------- admision

    async def submit(
        self,
        request: ResearchJobRequest,
        *,
        principal: Any,
        project_cfg: dict[str, Any],
        effective_limits: ResearchLimits | None = None,
    ) -> dict[str, Any]:
        """Admite el job: deriva politica, persiste y devuelve el documento."""
        if not bool(getattr(self._settings, "RESEARCH_ENABLED", False)):
            raise ResearchServiceError("research deshabilitado")
        policy = derive_effective_policy(
            principal=principal,
            request=request,
            project_cfg=project_cfg,
            settings=self._settings,
        )
        limits = effective_limits or policy.limits
        doc = {
            "research_id": new_research_id(),
            "project_key": policy.project_key,
            "owner_id": policy.owner_id,
            "purpose": request.purpose,
            "mode": request.mode.value,
            "question": request.question,
            "request_fingerprint": request.fingerprint(),
            "idempotency_key": request.idempotency_key,
            "status": ResearchJobStatus.QUEUED.value,
            "classification": policy.classification.value,
            "budget_ref": policy.budget_ref,
            "effective_limits": {
                "search_requests": limits.search_requests,
                "pages": limits.pages,
                "steps": limits.steps,
                "seconds": limits.seconds,
            },
            "seed_urls": list(request.seed_urls),
            "attempts": 0,
            "created_at": self._clock(),
            "updated_at": self._clock(),
        }
        stored, created = await self._deps.repo.create_research_job(doc)
        stored["_created"] = created
        return stored

    async def status(self, research_id: str) -> dict[str, Any] | None:
        return await self._deps.repo.get_research_job(research_id)

    # ------------------------------------------------------------ despacho

    async def run_once(self) -> dict[str, Any] | None:
        """Toma un job encolado y lo ejecuta. Devuelve el documento final o ``None``."""
        job = await self._deps.repo.claim_next_research_job(
            lease_owner=self._coordinator, lease_seconds=LEASE_SECONDS, now=self._clock()
        )
        if job is None:
            return None
        await self._deps.slots.acquire_job()
        try:
            return await self._execute(job)
        finally:
            await self._deps.slots.release_job()

    async def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        research_id = job["research_id"]
        scope = CapabilityScope(
            project_key=job["project_key"],
            owner_id=job["owner_id"],
            research_id=research_id,
            classification=Classification(job.get("classification") or "public"),
        )
        limits = job.get("effective_limits") or {}
        budget = StepBudget(
            steps=int(limits.get("steps", 8)),
            seconds=int(limits.get("seconds", 120)),
            clock=self._clock,
        )
        evidence_rows: dict[str, dict[str, Any]] = {}
        coverage: list[ResearchCoverageGap] = []
        searches = 0

        try:
            candidates, searches = await self._discover(job, scope, budget, limits, coverage)
            await self._read_pages(job, scope, budget, limits, candidates, evidence_rows, coverage)
            result = await self._synthesize(job, scope, budget, evidence_rows, coverage)
        except asyncio.CancelledError:
            raise
        except AcquisitionError as exc:
            return await self._finish(
                job,
                ResearchJobStatus.FAILED,
                failure_reason=exc.kind,
                coverage=list(coverage) + [ResearchCoverageGap(source="service", reason=exc.coverage_reason, detail=exc.detail)],
                evidence_rows=evidence_rows,
            )
        except Exception as exc:  # noqa: BLE001 - un fallo tecnico no puede tumbar el dispatcher
            logger.warning("Job %s fallo tecnicamente", research_id, exc_info=True)
            return await self._finish(
                job,
                ResearchJobStatus.FAILED,
                failure_reason=f"internal_error:{exc.__class__.__name__}",
                evidence_rows=evidence_rows,
            )

        status = self._decide_status(result, coverage, budget)
        await self._release_capabilities(research_id)
        return await self._finish(
            job, status, result=result, coverage=coverage, evidence_rows=evidence_rows,
            searched=searches,
        )

    # ------------------------------------------------------------ pasos

    async def _discover(
        self,
        job: dict[str, Any],
        scope: CapabilityScope,
        budget: StepBudget,
        limits: dict[str, Any],
        coverage: list[ResearchCoverageGap],
    ) -> tuple[list[str], int]:
        """Devuelve (candidatos, busquedas_realizadas). El conteo sale de aqui, no de
        un atributo oculto: los limites se cuentan donde se consumen."""
        urls: list[str] = [u for u in (job.get("seed_urls") or []) if isinstance(u, str)]
        max_searches = int(limits.get("search_requests", 0))
        if len(urls) >= int(limits.get("pages", 0)) or max_searches <= 0 or self._deps.discover is None:
            return urls, 0
        searches = 0
        for _ in range(max_searches):
            if not budget.consume():
                coverage.append(
                    ResearchCoverageGap(
                        source="service",
                        reason=CoverageReason.SOURCE_UNAVAILABLE,
                        detail="Limite de pasos o deadline agotado durante el descubrimiento",
                    )
                )
                break
            searches += 1
            try:
                found = await self._deps.discover(
                    question=job["question"], research_id=job["research_id"], project_key=job["project_key"]
                )
            except AcquisitionError as exc:
                coverage.append(
                    ResearchCoverageGap(source=exc.provider or "discover", reason=exc.coverage_reason, detail=exc.detail)
                )
                break
            for item in getattr(found, "items", ()) or ():
                url = getattr(item, "url", None)
                if isinstance(url, str) and url not in urls:
                    urls.append(url)
            if len(urls) >= int(limits.get("pages", 0)):
                break
        return urls, searches

    async def _read_pages(
        self,
        job: dict[str, Any],
        scope: CapabilityScope,
        budget: StepBudget,
        limits: dict[str, Any],
        urls: list[str],
        evidence_rows: dict[str, dict[str, Any]],
        coverage: list[ResearchCoverageGap],
    ) -> None:
        if self._deps.read is None:
            coverage.append(
                ResearchCoverageGap(
                    source="service",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="No hay lector HTTP configurado",
                )
            )
            return
        max_pages = int(limits.get("pages", 0))
        for index, url in enumerate(urls[:max_pages], start=1):
            if not budget.consume():
                coverage.append(
                    ResearchCoverageGap(
                        source="service",
                        reason=CoverageReason.SOURCE_UNAVAILABLE,
                        detail="Limite de pasos o deadline agotado durante la lectura",
                    )
                )
                break
            try:
                page = await self._deps.read(url=url, research_id=job["research_id"], project_key=job["project_key"])
            except AcquisitionError as exc:
                coverage.append(
                    ResearchCoverageGap(source=exc.provider or "read", reason=exc.coverage_reason, detail=exc.detail)
                )
                continue
            evidence_id = f"e{index}"
            stored = await self._store_evidence(
                job, scope, evidence_id=evidence_id, page=page
            )
            evidence_rows[evidence_id] = {
                "url": page.get("url") or url,
                "text": page.get("text") or "",
                "stored": stored,
            }

    async def _store_evidence(
        self, job: dict[str, Any], scope: CapabilityScope, *, evidence_id: str, page: dict[str, Any]
    ) -> Any:
        if self._deps.evidence is None:
            return None
        evidence = ResearchEvidence(
            id=evidence_id,
            url=str(page.get("url") or ""),
            access_method=AccessMethod.HTTP,
            visibility=scope.classification,
            observed_at=page.get("observed_at") or self._clock(),
            published_at=page.get("published_at"),
            content_hash=page.get("content_hash"),
            project_key=job["project_key"],
            owner_id=job["owner_id"],
        )
        return await self._deps.evidence.asave(
            evidence, content=page.get("text") or "", provider=page.get("provider")
        )

    async def _synthesize(
        self,
        job: dict[str, Any],
        scope: CapabilityScope,
        budget: StepBudget,
        evidence_rows: dict[str, dict[str, Any]],
        coverage: list[ResearchCoverageGap],
    ) -> ResearchJobResult | None:
        if self._deps.runtime is None:
            coverage.append(
                ResearchCoverageGap(
                    source="runtime",
                    reason=CoverageReason.MODEL_POLICY_UNAVAILABLE,
                    detail="No hay runtime de sintesis configurado",
                )
            )
            return None
        if not evidence_rows:
            coverage.append(
                ResearchCoverageGap(
                    source="service",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="Sin evidencia leida no hay sintesis que validar",
                )
            )
            return None
        if not budget.consume():
            coverage.append(
                ResearchCoverageGap(
                    source="service",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="Limite de pasos o deadline agotado antes de sintetizar",
                )
            )
            return None

        prompt = self._build_prompt(job, evidence_rows)
        await self._deps.slots.acquire_call()
        try:
            from research.runtimes.base import RuntimeRequest

            outcome = await self._deps.runtime.run(
                RuntimeRequest(
                    research_id=job["research_id"],
                    project_key=job["project_key"],
                    owner_id=job["owner_id"],
                    prompt=prompt,
                    workspace=self._workspace_for(job),
                    model=str(getattr(self._settings, "RESEARCH_OPENCODE_TEST_MODEL", "")),
                    timeout_seconds=int(budget.remaining_seconds) or 1,
                    allow_session_resume=False,
                )
            )
        except Exception as exc:  # noqa: BLE001 - el runtime es sustituible y puede fallar
            coverage.append(
                ResearchCoverageGap(
                    source="runtime",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail=f"Runtime no disponible: {exc.__class__.__name__}",
                )
            )
            return None
        finally:
            await self._deps.slots.release_call()

        text = getattr(outcome, "text", "") or ""
        result = self._parse_report(job, text, evidence_rows, coverage)
        return result

    def _workspace_for(self, job: dict[str, Any]):
        from pathlib import Path

        base = Path(str(getattr(self._settings, "RESEARCH_WORKSPACE_DIR", "/tmp/research")))
        return base / job["research_id"]

    def _build_prompt(self, job: dict[str, Any], evidence_rows: dict[str, dict[str, Any]]) -> str:
        lines = [
            "Investiga la pregunta y devuelve SOLO un JSON con esta forma:",
            '{"summary": "...", "claims": [{"id": "c1", "statement": "...", "kind": "fact", '
            '"evidence_ids": ["e1"], "support": [{"extract": "..."}]}]}',
            "Cada afirmacion material debe citar el id de evidencia que la respalda y un extracto.",
            f"Pregunta: {job['question']}",
            "Evidencia disponible:",
        ]
        for evidence_id, row in evidence_rows.items():
            extract = str(row.get("text") or "")[:400]
            lines.append(f"[{evidence_id}] {row.get('url')} :: {extract}")
        return "\n".join(lines)

    def _parse_report(
        self,
        job: dict[str, Any],
        text: str,
        evidence_rows: dict[str, dict[str, Any]],
        coverage: list[ResearchCoverageGap],
    ) -> ResearchJobResult | None:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            coverage.append(
                ResearchCoverageGap(
                    source="runtime",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="La sintesis no devolvio JSON interpretable",
                )
            )
            return None
        if not isinstance(payload, dict):
            coverage.append(
                ResearchCoverageGap(
                    source="runtime",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="La sintesis no devolvio un objeto",
                )
            )
            return None

        claims: list[ResearchClaim] = []
        for index, raw in enumerate(payload.get("claims") or [], start=1):
            if not isinstance(raw, dict):
                continue
            evidence_ids = [e for e in (raw.get("evidence_ids") or []) if isinstance(e, str)]
            support: list[EvidenceSupport] = []
            for entry in raw.get("support") or []:
                if isinstance(entry, dict) and (entry.get("location") or entry.get("extract")):
                    support.append(EvidenceSupport(location=entry.get("location"), extract=entry.get("extract")))
            try:
                kind = ClaimKind(str(raw.get("kind") or "fact"))
            except ValueError:
                kind = ClaimKind.INFERENCE
            claims.append(
                ResearchClaim(
                    id=str(raw.get("id") or f"c{index}"),
                    statement=str(raw.get("statement") or ""),
                    kind=kind,
                    evidence_ids=evidence_ids,
                    support=support,
                )
            )

        evidence_models = [
            ResearchEvidence(
                id=evidence_id,
                url=str(row.get("url") or ""),
                access_method=AccessMethod.HTTP,
                visibility=Classification(str(job.get("classification") or "public")),
                observed_at=self._clock(),
                project_key=job["project_key"],
                owner_id=job["owner_id"],
            )
            for evidence_id, row in evidence_rows.items()
        ]
        result = ResearchJobResult(
            research_id=job["research_id"],
            status=ResearchJobStatus.COMPLETED,
            project_key=job["project_key"],
            classification=Classification(str(job.get("classification") or "public")),
            summary=str(payload.get("summary") or ""),
            claims=claims,
            evidence=evidence_models,
            coverage_gaps=list(coverage),
            usage=ResearchUsage(search_requests=0, pages_read=len(evidence_rows)),
        )

        # Validacion de scope: una cita que no sea de **este** job no vale, aunque el
        # id exista en el almacen de otro owner.
        allowed = set(evidence_rows)
        for claim in result.claims:
            for cited in claim.evidence_ids:
                if cited not in allowed:
                    coverage.append(
                        ResearchCoverageGap(
                            source="service",
                            reason=CoverageReason.SOURCE_UNAVAILABLE,
                            detail=f"Cita {cited} no pertenece a la evidencia de este job",
                        )
                    )
        return result

    def _decide_status(
        self,
        result: ResearchJobResult | None,
        coverage: list[ResearchCoverageGap],
        budget: StepBudget,
    ) -> ResearchJobStatus:
        if result is None:
            return ResearchJobStatus.FAILED
        issues = result.mechanical_issues()
        if issues:
            coverage.append(
                ResearchCoverageGap(
                    source="service",
                    reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="Informe con problemas mecanicos: " + ", ".join(issues[:3]),
                )
            )
        if budget.expired or budget.steps_left <= 0:
            coverage.append(
                ResearchCoverageGap(
                    source="service",
                    reason=CoverageReason.BUDGET_EXHAUSTED,
                    detail="Limites de la investigacion agotados: " + ", ".join(budget.causes()),
                )
            )
        return ResearchJobStatus.PARTIAL if coverage else ResearchJobStatus.COMPLETED

    async def _finish(
        self,
        job: dict[str, Any],
        status: ResearchJobStatus,
        *,
        result: ResearchJobResult | None = None,
        coverage: list[ResearchCoverageGap] | None = None,
        evidence_rows: dict[str, dict[str, Any]] | None = None,
        failure_reason: str | None = None,
        searched: int = 0,
    ) -> dict[str, Any]:
        research_id = job["research_id"]
        updates: dict[str, Any] = {
            "evidence_ids": sorted((evidence_rows or {}).keys()),
            "coverage_gaps": [g.model_dump(mode="json") for g in (coverage or [])],
        }
        if result is not None:
            if coverage is not None:
                result.coverage_gaps = list(coverage)
            # El estado del informe debe coincidir con el del job: un `partial` no
            # puede llevar dentro un resultado que se declara `completed`.
            result.status = status
            result.usage = ResearchUsage(
                search_requests=searched,
                pages_read=len(evidence_rows or {}),
                estimated_micro_usd=result.usage.estimated_micro_usd,
                reconciled_micro_usd=result.usage.reconciled_micro_usd,
            )
            updates["result"] = result.model_dump(mode="json")
        if failure_reason:
            updates["failure_reason"] = failure_reason
        document = await self._deps.repo.transition_research_job(research_id, status, updates=updates)
        if document is None:
            # Carrera con otro escritor (p. ej. una cancelacion): se devuelve el
            # estado real en vez de reescribirlo.
            document = await self._deps.repo.get_research_job(research_id)
        await self._release_capabilities(research_id)
        return document or job

    # ------------------------------------------------------------ cancelacion

    async def cancel(self, research_id: str) -> dict[str, Any] | None:
        """Cancela por CAS. Si ya es terminal, devuelve el estado real."""
        current = await self._deps.repo.get_research_job(research_id)
        if current is None:
            return None
        if ResearchJobStatus(current["status"]) in TERMINAL_STATUSES:
            return current
        self._cancelled.add(research_id)
        if self._deps.runtime is not None and hasattr(self._deps.runtime, "cancel"):
            try:
                await self._deps.runtime.cancel(research_id)
            except Exception:  # noqa: BLE001 - cancelar no puede fallar por un runtime ausente
                logger.warning("El runtime fallo al cancelar %s", research_id, exc_info=True)
        await self._release_capabilities(research_id, reason="job cancelado")
        document = await self._deps.repo.transition_research_job(
            research_id, ResearchJobStatus.CANCELLED, updates={"failure_reason": "cancelled_by_request"}
        )
        return document or await self._deps.repo.get_research_job(research_id)

    # ------------------------------------------------------------ recuperacion

    async def recover_interrupted(self, *, cutoff: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Repara jobs `running` con lease vencida tras un reinicio.

        Nunca inventa un estado `interrupted` ni relanza gasto: con evidencia util el
        job queda `partial`; sin ella, `failed`. Ambos con causa `runtime_interrupted`.
        """
        moment = cutoff or (self._clock() - timedelta(seconds=LEASE_SECONDS))
        recovered: list[dict[str, Any]] = []
        rows = await self._deps.repo.list_research_jobs_with_expired_lease(moment, limit=limit)
        for job in rows:
            evidence_ids = list(job.get("evidence_ids") or [])
            status, reason = interrupted_recovery(bool(evidence_ids))
            document = await self._deps.repo.transition_research_job(
                job["research_id"],
                status,
                updates={"failure_reason": reason, "finished_at": self._clock()},
            )
            if document is not None:
                recovered.append(document)
            await self._release_capabilities(job["research_id"], reason="job interrumpido")
        return recovered

    # ------------------------------------------------------------ utilidades

    async def _release_capabilities(self, research_id: str, *, reason: str = "job terminado") -> int:
        if self._deps.capabilities is None:
            return 0
        return self._deps.capabilities.revoke_for_job(research_id, reason=reason)

    def slot_snapshot(self) -> dict[str, int]:
        return self._deps.slots.snapshot()
