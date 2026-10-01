"""Montaje real del servicio research en el proceso (TASK-2026-00066).

La fachada no debe construir clientes ni abrir conexiones: eso se hace aqui, una sola
vez, y se devuelve junto a los recursos que hay que cerrar **antes** que el repositorio.

Honestidad sobre lo que falta
=============================
Cada pieza se monta solo si su prerequisito existe: sin clave de Brave no hay
descubrimiento, sin CLI de OpenCode no hay runtime y sin aislamiento disponible el
runtime se negara a ejecutar. La ausencia **no** se disfraza: el servicio existe, el
manager explica el motivo, y los jobs terminan en `partial`/`failed` con su causa en
lugar de inventar cobertura. Un servicio que solo funciona con todas las claves seria
imposible de diagnosticar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.budget import ResearchBudgetLedger
from research.service import ResearchDependencies, ResearchService

logger = logging.getLogger(__name__)

__all__ = [
    "ResearchRuntimeResources",
    "build_research_service",
    "close_research_service",
]


@dataclass
class ResearchRuntimeResources:
    """Recursos que el proceso debe cerrar al apagarse."""

    vault: Any | None = None
    runtime: Any | None = None
    http_client: Any | None = None

    async def aclose(self) -> None:
        # Los workers y el almacen se cierran antes que el repositorio de Mongo: una
        # escritura suya despues del cierre quedaria huerfana.
        if self.runtime is not None and hasattr(self.runtime, "aclose"):
            try:
                await self.runtime.aclose()
            except Exception:  # noqa: BLE001 - el apagado no puede quedar bloqueado
                logger.warning("No se pudo cerrar el runtime de research", exc_info=True)
        if self.http_client is not None and hasattr(self.http_client, "aclose"):
            try:
                await self.http_client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("No se pudo cerrar el cliente HTTP de research", exc_info=True)
        if self.vault is not None and hasattr(self.vault, "close"):
            try:
                self.vault.close()
            except Exception:  # noqa: BLE001
                logger.warning("No se pudo cerrar el almacen de evidencias", exc_info=True)


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


class _ProjectEvidence:
    """Adapta el almacen por proyecto a la dependencia unica del servicio.

    El servicio no conoce el proyecto al construirse, pero la evidencia si: la ruta y
    el filtro salen del `project_key` de la propia evidencia, nunca del payload de un
    consumidor.
    """

    def __init__(self, vault: Any) -> None:
        self._vault = vault

    async def asave(self, evidence: Any, **kwargs: Any) -> Any:
        repository = self._vault.project(evidence.project_key)
        return await repository.asave(evidence, **kwargs)


def _build_evidence(settings: Any) -> tuple[Any, Any]:
    from research.evidence import EvidenceVault, RetentionWindows

    root = _setting(settings, "RESEARCH_EVIDENCE_DIR", "")
    if not root:
        return None, None
    try:
        vault = EvidenceVault(
            root=root, retention=RetentionWindows.from_settings(settings)
        )
    except Exception:  # noqa: BLE001 - sin almacen el servicio sigue, con su causa
        logger.warning("No se pudo abrir el almacen de evidencias de research", exc_info=True)
        return None, None
    return _ProjectEvidence(vault), vault


def _build_discovery(settings: Any, ledger: Any) -> Any | None:
    if not _setting(settings, "RESEARCH_BRAVE_API_KEY", ""):
        return None
    try:
        from research.acquisition.brave import BraveClient

        return BraveClient(settings=settings, ledger=ledger)
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo construir el cliente de Brave", exc_info=True)
        return None


def _build_reader(settings: Any) -> Any | None:
    """Lector HTTP publico con el guard de egress. Sin este, no hay lectura real."""
    try:
        from research.acquisition.http_reader import HttpReader
        from research.policies import EgressGuard, GuardedFetcher

        guard = EgressGuard()
        fetcher = GuardedFetcher(guard=guard)
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo construir el lector HTTP de research", exc_info=True)
        return None

    reader = HttpReader(fetcher=fetcher)

    async def read(*, url: str, research_id: str, project_key: str, **kwargs: Any) -> dict[str, Any]:
        outcome = await reader.read(
            url,
            research_id=research_id,
            project_key=project_key,
            deadline=kwargs.get("deadline"),
        )
        return {
            "url": getattr(outcome, "final_url", None) or url,
            "text": getattr(outcome, "text", "") or "",
            "observed_at": getattr(outcome, "observed_at", None),
            "published_at": getattr(outcome, "published_at", None),
            "content_hash": getattr(outcome, "content_hash", None),
            "provider": reader.provider,
        }

    return read


def _build_runtime(settings: Any) -> Any | None:
    cli = str(_setting(settings, "RESEARCH_OPENCODE_CLI_PATH", "") or "")
    if not cli:
        return None
    if Path(cli).exists() or cli in {"opencode"}:
        try:
            from research.runtimes.opencode import OpenCodeRuntime

            return OpenCodeRuntime(
                settings=settings,
                broker_url=str(_setting(settings, "RESEARCH_BROKER_URL", "") or ""),
                broker_bearer=str(_setting(settings, "RESEARCH_BROKER_BEARER", "") or ""),
            )
        except Exception:  # noqa: BLE001
            logger.warning("No se pudo construir el runtime de OpenCode", exc_info=True)
    else:
        logger.info("Runtime de research no montado: no existe el CLI en %s", cli)
    return None


def build_research_service(
    settings: Any,
    repo: Any,
    *,
    clock: Any = None,
) -> tuple[ResearchService, ResearchRuntimeResources]:
    """Construye el servicio y sus recursos. Funciona aunque falten claves."""
    resources = ResearchRuntimeResources()

    ledger = None
    if hasattr(repo, "research_budget_accounts") and hasattr(repo, "research_usage"):
        ledger = ResearchBudgetLedger(
            accounts=repo.research_budget_accounts,
            usage=repo.research_usage,
        )

    evidence, vault = _build_evidence(settings)
    resources.vault = vault

    runtime = _build_runtime(settings)
    resources.runtime = runtime

    deps = ResearchDependencies(
        repo=repo,
        ledger=ledger,
        evidence=evidence,
        runtime=runtime,
        discover=_build_discovery(settings, ledger),
        read=_build_reader(settings),
    )
    service = ResearchService(settings=settings, deps=deps, clock=clock)
    return service, resources


async def close_research_service(resources: ResearchRuntimeResources) -> None:
    await resources.aclose()
