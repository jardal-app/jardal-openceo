"""Almacen de evidencias por proyecto (SQLite/FTS5) con scope y retencion
(TASK-2026-00061, hijo 5/20 de TASK-2026-00056).

Por que existe
==============
El epico pide persistir y **consultar** evidencia con scope y retencion
verificables, sin mezclar el contenido con el estado de ejecucion. Mongo conserva
jobs, leases y presupuestos (TASK-2026-00057/00058); este modulo es el almacen de
archivos e indice lexico, detras de un repositorio.

Decisiones que evitan fallos concretos
======================================
* **Un store por proyecto, no uno global.** ``EvidenceVault`` abre un archivo
  SQLite por ``project_key`` con su propio directorio de blobs. El nombre del
  directorio sale de un hash interno del ``project_key``: ninguna cadena del
  usuario entra en una ruta (traversal imposible por construccion) y dos proyectos
  no comparten indice FTS ni blobs.
* **El scope se filtra ANTES de tocar el indice o el disco.** Un archivo por
  proyecto no separa owners: ``search()`` primero materializa los candidatos
  autorizados (proyecto + owner + techo de clasificacion) y solo despues ejecuta
  ``MATCH`` restringido a esos ``rowid``. Una fila restringida no es un resultado
  que se descarta: nunca llega a ser candidata. Leer el blob exige el mismo filtro,
  porque la ruta se obtiene de la fila ya filtrada.
* **FTS5 se verifica de verdad.** ``open()`` crea una tabla temporal FTS5, inserta
  una fila y ejecuta un ``MATCH`` real. Si el build de SQLite no trae FTS5 se falla
  con ``FTS5Unavailable`` en lugar de degradar a ``LIKE`` en silencio.
* **Blobs al disco, indice a SQLite.** El contenido normalizado se escribe en un
  temporal y se mueve con ``os.replace`` **antes** de insertar la fila: un crash
  deja un blob huerfano (inocuo) y nunca una fila apuntando a un archivo ausente.
* **La expiracion no usa barridos de background.** Se dispara al operar (cada
  ``save``/``get``/``search`` limpia lo vencido de su scope) o con una llamada
  explicita ``expire_due()``. Expirar borra FTS + blob y deja un **tombstone** sin
  contenido: sin el, un informe no podria distinguir "expiro" de "nunca existio" y
  devolveria una lista vacia que miente por omision.
* **Una URL firmada no manda la retencion.** La caducidad de una firma es del
  enlace, no del objeto: ``expires_at`` jamas se deduce de la URL.
* **El SERP de Brave no se almacena.** Las condiciones de Brave (epico, seccion
  11.2) prohiben guardar sus respuestas; el store rechaza el proveedor ``brave`` y
  el endpoint del API, en lugar de confiar en que el llamador se acuerde.
* **I/O bloqueante fuera del event loop.** Las corrutinas ``a*`` delegan en
  ``asyncio.to_thread`` y el acceso a la conexion se serializa con un lock.

Fuera de alcance en esta entrega: adquisicion, LLM, R2 y exportacion al vault. El
store no hace red.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.parse import urlsplit

from research.contracts import (
    AccessMethod,
    Classification,
    EvidenceSupport,
    ResearchEvidence,
    RetentionPolicy,
    SourceType,
)
from research.policies import RESEARCH_READ, Principal, ResearchAccessDenied

logger = logging.getLogger(__name__)

__all__ = [
    "EvidenceBlobMissing",
    "EvidenceConflict",
    "EvidenceError",
    "EvidenceHashMismatch",
    "EvidenceHit",
    "EvidenceLimits",
    "EvidencePathError",
    "EvidencePolicyError",
    "EvidenceRepository",
    "EvidenceScope",
    "EvidenceScopeError",
    "EvidenceState",
    "EvidenceStoreClosed",
    "EvidenceTooLarge",
    "ExpiryReport",
    "FTS5Unavailable",
    "RetentionWindows",
    "SQLiteEvidenceRepository",
    "SCHEMA_VERSION",
    "StoredEvidence",
    "EvidenceVault",
    "content_hash_of",
    "fts5_available",
    "fts_match_expression",
    "normalize_content",
    "project_dir_name",
    "safe_join",
    "scope_from_principal",
    "vault_from_settings",
]

SCHEMA_VERSION = 1
_FTS_TABLE = "evidence_fts"
_CANDIDATE_TABLE = "_evidence_scope_candidates"

# Un id de evidencia es interno (`res_ev_...`), pero se valida igual: si algun dia
# llega desde el payload, no puede convertirse en una componente de ruta.
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Palabras clave que identifican un SERP de buscador cuyo contrato prohibe
# almacenar la respuesta. No es una lista de proveedores "malos": es la aplicacion
# del punto 11.2 del epico.
_FORBIDDEN_PROVIDERS = frozenset({"brave", "brave_search", "brave-serp", "brave_serp"})
_FORBIDDEN_SOURCE_HOSTS = frozenset({"api.search.brave.com"})

# `\w` en modo str ya es Unicode: una consulta con acentos se tokeniza entera y no
# se parte por la vocal acentuada. Los terminos se citan, asi que la sintaxis FTS
# (`*`, `-`, `NEAR`, comillas) nunca llega como operador.
_TERM_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 16
_LOCK_RETRIES = 6
_BLOB_SUFFIX = ".blob"


# --------------------------------------------------------------------------- errores


class EvidenceError(RuntimeError):
    """Fallo del almacen de evidencias."""


class EvidencePathError(EvidenceError):
    """Ruta no derivada de un id interno, traversal o symlink."""


class EvidenceScopeError(EvidenceError):
    """Falta el scope o no corresponde al proyecto de este store."""


class EvidencePolicyError(EvidenceError):
    """Contenido cuya procedencia no se puede almacenar (p. ej. SERP de Brave)."""


class EvidenceTooLarge(EvidenceError):
    """Blob, indice o conjunto de candidatos por encima del limite."""


class EvidenceHashMismatch(EvidenceError):
    """El `content_hash` declarado no describe el contenido entregado."""


class EvidenceConflict(EvidenceError):
    """Mismo `evidence_id` con contenido distinto: no es una repeticion."""


class EvidenceBlobMissing(EvidenceError):
    """La fila existe pero el blob no: inconsistencia que se declara, no se oculta."""


class EvidenceStoreClosed(EvidenceError):
    """Se intento operar sobre un store ya cerrado."""


class FTS5Unavailable(EvidenceError):
    """El SQLite disponible no trae FTS5: no hay busqueda lexica real."""


# -------------------------------------------------------------------- normalizacion


# Ruido tipografico que no aporta significado: se elimina antes de hashear. No se
# toca caja, puntuacion ni acentos: eso es contenido material.
_INVISIBLE = dict.fromkeys(
    (ord(ch) for ch in "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None
)


def normalize_content(text: str) -> str:
    """Normaliza ruido de formato sin quitar contenido.

    NFKC unifica ligaduras y variantes de ancho; los finales de linea se unifican;
    se colapsan espacios/tabuladores y lineas en blanco repetidas. La caja, la
    puntuacion y los acentos se conservan a proposito: dos textos que dicen cosas
    distintas no pueden compartir hash.
    """
    if text is None:
        raise EvidenceError("El contenido no puede ser None")
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    unified = unicodedata.normalize("NFKC", unified).translate(_INVISIBLE)
    lines: list[str] = []
    blanks = 0
    for raw_line in unified.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        if line:
            blanks = 0
            lines.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                lines.append("")
    return "\n".join(lines).strip()


def content_hash_of(normalized: str) -> str:
    """SHA-256 hex del contenido **ya normalizado**. Nunca se hashea el ruido."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fts_match_expression(query: str) -> str | None:
    """Expresion FTS5 segura para texto libre.

    Los operandos se citan y se limita el numero de terminos: un ``MATCH`` con
    sintaxis del usuario puede ser un error de parseo o una consulta costosa.
    """
    terms = _TERM_RE.findall(query or "")
    if not terms:
        return None
    return " ".join(f'"{term}"' for term in terms[:_MAX_QUERY_TERMS])


