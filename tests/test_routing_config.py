from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from config_loader import ConfigLoader
import main
from router import CEORouter
from settings import Settings
from state import AgentName, ChatCompletionRequest, OpenAIMessage


class RouterConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo")
        self.router = CEORouter(self.settings)

    async def test_routes_from_project_config_cases(self) -> None:
        project_config = {
            "routing": {
                "llm_fallback": False,
                "default_agent": "dev",
                "agents": {
                    "pm": {
                        "triggers": ["entrevista"],
                        "patterns": [r"\bcrear\s+tickets?\b"],
                    },
                    "secretary": {
                        "triggers": ["correo"],
                        "patterns": [r"\benviar\s+correo\b"],
                    },
                    "finance": {
                        "triggers": ["factura"],
                        "patterns": [],
                    },
                },
            }
        }

        cases = [
            ("tengo una entrevista", AgentName.PM),
            ("puedes crear tickets para esto", AgentName.PM),
            ("necesito enviar correo al cliente", AgentName.SECRETARY),
            ("quiero pagar una factura", AgentName.FINANCE),
        ]

        for message, expected in cases:
            with self.subTest(message=message):
                selected = await self.router.select_agent(
                    title=message,
                    description=message,
                    project_config=project_config,
                )
                self.assertEqual(selected, expected)

    async def test_uses_default_agent_from_config_when_no_rule_matches(self) -> None:
        project_config = {
            "routing": {
                "llm_fallback": False,
                "default_agent": "secretary",
                "agents": {},
            }
        }

        selected = await self.router.select_agent(
            title="mensaje ambiguo",
            description="mensaje ambiguo",
            project_config=project_config,
        )

        self.assertEqual(selected, AgentName.SECRETARY)

    async def test_routes_requirement_like_payload_to_pm_without_keyword_trigger(self) -> None:
        project_config = {
            "routing": {
                "llm_fallback": False,
                "default_agent": "dev",
                "agents": {
                    "pm": {
                        "triggers": [],
                        "patterns": [],
                    }
                },
            }
        }

        message = "El cliente quiere login con Google, roles admin y dashboard de auditoría."

        selected = await self.router.select_agent(
            title=message,
            description=message,
            project_config=project_config,
        )

        self.assertEqual(selected, AgentName.PM)


