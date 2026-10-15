import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from business.errors import BusinessDependencyBlockedError, BusinessPermissionError, BusinessUnavailable, BusinessValidationError
from business.http import JardalERPClient
from business.links import business_link
from business.provider import build_business_work
from business.work import JardalERPWork, task_view
from business.webhooks import require_legacy_webhook
from business.migration import backfill_business_links
from objectives.engine import erp_group_name
from objectives.missions import _erp_group_name
from task_service import TaskService


def settings(**changes):
    return SimpleNamespace(BUSINESS_PROVIDER="jardal_erp", JARDAL_ERP_URL="http://127.0.0.1:31874",
        JARDAL_ERP_SERVICE_TOKEN="synthetic-service-token", JARDAL_ERP_WORKSPACE_ID="synthetic-workspace",
        ERPNEXT_TIMEOUT_SECONDS=1, ERPNEXT_CONNECT_TIMEOUT_SECONDS=1, **changes)


ROW = {"id": "00000000-0000-0000-0000-000000000001", "businessReference": "TASK-2026-99999",
       "title": "Synthetic integration", "projectKey": "contract-test", "status": "Open", "kind": "task",
       "version": 1, "execution": {"ref": "api:synthetic-integration", "autorun": False}}


def test_timeout_after_effect_reuses_same_operation_and_preserves_execution_boundary():
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ReadTimeout("synthetic timeout after commit", request=request)
            return httpx.Response(200, json={"task": ROW, "duplicate": True})

        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            result = await JardalERPWork(settings(), client=client).create_ticket(ref="api:synthetic-integration", title=ROW["title"], project_key="contract-test")
        finally:
            await client.aclose()
        assert result["duplicate"] is True
        assert result["task"]["name"] == "TASK-2026-99999"
        assert len(requests) == 2
        assert requests[0].content == requests[1].content
        assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"] == "api:synthetic-integration"
        assert b'"autorun":false' in requests[0].content
        assert requests[0].url.path == "/api/v1/tasks"
    asyncio.run(run())


@pytest.mark.parametrize("status,code,error", [(401, "UNAUTHORIZED", BusinessPermissionError), (403, "FORBIDDEN", BusinessPermissionError),
    (409, "IDEMPOTENCY_CONFLICT", BusinessValidationError), (409, "DEPENDENCY_OPEN", BusinessDependencyBlockedError),
    (422, "INVALID_TRANSITION", BusinessValidationError)])
def test_rejection_is_not_retried_or_converted_to_pending(status, code, error):
    async def run():
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(status, json={"code": code, "message": "untrusted private details"})
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(error) as caught:
                await client.request("POST", "/api/v1/tasks", body={}, key="synthetic")
            assert len(requests) == 1
            assert "private details" not in str(caught.value)
        finally:
            await client.aclose()
    asyncio.run(run())


def test_unavailable_does_not_fall_back_to_another_business_provider():
    async def run():
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(503, json={"code": "UNAVAILABLE"})
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(BusinessUnavailable):
                await JardalERPWork(settings(), client=client).create_ticket(ref="api:test", title="Synthetic", project_key="contract-test")
            assert len(requests) == 3
        finally:
            await client.aclose()
    asyncio.run(run())


def test_missing_configuration_and_unknown_provider_fail_closed():
    value = settings()
    value.BUSINESS_PROVIDER = "mongo"
    with pytest.raises(ValueError):
        build_business_work(value)
    value.BUSINESS_PROVIDER = "jardal_erp"
    value.JARDAL_ERP_SERVICE_TOKEN = ""
    with pytest.raises(ValueError):
        build_business_work(value)


