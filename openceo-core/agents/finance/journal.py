"""Escritura idempotente de un `Journal Entry` en ERPNext. Cero logica LLM.

El mecanismo de identidad del contrato (`docs/finance-erpnext-integration-contract.md`
§6.1) vive aqui, una sola vez: `openceo_ref` es unico en ERPNext, asi que un
reintento choca con el indice (HTTP 417) y se resuelve **releyendo** el
documento existente en vez de duplicarlo. Si quedo en borrador, se envia.

Lo comparten `finance.register_expense` y `finance.register_income`: son el
mismo asiento con las dos patas invertidas, y un cambio en la semantica del
reintento debe aplicarse a los dos flujos a la vez.

Contabiliza movimientos ya ocurridos; `execute_money_movements=false` sigue
vigente (contrato §8): nada de esto mueve dinero externo.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

_JOURNAL_FIELDS = ["name", "docstatus", "openceo_ref", "total_debit", "posting_date"]


def new_openceo_ref(prefix: str) -> str:
    """Identidad idempotente de la operacion. Reintento = mismo valor."""
    return f"{prefix}:{uuid4().hex}"


async def find_by_ref(client: Any, ref: str) -> dict[str, Any] | None:
    rows = await client.list_docs(
        "Journal Entry",
        filters=[["openceo_ref", "=", ref]],
        fields=_JOURNAL_FIELDS,
        limit=1,
    )
    return rows[0] if rows else None


async def submit_journal_entry(
    client: Any,
    payload: dict[str, Any],
    *,
    ref: str,
    submit: bool = True,
) -> dict[str, Any]:
    """Crea el asiento y lo envia. Idempotente por `ref`.

    Devuelve `{"ok", "duplicate", "doc", "ref"}`. `duplicate=True` significa
    que ese `openceo_ref` ya existia: no se creo nada nuevo.
    """
    try:
        created = await client.create_doc("Journal Entry", payload)
    except Exception as exc:
        if getattr(exc, "status_code", None) != 417:
            raise
        existing = await find_by_ref(client, ref)
        if not existing:
            raise
        doc = await _ensure_submitted(client, existing, submit)
        return {"ok": True, "duplicate": True, "doc": doc, "ref": ref}

    doc = await _ensure_submitted(client, created, submit)
    return {"ok": True, "duplicate": False, "doc": doc, "ref": ref}


async def _ensure_submitted(client: Any, doc: dict[str, Any], submit: bool) -> dict[str, Any]:
    if not submit:
        return doc
    if int(doc.get("docstatus") or 0) != 0:
        return doc
    name = str(doc.get("name") or "").strip()
    if not name:
        return doc
    return await client.update_doc("Journal Entry", name, {"docstatus": 1})
