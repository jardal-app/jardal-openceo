"""Persisted, scoped replayable events. Expired evidence downgrades the read view.

An event id or cursor never grants authority. No broadcast, financial instructions,
external consumer integration, or automatic vault export occurs here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from research.contracts import (Classification, ResearchEvent, ResearchJobResult, ResearchJobStatus,
                                VerificationVerdict, freshness_state)


class EventError(RuntimeError):
    pass


class ResearchEvents:
    def __init__(self, db, evidence, *, clock=None, retention_days=180):
        self.events, self.cursors = db.research_events, db.research_event_cursors
        self.evidence = evidence
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retention_days = max(0, int(retention_days))

    async def initialize(self):
        await self.events.create_index("event_id", unique=True)
        await self.events.create_index([("project_key", 1), ("owner_id", 1), ("cursor", 1)])
        await self.events.create_index("expires_at", expireAfterSeconds=0)

    async def publish(self, job, *, version=1, corrects=None):
        result = ResearchJobResult.model_validate(job["result"])
        if (result.project_key != job["project_key"] or result.research_id != job["research_id"]
                or result.classification.value != job["classification"]):
            raise EventError("scope_mismatch")
        if any(e.project_key != job["project_key"] or e.owner_id != job["owner_id"]
               or e.visibility.severity > result.classification.severity for e in result.evidence):
            raise EventError("scope_mismatch")
        if any(e.source_type.value == "social" for e in result.evidence):
            for claim in result.claims:
                if claim.kind.value == "fact":
                    from research.contracts import ClaimKind
                    claim.kind = ClaimKind.DECLARATION
        if corrects:
            previous = await self.events.find_one({"event_id": corrects, "project_key": job["project_key"],
                                                  "owner_id": job["owner_id"], "expires_at": {"$gt": self.clock()}})
            if previous is None or version <= previous["version"]:
                raise EventError("invalid_correction")
        identity = {"research_id": job["research_id"], "project": job["project_key"], "owner": job["owner_id"], "version": version}
        event_id = "re_" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps({"result": job["result"], "corrects": corrects,
            "verification_history": (job.get("deep_execution") or {}).get("verification_history", [])},
            sort_keys=True, default=str).encode()).hexdigest()
        existing = await self.events.find_one({"event_id": event_id})
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise EventError("version_conflict")
            return event_id
        scope_key = hashlib.sha256(json.dumps([job["project_key"], job["owner_id"]]).encode()).hexdigest()
        event = ResearchEvent(event_id=event_id, research_id=job["research_id"], project_key=job["project_key"],
            owner_id=job["owner_id"], classification=result.classification, entity=job["purpose"],
            observed_at=self.clock(), version=version, corrects_event_id=corrects, status=result.status,
            claims=result.claims, evidence=result.evidence, coverage_gaps=result.coverage_gaps,
            verification_history=(job.get("deep_execution") or {}).get("verification_history", []),
            freshness=freshness_state(max((e.observed_at for e in result.evidence if e.observed_at), default=None),
                                      max_age_hours=(job.get("freshness") or {}).get("max_age_hours"), now=self.clock()))
        doc = {**event.model_dump(mode="json"), "fingerprint": fingerprint,
               "freshness_max_age_hours": (job.get("freshness") or {}).get("max_age_hours"),
               "expires_at": self.clock() + timedelta(days=self.retention_days)}
        # The scope's atomic outbox assigns a cursor AND stores its payload together.
        # Readers drain this scoped outbox before advancing: insertion order or a
        # crash between counter/insert cannot make a lower cursor disappear forever.
        try:
            sequence = await self.cursors.find_one_and_update(
                {"_id": scope_key, "pending.event_id": {"$ne": event_id}},
                [{"$set": {"sequence": {"$add": [{"$ifNull": ["$sequence", 0]}, 1]}}},
                 {"$set": {"pending": {"$concatArrays": [{"$ifNull": ["$pending", []]},
                    [{"$mergeObjects": [{"$literal": doc}, {"cursor": "$sequence"}]}]]}}}],
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            sequence = await self.cursors.find_one({"_id": scope_key})
        pending = next((x for x in sequence.get("pending", []) if x["event_id"] == event_id), None)
        if pending and pending["fingerprint"] != fingerprint:
            raise EventError("version_conflict")
        await self._drain(scope_key)
        return event_id

    async def _drain(self, scope_key):
        outbox = await self.cursors.find_one({"_id": scope_key}) or {}
        for pending in outbox.get("pending", []):
            try:
                await self.events.insert_one(pending)
            except DuplicateKeyError:
                existing = await self.events.find_one({"event_id": pending["event_id"]})
                if existing["fingerprint"] != pending["fingerprint"]:
                    await self.cursors.update_one({"_id": scope_key}, {"$pull": {"pending": {"event_id": pending["event_id"]}}})
                    raise EventError("version_conflict") from None
            await self.cursors.update_one({"_id": scope_key}, {"$pull": {"pending": {"event_id": pending["event_id"]}}})

    async def _view(self, row, *, project, owner, ceiling):
        payload = ResearchEvent.model_validate({k: v for k, v in row.items()
            if k not in {"_id", "cursor", "fingerprint", "expires_at", "freshness_max_age_hours"}}).model_dump(mode="json")
        from research.acquisition.base import parse_datetime
        payload["freshness"] = freshness_state(
            max((parse_datetime(e.get("observed_at")) for e in payload["evidence"] if e.get("observed_at")), default=None),
            max_age_hours=row.get("freshness_max_age_hours"), now=self.clock())
        missing = set()
        from research.evidence import EvidenceScope
        scope = EvidenceScope(project_key=project, owner_id=owner, max_classification=ceiling)
        for ev in payload["evidence"]:
            if (ev.get("project_key") != project or ev.get("owner_id") != owner
                    or self.evidence is None or await self.evidence.aget(scope, ev["id"]) is None):
                missing.add(ev["id"])
        if missing:
            for claim in payload["claims"]:
                if missing.intersection(claim["evidence_ids"]):
                    claim["verification"] = "insufficient_evidence"
            payload["status"] = "partial"
            payload["coverage_gaps"].append({"source": "evidence", "reason": "source_unavailable", "detail": "Evidencia caducada o no accesible"})
        payload["cursor"] = row["cursor"]
        return payload

    async def list(self, *, project, owner, ceiling=Classification.PUBLIC, after=0, limit=20):
        if after < 0:
            raise EventError("invalid_cursor")
        scope_key = hashlib.sha256(json.dumps([project, owner]).encode()).hexdigest()
        await self._drain(scope_key)
        # A concurrent publication that appeared after drain remains below the
        # response's upper watermark. Newer cursors are delivered on the next read.
        outbox = await self.cursors.find_one({"_id": scope_key}) or {}
        upper = min((x["cursor"] for x in outbox.get("pending", [])), default=outbox.get("sequence", 0) + 1) - 1
        allowed = [x.value for x in Classification if x.severity <= ceiling.severity]
        rows = await self.events.find({"project_key": project, "owner_id": owner, "classification": {"$in": allowed},
                                      "cursor": {"$gt": after, "$lte": upper}, "expires_at": {"$gt": self.clock()}}).sort("cursor", 1).limit(max(1, min(limit, 100))).to_list(None)
        return [await self._view(row, project=project, owner=owner, ceiling=ceiling) for row in rows]

    async def get(self, event_id, *, project, owner, ceiling=Classification.PUBLIC):
        scope_key = hashlib.sha256(json.dumps([project, owner]).encode()).hexdigest()
        await self._drain(scope_key)
        allowed = [x.value for x in Classification if x.severity <= ceiling.severity]
        row = await self.events.find_one({"event_id": event_id, "project_key": project, "owner_id": owner,
                                          "classification": {"$in": allowed}, "expires_at": {"$gt": self.clock()}})
        if row is None:
            return None
        return await self._view(row, project=project, owner=owner, ceiling=ceiling)