def test_canonical_link_wins_and_objectives_and_missions_read_it():
    from ceo.service import CEOService
    from dispatcher import _erpnext_link_pending
    from horizon.cockpit import erp_mirror_name
    from orchestration.role_muse import badge_ticket_for_task
    from pilot_report import _ticket_row
    task = {"params": {"_mirrors": {"business": {"provider": "jardal_erp", "name": "TASK-2026-99999"},
                                     "erpnext": {"name": "TASK-2026-00001"}}}}
    assert business_link(task)["name"] == "TASK-2026-99999"
    assert erp_group_name(task) == _erp_group_name(task) == "TASK-2026-99999"
    assert business_link({"params": {"_mirrors": {"erpnext": {"name": "legacy"}}}})["name"] == "legacy"
    assert CEOService._erp_name(task) == erp_mirror_name(task) == badge_ticket_for_task(task) == "TASK-2026-99999"
    assert _ticket_row(task)["ref"] == "TASK-2026-99999"
    task["params"]["_mirrors"]["business"] = {"provider": "jardal_erp", "ref": "api:synthetic", "pending": True}
    assert _erpnext_link_pending(task)


def test_link_update_preserves_legacy_record_without_mutating_input():
    service = TaskService.__new__(TaskService)
    service._erpnext = SimpleNamespace(provider="jardal_erp")
    params = {"_mirrors": {"erpnext": {"name": "TASK-2026-00001"}}}
    merged = service._merge_erpnext_mirror_params(params, {"name": "TASK-2026-99999", "pending": False})
    assert params == {"_mirrors": {"erpnext": {"name": "TASK-2026-00001"}}}
    assert merged["_mirrors"]["business"]["provider"] == "jardal_erp"
    assert merged["_mirrors"]["erpnext"]["name"] == "TASK-2026-00001"


def test_retry_after_core_crash_recovers_original_execution_identity():
    async def run():
        registration = None
        def handle(request):
            nonlocal registration
            if request.method == "POST":
                body = json.loads(request.content)
                if registration is None:
                    registration = body
                    return httpx.Response(200, json={"task": {**ROW, "execution": body["execution"], "registration": body}})
                return httpx.Response(409, json={"code": "IDEMPOTENCY_CONFLICT"})
            return httpx.Response(200, json={"task": {**ROW, "execution": registration["execution"], "registration": registration}})
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        work = JardalERPWork(settings(), client=client)
        try:
            kwargs = dict(ref="api:synthetic", title="Synthetic integration", project_key="contract-test", task_id="original", ticket_number=1)
            await work.create_ticket(**kwargs)
            recovered = await work.create_ticket(**{**kwargs, "task_id": "new-after-crash", "ticket_number": 2})
            assert recovered["recovered"]
            assert recovered["task"]["openceo_task_id"] == "original"
            assert recovered["task"]["openceo_ticket_number"] == 1
            with pytest.raises(BusinessValidationError):
                await work.create_ticket(**{**kwargs, "title": "Unrelated work"})
        finally:
            await client.aclose()
    asyncio.run(run())


def test_protocol_disconnect_retries_without_changing_operation_identity():
    async def run():
        calls = []
        def handle(request):
            calls.append(request.headers["Idempotency-Key"])
            raise httpx.RemoteProtocolError("synthetic disconnect", request=request)
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(BusinessUnavailable):
                await client.request("POST", "/api/v1/tasks", body={}, key="unchanged")
            assert calls == ["unchanged"] * 3
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status,extra,target", [
    ("completed", {"last_response_summary": "Synthetic checks passed", "qa_review_required": False}, "Completed"),
    ("completed", {"qa_review_required": True, "qa_review_state": "pending"}, "Pending Review"),
    ("waiting", {"phase": "qa_review", "waiting_reason": "phase:qa_review"}, "Pending Review"),
])
def test_real_status_worker_supplies_erp_transition_evidence(status, extra, target):
    async def run():
        bodies = []
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json={"task": {**ROW, "status": "Working"}})
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"task": {**ROW, "status": target, "version": 2}})
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        service = TaskService.__new__(TaskService)
        service._erpnext = JardalERPWork(settings(), client=client)
        service._settings = SimpleNamespace(ERPNEXT_SITE_TIMEZONE="UTC")
        task = {"task_id": "synthetic", "status": status, "params": {"_mirrors": {"business": {"provider": "jardal_erp", "name": ROW["businessReference"]}}}, **extra}
        service._repo = SimpleNamespace(get_task=AsyncMock(return_value=task), patch_task=AsyncMock())
        service._enqueue_visibility = lambda *args: None
        try:
            await service._push_status({"task_id": "synthetic", "status": status, "name": ROW["businessReference"]})
            assert bodies[0]["toStatus"] == target
            assert bodies[0].get("evidence") if target == "Completed" else bodies[0].get("humanAction")
            written = service._repo.patch_task.call_args.args[1]["params"]["_mirrors"]["business"]
            assert written["status_pushed"] == status
        finally:
            await client.aclose()
    asyncio.run(run())


