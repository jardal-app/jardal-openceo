from typing import Any
import json
from pathlib import Path

from business.ports import BusinessWork


def validate_authority_manifest(settings: Any) -> None:
    authority_file = str(getattr(settings, "BUSINESS_AUTHORITY_FILE", "") or "")
    if not authority_file:
        raise ValueError("Activar Jardal ERP requiere BUSINESS_AUTHORITY_FILE generado por la migración verificada.")
    try:
        manifest = json.loads(Path(authority_file).read_text())
    except (OSError, ValueError):
        raise ValueError("No se pudo leer el manifiesto de autoridad.") from None
    if not isinstance(manifest, dict):
        raise ValueError("El manifiesto de autoridad debe ser un objeto JSON.")
    expected = {"schema_version": 1, "provider": "jardal_erp", "state": "ready",
                "workspace_id": settings.JARDAL_ERP_WORKSPACE_ID,
                "origin": settings.JARDAL_ERP_URL.rstrip("/"), "execution_database": settings.MONGODB_DATABASE}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("El manifiesto de autoridad no habilita este ERP/workspace/runtime.")


def build_business_work(settings: Any) -> BusinessWork:
    provider = str(settings.BUSINESS_PROVIDER).strip()
    if provider == "jardal_erp":
        validate_authority_manifest(settings)
        from business.work import JardalERPWork
        adapter = JardalERPWork(settings)
        if not adapter.is_enabled:
            raise ValueError(adapter.client.config_error())
        return adapter
    raise ValueError("BUSINESS_PROVIDER debe ser jardal_erp.")
