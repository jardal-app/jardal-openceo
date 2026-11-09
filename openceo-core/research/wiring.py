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

from research.budget import ResearchBudgetLedger, ResearchPolicyLedger
from research.money import parse_optional_micro
from research.service import ResearchDependencies, ResearchService

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectEvidence",
    "ResearchRuntimeResources",
    "build_research_service",
    "build_social_broker",
    "close_research_service",
]


@dataclass
class ResearchRuntimeResources:
    """Recursos que el proceso debe cerrar al apagarse."""

    vault: Any | None = None
    runtime: Any | None = None
    http_client: Any | None = None
    broker: Any | None = None
    capabilities: Any | None = None
    read_log: Any | None = None
    # Piezas que la vigilancia de suscripciones reutiliza: el mismo lector con guard de
    # egress y el mismo descubrimiento (con su ledger). No hay clientes paralelos.
    http_reader: Any | None = None
    discover: Any | None = None
    firecrawl: Any | None = None

    async def aclose(self) -> None:
        # Los workers y el almacen se cierran antes que el repositorio de Mongo: una
        # escritura suya despues del cierre quedaria huerfana.
        if self.broker is not None and self.capabilities is not None:
            # Una capability no debe sobrevivir al proceso que la emitio.
            try:
                self.capabilities.revoke_all(reason="broker apagado")
            except Exception:  # noqa: BLE001
                logger.warning("No se pudieron revocar las capabilities", exc_info=True)
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


class ProjectEvidence:
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

    async def aget(self, scope: Any, evidence_id: str):
        return await self._vault.project(scope.project_key).aget(scope, evidence_id)


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
    return ProjectEvidence(vault), vault


def _build_discovery(settings: Any, ledger: Any, *, repo=None, firecrawl=None) -> Any | None:
    """Descubrimiento como **callable**, que es el contrato que el servicio usa.

    `BraveClient` expone `search_web(query, ...)`; el servicio invoca su dependencia
    como `discover(question=..., research_id=..., project_key=...)`. Entregar el
    cliente sin adaptar no falla al construir ni al arrancar: falla **en el primer
    job con busqueda**, con un `TypeError` que el dispatcher reduce a un `failed`
    generico. Se adapta aqui, una sola vez, y el mismo callable lo comparte el broker.
    """
    from research.acquisition.brave import BraveClient
    from research.acquisition.serper import SerperClient
    from research.acquisition.base import DiscoveryResult, ProviderDisabledError, AcquisitionError
    clients = {}
    for name, factory in (("brave", BraveClient), ("serper", SerperClient)):
        if _setting(settings, f"RESEARCH_{name.upper()}_API_KEY", "") and _setting(settings, f"RESEARCH_{name.upper()}_ENABLED", True):
            extra = {}
            if name == "serper":
                extra["adapter_enabled"] = ledger is not None and getattr(repo, "db", None) is not None
            if name == "serper" and getattr(repo, "db", None) is not None:
                from research.acquisition.base import parse_optional_int
                from research.provider_credits import ProviderCredits
                extra["credit_budget"] = ProviderCredits(repo.db, provider="serper", account="default",
                    capacity=parse_optional_int(_setting(settings, "RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES", "")),
                    window="nonrenewing")
            clients[name] = factory(settings=settings, ledger=ledger, max_attempts=1, **extra)
    if firecrawl is not None:
        clients["firecrawl"] = firecrawl
    if not clients:
        return None

    async def discover(*, question: str, research_id: str, project_key: str, **kwargs: Any) -> Any:
        # Calls from the model carry no authority fields: resolve its persisted job.
        # Subscription ticks supply policy derived by their authenticated runner.
        job = await repo.get_research_job(research_id) if repo is not None else None
        policy = kwargs.get("policy")
        adapters = (job.get("adapters", {}) if job and job.get("project_key") == project_key else
                    dict(policy.adapters) if job is None and policy is not None and policy.project_key == project_key else {})
        classification = job.get("classification") if job else getattr(getattr(policy, "classification", None), "value", None)
        selected = kwargs.get("provider") or next((name for name in ("brave", "serper", "firecrawl") if adapters.get(name)), None)
        if classification != "public" or selected not in clients or not adapters.get(selected):
            return DiscoveryResult(provider=selected or "discovery", degradations=(ProviderDisabledError(
                "No hay proveedor publico autorizado y montado", provider=selected or "discovery").degradation(),))
        count = int(kwargs.get("count") or 10)
        common = dict(research_id=research_id, project_key=project_key, deadline=kwargs.get("deadline"))
        try:
            if selected == "brave":
                return await clients[selected].search_web(question, count=count, **common)
            if selected == "serper":
                return await clients[selected].search_web(question, num=count, **common)
            return await clients[selected].search(question, limit=count, **common)
        except AcquisitionError as exc:
            return DiscoveryResult(provider=selected, degradations=(exc.degradation(),))

    discover.providers = frozenset(clients)
    return discover