# ------------------------------------------------------------------------ retencion


@dataclass(frozen=True)
class RetentionWindows:
    """Plazos propuestos en el epico, subordinados a la politica de fuente/owner."""

    public_days: int = 7
    watch_days: int = 90
    report_days: int = 180

    @classmethod
    def from_settings(cls, settings: object) -> "RetentionWindows":
        return cls(
            public_days=int(getattr(settings, "RESEARCH_PUBLIC_RETENTION_DAYS", 7)),
            watch_days=int(getattr(settings, "RESEARCH_WATCH_RETENTION_DAYS", 90)),
            report_days=int(getattr(settings, "RESEARCH_REPORT_RETENTION_DAYS", 180)),
        )

    def days_for(self, policy: RetentionPolicy) -> int | None:
        """Dias de retencion, o ``None`` cuando manda la politica de la fuente/owner.

        ``SOURCE_SPECIFIC`` no inventa un plazo: el owner o la fuente lo fijan y el
        store lo recibe explicito. Un plazo inventado borraria evidencia legitima.
        """
        if policy is RetentionPolicy.PUBLIC_7D:
            return self.public_days
        if policy is RetentionPolicy.WATCH_90D:
            return self.watch_days
        if policy is RetentionPolicy.REPORT_180D:
            return self.report_days
        return None


@dataclass(frozen=True)
class EvidenceLimits:
    """Limites duros del store. Existen para que un input no agote disco o RAM."""

    max_blob_bytes: int = 5_000_000
    max_index_chars: int = 200_000
    max_search_results: int = 50
    max_candidates: int = 10_000


# ------------------------------------------------------------------------- modelo


@dataclass(frozen=True)
class EvidenceScope:
    """Quien consulta y hasta que clasificacion. Nunca lo aporta el payload.

    ``owner_id`` es obligatorio a proposito: no existe una consulta "de todos los
    owners del proyecto", que es justo el agujero que el epico prohibe.
    """

    project_key: str
    owner_id: str
    max_classification: Classification = Classification.PUBLIC

    def __post_init__(self) -> None:
        if not str(self.project_key or "").strip():
            raise EvidenceScopeError("EvidenceScope sin project_key")
        if not str(self.owner_id or "").strip():
            raise EvidenceScopeError("EvidenceScope sin owner_id")
        if not isinstance(self.max_classification, Classification):
            raise EvidenceScopeError("max_classification debe ser Classification")


class EvidenceState(str, Enum):
    """Estado de recuperabilidad de una evidencia referenciada por un informe."""

    AVAILABLE = "available"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StoredEvidence:
    evidence: ResearchEvidence
    content_hash: str
    blob_relpath: str
    created_at: datetime
    expires_at: datetime | None
    support: EvidenceSupport | None = None
    provider: str | None = None
    # Contenido normalizado. Solo lo carga `get`; en busquedas queda en disco.
    content: str | None = None

    @property
    def evidence_id(self) -> str:
        return self.evidence.id


@dataclass(frozen=True)
class EvidenceHit:
    evidence: ResearchEvidence
    content_hash: str
    support: EvidenceSupport | None
    rank: float
    expires_at: datetime | None


@dataclass(frozen=True)
class ExpiryReport:
    expired_ids: tuple[str, ...]
    expired_at: datetime
    reason: str = "retention_expired"


