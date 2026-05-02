from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.tmux_runtime import TmuxRuntime
from dev_session_service import DevSessionService
import main


class DevSessionMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = DevSessionService.__new__(DevSessionService)

    def test_wrapper_avoids_literal_placeholder_block(self) -> None:
        wrapped, begin, end = self.service._build_marker_wrapped_instruction("Analiza la integracion.")

        self.assertIn(f"Marcador inicial exacto: {begin}", wrapped)
        self.assertIn(f"Marcador final exacto: {end}", wrapped)
        self.assertNotIn("<frase corta en espanol natural para voz", wrapped.lower())
        self.assertNotIn(f"\n{begin}\n", wrapped)
        self.assertNotIn(f"\n{end}\n", wrapped)
        self.assertIn("Entre ambos escribe solo la respuesta final.", wrapped)
        self.assertIn("URLs, IDs, rutas y nombres de archivos", wrapped)
        self.assertNotIn("pensada para voz", wrapped)

    def test_opencode_wrapper_discourages_raw_links_for_voice(self) -> None:
        wrapped = self.service._build_opencode_run_instruction("Resume la integracion.")

        self.assertIn("Responde solo con la respuesta final.", wrapped)
        self.assertIn("URLs, IDs, rutas y nombres de archivos", wrapped)
        self.assertNotIn("pensada para voz", wrapped)

    def test_completion_block_ignores_placeholder_body(self) -> None:
        begin = "OPENCEO_FINAL_BEGIN::abc123"
        end = "OPENCEO_FINAL_END::abc123"
        placeholder = (
            f"{begin}\n"
            "<frase corta en espanol natural para voz; sin rutas, archivos, IDs ni nombres tecnicos>\n"
            f"{end}\n"
        )

        self.assertFalse(TmuxRuntime._has_completion_block(placeholder, begin, end))

        completed = placeholder + f"\n{begin}\nRespuesta final util.\n{end}\n"
        self.assertTrue(TmuxRuntime._has_completion_block(completed, begin, end))

    def test_extract_marked_summary_prefers_real_response_over_placeholder(self) -> None:
        begin = "OPENCEO_FINAL_BEGIN::abc123"
        end = "OPENCEO_FINAL_END::abc123"
        content = (
            f"{begin}\n"
            "<frase corta en espanol natural para voz; sin rutas, archivos, IDs ni nombres tecnicos>\n"
            f"{end}\n"
            "trabajo intermedio\n"
            f"{begin}\n"
            "Usa una accion compartida para crear tickets y un flujo aparte para escuchar cambios.\n"
            f"{end}\n"
        )

        summary = self.service._extract_marked_summary(content, begin, end)
        self.assertEqual(
            summary,
            "Usa una accion compartida para crear tickets y un flujo aparte para escuchar cambios.",
        )

    def test_extract_marked_summary_normalizes_wrapped_text_and_urls(self) -> None:
        begin = "OPENCEO_FINAL_BEGIN::abc123"
        end = "OPENCEO_FINAL_END::abc123"
        content = (
            f"{begin}\n"
            "Validé compilación.\n"
            "No hice lla\n"
            "mada real a Notion. Más detalle: https://developers.notion.com/reference/post-page\n"
            f"{end}\n"
        )

        summary = self.service._extract_marked_summary(content, begin, end)

        self.assertEqual(summary, "Validé compilación. No hice llamada real a Notion. Más detalle")

    def test_extract_marked_summary_preserves_numbered_lists(self) -> None:
        begin = "OPENCEO_FINAL_BEGIN::abc123"
        end = "OPENCEO_FINAL_END::abc123"
        content = (
            f"{begin}\n"
            "Resumen rápido.\n"
            "\n"
            "1. Primer punto\n"
            "2. Segundo punto\n"
            f"{end}\n"
        )

        summary = self.service._extract_marked_summary(content, begin, end)

        self.assertEqual(summary, "Resumen rápido.\n\n1. Primer punto\n2. Segundo punto")