def test_automatic_transition_rereads_after_concurrent_date_update():
    async def run():
        writes = []
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json={"task": {**ROW, "version": 1 + len(writes), "status": "Working"}})
            writes.append(json.loads(request.content))
            if len(writes) == 1:
                return httpx.Response(409, json={"code": "VERSION_CONFLICT"})
            return httpx.Response(200, json={"task": {**ROW, "status": "Completed", "version": 3}})
        client = JardalERPClient(settings(), transport=httpx.MockTransport(handle))
        try:
            result = await JardalERPWork(settings(), client=client).update_status(ROW["businessReference"], "completed", task={"task_id": "synthetic", "qa_review_required": False})
            assert result["ticket"]["status"] == "Completed"
            assert [body["version"] for body in writes] == [1, 2]
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["queued", "cancelled"])
def test_worker_rejects_legacy_authority_for_every_execution_status(status):
    async def run():
        service = TaskService.__new__(TaskService)
        service._erpnext = SimpleNamespace(provider="jardal_erp", is_enabled=True, update_status=AsyncMock())
        service._repo = SimpleNamespace(get_task=AsyncMock(return_value={"params": {"_mirrors": {"erpnext": {"name": ROW["businessReference"]}}}}))
        with pytest.raises(BusinessValidationError):
            await service._push_status({"task_id": "legacy", "status": status, "name": ROW["businessReference"]})
        service._erpnext.update_status.assert_not_awaited()
    asyncio.run(run())


def test_pending_registration_replays_original_request_if_no_effect_happened():
    async def run():
        request = {"ref": "api:synthetic", "title": "Synthetic", "project_key": "contract-test", "task_id": "original"}
        task = {"task_id": "original", "status": "queued", "params": {"_mirrors": {"business": {"provider": "jardal_erp", "name": None, "ref": "api:synthetic", "pending": True, "registration": request}}}}
        service = TaskService.__new__(TaskService)
        service._erpnext = SimpleNamespace(provider="jardal_erp", is_enabled=True, find_by_ref=AsyncMock(return_value=None),
            create_ticket=AsyncMock(return_value={"task": {"name": ROW["businessReference"], "openceo_task_id": "original"}}))
        service._repo = SimpleNamespace(patch_task=AsyncMock())
        service._wake_dispatcher = lambda: None
        await service._enqueue_erpnext_registration(task)
        service._erpnext.create_ticket.assert_awaited_once_with(**request)
        patch = service._repo.patch_task.call_args.args[1]
        assert patch["params"]["_mirrors"]["business"]["pending"] is False
    asyncio.run(run())


def test_directed_backfill_verifies_mapping_and_keeps_old_link():
    async def run():
        task = {"task_id": "original", "project": "contract-test", "params": {"_mirrors": {"erpnext": {"name": ROW["businessReference"], "ref": "api:synthetic"}}}}
        async def patch(task_id, changes):
            assert task_id == "original"
            task.update(changes)
        repo = SimpleNamespace(get_task=AsyncMock(side_effect=lambda _: copy.deepcopy(task)), patch_task=patch)
        adapter = SimpleNamespace(provider="jardal_erp", resolve_task=AsyncMock(return_value={"name": ROW["businessReference"], "project": "contract-test", "openceo_task_id": "original", "business_ticket_id": ROW["id"]}))
        assert await backfill_business_links(repo, adapter, ["original"]) == {"changed": 1}
        assert await backfill_business_links(repo, adapter, ["original"]) == {"changed": 0}
        assert task["params"]["_mirrors"]["erpnext"]["name"] == ROW["businessReference"]
        with pytest.raises(BusinessValidationError):
            task["params"]["horizon_scope"] = "personal"
            await backfill_business_links(repo, adapter, ["original"])
    asyncio.run(run())


