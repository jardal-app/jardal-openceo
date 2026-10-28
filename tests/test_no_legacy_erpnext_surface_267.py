"""No queda superficie legacy ERPNext (TASK-2026-00267).

El sunset retira alias, shims, ruta webhook vieja, valor de provider viejo
y variables ERPNEXT_* salvo las credenciales del adapter Frappe que finance
sigue usando (FINANCE_PROVIDER=erpnext, verificado aparte).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from settings import Settings


def test_legacy_shim_modules_are_gone():
    assert importlib.util.find_spec("tools.erpnext_client") is None
    assert importlib.util.find_spec("tools.erpnext_tickets") is None


def test_legacy_settings_fields_are_gone():
    fields = Settings.model_fields
    for name in ("ERPNEXT_TIMEOUT_SECONDS", "ERPNEXT_CONNECT_TIMEOUT_SECONDS",
                 "ERPNEXT_TICKETS_PROJECT_COMPANY", "ERPNEXT_WEBHOOK_SECRET",
                 "ERPNEXT_SITE_TIMEZONE", "ERPNEXT_ATTACHMENT_MAX_BYTES",
                 "ERPNEXT_ATTACHMENT_ALLOWED_ROOTS"):
        assert name not in fields, name
    for name in ("JARDAL_ERP_TIMEOUT_SECONDS",
                 "JARDAL_ERP_CONNECT_TIMEOUT_SECONDS",
                 "JARDAL_ERP_TICKETS_PROJECT_COMPANY",
                 "JARDAL_ERP_SITE_TIMEZONE",
                 "JARDAL_ERP_ATTACHMENT_MAX_BYTES",
                 "JARDAL_ERP_ATTACHMENT_ALLOWED_ROOTS"):
        assert name in fields, name


def test_business_provider_defaults_to_jardal_erp_and_rejects_erpnext():
    from business.provider import build_business_work
    assert Settings.model_fields["BUSINESS_PROVIDER"].default == "jardal_erp"
    with pytest.raises(ValueError):
        build_business_work(SimpleNamespace(BUSINESS_PROVIDER="erpnext"))


def test_legacy_webhook_guard_is_gone():
    import business.webhooks as webhooks_mod
    assert not hasattr(webhooks_mod, "require_legacy_webhook")
    assert hasattr(webhooks_mod, "require_erp_webhook")


def test_old_webhook_route_is_gone_from_main():
    main_py = Path(__file__).resolve().parents[1] / "openceo-core" / "main.py"
    assert '@app.post("/api/integrations/erpnext/task")' not in main_py.read_text()
