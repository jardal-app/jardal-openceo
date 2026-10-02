"""Vigilancia incremental de fuentes y descubrimiento periodico (TASK-2026-00069).

Que resuelve
============
Una suscripcion es un schedule (``research_schedules``) cuyo workflow es
``research.watch_tick`` o ``research.discovery_tick``. Cada tick lee la fuente, decide
**en codigo** que items son nuevos o cambiaron materialmente y, solo si hay algo
pertinente, admite **un** job quick. Sin cambios pertinentes el tick hace **0 llamadas
al modelo**: el modelo solo se invoca despues, por el despachador, sobre un job ya
admitido.

Orden de un tick (a prueba de crash)
====================================
1. Leer la fuente (condicional por ETag/Last-Modified cuando la fuente lo permite).
2. Deduplicar contra ``research_watch_events`` (autoridad de "ya lo vi").
3. **Insertar** los eventos nuevos (unicos por ``(subscription_id, event_key)``).
4. **Despues** avanzar el cursor, con fencing: un tick viejo no mueve el cursor de otro.
5. Admitir un job por lote de eventos ``pending``, con clave idempotente derivada del
   *lote*, no del tick.

Un crash entre 3 y 4 deja eventos sin cursor: el siguiente tick relee, no encuentra nada
nuevo y aun asi admite los ``pending`` huerfanos. Un crash entre 4 y 5 hace lo mismo. Un
crash entre admitir y marcar ``admitted`` repite el mismo lote, la misma clave y por lo
tanto el mismo job. Nunca se pierde un item ni se admite dos veces.

Cobertura incompleta (sin saltar items)
=======================================
Si la fuente trae mas items que el tope por tick, o hay mas paginas que las permitidas,
el tick **declara** cobertura incompleta y no avanza los validadores condicionales: el
siguiente tick relee y los ya persistidos no cuentan como nuevos, asi que hay progreso
garantizado. Con paginas, el cursor conserva la ``continuation`` y el siguiente tick la
retoma.

Normalizacion (sin ocultar cambios materiales)
==============================================
* La URL de un item se normaliza para identidad: esquema/host en minusculas, puerto por
  defecto fuera, fragmento fuera, parametros de seguimiento (``utm_*``, ``fbclid``,
  ``gclid``...) fuera, el resto de parametros ordenados. **No** se tocan ruta ni
  parametros con significado (``?id=123`` es otro item).
* La identidad prefiere el ``guid``/``id`` que declara el publicador; si no hay, la URL.
* La huella de contenido cubre titulo, resumen y URL, no las marcas de fecha: un feed que
  solo refresca su ``updated`` no genera ruido, pero un titulo o resumen editado genera
  un evento ``updated``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research.acquisition.base import (
    Deadline,
    Degradation,
    ProviderParseError,
    ProviderProtocolError,
    parse_datetime,
)
from research.acquisition.http_reader import HttpReader, PreviousReading, ReadOutcome
from research.acquisition.rss import parse_feed_page
from research.contracts import (
    AccessMethod,
    ResearchEvidence,
    ResearchJobRequest,
    RetentionPolicy,
    SourceType,
)
from research.execution import ResearchExecutionContext, admit_job
from research.policies import derive_effective_policy
from research.scheduler import schedule_fingerprint

logger = logging.getLogger(__name__)

__all__ = [
    "ATS_BOARD_HOSTS",
    "ItemFilter",
    "SOURCE_TYPES",
    "SourceAdapter",
    "SourcePage",
    "SourceSpecError",
    "WORKFLOW_DISCOVERY",
    "WORKFLOW_WATCH",
    "WatchEngine",
    "WatchItem",
    "WatchStaleClaim",
    "WatchTransientError",
    "build_watch_adapters",
    "content_fingerprint",
    "digest",
    "event_key_for",
    "is_transient",
    "item_key_for",
    "normalize_source",
    "normalize_url",
    "source_ref",
]

WORKFLOW_WATCH = "research.watch_tick"
WORKFLOW_DISCOVERY = "research.discovery_tick"

SOURCE_RSS = "rss"
SOURCE_GREENHOUSE = "greenhouse"
SOURCE_LEVER = "lever"
SOURCE_TYPES = (SOURCE_RSS, SOURCE_GREENHOUSE, SOURCE_LEVER)

# Hosts **fijos** de las APIs publicas de ATS: el usuario aporta un token de tablero, no
# una URL, de modo que una suscripcion ATS no puede apuntar a un destino arbitrario.
ATS_BOARD_HOSTS = {
    SOURCE_GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
    SOURCE_LEVER: "https://api.lever.co/v0/postings/{board}?mode=json&limit={limit}&skip={skip}",
}

EVENT_PENDING = "pending"
EVENT_FILTERED = "filtered"
EVENT_BASELINE = "baseline"
EVENT_ADMITTED = "admitted"

# Tope de **lectura** de un adaptador: protege la memoria ante una fuente patologica. Es
# mucho mayor que el tope de ingestion por tick (``RESEARCH_WATCH_MAX_ITEMS_PER_SOURCE``):
# recortar aqui es determinista (releer da lo mismo), asi que solo se declara; el tope de
# ingestion, en cambio, avanza con cada tick porque lo ya persistido deja de contar.
MAX_PARSED_ITEMS = 5000

MAX_TERMS = 20
MAX_TERM_LENGTH = 80
MAX_TITLE_LENGTH = 200
MAX_URL_LENGTH = 2000

_BOARD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_WHITESPACE = re.compile(r"\s+")
_TRACKING_EXACT = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref_src", "yclid"})
# Causas que un reintento con backoff puede resolver. Un XML mal formado, un 403 o un
# destino bloqueado no mejoran reintentando: se declaran y se espera a la siguiente cadencia.
_TRANSIENT_REASONS = frozenset(
    {"rate_limited", "transport", "timeout_ambiguous", "server_error", "deadline_exceeded", "circuit_open"}
)
_HTTP_5XX = re.compile(r"HTTP 5\d\d")


def is_transient(degradation: Degradation) -> bool:
    """Un reintento con backoff puede resolverlo. El lector reporta un 5xx como ``protocol``
    ("Respuesta HTTP 503"), asi que ese caso se reconoce por su detalle."""
    if degradation.reason in _TRANSIENT_REASONS:
        return True
    return degradation.reason == "protocol" and bool(_HTTP_5XX.search(degradation.detail or ""))


class SourceSpecError(ValueError):
    """La fuente o el filtro de una suscripcion no son validos (422 en la fachada)."""


class WatchTransientError(RuntimeError):
    """Fallo transitorio de la fuente: el scheduler reintenta con backoff acotado."""


class WatchStaleClaim(RuntimeError):
    """La toma del schedule ya no es la de este tick (fencing): no se escribe nada mas."""


# ------------------------------------------------------------------ normalizacion


def normalize_url(raw: Any, *, strip_tracking: bool = False) -> str:
    """URL canonica de identidad. Lanza ``SourceSpecError`` si no es http(s) valida."""
    text = str(raw or "").strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise SourceSpecError("URL vacia o demasiado larga")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise SourceSpecError("URL invalida") from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise SourceSpecError("Solo se admiten URLs http/https con host")
    if parts.username or parts.password:
        raise SourceSpecError("La URL no puede llevar credenciales")
    shown_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = shown_host if port in (None, default_port) else f"{shown_host}:{port}"
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        pairs = [
            (key, value) for key, value in pairs
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_EXACT
        ]
    query = urlencode(sorted(pairs))
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def normalize_source(source: Mapping[str, Any]) -> dict[str, str]:
    """Fuente canonica. Rechaza claves desconocidas: un campo ignorado seria un 200 mudo."""
    if not isinstance(source, Mapping):
        raise SourceSpecError("source debe ser un objeto")
    kind = str(source.get("type") or "").strip().lower()
    if kind not in SOURCE_TYPES:
        raise SourceSpecError(f"source.type debe ser uno de {list(SOURCE_TYPES)}")
    allowed = {"type", "url"} if kind == SOURCE_RSS else {"type", "board"}
    unknown = set(source) - allowed
    if unknown:
        raise SourceSpecError(f"Campos no admitidos en source: {sorted(unknown)}")
    if kind == SOURCE_RSS:
        return {"type": kind, "url": normalize_url(source.get("url"))}
    board = str(source.get("board") or "").strip()
    if not _BOARD_PATTERN.match(board):
        raise SourceSpecError("source.board no es un token de tablero valido")
    return {"type": kind, "board": board}


def source_ref(source: Mapping[str, str]) -> str:
    """Referencia estable de la fuente, para identidad y para mostrar."""
    if source["type"] == SOURCE_RSS:
        return f"rss:{source['url']}"
    return f"{source['type']}:{source['board']}"


def source_url(source: Mapping[str, str], **query: Any) -> str:
    """URL de la fuente. Las de ATS salen de una plantilla de host fijo, no de la entrada."""
    if source["type"] == SOURCE_RSS:
        return source["url"]
    return ATS_BOARD_HOSTS[source["type"]].format(board=source["board"], **query)


def _clean_text(text: Any) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", str(text or ""))).strip()


def _fold(text: Any) -> str:
    """Minusculas sin acentos, para que ``Ingeniería`` coincida con ``ingenieria``."""
    decomposed = unicodedata.normalize("NFKD", _clean_text(text))
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def digest(*parts: Any, size: int = 40) -> str:
    joined = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:size]


def item_key_for(entry_id: str | None, url: str | None) -> str | None:
    """Identidad del item: el id del publicador si existe; si no, la URL normalizada."""
    declared = _clean_text(entry_id)
    if declared:
        return digest("id", declared)
    if url:
        try:
            return digest("url", normalize_url(url, strip_tracking=True))
        except SourceSpecError:
            return None
    return None


def content_fingerprint(*parts: Any) -> str:
    """Huella del contenido material (sin fechas). Cambia si el titulo o el resumen cambian."""
    return digest("content", *(_clean_text(part) for part in parts))


def event_key_for(item_key: str, content_fp: str) -> str:
    return f"{item_key}@{content_fp[:16]}"


@dataclass(frozen=True)
class ItemFilter:
    """Filtro de pertinencia determinista. Sin terminos de inclusion, todo item es pertinente."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_params(cls, raw: Mapping[str, Any] | None) -> "ItemFilter":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise SourceSpecError("filter debe ser un objeto")
        unknown = set(raw) - {"include_terms", "exclude_terms"}
        if unknown:
            raise SourceSpecError(f"Campos no admitidos en filter: {sorted(unknown)}")
        return cls(
            include=cls._terms(raw.get("include_terms"), "include_terms"),
            exclude=cls._terms(raw.get("exclude_terms"), "exclude_terms"),
        )

    @staticmethod
    def _terms(raw: Any, name: str) -> tuple[str, ...]:
        if raw in (None, ""):
            return ()
        if not isinstance(raw, (list, tuple)) or len(raw) > MAX_TERMS:
            raise SourceSpecError(f"{name} debe ser una lista de hasta {MAX_TERMS} terminos")
        terms: list[str] = []
        for term in raw:
            text = _fold(term)
            if not text or len(text) > MAX_TERM_LENGTH:
                raise SourceSpecError(f"{name}: terminos no vacios de hasta {MAX_TERM_LENGTH} caracteres")
            if text not in terms:
                terms.append(text)
        return tuple(terms)

    def to_params(self) -> dict[str, list[str]]:
        # Orden canonico: el mismo filtro escrito en otro orden es el mismo spec (no un 409).
        return {"include_terms": sorted(self.include), "exclude_terms": sorted(self.exclude)}

    def matches(self, *texts: Any) -> bool:
        haystack = " ".join(_fold(text) for text in texts if text)
        if any(term in haystack for term in self.exclude):
            return False
        return not self.include or any(term in haystack for term in self.include)