class RoutingConfigLoaderTests(unittest.TestCase):
    def test_project_defaults_include_routing_schema(self) -> None:
        loader = ConfigLoader(Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo"))

        config = loader.load_project("jardal-openceo")

        self.assertIn("routing", config)
        self.assertEqual(config["routing"]["default_agent"], "dev")
        self.assertIn("pm", config["routing"]["agents"])
        self.assertIn("entrevista", config["routing"]["agents"]["pm"]["triggers"])


class PMStateRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_completions_keeps_pm_flow_when_pm_state_exists(self) -> None:
        class FakeRepo:
            async def get_pm_state(self, user_id: str):
                return {"step": "need_project", "interview": "tengo una entrevista"}

        expected = main.chat_response("ceo", "flujo pm").model_dump(mode="json")
        payload = ChatCompletionRequest(
            model="ceo",
            messages=[OpenAIMessage(role="user", content="1")],
            stream=False,
            user="pm-state-test",
        )

        with patch.object(main, "repo", FakeRepo()):
            with patch.object(main, "_handle_openceo_pm_direct", AsyncMock(return_value=expected)) as pm_handler:
                with patch.object(main.first_layer_gateway, "process_turn", AsyncMock(return_value=(False, ""))):
                    result = await main.chat_completions(payload)

        self.assertEqual(main._assistant_text_from_chat_result(result), "flujo pm")
        pm_handler.assert_awaited_once_with(payload, "pm-state-test", "1")

    async def test_chat_completions_routes_pm_background_queries_but_not_unrelated_messages(self) -> None:
        class FakeRepo:
            async def get_pm_state(self, user_id: str):
                return {
                    "step": "background_running",
                    "project": "melanyryr",
                    "tool": "codex",
                    "job": {"status": "running"},
                }

        pm_expected = main.chat_response("ceo", "estado pm").model_dump(mode="json")
        ceo_expected = main.chat_response("ceo", "routing normal").model_dump(mode="json")

        status_payload = ChatCompletionRequest(
            model="ceo",
            messages=[OpenAIMessage(role="user", content="ya termino de crear tickets de la entrevista?")],
            stream=False,
            user="pm-bg-status",
        )
        unrelated_payload = ChatCompletionRequest(
            model="ceo",
            messages=[OpenAIMessage(role="user", content="quiero pagar una factura")],
            stream=False,
            user="pm-bg-status",
        )

        with patch.object(main, "repo", FakeRepo()):
            with patch.object(main, "_handle_openceo_pm_direct", AsyncMock(return_value=pm_expected)) as pm_handler:
                with patch.object(main, "_handle_openceo_ceo_model", AsyncMock(return_value=ceo_expected)) as ceo_handler:
                    with patch.object(main.first_layer_gateway, "process_turn", AsyncMock(return_value=(False, ""))):
                        status_result = await main.chat_completions(status_payload)
                        unrelated_result = await main.chat_completions(unrelated_payload)

        self.assertEqual(main._assistant_text_from_chat_result(status_result), "estado pm")
        self.assertEqual(main._assistant_text_from_chat_result(unrelated_result), "routing normal")
        pm_handler.assert_awaited_once_with(status_payload, "pm-bg-status", "ya termino de crear tickets de la entrevista?")
        ceo_handler.assert_awaited_once_with(unrelated_payload, "pm-bg-status", "quiero pagar una factura")


class PMGuidedFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_trigger_does_not_store_placeholder_as_interview(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.state: dict[str, dict] = {}

            async def clear_pm_state(self, user_id: str) -> None:
                self.state.pop(user_id, None)

            async def get_pm_state(self, user_id: str):
                return self.state.get(user_id)

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                self.state[user_id] = dict(state)

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "cebitec"}, {"name": "jardal-openceo"}]

        repo = FakeRepo()
        payload = ChatCompletionRequest(
            model="pm",
            messages=[OpenAIMessage(role="user", content="tengo una entrevista")],
            stream=False,
            user="pm-generic-trigger",
        )

        with patch.object(main, "repo", repo):
            with patch.object(main, "project_service", FakeProjectService()):
                result = await main._handle_openceo_pm_direct(payload, "pm-generic-trigger", "tengo una entrevista")

        text = main._assistant_text_from_chat_result(result)
        self.assertIn("¿De qué proyecto es esta entrevista?", text)
        self.assertEqual(repo.state["pm-generic-trigger"]["step"], "need_project")
        self.assertNotIn("interview", repo.state["pm-generic-trigger"])

    async def test_pm_flow_collects_missing_fields_and_asks_interview_last_when_missing(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.state: dict[str, dict] = {}

            async def clear_pm_state(self, user_id: str) -> None:
                self.state.pop(user_id, None)

            async def get_pm_state(self, user_id: str):
                return self.state.get(user_id)

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                self.state[user_id] = dict(state)

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "cebitec"}, {"name": "jardal-openceo"}]

        repo = FakeRepo()

        with patch.object(main, "repo", repo):
            with patch.object(main, "project_service", FakeProjectService()):
                base_payload = ChatCompletionRequest(
                    model="pm",
                    messages=[OpenAIMessage(role="user", content="tengo una entrevista")],
                    stream=False,
                    user="pm-sequence",
                )

                first = await main._handle_openceo_pm_direct(base_payload, "pm-sequence", "tengo una entrevista")
                self.assertIn("¿De qué proyecto es esta entrevista?", main._assistant_text_from_chat_result(first))

                second = await main._handle_openceo_pm_direct(base_payload, "pm-sequence", "1")
                self.assertIn("¿Qué herramienta quieres usar para planificar?", main._assistant_text_from_chat_result(second))

                third = await main._handle_openceo_pm_direct(base_payload, "pm-sequence", "2")
                third_text = main._assistant_text_from_chat_result(third)
                self.assertIn("Ahora envíame solo la entrevista o el requerimiento", third_text)
                self.assertIn("Ya registré el proyecto cebitec y la herramienta Media (Codex CLI).", third_text)
                self.assertEqual(repo.state["pm-sequence"]["step"], "need_interview")
                self.assertEqual(repo.state["pm-sequence"]["project"], "cebitec")
                self.assertEqual(repo.state["pm-sequence"]["tool"], "codex")
                self.assertNotIn("interview", repo.state["pm-sequence"])

                fourth = await main._handle_openceo_pm_direct(
                    base_payload,
                    "pm-sequence",
                    "El cliente quiere login con Google, roles admin y auditoría.",
                )
                self.assertIn("Confirma antes de procesar", main._assistant_text_from_chat_result(fourth))
                self.assertEqual(repo.state["pm-sequence"]["step"], "confirm")
                self.assertIn("login con Google", repo.state["pm-sequence"]["interview"])

    async def test_pm_flow_keeps_interview_when_user_sends_it_before_project_and_tool(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.state: dict[str, dict] = {}

            async def clear_pm_state(self, user_id: str) -> None:
                self.state.pop(user_id, None)

            async def get_pm_state(self, user_id: str):
                return self.state.get(user_id)

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                self.state[user_id] = dict(state)

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "cebitec"}, {"name": "jardal-openceo"}]

        repo = FakeRepo()
        payload = ChatCompletionRequest(
            model="pm",
            messages=[
                OpenAIMessage(
                    role="user",
                    content="El cliente quiere login con Google, roles admin y dashboard de auditoría.",
                )
            ],
            stream=False,
            user="pm-interview-first",
        )

        with patch.object(main, "repo", repo):
            with patch.object(main, "project_service", FakeProjectService()):
                first = await main._handle_openceo_pm_direct(
                    payload,
                    "pm-interview-first",
                    "El cliente quiere login con Google, roles admin y dashboard de auditoría.",
                )
                self.assertIn("¿De qué proyecto es esta entrevista?", main._assistant_text_from_chat_result(first))
                self.assertEqual(
                    repo.state["pm-interview-first"]["interview"],
                    "El cliente quiere login con Google, roles admin y dashboard de auditoría.",
                )

                second = await main._handle_openceo_pm_direct(payload, "pm-interview-first", "1")
                self.assertIn("¿Qué herramienta quieres usar para planificar?", main._assistant_text_from_chat_result(second))

                third = await main._handle_openceo_pm_direct(payload, "pm-interview-first", "2")
                self.assertIn("Confirma antes de procesar", main._assistant_text_from_chat_result(third))
                self.assertEqual(repo.state["pm-interview-first"]["project"], "cebitec")
                self.assertEqual(repo.state["pm-interview-first"]["tool"], "codex")
                self.assertEqual(repo.state["pm-interview-first"]["step"], "confirm")

    async def test_pm_confirm_starts_background_job_and_returns_immediate_ack(self) -> None:
        class FakeRepo:
            async def clear_pm_state(self, user_id: str) -> None:
                return None

            async def get_pm_state(self, user_id: str):
                return {
                    "step": "confirm",
                    "project": "melanyryr",
                    "tool": "codex",
                    "interview": "El cliente quiere login con Google.",
                }

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                return None

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "melanyryr"}]

        payload = ChatCompletionRequest(
            model="pm",
            messages=[OpenAIMessage(role="user", content="1")],
            stream=False,
            user="pm-bg-confirm",
        )

        with patch.object(main, "repo", FakeRepo()):
            with patch.object(main, "project_service", FakeProjectService()):
                with patch.object(
                    main,
                    "_pm_start_background_job",
                    AsyncMock(return_value="Listo. Ya mandé a generar los tickets."),
                ) as start_job:
                    result = await main._handle_openceo_pm_direct(payload, "pm-bg-confirm", "1")

        self.assertEqual(main._assistant_text_from_chat_result(result), "Listo. Ya mandé a generar los tickets.")
        start_job.assert_awaited_once_with(
            user_id="pm-bg-confirm",
            project="melanyryr",
            tool="codex",
            interview="El cliente quiere login con Google.",
            autorun=False,
        )

    async def test_pm_background_running_reports_progress(self) -> None:
        class FakeRepo:
            async def clear_pm_state(self, user_id: str) -> None:
                return None

            async def get_pm_state(self, user_id: str):
                return {
                    "step": "background_running",
                    "project": "melanyryr",
                    "tool": "codex",
                    "job": {"status": "running"},
                }

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                return None

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "melanyryr"}]

        payload = ChatCompletionRequest(
            model="pm",
            messages=[OpenAIMessage(role="user", content="ya termino de crear tickets de la entrevista?")],
            stream=False,
            user="pm-bg-progress",
        )

        with patch.object(main, "repo", FakeRepo()):
            with patch.object(main, "project_service", FakeProjectService()):
                result = await main._handle_openceo_pm_direct(
                    payload,
                    "pm-bg-progress",
                    "ya termino de crear tickets de la entrevista?",
                )

        self.assertIn("sigue trabajando en segundo plano", main._assistant_text_from_chat_result(result))

    async def test_pm_background_done_reports_ticket_titles(self) -> None:
        class FakeRepo:
            async def clear_pm_state(self, user_id: str) -> None:
                return None

            async def get_pm_state(self, user_id: str):
                return {
                    "step": "background_done",
                    "project": "melanyryr",
                    "tool": "codex",
                    "tickets_created": 3,
                    "ticket_summaries": [
                        {"title": "Configurar Google OAuth", "ok": True},
                        {"title": "Implementar login en frontend", "ok": True},
                        {"title": "Construir dashboard de ventas", "ok": True},
                    ],
                    "job": {"status": "completed", "tickets_created": 3},
                }

            async def set_pm_state(self, user_id: str, state: dict) -> None:
                return None

        class FakeProjectService:
            def list_projects(self):
                return [{"name": "melanyryr"}]

        payload = ChatCompletionRequest(
            model="pm",
            messages=[OpenAIMessage(role="user", content="que tickets creo?")],
            stream=False,
            user="pm-bg-output",
        )

        with patch.object(main, "repo", FakeRepo()):
            with patch.object(main, "project_service", FakeProjectService()):
                result = await main._handle_openceo_pm_direct(payload, "pm-bg-output", "que tickets creo?")

        text = main._assistant_text_from_chat_result(result)
        self.assertIn("Configurar Google OAuth", text)
        self.assertIn("Implementar login en frontend", text)
        self.assertIn("Construir dashboard de ventas", text)

    def test_pm_interview_candidate_ignores_trigger_only_and_keeps_real_content(self) -> None:
        project_names = ["cebitec", "jardal-openceo"]

        self.assertIsNone(main._pm_extract_interview_candidate("tengo una entrevista", project_names))
        self.assertIsNone(main._pm_extract_interview_candidate("cebitec con codex", project_names))
        self.assertEqual(
            main._pm_extract_interview_candidate(
                "Entrevista: el cliente quiere login con Google y dashboard de auditoría.",
                project_names,
            ),
            "Entrevista: el cliente quiere login con Google y dashboard de auditoría.",
        )


if __name__ == "__main__":
    unittest.main()
