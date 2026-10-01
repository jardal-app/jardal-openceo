"""Lector RSS/Atom con guard de egress y parseo endurecido (TASK-2026-00060).

Seguridad del parser
====================
Un feed es contenido no confiable. Se rechazan explicitamente ``DOCTYPE`` y
``ENTITY``: un DTD puede intentar leer ficheros locales o resolver red (XXE) y un
arbol de entidades puede agotar memoria (billion laughs). El parser **no** descarga
``enclosure``/``media:content`` ni sigue enlaces: solo se guardan las URLs como
datos. Abrir una entrada es una decision explicita del broker que vuelve a pasar
por el guard de egress.

El feed se lee con ``HttpReader``, asi que hereda egress, limites de bytes/MIME,
condicionales ETag y el 304 que conserva la evidencia anterior.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence
from urllib.parse import urljoin

from research.contracts import (
    AccessMethod,
    Classification,
    ResearchEvidence,
    RetentionPolicy,
    SourceType,
)

from .base import Degradation, ProviderParseError
from .http_reader import (
    HttpReader,
    PreviousReading,
    ReadOutcome,
    normalize_text,
    parse_http_datetime,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FeedEntry",
    "FeedOutcome",
    "RssReader",
    "assert_safe_xml",
]

# Un DTD/entidad en un feed no tiene uso legitimo para nuestro caso y si un riesgo
# real de XXE o de agotamiento. Se rechaza antes de entregar el texto al parser.
_UNSAFE_XML = re.compile(
    r"<!DOCTYPE|<!ENTITY|SYSTEM\s+[\"']|PUBLIC\s+[\"']",
    re.IGNORECASE,
)


class UnsafeXmlError(ProviderParseError):
    """El feed trae DTD/entidades o recursos externos declarados."""

    kind = "unsafe_xml"


def assert_safe_xml(text: str) -> None:
    """Lanza ``UnsafeXmlError`` si el XML declara DTD, entidades o recursos externos."""
    if _UNSAFE_XML.search(text):
        raise UnsafeXmlError(
            "El feed declara DOCTYPE/ENTITY o recursos externos; se rechaza por seguridad",
            provider="rss",
        )


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _first_child(node: ET.Element, *names: str) -> ET.Element | None:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if _localname(child.tag) in wanted:
            return child
    return None


def _child_text(node: ET.Element, *names: str) -> str | None:
    child = _first_child(node, *names)
    if child is None:
        return None
    value = "".join(child.itertext()).strip()
    return value or None


def _entry_link(node: ET.Element) -> str | None:
    link = _first_child(node, "link")
    if link is None:
        return None
    href = link.get("href")
    if href:
        return href.strip() or None
    text = (link.text or "").strip()
    return text or None


@dataclass(frozen=True)
class FeedEntry:
    """Entrada de un feed. Solo metadatos; el texto de la entrada no se descarga aqui."""

    url: str
    title: str | None = None
    published_at: datetime | None = None
    summary: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class FeedOutcome:
    """Lectura de un feed con su procedencia y sus entradas."""

    feed_url: str
    final_url: str
    title: str | None = None
    entries: tuple[FeedEntry, ...] = ()
    evidence: ResearchEvidence | None = None
    feed_text: str = ""
    feed_body: bytes = b""
    not_modified: bool = False
    last_checked_at: datetime | None = None
    observed_at: datetime | None = None
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    degradations: tuple[Degradation, ...] = ()

    @property
    def ok(self) -> bool:
        return self.evidence is not None and not self.degradations

    def to_payload(self) -> dict[str, object]:
        return {
            "feed_url": self.feed_url,
            "final_url": self.final_url,
            "title": self.title,
            "entries": [entry.to_payload() for entry in self.entries],
            "not_modified": self.not_modified,
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None
            ),
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "content_hash": self.content_hash,
            "retention_policy": (
                self.evidence.retention_policy.value if self.evidence else None
            ),
            "degradations": [d.to_payload() for d in self.degradations],
        }


class RssReader:
    """Lee y parsea feeds RSS/Atom usando el ``HttpReader`` con guard de egress."""

    access_method = AccessMethod.RSS

    def __init__(
        self,
        *,
        reader: HttpReader,
        wall_clock: Callable[[], datetime] | None = None,
        max_entries: int = 25,
    ) -> None:
        self._reader = reader
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._max_entries = max(1, int(max_entries))

    async def read_feed(
        self,
        url: str,
        *,
        research_id: str,
        project_key: str,
        owner_id: str | None = None,
        previous: FeedOutcome | None = None,
        deadline=None,
        source_type: SourceType = SourceType.AGGREGATOR,
        retention_policy: RetentionPolicy = RetentionPolicy.PUBLIC_7D,
        classification: Classification = Classification.PUBLIC,
        max_entries: int | None = None,
    ) -> FeedOutcome:
        """Lee el feed y devuelve sus entradas sin descargar recursos del parser."""
        prior: PreviousReading | None = None
        if previous is not None and previous.evidence is not None:
            prior = PreviousReading(
                evidence=previous.evidence,
                text=previous.feed_text,
                body=previous.feed_body,
                etag=previous.etag,
                last_modified=previous.last_modified,
            )
        outcome: ReadOutcome = await self._reader.read(
            url,
            research_id=research_id,
            project_key=project_key,
            owner_id=owner_id,
            previous=prior,
            classification=classification,
            source_type=source_type,
            retention_policy=retention_policy,
            access_method=AccessMethod.RSS,
            deadline=deadline,
        )
        if outcome.evidence is None:
            return FeedOutcome(
                feed_url=url,
                final_url=outcome.final_url,
                not_modified=outcome.not_modified,
                last_checked_at=outcome.last_checked_at,
                etag=outcome.etag,
                last_modified=outcome.last_modified,
                degradations=outcome.degradations,
            )

        if outcome.not_modified and previous is not None:
            # Un 304 ahorra cuerpo y parseo: se reutilizan las entradas ya parseadas
            # en vez de volver a interpretar el XML cacheado. La comprobacion si
            # ocurrio, y por eso avanza `last_checked_at`.
            return FeedOutcome(
                feed_url=url,
                final_url=outcome.final_url,
                title=previous.title,
                entries=previous.entries,
                evidence=outcome.evidence,
                feed_text=previous.feed_text,
                feed_body=previous.feed_body,
                not_modified=True,
                last_checked_at=outcome.last_checked_at,
                observed_at=outcome.observed_at,
                content_hash=outcome.content_hash,
                etag=outcome.etag,
                last_modified=outcome.last_modified,
                degradations=outcome.degradations,
            )

        text = outcome.body.decode("utf-8", errors="replace") if outcome.body else outcome.text
        limit = self._max_entries if max_entries is None else max(1, int(max_entries))
        try:
            title, entries = parse_feed(text, feed_url=outcome.final_url, max_entries=limit)
        except ProviderParseError as exc:
            return FeedOutcome(
                feed_url=url,
                final_url=outcome.final_url,
                evidence=outcome.evidence,
                feed_text=text,
                feed_body=outcome.body,
                not_modified=outcome.not_modified,
                last_checked_at=outcome.last_checked_at,
                observed_at=outcome.observed_at,
                content_hash=outcome.content_hash,
                etag=outcome.etag,
                last_modified=outcome.last_modified,
                degradations=(exc.degradation(),),
            )

        return FeedOutcome(
            feed_url=url,
            final_url=outcome.final_url,
            title=title,
            entries=entries,
            evidence=outcome.evidence,
            feed_text=text,
            feed_body=outcome.body,
            not_modified=outcome.not_modified,
            last_checked_at=outcome.last_checked_at,
            observed_at=outcome.observed_at,
            content_hash=outcome.content_hash,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            degradations=outcome.degradations,
        )

    async def open_entries(
        self,
        outcome: FeedOutcome,
        *,
        research_id: str,
        project_key: str,
        owner_id: str | None = None,
        limit: int | None = None,
        deadline=None,
        source_type: SourceType = SourceType.SECONDARY,
        retention_policy: RetentionPolicy = RetentionPolicy.PUBLIC_7D,
        classification: Classification = Classification.PUBLIC,
    ) -> tuple[ReadOutcome, ...]:
        """Abre entradas concretas. Solo aqui se descarga contenido de la entrada."""
        selected: Sequence[FeedEntry] = outcome.entries
        if limit is not None:
            selected = selected[: max(0, int(limit))]
        results: list[ReadOutcome] = []
        for entry in selected:
            results.append(
                await self._reader.read(
                    entry.url,
                    research_id=research_id,
                    project_key=project_key,
                    owner_id=owner_id,
                    source_type=source_type,
                    retention_policy=retention_policy,
                    classification=classification,
                    deadline=deadline,
                )
            )
        return tuple(results)


def parse_feed(
    text: str, *, feed_url: str = "", max_entries: int = 25
) -> tuple[str | None, tuple[FeedEntry, ...]]:
    """Parsea RSS 2.0 o Atom. Sin descargar nada y tras rechazar DTD/entidades."""
    assert_safe_xml(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ProviderParseError(f"XML invalido: {exc}", provider="rss") from exc

    kind = _localname(root.tag)
    if kind == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            raise ProviderParseError("RSS sin canal", provider="rss")
        feed_title = _child_text(channel, "title")
        nodes = [node for node in list(channel) if _localname(node.tag) == "item"]
    elif kind == "feed":
        feed_title = _child_text(root, "title")
        nodes = [node for node in list(root) if _localname(node.tag) == "entry"]
    else:
        raise ProviderParseError(f"Documento XML no reconocido como feed: {kind}", provider="rss")

    entries: list[FeedEntry] = []
    for node in nodes[: max(1, int(max_entries))]:
        raw_link = _entry_link(node)
        if not raw_link:
            continue
        link = urljoin(feed_url, raw_link) if feed_url else raw_link
        if not link.startswith(("http://", "https://")):
            continue
        summary = _child_text(node, "description", "summary", "content")
        entries.append(
            FeedEntry(
                url=link,
                title=_child_text(node, "title"),
                published_at=parse_http_datetime(
                    _child_text(node, "pubdate", "published", "updated", "date")
                ),
                summary=normalize_text(summary) if summary else None,
            )
        )
    return feed_title, tuple(entries)