def test_history_uses_actual_dates_and_retains_original_plan():
    row = {**ROW, "status": "Completed", "actualStart": "2026-10-15T13:00:00Z", "actualEnd": "2026-10-15T14:00:00Z",
           "plannedStart": "2026-10-16T13:00:00Z", "plannedEnd": "2026-10-20T22:00:00Z"}
    view = task_view(row, zone=ZoneInfo("America/Caracas"))
    assert view["exp_start_date"] == "2026-10-15 09:00:00"
    assert view["exp_end_date"] == "2026-10-15 10:00:00"
    assert view["completed_on"] == "2026-10-15"
    assert view["plannedEnd"] == "2026-10-20T22:00:00Z"
    view = task_view({**row, "actualStart": None, "actualEnd": None})
    assert view["exp_start_date"] is None and view["exp_end_date"] is None


def test_legacy_webhook_rejects_valid_secret_after_authority_switch():
    from fastapi import FastAPI, Header
    async def run():
        app = FastAPI()
        context = SimpleNamespace(provider="erpnext", writes=0)
        @app.post('/api/integrations/erpnext/task')
        async def receive(x_openceo_secret: str | None = Header(default=None)):
            require_legacy_webhook(SimpleNamespace(ERPNEXT_WEBHOOK_SECRET="synthetic"), context.provider, x_openceo_secret)
            context.writes += 1
            return {"ok": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-OpenCEO-Secret": "synthetic"}
            assert (await client.post('/api/integrations/erpnext/task', headers=headers)).status_code == 200
            context.provider = "jardal_erp"
            assert (await client.post('/api/integrations/erpnext/task', headers=headers)).status_code == 409
            assert context.writes == 1
    asyncio.run(run())


def test_business_status_callback_rejects_legacy_events_before_lookup_or_visibility():
    async def run():
        service = TaskService.__new__(TaskService)
        service._erpnext = SimpleNamespace(provider="jardal_erp")
        service.find_task_for_business_ref = AsyncMock()
        result = await service.apply_business_status(task_id="original", business_status="Cancelled", business_name=ROW["businessReference"])
        assert result["status"] == "authority_mismatch"
        service.find_task_for_business_ref.assert_not_awaited()
    asyncio.run(run())


def test_visibility_does_not_repair_unknown_actual_dates_with_planned_writes():
    async def run():
        row = {**ROW, "status": "Completed", "actualStart": None, "actualEnd": None,
               "createdAt": "2026-10-15T12:00:00Z", "plannedStart": "2026-10-16T13:00:00Z"}
        view = task_view(row)
        service = TaskService.__new__(TaskService)
        adapter = SimpleNamespace(legacy_date_repairs=False, update_work_dates=AsyncMock())
        for _ in range(2):
            assert not await service._ensure_own_dates(adapter, view, None, "UTC", datetime.now(timezone.utc), "synthetic")
        adapter.update_work_dates.assert_not_awaited()
        assert view["plannedStart"] == "2026-10-16T13:00:00Z" and view["actualStart"] is None
    asyncio.run(run())


def test_group_rollup_compares_plans_and_does_not_rewrite_from_actual_dates():
    async def run():
        work = JardalERPWork.__new__(JardalERPWork)
        work._zone = ZoneInfo("UTC")
        plan = {"plannedStart": "2026-10-16T13:00:00Z", "plannedEnd": "2026-10-20T22:00:00Z"}
        group = work._view({**ROW, **plan, "kind": "epic", "status": "Working", "actualStart": "2026-10-15T13:00:00Z", "progressPercent": 33.33})
        children = [work._view({**ROW, **plan, "status": status, "actualStart": "2026-10-15T13:00:00Z", "actualEnd": "2026-10-15T14:00:00Z"}) for status in ("Completed", "Open", "Open")]
        adapter = SimpleNamespace(resolve_task=AsyncMock(return_value=group), list_children=AsyncMock(return_value=children),
            rollup_view=work.rollup_view, update_group_rollup=AsyncMock())
        service = TaskService.__new__(TaskService)
        service.find_task_for_business_ref = AsyncMock(return_value=None)
        for _ in range(2):
            assert await service._rollup_single(adapter, ROW["businessReference"]) == (False, None)
        adapter.update_group_rollup.assert_not_awaited()
    asyncio.run(run())