@runtime_checkable
class EvidenceRepository(Protocol):
    """Contrato del repositorio. La implementacion SQLite es una, no la unica."""

    def save(
        self,
        evidence: ResearchEvidence,
        *,
        content: str | bytes,
        support: EvidenceSupport | None = None,
        provider: str | None = None,
        retention_days: int | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> StoredEvidence: ...

    def get(self, scope: EvidenceScope, evidence_id: str, *, now: datetime | None = None) -> StoredEvidence | None: ...

    def search(
        self, scope: EvidenceScope, query: str, *, limit: int | None = None, now: datetime | None = None
    ) -> list[EvidenceHit]: ...

    def candidate_ids(self, scope: EvidenceScope, *, now: datetime | None = None) -> list[str]: ...

    def expire_due(
        self, scope: EvidenceScope | None = None, *, now: datetime | None = None
    ) -> ExpiryReport: ...

    def evidence_state(self, scope: EvidenceScope, evidence_id: str) -> EvidenceState: ...

    def missing_evidence_notes(self, scope: EvidenceScope, evidence_ids: list[str]) -> list[str]: ...

    def close(self) -> None: ...


# ------------------------------------------------------------------------ fechas


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _now(moment: datetime | None = None) -> datetime:
    return _aware(moment) or datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    value = _aware(moment)
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: object) -> datetime | None:
    """Desconocido es ``None``, nunca epoch.

    El esquema guarda NULL para una fecha no medida. Convertir NULL a ``0`` o a
    ``1970-01-01`` afirmaria una observacion que nadie hizo.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _aware(datetime.fromisoformat(text))


# ------------------------------------------------------------------------- rutas


def project_dir_name(project_key: str) -> str:
    """Nombre de directorio derivado del proyecto. Nunca se concatena el texto.

    El prefijo ``project:`` separa el dominio de otros hashes; el resultado es
    hexadecimal, asi que ``..``, ``/`` o una URL no pueden aparecer.
    """
    material = str(project_key or "")
    if not material.strip():
        raise EvidenceScopeError("project_key vacio")
    return hashlib.sha256(("project:" + material).encode("utf-8")).hexdigest()[:32]


def safe_join(base: str | Path, relpath: str) -> Path:
    """Resuelve ``base/relpath`` y **rechaza** traversal y symlinks.

    No basta con normalizar: se comprueba que la ruta real cae dentro de la base y
    que ninguna componente es un enlace. Un enlace dentro de la base tambien se
    rechaza: el blob debe ser un archivo regular, no un puntero a otra cosa.
    """
    text = str(relpath or "")
    if not text or "\x00" in text or os.path.isabs(text) or text.startswith(("/", "\\")):
        raise EvidencePathError(f"Ruta relativa invalida: {relpath!r}")
    parts = Path(text).parts
    if any(part in ("..", ".") for part in parts):
        raise EvidencePathError(f"Traversal en la ruta: {relpath!r}")

    base_path = Path(base)
    target = base_path / text
    base_real = Path(os.path.realpath(base_path))
    target_real = Path(os.path.realpath(target))
    try:
        common = os.path.commonpath([str(base_real), str(target_real)])
    except ValueError as exc:  # rutas en volumenes distintos
        raise EvidencePathError(f"Ruta fuera de la base: {relpath!r}") from exc
    if common != str(base_real):
        raise EvidencePathError(f"Ruta fuera de la base: {relpath!r}")

    current = target
    while True:
        if os.path.islink(current):
            raise EvidencePathError(f"Symlink rechazado: {current}")
        parent = current.parent
        if parent == current or Path(os.path.realpath(parent)) == base_real:
            break
        current = parent
    return target


def _validate_evidence_id(evidence_id: str) -> str:
    text = str(evidence_id or "")
    if not _EVIDENCE_ID_RE.match(text) or ".." in text:
        raise EvidencePathError(f"evidence_id invalido: {evidence_id!r}")
    return text


def _blob_relpath(evidence_id: str, content_hash: str) -> str:
    """Ruta del blob derivada de ids internos, con el hash del contenido.

    Incluir el hash evita que dos escritores concurrentes con el mismo
    ``evidence_id`` y contenido distinto se pisen el archivo: el perdedor de la
    carrera borra **su** blob, no el de la fila que gano. Mismo id y mismo
    contenido producen la misma ruta, asi que la idempotencia sigue siendo exacta.
    """
    _validate_evidence_id(evidence_id)
    digest = hashlib.sha256(evidence_id.encode("utf-8")).hexdigest()
    return f"{digest[:2]}/{evidence_id}-{content_hash[:16]}{_BLOB_SUFFIX}"


def _is_forbidden_provider(provider: str | None) -> bool:
    if provider is None:
        return False
    return str(provider).strip().lower() in _FORBIDDEN_PROVIDERS


def _is_forbidden_source_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _FORBIDDEN_SOURCE_HOSTS


# ------------------------------------------------------------------------- FTS5


def fts5_available(connection: sqlite3.Connection) -> bool:
    """Comprueba FTS5 con una consulta real, no con `hasattr` ni una suposicion."""
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.__fts5_probe USING fts5(body)")
        connection.execute("DELETE FROM temp.__fts5_probe")
        connection.execute(
            "INSERT INTO temp.__fts5_probe(body) VALUES (?)", ("sonda fts5 contrato",)
        )
        rows = connection.execute(
            "SELECT count(*) FROM temp.__fts5_probe WHERE __fts5_probe MATCH ?",
            ("contrato",),
        ).fetchone()
        return bool(rows and int(rows[0]) == 1)
    except sqlite3.Error:
        return False
    finally:
        try:
            connection.execute("DROP TABLE IF EXISTS temp.__fts5_probe")
        except sqlite3.Error:  # pragma: no cover - la sonda ya fallo
            pass


def _require_fts5(connection: sqlite3.Connection) -> None:
    if not fts5_available(connection):
        raise FTS5Unavailable(
            "El SQLite de este runtime no ofrece FTS5: no hay busqueda lexica real "
            "y el store no degrada a LIKE en silencio"
        )


# ------------------------------------------------------------------ esquema v1


_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_id TEXT NOT NULL UNIQUE,
        project_key TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        classification TEXT NOT NULL,
        classification_severity INTEGER NOT NULL,
        access_method TEXT NOT NULL,
        source_type TEXT NOT NULL,
        retention_policy TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        support_location TEXT,
        support_extract TEXT,
        provider TEXT,
        blob_relpath TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        published_at TEXT,
        event_at TEXT,
        observed_at TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_evidence_scope
        ON evidence(project_key, owner_id, classification_severity, id)
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_expiry ON evidence(expires_at)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
        title, body, tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_tombstones (
        evidence_id TEXT NOT NULL,
        project_key TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        classification TEXT NOT NULL,
        classification_severity INTEGER NOT NULL,
        retention_policy TEXT NOT NULL,
        content_hash TEXT,
        observed_at TEXT,
        expired_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (evidence_id, project_key, owner_id)
    )
    """,
)


