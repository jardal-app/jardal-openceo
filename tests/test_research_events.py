import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from pathlib import Path
from datetime import datetime, timedelta, timezone

import research_support as support
from research.contracts import (ResearchJobResult, ResearchJobStatus, ResearchClaim, ClaimKind,
    Classification, ResearchEvidence, AccessMethod, VerificationVerdict, EvidenceSupport)
from research.events import ResearchEvents, EventError
from research.export import export_conclusion, ExportError
from research.evidence import EvidenceVault, EvidenceScope
from research.wiring import ProjectEvidence


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class EventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_events")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_events")
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = EvidenceVault(root=self.tmp.name)
        self.evidence = ProjectEvidence(self.vault)
        self.now = datetime.now(timezone.utc)
        self.events = ResearchEvents(self.repo.db, self.evidence, clock=lambda: self.now)
        await self.events.initialize()
        self.job = support.job_doc(owner_id="alice", status="completed")
        ev = ResearchEvidence(id=self.job["research_id"] + "-e1", url="https://example.org", access_method=AccessMethod.HTTP,
            project_key="jardal-openceo", owner_id="alice", observed_at=self.now)
        await self.evidence.asave(ev, content="Reusable fact", expires_at=self.now + timedelta(hours=1))
        self.job["result"] = ResearchJobResult(research_id=self.job["research_id"], status=ResearchJobStatus.COMPLETED,
            project_key="jardal-openceo", classification=Classification.PUBLIC, evidence=[ev],
            claims=[ResearchClaim(id="c1", statement="Reusable fact", kind=ClaimKind.FACT, evidence_ids=[ev.id],
                                 support=[EvidenceSupport(extract="Reusable fact")], verification=VerificationVerdict.SUPPORTED)]).model_dump(mode="json")
    async def asyncTearDown(self):
        self.vault.close()
        self.tmp.cleanup()
        await self.repo.db.client.drop_database("openceo_test_events")
        await self.repo.close()
    async def test_publish_replay_scope_and_cursor(self):
        first = await self.events.publish(self.job)
        self.assertEqual(first, await self.events.publish(self.job))
        self.assertEqual(await self.events.list(project="jardal-openceo", owner="bob"), [])
        self.assertEqual(await self.events.list(project="synthetic_other", owner="alice"), [])
        rows = await self.events.list(project="jardal-openceo", owner="alice")
        self.assertEqual(rows[0]["claims"][0]["verification"], "supported")
        self.assertEqual(await self.events.list(project="jardal-openceo", owner="alice", after=rows[0]["cursor"]), [])

    async def test_outbox_survives_crash_and_later_publication_without_skipping_cursor(self):
        with patch.object(self.events, "_drain", new=AsyncMock(side_effect=RuntimeError("crash before insert"))):
            with self.assertRaises(RuntimeError):
                await self.events.publish(self.job)
        self.assertEqual(await self.events.events.count_documents({}), 0)
        next_job = deepcopy(self.job)
        next_job["research_id"] = next_job["result"]["research_id"] = "res_after_crash"
        restarted = ResearchEvents(self.repo.db, self.evidence, clock=lambda: self.now)
        await restarted.publish(next_job)
        rows = await restarted.list(project="jardal-openceo", owner="alice")
        self.assertEqual([r["cursor"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["research_id"], self.job["research_id"])
        self.assertEqual(await restarted.list(project="jardal-openceo", owner="alice", after=2), [])
        await restarted.publish(self.job)
        self.assertEqual(await self.events.events.count_documents({}), 2)

    async def test_verification_history_cannot_change_without_new_version(self):
        await self.events.publish(self.job)
        self.job["deep_execution"] = {"verification_history": [{"verdict": "contradicted"}]}
        with self.assertRaisesRegex(EventError, "version_conflict"):
            await self.events.publish(self.job)
    async def test_same_version_changed_payload_conflicts_and_correction_retained(self):
        first = await self.events.publish(self.job)
        self.job["result"]["summary"] = "Correction"
        with self.assertRaises(EventError):
            await self.events.publish(self.job)
        second = await self.events.publish(self.job, version=2, corrects=first)
        self.assertNotEqual(first, second)
        rows = await self.events.list(project="jardal-openceo", owner="alice")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["corrects_event_id"], first)
    async def test_expired_evidence_downgrades_supported_claim(self):
        eid = await self.events.publish(self.job)
        self.vault.project("jardal-openceo").expire_due(now=self.now + timedelta(hours=2))
        view = await self.events.get(eid, project="jardal-openceo", owner="alice")
        self.assertEqual(view["status"], "partial")
        self.assertEqual(view["claims"][0]["verification"], "insufficient_evidence")

    async def test_freshness_recomputed_on_read_without_mutating_history(self):
        self.job["freshness"] = {"max_age_hours": 1}
        eid = await self.events.publish(self.job)
        self.now += timedelta(hours=2)
        view = await self.events.get(eid, project="jardal-openceo", owner="alice")
        self.assertEqual(view["freshness"], "stale")
        stored = await self.events.events.find_one({"event_id": eid})
        self.assertEqual(stored["freshness"], "fresh")
    async def test_foreign_evidence_rejected_before_persistence(self):
        self.job["result"]["evidence"][0]["owner_id"] = "bob"
        with self.assertRaises(EventError):
            await self.events.publish(self.job)
        self.assertEqual(await self.events.events.count_documents({}), 0)
    async def test_social_is_only_declared_and_private_hidden(self):
        self.job["result"]["evidence"][0]["source_type"] = "social"
        eid = await self.events.publish(self.job)
        view = await self.events.get(eid, project="jardal-openceo", owner="alice")
        self.assertEqual(view["claims"][0]["kind"], "declaration")
        self.job["research_id"] = "new"
        self.job["result"]["research_id"] = "new"
        self.job["classification"] = self.job["result"]["classification"] = "restricted"
        restricted = await self.events.publish(self.job)
        self.assertIsNone(await self.events.get(restricted, project="jardal-openceo", owner="alice"))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "projects/jardal/jardal-openceo/research").mkdir(parents=True)
        self.kw = dict(event={"project_key": "jardal-openceo", "owner_id": "alice", "classification": "public", "status": "completed",
            "freshness": "fresh", "observed_at": "2026-10-02T00:00:00Z", "event_id": "re_test", "research_id": "res_test",
            "claims": [{"id": "c1", "statement": "Reusable architecture conclusion", "verification": "supported", "kind": "fact"}]},
            project_key="jardal-openceo", owner_id="alice", mappings={"jardal-openceo": "projects/jardal/jardal-openceo"},
            vault_root=self.root, slug="synthetic-conclusion", claim_ids=["c1"], authorized=True, reviewed_durable_public=True)
    def tearDown(self):
        self.tmp.cleanup()
    def test_only_selected_public_conclusion_and_no_overwrite(self):
        result = export_conclusion(**self.kw)
        body = (self.root / result["note"]).read_text()
        self.assertIn("Reusable architecture conclusion", body)
        with self.assertRaises(ExportError):
            export_conclusion(**self.kw)
    def test_scope_authorization_mapping_traversal(self):
        for overrides in ({"authorized": False}, {"owner_id": "bob"}, {"mappings": {}},
            {"mappings": {"jardal-openceo": "shared/research"}}, {"slug": "../escape"}):
            with self.subTest(overrides=overrides), self.assertRaises(ExportError):
                export_conclusion(**{**self.kw, **overrides})
    def test_symlink_rejected_and_secret_not_written(self):
        directory = self.root / "projects/jardal/jardal-openceo/research"
        directory.rmdir()
        directory.symlink_to(self.root)
        with self.assertRaises(ExportError):
            export_conclusion(**self.kw)
        directory.unlink()
        directory.mkdir()
        self.kw["event"]["claims"][0]["statement"] = "auth_token=SECRET"
        with self.assertRaises(ExportError):
            export_conclusion(**self.kw)
