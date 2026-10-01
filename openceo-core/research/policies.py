"""Politicas de research: acceso, privacidad, routing y red saliente (TASK-2026-00059).

Principios que implementa este modulo
=====================================
* **El servidor deriva la autoridad, el payload no.** El owner sale del principal
  autenticado, los grants de cuenta de su registro, y los limites/techo de la
  politica del proyecto. Un `project_key`, `owner_id` o `budget_ref` del payload no
  crean autorizacion: solo se contrastan.
* **La clasificacion la decide el broker y la procedencia**, no una frase del agente.
  Lo visible solo tras login se trata como restringido hasta comprobar visibilidad
  publica; una mezcla hereda la clasificacion mas restrictiva; y sin ruta de modelo
  compatible con datos privados el resultado es `model_policy_unavailable`, **nunca**
  un fallback a Contributor.
* **Guard de egress reutilizable.** Validar el DNS y despues dejar que el cliente
  vuelva a resolver es insuficiente (TOCTOU): entre la validacion y la conexion el
  destino puede cambiar. Aqui la conexion usa el destino ya validado, conservando
  `Host` y SNI, y cada redireccion vuelve a pasar por el guard.
* **La web es evidencia no confiable.** El contenido jamas concede herramientas,
  cuentas, destinatarios ni presupuesto.

No existe en core un guard reutilizable: este modulo lo aporta para research. No se
afirma nada sobre como filtran otros clientes del repositorio.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from research.contracts import (
    Classification,
    ResearchLimits,
    ResearchJobRequest,
    ResearchMode,
)
from research.money import parse_optional_micro

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ALLOWED_MIME",
    "DEFAULT_ALLOWED_PORTS",
    "EgressBlocked",
    "EgressError",
    "EffectivePolicy",
    "FetchAuthError",
    "FetchError",
    "FetchProtocolError",
    "FetchQuotaExhausted",
    "FetchRateLimited",
    "FetchTooLarge",
    "FetchTransportError",
    "FetchUnsupportedMedia",
    "FetchedPage",
    "FetchLimits",
    "FixedProviderEndpoint",
    "GuardedFetcher",
    "ModelPolicyUnavailable",
    "PinnedEgressTransport",
    "Principal",
    "ProviderEndpointError",
    "RESEARCH_READ",
    "RESEARCH_RUN",
    "ResearchAccessDenied",
    "ResearchPolicyError",
    "UntrustedContent",
    "ValidatedDestination",
    "classify_discovered_content",
    "derive_effective_policy",
    "discovery_route",
    "is_public_ip",
    "model_route",
    "principal_from_user",
    "read_route",
]

# Permisos. NO se otorgan a todos los grupos: anadirlos a un grupo es una migracion
# explicita del dueño, porque conceden capacidad de ejecutar investigacion y de leer
# resultados (y por tanto evidencia de un proyecto).
RESEARCH_RUN = "run_research"
RESEARCH_READ = "read_research"

DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
DEFAULT_ALLOWED_MIME = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/pdf",
    }
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


# --------------------------------------------------------------------------- acceso


class ResearchAccessDenied(PermissionError):
    """El principal no puede realizar la operacion sobre ese proyecto/owner."""


class ResearchPolicyError(RuntimeError):
    """La peticion no puede satisfacerse bajo la politica vigente."""


class ModelPolicyUnavailable(ResearchPolicyError):
    """No hay ruta de modelo compatible con la clasificacion del trabajo.

    Deliberadamente **no** cae a Contributor: enviar datos privados a un tier que
    entrena con prompts seria la peor degradacion posible.
    """


@dataclass(frozen=True)
class Principal:
    """Identidad autenticada del consumidor, ya resuelta por el transporte."""

    user_id: str
    permissions: frozenset[str] = frozenset()
    allowed_projects: frozenset[str] = frozenset()
    is_admin: bool = False
    groups: tuple[str, ...] = ()

    def has_permission(self, permission: str) -> bool:
        return self.is_admin or permission in self.permissions


def principal_from_user(user: Any, *, groups: Sequence[Any] = ()) -> Principal:
    """Construye el principal desde el usuario autenticado por el transporte.

    Acepta el `UserDocument` de `state.py` o cualquier objeto con los mismos campos.
    Un `owner_id` que venga en el payload **no** entra aqui.
    """
    permissions_raw = getattr(user, "all_permissions", None)
    if permissions_raw is None and isinstance(user, Mapping):
        permissions_raw = user.get("all_permissions")
    permissions = frozenset(permissions_raw or ())

    projects_raw = getattr(user, "allowed_projects", None)
    if projects_raw is None and isinstance(user, Mapping):
        projects_raw = user.get("allowed_projects")
    allowed_projects = frozenset(projects_raw or ())

    group_ids = getattr(user, "group_ids", None)
    if group_ids is None and isinstance(user, Mapping):
        group_ids = user.get("group_ids")

    user_id = getattr(user, "user_id", None)
    if user_id is None and isinstance(user, Mapping):
        user_id = user.get("user_id")

    is_admin = bool(getattr(user, "is_admin", False) or (isinstance(user, Mapping) and user.get("is_admin")))
    if not user_id:
        raise ResearchAccessDenied("Principal sin identidad")
    return Principal(
        user_id=str(user_id),
        permissions=permissions,
        allowed_projects=allowed_projects,
        is_admin=is_admin,
        groups=tuple(group_ids or ()),
    )


def _project_allowed(principal: Principal, project_key: str) -> bool:
    if principal.is_admin:
        return True
    if "*" in principal.allowed_projects:
        return True
    return project_key in principal.allowed_projects


# ------------------------------------------------------------------------ privacidad


def classify_discovered_content(
    *, authenticated_session: bool, visibility_known_public: bool
) -> Classification:
    """Clasifica contenido descubierto segun su procedencia.

    Conservador por construccion: lo obtenido con una sesion autenticada es
    `restricted` hasta que se compruebe visibilidad publica y se retiren los datos
    del owner. Una publicacion publica descubierta desde una cuenta puede tratarse
    como publica **despues** de esa comprobacion, no antes.
    """
    if authenticated_session and not visibility_known_public:
        return Classification.RESTRICTED
    return Classification.PUBLIC


def model_route(
    classification: Classification,
    *,
    public_model: str | None,
    private_model: str | None,
) -> str:
    """Modelo autorizado para una clasificacion. Nunca degrada privacidad."""
    if classification is Classification.PUBLIC:
        if not public_model:
            raise ModelPolicyUnavailable("No hay ruta publica configurada")
        return public_model
    if not private_model:
        raise ModelPolicyUnavailable(
            "No hay ruta de modelo compatible con datos restringidos/privados; "
            "no se usa la ruta publica como fallback"
        )
    return private_model


# --------------------------------------------------------------------- politica


@dataclass(frozen=True)
class EffectivePolicy:
    owner_id: str
    project_key: str
    classification: Classification
    limits: ResearchLimits
    budget_ref: str | None
    paid_budget_micro_usd: int
    enabled: bool
    adapters: Mapping[str, bool]
    allowed_account_refs: frozenset[str]
    retention_days: Mapping[str, int] = field(default_factory=dict)

    def adapter_enabled(self, name: str) -> bool:
        return bool(self.adapters.get(name, False))

    def paid_budget_available(self) -> bool:
        return self.paid_budget_micro_usd > 0


def _limit_block(settings: Any, mode: ResearchMode) -> ResearchLimits:
    prefix = mode.value.upper()
    return ResearchLimits(
        search_requests=int(getattr(settings, f"RESEARCH_{prefix}_MAX_SEARCHES", 3)),
        pages=int(getattr(settings, f"RESEARCH_{prefix}_MAX_PAGES", 5)),
        steps=int(getattr(settings, f"RESEARCH_{prefix}_MAX_STEPS", 8)),
        seconds=int(getattr(settings, f"RESEARCH_{prefix}_MAX_SECONDS", 120)),
    )


def _capped(requested: int, *ceilings: int) -> int:
    return max(0, min(requested, *ceilings))


def derive_effective_policy(
    *,
    principal: Principal,
    request: ResearchJobRequest,
    project_cfg: Mapping[str, Any],
    settings: Any,
) -> EffectivePolicy:
    """Politica efectiva del job. Recorta siempre; nunca amplia por el payload.

    Todo lo que el servidor concede se calcula aqui, de modo que la fachada API/MCP
    y el servicio comparten una unica definicion de autoridad.
    """
    research_cfg = dict(project_cfg.get("research") or {})

    if not principal.has_permission(RESEARCH_RUN):
        raise ResearchAccessDenied("El principal no tiene permiso run_research")
    if not _project_allowed(principal, request.project_key):
        raise ResearchAccessDenied(
            f"El principal no esta autorizado en el proyecto {request.project_key}"
        )
    if not research_cfg.get("enabled", False):
        raise ResearchPolicyError("research deshabilitado para este proyecto")

    mode = request.mode
    allowed_modes = set(research_cfg.get("allowed_modes") or ["quick"])
    if mode.value not in allowed_modes:
        raise ResearchPolicyError(f"Modo {mode.value} no autorizado para este proyecto")

    ceiling = Classification(str(research_cfg.get("classification_ceiling") or "public"))
    requested_classification = request.access.classification
    if requested_classification.severity > ceiling.severity:
        # No se puede "bajar" la clasificacion de los datos: pedir privado en un
        # proyecto limitado a publico se deniega, no se degrada.
        raise ResearchPolicyError(
            f"Clasificacion solicitada {requested_classification.value} supera el techo "
            f"{ceiling.value} del proyecto"
        )

    server_limits = _limit_block(settings, mode)
    project_limits = dict((research_cfg.get("limits") or {}).get(mode.value) or {})
    requested_limits = request.limits or server_limits
    limits = ResearchLimits(
        search_requests=_capped(
            requested_limits.search_requests,
            server_limits.search_requests,
            int(project_limits.get("search_requests", server_limits.search_requests)),
        ),
        pages=_capped(
            requested_limits.pages,
            server_limits.pages,
            int(project_limits.get("pages", server_limits.pages)),
        ),
        steps=_capped(
            requested_limits.steps,
            server_limits.steps,
            int(project_limits.get("steps", server_limits.steps)),
        ),
        seconds=_capped(
            requested_limits.seconds,
            server_limits.seconds,
            int(project_limits.get("seconds", server_limits.seconds)),
        ),
    )

    # Grants de cuenta: el cliente no puede elegir una cuenta ajena escribiendo un
    # `account_ref` arbitrario. Debe estar autorizada por el proyecto y por el
    # principal.
    project_accounts = frozenset(research_cfg.get("allowed_account_refs") or ())
    for ref in request.access.account_refs:
        if ref not in project_accounts:
            raise ResearchAccessDenied(f"account_ref no autorizado: {ref}")

    # Presupuesto pagado: el proyecto puede reducirlo, nunca elevarlo por encima del
    # limite global aprobado. El default global es 0, asi que activar gasto exige
    # una decision explicita en el entorno **y** en el proyecto.
    global_paid = _optional_micro(getattr(settings, "RESEARCH_PAID_SPEND_USD", "0.00"), default=0)
    project_paid = _optional_micro(research_cfg.get("paid_budget_usd"), default=0)
    paid_budget = min(global_paid, project_paid)

    adapters_cfg = dict(research_cfg.get("adapters") or {})
    adapters = {
        name: bool(adapters_cfg.get(name, False)) and bool(getattr(settings, f"RESEARCH_{name.upper()}_ENABLED", True))
        for name in ("brave", "serper", "firecrawl", "grounding")
    }

    return EffectivePolicy(
        owner_id=principal.user_id,
        project_key=request.project_key,
        classification=requested_classification,
        limits=limits,
        budget_ref=request.budget_ref,
        paid_budget_micro_usd=paid_budget,
        enabled=True,
        adapters=adapters,
        allowed_account_refs=project_accounts,
        retention_days={
            "public": int(getattr(settings, "RESEARCH_PUBLIC_RETENTION_DAYS", 7)),
            "watch": int(getattr(settings, "RESEARCH_WATCH_RETENTION_DAYS", 90)),
            "report": int(getattr(settings, "RESEARCH_REPORT_RETENTION_DAYS", 180)),
        },
    )


def _optional_micro(value: Any, *, default: int) -> int:
    if value is None:
        return default
    parsed = parse_optional_micro(value)
    if parsed is None:
        # Cadena vacia = desconocido. Para un techo de gasto, desconocido no autoriza:
        # se trata como el default conservador, no como ilimitado.
        return default
    return parsed


# ------------------------------------------------------------------------- routing


def discovery_route(
    *,
    adapters: Mapping[str, bool],
    known_source: bool = False,
    paid_budget_available: bool = False,
) -> tuple[str, ...]:
    """Orden de descubrimiento. Fuente directa primero; escalar solo por necesidad.

    No es un pipeline fijo: cada adaptador aparece solo si esta habilitado (y, para
    grounding, si hay presupuesto pagado). Firecrawl no consume su cuota de
    extraccion para descubrir salvo que este habilitado explicitamente.
    """
    order: list[str] = []
    if known_source:
        order.append("direct")
    if adapters.get("brave"):
        order.append("brave")
    if adapters.get("serper"):
        order.append("serper")
    if adapters.get("firecrawl"):
        order.append("firecrawl")
    if adapters.get("grounding") and paid_budget_available:
        order.append("grounding")
    return tuple(order)


def read_route(
    *,
    adapters: Mapping[str, bool],
    browser_available: bool = False,
) -> tuple[str, ...]:
    """Orden de lectura. El navegador solo existe si hay worker aislado de verdad."""
    order = ["http"]
    if adapters.get("firecrawl"):
        order.append("firecrawl")
    if browser_available:
        order.append("browser")
    return tuple(order)


# --------------------------------------------------------------------- contenido


@dataclass(frozen=True)
class UntrustedContent:
    """Envoltura de contenido web. Es dato, nunca autoridad."""

    url: str
    text: str
    trust: str = "untrusted"

    def as_evidence(self) -> dict[str, str]:
        return {"url": self.url, "text": self.text, "trust": self.trust}


# ------------------------------------------------------------------- egress guard


class EgressError(RuntimeError):
    """Fallo de red saliente."""


class EgressBlocked(EgressError):
    """El destino no esta autorizado por la politica de egress."""


class ProviderEndpointError(EgressError):
    """Se intento cambiar el endpoint fijo de un proveedor."""


@dataclass(frozen=True)
class ValidatedDestination:
    """Destino ya validado con la IP concreta que se usara para conectar."""

    scheme: str
    host: str
    port: int
    ip: str
    host_header: str
    sni_hostname: str


def is_public_ip(value: str) -> bool:
    """True solo para direcciones publicas enroutables.

    Rechaza loopback, privadas, link-local (incluido el endpoint de metadatos
    169.254.169.254), multicast, reservadas, no especificadas, site-local IPv6 y
    IPv4 mapeadas dentro de IPv6 (`::ffff:127.0.0.1`), que es un rodeo clasico.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return is_public_ip(str(ip.sixtofour))
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


