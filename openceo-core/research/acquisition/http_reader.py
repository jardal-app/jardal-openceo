"""Lectura HTTP publica normalizada, con procedencia y evidencia previa
(TASK-2026-00060).

Que garantiza
=============
* **Egress del modulo de politicas.** No se abre un socket propio: toda lectura
  pasa por ``GuardedFetcher`` (``research/policies.py``), que valida el destino,
  fija la IP ya validada y vuelve a validar cada redireccion. Sin esto, un enlace
  del SERP o un redirect podria apuntar a la red interna o al endpoint de metadatos.
* **Limites duros.** Bytes descomprimidos, MIME autorizado y numero de redirects
  se aplican **antes** de guardar nada; un cuerpo comprimido enorme no llena disco.
* **Procedencia honesta.** Se registra la URL final, la fecha de observacion
  (descarga), la fecha de publicacion **solo si la fuente la declara** y el hash del
  cuerpo. Un ``304`` no descarga cuerpo pero **si** cuenta como comprobacion: se
  distingue ``last_checked_at`` de la fecha de descarga y se conserva la evidencia
  anterior en vez de sustituirla por vacio.
* **Normalizacion conservadora.** Solo se colapsa ruido de espacios y se descarta
  ``script``/``style``: no se recorta texto material para "limpiar" la pagina.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

from research.contracts import (
    AccessMethod,
    Classification,
    ResearchEvidence,
    RetentionPolicy,
    SourceType,
)
from research.policies import (
    EgressBlocked,
    EgressError,
    FetchAuthError,
    FetchError,
    FetchLimits,
    FetchProtocolError,
    FetchQuotaExhausted,
    FetchRateLimited,
    FetchTooLarge,
    FetchTransportError,
    FetchUnsupportedMedia,
    FetchedPage,
    GuardedFetcher,
)

from .base import (
    CircuitBreaker,
    CircuitOpenError,
    Clock,
    Deadline,
    DeadlineExceededError,
    Degradation,
    DestinationBlockedError,
    DiscoveryItem,
    DiscoveryResult,
    ProviderAuthError,
    ProviderProtocolError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderTooLarge,
    ProviderTransportError,
    ProviderUnsupportedMedia,
    SYSTEM_CLOCK,
    parse_datetime,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HTTP_LIMITS",
    "HttpReader",
    "PreviousReading",
    "ReadOutcome",
    "normalize_text",
    "open_publications",
    "parse_http_datetime",
]

DEFAULT_HTTP_LIMITS = FetchLimits()
DEFAULT_PROVIDER = "http"

# Claves de metadatos que declaran fecha de publicacion. Solo se aceptan valores
# que la propia pagina publica; no se deduce fecha por heuristica.
_PUBLISHED_META_KEYS = (
    "article:published_time",
    "og:published_time",
    "datepublished",
    "date",
    "pubdate",
    "publish-date",
    "dcterms.date",
    "dc.date",
    "dc.date.issued",
    "sailthru.date",
    "parsely-pub-date",
    "timestamp",
)


def parse_http_datetime(value: str | None) -> datetime | None:
    """Alias explicito de ``base.parse_datetime`` para fechas de pagina."""
    return parse_datetime(value)


def normalize_text(text: str) -> str:
    """Colapsa ruido de espacios sin borrar contenido material.

    Se conservan los saltos de linea (estructura de parrafos) y **todo** el texto
    visible; solo se normalizan espacios repetidos y lineas en blanco excesivas.
    """
    normalized = re.sub(r"[^\S\n]+", " ", text)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


@dataclass(frozen=True)
class PreviousReading:
    """Evidencia y cuerpo de una lectura anterior, para responder a un ``304``.

    Sin esto, un ``304`` se interpretaria como "sin contenido" y destruiria la
    evidencia valida que ya se tenia.
    """

    evidence: ResearchEvidence
    text: str = ""
    body: bytes = b""
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ReadOutcome:
    """Lectura de una URL publica con su procedencia.

    ``observed_at`` es la fecha de **descarga** del cuerpo (``None`` en un ``304``);
    ``last_checked_at`` es cuando se comprobo la URL, haya cuerpo o no. No son lo
    mismo y no se colapsan.
    """

    url: str
    final_url: str
    access_method: AccessMethod
    evidence: ResearchEvidence | None
    text: str = ""
    body: bytes = b""
    title: str | None = None
    published_at: datetime | None = None
    not_modified: bool = False
    last_checked_at: datetime | None = None
    observed_at: datetime | None = None
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    redirects: int = 0
    degradations: tuple[Degradation, ...] = ()

    @property
    def ok(self) -> bool:
        return self.evidence is not None

    def to_payload(self) -> dict[str, Any]:
        # Lista blanca para el modelo/almacen: el cuerpo crudo no viaja aqui.
        return {
            "url": self.url,
            "final_url": self.final_url,
            "access_method": self.access_method.value,
            "title": self.title,
            "text": self.text,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None
            ),
            "content_hash": self.content_hash,
            "not_modified": self.not_modified,
            "retention_policy": (
                self.evidence.retention_policy.value if self.evidence else None
            ),
            "degradations": [d.to_payload() for d in self.degradations],
        }


class _HtmlDocumentParser(HTMLParser):
    """Extrae titulo, fecha declarada y texto visible. No descarga nada.

    ``HTMLParser`` no resuelve recursos externos: ni imagenes, ni hojas de estilo,
    ni DTD. Ese es justo el motivo de no usar un navegador aqui.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._meta: dict[str, str] = {}
        self._time_datetime: str | None = None
        self._in_title = False
        self._in_head = False
        self._skip_depth = 0

    @property
    def title(self) -> str | None:
        value = " ".join(" ".join(self._title_parts).split())
        return value or None

    @property
    def text(self) -> str:
        return normalize_text("".join(self._text_parts))

    def published_hint(self) -> str | None:
        for key in _PUBLISHED_META_KEYS:
            value = self._meta.get(key)
            if value:
                return value
        return self._time_datetime

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "head":
            self._in_head = True
            return
        if name == "title":
            self._in_title = True
            return
        if name in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return
        if name == "meta":
            data = {k.lower(): v for k, v in attrs if k}
            key = (data.get("property") or data.get("name") or data.get("itemprop") or "").lower()
            content = data.get("content")
            if key and content:
                self._meta.setdefault(key, content)
            return
        if name == "time" and self._time_datetime is None:
            data = {k.lower(): v for k, v in attrs if k}
            if data.get("datetime"):
                self._time_datetime = data["datetime"]

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "head":
            self._in_head = False
        elif name == "title":
            self._in_title = False
        elif name in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._in_head and self._skip_depth == 0:
            self._text_parts.append(data)


