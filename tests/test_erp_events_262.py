"""ERP event bridge (TASK-2026-00262): inbox, decisions, adoption, outbox.

Mocks and synthetic fixtures only; no live models, no network, no real data.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from business.approvals import JardalERPApproval
from business.errors import BusinessPermissionError, BusinessValidationError
from business.events import (
    MemoryEventStore,
    build_outbound_event,
    classify_delivery_error,
    decide_inbound,
    deliver_outbound,
    drain_outbox,
    replay_outbox_entry,
    validate_inbound_envelope,
)
from business.http import JardalERPClient
from business.webhooks import require_erp_webhook
from state import TaskStatus


def settings(**changes):
    base = dict(
        JARDAL_ERP_URL="http://127.0.0.1:31874",
        JARDAL_ERP_SERVICE_TOKEN="synthetic-service-token",
        JARDAL_ERP_WORKSPACE_ID="synthetic-workspace",
        JARDAL_ERP_WEBHOOK_SECRET="synthetic-webhook-secret",
        JARDAL_ERP_OPENCEO_USER="openceo@jardal.app",
        ROLLUP_PROVIDER="core",
        ERPNEXT_TIMEOUT_SECONDS=1,
        ERPNEXT_CONNECT_TIMEOUT_SECONDS=1,
    )
    base.update(changes)
    return SimpleNamespace(**base)


def envelope(**changes):
    base = {
        "event_id": "evt-001",
        "schema_version": 1,
        "type": "approval.decision",
        "origin": "jardal-erp",
        "workspace_id": "synthetic-workspace",
        "aggregate_id": "TASK-2026-99999",
        "aggregate_version": 7,
        "occurred_at": "2026-10-14T12:00:00Z",
        "correlation_id": "corr-1",
        "causation_id": "cause-1",
        "actor": {"kind": "human", "id": "user-1"},
        "payload": {},
    }
    base.update(changes)
    return base


class FakeRepo:
    def __init__(self):
        self.tasks = {}
        self.ticket_number = 100
        self.blocked_run = None

    async def get_task(self, task_id):
        row = self.tasks.get(str(task_id))
        return dict(row) if row else None

    async def patch_task(self, task_id, updates):
        row = self.tasks.get(str(task_id))
        if row is None:
            return None
        row.update(dict(updates))
        return dict(row)

    async def find_task_by_source(self, source, source_id):
        for row in self.tasks.values():
            if str(row.get("source") or "") == str(source) and str(row.get("source_id") or "") == str(source_id):
                return dict(row)
        return None

    async def find_task_by_business_name(self, name):
        for row in self.tasks.values():
            mirrors = (row.get("params") or {}).get("_mirrors") or {}
            business = mirrors.get("business") or {}
            if business.get("name") == name:
                return dict(row)
        return None

    async def get_next_ticket_number(self):
        self.ticket_number += 1
        return self.ticket_number

    async def create_task(self, doc):
        row = dict(doc)
        # model_dump(mode="python") keeps enums (str subclasses); normalize.
        for key in ("status", "source"):
            value = row.get(key)
            if value is not None and type(value) is not str:
                row[key] = getattr(value, "value", str(value))
        self.tasks[row["task_id"]] = row
        return dict(row)

    async def latest_workflow_run_for_task(self, task_id):
        return dict(self.blocked_run) if self.blocked_run else None


def linked_task(**changes):
    base = {
        "task_id": "exec-1",
        "status": TaskStatus.IN_PROGRESS.value,
        "project": "contract-test",
        "ticket_number": 101,
        "params": {"_mirrors": {"business": {"provider": "jardal_erp", "name": "TASK-2026-99999"}}},
    }
    base.update(changes)
    return base


def make_service(repo, adapter=None, **sets):
    from task_service import TaskService

    service = TaskService.__new__(TaskService)
    service._repo = repo
    service._settings = settings()
    service._erpnext = adapter
    service._business = adapter
    service._event_store = MemoryEventStore()
    service._workflow_service = None
    service._hub = SimpleNamespace(broadcast=AsyncMock())
    service._dispatcher_wake = None
    for key, value in sets.items():
        setattr(service, key, value)
    return service


def test_envelope_validation_accepts_rejects_and_ignores():
    event = validate_inbound_envelope(envelope(), settings=settings())
    assert event["event_id"] == "evt-001"
    assert event["aggregate_version"] == 7
    assert event["actor"] == {"kind": "human", "id": "user-1"}
    assert validate_inbound_envelope(envelope(type="nope.unknown"), settings=settings())["ignored"] == "unknown_type"
    for bad in [envelope(origin="evil"), envelope(workspace_id="other"),
                envelope(schema_version=2), envelope(event_id=""),
                envelope(aggregate_version=-1), envelope(occurred_at="ayer"),
                envelope(actor={"kind": "human"}), envelope(payload=[]),
                envelope(type=""), "not-a-dict"]:
        with pytest.raises((BusinessValidationError, BusinessPermissionError)):
            validate_inbound_envelope(bad, settings=settings())


def test_inbox_routing_duplicate_and_stale():
    event = validate_inbound_envelope(envelope(), settings=settings())
    assert decide_inbound(None, None, event) == "process"
    assert decide_inbound({"status": "processed"}, 7, event) == "duplicate"
    assert decide_inbound(None, 7, event) == "stale"
    assert decide_inbound(None, 9, event) == "stale"
    assert decide_inbound(None, 6, event) == "process"


def test_claim_and_cursor_are_race_safe():
    async def run():
        store = MemoryEventStore()
        assert await store.claim_inbox("e1", {"status": "processing"}) is True
        assert await store.claim_inbox("e1", {"status": "processing"}) is False
        await store.put_inbox("e1", {"status": "failed"})
        assert await store.claim_inbox("e1", {"status": "processing"}) is True
        await store.put_cursor("agg", 5)
        await store.put_cursor("agg", 3)
        assert await store.get_cursor("agg") == 5
        await store.put_cursor("agg", 6)
        assert await store.get_cursor("agg") == 6
    asyncio.run(run())


def test_decision_applies_exactly_once():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task(status=TaskStatus.WAITING.value)
        adapter = SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None)
        service = make_service(repo, adapter)
        await service.register_erp_approval("exec-1", {"id": "apr-1", "version": 2})
        service.apply_owner_decision = AsyncMock(return_value={"applied": True, "route": "phase_action"})
        event = validate_inbound_envelope(envelope(payload={
            "approval_id": "apr-1", "decision_id": "dec-1", "verdict": "approved",
            "version": 2, "execution_task_id": "exec-1", "comment": "ok"}), settings=settings())
        first = await service.apply_erp_event(event)
        assert first["status"] == "applied"
        assert service.apply_owner_decision.await_count == 1
        kwargs = service.apply_owner_decision.await_args.kwargs
        assert kwargs["approve"] is True and kwargs["actor_id"] == "erp:user-1"
        second = await service.apply_erp_event(event)
        assert second["status"] == "duplicate"
        assert service.apply_owner_decision.await_count == 1
        stored = await repo.get_task("exec-1")
        assert stored["params"]["_erp_decisions"]["dec-1"]["verdict"] == "approved"
    asyncio.run(run())


def test_decision_rejects_expired_superseded_and_invalid():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task(status=TaskStatus.WAITING.value)
        service = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None))
        await service.register_erp_approval("exec-1", {"id": "apr-1", "version": 3})
        service.apply_owner_decision = AsyncMock(return_value={"applied": True})
        base = {"approval_id": "apr-1", "execution_task_id": "exec-1"}
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        cases = [
            (dict(base, decision_id="d-exp", verdict="approved", expires_at=expired_at), "expired"),
            (dict(base, decision_id="d-sup", verdict="approved", version=1), "superseded"),
            (dict(base, decision_id="d-bad", verdict="maybe"), "invalid_decision"),
            (dict(base, decision_id="d-no", verdict="approved", execution_task_id="exec-x"), "not_found"),
        ]
        for payload, expected in cases:
            if expected == "not_found":
                payload = dict(payload, business_reference="TASK-2026-00000")
            event = validate_inbound_envelope(envelope(payload=payload), settings=settings())
            assert (await service.apply_erp_event(event))["status"] == expected
        assert service.apply_owner_decision.await_count == 0
        erp_side = validate_inbound_envelope(envelope(payload=dict(
            base, decision_id="d-erp", verdict="approved", approval_id="apr-erp")), settings=settings())
        assert (await service.apply_erp_event(erp_side))["status"] == "applied"
        service.apply_owner_decision = AsyncMock(return_value={"applied": False, "reason": "not_answerable"})
        pending = validate_inbound_envelope(envelope(payload=dict(
            base, decision_id="d-pend", verdict="approved", version=3)), settings=settings())
        result = await service.apply_erp_event(pending)
        assert result["status"] == "not_answerable" and result["pending"] is True
    asyncio.run(run())


def test_concurrent_same_decision_applies_once():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task(status=TaskStatus.WAITING.value)
        service = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None))
        service.apply_owner_decision = AsyncMock(return_value={"applied": True, "route": "phase_action"})

        def decision_event(event_id):
            return validate_inbound_envelope(envelope(
                event_id=event_id, payload={"approval_id": "apr-1", "decision_id": "dec-race",
                                            "verdict": "approved", "execution_task_id": "exec-1"}),
                settings=settings())

        first, second = await asyncio.gather(
            service.apply_erp_event(decision_event("evt-a")),
            service.apply_erp_event(decision_event("evt-b")))
        assert sorted([first["status"], second["status"]]) == ["applied", "duplicate"]
        assert service.apply_owner_decision.await_count == 1
    asyncio.run(run())


def test_failed_inbox_replays_and_stuck_resets():
    async def run():
        from business.events import reset_stuck_inbox

        store = MemoryEventStore()
        event = validate_inbound_envelope(envelope(), settings=settings())
        assert decide_inbound({"status": "failed"}, None, event) == "process"
        assert await store.claim_inbox("evt-001", {"status": "processing"}) is True
        await store.put_inbox("evt-001", {"status": "failed", "error": "boom"})
        assert decide_inbound(await store.get_inbox("evt-001"), None, event) == "process"
        assert await store.claim_inbox("evt-001", {"status": "processing"}) is True
        await store.put_inbox("evt-001", {"status": "processed"})
        await store.put_inbox("evt-stuck", {"status": "processing"})
        await store.put_inbox("evt-done", {"status": "processed"})
        assert await reset_stuck_inbox(store) == {"reset": 1}
        assert (await store.get_inbox("evt-stuck"))["status"] == "failed"
        assert (await store.get_inbox("evt-done"))["status"] == "processed"
    asyncio.run(run())


def test_close_respects_gates_and_delegates_otherwise():
    async def run():
        repo = FakeRepo()
        gated = linked_task(task_id="exec-g", status=TaskStatus.WAITING.value,
                            qa_review_required=True, qa_review_state="pending")
        gated["params"]["_mirrors"]["business"]["name"] = "TASK-2026-00001"
        repo.tasks["exec-g"] = gated
        clear = linked_task(task_id="exec-c", status=TaskStatus.IN_PROGRESS.value)
        clear["params"]["_mirrors"]["business"]["name"] = "TASK-2026-00002"
        repo.tasks["exec-c"] = clear
        blocked = linked_task(task_id="exec-b", status=TaskStatus.IN_PROGRESS.value, blocked_by=["exec-c"])
        blocked["params"]["_mirrors"]["business"]["name"] = "TASK-2026-00003"
        repo.tasks["exec-b"] = blocked
        service = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None))
        service.apply_business_status = AsyncMock(return_value={"ok": True, "status": "applied"})

        def status_event(name):
            return validate_inbound_envelope(envelope(
                type="task.status_changed", aggregate_id=name,
                payload={"business_reference": name, "business_status": "Completed"}),
                settings=settings())

        qa = await service.apply_erp_event(status_event("TASK-2026-00001"))
        assert qa["status"] == "gated" and qa["gate"] == "qa_pending"
        dep = await service.apply_erp_event(status_event("TASK-2026-00003"))
        assert dep["status"] == "gated" and dep["gate"] == "dependency_open"
        still = await repo.get_task("exec-g")
        assert still["status"] == TaskStatus.WAITING.value
        assert still["params"]["_mirrors"]["business"]["business_status"] == "Completed"
        ok = await service.apply_erp_event(status_event("TASK-2026-00002"))
        assert ok["status"] == "applied"
        call = service.apply_business_status.await_args
        assert call.kwargs["source_provider"] == "jardal_erp"
        assert call.kwargs["business_status"] == "Completed"
        cancelled = validate_inbound_envelope(envelope(
            type="task.status_changed", aggregate_id="TASK-2026-00001",
            payload={"business_reference": "TASK-2026-00001", "business_status": "Cancelled"}),
            settings=settings())
        assert (await service.apply_erp_event(cancelled))["status"] == "applied"
    asyncio.run(run())


def test_adoption_only_when_assigned_to_openceo():
    async def run():
        repo = FakeRepo()
        service = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None))

        def order_event(order_id, assigned):
            return validate_inbound_envelope(envelope(
                type="execution.requested", aggregate_id="TASK-2026-99999",
                payload={"order_id": order_id, "business_reference": "TASK-2026-99999",
                         "assigned_to": assigned, "title": "Synthetic order"}),
                settings=settings())

        assert (await service.apply_erp_event(order_event("o-1", [])))["status"] == "ignored"
        assert (await service.apply_erp_event(order_event("o-1", ["human@x.test"])))["status"] == "ignored"
        assert repo.tasks == {}
        adopted = await service.apply_erp_event(order_event("o-2", ["openceo@jardal.app"]))
        assert adopted["status"] == "adopted"
        task = await repo.get_task(adopted["task_id"])
        assert task["source_id"] == "jardal-erp:execution:o-2"
        assert task["autorun"] is True
        mirror = task["params"]["_mirrors"]["business"]
        assert mirror["provider"] == "jardal_erp" and mirror["name"] == "TASK-2026-99999"
        assert mirror["status_pushed"] == TaskStatus.QUEUED.value
        again = await service.apply_erp_event(order_event("o-2", ["openceo@jardal.app"]))
        assert again["status"] == "already_adopted" and again["task_id"] == adopted["task_id"]
        assert len(repo.tasks) == 1
        accepted = [row for row in service._event_store.outbox.values() if row["type"] == "execution.accepted"]
        assert len(accepted) == 1 and accepted[0]["state"] == "failed"
    asyncio.run(run())


def test_progress_observed_until_rollup_cutover():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task()
        service = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None))

        def progress_event(value):
            return validate_inbound_envelope(envelope(
                type="task.progress_changed", aggregate_id="TASK-2026-99999",
                payload={"business_reference": "TASK-2026-99999", "progress": value}),
                settings=settings())

        observed = await service.apply_erp_event(progress_event(33.333))
        assert observed["status"] == "observed"
        mirror = (await repo.get_task("exec-1"))["params"]["_mirrors"]["business"]
        assert mirror["observed_business_progress"] == 33.33
        assert "business_progress" not in mirror
        service._settings.ROLLUP_PROVIDER = "erp"
        recorded = await service.apply_erp_event(progress_event(50))
        assert recorded["status"] == "recorded"
        mirror = (await repo.get_task("exec-1"))["params"]["_mirrors"]["business"]
        assert mirror["business_progress"] == 50
        assert (await service.apply_erp_event(progress_event(101)))["status"] == "invalid_progress"
    asyncio.run(run())


def test_outbox_delivery_retry_and_directed_replay():
    async def run():
        entry = build_outbound_event(type="execution.finished", aggregate_id="TASK-2026-99999",
                                     aggregate_version=9, payload={"status": "completed"},
                                     business_reference="TASK-2026-99999")
        assert entry["state"] == "pending" and entry["origin"] == "openceo-core"
        with pytest.raises(BusinessValidationError):
            build_outbound_event(type="nope", aggregate_id="a", aggregate_version=0, payload={})

        requests = []

        def handle_ok(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True})

        cfg = settings()
        client = JardalERPClient(cfg, transport=httpx.MockTransport(handle_ok))
        try:
            assert (await deliver_outbound(client, entry))["delivered"] is True
            assert requests[0].url.path == "/api/v1/integrations/openceo/events"
            assert requests[0].headers["Idempotency-Key"] == entry["event_id"]
        finally:
            await client.aclose()

        calls = []

        def handle_flaky(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503, json={"code": "BUSY"})
            return httpx.Response(200, json={"ok": True})

        store = MemoryEventStore()
        await store.put_outbox(dict(entry, state="pending", attempts=0))
        flaky = JardalERPClient(cfg, transport=httpx.MockTransport(handle_flaky))
        try:
            assert await drain_outbox(store, flaky, 10) == {"sent": 1, "retried": 0, "failed": 0}
            assert len(calls) == 3
            assert (await store.get_outbox(entry["event_id"]))["state"] == "sent"
            assert (await replay_outbox_entry(store, flaky, entry["event_id"]))["reason"] == "already_sent"
        finally:
            await flaky.aclose()

        def handle_down(request):
            return httpx.Response(503, json={"code": "BUSY"})

        down = JardalERPClient(cfg, transport=httpx.MockTransport(handle_down))
        try:
            await store.put_outbox(dict(entry, event_id="evt-retry", state="pending", attempts=0))
            assert await drain_outbox(store, down, 10) == {"sent": 0, "retried": 1, "failed": 0}
            assert (await store.get_outbox("evt-retry"))["state"] == "retry"
        finally:
            await down.aclose()

        def handle_hard(request):
            return httpx.Response(422, json={"code": "BAD"})

        store = MemoryEventStore()
        hard = JardalERPClient(cfg, transport=httpx.MockTransport(handle_hard))
        try:
            await store.put_outbox(dict(entry, event_id="evt-hard", state="pending", attempts=0))
            assert await drain_outbox(store, hard, 10) == {"sent": 0, "retried": 0, "failed": 1}
            assert (await replay_outbox_entry(store, hard, "evt-hard"))["outcome"] == "fail"
            assert (await replay_outbox_entry(store, hard, "missing"))["reason"] == "not_found"
            await store.put_outbox(dict(entry, event_id="evt-void", state="void", attempts=0))
            assert (await replay_outbox_entry(store, hard, "evt-void"))["reason"] == "void"
        finally:
            await hard.aclose()
    asyncio.run(run())


def test_delivery_classifier_and_webhook_guard():
    from business.errors import BusinessUnavailable

    assert classify_delivery_error(BusinessUnavailable("x")) == "retry"
    assert classify_delivery_error(BusinessValidationError("x", status_code=500)) == "retry"
    assert classify_delivery_error(BusinessValidationError("x", status_code=422)) == "fail"
    assert classify_delivery_error(BusinessPermissionError("x", status_code=403)) == "fail"
    assert classify_delivery_error(RuntimeError("x")) == "fail"

    from fastapi import HTTPException

    require_erp_webhook(settings(), "jardal_erp", "synthetic-webhook-secret")
    with pytest.raises(HTTPException) as exc:
        require_erp_webhook(settings(JARDAL_ERP_WEBHOOK_SECRET=""), "jardal_erp", "s")
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        require_erp_webhook(settings(), "jardal_erp", "wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        require_erp_webhook(settings(), "erpnext", "synthetic-webhook-secret")
    assert exc.value.status_code == 409


def test_approvals_port_posts_and_gets():
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"approval": {"id": "apr-9", "status": "pending"}})
            return httpx.Response(200, json={"id": "apr-9", "status": "decided",
                                             "decision": {"verdict": "approved"}})

        cfg = settings()
        adapter = JardalERPApproval(cfg, transport=httpx.MockTransport(handle))
        try:
            assert adapter.is_enabled is True
            created = await adapter.request_approval({"resource": "TASK-2026-99999"}, key="k-1")
            assert created["id"] == "apr-9"
            assert requests[0].url.path == "/api/v1/approvals"
            assert requests[0].headers["Idempotency-Key"] == "k-1"
            seen = await adapter.get_approval("apr-9")
            assert seen["decision"] == {"verdict": "approved"}
            with pytest.raises(BusinessValidationError):
                await adapter.request_approval({}, key="k-2")
            with pytest.raises(BusinessValidationError):
                await adapter.get_approval("")
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_publish_mapping_and_provider_gating():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task()
        service = make_service(repo, SimpleNamespace(provider="erpnext", is_enabled=True, client=None))
        assert service._push_event_type("in_progress", {}) == "execution.started"
        assert service._push_event_type("completed", {}) == "execution.finished"
        assert service._push_event_type("failed", {}) == "execution.finished"
        assert service._push_event_type("queued", {}) is None
        assert service._push_event_type("waiting", {}) == "execution.paused"
        assert await service._publish_execution_event("execution.started", await repo.get_task("exec-1")) is None
        assert await service.drain_erp_outbox_startup() == {"sent": 0, "retried": 0, "failed": 0, "skipped": True}
        assert (await service.replay_erp_outbox_entry("x"))["reason"] == "provider_inactive"

        def handle(request):
            return httpx.Response(200, json={"ok": True})

        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            live = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=client))
            entry = await live._publish_execution_event("execution.started", await repo.get_task("exec-1"))
            assert (await live.event_store.get_outbox(entry["event_id"]))["state"] == "sent"
            assert entry["payload"]["business_reference"] == "TASK-2026-99999"
            assert entry["aggregate_id"] == "TASK-2026-99999"
        finally:
            await client.aclose()
    asyncio.run(run())


def test_outbound_envelope_carries_contract_routing():
    async def run():
        repo = FakeRepo()
        repo.tasks["exec-1"] = linked_task()
        seen: dict[str, Any] = {}

        def handle(request):
            seen.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})

        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            live = make_service(repo, SimpleNamespace(provider="jardal_erp", is_enabled=True, client=client))
            entry = await live._publish_execution_event("execution.started", await repo.get_task("exec-1"))
            assert entry["workspace_id"] == "synthetic-workspace"
            assert entry["project_key"] == "contract-test"
            assert entry["actor"] == {"kind": "agent", "id": "openceo-runtime"}
            assert seen["workspace_id"] == "synthetic-workspace"
            assert seen["project_key"] == "contract-test"
            assert seen["actor"] == {"kind": "agent", "id": "openceo-runtime"}
        finally:
            await client.aclose()
    asyncio.run(run())