def _build_firecrawl(settings, ledger, repo):
    """Basic public extraction only, with measured, shared native credits."""
    import hashlib
    from research.acquisition.base import parse_optional_int
    from research.acquisition.firecrawl import FirecrawlClient
    from research.provider_credits import ProviderCredits
    key = _setting(settings, "RESEARCH_FIRECRAWL_API_KEY", "")
    window = _setting(settings, "RESEARCH_FIRECRAWL_CREDIT_WINDOW", "")
    db = getattr(repo, "db", None)
    if ledger is None or not key or not window or db is None or not _setting(settings, "RESEARCH_FIRECRAWL_ENABLED", True):
        return None
    budget = ProviderCredits(db, provider="firecrawl", account=hashlib.sha256(key.encode()).hexdigest()[:16],
        capacity=parse_optional_int(_setting(settings, "RESEARCH_FIRECRAWL_ACCOUNT_CREDITS", "")), window=window)
    return FirecrawlClient(settings=settings, ledger=ledger, credit_budget=budget, max_attempts=1)


def _provider_status(settings, *, ledger, discover, firecrawl, deep_model, runtime=None):
    from decimal import Decimal, InvalidOperation

    def positive(name):
        try:
            value = Decimal(str(_setting(settings, name, "")))
            return value.is_finite() and value > 0
        except InvalidOperation:
            return False

    configured = {
        "musej": bool(_setting(settings, "RESEARCH_MUSEJ_FALLBACK_ENABLED", True)
                      and _setting(settings, "MUSE_CLI_PATH", "")),
        "deep-free": bool(_setting(settings, "RESEARCH_FREE_ROUTER_ENABLED", False)
                          and _setting(settings, "RESEARCH_OPENCODE_CLI_PATH", "")),
        "deep": bool(_setting(settings, "RESEARCH_DEEP_API_KEY", "") and _setting(settings, "RESEARCH_DEEP_MODEL", ""))
            and all(positive(name) for name in ("RESEARCH_DEEP_INPUT_USD_PER_MILLION",
                "RESEARCH_DEEP_OUTPUT_USD_PER_MILLION", "RESEARCH_DEEP_ACCOUNT_CEILING_USD", "RESEARCH_PAID_SPEND_USD")),
        "serper": bool(_setting(settings, "RESEARCH_SERPER_API_KEY", "")) and positive("RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES"),
        "firecrawl": bool(_setting(settings, "RESEARCH_FIRECRAWL_API_KEY", "")
            and _setting(settings, "RESEARCH_FIRECRAWL_CREDIT_WINDOW", "")) and positive("RESEARCH_FIRECRAWL_ACCOUNT_CREDITS"),
    }
    mounted = {"musej": runtime is not None, "deep-free": runtime is not None,
               "deep": deep_model is not None and ledger is not None,
               "serper": "serper" in getattr(discover, "providers", ()) and ledger is not None,
               "firecrawl": firecrawl is not None and ledger is not None}
    return {name: {"configured": ready, "mounted": mounted[name], "verified": None}
            for name, ready in configured.items()}


def _build_http_reader(settings: Any) -> Any | None:
    """Lector HTTP publico con el guard de egress. Sin este, no hay lectura real."""
    try:
        from research.acquisition.http_reader import HttpReader
        from research.policies import EgressGuard, GuardedFetcher

        guard = EgressGuard()
        fetcher = GuardedFetcher(guard=guard)
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo construir el lector HTTP de research", exc_info=True)
        return None
    return HttpReader(fetcher=fetcher)


def _reader_callable(reader: Any) -> Any:
    async def read(*, url: str, research_id: str, project_key: str, **kwargs: Any) -> dict[str, Any]:
        outcome = await reader.read(
            url,
            research_id=research_id,
            project_key=project_key,
            deadline=kwargs.get("deadline"),
        )
        if outcome.degradations:
            from research.acquisition.base import AcquisitionError
            gap = outcome.degradations[0]
            error = AcquisitionError(gap.detail, provider=gap.provider)
            error.coverage_reason, error.kind = gap.coverage_reason, gap.reason
            raise error
        text = getattr(outcome, "text", "") or ""
        # `outcome.content_hash` describe el **cuerpo crudo** descargado; el almacen de
        # evidencias valida que el hash declare el contenido que guarda, que es el texto
        # extraido. Reenviarlo tal cual hacia fallar **toda** lectura HTML con
        # `EvidenceHashMismatch` (el extractor quita etiquetas, luego los bytes no
        # coinciden). Se publica el hash del texto que se almacena y se conserva el del
        # cuerpo aparte, como dato de transporte.
        from research.evidence import content_hash_of, normalize_content

        body_hash = getattr(outcome, "content_hash", None)
        return {
            "url": getattr(outcome, "final_url", None) or url,
            "text": text,
            "observed_at": getattr(outcome, "observed_at", None),
            "published_at": getattr(outcome, "published_at", None),
            "content_hash": content_hash_of(normalize_content(text)) if text else None,
            "body_hash": body_hash,
            "provider": reader.provider,
        }

    return read


