"""Demo B: suscripcion acotada al feed autorizado (ticket #643).

El feed autorizado es unico (Owner 2026-11-06, correccion 2026-11-09T12:49
que sustituye la URL) y este modulo es su fuente
canonica: los tests de verificacion y el runbook productivo
(``docs/research-operations.md``, seccion Demo B) construyen la misma
suscripcion desde aqui, en vez de repetir la URL y los topes en cada sitio.

Acotacion: intervalo por encima del minimo del servidor, proposito y clave
idempotente fijos (una sola Demo B por owner), modo quick y techo publico
heredados de la politica efectiva. Los topes de ingestion por tick y de
suscripciones activas los aplica el motor, no este spec.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from research.subscriptions import SubscriptionRequest
from research.watch import normalize_source

__all__ = [
    "DEMO_B_FEED_URL",
    "DEMO_B_IDEMPOTENCY_KEY",
    "DEMO_B_INTERVAL_SECONDS",
    "DEMO_B_PROJECT_KEY",
    "DEMO_B_PURPOSE",
    "DEMO_B_QUESTION",
    "demo_b_bounds_problems",
    "demo_b_source",
    "demo_b_subscription_request",
]

# Feed autorizado unico de Demo B (Owner 2026-11-06, correccion 2026-11-09T12:49
# que sustituye la URL muerta Jardal/ por jardal-app/). Solo publico: una URL
# con credenciales o esquema no https se rechaza al normalizar.
DEMO_B_FEED_URL = "https://github.com/jardal-app/jardal-openceo/releases.atom"
DEMO_B_PROJECT_KEY = "jardal-openceo"
DEMO_B_PURPOSE = "demo-b"
DEMO_B_QUESTION = "Que releases nuevos publico jardal-openceo?"
DEMO_B_INTERVAL_SECONDS = 3600
DEMO_B_IDEMPOTENCY_KEY = "demo-b"


def demo_b_source() -> dict[str, str]:
    """Fuente canonica de Demo B, en la forma que la suscripcion persiste."""
    return normalize_source({"type": "rss", "url": DEMO_B_FEED_URL})


def demo_b_subscription_request() -> SubscriptionRequest:
    """Peticion acotada de Demo B: primera pasada como linea base, sin backfill."""
    return SubscriptionRequest(
        project_key=DEMO_B_PROJECT_KEY,
        purpose=DEMO_B_PURPOSE,
        question=DEMO_B_QUESTION,
        source=demo_b_source(),
        interval_seconds=DEMO_B_INTERVAL_SECONDS,
        idempotency_key=DEMO_B_IDEMPOTENCY_KEY,
    )


def demo_b_bounds_problems(settings: Any, project_cfg: Mapping[str, Any]) -> list[str]:
    """Topes que la Demo B exige al despliegue y al proyecto, en prosa.

    Vacio significa apto: research esta habilitado, el intervalo respeta el
    minimo del servidor, el techo es publico y quick esta permitido (los ticks
    admiten jobs quick). El guard de egress sigue siendo quien bloquea
    destinos no publicos en cada lectura.
    """
    problems: list[str] = []
    minimum = int(getattr(settings, "RESEARCH_SUBSCRIPTIONS_MIN_INTERVAL_SECONDS", 900) or 0)
    if DEMO_B_INTERVAL_SECONDS < minimum:
        problems.append(f"intervalo Demo B ({DEMO_B_INTERVAL_SECONDS} s) bajo el minimo ({minimum} s)")
    research = (project_cfg or {}).get("research") or {}
    if not research.get("enabled"):
        problems.append("Demo B exige research habilitado (research.enabled)")
    if str(research.get("classification_ceiling") or "").lower() != "public":
        problems.append("Demo B exige classification_ceiling public")
    if "quick" not in list(research.get("allowed_modes") or []):
        problems.append("Demo B exige allowed_modes con quick")
    host = (urlsplit(DEMO_B_FEED_URL).hostname or "").lower()
    if urlsplit(DEMO_B_FEED_URL).scheme != "https" or not host or host in {"localhost", "127.0.0.1"}:
        problems.append("Demo B exige un feed https publico")
    return problems