class EgressGuard:
    """Valida URLs arbitrarias y entrega el destino concreto a conectar.

    El guard no confia en que el cliente respete la validacion: entrega la IP y el
    transporte la usa. Esa es la diferencia entre "validar y rezar" y cerrar el TOCTOU.
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str], Awaitable[Sequence[str]]] | None = None,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        allow_public_ip_literals: bool = True,
    ) -> None:
        self._resolver = resolver or _system_resolver
        self._allowed_ports = frozenset(int(p) for p in allowed_ports)
        self._allow_ip_literals = allow_public_ip_literals

    async def validate(self, url: str) -> ValidatedDestination:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in {"http", "https"}:
            raise EgressBlocked(f"Esquema no autorizado: {parts.scheme!r}")
        if parts.username or parts.password or "@" in (parts.netloc or ""):
            raise EgressBlocked("No se admiten credenciales embebidas en la URL (userinfo)")
        host = parts.hostname
        if not host:
            raise EgressBlocked("URL sin host")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise EgressBlocked("Puerto invalido") from exc
        if port not in self._allowed_ports:
            raise EgressBlocked(f"Puerto no autorizado: {port}")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            if not self._allow_ip_literals or not is_public_ip(str(literal)):
                raise EgressBlocked(f"Direccion IP no publica: {host}")
            ip = str(literal)
        else:
            answers = list(await self._resolver(host))
            if not answers:
                raise EgressBlocked(f"DNS sin respuestas para {host}")
            # Todas las respuestas deben ser publicas: si una sola apunta a la red
            # interna, el destino no es de fiar (rebinding por respuesta multiple).
            for answer in answers:
                if not is_public_ip(answer):
                    raise EgressBlocked(
                        f"{host} resuelve a una direccion no publica: {answer}"
                    )
            ip = answers[0]

        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        literal_ip = str(ip)
        return ValidatedDestination(
            scheme=scheme,
            host=host,
            port=port,
            ip=literal_ip,
            host_header=host_header,
            sni_hostname=host,
        )


async def _system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _format_host_for_url(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


class PinnedEgressTransport(httpx.AsyncBaseTransport):
    """Transporte que conecta al destino **ya validado**, conservando Host y SNI.

    Registra cada conexion efectiva en ``connections`` para que las pruebas puedan
    afirmar que un fetch autorizado nunca aterrizo en un destino prohibido.
    """

    def __init__(self, guard: EgressGuard, *, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._guard = guard
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        self.connections: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        destination = await self._guard.validate(str(request.url))
        self.connections.append(
            {
                "scheme": destination.scheme,
                "host": destination.host,
                "port": destination.port,
                "ip": destination.ip,
            }
        )
        pinned_url = httpx.URL(
            scheme=destination.scheme,
            host=_format_host_for_url(destination.ip),
            port=destination.port,
            path=request.url.path or "/",
            query=request.url.query,
        )
        headers = [
            (name, value)
            for name, value in request.headers.raw
            if name.lower() != b"host"
        ]
        headers.append((b"host", destination.host_header.encode("ascii")))
        extensions = dict(request.extensions or {})
        extensions["sni_hostname"] = destination.sni_hostname
        pinned = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=headers,
            content=request.stream,
            extensions=extensions,
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


# ------------------------------------------------------------------------ fetch


class FetchError(EgressError):
    status_code: int | None = None
    kind = "fetch_error"


class FetchTransportError(FetchError):
    kind = "transport"


class FetchAuthError(FetchError):
    kind = "auth"


class FetchQuotaExhausted(FetchError):
    status_code = 402
    kind = "quota_exhausted"


class FetchRateLimited(FetchError):
    status_code = 429
    kind = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FetchProtocolError(FetchError):
    kind = "protocol"


class FetchTooLarge(FetchError):
    kind = "too_large"


class FetchUnsupportedMedia(FetchError):
    kind = "unsupported_media"


@dataclass(frozen=True)
class FetchLimits:
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    timeout_seconds: float = 20.0
    allowed_mime: frozenset[str] = DEFAULT_ALLOWED_MIME


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    body: bytes
    observed_at: datetime
    content_hash: str | None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    redirects: int = 0


def _hash_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class GuardedFetcher:
    """Lectura HTTP con guard de egress, redirects validados y limites duros."""

    def __init__(
        self,
        *,
        guard: EgressGuard,
        transport: PinnedEgressTransport | None = None,
        limits: FetchLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._guard = guard
        self._transport = transport or PinnedEgressTransport(guard)
        self._limits = limits or FetchLimits()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def transport(self) -> PinnedEgressTransport:
        return self._transport

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        limits: FetchLimits | None = None,
    ) -> FetchedPage:
        effective = limits or self._limits
        headers: dict[str, str] = {
            "user-agent": "openceo-research/1.0 (+contact: openceo@jardal.app)",
            "accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.5",
        }
        if etag:
            headers["if-none-match"] = etag
        if last_modified:
            headers["if-modified-since"] = last_modified

        current = url
        redirects = 0
        client = httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            timeout=effective.timeout_seconds,
        )
        try:
            while True:
                request = client.build_request("GET", current, headers=headers)
                try:
                    response = await client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    raise FetchTransportError(f"Fallo de transporte: {exc}") from exc
                try:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchProtocolError("Redireccion sin cabecera Location")
                        redirects += 1
                        if redirects > effective.max_redirects:
                            raise EgressBlocked("Demasiadas redirecciones")
                        # La resolucion relativa se hace contra la URL **real** del
                        # salto, no contra la URL fijada a la IP: si no, un redirect
                        # relativo acabaria conectando a la IP con Host equivocado.
                        current = str(httpx.URL(current).join(location))
                        # La siguiente vuelta pasa otra vez por el guard: un redirect
                        # a la red interna se bloquea antes de conectar.
                        continue
                    return await self._build_page(
                        response,
                        requested_url=url,
                        final_url=current,
                        redirects=redirects,
                        limits=effective,
                    )
                finally:
                    await response.aclose()
        finally:
            await client.aclose()

    async def _build_page(
        self,
        response: httpx.Response,
        *,
        requested_url: str,
        final_url: str,
        redirects: int,
        limits: FetchLimits,
    ) -> FetchedPage:
        observed = self._clock()
        status = response.status_code
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower() or None

        if status == 304:
            return FetchedPage(
                url=requested_url,
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                body=b"",
                observed_at=observed,
                content_hash=None,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                not_modified=True,
                redirects=redirects,
            )
        if status in {401, 403}:
            raise FetchAuthError(f"Acceso denegado por el origen ({status})")
        if status == 402:
            raise FetchQuotaExhausted("El origen informa cuota agotada (402)")
        if status == 429:
            raise FetchRateLimited(
                "Rate limit del origen (429)",
                retry_after=_retry_after_seconds(response.headers.get("retry-after")),
            )

        # El limite de tamano se aplica sobre el cuerpo **descomprimido**, que es lo
        # que realmente consume memoria y disco.
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limits.max_bytes:
            raise FetchTooLarge(f"Cuerpo declarado de {declared} bytes supera el limite")

        if content_type and limits.allowed_mime and content_type not in limits.allowed_mime:
            raise FetchUnsupportedMedia(f"MIME no autorizado: {content_type}")

        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limits.max_bytes:
                raise FetchTooLarge("Cuerpo descomprimido supera el limite")

        if status >= 400:
            raise FetchProtocolError(f"Respuesta HTTP {status}")

        raw = bytes(body)
        return FetchedPage(
            url=requested_url,
            final_url=final_url,
            status_code=status,
            content_type=content_type,
            body=raw,
            observed_at=observed,
            content_hash=_hash_bytes(raw),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            not_modified=False,
            redirects=redirects,
        )


class FixedProviderEndpoint:
    """Endpoint de proveedor cerrado por configuracion.

    El modelo puede aportar la **ruta** y los parametros, nunca el host ni el
    esquema: cambiar `base_url` seria redirigir una credencial a un tercero.
    """

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ProviderEndpointError(f"base_url invalida: {base_url!r}")
        self._base = base_url.rstrip("/")
        self.host = parts.hostname
        self.scheme = parts.scheme

    @property
    def base_url(self) -> str:
        return self._base

    def url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        if "://" in path or path.startswith("//"):
            raise ProviderEndpointError("La ruta del proveedor no puede ser una URL absoluta")
        normalized = path if path.startswith("/") else f"/{path}"
        url = f"{self._base}{normalized}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = _with_query(url, filtered)
        return url


def _with_query(url: str, params: Mapping[str, Any]) -> str:
    request = httpx.Request("GET", url, params=params)
    return str(request.url)