# ------------------------------------------------------------------ items y fuentes


@dataclass(frozen=True)
class WatchItem:
    item_key: str
    content_fp: str
    url: str | None
    title: str | None = None
    published_at: datetime | None = None
    # Solo para la pertinencia en memoria: el resumen de un tercero no se persiste.
    summary: str | None = None


@dataclass
class SourcePage:
    """Una pagina de una fuente. ``next_page`` es la continuacion, si la hay."""

    items: list[WatchItem] = field(default_factory=list)
    next_page: str | None = None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    degradations: tuple[Degradation, ...] = ()
    # El adaptador recorto items por su propio tope: la cobertura es incompleta.
    truncated: bool = False


class SourceAdapter(Protocol):
    name: str

    async def fetch_page(
        self,
        source: Mapping[str, str],
        *,
        page_token: str | None,
        conditional: Mapping[str, Any] | None,
        scope: Mapping[str, str],
        deadline: Deadline | None,
    ) -> SourcePage: ...


class _HttpSource:
    """Base de los adaptadores: lectura por el guard de egress, condicional por validadores."""

    name = "http"
    access_method = AccessMethod.HTTP

    def __init__(self, reader: HttpReader, *, max_items: int = MAX_PARSED_ITEMS) -> None:
        self._reader = reader
        self._max_items = max(1, int(max_items))

    async def _read(
        self,
        url: str,
        *,
        conditional: Mapping[str, Any] | None,
        scope: Mapping[str, str],
        deadline: Deadline | None,
    ) -> ReadOutcome:
        previous: PreviousReading | None = None
        if conditional and (conditional.get("etag") or conditional.get("last_modified")):
            # Un 304 necesita una lectura previa a la que remitirse. El cursor guarda los
            # validadores, no el cuerpo: se arma una evidencia minima solo para que el
            # lector emita la peticion condicional. No se persiste.
            stub = ResearchEvidence(
                id="watch-validator", url=url, access_method=self.access_method,
                content_hash=conditional.get("content_hash"),
                project_key=scope.get("project_key"), owner_id=scope.get("owner_id"),
            )
            previous = PreviousReading(
                evidence=stub, etag=conditional.get("etag"),
                last_modified=conditional.get("last_modified"),
            )
        return await self._reader.read(
            url, research_id=scope["research_id"], project_key=scope["project_key"],
            owner_id=scope.get("owner_id"), previous=previous,
            source_type=SourceType.PRIMARY, retention_policy=RetentionPolicy.WATCH_90D,
            access_method=self.access_method, deadline=deadline,
        )

    @staticmethod
    def _unchanged(outcome: ReadOutcome, conditional: Mapping[str, Any] | None) -> SourcePage | None:
        """304, o 200 con el mismo cuerpo que la ultima lectura (servidor sin validadores)."""
        same_body = bool(
            conditional and conditional.get("content_hash")
            and outcome.content_hash == conditional.get("content_hash")
        )
        if outcome.not_modified or same_body:
            return SourcePage(
                not_modified=True, etag=outcome.etag, last_modified=outcome.last_modified,
                content_hash=outcome.content_hash,
            )
        return None

    @staticmethod
    def _protocol_degradation(provider: str, detail: str) -> tuple[Degradation, ...]:
        return (ProviderProtocolError(detail, provider=provider).degradation(),)


