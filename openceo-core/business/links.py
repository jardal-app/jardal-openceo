from collections.abc import Mapping
from typing import Any

from business.errors import BusinessValidationError


def business_mirror(mirrors: Any) -> dict[str, Any]:
    if not isinstance(mirrors, Mapping):
        return {}
    for key in ("business", "erpnext"):
        value = mirrors.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def business_link(task: Mapping[str, Any] | None) -> dict[str, Any]:
    params = (task or {}).get("params")
    return business_mirror(params.get("_mirrors") if isinstance(params, Mapping) else None)


def assert_link_provider(task: Mapping[str, Any] | None, provider: str) -> None:
    mirror = business_link(task)
    if mirror and str(mirror.get("provider") or "erpnext") != provider:
        raise BusinessValidationError("El enlace pertenece a otra autoridad; falta el backfill verificado.", status_code=409)
