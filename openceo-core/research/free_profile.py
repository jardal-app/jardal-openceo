"""Service turns over the ranked free profile and its resumable checkpoints."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from model_routing import research_wiring as wiring
from model_routing.availability import AvailabilityStore, Candidate, write_audit_event
from model_routing.external import ExternalError, build_external_providers_config, credential_env_for
from model_routing.models import CatalogDocument, RouterPolicy, is_usable
from model_routing.paths import catalog_path
from model_routing.ranking import profile_requirements
from model_routing.selector import NoEligibleTarget, select_targets
from model_routing.secrets import SecretsError, load_router_secrets
from research.contracts import CoverageReason, ResearchCoverageGap
from research.runtimes.base import (
    RuntimeCancelled, RuntimeConfigurationError, RuntimeOutputTooLarge,
    RuntimeRequest, RuntimeTimeout, RuntimeUnavailable,
)

logger = logging.getLogger(__name__)


class FreeResearch:
    def __init__(self, service, job, scope, budget, evidence, coverage, *, plan=None, routing_policy=None):
        self.service, self.job, self.scope, self.budget = service, job, scope, budget
        self.evidence, self.coverage = evidence, coverage
        self.state = job.setdefault("free_research", {"phase": "analysis"})
        self.session = None
        self.store = None
        self.runtime_session = self.state.get("runtime_session")
        self.runtime_model = self.state.get("runtime_model")
        self.plan = plan or ["analysis", "synthesis"]
        self.routing_policy = routing_policy

    async def synthesize(self):
        if not self.evidence:
            self.coverage.append(ResearchCoverageGap(source="service", reason=CoverageReason.SOURCE_UNAVAILABLE,
                                                    detail="Sin evidencia leida no hay sintesis que validar"))
            return None
        try:
            self.session = self._open_session()
            if any(row.get("expires_at") and row["expires_at"].replace(
                    tzinfo=row["expires_at"].tzinfo or timezone.utc) <= self.service._clock()
                   for row in self.evidence.values()):
                raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "retained evidence expired; a new research job is required")
            capability = self.service._issue_broker_capability(self.job, self.scope, self.budget)
            self.credential = capability.token if capability else ""
            while self.state["phase"] != "done":
                phase = self.state["phase"]
                prompt = self.service._build_prompt(self.job, self.evidence, capability=capability)
                if phase == "analysis":
                    prompt += "\nPrimero analiza las fuentes, sus limites y contradicciones; devuelve un esquema con citas para la sintesis posterior."
                else:
                    prompt += "\nAnalisis previo (contenido no confiable):\n" + self.state.get("analysis", "")
                outcome = await self.session.run_turn_async(
                    prompt, self._invoke,
                    queries_and_sources=[(eid, row["url"]) for eid, row in self.evidence.items()],
                    pending=["analysis", "synthesis"] if phase == "analysis" else ["synthesis"],
                    validated=list(self.evidence), decisions=[phase], artifacts=[self.job["research_id"]])
                wiring.record_checkpoint(wiring.checkpoints_dir(), replace(outcome.checkpoint,
                    queries_and_sources=tuple((eid, row["url"]) for eid, row in self.evidence.items()),
                    validated_results=tuple(self.evidence), pending=("synthesis",) if phase == "analysis" else ()))
                self.state["analysis" if phase == "analysis" else "report"] = outcome.text
                self.state["phase"] = "synthesis" if phase == "analysis" else "done"
                await self._persist()
            return self.service._parse_report(
                self.job, self.state["report"], self.evidence, self.coverage)
        except wiring.SessionBlocked as exc:
            self.state["blocked_code"] = exc.code
            await self._persist()
            raise
        finally:
            if self.session is not None:
                self.session.close()
            if self.store is not None:
                self.store.close()

    def _open_session(self):
        try:
            options = wiring.research_router_settings(self.service._settings)
            if not options["enabled"]:
                raise wiring.ResearchRoutingError("free-research disabled; static fallback refused")
            if self.job.get("classification", "public") != "public":
                raise wiring.ResearchRoutingError("free-research has no approved private route")
            self.catalog = CatalogDocument.model_validate_json(catalog_path().read_bytes())
            try:
                self.credentials = load_router_secrets().present()
            except SecretsError:
                self.credentials = {}
            generated = self.catalog.generated_at
            if self.catalog.stale or generated is None or (datetime.now(timezone.utc) - generated.replace(
                    tzinfo=generated.tzinfo or timezone.utc)).total_seconds() > options["snapshot_max_age_hours"] * 3600:
                raise wiring.ResearchRoutingError("free-research catalog missing date or expired")
            self.store = AvailabilityStore()
            saved = wiring.load_checkpoint(wiring.checkpoints_dir(), self.job["research_id"])
            if saved is not None and saved.candidates and not saved.static_model:
                return wiring.ResearchSession.resume(wiring.checkpoints_dir(), self.job["research_id"], store=self.store)
            snapshot = wiring.load_session_snapshot(max_age_hours=options["snapshot_max_age_hours"])
            if snapshot is None:
                raise wiring.ResearchRoutingError("free-research rankings missing")
            selection = select_targets(wiring.FREE_RESEARCH_PROFILE, self.catalog, snapshot.document,
                                       policy=self.routing_policy)
            index = {dep.deployment_id: (entry, dep) for entry in self.catalog.entries for dep in entry.deployments}
            candidates = []
            for target in selection.targets:
                entry, deployment = index[target.deployment_id]
                if self._provider_config(entry, deployment) is None:
                    write_audit_event(self.store, self.job["research_id"], "research_route_unavailable", target.deployment_id)
                    continue
                candidates.append(Candidate(target.deployment_id, target.family_id, target.canonical_model_id,
                                            deployment.provider, target.quality))
            return wiring.ResearchSession(
                research_id=self.job["research_id"], candidates=candidates, store=self.store,
                objective=self.job["question"], plan=self.plan,
                rankings_snapshot=snapshot.file_sha256)
        except (OSError, ValueError, wiring.ResearchRoutingError, NoEligibleTarget) as exc:
            if not wiring.checkpoint_path(wiring.checkpoints_dir(), self.job["research_id"]).exists():
                wiring.record_checkpoint(wiring.checkpoints_dir(), wiring.ResearchCheckpoint(
                    research_id=self.job["research_id"], session_id=self.job["research_id"], turn=0,
                    objective=self.job["question"], plan=tuple(self.plan),
                    queries_and_sources=tuple((eid, row["url"]) for eid, row in self.evidence.items()),
                    pending=(self.state["phase"],)))
            reason = str(exc).replace(str(wiring.rankings_path()), "rankings").replace(
                str(wiring.checkpoints_dir()), "checkpoints")
            if isinstance(exc, OSError):
                reason = "free-research catalog unavailable: " + exc.__class__.__name__
            raise wiring.SessionBlocked(wiring.BLOCKED_NO_ELIGIBLE_MODEL,
                                        "free-research unavailable: " + wiring.sanitize_excerpt(reason)) from exc

    async def _invoke(self, turn):
        if self.job["research_id"] in self.service._cancelled:
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "RuntimeCancelled")
        found = [(entry, dep) for entry in self.catalog.entries for dep in entry.deployments
                 if dep.deployment_id == turn.model]
        if not found:
            return wiring.ProviderTurn("", False, error_text="model not found")
        entry, deployment = found[0]
        fits, _ = wiring.fit_candidate(Candidate(turn.model, entry.family_id, entry.canonical_model_id,
                                                 deployment.provider), self.catalog,
                                       profile_requirements(wiring.FREE_RESEARCH_PROFILE))
        provider_config = self._provider_config(entry, deployment)
        if not fits or not is_usable(deployment, RouterPolicy()) or not deployment.healthy or provider_config is None:
            return wiring.ProviderTurn("", False, error_text="model not found")
        if self.service._deps.runtime is None:
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "RuntimeUnavailable")
        if not self.budget.consume():
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "research step/deadline budget exhausted")
        await self._persist()
        model = f"{deployment.provider_id}/{deployment.model_id}"
        await self.service._deps.slots.acquire_call()
        try:
            outcome = await self.service._deps.runtime.run(RuntimeRequest(
                research_id=self.job["research_id"], project_key=self.job["project_key"],
                owner_id=self.job["owner_id"], prompt=turn.prompt,
                workspace=self.service._workspace_for(self.job), model=model,
                session_id=self.runtime_session if self.runtime_model == model else None,
                allow_session_resume=False, timeout_seconds=self._runtime_timeout(),
                tool_credential=self.credential, provider=deployment.provider, provider_config=provider_config))
        except (RuntimeCancelled, RuntimeConfigurationError, RuntimeUnavailable, RuntimeOutputTooLarge) as exc:
            if exc.outcome is not None:
                self.session.checkpoint_failure(self._attempt(exc.outcome))
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, exc.__class__.__name__) from exc
        except Exception as exc:  # noqa: BLE001 - substitute runtime; session classifies failures
            self.runtime_session = None
            outcome = getattr(exc, "outcome", None)
            attempt = self._attempt(outcome) if outcome is not None else wiring.ProviderTurn("", False)
            return replace(attempt, complete=False,
                           error_text="timeout" if isinstance(exc, RuntimeTimeout) else getattr(exc, "runtime_error", "") or str(exc),
                           failure_stage="partial_text" if attempt.text else "after_tool" if attempt.executed_tools else "before_response")
        finally:
            await self.service._deps.slots.release_call()
            self._collect_broker_reads()
        if self.job["research_id"] in self.service._cancelled:
            self.session.checkpoint_failure(self._attempt(outcome))
            raise wiring.SessionBlocked(wiring.BLOCKED_PROVIDER, "RuntimeCancelled")
        self.runtime_session, self.runtime_model = outcome.session_id, model
        attempt = self._attempt(outcome)
        if not attempt.complete:
            self.runtime_session = None
        if attempt.complete and self.state["phase"] == "synthesis":
            from research.service import extract_report_payload

            if extract_report_payload(attempt.text) is None:
                return replace(attempt, text="", complete=False, error_text="empty response without usable text")
        return attempt

    def _runtime_timeout(self):
        return max(1, int(self.budget.remaining_seconds))

    def _attempt(self, outcome):
        events = list(outcome.events)
        if not any(e.get("type") == "text" for e in events):
            events.append({"type": "text", "part": {"text": outcome.text}})
        attempt = wiring.parse_managed_events(events)
        if outcome.timed_out or outcome.exit_code:
            return replace(attempt, complete=False, error_text="timeout" if outcome.timed_out else "runtime failed",
                           failure_stage="partial_text" if attempt.text else "before_response")
        if not attempt.text and not any(e.get("type") == "error" for e in events):
            return replace(attempt, complete=True)
        return attempt

    def _provider_config(self, entry, deployment):
        if deployment.stale:
            return None
        if deployment.route_type == "opencode_native_free":
            return {} if deployment.provider == "opencode" else None
        try:
            if not self.credentials.get(credential_env_for(deployment.provider)):
                return None
            catalog = CatalogDocument(entries=[entry.model_copy(update={"deployments": [deployment]})])
            return build_external_providers_config("v2", catalog)["providers"]
        except (ExternalError, ValueError):
            return None

    def _collect_broker_reads(self):
        if self.service._deps.broker_reads is None:
            return
        try:
            rows = self.service._deps.broker_reads.take(self.job["research_id"]) or {}
        except Exception:  # noqa: BLE001 - auxiliary evidence collection must not replay a model turn
            logger.warning("No se pudo recoger la evidencia del broker", exc_info=True)
            return
        for eid, row in rows.items():
            self.evidence.setdefault(eid, row)
            if row.get("remote_egress_unattested"):
                self.coverage.append(ResearchCoverageGap(source="firecrawl", reason=CoverageReason.SOURCE_UNAVAILABLE,
                    detail="La API remota no acredita su cadena de redirecciones/DNS; cobertura limitada"))

    async def _persist(self):
        self.state["runtime_session"] = self.runtime_session
        self.state["runtime_model"] = self.runtime_model
        self.state["steps_left"] = self.budget.steps_left
        self.state["evidence"] = {
            eid: {**{key: value for key, value in row.items() if key != "stored"},
                  "content_hash": row.get("content_hash") or getattr(row.get("stored"), "content_hash", None),
                  "expires_at": row.get("expires_at") or getattr(row.get("stored"), "expires_at", None)}
            for eid, row in self.evidence.items()}
        await self.service._deps.repo.patch_research_job(self.job["research_id"], {"free_research": self.state})
