from typing import Any

from business.errors import BusinessValidationError
from business.links import business_link
from horizon.privacy import is_personal_scope


async def backfill_business_links(repo: Any, adapter: Any, task_ids: list[str]) -> dict[str, int]:
    if not task_ids or len(task_ids) > 200:
        raise ValueError("El backfill requiere entre 1 y 200 task_ids explícitos.")
    changed = 0
    for task_id in dict.fromkeys(task_ids):
        task = await repo.get_task(task_id)
        if not task or is_personal_scope(task):
            raise BusinessValidationError("Ticket ausente o personal; no se migra por el canal empresarial.", status_code=422)
        old = business_link(task)
        if not old.get("name") or not old.get("ref"):
            raise BusinessValidationError("El backfill exige TASK y ref de origen conocidos.", status_code=422)
        target = await adapter.resolve_task(old["ref"])
        if target.get("name") != old["name"] or target.get("project") != task.get("project"):
            raise BusinessValidationError("El mapping de TASK/proyecto no coincide con el origen.", status_code=409)
        if target.get("openceo_task_id") != task_id:
            raise BusinessValidationError("El mapping de ejecución no coincide con el origen.", status_code=409)
        params = dict(task.get("params") or {})
        mirrors = dict(params.get("_mirrors") or {})
        canonical = {**old, "provider": adapter.provider, "business_ticket_id": target.get("business_ticket_id")}
        if mirrors.get("business") == canonical:
            continue
        mirrors["business"] = canonical
        params["_mirrors"] = mirrors
        await repo.patch_task(task_id, {"params": params})
        if business_link(await repo.get_task(task_id)) != canonical:
            raise RuntimeError("No se pudo verificar el enlace migrado.")
        changed += 1
    return {"changed": changed}
