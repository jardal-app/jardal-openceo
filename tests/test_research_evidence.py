"""Almacen de evidencias por proyecto (SQLite/FTS5): scope, retencion y honestidad.

Las pruebas usan **SQLite real en un directorio temporal** (no un fake en memoria):
lo que se comprueba es que FTS5 existe de verdad, que la migracion es idempotente,
que dos escritores serializan sin corromper, que el filtro de proyecto/owner/
clasificacion ocurre antes de la consulta lexica, que el traversal y los symlinks
se rechazan, y que la expiracion borra indice y blob dejando un informe que dice la
verdad en lugar de una lista vacia.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from research.contracts import (  # noqa: E402
    AccessMethod,
    Classification,
    EvidenceSupport,
    ResearchEvidence,
    RetentionPolicy,
    SourceType,
)
from research.evidence import (  # noqa: E402
    SCHEMA_VERSION,
    EvidenceConflict,
    EvidenceError,
    EvidenceHashMismatch,
    EvidenceLimits,
    EvidencePathError,
    EvidencePolicyError,
    EvidenceScope,
    EvidenceScopeError,
    EvidenceState,
    EvidenceStoreClosed,
    EvidenceTooLarge,
    EvidenceVault,
    RetentionWindows,
    SQLiteEvidenceRepository,
    content_hash_of,
    fts5_available,
    normalize_content,
    project_dir_name,
    safe_join,
    scope_from_principal,
)
from research.policies import (  # noqa: E402
    RESEARCH_READ,
    RESEARCH_RUN,
    Principal,
    ResearchAccessDenied,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _evidence(
    evidence_id: str,
    *,
    project_key: str = "proyecto-a",
    owner_id: str = "usr_a",
    visibility: Classification = Classification.PUBLIC,
    retention: RetentionPolicy = RetentionPolicy.SOURCE_SPECIFIC,
    url: str = "https://example.org/articulo",
    title: str = "Articulo",
    published_at: datetime | None = None,
    event_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> ResearchEvidence:
    return ResearchEvidence(
        id=evidence_id,
        url=url,
        access_method=AccessMethod.HTTP,
        visibility=visibility,
        source_type=SourceType.PRIMARY,
        title=title,
        published_at=published_at,
        event_at=event_at,
        observed_at=observed_at,
        retention_policy=retention,
        project_key=project_key,
        owner_id=owner_id,
    )


def _scope(
    project_key: str = "proyecto-a",
    owner_id: str = "usr_a",
    ceiling: Classification = Classification.PUBLIC,
) -> EvidenceScope:
    return EvidenceScope(
        project_key=project_key, owner_id=owner_id, max_classification=ceiling
    )


def _raw_scalar(path: Path, sql: str, params: tuple = ()) -> object:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(sql, params).fetchone()[0]
    finally:
        connection.close()


class _StoreTestCase(unittest.TestCase):
    """Base con un SQLite temporal real por prueba."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "evidence"
        self._repos: list[SQLiteEvidenceRepository] = []

    def tearDown(self) -> None:
        for repo in self._repos:
            try:
                repo.close()
            except Exception:  # pragma: no cover - limpieza defensiva
                pass

    def open(self, project_key: str = "proyecto-a", **kwargs) -> SQLiteEvidenceRepository:
        repo = SQLiteEvidenceRepository(root=self.root, project_key=project_key, **kwargs)
        self._repos.append(repo)
        return repo