# ------------------------------------------------------------------------- store


class SQLiteEvidenceRepository:
    """Repositorio de evidencias de **un** proyecto sobre SQLite + FTS5.

    Una instancia mantiene una conexion serializada por un ``RLock``. El archivo
    SQLite es local, jamas compartido por red: WAL + ``busy_timeout`` serializan
    escritores, y un lock retenido falla con error claro en vez de corromper.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        project_key: str,
        limits: EvidenceLimits | None = None,
        retention: RetentionWindows | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._project_key = str(project_key or "").strip()
        if not self._project_key:
            raise EvidenceScopeError("project_key vacio")
        self._root = _local_root(root)
        self._limits = limits or EvidenceLimits()
        self._retention = retention or RetentionWindows()
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._project_dir: Path | None = None
        self._blobs_dir: Path | None = None
        self.open()

    # ------------------------------------------------------------------ apertura

    @property
    def project_key(self) -> str:
        return self._project_key

    @property
    def project_dir(self) -> Path:
        self._require_open()
        assert self._project_dir is not None
        return self._project_dir

    @property
    def blobs_dir(self) -> Path:
        self._require_open()
        assert self._blobs_dir is not None
        return self._blobs_dir

    @property
    def database_path(self) -> Path:
        return self.project_dir / "evidence.sqlite3"

    @property
    def schema_version(self) -> int:
        with self._lock:
            conn = self._require_open()
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def open(self) -> "SQLiteEvidenceRepository":
        with self._lock:
            if self._conn is not None:
                return self
            project_dir, blobs_dir = self._ensure_dirs()
            connection = sqlite3.connect(
                str(project_dir / "evidence.sqlite3"),
                check_same_thread=False,
                isolation_level=None,  # autocommit: las transacciones se abren explicitas
                timeout=self._busy_timeout_ms / 1000.0,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise EvidenceError(
                        f"journal_mode={mode}: el store exige WAL local para serializar "
                        "escritores; un SQLite por red no es una solucion distribuida"
                    )
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA foreign_keys=ON")
                _require_fts5(connection)
                self._migrate(connection)
            except BaseException:
                connection.close()
                raise
            self._conn = connection
            self._project_dir = project_dir
            self._blobs_dir = blobs_dir
            return self

    def _ensure_dirs(self) -> tuple[Path, Path]:
        root = self._root
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise EvidencePathError(f"La raiz del store no puede ser un symlink: {root}")
        project_dir = root / project_dir_name(self._project_key)
        project_dir.mkdir(parents=True, exist_ok=True)
        if project_dir.is_symlink():
            raise EvidencePathError(
                f"El directorio de proyecto no puede ser un symlink: {project_dir}"
            )
        blobs_dir = project_dir / "blobs"
        blobs_dir.mkdir(exist_ok=True)
        if blobs_dir.is_symlink():
            raise EvidencePathError(f"El directorio de blobs no puede ser un symlink: {blobs_dir}")
        root_real = Path(os.path.realpath(root))
        project_real = Path(os.path.realpath(project_dir))
        if os.path.commonpath([str(root_real), str(project_real)]) != str(root_real):
            raise EvidencePathError("El directorio de proyecto escapa de la raiz del store")
        return project_dir, blobs_dir

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise EvidenceError(
                f"El store esta en la version {version} y este codigo solo conoce "
                f"{SCHEMA_VERSION}: se niega a degradarlo"
            )
        if version == SCHEMA_VERSION:
            return
        # Migracion idempotente y transaccional: reejecutarla no duplica objetos y
        # una interrupcion no deja el esquema a medias.
        with self._write_txn(connection) as conn:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current < 1:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        logger.info(
            "evidence store migrado a v%s (proyecto %s)", SCHEMA_VERSION, self._project_key
        )

    # ------------------------------------------------------------- transacciones

    @contextmanager
    def _write_txn(self, connection: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        """Transaccion de escritura con reintento ante lock de otro escritor."""
        with self._lock:
            conn = connection or self._require_open()
            for attempt in range(_LOCK_RETRIES):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        if attempt + 1 < _LOCK_RETRIES:
                            time.sleep(0.02 * (attempt + 1))
                            continue
                    raise
                try:
                    yield conn
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:  # pragma: no cover - la transaccion ya murio
                        pass
                    raise
                else:
                    conn.execute("COMMIT")
                    return
            raise EvidenceError("No se pudo obtener el lock de escritura de SQLite")

    # ------------------------------------------------------------------- guardas

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise EvidenceStoreClosed("El store de evidencias esta cerrado")
        return self._conn

    def _validated_scope(self, scope: EvidenceScope) -> EvidenceScope:
        if not isinstance(scope, EvidenceScope):
            raise EvidenceScopeError("Se requiere un EvidenceScope")
        if scope.project_key != self._project_key:
            raise EvidenceScopeError(
                f"El scope es del proyecto {scope.project_key!r} y este store es de "
                f"{self._project_key!r}"
            )
        return scope

    def _severity(self, classification: Classification) -> int:
        return int(classification.severity)

    # --------------------------------------------------------------------- save

    def save(
        self,
        evidence: ResearchEvidence,
        *,
        content: str | bytes,
        support: EvidenceSupport | None = None,
        provider: str | None = None,
        retention_days: int | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> StoredEvidence:
        """Persiste evidencia y blob. Idempotente por ``evidence_id`` + hash."""
        with self._lock:
            self._require_open()
            if evidence.project_key != self._project_key:
                raise EvidenceScopeError(
                    "La evidencia pertenece a otro proyecto; el store no la acepta"
                )
            if not str(evidence.project_key or "").strip() or not str(evidence.owner_id or "").strip():
                raise EvidenceScopeError("project_key y owner_id son obligatorios al guardar")
            if _is_forbidden_provider(provider):
                raise EvidencePolicyError(
                    f"El proveedor {provider!r} prohibe almacenar sus respuestas (SERP); "
                    "solo se persiste la procedencia de la pagina visitada"
                )
            if _is_forbidden_source_url(evidence.url):
                raise EvidencePolicyError(
                    "No se almacena una respuesta del endpoint de busqueda; se guarda la "
                    "URL de la publicacion efectivamente visitada"
                )

            evidence_id = _validate_evidence_id(evidence.id)
            try:
                text = content if isinstance(content, str) else bytes(content).decode("utf-8")
            except (UnicodeDecodeError, TypeError) as exc:
                raise EvidenceError("El contenido debe ser texto UTF-8") from exc
            normalized = normalize_content(text)
            payload = normalized.encode("utf-8")
            if len(payload) > self._limits.max_blob_bytes:
                raise EvidenceTooLarge(
                    f"El blob ({len(payload)} bytes) supera el limite "
                    f"{self._limits.max_blob_bytes}"
                )
            digest = content_hash_of(normalized)
            if evidence.content_hash and evidence.content_hash != digest:
                # Guardar un hash que no describe el contenido convertiria la
                # deduplicacion y la verificacion en una mentira.
                raise EvidenceHashMismatch(
                    "content_hash declarado no coincide con el hash del contenido normalizado"
                )

            moment = _now(now)
            expiry = self._resolve_expiry(
                evidence, now=moment, retention_days=retention_days, explicit=expires_at
            )
            blob_relpath = _blob_relpath(evidence_id, digest)
            blob_path = safe_join(self.blobs_dir, blob_relpath)

            # Operacion que limpia: al escribir se purga lo vencido de **todo** el
            # owner (el techo PRIVATE cubre las tres clasificaciones), porque quien
            # escribe es dueno principal de esas filas y no hay barrido de fondo.
            self._expire_due_locked(
                EvidenceScope(
                    self._project_key, evidence.owner_id, Classification.PRIVATE
                ),
                now=moment,
            )

            existing = self._fetch_row_locked(evidence_id)
            if existing is not None:
                if existing["owner_id"] != evidence.owner_id:
                    # Un id no puede cambiar de dueno: devolver la fila del otro
                    # owner filtraria su metadata a quien no la posee.
                    raise EvidenceConflict(
                        f"{evidence_id} ya pertenece a otro owner dentro del proyecto"
                    )
                if existing["content_hash"] == digest:
                    return self._row_to_stored(existing)
                raise EvidenceConflict(
                    f"{evidence_id} ya existe con otro contenido; se niega a sobrescribir"
                )

            # Blob primero (atomico) y fila despues: un crash deja un huerfano
            # inocuo, nunca una fila sin archivo.
            self._write_blob_locked(blob_path, payload)
            classification = evidence.visibility
            index_body = self._index_body(normalized, support)
            try:
                with self._write_txn() as conn:
                    conn.execute(
                        """
                        INSERT INTO evidence (
                            evidence_id, project_key, owner_id, classification,
                            classification_severity, access_method, source_type,
                            retention_policy, url, title, support_location,
                            support_extract, provider, blob_relpath, content_hash,
                            published_at, event_at, observed_at, created_at, expires_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            evidence_id,
                            evidence.project_key,
                            evidence.owner_id,
                            classification.value,
                            self._severity(classification),
                            evidence.access_method.value,
                            evidence.source_type.value,
                            evidence.retention_policy.value,
                            evidence.url,
                            evidence.title,
                            support.location if support else None,
                            support.extract if support else None,
                            provider,
                            blob_relpath,
                            digest,
                            _iso(evidence.published_at),
                            _iso(evidence.event_at),
                            _iso(evidence.observed_at),
                            _iso(moment),
                            _iso(expiry),
                        ),
                    )
                    rowid = conn.execute(
                        "SELECT id FROM evidence WHERE evidence_id=?", (evidence_id,)
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT INTO evidence_fts(rowid, title, body) VALUES (?,?,?)",
                        (rowid, evidence.title or "", index_body),
                    )
            except sqlite3.IntegrityError as exc:
                # Carrera entre dos escritores por el mismo id.
                current = self._fetch_row_locked(evidence_id)
                if (
                    current is not None
                    and current["owner_id"] == evidence.owner_id
                    and current["content_hash"] == digest
                ):
                    # Mismo id y mismo contenido: la ruta del blob tambien coincide y
                    # contiene bytes identicos, asi que no se borra nada.
                    return self._row_to_stored(current)
                self._unlink_blob_locked(blob_relpath)
                raise EvidenceConflict(
                    f"{evidence_id} ya existe con otro contenido"
                ) from exc
            except BaseException:
                self._unlink_blob_locked(blob_relpath)
                raise

            row = self._fetch_row_locked(evidence_id)
            assert row is not None
            return self._row_to_stored(row)

    def _index_body(self, normalized: str, support: EvidenceSupport | None) -> str:
        parts = [normalized[: self._limits.max_index_chars]]
        if support and support.extract:
            parts.append(support.extract)
        return "\n".join(parts)

    def _resolve_expiry(
        self,
        evidence: ResearchEvidence,
        *,
        now: datetime,
        retention_days: int | None,
        explicit: datetime | None,
    ) -> datetime | None:
        """Plazo efectivo. La politica del owner/fuente solo puede recortar.

        Un plazo explicito o un override nunca **amplian** el de la politica: si un
        llamador pide 30 dias para contenido publico de 7, gana el mas corto.
        """
        policy_days = self._retention.days_for(evidence.retention_policy)
        expiry = now + timedelta(days=policy_days) if policy_days is not None else None
        if retention_days is not None:
            override = now + timedelta(days=max(0, int(retention_days)))
            expiry = override if expiry is None else min(expiry, override)
        if explicit is not None:
            explicit_aware = _aware(explicit)
            assert explicit_aware is not None
            expiry = explicit_aware if expiry is None else min(expiry, explicit_aware)
        return expiry

    # ---------------------------------------------------------------------- get

    def get(
        self, scope: EvidenceScope, evidence_id: str, *, now: datetime | None = None
    ) -> StoredEvidence | None:
        """Devuelve la evidencia si esta en el scope; si no, ``None`` sin filtrar datos."""
        scope = self._validated_scope(scope)
        evidence_id = _validate_evidence_id(evidence_id)
        with self._lock:
            self._require_open()
            self._expire_due_locked(scope, now=now)
            row = self._fetch_scoped_row_locked(scope, evidence_id)
            if row is None:
                return None
            blob = self._read_blob_locked(row["blob_relpath"])
            return self._row_to_stored(row, content=blob.decode("utf-8"))

    # ------------------------------------------------------------------- search

    def candidate_ids(self, scope: EvidenceScope, *, now: datetime | None = None) -> list[str]:
        """Ids autorizados **antes** de consultar FTS. Base de la busqueda."""
        scope = self._validated_scope(scope)
        with self._lock:
            self._require_open()
            moment = _now(now)
            self._expire_due_locked(scope, now=moment)
            return [row["evidence_id"] for row in self._candidate_rows_locked(scope, moment)]

    def search(
        self,
        scope: EvidenceScope,
        query: str,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[EvidenceHit]:
        """Busqueda lexica restringida al scope autorizado.

        El orden importa: se materializan los candidatos (proyecto/owner/techo) y
        solo entonces se ejecuta ``MATCH`` contra ese conjunto. Asi una fila
        restringida nunca aparece ni siquiera como candidata.
        """
        scope = self._validated_scope(scope)
        expression = fts_match_expression(query)
        if expression is None:
            return []
        effective = int(limit) if limit is not None else self._limits.max_search_results
        effective = max(0, min(effective, self._limits.max_search_results))
        if effective == 0:
            return []
        with self._lock:
            conn = self._require_open()
            moment = _now(now)
            self._expire_due_locked(scope, now=moment)
            candidates = self._candidate_rows_locked(scope, moment)
            if not candidates:
                # Sin candidatos autorizados ni se toca el indice: no hay nada que
                # buscar y no se revela si existe contenido de otro owner.
                return []
            conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_CANDIDATE_TABLE}(id INTEGER PRIMARY KEY)")
            conn.execute(f"DELETE FROM {_CANDIDATE_TABLE}")
            conn.executemany(
                f"INSERT INTO {_CANDIDATE_TABLE}(id) VALUES (?)",
                [(int(row["id"]),) for row in candidates],
            )
            rows = conn.execute(
                f"""
                SELECT e.*, bm25({_FTS_TABLE}) AS rank
                FROM {_FTS_TABLE}
                JOIN {_CANDIDATE_TABLE} c ON c.id = {_FTS_TABLE}.rowid
                JOIN evidence e ON e.id = {_FTS_TABLE}.rowid
                WHERE {_FTS_TABLE} MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (expression, effective),
            ).fetchall()
            conn.execute(f"DELETE FROM {_CANDIDATE_TABLE}")
            return [
                EvidenceHit(
                    evidence=self._row_to_evidence(row),
                    content_hash=row["content_hash"],
                    support=self._row_to_support(row),
                    rank=float(row["rank"]),
                    expires_at=_parse_dt(row["expires_at"]),
                )
                for row in rows
            ]

    def count(self, scope: EvidenceScope, *, now: datetime | None = None) -> int:
        scope = self._validated_scope(scope)
        with self._lock:
            self._require_open()
            moment = _now(now)
            self._expire_due_locked(scope, now=moment)
            row = self._require_open().execute(
                """
                SELECT count(*) FROM evidence
                WHERE project_key=? AND owner_id=? AND classification_severity<=?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (scope.project_key, scope.owner_id, self._severity(scope.max_classification), _iso(moment)),
            ).fetchone()
            return int(row[0])

    def _candidate_rows_locked(self, scope: EvidenceScope, now: datetime) -> list[sqlite3.Row]:
        rows = self._require_open().execute(
            """
            SELECT id, evidence_id FROM evidence
            WHERE project_key=? AND owner_id=? AND classification_severity<=?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY id
            LIMIT ?
            """,
            (
                scope.project_key,
                scope.owner_id,
                self._severity(scope.max_classification),
                _iso(now),
                self._limits.max_candidates + 1,
            ),
        ).fetchall()
        if len(rows) > self._limits.max_candidates:
            raise EvidenceTooLarge(
                "El conjunto de candidatos autorizados supera el limite; se exige "
                "acotar la consulta en vez de indexar sin control"
            )
        return rows

    # ------------------------------------------------------------------- expiry

    def expire_due(
        self, scope: EvidenceScope | None = None, *, now: datetime | None = None
    ) -> ExpiryReport:
        """Limpieza por operacion o vencimiento persistido. Sin barridos de fondo."""
        with self._lock:
            self._require_open()
            moment = _now(now)
            validated = self._validated_scope(scope) if scope is not None else None
            return self._expire_due_locked(validated, now=moment)

    def _expire_due_locked(
        self, scope: EvidenceScope | None, *, now: datetime
    ) -> ExpiryReport:
        moment = _now(now)
        sql = "SELECT * FROM evidence WHERE expires_at IS NOT NULL AND expires_at <= ?"
        params: list[object] = [_iso(moment)]
        if scope is not None:
            sql += " AND project_key=? AND owner_id=? AND classification_severity<=?"
            params.extend(
                [scope.project_key, scope.owner_id, self._severity(scope.max_classification)]
            )
        sql += " ORDER BY id"
        rows = self._require_open().execute(sql, tuple(params)).fetchall()
        if not rows:
            return ExpiryReport((), moment)
        with self._write_txn() as conn:
            for row in rows:
                conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid=?", (row["id"],))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO evidence_tombstones (
                        evidence_id, project_key, owner_id, classification,
                        classification_severity, retention_policy, content_hash,
                        observed_at, expired_at, reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["evidence_id"],
                        row["project_key"],
                        row["owner_id"],
                        row["classification"],
                        row["classification_severity"],
                        row["retention_policy"],
                        row["content_hash"],
                        row["observed_at"],
                        _iso(moment),
                        "retention_expired",
                    ),
                )
                conn.execute("DELETE FROM evidence WHERE id=?", (row["id"],))
        # Los blobs se borran despues del commit: si algo falla antes, la fila y el
        # tombstone siguen coherentes y solo queda un archivo huerfano.
        for row in rows:
            self._unlink_blob_locked(row["blob_relpath"])
        expired = tuple(row["evidence_id"] for row in rows)
        logger.info(
            "evidence expired proyecto=%s ids=%s", self._project_key, ",".join(expired)
        )
        return ExpiryReport(expired, moment)

    # ------------------------------------------------- informes y recuperabilidad

    def evidence_state(self, scope: EvidenceScope, evidence_id: str) -> EvidenceState:
        """Estado honesto de una evidencia referenciada. No revela fuera de scope."""
        scope = self._validated_scope(scope)
        evidence_id = _validate_evidence_id(evidence_id)
        with self._lock:
            self._require_open()
            self._expire_due_locked(scope, now=None)
            if self._fetch_scoped_row_locked(scope, evidence_id) is not None:
                return EvidenceState.AVAILABLE
            tombstone = self._fetch_tombstone_locked(scope, evidence_id)
            if tombstone is not None:
                return EvidenceState.EXPIRED
            return EvidenceState.UNKNOWN

    def missing_evidence_notes(self, scope: EvidenceScope, evidence_ids: list[str]) -> list[str]:
        """Explicacion para el informe: que evidencia ya no es recuperable y por que.

        Devuelve una nota por id ausente en lugar de una lista vacia que se
        confundiria con "el informe no citaba nada".
        """
        notes: list[str] = []
        for evidence_id in evidence_ids or []:
            state = self.evidence_state(scope, evidence_id)
            if state is EvidenceState.AVAILABLE:
                continue
            if state is EvidenceState.EXPIRED:
                tombstone = self._fetch_tombstone_locked(scope, evidence_id)
                expired_at = tombstone["expired_at"] if tombstone is not None else None
                notes.append(
                    f"evidencia {evidence_id}: expirada por retencion y eliminada del "
                    f"indice y del disco ({expired_at}); ya no es recuperable"
                )
            else:
                notes.append(
                    f"evidencia {evidence_id}: no esta disponible en el scope autorizado "
                    "(ausente o fuera de proyecto/owner/clasificacion); no es recuperable aqui"
                )
        return notes

    # ----------------------------------------------------------- acceso a filas

    def _fetch_row_locked(self, evidence_id: str) -> sqlite3.Row | None:
        return self._require_open().execute(
            "SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()

    def _fetch_scoped_row_locked(self, scope: EvidenceScope, evidence_id: str) -> sqlite3.Row | None:
        return self._require_open().execute(
            """
            SELECT * FROM evidence
            WHERE evidence_id=? AND project_key=? AND owner_id=? AND classification_severity<=?
            """,
            (
                evidence_id,
                scope.project_key,
                scope.owner_id,
                self._severity(scope.max_classification),
            ),
        ).fetchone()

    def _fetch_tombstone_locked(self, scope: EvidenceScope, evidence_id: str) -> sqlite3.Row | None:
        return self._require_open().execute(
            """
            SELECT * FROM evidence_tombstones
            WHERE evidence_id=? AND project_key=? AND owner_id=? AND classification_severity<=?
            """,
            (
                evidence_id,
                scope.project_key,
                scope.owner_id,
                self._severity(scope.max_classification),
            ),
        ).fetchone()

    def _row_to_evidence(self, row: sqlite3.Row) -> ResearchEvidence:
        return ResearchEvidence(
            id=row["evidence_id"],
            url=row["url"],
            access_method=AccessMethod(row["access_method"]),
            visibility=Classification(row["classification"]),
            source_type=SourceType(row["source_type"]),
            title=row["title"],
            published_at=_parse_dt(row["published_at"]),
            event_at=_parse_dt(row["event_at"]),
            observed_at=_parse_dt(row["observed_at"]),
            content_hash=row["content_hash"],
            retention_policy=RetentionPolicy(row["retention_policy"]),
            project_key=row["project_key"],
            owner_id=row["owner_id"],
        )

    def _row_to_support(self, row: sqlite3.Row) -> EvidenceSupport | None:
        location = row["support_location"]
        extract = row["support_extract"]
        if not location and not extract:
            return None
        return EvidenceSupport(location=location, extract=extract)

    def _row_to_stored(self, row: sqlite3.Row, content: str | None = None) -> StoredEvidence:
        return StoredEvidence(
            evidence=self._row_to_evidence(row),
            content_hash=row["content_hash"],
            blob_relpath=row["blob_relpath"],
            created_at=_parse_dt(row["created_at"]) or _now(),
            expires_at=_parse_dt(row["expires_at"]),
            support=self._row_to_support(row),
            provider=row["provider"],
            content=content,
        )

    # --------------------------------------------------------------------- blobs

    def blob_path(self, relpath: str) -> Path:
        """Ruta absoluta validada para un blob relativo ya derivado del id."""
        return safe_join(self.blobs_dir, relpath)

    def _write_blob_locked(self, path: Path, payload: bytes) -> None:
        parent = path.parent
        if parent.is_symlink():
            raise EvidencePathError(f"Directorio de blob es un symlink: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            # Nunca se escribe siguiendo un enlace colocado por terceros.
            raise EvidencePathError(f"El blob destino es un symlink: {path}")
        handle_fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(parent))
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _read_blob_locked(self, relpath: str) -> bytes:
        path = self.blob_path(relpath)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError as exc:
            raise EvidenceBlobMissing(
                f"La fila declara {relpath!r} pero el blob no existe: inconsistencia declarada"
            ) from exc

    def _unlink_blob_locked(self, relpath: str) -> None:
        """Borra el blob sin seguir symlinks; un enlace se elimina, no su destino."""
        try:
            path = safe_join(self.blobs_dir, relpath)
        except EvidencePathError:
            logger.warning("blob con ruta no valida, no se borra: %r", relpath)
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - permisos del filesystem
            logger.warning("no se pudo borrar el blob %s: %s", path, exc)

    # --------------------------------------------------------------------- cierre

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:  # pragma: no cover - checkpoint best-effort
                pass
            finally:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "SQLiteEvidenceRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ asincrono

    async def asave(self, evidence: ResearchEvidence, **kwargs: object) -> StoredEvidence:
        return await asyncio.to_thread(self.save, evidence, **kwargs)

    async def aget(
        self, scope: EvidenceScope, evidence_id: str, *, now: datetime | None = None
    ) -> StoredEvidence | None:
        return await asyncio.to_thread(self.get, scope, evidence_id, now=now)

    async def asearch(
        self,
        scope: EvidenceScope,
        query: str,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[EvidenceHit]:
        return await asyncio.to_thread(self.search, scope, query, limit=limit, now=now)

    async def acandidate_ids(
        self, scope: EvidenceScope, *, now: datetime | None = None
    ) -> list[str]:
        return await asyncio.to_thread(self.candidate_ids, scope, now=now)

    async def aexpire_due(
        self, scope: EvidenceScope | None = None, *, now: datetime | None = None
    ) -> ExpiryReport:
        return await asyncio.to_thread(self.expire_due, scope, now=now)


# --------------------------------------------------------------------------- vault


class EvidenceVault:
    """Raiz del almacen: abre y conserva un repositorio por proyecto.

    La raiz es local. Un ``file:`` URI o una ruta UNC se rechazan: SQLite sobre red
    no serializa de forma fiable y el epico pide explicitamente una sola instancia
    escritora local.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        limits: EvidenceLimits | None = None,
        retention: RetentionWindows | None = None,
    ) -> None:
        self._root = _local_root(root)
        self._limits = limits or EvidenceLimits()
        self._retention = retention or RetentionWindows()
        self._lock = threading.RLock()
        self._repositories: dict[str, SQLiteEvidenceRepository] = {}

    @property
    def root(self) -> Path:
        return self._root

    def project(self, project_key: str) -> SQLiteEvidenceRepository:
        with self._lock:
            key = str(project_key or "").strip()
            if not key:
                raise EvidenceScopeError("project_key vacio")
            repository = self._repositories.get(key)
            if repository is None:
                repository = SQLiteEvidenceRepository(
                    root=self._root,
                    project_key=key,
                    limits=self._limits,
                    retention=self._retention,
                )
                self._repositories[key] = repository
            return repository

    def close(self) -> None:
        with self._lock:
            for repository in self._repositories.values():
                repository.close()
            self._repositories.clear()

    def __enter__(self) -> "EvidenceVault":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _local_root(root: str | Path) -> Path:
    text = str(root or "").strip()
    if not text:
        raise EvidenceError("El store necesita un directorio raiz")
    if text.startswith("//") or text.startswith("\\\\"):
        raise EvidenceError("SQLite compartido por red (UNC) no es una solucion valida")
    if "://" in text:
        raise EvidenceError("El store es local: no se aceptan URIs de conexion")
    return Path(text).expanduser()


# ----------------------------------------------------------------------- fachada


def vault_from_settings(settings: object) -> EvidenceVault:
    """Construye el vault con el directorio y los plazos de la configuracion."""
    root = getattr(settings, "RESEARCH_EVIDENCE_DIR", None)
    if not root:
        raise EvidenceError("RESEARCH_EVIDENCE_DIR no esta configurado")
    return EvidenceVault(root, retention=RetentionWindows.from_settings(settings))


def scope_from_principal(
    principal: Principal,
    project_key: str,
    *,
    max_classification: Classification = Classification.PUBLIC,
) -> EvidenceScope:
    """Deriva el scope del principal autenticado. El payload no autoriza.

    Se comprueba el permiso de lectura y la pertenencia al proyecto; el resultado
    fija owner y techo de clasificacion para que la consulta los aplique antes de
    buscar.
    """
    if not isinstance(principal, Principal):
        raise EvidenceScopeError("Se requiere un Principal")
    if not principal.has_permission(RESEARCH_READ):
        raise ResearchAccessDenied("El principal no tiene permiso read_research")
    if not (
        principal.is_admin
        or "*" in principal.allowed_projects
        or project_key in principal.allowed_projects
    ):
        raise ResearchAccessDenied(
            f"El principal no esta autorizado en el proyecto {project_key}"
        )
    return EvidenceScope(
        project_key=project_key,
        owner_id=principal.user_id,
        max_classification=max_classification,
    )