class RssSourceAdapter(_HttpSource):
    name = SOURCE_RSS
    access_method = AccessMethod.RSS

    async def fetch_page(self, source, *, page_token, conditional, scope, deadline) -> SourcePage:
        outcome = await self._read(source["url"], conditional=conditional, scope=scope, deadline=deadline)
        if outcome.degradations:
            return SourcePage(degradations=tuple(outcome.degradations))
        unchanged = self._unchanged(outcome, conditional)
        if unchanged is not None:
            return unchanged
        if outcome.evidence is None:
            return SourcePage(degradations=self._protocol_degradation("rss", "Lectura sin evidencia"))
        text = outcome.body.decode("utf-8", errors="replace") if outcome.body else outcome.text
        try:
            _title, entries, total = parse_feed_page(
                text, feed_url=outcome.final_url, max_entries=self._max_items
            )
        except ProviderParseError as exc:
            return SourcePage(degradations=(exc.degradation(),))
        items: list[WatchItem] = []
        for entry in entries:
            key = item_key_for(entry.entry_id, entry.url)
            if key is None:
                continue
            items.append(WatchItem(
                item_key=key,
                content_fp=content_fingerprint(entry.title, entry.summary, entry.url),
                url=entry.url, title=entry.title, published_at=entry.published_at,
                summary=entry.summary,
            ))
        return SourcePage(
            items=items, etag=outcome.etag, last_modified=outcome.last_modified,
            content_hash=outcome.content_hash, truncated=total > self._max_items,
        )