def _decode_body(body: bytes, content_type: str | None) -> str:
    charset = "utf-8"
    if content_type and "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _evidence_id(content_hash: str | None) -> str:
    if content_hash:
        return f"ev_{content_hash[:16]}"
    return f"ev_{uuid4().hex[:16]}"


def _degradation_from_fetch_error(exc: FetchError, *, provider: str) -> Degradation:
    """Traduce el vocabulario de ``policies`` al de adquisicion, sin perder causa."""
    if isinstance(exc, EgressBlocked):
        return DestinationBlockedError(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchAuthError):
        return ProviderAuthError(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchQuotaExhausted):
        return ProviderQuotaExhausted(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchRateLimited):
        return ProviderRateLimited(
            str(exc), provider=provider, retry_after=exc.retry_after
        ).degradation()
    if isinstance(exc, FetchTooLarge):
        return ProviderTooLarge(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchUnsupportedMedia):
        return ProviderUnsupportedMedia(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchProtocolError):
        return ProviderProtocolError(str(exc), provider=provider).degradation()
    if isinstance(exc, FetchTransportError):
        return ProviderTransportError(str(exc), provider=provider).degradation()
    return ProviderProtocolError(str(exc), provider=provider).degradation()


class HttpReader:
    """Lee una URL publica y produce evidencia con procedencia."""

    access_method = AccessMethod.HTTP
    provider = DEFAULT_PROVIDER

    def __init__(
        self,
        *,
        fetcher: GuardedFetcher,
        limits: FetchLimits | None = None,
        clock: Clock | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        max_attempts: int = 2,
        backoff_seconds: float = 0.5,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._limits = limits or DEFAULT_HTTP_LIMITS
        self._clock = clock or SYSTEM_CLOCK
        # Reloj de pared para fechas de evidencia. Es distinto del reloj monotono
        # del deadline: `last_checked_at` y `observed_at` son instantes de calendario.
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._max_attempts = max(1, int(max_attempts))
        self._backoff = float(backoff_seconds)
        self._breaker = breaker

    async def read(
        self,
        url: str,
        *,
        research_id: str,
        project_key: str,
        owner_id: str | None = None,
        previous: PreviousReading | None = None,
        classification: Classification = Classification.PUBLIC,
        source_type: SourceType = SourceType.UNKNOWN,
        retention_policy: RetentionPolicy = RetentionPolicy.PUBLIC_7D,
        access_method: AccessMethod | None = None,
        deadline: Deadline | None = None,
        limits: FetchLimits | None = None,
    ) -> ReadOutcome:
        """Lee ``url`` pasando por el guard de egress.

        El reintento acotado solo cubre fallos de transporte de una fuente
        **gratuita**: no hay doble gasto posible. Un 401/403/402/429 se degrada sin
        reintento o con la espera indicada.
        """
        effective = limits or self._limits
        if access_method is not None:
            method = access_method
        else:
            method = self.access_method
        etag = previous.etag if previous else None
        last_modified = previous.last_modified if previous else None
        degradation: Degradation | None = None

        for index in range(1, self._max_attempts + 1):
            if deadline is not None and deadline.expired:
                degradation = DeadlineExceededError(
                    "Deadline agotado antes de leer", provider=self.provider
                ).degradation()
                break
            if self._breaker is not None:
                try:
                    self._breaker.check(self.provider, provider=self.provider)
                except CircuitOpenError as exc:
                    degradation = exc.degradation()
                    break
            try:
                per_call = replace(
                    effective,
                    timeout_seconds=(
                        deadline.phase_timeout(effective.timeout_seconds, provider=self.provider)
                        if deadline is not None
                        else effective.timeout_seconds
                    ),
                )
            except DeadlineExceededError as exc:
                degradation = exc.degradation()
                break
            try:
                page = await self._fetcher.fetch(
                    url, etag=etag, last_modified=last_modified, limits=per_call
                )
            except FetchRateLimited as exc:
                wait = exc.retry_after
                if (
                    wait is not None
                    and index < self._max_attempts
                    and self._wait_fits(deadline, wait)
                ):
                    if self._breaker is not None:
                        self._breaker.record_failure(self.provider)
                    if wait > 0:
                        await self._clock.sleep(self._bounded(deadline, wait))
                    continue
                degradation = _degradation_from_fetch_error(exc, provider=self.provider)
                break
            except FetchTransportError as exc:
                if index < self._max_attempts and self._wait_fits(deadline, self._backoff):
                    if self._breaker is not None:
                        self._breaker.record_failure(self.provider)
                    if self._backoff > 0:
                        await self._clock.sleep(self._bounded(deadline, self._backoff))
                    continue
                degradation = _degradation_from_fetch_error(exc, provider=self.provider)
                break
            except FetchError as exc:
                degradation = _degradation_from_fetch_error(exc, provider=self.provider)
                break
            except EgressBlocked as exc:
                # El guard bloqueo un redirect o un destino prohibido **antes** de
                # conectar. Es una denegacion de politica, no un fallo transitorio:
                # no se reintenta.
                degradation = DestinationBlockedError(str(exc), provider=self.provider).degradation()
                break
            except EgressError as exc:
                degradation = DestinationBlockedError(str(exc), provider=self.provider).degradation()
                break

            if self._breaker is not None:
                self._breaker.record_success(self.provider)
            return self._build_outcome(
                page,
                url=url,
                previous=previous,
                classification=classification,
                source_type=source_type,
                retention_policy=retention_policy,
                access_method=method,
                owner_id=owner_id,
                project_key=project_key,
            )

        return ReadOutcome(
            url=url,
            final_url=url,
            access_method=method,
            evidence=None,
            degradations=(degradation,) if degradation else (),
            last_checked_at=self._clock_now(),
        )

    # ------------------------------------------------------------- internos

    def _clock_now(self) -> datetime:
        return self._wall_clock()

    def _wait_fits(self, deadline: Deadline | None, delay: float) -> bool:
        if deadline is None:
            return delay <= 5.0
        return deadline.allows(delay)

    def _bounded(self, deadline: Deadline | None, delay: float) -> float:
        if deadline is None:
            return max(0.0, delay)
        return max(0.0, min(delay, deadline.remaining))

    def _build_outcome(
        self,
        page: FetchedPage,
        *,
        url: str,
        previous: PreviousReading | None,
        classification: Classification,
        source_type: SourceType,
        retention_policy: RetentionPolicy,
        access_method: AccessMethod,
        owner_id: str | None,
        project_key: str,
    ) -> ReadOutcome:
        checked_at = self._clock_now()

        if page.not_modified:
            if previous is None or previous.evidence is None:
                # Un 304 sin evidencia previa no es "nada cambio": es una respuesta
                # inutilizable. Se declara en vez de inventar una pagina vacia.
                return ReadOutcome(
                    url=url,
                    final_url=page.final_url,
                    access_method=access_method,
                    evidence=None,
                    not_modified=True,
                    last_checked_at=checked_at,
                    etag=page.etag,
                    last_modified=page.last_modified or (previous.last_modified if previous else None),
                    redirects=page.redirects,
                    degradations=(
                        ProviderProtocolError(
                            "304 sin evidencia previa que conservar", provider=self.provider
                        ).degradation(),
                    ),
                )
            evidence = previous.evidence
            return ReadOutcome(
                url=url,
                final_url=page.final_url,
                access_method=access_method,
                evidence=evidence,
                text=previous.text,
                body=previous.body,
                title=evidence.title,
                published_at=evidence.published_at,
                not_modified=True,
                # La fecha de descarga NO se toca: sigue siendo la de la lectura
                # original. Solo avanza la comprobacion.
                last_checked_at=checked_at,
                observed_at=evidence.observed_at,
                content_hash=evidence.content_hash,
                etag=page.etag or previous.etag,
                last_modified=page.last_modified or previous.last_modified,
                redirects=page.redirects,
            )

        decoded = _decode_body(page.body, page.content_type)
        title: str | None = None
        published_at: datetime | None = None
        if page.content_type in {"text/html", "application/xhtml+xml"}:
            parser = _HtmlDocumentParser()
            parser.feed(decoded)
            parser.close()
            title = parser.title
            text = parser.text
            published_at = parse_datetime(parser.published_hint())
        else:
            text = normalize_text(decoded)
        # Last-Modified dates a representation update, not its publication
        # (RFC 9110 §8.8.2). Keep it for conditional HTTP, never invent published_at.

        evidence = ResearchEvidence(
            id=_evidence_id(page.content_hash),
            url=url,
            access_method=access_method,
            visibility=classification,
            source_type=source_type,
            title=title,
            published_at=published_at,
            observed_at=page.observed_at,
            content_hash=page.content_hash,
            retention_policy=retention_policy,
            project_key=project_key,
            owner_id=owner_id,
        )
        return ReadOutcome(
            url=url,
            final_url=page.final_url,
            access_method=access_method,
            evidence=evidence,
            text=text,
            body=page.body,
            title=title,
            published_at=published_at,
            not_modified=False,
            last_checked_at=checked_at,
            observed_at=page.observed_at,
            content_hash=page.content_hash,
            etag=page.etag,
            last_modified=page.last_modified,
            redirects=page.redirects,
        )


async def open_publications(
    reader: HttpReader,
    discovery: DiscoveryResult | Sequence[DiscoveryItem],
    *,
    research_id: str,
    project_key: str,
    owner_id: str | None = None,
    limit: int | None = None,
    source_type: SourceType = SourceType.UNKNOWN,
    retention_policy: RetentionPolicy = RetentionPolicy.PUBLIC_7D,
    classification: Classification = Classification.PUBLIC,
    deadline: Deadline | None = None,
) -> tuple[ReadOutcome, ...]:
    """Abre las publicaciones originales descubiertas por un buscador.

    Este es el paso que convierte un resultado de proveedor (transitorio) en
    evidencia leida: se visita la fuente y se guarda **su** texto, con la retencion
    de esa fuente, en vez de conservar el snippet del buscador.
    """
    items: Iterable[DiscoveryItem]
    if isinstance(discovery, DiscoveryResult):
        items = discovery.items
    else:
        items = discovery
    selected = list(items)
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    outcomes: list[ReadOutcome] = []
    for item in selected:
        outcomes.append(
            await reader.read(
                item.url,
                research_id=research_id,
                project_key=project_key,
                owner_id=owner_id,
                source_type=source_type,
                retention_policy=retention_policy,
                classification=classification,
                deadline=deadline,
            )
        )
    return tuple(outcomes)