def _build_reader(settings: Any) -> Any | None:
    """Lector como **callable** (contrato del servicio y del broker)."""
    reader = _build_http_reader(settings)
    return None if reader is None else _reader_callable(reader)


def _build_runtime(settings: Any, *, broker_url: str = "") -> Any | None:
    cli = str(_setting(settings, "RESEARCH_OPENCODE_CLI_PATH", "") or "")
    if not cli:
        return None
    if Path(cli).exists() or cli in {"opencode"}:
        try:
            from research.runtimes.isolation import ContainerBackend
            from research.runtimes.opencode import OpenCodeRuntime, probe_cli_version

            run_as = str(_setting(settings, "RESEARCH_WORKER_RUN_AS", "") or "")
            container = ContainerBackend(
                "docker",
                str(_setting(settings, "RESEARCH_WORKER_IMAGE", "") or "openceo-research-worker:latest"),
                network=str(_setting(settings, "RESEARCH_WORKER_NETWORK", "") or "openceo-research-egress"),
                run_user=run_as,
            )
            return OpenCodeRuntime(
                settings=settings,
                preferred_backend=str(_setting(settings, "RESEARCH_ISOLATION_BACKEND", "") or ""),
                container_backend=container,
                # La version fijada se **comprueba** contra el binario: sin esta sonda
                # el pin era una declaracion y una version distinta ejecutaba igual.
                version_probe=probe_cli_version,
                broker_url=broker_url or str(_setting(settings, "RESEARCH_BROKER_URL", "") or ""),
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
    """Construye el servicio y sus recursos. Funciona aunque falten claves.

    Incluye el **broker** del worker: sin el montado, el runtime se niega a lanzar
    (no hay adquisicion posible) y el circuito modelo -> tool MCP no existe. El
    broker comparte lector, evidencias y ledger con el servicio; no tiene claves ni
    red propias.
    """
    from research.broker_mount import (
        BrokerReadLog,
        build_broker_operations,
        build_research_broker_mount,
    )
    from research.worker.capabilities import CapabilityStore

    resources = ResearchRuntimeResources()

    ledger = None
    if hasattr(repo, "research_budget_accounts") and hasattr(repo, "research_usage"):
        ledger = ResearchBudgetLedger(
            accounts=repo.research_budget_accounts,
            usage=repo.research_usage,
        )
        ledger = ResearchPolicyLedger(ledger, repo,
            parse_optional_micro(_setting(settings, "RESEARCH_PAID_SPEND_USD", "0.00")) or 0,
            parse_optional_micro(_setting(settings, "RESEARCH_DEEP_ACCOUNT_CEILING_USD", "0.00")) or 0)

    evidence, vault = _build_evidence(settings)
    resources.vault = vault

    # Una sola tabla de capacidades por proceso: la emite el servicio para cada job y
    # la autentica el broker. Si fueran dos instancias, ninguna autenticaria nada.
    capabilities = CapabilityStore()
    resources.capabilities = capabilities

    http_reader = _build_http_reader(settings)
    reader = None if http_reader is None else _reader_callable(http_reader)
    firecrawl = _build_firecrawl(settings, ledger, repo)
    resources.firecrawl = firecrawl
    discover = _build_discovery(settings, ledger, repo=repo, firecrawl=firecrawl)
    http_read = reader
    async def selected_read(*, url, research_id, project_key, **kwargs):
        job = await repo.get_research_job(research_id)
        from research.acquisition.base import ProviderDisabledError, DestinationBlockedError, MissingConfigError
        if job is None or job.get("project_key") != project_key:
            raise ProviderDisabledError("Lectura sin job autorizado", provider="read")
        if job.get("read_provider", "http") != "firecrawl":
            if http_read is None:
                raise MissingConfigError("Lector HTTP no montado", provider="http")
            return await http_read(url=url, research_id=research_id, project_key=project_key, **kwargs)
        from research.policies import EgressGuard, EgressError
        if not job.get("adapters", {}).get("firecrawl") or job.get("classification") != "public":
            raise ProviderDisabledError("Extraccion Firecrawl no autorizada", provider="firecrawl")
        if firecrawl is None:
            raise MissingConfigError("Firecrawl no montado", provider="firecrawl")
        # The initial URL and returned URL are checked locally. Firecrawl's own
        # remote redirect/network path cannot be attested by this API; declare it.
        guard = EgressGuard()
        try:
            await guard.validate(url)
            outcome = await firecrawl.scrape(url, research_id=research_id, project_key=project_key,
                                            owner_id=job["owner_id"], deadline=kwargs.get("deadline"))
            if outcome.degradations:
                from research.acquisition.base import AcquisitionError
                gap = outcome.degradations[0]
                error = AcquisitionError(gap.detail, provider=gap.provider)
                error.coverage_reason, error.kind = gap.coverage_reason, gap.reason
                raise error
            await guard.validate(outcome.final_url)
        except EgressError:
            raise DestinationBlockedError("Destino no publico", provider="firecrawl") from None
        from research.evidence import content_hash_of, normalize_content
        return {"url": outcome.final_url, "text": outcome.text, "provider": "firecrawl",
                "access_method": "firecrawl", "observed_at": outcome.observed_at,
                "published_at": outcome.published_at, "content_hash": content_hash_of(normalize_content(outcome.text)),
                "remote_egress_unattested": True}
    reader = selected_read
    resources.http_reader = http_reader
    resources.discover = discover

    # Montura del broker. Se construye antes que el runtime porque la URL que el
    # worker necesita sale de la ruta real de la montura, no de una suposicion.
    read_log = BrokerReadLog()
    resources.read_log = read_log
    broker = build_research_broker_mount(
        settings=settings,
        capabilities=capabilities,
        deps=build_broker_operations(
            reader=reader,
            evidence=evidence,
            discover=discover,
            # Grounding pagado sigue apagado: el adaptador se expone y responde
            # `missing_config`, que es una respuesta honesta y no un fake.
            grounded=None,
            read_log=read_log,
        ),
    )
    resources.broker = broker

    broker_url = str(_setting(settings, "RESEARCH_BROKER_URL", "") or "")
    if not broker_url and broker is not None:
        broker_url = broker.url

    runtime = _build_runtime(settings, broker_url=broker_url)
    resources.runtime = runtime

    deps = ResearchDependencies(
        repo=repo,
        ledger=ledger,
        evidence=evidence,
        runtime=runtime,
        discover=discover,
        read=reader,
        capabilities=capabilities,
        broker_reads=read_log,
    )
    from research.deep_model import BudgetedDeepModel
    deps.deep_model = BudgetedDeepModel(settings, ledger)
    deps.provider_status = _provider_status(settings, ledger=ledger, discover=discover,
                                          firecrawl=firecrawl, deep_model=deps.deep_model, runtime=runtime)
    from research.events import ResearchEvents
    event_db = getattr(repo, "db", None)
    if event_db is not None:
        deps.events = ResearchEvents(event_db, evidence, retention_days=int(_setting(settings, "RESEARCH_REPORT_RETENTION_DAYS", 180)))
    service = ResearchService(settings=settings, deps=deps, clock=clock)
    return service, resources


async def build_social_broker(settings: Any, db: Any) -> tuple[Any | None, str]:
    """Broker de lectura de X, opcional: un directorio de sesiones invalido lo apaga.

    Devuelve `(broker, motivo)`; sin broker, la ruta social responde 503. El arranque
    del servicio entero no depende de el: antes un directorio con permisos 0755, un
    enlace simbolico en la ruta o una ruta dentro del repo/vault lanzaba la excepcion
    dentro del lifespan y el core completo (voz, tickets, Horizon) no arrancaba por
    una funcion opcional y apagada por defecto. Solo se registra el codigo del motivo.
    """
    root = _setting(settings, "RESEARCH_SOCIAL_SESSION_DIR", "")
    if not root:
        return None, "not_configured"
    try:
        from research.social import SocialBroker, SocialError, SocialSessions
        from research.social_x import XReader

        session_root = Path(root).expanduser().resolve()
        forbidden = [Path(__file__).resolve().parents[2]]
        for name in ("RESEARCH_WORKSPACE_DIR", "RESEARCH_EVIDENCE_DIR"):
            configured = _setting(settings, name, "")
            if configured:
                forbidden.append(Path(configured).expanduser().resolve())
        if any(session_root.is_relative_to(path) for path in forbidden) or "vault" in session_root.parts:
            reason = "session_dir_not_isolated"
        else:
            broker = SocialBroker(db, SocialSessions(root), adapter=XReader())
            await broker.initialize()
            return broker, "ready"
    except SocialError as exc:
        reason = exc.reason
    except Exception as exc:  # noqa: BLE001 - optional capability must not stop the service
        reason = "social_init_failed"
        logger.warning("Research social: fallo de inicializacion (%s)", type(exc).__name__)
    logger.warning("Research social deshabilitado: %s", reason)
    return None, reason


async def close_research_service(resources: ResearchRuntimeResources) -> None:
    await resources.aclose()