class _JsonBoardSource(_HttpSource):
    """Tableros ATS publicos: respuesta JSON con las vacantes de la pagina pedida."""

    access_method = AccessMethod.API

    def _url(self, source: Mapping[str, str], page_token: str | None) -> str | None:
        return source_url(source)

    def _validators(self, conditional: Mapping[str, Any] | None, page_token: str | None):
        return conditional

    def _next_page(self, page_token: str | None, row_count: int) -> str | None:
        return None

    async def fetch_page(self, source, *, page_token, conditional, scope, deadline) -> SourcePage:
        url = self._url(source, page_token)
        if url is None:
            return SourcePage(degradations=self._protocol_degradation(self.name, "continuacion invalida"))
        conditional = self._validators(conditional, page_token)
        outcome = await self._read(url, conditional=conditional, scope=scope, deadline=deadline)
        if outcome.degradations:
            return SourcePage(degradations=tuple(outcome.degradations))
        unchanged = self._unchanged(outcome, conditional)
        if unchanged is not None:
            return unchanged
        if outcome.evidence is None:
            return SourcePage(degradations=self._protocol_degradation(self.name, "Lectura sin evidencia"))
        try:
            payload = json.loads(outcome.body.decode("utf-8", errors="replace"))
            rows = self._rows(payload)
        except (ValueError, ProviderParseError) as exc:
            detail = str(exc) or "JSON invalido"
            return SourcePage(degradations=(ProviderParseError(
                f"{self.name}: {detail}", provider=self.name).degradation(),))
        items: list[WatchItem] = []
        for row in rows[: self._max_items]:
            item = self._item(row, source)
            if item is not None:
                items.append(item)
        return SourcePage(
            items=items, etag=outcome.etag, last_modified=outcome.last_modified,
            content_hash=outcome.content_hash, truncated=len(rows) > self._max_items,
            next_page=self._next_page(page_token, len(rows)),
        )

    def _rows(self, payload: Any) -> list[dict[str, Any]]:  # pragma: no cover - lo define cada ATS
        raise NotImplementedError

    def _item(self, row: Mapping[str, Any], source: Mapping[str, str]) -> WatchItem | None:  # pragma: no cover
        raise NotImplementedError


class GreenhouseSourceAdapter(_JsonBoardSource):
    """Job Board API de Greenhouse: ``{"jobs": [{id, title, absolute_url, location, ...}]}``."""

    name = SOURCE_GREENHOUSE

    def _rows(self, payload: Any) -> list[dict[str, Any]]:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ProviderParseError("la respuesta no trae la lista jobs", provider=self.name)
        return [row for row in jobs if isinstance(row, dict)]

    def _item(self, row, source) -> WatchItem | None:
        identity = row.get("id")
        url = row.get("absolute_url")
        if identity is None or not isinstance(url, str):
            return None
        location = row.get("location") if isinstance(row.get("location"), dict) else {}
        title = row.get("title")
        return WatchItem(
            item_key=digest("id", f"greenhouse:{source['board']}:{identity}"),
            content_fp=content_fingerprint(title, location.get("name"), url),
            url=url, title=title if isinstance(title, str) else None,
            published_at=parse_datetime(row.get("updated_at")) if isinstance(row.get("updated_at"), str) else None,
            summary=location.get("name") if isinstance(location.get("name"), str) else None,
        )


class LeverSourceAdapter(_JsonBoardSource):
    """Postings API de Lever: una lista JSON de vacantes publicas, paginada con skip/limit.

    La documentacion de Lever no fija cuantas vacantes devuelve sin ``limit``, asi que se
    pide siempre una pagina explicita y se sigue con ``skip`` mientras la pagina venga
    llena. Los validadores condicionales **no** se usan: un 304 de la primera pagina
    ocultaria cambios en las siguientes, y la dedup ya hace barata la repeticion.
    """

    name = SOURCE_LEVER
    PAGE_SIZE = 100
    MAX_SKIP = 100_000

    def _url(self, source, page_token):
        try:
            skip = int(page_token or 0)
        except (TypeError, ValueError):
            return None
        if not 0 <= skip <= self.MAX_SKIP:
            return None
        return source_url(source, limit=self.PAGE_SIZE, skip=skip)

    def _validators(self, conditional, page_token):
        return None

    def _next_page(self, page_token, row_count):
        if row_count < self.PAGE_SIZE:
            return None
        skip = int(page_token or 0) + self.PAGE_SIZE
        return str(skip) if skip <= self.MAX_SKIP else None

    def _rows(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise ProviderParseError("la respuesta no es una lista de postings", provider=self.name)
        return [row for row in payload if isinstance(row, dict)]

    def _item(self, row, source) -> WatchItem | None:
        identity = row.get("id")
        url = row.get("hostedUrl")
        if identity is None or not isinstance(url, str):
            return None
        categories = row.get("categories") if isinstance(row.get("categories"), dict) else {}
        title = row.get("text")
        facets = [categories.get(name) for name in ("location", "team", "department", "commitment")]
        created = row.get("createdAt")  # no documentado: se usa solo si viene
        published = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            if isinstance(created, (int, float)) and not isinstance(created, bool) else None
        )
        return WatchItem(
            item_key=digest("id", f"lever:{source['board']}:{identity}"),
            content_fp=content_fingerprint(title, *facets, url),
            url=url, title=title if isinstance(title, str) else None, published_at=published,
            summary=" ".join(str(f) for f in facets if isinstance(f, str)) or None,
        )


