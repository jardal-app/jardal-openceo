"""Soporte compartido para los tests de research.

Contiene el arranque de un ``MongoRepository`` real contra una base aislada y
constructores de documentos de job. Se usa Mongo de verdad, no un fake de saldo: una
divergencia entre el fake y el motor se veria como un test verde que falla en
produccion, que es exactamente lo que el epico prohibe aceptar como prueba.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from db.mongo import MongoRepository  # noqa: E402
from research.contracts import (  # noqa: E402
    Classification,
    ResearchJobRequest,
    new_research_id,
)
from settings import Settings  # noqa: E402

MONGO_URL = "mongodb://localhost:27017"
TEST_DB = "openceo_test_research"


def mongo_available(url: str = MONGO_URL) -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(url, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


MONGO_AVAILABLE = mongo_available()


def test_settings(db_name: str = TEST_DB) -> Settings:
    return Settings(_env_file=None, MONGODB_URL=MONGO_URL, MONGODB_DATABASE=db_name)


def build_repo(db_name: str = TEST_DB) -> MongoRepository:
    return MongoRepository(test_settings(db_name))


def request(**overrides: Any) -> ResearchJobRequest:
    payload: dict[str, Any] = {
        "project_key": "jardal-openceo",
        "purpose": "company_interview_brief",
        "mode": "quick",
        "question": "Investigar contexto del rol en Empresa Ejemplo",
    }
    payload.update(overrides)
    return ResearchJobRequest(**payload)


def job_doc(
    req: ResearchJobRequest | None = None,
    *,
    owner_id: str = "usr_test",
    research_id: str | None = None,
    effective_limits: dict[str, int] | None = None,
    status: str = "queued",
) -> dict[str, Any]:
    req = req or request()
    return {
        "research_id": research_id or new_research_id(),
        "project_key": req.project_key,
        "owner_id": owner_id,
        "purpose": req.purpose,
        "mode": req.mode.value,
        "question": req.question,
        "request_fingerprint": req.fingerprint(),
        "idempotency_key": req.idempotency_key,
        "status": status,
        "classification": Classification.PUBLIC.value,
        "budget_ref": req.budget_ref,
        "effective_limits": effective_limits or {},
        "seed_urls": list(req.seed_urls),
        "attempts": 0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


# --------------------------------------------------------------------------- vigilancia
# Soporte de los tests de suscripciones (TASK-2026-00069): una fuente guionada y un item.


def watch_item(identity: str, title: str = "Vacante", *, url: str | None = None, summary: str | None = None):
    from research.watch import WatchItem, content_fingerprint, item_key_for

    url = url or f"https://example.org/jobs/{identity}"
    return WatchItem(item_key=item_key_for(identity, url), content_fp=content_fingerprint(title, summary, url),
                     url=url, title=title, summary=summary)


class ScriptedAdapter:
    """Fuente guionada: cada llamada devuelve la pagina que dicte ``queue`` o ``pages``."""

    name = "rss"

    def __init__(self) -> None:
        from research.watch import SourcePage

        self._page_type = SourcePage
        self.calls: list[dict] = []
        self.pages: dict = {}
        self.queue: list = []

    async def fetch_page(self, source, *, page_token, conditional, scope, deadline):
        self.calls.append({"page_token": page_token, "conditional": dict(conditional or {}), "scope": dict(scope)})
        if self.queue:
            return self.queue.pop(0)
        return self.pages.get(page_token, self._page_type())
