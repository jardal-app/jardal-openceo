"""Service-owned research phases sharing one job, deadline, slots and evidence.

Two selected evidence sets at most; fresh verifier context, one correction and a
final verifier. A semantic verdict is a model opinion backed by checked excerpts,
not a proof of truth or independent consensus.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import timezone

from model_routing import research_wiring as wiring
from model_routing.models import RouterPolicy
from research.contracts import (CoverageReason, ResearchCoverageGap, ResearchJobResult,
                                ResearchJobStatus, ResearchUsage, VerificationVerdict)
from research.deep_model import DeepModelError, PhaseOutcome
from research.free_profile import FreeResearch


def claim_digest(claim):
    return hashlib.sha256(json.dumps(claim, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FreeDeepModel(FreeResearch):
    controlled_calls = True
    manages_budget = True
    name = "free-research"

    def __init__(self, *args):
        super().__init__(*args, plan=["researcher-1", "researcher-2", "verifier", "correction", "verifier-final"],
                         routing_policy=RouterPolicy(selection_policy="native_first"))
        self.state.setdefault("deep_phases", {})

    async def run_phase(self, *, job, phase, prompt, seconds):
        try:
            return await self._phase(job, phase, prompt)
        except wiring.SessionBlocked as exc:
            self.state["blocked_code"] = exc.code
            await self._persist()
            raise

    async def _phase(self, job, phase, prompt):
        if any(row.get("expires_at") and row["expires_at"].replace(
                tzinfo=row["expires_at"].tzinfo or timezone.utc) <= self.service._clock()
               for row in self.evidence.values()):
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "retained evidence expired; a new research job is required")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        saved = self.state["deep_phases"].get(phase)
        if saved and saved["digest"] == digest:
            return PhaseOutcome(saved["text"], estimated_micro_usd=0, usage={"route": self.name, "cached": True})
        if self.state["phase"] != phase:
            self.runtime_session = self.runtime_model = None
        self.state["phase"] = phase
        if self.session is None:
            self.session = self._open_session()
        # Deep phases use only the supplied evidence; tools remain service-owned.
        self.credential = ""
        outcome = await self.session.run_turn_async(
            prompt, self._invoke, pending=[phase], decisions=[phase],
            queries_and_sources=[(eid, row["url"]) for eid, row in self.evidence.items()],
            validated=list(self.evidence), artifacts=[job["research_id"]])
        self.state["deep_phases"][phase] = {"digest": digest, "text": outcome.text}
        wiring.record_checkpoint(wiring.checkpoints_dir(), replace(outcome.checkpoint, pending=()))
        await self._persist()
        return PhaseOutcome(outcome.text, estimated_micro_usd=0,
                            usage={"route": self.name, "model": outcome.model_effective})

    async def _invoke(self, turn):
        before = self.budget.steps_left
        try:
            return await super()._invoke(turn)
        finally:
            self.state["deep_model_calls"] = self.state.get("deep_model_calls", 0) + before - self.budget.steps_left
            await self._persist()

    def _attempt(self, outcome):
        if wiring._managed_cost(outcome.events):
            self.state["billing_uncertain"] = True
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "nonzero cost on free deep route")
        return super()._attempt(outcome)

    def close(self):
        if self.session is not None:
            self.session.close()
        if self.store is not None:
            self.store.close()


class DeepResearch:
    def __init__(self, service, model):
        self.service, self.model = service, model

    async def synthesize(self, job, budget, evidence, coverage):
        if self.model is None or not getattr(self.model, "controlled_calls", False):
            coverage.append(ResearchCoverageGap(source="deep", reason=CoverageReason.MODEL_POLICY_UNAVAILABLE,
                detail="Deep necesita un adapter de llamadas controladas; el CLI quick no garantiza ese control"))
            return None
        if job.get("classification", "public") != "public":
            coverage.append(ResearchCoverageGap(source="deep", reason=CoverageReason.MODEL_POLICY_UNAVAILABLE,
                detail="No hay una ruta privada autorizada para deep"))
            return None
        phases, estimates = [], []
        outputs = []

        async def call(phase, payload):
            if job["research_id"] in self.service._cancelled:
                raise DeepModelError("cancelled")
            managed = getattr(self.model, "manages_budget", False)
            if not managed and not budget.consume():
                raise DeepModelError("budget_exhausted")
            async with __import__("asyncio").timeout(budget.remaining_seconds):
                if not managed:
                    await self.service._deps.slots.acquire_call()
                try:
                    phases.append(phase)  # includes failed/ambiguous attempts
                    began = time.monotonic()
                    outcome = await self.model.run_phase(job=job, phase=phase,
                        prompt=json.dumps(payload, ensure_ascii=False, default=str), seconds=budget.remaining_seconds)
                finally:
                    if not managed:
                        await self.service._deps.slots.release_call()
            estimates.append(outcome.estimated_micro_usd)
            from research.service import extract_report_payload
            try:
                result = json.loads(outcome.text)
            except ValueError:
                result = extract_report_payload(outcome.text)
            if not isinstance(result, dict):
                result = None
            if result is None:
                raise DeepModelError("malformed_output")
            outputs.append({"phase": phase, "usage": outcome.usage, "latency_seconds": time.monotonic() - began})
            return result

        # Exact copies are one source; consumers see duplicate/independence caveats.
        unique, hashes = {}, set()
        for key, row in evidence.items():
            digest = hashlib.sha256(str(row.get("text") or "").encode()).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)
            unique[key] = {"id": key, "url": row.get("url"), "text": str(row.get("text") or "")[:12_000],
                           "published_at": row.get("published_at"), "observed_at": row.get("observed_at"),
                           "trust": "untrusted"}
        if not unique:
            coverage.append(ResearchCoverageGap(source="deep", reason=CoverageReason.SOURCE_UNAVAILABLE, detail="Sin evidencia"))
            return None
        candidates = []
        history = []
        residual = []
        try:
            items = list(unique.values())
            groups = [items] if len(items) < 4 else [items[::2], items[1::2]]
            for number, rows in enumerate(groups):
                draft = await call(f"researcher-{number+1}", {"task": "researcher",
                    "question": job["question"], "subquestion": "Produce checkable claims for this selected evidence set",
                    "schema": {"summary": "text", "claims": [{"id": "c1", "statement": "text", "kind": "fact|declaration|inference|rumor",
                        "evidence_ids": ["evidence id"], "support": [{"extract": "exact source excerpt"}]}]},
                    "evidence": rows, "constraints": "No financial orders. Copies are not independent sources. Missing event/publication dates stay unknown."})
                for claim in draft.get("claims", []):
                    if isinstance(claim, dict):
                        claim["id"] = f"r{number+1}-{claim.get('id', len(candidates))}"
                        candidates.append(claim)
            report = {"summary": " ".join(str(x.get("statement") or "") for x in candidates), "claims": candidates}

            async def verify(report, phase):
                claims = report.get("claims") or []
                checked = await call(phase, {"task": "verifier", "question": job["question"],
                    "claims": [{**c, "claim_digest": claim_digest(c)} for c in claims], "evidence": list(unique.values()),
                    "schema": {"verdicts": [{"claim_digest": "exact supplied digest", "verdict": "supported|contradicted|insufficient_evidence",
                        "evidence_id": "source id", "extract": "exact source excerpt", "reason": "reason", "observed_at": "source date or unknown"}]},
                    "constraints": "Assess each statement against source text, preserve contradictions and attribution; a social statement is not evidence of its underlying fact. Do not treat matching citation IDs or an excerpt that merely exists as support. Reject unrelated UI/footer text as support. Do not infer facts about the user, their tools, hiring, or an absent corpus from document metadata. A source announcement supports its attributed declaration, not independent confirmation. Claims about missing evidence need explicit scope; use insufficient_evidence when the cited source cannot support their meaning."})
                mapped = {v.get("claim_digest"): v for v in checked.get("verdicts", []) if isinstance(v, dict)}
                bad = []
                for c in claims:
                    v = mapped.get(claim_digest(c), {})
                    row = unique.get(v.get("evidence_id"), {})
                    verdict = v.get("verdict", "insufficient_evidence")
                    if (not v.get("extract") or v["extract"] not in row.get("text", "") or
                            v.get("evidence_id") not in c.get("evidence_ids", []) or not v.get("reason") or
                            verdict not in {x.value for x in VerificationVerdict}):
                        verdict = "insufficient_evidence"
                    c["verification"] = verdict
                    history.append({"phase": phase, "statement": c.get("statement"), "verdict": verdict,
                                    "evidence_id": v.get("evidence_id"), "extract": v.get("extract"), "reason": v.get("reason")})
                    if verdict != "supported":
                        bad.append(c)
                return bad

            residual = await verify(report, "verifier")
            if residual:
                corrected = await call("correction", {"task": "correction", "question": job["question"],
                    "draft": report, "verdicts": history, "evidence": list(unique.values()),
                    "constraints": "One correction only. Preserve contradictions in the report. Every changed or new fact will be verified. Return the same summary/claims schema."})
                # Verification field is service authority, never trusted from correction.
                for c in corrected.get("claims", []):
                    if isinstance(c, dict):
                        c.pop("verification", None)
                report = corrected
                residual = await verify(report, "verifier-final")
            result = self.service._parse_report(job, json.dumps(report), evidence, coverage)
            if result:
                verdict_by_id = {c.get("id"): c.get("verification", "insufficient_evidence") for c in report.get("claims", [])}
                for c in result.claims:
                    c.verification = VerificationVerdict(verdict_by_id.get(c.id, "insufficient_evidence"))
                    for support in c.support:
                        if support.extract and not any(support.extract in str(evidence.get(eid, {}).get("text") or "") for eid in c.evidence_ids):
                            c.verification = VerificationVerdict.INSUFFICIENT_EVIDENCE
                            residual.append(c)
                # Contradictions stay in history even if correction deleted the claim.
                if residual or not result.claims or any(x["verdict"] == "contradicted" for x in history):
                    coverage.append(ResearchCoverageGap(source="verifier", reason=CoverageReason.SOURCE_UNAVAILABLE,
                        detail="Soporte insuficiente o contradicciones conservadas; consultar verification_history"))
                return result
            return None
        except (DeepModelError, TimeoutError, ValueError, TypeError, KeyError):
            coverage.append(ResearchCoverageGap(source="deep", reason=CoverageReason.MODEL_POLICY_UNAVAILABLE,
                detail="Fase incompleta por limites, configuracion, salida o disponibilidad del modelo"))
            if candidates:
                result = self.service._parse_report(job, json.dumps({"summary": "Investigacion incompleta", "claims": candidates}), evidence, coverage)
                if result:
                    for claim in result.claims:
                        claim.verification = VerificationVerdict.INSUFFICIENT_EVIDENCE
                return result
            return None
        finally:
            calls = job.get("deep_http_attempts", 0) if getattr(self.model, "measures_calls", False) else len(phases)
            if getattr(self.model, "manages_budget", False):
                calls = job["free_research"].get("deep_model_calls", 0)
                self.model.close()
            job["deep_execution"] = {"phases": phases, "model_calls": calls, "auxiliary_calls": 0,
                                     "phase_usage": outputs, "verification_history": history,
                                     "uncertain": len(estimates) != len(phases) or any((o["usage"] or {}).get("billing_uncertain") for o in outputs),
                                     "estimated_micro_usd": sum(estimates) if estimates and len(estimates) == len(phases) and all(x is not None for x in estimates) else None}
            if getattr(self.model, "manages_budget", False):
                uncertain = bool(job["free_research"].get("billing_uncertain"))
                job["deep_execution"].update(estimated_micro_usd=None if uncertain else 0, uncertain=uncertain)