def build_watch_adapters(reader: HttpReader | None, *, max_items: int = MAX_PARSED_ITEMS) -> dict[str, SourceAdapter]:
    """Adaptadores montables. Sin lector HTTP no hay adquisicion: dict vacio, no fakes."""
    if reader is None:
        return {}
    return {
        SOURCE_RSS: RssSourceAdapter(reader, max_items=max_items),
        SOURCE_GREENHOUSE: GreenhouseSourceAdapter(reader, max_items=max_items),
        SOURCE_LEVER: LeverSourceAdapter(reader, max_items=max_items),
    }


# ------------------------------------------------------------------ motor


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _setting(settings: Any, name: str, default: int) -> int:
    value = getattr(settings, name, default)
    return int(default if value is None else value)


class WatchEngine:
    """Ejecuta los ticks de vigilancia y descubrimiento. No crea Tasks ni llama al modelo."""

    def __init__(
        self,
        *,
        repo: Any,
        settings: Any,
        service: Any,
        adapters: Mapping[str, SourceAdapter] | None = None,
        discover: Any | None = None,
        now_fn: Any | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._service = service
        self._adapters = dict(adapters or {})
        self._discover = discover
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ helpers

    @property
    def max_items_per_tick(self) -> int:
        return max(1, _setting(self._settings, "RESEARCH_WATCH_MAX_ITEMS_PER_TICK", 5))

    @property
    def retention(self) -> timedelta:
        return timedelta(days=max(1, _setting(self._settings, "RESEARCH_WATCH_RETENTION_DAYS", 90)))

    def _authorize(self, execution: ResearchExecutionContext, question: str):
        # La autoridad se resuelve en **cada** tick con la misma politica que un job:
        # owner inactivo, permiso o proyecto retirado, research apagado => se deniega aqui
        # y el runtime pausa la suscripcion en lugar de reintentar.
        request = ResearchJobRequest(
            project_key=execution.project_key, purpose=execution.purpose,
            question=question, mode="quick",
        )
        return derive_effective_policy(
            principal=execution.principal, request=request,
            project_cfg=execution.project_cfg, settings=self._settings,
        )

    def _deadline(self, schedule: Mapping[str, Any], now: datetime) -> Deadline:
        seconds = float(schedule.get("deadline_seconds") or 120)
        lease = schedule.get("lease_expires_at")
        if lease is not None:
            # El tick debe acabar antes de que venza su lease: despues, el fencing
            # rechazaria el commit del cursor y el trabajo se repetiria.
            seconds = min(seconds, (_aware(lease) - now).total_seconds() - 5.0)
        return Deadline.start(max(5.0, seconds))

    async def _commit_cursor(self, schedule: Mapping[str, Any], cursor: Mapping[str, Any]) -> None:
        stored = await self._repo.patch_research_schedule(
            str(schedule["schedule_id"]), {"cursor": dict(cursor)},
            lease_owner=schedule.get("lease_owner"), fencing_token=schedule.get("fencing_token"),
            now=self._now(),
        )
        if stored is None:
            raise WatchStaleClaim("La toma del schedule cambio durante el tick")

    def _event_document(
        self, *, schedule: Mapping[str, Any], item: WatchItem, kind: str, status: str,
        now: datetime, execution_key: str, keep_url: bool = True,
    ) -> dict[str, Any]:
        return {
            "subscription_id": schedule["schedule_id"],
            "owner_id": schedule["owner_id"],
            "project_key": schedule["project_key"],
            "event_key": event_key_for(item.item_key, item.content_fp),
            "item_key": item.item_key,
            "content_fp": item.content_fp,
            "kind": kind,
            "status": status,
            "title": (item.title or "")[:MAX_TITLE_LENGTH] or None,
            "url": (item.url or "")[:MAX_URL_LENGTH] if keep_url and item.url else None,
            "published_at": item.published_at,
            "occurrence": execution_key,
            "research_id": None,
            "first_seen_at": now,
            "last_seen_at": now,
            "updated_at": now,
            "expires_at": now + self.retention,
        }

    async def _classify(
        self,
        *,
        schedule: Mapping[str, Any],
        items: Sequence[WatchItem],
        item_filter: ItemFilter,
        baseline: bool,
        now: datetime,
        execution_key: str,
        cap: int,
        keep_url: bool,
    ) -> dict[str, Any]:
        """Deduplica ``items`` contra los eventos vistos y arma los eventos nuevos."""
        unique: dict[str, WatchItem] = {}
        for item in items:
            unique.setdefault(item.item_key, item)
        existing: list[dict[str, Any]] = []
        keys = list(unique)
        for start in range(0, len(keys), 500):
            existing.extend(
                await self._repo.find_research_watch_events_by_item(
                    str(schedule["schedule_id"]), keys[start:start + 500]
                )
            )
        known_events = {row["event_key"]: row for row in existing}
        known_items = {row["item_key"] for row in existing}
        # TTL deslizante: se renueva solo cuando queda menos de la mitad, para no
        # reescribir cada evento en cada tick.
        refresh_before = now + self.retention / 2
        touch: list[str] = []
        fresh: list[dict[str, Any]] = []
        for item in unique.values():
            event_key = event_key_for(item.item_key, item.content_fp)
            seen = known_events.get(event_key)
            if seen is not None:
                expires = seen.get("expires_at")
                if expires is not None and _aware(expires) < refresh_before:
                    touch.append(event_key)
                continue
            kind = "updated" if item.item_key in known_items else "new"
            if baseline:
                status = EVENT_BASELINE
            elif item_filter.matches(item.title, item.summary, item.url):
                status = EVENT_PENDING
            else:
                status = EVENT_FILTERED
            fresh.append(self._event_document(
                schedule=schedule, item=item, kind=kind, status=status, now=now,
                execution_key=execution_key, keep_url=keep_url,
            ))
        capped = len(fresh) > cap
        return {"fresh": fresh[:cap], "touch": touch, "capped": capped, "seen": len(unique)}

    # ------------------------------------------------------------ admision

    async def _admit_pending(
        self, execution: ResearchExecutionContext, schedule: Mapping[str, Any],
        question: str, *, prefix: str,
    ) -> dict[str, Any] | None:
        sid = str(schedule["schedule_id"])
        pending = await self._repo.list_pending_research_watch_events(sid, self.max_items_per_tick)
        if not pending:
            return None
        withdrawn = [row["event_key"] for row in pending if not row.get("url")]
        if withdrawn:
            # Sin URL no hay que investigar: admitir igual haria una busqueda generica con la
            # pregunta y gastaria presupuesto sobre algo que no es el item detectado.
            logger.warning("research: %d eventos sin URL descartados en %s", len(withdrawn), sid)
            await self._repo.mark_research_watch_events(
                sid, withdrawn, "dropped", only_from=EVENT_PENDING, now=self._now())
            pending = [row for row in pending if row.get("url")]
            if not pending:
                return None
        keys = [row["event_key"] for row in pending]
        # La clave sale del lote y del spec, no del tick: un replay tras un crash
        # reconcilia el mismo job; un spec editado abre un lote distinto.
        batch = digest(schedule_fingerprint(schedule), *sorted(keys), size=32)
        urls = [row["url"] for row in pending if row.get("url")]
        admitted = await admit_job(
            self._service, execution, {"question": question, "seed_urls": urls},
            idempotency_key=f"{prefix}:{sid}:{batch}",
        )
        await self._repo.mark_research_watch_events(
            sid, keys, EVENT_ADMITTED, research_id=admitted["research_id"],
            only_from=EVENT_PENDING, now=self._now(),
        )
        return {"research_id": admitted["research_id"], "created": admitted["created"],
                "events": len(keys), "status": admitted["status"]}

    # ------------------------------------------------------------ ticks publicos

    async def watch_tick(self, execution: ResearchExecutionContext, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await self._watch_tick(execution, params)
        except WatchStaleClaim:
            # Otro ejecutor tomo el schedule: lo ya insertado es idempotente y sigue siendo
            # valido, pero este tick no escribe mas ni admite nada.
            return {"kind": "watch", "status": "skipped", "skipped": "stale_claim"}

    async def discovery_tick(self, execution: ResearchExecutionContext, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await self._discovery_tick(execution, params)
        except WatchStaleClaim:
            return {"kind": "discovery", "status": "skipped", "skipped": "stale_claim"}

    # ------------------------------------------------------------ watch_tick

    async def _watch_tick(self, execution: ResearchExecutionContext, params: Mapping[str, Any]) -> dict[str, Any]:
        schedule = execution.schedule
        if not schedule or not schedule.get("schedule_id"):
            raise ValueError("watch_tick necesita el schedule vigente en el contexto")
        try:
            source = normalize_source(params.get("source"))  # type: ignore[arg-type]
            item_filter = ItemFilter.from_params(params.get("filter"))
        except SourceSpecError as exc:
            # Un spec corrupto no se arregla reintentando: se declara y se sigue.
            return {"kind": "watch", "status": "invalid_spec", "detail": str(exc)}
        question = str(params.get("question") or "").strip()
        if not question:
            return {"kind": "watch", "status": "invalid_spec", "detail": "question vacia"}

        self._authorize(execution, question)  # puede lanzar: el runtime pausa
        adapter = self._adapters.get(source["type"])
        result: dict[str, Any] = {"kind": "watch", "source": source_ref(source)}
        if adapter is None:
            return {**result, "status": "skipped", "skipped": "adapter_unavailable"}

        now = self._now()
        sid = str(schedule["schedule_id"])
        cursor = dict(schedule.get("cursor") or {})
        scope = {"research_id": f"watch-{sid}", "project_key": execution.project_key,
                 "owner_id": execution.owner_id}
        deadline = self._deadline(schedule, now)
        max_pages = max(1, _setting(self._settings, "RESEARCH_WATCH_MAX_PAGES_PER_TICK", 3))
        cap = max(1, _setting(self._settings, "RESEARCH_WATCH_MAX_ITEMS_PER_SOURCE", 200))

        resume = (cursor.get("continuation") or {}).get("page_token")
        head_read = resume is None
        token: str | None = resume
        conditional: Mapping[str, Any] | None = None
        if head_read:
            conditional = {key: cursor.get(key) for key in ("etag", "last_modified", "content_hash")}
        items: list[WatchItem] = []
        head: SourcePage | None = None
        next_page: str | None = None
        page_truncated = False
        for _ in range(max_pages):
            page = await adapter.fetch_page(
                source, page_token=token, conditional=conditional, scope=scope, deadline=deadline
            )
            conditional = None
            if page.degradations:
                return await self._source_failed(schedule, cursor, page.degradations, result, now)
            if head is None and head_read:
                head = page
            if page.not_modified:
                break
            items.extend(page.items)
            page_truncated = page_truncated or page.truncated
            next_page = page.next_page
            if not next_page:
                break
            token = next_page

        unchanged = bool(head is not None and head.not_modified)
        baseline = not cursor.get("initialized") and not bool(params.get("backfill"))
        classified = await self._classify(
            schedule=schedule, items=items, item_filter=item_filter, baseline=baseline,
            now=now, execution_key=execution.execution_key, cap=cap, keep_url=True,
        )
        inserted = await self._repo.insert_research_watch_events(classified["fresh"])
        if classified["touch"]:
            await self._repo.touch_research_watch_events(
                sid, classified["touch"], seen_at=now, expires_at=now + self.retention
            )

        incomplete_reason: str | None = None
        if classified["capped"]:
            incomplete_reason = "item_cap"
        elif next_page:
            incomplete_reason = "page_limit"
        new_cursor = {**cursor, "last_checked_at": now, "consecutive_failures": 0,
                      "last_failure": None}
        if incomplete_reason is None:
            if head is not None:
                new_cursor.update({"etag": head.etag, "last_modified": head.last_modified,
                                   "content_hash": head.content_hash})
            # Un recorte del adaptador es determinista: releer devolveria lo mismo, asi que
            # no se retiene el cursor (eso dejaria la suscripcion en linea base para siempre).
            # Se avanza y se **declara** que parte de la fuente no se vigila.
            new_cursor.update({"initialized": True, "continuation": None,
                               "coverage": "incomplete" if page_truncated else "complete",
                               "coverage_reason": "source_truncated" if page_truncated else None})
        elif incomplete_reason == "page_limit":
            # Se conservan los validadores viejos: el cabezal se relee al terminar la pasada.
            new_cursor.update({"continuation": {"page_token": next_page},
                               "coverage": "incomplete", "coverage_reason": incomplete_reason})
        else:
            # Tope de items: no se avanzan los validadores, asi el siguiente tick relee
            # y los ya persistidos no cuentan como nuevos (progreso sin saltar items).
            new_cursor.update({"continuation": None, "coverage": "incomplete",
                               "coverage_reason": incomplete_reason})
        if inserted:
            new_cursor["last_changed_at"] = now
        await self._commit_cursor(schedule, new_cursor)

        admitted = await self._admit_pending(execution, schedule, question, prefix="watch")
        counts = {"new": 0, "updated": 0, "filtered": 0, "baseline": 0}
        stored = set(inserted)
        for doc in classified["fresh"]:
            if doc["event_key"] not in stored:
                continue
            counts[doc["kind"] if doc["status"] == EVENT_PENDING else doc["status"]] += 1
        return {
            **result, "status": "ok", "not_modified": unchanged, "items_seen": classified["seen"],
            "inserted": counts, "coverage": new_cursor.get("coverage", "complete"),
            "coverage_reason": new_cursor.get("coverage_reason"),
            "backlog": await self._repo.count_research_watch_events(sid, status=EVENT_PENDING),
            "admitted": admitted, "baseline": baseline,
        }

    async def _source_failed(
        self, schedule: Mapping[str, Any], cursor: Mapping[str, Any],
        degradations: Sequence[Degradation], result: dict[str, Any], now: datetime,
    ) -> dict[str, Any]:
        gaps = [d.to_payload() for d in degradations]
        failures = int(cursor.get("consecutive_failures") or 0) + 1
        # El cursor **no** avanza: ni validadores ni continuation. Solo se registra el fallo.
        await self._commit_cursor(schedule, {
            **cursor, "last_checked_at": now, "consecutive_failures": failures,
            "last_failure": gaps[0]["reason"] if gaps else "unknown",
        })
        if any(is_transient(d) for d in degradations):
            raise WatchTransientError(f"fuente no disponible: {gaps[0]['reason']}")
        return {**result, "status": "degraded", "coverage_gaps": gaps}

    # ------------------------------------------------------------ discovery_tick

    async def _discovery_tick(self, execution: ResearchExecutionContext, params: Mapping[str, Any]) -> dict[str, Any]:
        schedule = execution.schedule
        if not schedule or not schedule.get("schedule_id"):
            raise ValueError("discovery_tick necesita el schedule vigente en el contexto")
        question = str(params.get("question") or "").strip()
        result: dict[str, Any] = {"kind": "discovery"}
        if not question:
            return {**result, "status": "invalid_spec", "detail": "question vacia"}
        try:
            parsed = ItemFilter.from_params(params.get("filter"))
        except SourceSpecError as exc:
            return {**result, "status": "invalid_spec", "detail": str(exc)}
        # Un resultado del buscador solo trae la URL (sin titulo ni resumen): ``include_terms``
        # compararia contra la URL y descartaria en silencio fuentes pertinentes. Solo se aplica
        # ``exclude_terms``; el servicio de suscripciones tampoco admite ``include_terms`` aqui.
        item_filter = ItemFilter(exclude=parsed.exclude)

        policy = self._authorize(execution, question)
        if not policy.adapter_enabled("brave"):
            # Gasto no aprobado: el proyecto no habilito el buscador, no se llama.
            return {**result, "status": "skipped", "skipped": "adapter_disabled"}
        if self._discover is None:
            return {**result, "status": "skipped", "skipped": "discovery_unavailable"}

        now = self._now()
        sid = str(schedule["schedule_id"])
        cursor = dict(schedule.get("cursor") or {})
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        window = cursor.get("search_window") or {}
        used = int(window.get("count") or 0) if window.get("day") == day else 0
        server_cap = max(0, _setting(self._settings, "RESEARCH_DISCOVERY_DAILY_SEARCH_CAP", 2))
        requested = params.get("max_searches_per_day")
        # Ausente es ausente: ``""`` (lo que el templating YAML da a un param faltante) no es
        # 0 ni un entero invalido, es "sin tope propio".
        cap = server_cap if requested in (None, "") else max(0, min(int(requested), server_cap))
        if used >= cap:
            return {**result, "status": "skipped", "skipped": "daily_search_cap",
                    "searches_today": used, "cap": cap}

        # Se consume el cupo **antes** de buscar: si el proceso cae a mitad, el cupo ya
        # quedo gastado (lado conservador) y un replay no repite la busqueda pagable.
        cursor["search_window"] = {"day": day, "count": used + 1}
        await self._commit_cursor(schedule, cursor)

        count = max(1, min(int(params.get("count") or 10), 20))
        occurrence = digest(execution.execution_key, size=24)
        found = await self._discover(
            question=str(params.get("query") or question), research_id=f"disc-{occurrence}",
            project_key=execution.project_key, count=count,
            policy=policy,
        )
        degradations = tuple(getattr(found, "degradations", ()) or ())
        if degradations:
            # Cuota agotada, tope de presupuesto o limite: se declara, no se reintenta ni
            # se cae a otro proveedor. La siguiente ocurrencia vuelve a intentar.
            return {**result, "status": "degraded", "searches_today": used + 1, "cap": cap,
                    "coverage_gaps": [d.to_payload() for d in degradations]}

        items: list[WatchItem] = []
        for entry in getattr(found, "items", ()) or ():
            try:
                normalized = normalize_url(getattr(entry, "url", ""), strip_tracking=True)
            except SourceSpecError:
                continue
            items.append(WatchItem(item_key=digest("url", normalized), content_fp="discovered",
                                   url=normalized))
        baseline = not cursor.get("initialized") and not bool(params.get("backfill"))
        # La URL de un SERP no se persiste (terminos de Brave, ver acquisition/base.py):
        # solo se guarda el hash para no volver a proponerla.
        classified = await self._classify(
            schedule=schedule, items=items, item_filter=item_filter, baseline=baseline,
            now=now, execution_key=execution.execution_key,
            cap=max(len(items), 1), keep_url=False,
        )
        relevant = [doc for doc in classified["fresh"] if doc["status"] == EVENT_PENDING]
        by_hash = {item.item_key: item for item in items}
        chosen = relevant[: self.max_items_per_tick]
        admitted: dict[str, Any] | None = None
        if chosen:
            # Admitir primero y registrar despues: un crash entre ambos puede repetir el
            # descubrimiento (at-least-once) pero nunca pierde una URL encontrada.
            batch = digest(schedule_fingerprint(schedule), *sorted(d["event_key"] for d in chosen), size=32)
            job = await admit_job(
                self._service, execution,
                {"question": question, "seed_urls": [by_hash[d["item_key"]].url for d in chosen]},
                idempotency_key=f"disc:{sid}:{batch}",
            )
            admitted = {"research_id": job["research_id"], "created": job["created"],
                        "events": len(chosen), "status": job["status"]}
            for doc in chosen:
                doc["status"] = EVENT_ADMITTED
                doc["research_id"] = job["research_id"]
        # Lo pertinente que excede el lote no se registra: reaparecera en otra busqueda.
        chosen_keys = {d["event_key"] for d in chosen}
        to_store = [d for d in classified["fresh"] if d["status"] != EVENT_PENDING or d["event_key"] in chosen_keys]
        inserted = await self._repo.insert_research_watch_events(to_store)
        if classified["touch"]:
            await self._repo.touch_research_watch_events(
                sid, classified["touch"], seen_at=now, expires_at=now + self.retention
            )
        await self._commit_cursor(schedule, {
            **cursor, "last_checked_at": now, "initialized": True,
            "consecutive_failures": 0,
        })
        return {
            **result, "status": "ok", "baseline": baseline, "searches_today": used + 1,
            "cap": cap, "results": len(items), "recorded": len(inserted),
            "overflow": len(relevant) - len(chosen), "admitted": admitted,
        }
