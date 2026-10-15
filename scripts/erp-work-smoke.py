import asyncio
import os
import base64
from uuid import uuid4

from business.errors import BusinessDependencyBlockedError, BusinessValidationError
from business.work import JardalERPWork
from settings import Settings
from visibility import group_progress, parse_site_datetime


async def main():
    settings = Settings(_env_file=None)
    if os.environ.get("JARDAL_ERP_SYNTHETIC_TEST") != "1":
        raise RuntimeError("This smoke requires an explicitly isolated synthetic test environment.")
    work = JardalERPWork(settings)
    try:
        await work.ensure_project("core-contract-test", "Synthetic Core contract")
        ref = "api:synthetic-core-" + str(uuid4())
        kwargs = {"ref": ref, "title": "Synthetic HTTP integration", "project_key": "core-contract-test", "task_id": str(uuid4())}
        first = await work.create_ticket(**kwargs)
        second = await work.create_ticket(**kwargs)
        assert first["task"]["name"] == second["task"]["name"]
        assert second["duplicate"]
        assert first["task"]["execution"]["autorun"] is False
        recovered = await work.create_ticket(**{**kwargs, "task_id": str(uuid4()), "ticket_number": 99})
        assert recovered["recovered"]
        assert recovered["task"]["openceo_task_id"] == first["task"]["openceo_task_id"]
        resolved = await work.resolve_task(ref)
        assert resolved["name"] == first["task"]["name"]
        try:
            await work.create_ticket(**{**kwargs, "title": "Different payload"})
        except BusinessValidationError as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("Conflicting payload reused an idempotency key")
        child = await work.create_ticket(ref=ref + ":child", title="Synthetic dependent", project_key="core-contract-test", depends_on=[resolved["name"]])
        try:
            await work.transition_status(child["task"]["name"], "Completed", evidence="Synthetic evidence")
        except BusinessDependencyBlockedError:
            pass
        else:
            raise AssertionError("Open dependency was ignored")
        await work.transition_status(resolved["name"], "Working", reason="Synthetic execution")
        await work.add_comment(resolved["name"], "Synthetic communication evidence")
        await work.add_comment(resolved["name"], "Synthetic communication evidence")
        thread = await work.list_thread(resolved["name"])
        assert len(thread["comments"]) == 1
        comment = thread["comments"][0]
        await work.update_comment(comment["id"], "Synthetic corrected communication evidence")
        attached = await work.attach_file(resolved["name"], content_base64=base64.b64encode(b"synthetic private evidence").decode(), file_name="synthetic.txt")
        attachment = attached["attachment"]
        read = await work.get_attachment(attachment["id"], include_content=True)
        assert base64.b64decode(read["attachment"]["content_base64"]) == b"synthetic private evidence"
        assert read["attachment"]["private"]
        await work.update_attachment(attachment["id"], resolved["name"], file_name="synthetic-renamed.txt")
        await work.delete_attachment(attachment["id"], resolved["name"])
        group = await work.create_group_task(ref=ref + ":group", title="Synthetic group", project_key="core-contract-test")
        progress = group_progress([{"status": "Completed"}, {"status": "Open"}, {"status": "Open"}])
        await work.update_group_rollup(group["task"]["name"], {"progress": progress, "exp_start_date": "2026-10-15 09:00:00", "exp_end_date": "2026-10-16 09:00:00"})
        group_view = await work.resolve_task(group["task"]["name"])
        assert group_view["progress"] == 33.33
        assert parse_site_datetime(group_view["exp_start_date"]) is not None
        await work.transition_status(resolved["name"], "Completed", reason="Synthetic checks passed", evidence="HTTP, idempotency and dependencies verified")
        await work.transition_status(child["task"]["name"], "Working", reason="Synthetic execution")
        await work.transition_status(child["task"]["name"], "Completed", reason="Synthetic checks passed", evidence="Dependency completed first")
        print("Core → HTTP → Jardal ERP: creation, source/TASK references, replay/conflict, thread and dependency transitions verified; no models.")
    finally:
        await work.aclose()


asyncio.run(main())