class Fts5Tests(_StoreTestCase):
    def test_fts5_is_really_available_with_a_match(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            self.assertTrue(fts5_available(connection))
        finally:
            connection.close()

    def test_full_text_search_finds_the_blob_body_not_only_the_title(self) -> None:
        repo = self.open()
        scope = _scope()
        repo.save(
            _evidence("ev_1", title="Documento"),
            content="El contrato de arrendamiento incluye una clausula de penalizacion.",
        )
        repo.save(_evidence("ev_2", title="Otro"), content="Nota sobre meteorologia costera.")

        hits = repo.search(scope, "penalizacion")
        self.assertEqual([hit.evidence.id for hit in hits], ["ev_1"])
        self.assertLessEqual(hits[0].rank, 0.0)  # bm25 real, no un orden inventado

    def test_search_is_case_insensitive_and_tokenized(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_1"), content="Informe de AUDITORIA interna.")
        self.assertEqual(
            [hit.evidence.id for hit in repo.search(_scope(), "auditoria")], ["ev_1"]
        )
        # Una consulta sin terminos utiles no revienta ni devuelve todo.
        self.assertEqual(repo.search(_scope(), "  "), [])

    def test_search_handles_accents_and_fts_operators_as_plain_text(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_acento"), content="Informe de auditoria tributaria anual.")
        # `remove_diacritics` ya indexa sin acento; la consulta con acento tambien debe
        # funcionar y no partirse en la vocal acentuada.
        self.assertEqual(
            [hit.evidence.id for hit in repo.search(_scope(), "auditoría")], ["ev_acento"]
        )
        # Los operadores FTS se citan: se buscan como texto, no como sintaxis.
        repo.save(_evidence("ev_operador"), content="Operador NEAR y comilla simple.")
        self.assertIsInstance(repo.search(_scope(), 'NEAR OR "'), list)
        self.assertEqual(
            [hit.evidence.id for hit in repo.search(_scope(), "NEAR")], ["ev_operador"]
        )

    def test_an_expired_row_disappears_from_the_index(self) -> None:
        repo = self.open()
        scope = _scope()
        repo.save(
            _evidence("ev_efimera", retention=RetentionPolicy.PUBLIC_7D),
            content="termino efimero caducable",
            now=T0,
        )
        self.assertEqual(len(repo.search(scope, "caducable", now=T0)), 1)
        repo.expire_due(scope, now=T0 + timedelta(days=8))
        self.assertEqual(repo.search(scope, "caducable", now=T0 + timedelta(days=8)), [])
        self.assertEqual(
            int(_raw_scalar(repo.database_path, "SELECT count(*) FROM evidence_fts")), 0
        )


class MigrationTests(_StoreTestCase):
    def test_migration_is_idempotent_and_data_survives_reopen(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_1"), content="contenido persistente")
        database = repo.database_path
        repo.close()

        reopened = self.open()
        self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
        self.assertEqual(
            int(_raw_scalar(database, "PRAGMA user_version")), SCHEMA_VERSION
        )
        stored = reopened.get(_scope(), "ev_1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.content, "contenido persistente")

    def test_a_store_from_the_future_is_not_silently_downgraded(self) -> None:
        repo = self.open()
        connection = sqlite3.connect(str(repo.database_path))
        try:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
            connection.commit()
        finally:
            connection.close()
        repo.close()
        with self.assertRaises(EvidenceError) as ctx:
            self.open()
        self.assertIn("version", str(ctx.exception).lower())


class ConcurrencyAndCloseTests(_StoreTestCase):
    def test_wal_mode_is_enabled(self) -> None:
        repo = self.open()
        connection = sqlite3.connect(str(repo.database_path))
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(str(mode).lower(), "wal")

    def test_two_writers_serialize_without_corruption(self) -> None:
        limits = EvidenceLimits(max_search_results=200)
        first = self.open(limits=limits)
        second = self.open(limits=limits)
        scope = _scope()
        errors: list[BaseException] = []

        def worker(repo: SQLiteEvidenceRepository, prefix: str) -> None:
            try:
                for index in range(25):
                    repo.save(
                        _evidence(f"{prefix}_{index:02d}"),
                        content=f"documento {prefix} numero {index} con termino compartido",
                    )
            except BaseException as exc:  # pragma: no cover - se reporta abajo
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(first, "a")),
            threading.Thread(target=worker, args=(second, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(first.count(scope), 50)
        connection = sqlite3.connect(str(first.database_path))
        try:
            self.assertEqual(
                str(connection.execute("PRAGMA integrity_check").fetchone()[0]), "ok"
            )
            indexed = int(connection.execute("SELECT count(*) FROM evidence_fts").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(indexed, 50)
        hits = first.search(scope, "compartido", limit=200)
        self.assertEqual(len(hits), 50)

    def test_close_is_clean_and_blocks_further_operations(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_1"), content="algo")
        repo.close()
        with self.assertRaises(EvidenceStoreClosed):
            repo.search(_scope(), "algo")
        repo.close()  # cerrar dos veces no revienta


class IsolationTests(_StoreTestCase):
    def test_scope_filter_runs_before_the_full_text_query(self) -> None:
        repo = self.open()
        public_scope = _scope()
        repo.save(
            _evidence("ev_publica", visibility=Classification.PUBLIC),
            content="informe confidencial de auditoria",
        )
        repo.save(
            _evidence("ev_restringida", visibility=Classification.RESTRICTED),
            content="informe confidencial de auditoria",
        )

        candidates = set(repo.candidate_ids(public_scope))
        self.assertIn("ev_publica", candidates)
        self.assertNotIn("ev_restringida", candidates)  # ni siquiera candidata

        # Ambas filas estan indexadas en FTS: lo que excluye a la restringida es el
        # filtro de scope aplicado antes de la consulta, no una falta de indice.
        self.assertEqual(
            int(
                _raw_scalar(
                    repo.database_path,
                    "SELECT count(*) FROM evidence_fts WHERE evidence_fts MATCH ?",
                    ("confidencial",),
                )
            ),
            2,
        )

        hits = repo.search(public_scope, "confidencial")
        self.assertEqual([hit.evidence.id for hit in hits], ["ev_publica"])

        # Con techo suficiente, la misma fila si aparece: el filtro es de scope,
        # no una censura de terminos.
        full_scope = _scope(ceiling=Classification.RESTRICTED)
        self.assertEqual(
            {hit.evidence.id for hit in repo.search(full_scope, "confidencial")},
            {"ev_publica", "ev_restringida"},
        )

    def test_another_owner_inside_the_same_project_is_never_returned(self) -> None:
        repo = self.open()
        owner_a = _scope(owner_id="usr_a")
        repo.save(_evidence("ev_a", owner_id="usr_a"), content="secreto compartido de equipo")
        repo.save(_evidence("ev_b", owner_id="usr_b"), content="secreto compartido de equipo")

        candidates = set(repo.candidate_ids(owner_a))
        self.assertIn("ev_a", candidates)
        self.assertNotIn("ev_b", candidates)
        self.assertEqual([hit.evidence.id for hit in repo.search(owner_a, "secreto")], ["ev_a"])
        self.assertIsNone(repo.get(owner_a, "ev_b"))
        self.assertEqual(owner_a.owner_id, "usr_a")

    def test_a_project_store_cannot_see_or_write_another_project(self) -> None:
        repo_a = self.open("proyecto-a")
        repo_b = self.open("proyecto-b")
        repo_a.save(_evidence("ev_a", project_key="proyecto-a"), content="termino comun alfa")
        repo_b.save(_evidence("ev_b", project_key="proyecto-b"), content="termino comun alfa")

        self.assertEqual(
            [hit.evidence.id for hit in repo_a.search(_scope("proyecto-a"), "comun")],
            ["ev_a"],
        )
        # Los archivos son distintos: no hay forma de ver el otro proyecto.
        self.assertNotEqual(repo_a.database_path, repo_b.database_path)
        with self.assertRaises(EvidenceScopeError):
            repo_a.search(_scope("proyecto-b"), "comun")
        with self.assertRaises(EvidenceScopeError):
            repo_a.save(_evidence("ev_x", project_key="proyecto-b"), content="intruso")

    def test_scope_owner_is_required(self) -> None:
        with self.assertRaises(EvidenceScopeError):
            EvidenceScope(project_key="proyecto-a", owner_id="")
        repo = self.open()
        with self.assertRaises(EvidenceScopeError):
            repo.save(_evidence("ev_sin_owner", owner_id=""), content="x")

    def test_an_id_cannot_change_owner_inside_a_project(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_id", owner_id="usr_a"), content="contenido de A")
        with self.assertRaises(EvidenceConflict):
            repo.save(_evidence("ev_id", owner_id="usr_b"), content="contenido de A")
        self.assertIsNone(repo.get(_scope(owner_id="usr_b"), "ev_id"))

    def test_scope_comes_from_the_authenticated_principal(self) -> None:
        principal = Principal(
            user_id="usr_a",
            permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
            allowed_projects=frozenset({"proyecto-a"}),
        )
        scope = scope_from_principal(principal, "proyecto-a")
        self.assertEqual(scope.owner_id, "usr_a")
        with self.assertRaises(ResearchAccessDenied):
            scope_from_principal(principal, "proyecto-b")
        without_read = Principal(
            user_id="usr_a",
            permissions=frozenset({RESEARCH_RUN}),
            allowed_projects=frozenset({"proyecto-a"}),
        )
        with self.assertRaises(ResearchAccessDenied):
            scope_from_principal(without_read, "proyecto-a")


class PathSecurityTests(_StoreTestCase):
    def test_traversal_in_an_evidence_id_is_rejected(self) -> None:
        repo = self.open()
        for bad in ("../evil", "a/../../evil", "..", ".oculto", "a\\b", "/etc/passwd"):
            with self.assertRaises(EvidencePathError, msg=bad):
                repo.get(_scope(), bad)
        with self.assertRaises(EvidencePathError):
            repo.save(_evidence("../../evil"), content="x")

    def test_safe_join_refuses_to_escape_the_base(self) -> None:
        repo = self.open()
        with self.assertRaises(EvidencePathError):
            safe_join(repo.blobs_dir, "../fuera.blob")
        with self.assertRaises(EvidencePathError):
            safe_join(repo.blobs_dir, "/etc/passwd")

    def test_a_symlinked_blob_is_rejected_instead_of_followed(self) -> None:
        repo = self.open()
        stored = repo.save(_evidence("ev_enlace"), content="contenido legitimo")
        outside = Path(self._tmp.name) / "secreto.txt"
        outside.write_text("secreto ajeno", encoding="utf-8")

        blob = repo.blob_path(stored.blob_relpath)
        self.assertTrue(blob.exists())
        os.unlink(blob)
        os.symlink(outside, blob)

        with self.assertRaises(EvidencePathError):
            repo.get(_scope(), "ev_enlace")
        # El archivo apuntado sigue intacto: no se siguio el enlace.
        self.assertEqual(outside.read_text(encoding="utf-8"), "secreto ajeno")

    def test_a_symlinked_project_directory_is_rejected_at_open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        real = Path(self._tmp.name) / "otro"
        real.mkdir()
        link = self.root / project_dir_name("proyecto-a")
        os.symlink(real, link)
        with self.assertRaises(EvidencePathError):
            SQLiteEvidenceRepository(root=self.root, project_key="proyecto-a")

    def test_project_directory_name_never_contains_user_text(self) -> None:
        name = project_dir_name("../../etc/passwd")
        self.assertEqual(len(name), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in name))


class RetentionAndExpiryTests(_StoreTestCase):
    def test_expiry_removes_fts_and_blob_and_reports_honestly(self) -> None:
        repo = self.open()
        scope = _scope()
        stored = repo.save(
            _evidence("ev_caduca", retention=RetentionPolicy.PUBLIC_7D),
            content="termino efimero caducable",
            support=EvidenceSupport(location="seccion 2"),
            now=T0,
        )
        self.assertEqual(stored.expires_at, T0 + timedelta(days=7))
        blob = repo.blob_path(stored.blob_relpath)
        self.assertTrue(blob.exists())

        report = repo.expire_due(scope, now=T0 + timedelta(days=8))
        self.assertIn("ev_caduca", report.expired_ids)
        self.assertFalse(blob.exists())
        self.assertEqual(repo.candidate_ids(scope), [])
        self.assertEqual(repo.search(scope, "caducable"), [])
        self.assertEqual(repo.count(scope), 0)

        # Honestidad: no hay silencio. La evidencia se declara expirada y no
        # recuperable, distinta de "nunca existio".
        self.assertEqual(repo.evidence_state(scope, "ev_caduca"), EvidenceState.EXPIRED)
        notes = repo.missing_evidence_notes(scope, ["ev_caduca"])
        self.assertEqual(len(notes), 1)
        self.assertIn("ev_caduca", notes[0])
        self.assertIn("expirada", notes[0].lower())
        self.assertIn("no es recuperable", notes[0].lower())

        # El tombstone no conserva el contenido.
        connection = sqlite3.connect(str(repo.database_path))
        try:
            self.assertEqual(
                int(connection.execute("SELECT count(*) FROM evidence").fetchone()[0]), 0
            )
            self.assertEqual(
                int(connection.execute("SELECT count(*) FROM evidence_fts").fetchone()[0]), 0
            )
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT count(*) FROM evidence_tombstones WHERE evidence_id=?",
                        ("ev_caduca",),
                    ).fetchone()[0]
                ),
                1,
            )
        finally:
            connection.close()

    def test_an_unknown_id_is_reported_as_unknown_not_as_expired(self) -> None:
        repo = self.open()
        self.assertEqual(
            repo.evidence_state(_scope(), "ev_inexistente"), EvidenceState.UNKNOWN
        )
        notes = repo.missing_evidence_notes(_scope(), ["ev_inexistente"])
        self.assertEqual(len(notes), 1)
        self.assertIn("scope autorizado", notes[0])

    def test_a_read_operation_triggers_expiry_without_background_sweeps(self) -> None:
        repo = self.open()
        scope = _scope()
        stored = repo.save(
            _evidence("ev_vencida", retention=RetentionPolicy.SOURCE_SPECIFIC),
            content="contenido que vence de inmediato",
            retention_days=0,
            now=T0,
        )
        blob = repo.blob_path(stored.blob_relpath)
        self.assertTrue(blob.exists())

        # La propia lectura limpia lo vencido de su scope: no hay barrido de fondo.
        self.assertEqual(
            repo.search(scope, "vence", now=T0 + timedelta(seconds=1)), []
        )
        self.assertFalse(blob.exists())
        self.assertEqual(repo.evidence_state(scope, "ev_vencida"), EvidenceState.EXPIRED)

    def test_source_specific_without_override_never_expires_by_itself(self) -> None:
        repo = self.open()
        stored = repo.save(_evidence("ev_owner"), content="politica del owner", now=T0)
        self.assertIsNone(stored.expires_at)
        repo.expire_due(_scope(), now=T0 + timedelta(days=3650))
        self.assertIsNotNone(repo.get(_scope(), "ev_owner"))

    def test_a_later_override_cannot_extend_the_policy_window(self) -> None:
        repo = self.open()
        stored = repo.save(
            _evidence("ev_publica", retention=RetentionPolicy.PUBLIC_7D),
            content="contenido publico",
            retention_days=30,
            now=T0,
        )
        self.assertEqual(stored.expires_at, T0 + timedelta(days=7))

    def test_watch_and_report_windows_follow_the_configured_days(self) -> None:
        repo = self.open(
            retention=RetentionWindows(public_days=7, watch_days=90, report_days=180)
        )
        watch = repo.save(
            _evidence("ev_watch", retention=RetentionPolicy.WATCH_90D),
            content="vigilancia",
            now=T0,
        )
        report = repo.save(
            _evidence("ev_report", retention=RetentionPolicy.REPORT_180D),
            content="informe",
            now=T0,
        )
        self.assertEqual(watch.expires_at, T0 + timedelta(days=90))
        self.assertEqual(report.expires_at, T0 + timedelta(days=180))

    def test_vault_from_settings_uses_the_frozen_settings_names(self) -> None:
        from research.evidence import vault_from_settings
        from settings import Settings

        settings = Settings(
            _env_file=None,
            RESEARCH_EVIDENCE_DIR=str(self.root),
            RESEARCH_PUBLIC_RETENTION_DAYS=3,
            RESEARCH_WATCH_RETENTION_DAYS=30,
            RESEARCH_REPORT_RETENTION_DAYS=60,
        )
        vault = vault_from_settings(settings)
        self.addCleanup(vault.close)
        repo = vault.project("proyecto-a")
        stored = repo.save(
            _evidence("ev_config", retention=RetentionPolicy.PUBLIC_7D),
            content="contenido publico",
            now=T0,
        )
        self.assertEqual(stored.expires_at, T0 + timedelta(days=3))

    def test_an_expiring_signed_url_does_not_delete_or_schedule_deletion(self) -> None:
        repo = self.open()
        signed = "https://example.org/doc.pdf?X-Amz-Expires=60&X-Amz-Signature=abc"
        stored = repo.save(
            _evidence("ev_firmada", url=signed, retention=RetentionPolicy.SOURCE_SPECIFIC),
            content="documento con enlace firmado",
            now=T0,
        )
        # La firma caduca; el objeto no. El store no deduce expiracion de la URL.
        self.assertIsNone(stored.expires_at)
        repo.expire_due(_scope(), now=T0 + timedelta(days=4000))
        self.assertIsNotNone(repo.get(_scope(), "ev_firmada"))


class ProvenanceTests(_StoreTestCase):
    def test_a_brave_serp_is_not_stored(self) -> None:
        repo = self.open()
        with self.assertRaises(EvidencePolicyError):
            repo.save(
                _evidence(
                    "ev_brave",
                    url="https://api.search.brave.com/res/v1/web/search?q=contrato",
                ),
                content='{"web": {"results": []}}',
            )
        with self.assertRaises(EvidencePolicyError):
            repo.save(_evidence("ev_brave_2"), content="serp", provider="Brave")
        self.assertEqual(repo.candidate_ids(_scope()), [])

    def test_owner_classification_and_provenance_are_persisted(self) -> None:
        repo = self.open()
        repo.save(
            _evidence("ev_1", visibility=Classification.RESTRICTED),
            content="contenido restringido",
            provider="direct-http",
            support=EvidenceSupport(extract="extracto de soporte"),
        )
        stored = repo.get(_scope(ceiling=Classification.RESTRICTED), "ev_1")
        self.assertEqual(stored.evidence.project_key, "proyecto-a")
        self.assertEqual(stored.evidence.owner_id, "usr_a")
        self.assertEqual(stored.evidence.visibility, Classification.RESTRICTED)
        self.assertEqual(stored.evidence.access_method, AccessMethod.HTTP)
        self.assertEqual(stored.evidence.source_type, SourceType.PRIMARY)
        self.assertEqual(stored.provider, "direct-http")
        self.assertEqual(stored.support.extract, "extracto de soporte")


class HashAndDateTests(_StoreTestCase):
    def test_unknown_dates_stay_none_and_are_null_on_disk(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_sin_fechas"), content="sin fechas conocidas")
        stored = repo.get(_scope(), "ev_sin_fechas")
        self.assertIsNone(stored.evidence.published_at)
        self.assertIsNone(stored.evidence.event_at)
        self.assertIsNone(stored.evidence.observed_at)

        connection = sqlite3.connect(str(repo.database_path))
        try:
            row = connection.execute(
                "SELECT published_at, event_at, observed_at FROM evidence WHERE evidence_id=?",
                ("ev_sin_fechas",),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (None, None, None))
        self.assertNotEqual(row[2], 0)
        self.assertNotEqual(row[2], "1970-01-01T00:00:00+00:00")

    def test_a_known_date_round_trips_with_timezone(self) -> None:
        repo = self.open()
        observed = datetime(2026, 3, 4, 8, 30, tzinfo=timezone.utc)
        repo.save(_evidence("ev_fecha", observed_at=observed), content="con fecha")
        stored = repo.get(_scope(), "ev_fecha")
        self.assertEqual(stored.evidence.observed_at, observed)

    def test_normalization_removes_noise_but_keeps_material_content(self) -> None:
        noisy = "El   contrato\tde arrendamiento\r\n\r\n\r\n  exige penalizacion de 500 USD. \n"
        clean = "El contrato de arrendamiento\n\nexige penalizacion de 500 USD."
        self.assertEqual(normalize_content(noisy), clean)
        self.assertEqual(
            content_hash_of(normalize_content(noisy)),
            content_hash_of(normalize_content(clean)),
        )

        # El contenido material altera el hash: no se normaliza hasta borrarlo.
        extended = clean + " Ademas, incluye arbitraje."
        self.assertNotEqual(
            content_hash_of(normalize_content(clean)),
            content_hash_of(normalize_content(extended)),
        )
        self.assertNotEqual(
            content_hash_of(normalize_content("Contrato")),
            content_hash_of(normalize_content("contrato")),
        )

    def test_the_stored_hash_matches_the_normalized_content(self) -> None:
        repo = self.open()
        content = "Contenido   con   ruido\n\n\nnormalizable."
        stored = repo.save(_evidence("ev_hash"), content=content)
        expected = hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()
        self.assertEqual(stored.content_hash, expected)
        self.assertEqual(stored.content_hash, content_hash_of(normalize_content(content)))

    def test_a_declared_hash_that_does_not_describe_the_content_is_rejected(self) -> None:
        repo = self.open()
        lying = _evidence("ev_mentiroso").model_copy(update={"content_hash": "deadbeef"})
        with self.assertRaises(EvidenceHashMismatch):
            repo.save(lying, content="contenido real")
        self.assertEqual(repo.candidate_ids(_scope()), [])

    def test_the_same_id_with_other_content_is_a_conflict_not_a_silent_overwrite(self) -> None:
        repo = self.open()
        repo.save(_evidence("ev_doble"), content="primera version")
        with self.assertRaises(EvidenceConflict):
            repo.save(_evidence("ev_doble"), content="segunda version distinta")
        # Idempotencia real: mismo id y mismo contenido devuelve lo existente.
        again = repo.save(_evidence("ev_doble"), content="primera   version")
        self.assertEqual(again.evidence.id, "ev_doble")

    def test_a_conflicting_save_does_not_destroy_the_existing_blob(self) -> None:
        repo = self.open()
        stored = repo.save(_evidence("ev_conflicto"), content="primera version")
        original = repo.blob_path(stored.blob_relpath)
        with self.assertRaises(EvidenceConflict):
            repo.save(_evidence("ev_conflicto"), content="segunda version distinta")
        self.assertTrue(original.exists())
        self.assertEqual(repo.get(_scope(), "ev_conflicto").content, "primera version")

    def test_an_idempotent_save_reuses_one_blob(self) -> None:
        repo = self.open()
        first = repo.save(_evidence("ev_idem"), content="igual   contenido")
        second = repo.save(_evidence("ev_idem"), content="igual contenido")
        self.assertEqual(first.blob_relpath, second.blob_relpath)
        self.assertEqual(len(list(repo.blobs_dir.rglob("*.blob"))), 1)


class LimitsTests(_StoreTestCase):
    def test_the_blob_limit_is_enforced(self) -> None:
        repo = self.open(limits=EvidenceLimits(max_blob_bytes=16))
        with self.assertRaises(EvidenceTooLarge):
            repo.save(_evidence("ev_grande"), content="x" * 64)

    def test_the_vault_opens_one_store_per_project(self) -> None:
        vault = EvidenceVault(self.root)
        self.addCleanup(vault.close)
        project_a = vault.project("proyecto-a")
        project_b = vault.project("proyecto-b")
        self.assertIs(project_a, vault.project("proyecto-a"))
        self.assertNotEqual(project_a.database_path, project_b.database_path)
        self.assertEqual(project_a.project_key, "proyecto-a")

    def test_a_network_root_is_refused(self) -> None:
        with self.assertRaises(EvidenceError):
            EvidenceVault("//servidor/compartido/evidence")
        with self.assertRaises(EvidenceError):
            EvidenceVault("file:///tmp/evidence.sqlite3")


class AsyncIoTests(unittest.IsolatedAsyncioTestCase):
    """El I/O bloqueante sale del event loop con `asyncio.to_thread`."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "evidence"
        self.repo = SQLiteEvidenceRepository(root=self.root, project_key="proyecto-a")
        self.addCleanup(self.repo.close)
        self.scope = _scope()

    async def test_async_save_search_and_get_work(self) -> None:
        stored = await self.repo.asave(_evidence("ev_async"), content="contenido asincrono unico")
        self.assertEqual(stored.evidence.id, "ev_async")
        hits = await self.repo.asearch(self.scope, "asincrono")
        self.assertEqual([hit.evidence.id for hit in hits], ["ev_async"])
        loaded = await self.repo.aget(self.scope, "ev_async")
        self.assertEqual(loaded.content, "contenido asincrono unico")
        self.assertIsNone(await self.repo.aget(self.scope, "ev_ausente"))
        self.assertIn("ev_async", await self.repo.acandidate_ids(self.scope))

    async def test_async_wrappers_do_not_run_on_the_event_loop_thread(self) -> None:
        main_thread = threading.get_ident()
        seen: list[int] = []
        original = self.repo.save

        def spy(*args, **kwargs):
            seen.append(threading.get_ident())
            return original(*args, **kwargs)

        self.repo.save = spy  # type: ignore[method-assign]
        await self.repo.asave(_evidence("ev_hilo"), content="contenido en worker")
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], main_thread)


if __name__ == "__main__":
    unittest.main()