class DevSessionIntentPatternTests(unittest.TestCase):
    def test_resume_language_now_maps_to_session_focus(self) -> None:
        self.assertFalse(main._wants_restore_last_conversation("reanuda la sesion 2"))
        self.assertTrue(main._wants_session_focus("reanuda la sesion 2"))
        self.assertFalse(main._wants_restore_last_conversation("/resume"))

    def test_natural_status_and_output_phrases_are_trimmed(self) -> None:
        self.assertTrue(main._wants_dev_session_progress("ya termino"))
        self.assertTrue(main._wants_dev_session_progress("como va"))
        self.assertFalse(main._wants_dev_session_progress("status"))
        self.assertFalse(main._wants_dev_session_progress("estado"))

        self.assertTrue(main._wants_dev_session_output("que respondio"))
        self.assertTrue(main._wants_dev_session_output("que dijo"))
        self.assertTrue(main._wants_dev_session_output("que salio"))
        self.assertFalse(main._wants_dev_session_output("respuesta"))
        self.assertFalse(main._wants_dev_session_output("resultado"))
        self.assertFalse(main._wants_dev_session_output("salida"))

    def test_project_selection_does_not_trigger_from_generic_usa_and_primero(self) -> None:
        projects = ["cebitec", "jardal-openceo", "jardal-trading", "jardal-vpn"]
        prompt = (
            "usa este token de notion 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 "
            "y limpia todo lo por defecto que viene porque es un espacio de trabajo sucio. "
            "analiza la mejor manera de organizar la informacion para este proyecto con los estados "
            "definidos en el readme.md primero prepara el terreno de notion"
        )

        self.assertFalse(main._wants_project_selection(prompt))
        self.assertIsNone(main._extract_project_from_text(prompt, projects))

    def test_project_selection_keeps_working_with_explicit_project_context(self) -> None:
        projects = ["cebitec", "jardal-openceo", "jardal-trading", "jardal-vpn"]

        self.assertTrue(main._wants_project_selection("selecciona el proyecto 1"))
        self.assertEqual(main._extract_project_from_text("selecciona el proyecto 1", projects), "cebitec")
        self.assertEqual(main._extract_project_from_text("selecciona el primer proyecto", projects), "cebitec")


class DevSessionVoiceOutputTests(unittest.TestCase):
    def test_result_text_returns_full_reply_without_header_or_ellipsis(self) -> None:
        long_reply = (
            "Yo usaria un esquema hibrido para Notion con una herramienta compartida para escribir "
            "tickets y un flujo aparte para reaccionar a cambios y mandarlos al agente dev. "
            "Eso evita acoplar una integracion global a un solo agente y mantiene el disparo externo "
            "en una pieza visible y facil de reintentar."
        )
        session = {
            "project": "jardal-openceo",
            "provider": "codex",
            "status": "waiting_user",
            "messages": [{"role": "assistant", "content": long_reply}],
        }

        result = main._build_dev_session_result_text(session)

        self.assertEqual(result, long_reply)
        self.assertNotIn("Ultima respuesta de la sesion", result)
        self.assertNotIn("...", result)

    def test_result_text_keeps_running_message_short(self) -> None:
        session = {
            "project": "jardal-openceo",
            "provider": "codex",
            "status": "running",
        }

        result = main._build_dev_session_result_text(session)

        self.assertEqual(result, "la sesion codex en jardal-openceo sigue trabajando.")

    def test_result_text_strips_urls_and_repairs_wrapped_words(self) -> None:
        reply = (
            "Validé compilación.\n"
            "No hice lla\n"
            "mada real a Notion y\n"
            "otro ajuste en el c\n"
            "ore. Más detalle: https://developers.notion.com/reference/post-page\n"
        )
        session = {
            "project": "jardal-openceo",
            "provider": "codex",
            "status": "waiting_user",
            "messages": [{"role": "assistant", "content": reply}],
        }

        result = main._build_dev_session_result_text(session)

        self.assertEqual(result, "Validé compilación. No hice llamada real a Notion y otro ajuste en el core. Más detalle")
        self.assertNotIn("https://", result)

    def test_result_text_preserves_multiline_lists(self) -> None:
        reply = "Resumen rápido.\n\n1. Primer punto\n2. Segundo punto\n"
        session = {
            "project": "jardal-openceo",
            "provider": "codex",
            "status": "waiting_user",
            "messages": [{"role": "assistant", "content": reply}],
        }

        result = main._build_dev_session_result_text(session)

        self.assertEqual(result, "Resumen rápido.\n\n1. Primer punto\n2. Segundo punto")


class DevSessionAsyncTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_async_peer_turn_replaces_stale_task(self) -> None:
        service = DevSessionService.__new__(DevSessionService)
        service._peer_turn_tasks = {}

        async def stale_turn() -> None:
            await asyncio.sleep(60)

        started = asyncio.Event()

        async def fresh_turn(session_id: str, project: str, instruction: str) -> None:
            self.assertEqual(session_id, "session-1")
            self.assertEqual(project, "jardal-openceo")
            self.assertEqual(instruction, "hola")
            started.set()

        stale_task = asyncio.create_task(stale_turn())
        service._peer_turn_tasks["session-1"] = stale_task
        service._run_async_peer_turn = fresh_turn

        service._start_async_peer_turn("session-1", "jardal-openceo", "hola")

        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)

        replacement = service._peer_turn_tasks["session-1"]
        self.assertIsNot(replacement, stale_task)
        self.assertTrue(stale_task.cancelled() or stale_task.done())
        await replacement


if __name__ == "__main__":
    unittest.main()
