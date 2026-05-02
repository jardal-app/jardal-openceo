import { AsyncLocalStorage } from "node:async_hooks";
import { spawn } from "node:child_process";
import { randomBytes, createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  registerAppResource,
  registerAppTool,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import * as z from "zod/v4";
import {
  createLocalJWKSet,
  createRemoteJWKSet,
  exportJWK,
  generateKeyPair,
  importJWK,
  jwtVerify,
  SignJWT,
} from "jose";

const port = Number.parseInt(process.env.PORT ?? "8787", 10);
const mcpPath = normalizePath(process.env.MCP_PATH ?? "/mcp");
const appName = normalizeText(process.env.MCP_TEST_NAME, "openceo-chatgpt-connector");
const authMode = normalizeAuthMode(process.env.MCP_TEST_AUTH_MODE ?? "none");
const publicBaseUrl = stripTrailingSlash(process.env.APP_PUBLIC_BASE_URL ?? "");
const oauthProviderMode = normalizeOAuthProvider(
  process.env.MCP_TEST_OAUTH_PROVIDER ??
    (process.env.MCP_TEST_OAUTH_ISSUER_URL ? "external" : "embedded")
);
const explicitOauthIssuerUrl = stripTrailingSlash(process.env.MCP_TEST_OAUTH_ISSUER_URL ?? "");
const oauthIssuerUrl =
  oauthProviderMode === "embedded" && publicBaseUrl
    ? stripTrailingSlash(joinBaseUrl(publicBaseUrl, "oauth"))
    : explicitOauthIssuerUrl;
const oauthResource = stripTrailingSlash(process.env.MCP_TEST_OAUTH_RESOURCE ?? publicBaseUrl);
const oauthAudience = stripTrailingSlash(process.env.MCP_TEST_OAUTH_AUDIENCE ?? oauthResource);
const oauthScopes = parseCsv(process.env.MCP_TEST_OAUTH_SCOPES ?? "connector.access");
const oauthJwksUri = stripTrailingSlash(process.env.MCP_TEST_OAUTH_JWKS_URI ?? "");
const oauthAutoApprove = normalizeBoolean(process.env.MCP_TEST_OAUTH_AUTO_APPROVE ?? "true");
const oauthAccessTokenTtlSeconds = normalizePositiveInteger(
  process.env.MCP_TEST_OAUTH_ACCESS_TOKEN_TTL_SECONDS,
  3600
);
const demoUser = {
  sub: normalizeText(process.env.MCP_TEST_DEMO_USER_SUB, "demo-user"),
  email: normalizeText(process.env.MCP_TEST_DEMO_USER_EMAIL, "demo@example.com"),
  name: normalizeText(process.env.MCP_TEST_DEMO_USER_NAME, "Demo User"),
};
const authContextStore = new AsyncLocalStorage();
let oauthRuntimePromise;
const oauthClients = new Map();
const oauthCodes = new Map();
const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(normalizeText(process.env.OPENCEO_REPO_ROOT, resolve(__dirname, "..")));
const publicDir = resolve(__dirname, "public");
const extAppsBundlePath = resolve(
  __dirname,
  "node_modules",
  "@modelcontextprotocol",
  "ext-apps",
  "dist",
  "src",
  "app-with-deps.js"
);
const oauthDataDir = resolve(normalizeText(process.env.MCP_TEST_DATA_DIR, join(__dirname, ".runtime")));
const oauthSigningKeyPath = join(oauthDataDir, "oauth-signing-key.json");
const oauthClientsPath = join(oauthDataDir, "oauth-clients.json");
const connectorToolNames = [
  "get_company_context",
  "get_user_context",
  "execute_workflow",
  "get_task_status",
];
const executeWorkflowUiResourceUri = "ui://execute-workflow/view.html";
const executeWorkflowUiAssetPath = "/assets/mcp-apps/execute-workflow.js";
const extAppsBundleAssetPath = "/assets/vendor/mcp-apps-app-with-deps.js";
const anyObjectSchema = z.object({}).passthrough();
const pythonBridge = String.raw`
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(os.environ["OPENCEO_REPO_ROOT"]).resolve()
sys.path.insert(0, str(REPO_ROOT / "openceo-core"))

from agents.finance.elfec import run_elfec_house_payment_practice
from agents.finance.session import FinanceSessionManager
from agents.pm.session import PMSessionManager
from agents.secretary.session import SecretarySessionManager
from config_loader import ConfigLoader
from db.mongo import MongoRepository
from dev_session_service import DevSessionService
from events import RealtimeHub
from graph import OpenCEOGraph
from project_service import ProjectService
from rate_monitor import RateMonitor
from router import CEORouter
from settings import get_settings
from state import (
    AgentName,
    DevProvider,
    DevSessionControlRequest,
    DevSessionCreateRequest,
    DevSessionMessageRequest,
    DevSessionStatus,
    SessionControlAction,
    SessionMessageRole,
    TaskCreateRequest,
    TaskPriority,
    TaskSource,
    TaskStatus,
)
from task_service import TaskService
from tools.email_sender import SMTPEmailSender
from tools.notion import NotionTicketManager

OPEN_DEV_STATUSES = {
    DevSessionStatus.RUNNING.value,
    DevSessionStatus.WAITING_USER.value,
}
DEV_PROVIDERS = ["auto", "codex", "claude", "gemini", "opencode", "glm"]
PM_TOOLS = ["langraph", "codex", "claude"]


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _dump(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=_json_default))
    sys.stdout.flush()


def _now():
    return datetime.now(timezone.utc)


def _enum_field(required, values, note=None):
    return {
        "type": "string",
        "required": required,
        "enum": list(values),
        "note": note,
    }


def _string_field(required, note=None):
    return {
        "type": "string",
        "required": required,
        "enum": None,
        "note": note,
    }


def _array_field(required, note=None):
    return {
        "type": "array",
        "required": required,
        "enum": None,
        "note": note,
    }


def _boolean_field(required, note=None):
    return {
        "type": "boolean",
        "required": required,
        "enum": None,
        "note": note,
    }


def _catalog(config_loader):
    projects = config_loader.available_projects()
    finance_workflows = {}
    for project_name in projects:
        cfg = config_loader.load_project(project_name)
        for workflow in (((cfg.get("finance") or {}).get("workflows")) or []):
            workflow_id = str(workflow.get("id") or "").strip().lower()
            if not workflow_id:
                continue
            bucket = finance_workflows.setdefault(
                workflow_id,
                {
                    "name": workflow_id,
                    "description": str(workflow.get("name") or workflow_id).strip(),
                    "projects": set(),
                },
            )
            bucket["projects"].add(project_name)
            if str(workflow.get("name") or "").strip():
                bucket["description"] = str(workflow.get("name") or workflow_id).strip()

    finance_entries = {}
    for workflow_id, meta in sorted(finance_workflows.items()):
        projects_note = ", ".join(sorted(meta["projects"]))
        finance_entries[workflow_id] = {
            "description": meta["description"],
            "execution_mode": "sync",
            "requires_approval": True,
            "params": {
                "project": _string_field(
                    False,
                    f"Usa active_project si existe. Disponible en: {projects_note}.",
                ),
            },
        }

    return {
        "agents": {
            "dev": {
                "description": "Persistent coding sessions.",
                "workflows": {
                    "create_session": {
                        "description": "Create or focus a dev session.",
                        "execution_mode": "sync",
                        "requires_approval": False,
                        "params": {
                            "project": _string_field(False, "Usa active_project si existe."),
                            "provider": _enum_field(False, DEV_PROVIDERS, "Default: auto."),
                            "title": _string_field(False, "Default: ChatGPT dev session."),
                            "objective": _string_field(False, "Default: sesion dev iniciada desde ChatGPT."),
                        },
                    },
                    "send_message_async": {
                        "description": "Send one instruction to the active dev session.",
                        "execution_mode": "async",
                        "requires_approval": False,
                        "params": {
                            "message": _string_field(True),
                            "session_id": _string_field(False, "Usa la sesion activa si existe."),
                            "project": _string_field(False, "Ayuda a resolver la sesion si no se pasa session_id."),
                            "create_if_missing": _boolean_field(False, "Si es true, crea sesion cuando no exista."),
                            "provider": _enum_field(False, DEV_PROVIDERS, "Solo se usa cuando create_if_missing=true."),
                            "title": _string_field(False, "Solo se usa cuando create_if_missing=true."),
                            "objective": _string_field(False, "Solo se usa cuando create_if_missing=true."),
                        },
                    },
                    "close_session": {
                        "description": "Cancel the active dev session.",
                        "execution_mode": "sync",
                        "requires_approval": False,
                        "params": {
                            "session_id": _string_field(False, "Usa la sesion activa si existe."),
                            "project": _string_field(False, "Ayuda a resolver la sesion si no se pasa session_id."),
                        },
                    },
                },
            },
            "pm": {
                "description": "Create tickets from structured product input.",
                "workflows": {
                    "create_tickets": {
                        "description": "Register a pre-structured ticket batch in the canonical Mongo queue. Human-facing mirrors sync automatically after registration. If project is still unresolved, call it anyway and let the connector force confirmation or UI selection before registration.",
                        "execution_mode": "sync",
                        "requires_approval": True,
                        "params": {
                            "project": _string_field(False, "Prefiere proyecto explicito. Si falta, execute_workflow debe forzar confirmacion o UI antes de registrar. No lo infieras desde active_project."),
                            "tickets": _array_field(True),
                            "source": _string_field(False, "Default: entrevista."),
                            "source_id": _string_field(False, "Default: chatgpt-pm-<id>."),
                        },
                    },
                    "extract_and_create_tickets": {
                        "description": "Extract tickets from raw text and create them.",
                        "execution_mode": "async",
                        "requires_approval": True,
                        "note": "Path B only. In ChatGPT path always use create_tickets.",
                        "params": {
                            "project": _string_field(False, "No lo infieras desde active_project. Requiere proyecto explicito o confirmacion de UI."),
                            "raw_text": _string_field(True),
                            "tool": _enum_field(False, PM_TOOLS, "Default: project pm.default_tool or langraph."),
                        },
                    },
                },
            },
            "secretary": {
                "description": "Send operational email.",
                "workflows": {
                    "send_email": {
                        "description": "Send one SMTP email.",
                        "execution_mode": "sync",
                        "requires_approval": True,
                        "params": {
                            "from_email": _string_field(False),
                            "to": _array_field(True),
                            "cc": _array_field(False),
                            "bcc": _array_field(False),
                            "subject": _string_field(True),
                            "body_text": _string_field(True),
                            "body_html": _string_field(False),
                            "reply_to": _string_field(False),
                            "attachments": _array_field(False),
                            "language": _enum_field(False, ["es", "en"]),
                        },
                    },
                },
            },
            "finance": {
                "description": "Run deterministic payment workflows.",
                "workflows": finance_entries,
            },
        },
        "active_projects": projects,
        "usage_hints": {
            "catalog_cache": "Cachea este catalogo al inicio del chat y reutilizalo hasta que parezca viejo, incierto o el usuario pida refrescarlo.",
            "mcp_first": "Si aqui existe un workflow compatible para una accion real, prioriza el workflow conectado sobre resolver la accion solo en el chat.",
            "missing_params": "Cuando ya conozcas el workflow correcto y solo falten confirmaciones o selecciones finales que el connector puede resolver, llama execute_workflow en lugar de cerrar todo el loop manualmente en chat. El connector puede devolver needs_input o abrir MCP Apps para esos faltantes.",
        },
    }


async def _services(connect_repo=True):
    settings = get_settings()
    repo = MongoRepository(settings)
    if connect_repo:
        await repo.connect()
    config_loader = ConfigLoader(settings)
    router = CEORouter(settings)
    project_service = ProjectService(settings=settings, repo=repo, config_loader=config_loader)
    rate_monitor = RateMonitor(settings)
    rate_monitor.set_repo(repo)
    realtime_hub = RealtimeHub()
    notion = NotionTicketManager(settings) if settings.NOTION_API_TOKEN.strip() else None
    dev = DevSessionService(
        settings=settings,
        repo=repo,
        config_loader=config_loader,
        project_service=project_service,
        realtime_hub=realtime_hub,
        rate_monitor=rate_monitor,
    )
    pm = PMSessionManager(settings, config_loader=config_loader)
    finance = FinanceSessionManager(settings)
    secretary = SecretarySessionManager(settings)
    graph = OpenCEOGraph(
        router=router,
        config_loader=config_loader,
        dev_session=dev,
        finance_session=finance,
        secretary_session=secretary,
        pm_session=pm,
        notion=notion,
    )
    task_service = TaskService(
        settings=settings,
        repo=repo,
        graph=graph,
        realtime_hub=realtime_hub,
        notion=notion,
        rate_monitor=rate_monitor,
    )
    email = SMTPEmailSender(settings)
    return {
        "settings": settings,
        "repo": repo,
        "config_loader": config_loader,
        "project_service": project_service,
        "rate_monitor": rate_monitor,
        "realtime_hub": realtime_hub,
        "notion": notion,
        "dev": dev,
        "pm": pm,
        "task_service": task_service,
        "email": email,
    }


async def _close_services(services):
    repo = services.get("repo")
    if repo is not None:
        await repo.close()


async def _active_user_row(repo, user_id):
    return await repo.user_context.find_one({"user_id": user_id}) or {}


def _last_assistant_message(session):
    messages = list(session.get("messages") or [])
    for message in reversed(messages):
        if str(message.get("role") or "").strip().lower() != SessionMessageRole.ASSISTANT.value:
            continue
        content = str(message.get("content") or "").strip()
        if content:
            return content
    return None


async def _list_open_sessions(repo, user_id):
    sessions = await repo.list_dev_sessions(user_id=user_id, limit=100)
    return [
        session
        for session in sessions
        if str(session.get("status") or "").strip().lower() in OPEN_DEV_STATUSES
    ]


def _session_label(session):
    title = str(session.get("title") or "").strip()
    project = str(session.get("project") or "").strip()
    provider = str(session.get("provider") or "").strip()
    if title:
        return title
    if project and provider:
        return f"{project} ({provider})"
    return project or provider or str(session.get("session_id") or "").strip()


_PRIORITY_ALIASES = {
    "alta": "high",
    "media": "medium",
    "baja": "low",
    "critica": "critical",
    "crítica": "critical",
}


def _coerce_task_priority(raw_value):
    normalized = str(raw_value or "").strip().lower()
    normalized = _PRIORITY_ALIASES.get(normalized, normalized)
    if normalized in {item.value for item in TaskPriority}:
        return TaskPriority(normalized)
    return TaskPriority.MEDIUM


def _coerce_ticket_description(ticket):
    description = str(ticket.get("description") or "").strip()
    if description:
        return description
    fallback_lines = []
    module = str(ticket.get("module") or "").strip()
    if module:
        fallback_lines.append(f"Modulo: {module}")
    acceptance = ticket.get("acceptance_criteria")
    if isinstance(acceptance, list):
        rendered = [str(item).strip() for item in acceptance if str(item).strip()]
        if rendered:
            fallback_lines.append("Criterios de aceptacion:\n- " + "\n- ".join(rendered))
    client_summary = str(ticket.get("client_summary") or "").strip()
    if client_summary:
        fallback_lines.append(client_summary)
    rendered = "\n\n".join(fallback_lines).strip()
    if rendered:
        return rendered
    return str(ticket.get("title") or "").strip()


async def _resolve_user_context(repo, user_id):
    row = await _active_user_row(repo, user_id)
    active_project = str(row.get("selected_project") or "").strip() or None
    active_session = None
    session_id = str(row.get("active_dev_session_id") or "").strip()
    if session_id:
        session = await repo.get_dev_session(session_id)
        if session is not None:
            active_session = {
                "session_name": _session_label(session),
                "provider": str(session.get("provider") or "").strip() or None,
                "status": str(session.get("status") or "").strip() or None,
            }

    background_jobs = []
    for job in list(row.get("background_jobs") or []):
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status not in {"queued", "running", "waiting"}:
            continue
        background_jobs.append(
            {
                "task_id": str(job.get("task_id") or "").strip() or None,
                "agent": str(job.get("agent") or "").strip() or None,
                "workflow": str(job.get("workflow") or "").strip() or None,
                "status": status,
                "started_at": job.get("started_at"),
            }
        )

    pm_state = row.get("pm_state") or {}
    pm_job = pm_state.get("job") or {}
    if str(pm_job.get("status") or "").strip().lower() == "running":
        background_jobs.append(
            {
                "task_id": str(pm_job.get("id") or "").strip() or None,
                "agent": "pm",
                "workflow": "extract_and_create_tickets",
                "status": "running",
                "started_at": pm_job.get("started_at"),
            }
        )

    return {
        "active_project": active_project,
        "active_dev_session": active_session,
        "background_jobs": background_jobs,
    }


def _normalize_status(raw_status):
    value = str(raw_status or "").strip().lower()
    if value in {"in_progress", "running", "waiting", "waiting_user"}:
        return "running"
    if value in {"queued", "completed", "failed"}:
        return value
    return "failed" if value else "queued"


async def _upsert_background_job(repo, user_id, job):
    payload = dict(job)
    payload["started_at"] = payload.get("started_at") or _now()
    payload["updated_at"] = _now()
    result = await repo.user_context.update_one(
        {"user_id": user_id, "background_jobs.task_id": payload["task_id"]},
        {"$set": {"background_jobs.$": payload, "updated_at": _now()}},
    )
    if result.matched_count:
        return
    await repo.user_context.update_one(
        {"user_id": user_id},
        {
            "$set": {"user_id": user_id, "updated_at": _now()},
            "$push": {"background_jobs": payload},
        },
        upsert=True,
    )


async def _patch_task(repo, task_id, updates):
    payload = dict(updates)
    payload["updated_at"] = _now()
    await repo.tasks.update_one({"task_id": task_id}, {"$set": payload})


def _needs_input(message, field, label=None, options=None, target=None):
    payload = {
        "status": "needs_input",
        "message": message,
        "missing_param": field,
    }
    if options:
        payload["widget"] = {
            "label": label or field,
            "target": target or field,
            "options": options,
        }
    return payload


async def _resolve_project(repo, config_loader, user_id, explicit_project=None):
    if explicit_project:
        project = str(explicit_project).strip()
        if not config_loader.has_project(project):
            raise ValueError(f"Project not found: {project}")
        return project
    selected = await repo.get_selected_project(user_id)
    if selected:
        return selected
    available = config_loader.available_projects()
    if len(available) == 1:
        return available[0]
    return _needs_input(
        "Necesito saber que proyecto usar.",
        "project",
        label="Proyecto",
        target="params.project",
        options=[{"value": name, "label": name} for name in available],
    )


async def _resolve_pm_project(config_loader, params):
    explicit_project = str(params.get("project") or "").strip()
    if explicit_project:
        if not config_loader.has_project(explicit_project):
            raise ValueError(f"Project not found: {explicit_project}")
        return explicit_project
    available = config_loader.available_projects()
    if len(available) == 1:
        return available[0]
    return _needs_input(
        "Necesito confirmar que proyecto usar antes de crear tickets.",
        "project",
        label="Proyecto",
        target="params.project",
        options=[{"value": name, "label": name} for name in available],
    )


async def _select_session(repo, user_id, params, allow_widget):
    explicit_session_id = str(params.get("session_id") or "").strip()
    if explicit_session_id:
        session = await repo.get_dev_session(explicit_session_id)
        if session is None or str(session.get("user_id") or "").strip() != user_id:
            raise ValueError("Session not found")
        return session

    active_session_id, _ = await repo.get_active_dev_chat_session(user_id)
    if active_session_id:
        session = await repo.get_dev_session(active_session_id)
        if session is not None and str(session.get("status") or "").strip().lower() in OPEN_DEV_STATUSES:
            return session

    project = str(params.get("project") or "").strip()
    if project:
        session = await repo.find_latest_user_open_session(user_id=user_id, project=project)
        if session is not None:
            return session

    sessions = await _list_open_sessions(repo, user_id)
    if len(sessions) == 1:
        return sessions[0]
    if len(sessions) > 1 and allow_widget:
        return _needs_input(
            "Necesito saber que sesion usar.",
            "session_id",
            label="Sesion",
            target="params.session_id",
            options=[
                {
                    "value": str(session.get("session_id") or ""),
                    "label": _session_label(session),
                }
                for session in sessions
            ],
        )
    return None


async def _prepare_dev_create_session(services, user_id, params):
    project = await _resolve_project(
        services["repo"],
        services["config_loader"],
        user_id,
        params.get("project"),
    )
    if isinstance(project, dict):
        return project
    provider = str(params.get("provider") or "auto").strip().lower() or "auto"
    if provider not in DEV_PROVIDERS:
        return _needs_input(
            "Provider no soportado.",
            "provider",
            label="Provider",
            target="params.provider",
            options=[{"value": value, "label": value} for value in DEV_PROVIDERS],
        )
    payload = DevSessionCreateRequest(
        user_id=user_id,
        project=project,
        provider=DevProvider(provider),
        title=str(params.get("title") or "ChatGPT dev session").strip() or "ChatGPT dev session",
        objective=str(params.get("objective") or "Sesion dev iniciada desde ChatGPT").strip()
        or "Sesion dev iniciada desde ChatGPT",
    )
    created = await services["dev"].create_session(payload)
    await services["repo"].set_active_dev_chat_session(
        user_id=user_id,
        session_id=str(created["session_id"]),
        project=str(created["project"]),
    )
    return {
        "mode": "sync",
        "result": {
            "session_id": str(created["session_id"]),
            "session_name": _session_label(created),
            "project": str(created.get("project") or ""),
            "provider": str(created.get("provider") or ""),
            "status": str(created.get("status") or ""),
        },
    }


async def _prepare_dev_send_message(services, user_id, params):
    message = str(params.get("message") or "").strip()
    if not message:
        return _needs_input("Falta params.message.", "message")

    session = await _select_session(services["repo"], user_id, params, allow_widget=True)
    if isinstance(session, dict) and session.get("status") == "needs_input":
        return session

    create_if_missing = bool(params.get("create_if_missing"))
    if session is None and create_if_missing:
        create_result = await _prepare_dev_create_session(services, user_id, params)
        if create_result.get("mode") != "sync":
            return create_result
        session_id = str(create_result["result"]["session_id"])
    elif session is None:
        return _needs_input("No hay sesion dev activa. Crea una primero o usa create_if_missing=true.", "session_id")
    else:
        session_id = str(session["session_id"])

    return {
        "mode": "async",
        "agent": "dev",
        "workflow": "send_message_async",
        "normalized_params": {
            "session_id": session_id,
            "message": message,
        },
    }


async def _prepare_dev_close_session(services, user_id, params):
    session = await _select_session(services["repo"], user_id, params, allow_widget=True)
    if isinstance(session, dict) and session.get("status") == "needs_input":
        return session
    if session is None:
        return _needs_input("No encontre una sesion dev abierta.", "session_id")

    updated = await services["dev"].control_session(
        str(session["session_id"]),
        DevSessionControlRequest(user_id=user_id, action=SessionControlAction.CANCEL),
    )
    active_session_id, _ = await services["repo"].get_active_dev_chat_session(user_id)
    if active_session_id and active_session_id == str(session["session_id"]):
        await services["repo"].clear_active_dev_chat_session(user_id)
    return {
        "mode": "sync",
        "result": {
            "session_id": str(updated.get("session_id") or session["session_id"]),
            "session_name": _session_label(updated or session),
            "status": str((updated or session).get("status") or ""),
        },
    }


async def _prepare_pm_direct(services, user_id, params):
    tickets = params.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        return _needs_input("Falta params.tickets con un array estructurado.", "tickets")

    for index, ticket in enumerate(tickets, 1):
        if not isinstance(ticket, dict) or not str(ticket.get("title") or "").strip():
            raise ValueError(f"Ticket {index} requiere title")

    project = await _resolve_pm_project(services["config_loader"], params)
    if isinstance(project, dict):
        return project

    source_id = str(params.get("source_id") or f"chatgpt-pm-{uuid4().hex[:12]}").strip()
    source = str(params.get("source") or "entrevista").strip() or "entrevista"
    created = 0
    existing = 0
    task_ids = []
    results = []
    total = len(tickets)

    for index, item in enumerate(tickets, 1):
        ticket = dict(item)
        task_source_id = f"{source_id}:{index}"
        existing_task = await services["repo"].find_task_by_source(TaskSource.API.value, task_source_id)
        payload = TaskCreateRequest(
            project=project,
            source=TaskSource.API,
            source_id=task_source_id,
            title=str(ticket.get("title") or "").strip(),
            description=_coerce_ticket_description(ticket),
            agent_hint=AgentName.DEV,
            priority=_coerce_task_priority(ticket.get("priority")),
            params={
                "workflow": "pm.create_tickets",
                "pm_ticket": ticket,
                "pm_batch": {
                    "source": source,
                    "source_id": source_id,
                    "position": index,
                    "total": total,
                },
            },
            autorun=False,
        )
        task = await services["task_service"].create_task(payload)
        if existing_task is None:
            created += 1
        else:
            existing += 1
        task_ids.append(str(task.get("task_id") or ""))
        results.append(
            {
                "index": index - 1,
                "task_id": str(task.get("task_id") or ""),
                "title": str(task.get("title") or ticket.get("title") or ""),
                "status": str(task.get("status") or ""),
                "created": existing_task is None,
            }
        )

    return {
        "mode": "sync",
        "result": {
            "registered": total,
            "created": created,
            "existing": existing,
            "failed": 0,
            "source_id": source_id,
            "task_ids": task_ids,
            "results": results,
            "queue_status": "queued",
            "mirror_sync": "scheduled",
        },
    }


async def _prepare_pm_extract(services, user_id, params):
    raw_text = str(params.get("raw_text") or params.get("interview") or "").strip()
    if not raw_text:
        return _needs_input("Falta params.raw_text.", "raw_text")

    project = await _resolve_pm_project(services["config_loader"], params)
    if isinstance(project, dict):
        return project
    project_cfg = services["config_loader"].load_project(project)
    default_tool = str((((project_cfg.get("pm") or {}).get("default_tool")) or "langraph")).strip().lower() or "langraph"
    tool = str(params.get("tool") or default_tool).strip().lower()
    if tool not in PM_TOOLS:
        return _needs_input(
            "Tool de PM no soportado.",
            "tool",
            label="PM tool",
            target="params.tool",
            options=[{"value": value, "label": value} for value in PM_TOOLS],
        )

    return {
        "mode": "async",
        "agent": "pm",
        "workflow": "extract_and_create_tickets",
        "normalized_params": {
            "project": project,
            "tool": tool,
            "raw_text": raw_text,
        },
    }


async def _prepare_secretary_send_email(services, user_id, params):
    del user_id
    to = params.get("to")
    if not isinstance(to, list) or not to:
        return _needs_input("Falta params.to.", "to")
    subject = str(params.get("subject") or "").strip()
    if not subject:
        return _needs_input("Falta params.subject.", "subject")
    body_text = str(params.get("body_text") or "").strip()
    if not body_text:
        return _needs_input("Falta params.body_text.", "body_text")

    result = await asyncio.to_thread(
        services["email"].send_email,
        from_email=params.get("from_email"),
        to=to,
        cc=params.get("cc"),
        bcc=params.get("bcc"),
        subject=subject,
        body_text=body_text,
        body_html=params.get("body_html"),
        reply_to=params.get("reply_to"),
        attachments=params.get("attachments"),
        language=params.get("language"),
    )
    return {
        "mode": "sync",
        "result": result,
    }


async def _prepare_finance_workflow(services, user_id, workflow, params):
    project = await _resolve_project(
        services["repo"],
        services["config_loader"],
        user_id,
        params.get("project"),
    )
    if isinstance(project, dict):
        return project
    project_cfg = services["config_loader"].load_project(project)
    configured = {
        str(item.get("id") or "").strip().lower(): item
        for item in (((project_cfg.get("finance") or {}).get("workflows")) or [])
        if str(item.get("id") or "").strip()
    }
    workflow_cfg = configured.get(workflow)
    if workflow_cfg is None:
        options = []
        for project_name in services["config_loader"].available_projects():
            cfg = services["config_loader"].load_project(project_name)
            if any(str(item.get("id") or "").strip().lower() == workflow for item in (((cfg.get("finance") or {}).get("workflows")) or [])):
                options.append({"value": project_name, "label": project_name})
        if options:
            return _needs_input(
                "Ese workflow financiero no esta configurado para el proyecto resuelto.",
                "project",
                label="Proyecto",
                target="params.project",
                options=options,
            )
        raise ValueError(f"Workflow financiero no soportado: {workflow}")

    result = await asyncio.to_thread(run_elfec_house_payment_practice, services["settings"], workflow_cfg)
    return {
        "mode": "sync",
        "result": result,
    }


async def _prepare_execute_workflow(payload):
    services = await _services(connect_repo=True)
    try:
        user_id = str(payload.get("user_id") or "owner").strip() or "owner"
        agent = str(payload.get("agent") or "").strip().lower()
        workflow = str(payload.get("workflow") or "").strip().lower()
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if not agent:
            catalog = _catalog(services["config_loader"])
            return _needs_input(
                "Falta agent.",
                "agent",
                label="Agent",
                target="agent",
                options=[{"value": name, "label": name} for name in sorted(catalog["agents"])],
            )
        if not workflow:
            catalog = _catalog(services["config_loader"])
            workflows = catalog["agents"].get(agent, {}).get("workflows", {})
            if not workflows:
                raise ValueError(f"Unknown agent: {agent}")
            return _needs_input(
                "Falta workflow.",
                "workflow",
                label="Workflow",
                target="workflow",
                options=[{"value": name, "label": name} for name in sorted(workflows)],
            )

        if agent == "dev" and workflow == "create_session":
            return await _prepare_dev_create_session(services, user_id, params)
        if agent == "dev" and workflow == "send_message_async":
            return await _prepare_dev_send_message(services, user_id, params)
        if agent == "dev" and workflow == "close_session":
            return await _prepare_dev_close_session(services, user_id, params)
        if agent == "pm" and workflow == "create_tickets":
            return await _prepare_pm_direct(services, user_id, params)
        if agent == "pm" and workflow == "extract_and_create_tickets":
            return await _prepare_pm_extract(services, user_id, params)
        if agent == "secretary" and workflow == "send_email":
            return await _prepare_secretary_send_email(services, user_id, params)
        if agent == "finance":
            return await _prepare_finance_workflow(services, user_id, workflow, params)

        raise ValueError(f"Unknown workflow: {agent}.{workflow}")
    finally:
        await _close_services(services)


async def _run_async_task(payload):
    services = await _services(connect_repo=True)
    task_id = str(payload["task_id"])
    user_id = str(payload.get("user_id") or "owner").strip() or "owner"
    agent = str(payload["agent"]).strip().lower()
    workflow = str(payload["workflow"]).strip().lower()
    params = payload.get("params") or {}
    repo = services["repo"]
    now = _now()
    await repo.tasks.insert_one(
        {
            "task_id": task_id,
            "user_id": user_id,
            "agent": agent,
            "workflow": workflow,
            "status": "running",
            "summary": None,
            "result": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    await _upsert_background_job(
        repo,
        user_id,
        {
            "task_id": task_id,
            "agent": agent,
            "workflow": workflow,
            "status": "running",
            "started_at": now,
        },
    )

    try:
        if agent == "pm" and workflow == "extract_and_create_tickets":
            tool = str(params.get("tool") or "langraph").strip().lower() or "langraph"
            if tool == "langraph":
                result = await services["pm"].extract_tickets(
                    interview_text=str(params.get("raw_text") or ""),
                    project=str(params.get("project") or ""),
                    notion_manager=services["notion"],
                )
            else:
                result = await services["pm"].extract_tickets_subprocess(
                    interview_text=str(params.get("raw_text") or ""),
                    project=str(params.get("project") or ""),
                    tool=tool,
                    notion_manager=services["notion"],
                )
            final_result = {
                "created": int(result.tickets_created or 0),
                "failed": int(result.tickets_failed or 0),
                "tool": tool,
                "project": str(params.get("project") or ""),
                "ticket_summaries": list(result.ticket_summaries[:20]),
            }
            final_status = "completed" if result.status == TaskStatus.COMPLETED else "failed"
            summary = result.summary if result.summary else result.error
        elif agent == "dev" and workflow == "send_message_async":
            session_id = str(params.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id is required")
            await services["dev"].cleanup_runtime_orphans()
            await services["dev"].reconcile_open_sessions(user_id=user_id)
            await services["dev"].send_message_async(
                session_id,
                DevSessionMessageRequest(user_id=user_id, message=str(params.get("message") or "")),
            )
            timeout_seconds = max(60, int(services["settings"].DEV_TURN_TIMEOUT_SECONDS))
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            session = None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(2)
                session = await repo.get_dev_session(session_id)
                if session is None:
                    break
                status = str(session.get("status") or "").strip().lower()
                if status == DevSessionStatus.RUNNING.value:
                    continue
                break
            if session is None:
                raise ValueError("Session not found after async run")
            status = str(session.get("status") or "").strip().lower()
            if status == DevSessionStatus.RUNNING.value:
                raise TimeoutError("Dev session did not finish before timeout")
            if status == DevSessionStatus.FAILED.value:
                raise RuntimeError(str(session.get("waiting_reason") or "Dev session failed"))
            if status == DevSessionStatus.CANCELED.value:
                raise RuntimeError(str(session.get("waiting_reason") or "Dev session canceled"))
            last_response = _last_assistant_message(session)
            final_result = {
                "session_id": session_id,
                "session_name": _session_label(session),
                "status": status,
                "last_response": last_response,
            }
            final_status = "completed"
            summary = last_response or str(session.get("waiting_reason") or "Dev session finished")
        else:
            raise ValueError(f"Unsupported async workflow: {agent}.{workflow}")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        await _patch_task(
            repo,
            task_id,
            {
                "status": "failed",
                "summary": message,
                "result": {"error": message},
            },
        )
        await _upsert_background_job(
            repo,
            user_id,
            {
                "task_id": task_id,
                "agent": agent,
                "workflow": workflow,
                "status": "failed",
                "started_at": now,
            },
        )
    else:
        await _patch_task(
            repo,
            task_id,
            {
                "status": final_status,
                "summary": summary,
                "result": final_result,
            },
        )
        await _upsert_background_job(
            repo,
            user_id,
            {
                "task_id": task_id,
                "agent": agent,
                "workflow": workflow,
                "status": final_status,
                "started_at": now,
            },
        )
    finally:
        await _close_services(services)


async def _get_task_status(payload):
    services = await _services(connect_repo=True)
    try:
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        row = await services["repo"].tasks.find_one({"task_id": task_id})
        if row is None:
            raise ValueError("Task not found")
        return {
            "task_id": task_id,
            "status": _normalize_status(row.get("status")),
            "agent": str(row.get("agent") or "").strip() or None,
            "workflow": str(row.get("workflow") or "").strip() or None,
            "summary": row.get("summary") or row.get("last_response_summary"),
            "result": row.get("result"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at") or row.get("completed_at") or row.get("created_at"),
        }
    finally:
        await _close_services(services)


async def _main():
    operation = sys.argv[1]
    payload = json.loads(sys.stdin.read() or "{}")

    if operation == "get_company_context":
        config_loader = ConfigLoader(get_settings())
        _dump(_catalog(config_loader))
        return

    if operation == "get_user_context":
        services = await _services(connect_repo=True)
        try:
            user_id = str(payload.get("user_id") or "owner").strip() or "owner"
            _dump(await _resolve_user_context(services["repo"], user_id))
        finally:
            await _close_services(services)
        return

    if operation == "prepare_execute_workflow":
        _dump(await _prepare_execute_workflow(payload))
        return

    if operation == "run_async_task":
        await _run_async_task(payload)
        return

    if operation == "get_task_status":
        _dump(await _get_task_status(payload))
        return

    raise ValueError(f"Unknown operation: {operation}")


if __name__ == "__main__":
    asyncio.run(_main())
`;

loadPersistedOauthClients();
validateConfig();

function createMcpServer() {
  const server = new McpServer({ name: appName, version: "0.5.0" });

  server.server.registerCapabilities({
    tools: {
      listChanged: false,
    },
  });

  // Architecture rule for every tool below:
  // Path A = ChatGPT + MCP. ChatGPT does routing, extraction, summarization,
  // and approval loops before execute_workflow is called.
  // Path B = OpenWebUI + DeepSeek + LiveKit. That existing router/graph path
  // keeps its own intelligence and is not modified here.
  // Never make Path A tools redo intelligence ChatGPT already completed.
  registerAppResource(
    server,
    "Execute Workflow UI",
    executeWorkflowUiResourceUri,
    {
      description: "UI MCP Apps estandar para execute_workflow.",
      _meta: {
        ui: buildExecuteWorkflowUiResourceMeta(),
      },
    },
    async () => ({
      contents: [
        {
          uri: executeWorkflowUiResourceUri,
          mimeType: RESOURCE_MIME_TYPE,
          text: buildExecuteWorkflowUiHtml(),
          _meta: {
            ui: buildExecuteWorkflowUiResourceMeta(),
          },
        },
      ],
    })
  );
  server.registerTool(
    "get_company_context",
    {
      title: "Discover OpenCEO Capabilities",
      description:
        "Discovery step to run near chat start and then reuse from cache unless the catalog may be stale, missing, or uncertain. Use this to decide whether OpenCEO can perform the user's requested real-world action. If the cached/live catalog shows a compatible workflow, continue into approval or execute_workflow instead of satisfying the request only with local chat output. When the workflow is already identified and only confirmable/selectable params remain, prefer letting execute_workflow collect them through needs_input or MCP Apps rather than closing the whole loop manually in chat.",
      inputSchema: {},
      outputSchema: anyObjectSchema,
      annotations: {
        readOnlyHint: true,
        idempotentHint: true,
      },
    },
    async () => {
      try {
        return toolSuccess(await callPythonBridge("get_company_context", {}));
      } catch (error) {
        return toolFailure(error);
      }
    }
  );
  server.registerTool(
    "get_user_context",
    {
      title: "Read OpenCEO User State",
      description:
        "Read the current user state only when project, session, pending-work, or running-task resolution is relevant to the request. Returns active project, active dev session, and running jobs as advisory context. Do not silently use active_project to choose a project for project-sensitive write workflows; ask or confirm instead.",
      inputSchema: {
        user_id: z.string().default("owner"),
      },
      outputSchema: anyObjectSchema,
      annotations: {
        readOnlyHint: true,
        idempotentHint: true,
      },
    },
    async ({ user_id }) => {
      try {
        return toolSuccess(await callPythonBridge("get_user_context", { user_id: resolveUserId(user_id) }));
      } catch (error) {
        return toolFailure(error);
      }
    }
  );
  registerAppTool(
    server,
    "execute_workflow",
    {
      title: "Execute Discovered Workflow",
      description:
        "Single execution entrypoint for real connected OpenCEO actions after the model has already discovered the workflow from the cached catalog and prepared the params. If a compatible workflow exists, prefer this over local-only behavior. Once the workflow is known, call this even when a few remaining params still need confirmation or selection and the connector can resolve them through needs_input or MCP Apps. For approval-required side effects, move to a short approval summary and then execution instead of stopping at a drafted artifact in chat, unless the user explicitly asked to review the exact content first.",
      inputSchema: {
        agent: z.string().optional(),
        workflow: z.string().optional(),
        params: anyObjectSchema.default({}),
        user_id: z.string().default("owner"),
      },
      outputSchema: anyObjectSchema,
      annotations: {
        destructiveHint: true,
      },
      _meta: {
        ui: {
          resourceUri: executeWorkflowUiResourceUri,
        },
      },
    },
    async ({ agent, workflow, params, user_id }) => {
      try {
        const resolvedUserId = resolveUserId(user_id);
        const normalizedAgent = normalizeText(agent, "").toLowerCase();
        const normalizedWorkflow = normalizeText(workflow, "").toLowerCase();
        const rawParams = params && typeof params === "object" && !Array.isArray(params) ? { ...params } : {};
        const uiCommit = rawParams._ui_commit === true;
        delete rawParams._ui_commit;

        if (
          !uiCommit &&
          normalizedAgent === "pm" &&
          normalizedWorkflow === "create_tickets" &&
          Array.isArray(rawParams.tickets) &&
          rawParams.tickets.length
        ) {
          return toolSuccess(
            await buildPmCreateTicketsBatchUiPayload({
              agent: normalizedAgent,
              workflow: normalizedWorkflow,
              params: rawParams,
              user_id: resolvedUserId,
            }),
            "Abri la UI estandar del workflow PM para elegir proyecto y confirmar el registro del batch en Mongo antes de la sincronizacion automatica del espejo."
          );
        }

        const prepared = await callPythonBridge("prepare_execute_workflow", {
          agent: normalizedAgent || agent,
          workflow: normalizedWorkflow || workflow,
          params: rawParams,
          user_id: resolvedUserId,
        });

        if (prepared?.status === "needs_input") {
          return toolSuccess(prepared, prepared.message ?? "Necesito un dato adicional.");
        }

        if (prepared?.mode === "async") {
          const taskId = randomUUID();
          spawnPythonBridgeDetached("run_async_task", {
            task_id: taskId,
            user_id: resolvedUserId,
            agent: prepared.agent,
            workflow: prepared.workflow,
            params: prepared.normalized_params ?? {},
          });
          return toolSuccess(
            { task_id: taskId, status: "running" },
            `Inicie ${prepared.agent}.${prepared.workflow} en background.`
          );
        }

        if (prepared?.mode === "sync") {
          return toolSuccess(prepared.result ?? {}, summarizeResult(prepared.result));
        }

        return toolSuccess(prepared ?? {});
      } catch (error) {
        return toolFailure(error);
      }
    }
  );
  server.registerTool(
    "get_task_status",
    {
      title: "Read Async Task Status",
      description:
        "Read the latest status of one background task after an async execute_workflow call.",
      inputSchema: {
        task_id: z.string(),
      },
      outputSchema: anyObjectSchema,
      annotations: {
        readOnlyHint: true,
        idempotentHint: true,
      },
    },
    async ({ task_id }) => {
      try {
        return toolSuccess(await callPythonBridge("get_task_status", { task_id }));
      } catch (error) {
        return toolFailure(error);
      }
    }
  );
  return server;
}

function resolveUserId(userId) {
  const explicit = normalizeText(userId, "");
  if (explicit) {
    return explicit;
  }
  const authContext = authContextStore.getStore();
  const subject = normalizeText(authContext?.claims?.sub, "");
  return subject || "owner";
}

function resolvePythonBin() {
  const candidates = [
    normalizeText(process.env.OPENCEO_PYTHON_BIN, ""),
    join(repoRoot, ".venv", "bin", "python"),
    "python3",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (candidate === "python3" || existsSync(candidate)) {
      return candidate;
    }
  }
  throw new Error("No encontre un interprete Python compatible para el bridge de OpenCEO.");
}

function callPythonBridge(operation, payload) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(resolvePythonBin(), ["-c", pythonBridge, operation], {
      cwd: repoRoot,
      env: {
        ...process.env,
        OPENCEO_REPO_ROOT: repoRoot,
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", rejectPromise);
    child.on("close", (code) => {
      if (code !== 0) {
        rejectPromise(new Error((stderr || stdout || `Python bridge exited with code ${code}`).trim()));
        return;
      }
      const trimmed = stdout.trim();
      if (!trimmed) {
        resolvePromise({});
        return;
      }
      try {
        resolvePromise(JSON.parse(trimmed));
      } catch (error) {
        rejectPromise(new Error(`No pude parsear la salida del bridge Python: ${trimmed}`));
      }
    });
    child.stdin.end(JSON.stringify(payload ?? {}));
  });
}

function spawnPythonBridgeDetached(operation, payload) {
  const child = spawn(resolvePythonBin(), ["-c", pythonBridge, operation], {
    cwd: repoRoot,
    env: {
      ...process.env,
      OPENCEO_REPO_ROOT: repoRoot,
      PYTHONUNBUFFERED: "1",
    },
    detached: true,
    stdio: ["pipe", "ignore", "ignore"],
  });
  child.stdin.end(JSON.stringify(payload ?? {}));
  child.unref();
}

function buildPublicAssetBaseUrl() {
  return publicBaseUrl || `http://127.0.0.1:${port}`;
}

function buildPublicAssetUrl(pathname) {
  return joinBaseUrl(buildPublicAssetBaseUrl(), stripLeadingSlash(pathname));
}

function buildPublicAssetOrigin() {
  try {
    return new URL(buildPublicAssetBaseUrl()).origin;
  } catch {
    return null;
  }
}

function normalizePmBatchTickets(tickets) {
  if (!Array.isArray(tickets) || !tickets.length) {
    throw new Error("tickets requiere un array no vacio.");
  }

  return tickets.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`Ticket ${index + 1} debe ser un objeto.`);
    }
    const title = normalizeText(item.title, "");
    if (!title) {
      throw new Error(`Ticket ${index + 1} requiere title.`);
    }
    const normalized = { ...item, title };
    if (Array.isArray(item.tags)) {
      normalized.tags = item.tags
        .map((tag) => normalizeText(tag, ""))
        .filter(Boolean);
    }
    return normalized;
  });
}

function buildPmTicketPreview(tickets) {
  const preview = tickets.slice(0, 8).map((ticket, index) => ({
    index: index + 1,
    title: normalizeText(ticket.title, `Ticket ${index + 1}`),
    priority: normalizeText(ticket.priority, "") || null,
    ticket_type: normalizeText(ticket.ticket_type, "") || null,
    tags: Array.isArray(ticket.tags) ? ticket.tags.slice(0, 4) : [],
  }));
  return {
    count: tickets.length,
    preview,
    remaining_count: Math.max(tickets.length - preview.length, 0),
  };
}

function buildExecuteWorkflowUiResourceMeta() {
  const resourceOrigin = buildPublicAssetOrigin();
  const csp = {};
  if (resourceOrigin) {
    csp.resourceDomains = [resourceOrigin];
  }
  const meta = {
    prefersBorder: true,
    csp,
  };
  if (resourceOrigin) {
    meta.domain = resourceOrigin;
  }
  return meta;
}

function buildExecuteWorkflowUiHtml() {
  const scriptUrl = buildPublicAssetUrl(executeWorkflowUiAssetPath);
  return `<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenCEO PM Batch UI</title>
    <style>
      :root { color-scheme: dark; }
      html, body {
        margin: 0;
        min-height: 100%;
        background: var(--color-background-primary, #212121);
        color: var(--color-text-primary, #ececec);
        font-family: var(--font-sans, ui-sans-serif, system-ui, sans-serif);
      }
      body { min-height: 100vh; }
      #app { min-height: 100vh; }
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script>
      window.__OPENCEO_EXECUTE_WORKFLOW_UI__ = {
        executeToolName: "execute_workflow",
      };
    </script>
    <script type="module" src="${escapeHtml(scriptUrl)}"></script>
  </body>
</html>`;
}

async function buildPmCreateTicketsBatchUiPayload(input = {}) {
  const userId = resolveUserId(input.user_id);
  const params = input.params && typeof input.params === "object" && !Array.isArray(input.params)
    ? input.params
    : {};
  const tickets = normalizePmBatchTickets(params.tickets);
  const source = normalizeText(params.source, "entrevista");
  const sourceId = normalizeText(
    params.source_id,
    `chatgpt-pm-${randomUUID().replace(/-/g, "").slice(0, 12)}`
  );

  const companyContext = await callPythonBridge("get_company_context", {});

  const projects = Array.isArray(companyContext?.active_projects)
    ? companyContext.active_projects
      .map((project) => normalizeText(project, ""))
      .filter(Boolean)
    : [];

  let selectedProject = normalizeText(params.project, "");
  if (!selectedProject && projects.length === 1) {
    selectedProject = projects[0];
  }

  return {
    kind: "execute_workflow_pm_create_tickets_ui",
    title: "PM Batch Tickets",
    subtitle: "Selecciona el proyecto y confirma el registro del batch en Mongo. El espejo humano se sincroniza automaticamente.",
    fallback_text:
      "Si tu cliente no renderiza MCP Apps, llama execute_workflow con agent=pm, workflow=create_tickets y params.project.",
    user_context: {
      user_id: userId,
      active_project: null,
    },
    project: {
      selected: selectedProject || "",
      required: !selectedProject,
      options: projects.map((project) => ({
        value: project,
        label: project,
      })),
    },
    pending_workflow: {
      agent: normalizeText(input.agent, "pm") || "pm",
      workflow: normalizeText(input.workflow, "create_tickets") || "create_tickets",
      user_id: userId,
      params: {
        ...params,
        source,
        source_id: sourceId,
        tickets,
      },
    },
    ticket_summary: buildPmTicketPreview(tickets),
    notes: [
      "Esta UI es solo capa de presentacion; la ejecucion real sigue pasando por workflows deterministas.",
      "La seleccion de proyecto ocurre aqui para mantener compatibilidad con clientes MCP que soporten MCP Apps estandar.",
      "Los tickets se registran primero en Mongo y el espejo humano se sincroniza automaticamente desde el runtime principal.",
    ],
    generated_at: new Date().toISOString(),
  };
}

function toolSuccess(structuredContent, text, meta) {
  const result = {
    content: [
      {
        type: "text",
        text: text ?? summarizeResult(structuredContent),
      },
    ],
    structuredContent,
  };
  if (meta && typeof meta === "object" && Object.keys(meta).length) {
    result._meta = meta;
  }
  return result;
}

function toolFailure(error) {
  const message = error instanceof Error ? error.message : String(error);
  return {
    content: [
      {
        type: "text",
        text: message,
      },
    ],
    structuredContent: {
      ok: false,
      error: message,
    },
    isError: true,
  };
}

function summarizeResult(result) {
  if (!result || typeof result !== "object") {
    return String(result ?? "");
  }
  if (typeof result.message === "string" && result.message.trim()) {
    return result.message.trim();
  }
  if (typeof result.summary === "string" && result.summary.trim()) {
    return result.summary.trim();
  }
  if (typeof result.status === "string" && typeof result.task_id === "string") {
    return `task ${result.task_id}: ${result.status}`;
  }
  return JSON.stringify(result, null, 2);
}

function serveStaticAsset(res, pathname) {
  const asset = resolveStaticAsset(pathname);
  if (!asset) {
    res.writeHead(404).end("Not Found");
    return;
  }

  try {
    const body = readFileSync(asset.filePath);
    res.writeHead(200, {
      "content-type": asset.contentType,
      "cache-control": "public, max-age=300",
    });
    res.end(body);
  } catch (error) {
    res.writeHead(500).end(error instanceof Error ? error.message : "Asset read failed");
  }
}

function resolveStaticAsset(pathname) {
  if (pathname === extAppsBundleAssetPath) {
    return {
      filePath: extAppsBundlePath,
      contentType: "text/javascript; charset=utf-8",
    };
  }

  const relativePath = stripLeadingSlash(pathname.replace(/^\/assets\//, ""));
  if (!relativePath) {
    return null;
  }
  const candidate = resolve(publicDir, relativePath);
  if (!candidate.startsWith(`${publicDir}/`) && candidate !== publicDir) {
    return null;
  }
  if (!existsSync(candidate)) {
    return null;
  }
  return {
    filePath: candidate,
    contentType: guessContentType(candidate),
  };
}

function guessContentType(filePath) {
  if (filePath.endsWith(".js")) {
    return "text/javascript; charset=utf-8";
  }
  if (filePath.endsWith(".css")) {
    return "text/css; charset=utf-8";
  }
  if (filePath.endsWith(".svg")) {
    return "image/svg+xml";
  }
  if (filePath.endsWith(".png")) {
    return "image/png";
  }
  if (filePath.endsWith(".jpg") || filePath.endsWith(".jpeg")) {
    return "image/jpeg";
  }
  if (filePath.endsWith(".html")) {
    return "text/html; charset=utf-8";
  }
  return "application/octet-stream";
}

const httpServer = createServer(async (req, res) => {
  if (!req.url) {
    res.writeHead(400).end("Missing URL");
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host ?? "localhost"}`);
  const isMcpRequest = isMcpPath(url.pathname);

  if (
    req.method === "OPTIONS" &&
    (isMcpRequest || isAuthMetadataPath(url.pathname) || isEmbeddedOauthPath(url.pathname))
  ) {
    writeCorsHeaders(res);
    res.writeHead(204).end();
    return;
  }

  if (req.method === "GET" && url.pathname === "/") {
    writeCorsHeaders(res);
    res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify(
        {
          name: appName,
          status: "ok",
          auth_mode: authMode,
          tools: connectorToolNames,
          local_mcp_url: `http://127.0.0.1:${port}${mcpPath}`,
          public_mcp_url: publicBaseUrl ? joinBaseUrl(publicBaseUrl, stripLeadingSlash(mcpPath)) : null,
        },
        null,
        2
      )
    );
    return;
  }

  if (req.method === "GET" && url.pathname.startsWith("/assets/")) {
    writeCorsHeaders(res);
    return serveStaticAsset(res, url.pathname);
  }

  if (req.method === "GET" && url.pathname === "/.well-known/oauth-protected-resource") {
    if (authMode === "none") {
      writeCorsHeaders(res);
      res.writeHead(404).end("Not Found");
      return;
    }

    writeCorsHeaders(res);
    res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify(
        {
          resource: oauthResource,
          authorization_servers: [oauthIssuerUrl],
          scopes_supported: oauthScopes,
          resource_documentation: joinBaseUrl(publicBaseUrl, ""),
        },
        null,
        2
      )
    );
    return;
  }

  if (authMode !== "none" && oauthProviderMode === "embedded" && isEmbeddedOauthPath(url.pathname)) {
    await handleEmbeddedOauthRequest(req, res, url);
    return;
  }

  if (req.method === "GET" && isUnusedOAuthDiscoveryPath(url.pathname)) {
    writeCorsHeaders(res);
    res.writeHead(404).end("Not Found");
    return;
  }

  if (isMcpRequest && req.method && ["GET", "POST", "DELETE"].includes(req.method)) {
    writeCorsHeaders(res);

    const server = createMcpServer();
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    const authContext = await resolveAuthContext(req);

    res.on("close", () => {
      transport.close();
      server.close();
    });

    try {
      await authContextStore.run(authContext, async () => {
        await server.connect(transport);
        await transport.handleRequest(req, res);
      });
    } catch (error) {
      console.error("Error handling MCP request:", error);
      if (!res.headersSent) {
        res.writeHead(500).end("Internal server error");
      }
    }
    return;
  }

  writeCorsHeaders(res);
  res.writeHead(404).end("Not Found");
});

if (authMode !== "none") {
  const runtime = await getOauthRuntime();
  const registrationStatus = runtime.metadata.registration_endpoint
    ? "dynamic client registration detectado"
    : "sin registration_endpoint en discovery";
  const pkceStatus = runtime.metadata.code_challenge_methods_supported?.includes("S256")
    ? "PKCE S256 detectado"
    : "sin PKCE S256 en discovery";

  console.log(`[auth] provider=${oauthProviderMode}`);
  console.log(`[auth] issuer=${runtime.metadata.issuer ?? oauthIssuerUrl}`);
  console.log(`[auth] ${registrationStatus}; ${pkceStatus}`);
}

httpServer.listen(port, () => {
  const localUrl = `http://127.0.0.1:${port}${mcpPath}`;
  const publicConnectorUrl = publicBaseUrl
    ? joinBaseUrl(publicBaseUrl, stripLeadingSlash(mcpPath))
    : null;

  console.log(`MCP server listening on ${localUrl}`);
  console.log(`auth mode: ${authMode}`);
  if (publicConnectorUrl) {
    console.log(`ChatGPT connector URL: ${publicConnectorUrl}`);
  } else {
    console.log("ChatGPT connector URL: configura APP_PUBLIC_BASE_URL cuando tengas un tunel HTTPS.");
  }
});

async function resolveAuthContext(req) {
  if (authMode === "none") {
    return anonymousAuthContext();
  }

  const authorization = normalizeText(req.headers.authorization, "");
  if (!authorization.startsWith("Bearer ")) {
    return anonymousAuthContext({
      errorCode: "invalid_token",
      message: "Necesitas autenticar el conector antes de usar esta herramienta.",
    });
  }

  const token = authorization.slice("Bearer ".length).trim();
  if (!token) {
    return anonymousAuthContext({
      errorCode: "invalid_token",
      message: "No llego un access token valido.",
    });
  }

  try {
    const runtime = await getOauthRuntime();
    const { payload } = await jwtVerify(token, runtime.verifyKeySet, {
      issuer: runtime.metadata.issuer ?? oauthIssuerUrl,
      audience: oauthAudience || undefined,
    });

    return {
      authenticated: true,
      scopes: extractScopes(payload),
      claims: payload,
      errorCode: null,
      message: null,
    };
  } catch (error) {
    return anonymousAuthContext({
      errorCode: "invalid_token",
      message: `No pude validar el access token: ${error.message}`,
    });
  }
}

async function getOauthRuntime() {
  if (!oauthRuntimePromise) {
    oauthRuntimePromise = oauthProviderMode === "embedded"
      ? buildEmbeddedOauthRuntime()
      : buildExternalOauthRuntime();
  }

  return oauthRuntimePromise;
}

async function buildExternalOauthRuntime() {
  const metadata = await discoverAuthorizationServerMetadata();
  const jwksUrl = oauthJwksUri || metadata.jwks_uri;
  if (!jwksUrl) {
    throw new Error("El issuer no expone jwks_uri y MCP_TEST_OAUTH_JWKS_URI no fue configurado.");
  }

  return {
    providerMode: "external",
    metadata,
    verifyKeySet: createRemoteJWKSet(new URL(jwksUrl)),
  };
}

async function buildEmbeddedOauthRuntime() {
  const { publicKey, privateKey, publicJwk } = await loadOrCreateEmbeddedOauthSigningKeys();

  const metadata = {
    issuer: oauthIssuerUrl,
    authorization_endpoint: `${oauthIssuerUrl}/authorize`,
    token_endpoint: `${oauthIssuerUrl}/token`,
    registration_endpoint: `${oauthIssuerUrl}/register`,
    jwks_uri: `${oauthIssuerUrl}/jwks`,
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code"],
    token_endpoint_auth_methods_supported: ["none"],
    code_challenge_methods_supported: ["S256"],
    scopes_supported: oauthScopes,
  };

  return {
    providerMode: "embedded",
    metadata,
    verifyKeySet: createLocalJWKSet({ keys: [publicJwk] }),
    publicJwks: { keys: [publicJwk] },
    signAccessToken: async ({ scopes, resource, user }) => {
      const now = Math.floor(Date.now() / 1000);
      const expiresAt = now + oauthAccessTokenTtlSeconds;
      const token = await new SignJWT({
        sub: user.sub,
        email: user.email,
        name: user.name,
        scope: scopes.join(" "),
        aud: resource,
      })
        .setProtectedHeader({ alg: "RS256", kid: publicJwk.kid })
        .setIssuer(metadata.issuer)
        .setAudience(resource)
        .setIssuedAt(now)
        .setExpirationTime(expiresAt)
        .sign(privateKey);

      return {
        access_token: token,
        token_type: "Bearer",
        expires_in: oauthAccessTokenTtlSeconds,
        scope: scopes.join(" "),
      };
    },
  };
}

async function loadOrCreateEmbeddedOauthSigningKeys() {
  const persisted = await loadPersistedEmbeddedOauthSigningKeys();
  if (persisted) {
    return persisted;
  }

  const { publicKey, privateKey } = await generateKeyPair("RS256", { extractable: true });
  const kid = `connector-demo-key-${randomToken(6)}`;
  const publicJwk = await exportJWK(publicKey);
  publicJwk.alg = "RS256";
  publicJwk.use = "sig";
  publicJwk.kid = kid;

  const privateJwk = await exportJWK(privateKey);
  privateJwk.alg = "RS256";
  privateJwk.use = "sig";
  privateJwk.kid = kid;

  persistEmbeddedOauthSigningKeys({
    public_jwk: publicJwk,
    private_jwk: privateJwk,
  });

  return { publicKey, privateKey, publicJwk };
}

async function loadPersistedEmbeddedOauthSigningKeys() {
  try {
    if (!existsSync(oauthSigningKeyPath)) {
      return null;
    }
    const payload = JSON.parse(readFileSync(oauthSigningKeyPath, "utf8"));
    const publicJwk = payload?.public_jwk;
    const privateJwk = payload?.private_jwk;
    if (!publicJwk || !privateJwk) {
      return null;
    }
    const publicKey = await importJWK(publicJwk, "RS256");
    const privateKey = await importJWK(privateJwk, "RS256");
    return { publicKey, privateKey, publicJwk };
  } catch (error) {
    console.warn("[auth] no pude cargar oauth-signing-key.json; voy a regenerar la clave:", error);
    return null;
  }
}

function persistEmbeddedOauthSigningKeys(payload) {
  try {
    mkdirSync(oauthDataDir, { recursive: true });
    writeFileSync(oauthSigningKeyPath, JSON.stringify(payload, null, 2));
  } catch (error) {
    console.warn("[auth] no pude persistir oauth-signing-key.json:", error);
  }
}

async function discoverAuthorizationServerMetadata() {
  const candidates = [
    `${oauthIssuerUrl}/.well-known/openid-configuration`,
    `${oauthIssuerUrl}/.well-known/oauth-authorization-server`,
  ];

  const errors = [];
  for (const candidate of candidates) {
    try {
      const response = await fetch(candidate, {
        headers: { accept: "application/json" },
      });
      if (!response.ok) {
        errors.push(`${candidate} -> HTTP ${response.status}`);
        continue;
      }

      return await response.json();
    } catch (error) {
      errors.push(`${candidate} -> ${error.message}`);
    }
  }

  throw new Error(`No pude descubrir metadata OAuth. ${errors.join(" | ")}`);
}

async function handleEmbeddedOauthRequest(req, res, url) {
  writeCorsHeaders(res);
  const runtime = await getOauthRuntime();

  if (req.method === "GET" && isEmbeddedDiscoveryPath(url.pathname)) {
    return sendJson(res, 200, runtime.metadata);
  }

  if (req.method === "GET" && url.pathname === "/oauth/jwks") {
    return sendJson(res, 200, runtime.publicJwks);
  }

  if (req.method === "POST" && url.pathname === "/oauth/register") {
    const payload = await readJsonBody(req);
    const redirectUris = Array.isArray(payload.redirect_uris)
      ? payload.redirect_uris.filter((value) => typeof value === "string" && value.startsWith("https://"))
      : [];
    if (!redirectUris.length) {
      return sendJson(res, 400, {
        error: "invalid_client_metadata",
        error_description: "redirect_uris debe incluir al menos una URL HTTPS.",
      });
    }

    const clientId = `client_${randomToken(18)}`;
    oauthClients.set(clientId, {
      client_id: clientId,
      client_name: normalizeText(payload.client_name, "ChatGPT Connector"),
      redirect_uris: redirectUris,
      scope: normalizeText(payload.scope, oauthScopes.join(" ")),
      metadata: payload,
    });
    persistOauthClients();

    return sendJson(res, 201, {
      client_id: clientId,
      client_id_issued_at: Math.floor(Date.now() / 1000),
      client_name: normalizeText(payload.client_name, "ChatGPT Connector"),
      redirect_uris: redirectUris,
      grant_types: ["authorization_code"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
      scope: normalizeText(payload.scope, oauthScopes.join(" ")),
    });
  }

  if (req.method === "GET" && url.pathname === "/oauth/authorize") {
    let authorization;
    try {
      authorization = validateAuthorizeParams(url.searchParams);
    } catch (error) {
      return sendAuthorizeValidationError(res, error);
    }
    if (oauthAutoApprove) {
      return redirectWithAuthorizationCode(res, authorization);
    }

    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(renderAuthorizePage(authorization));
    return;
  }

  if (req.method === "POST" && url.pathname === "/oauth/authorize") {
    const params = await readFormBody(req);
    let authorization;
    try {
      authorization = validateAuthorizeParams(params);
    } catch (error) {
      return sendAuthorizeValidationError(res, error);
    }
    return redirectWithAuthorizationCode(res, authorization);
  }

  if (req.method === "POST" && url.pathname === "/oauth/token") {
    const params = await readFormBody(req);
    if (normalizeText(params.get("grant_type"), "") !== "authorization_code") {
      return sendJson(res, 400, {
        error: "unsupported_grant_type",
        error_description: "Solo authorization_code esta soportado en este issuer de prueba.",
      });
    }

    const code = normalizeText(params.get("code"), "");
    const redirectUri = normalizeText(params.get("redirect_uri"), "");
    const clientId = normalizeText(params.get("client_id"), "");
    const codeVerifier = normalizeText(params.get("code_verifier"), "");
    const resource = stripTrailingSlash(normalizeText(params.get("resource"), oauthResource));
    const pendingCode = oauthCodes.get(code);
    const client = oauthClients.get(clientId);

    if (!pendingCode || pendingCode.expires_at < Date.now()) {
      return sendJson(res, 400, {
        error: "invalid_grant",
        error_description: "El authorization code no existe o ya expiro.",
      });
    }
    if (!client || pendingCode.client_id !== clientId) {
      return sendJson(res, 400, {
        error: "invalid_client",
        error_description: "client_id invalido para este authorization code.",
      });
    }
    if (pendingCode.redirect_uri !== redirectUri) {
      return sendJson(res, 400, {
        error: "invalid_grant",
        error_description: "redirect_uri no coincide con el authorization code.",
      });
    }
    if (pendingCode.resource !== resource) {
      return sendJson(res, 400, {
        error: "invalid_target",
        error_description: "resource no coincide con el authorization code.",
      });
    }
    if (!verifyPkce(codeVerifier, pendingCode.code_challenge)) {
      return sendJson(res, 400, {
        error: "invalid_grant",
        error_description: "code_verifier invalido para el challenge registrado.",
      });
    }

    oauthCodes.delete(code);
    const tokenResponse = await runtime.signAccessToken({
      scopes: pendingCode.scopes,
      resource: pendingCode.resource,
      user: pendingCode.user,
    });

    return sendJson(res, 200, tokenResponse);
  }

  res.writeHead(404).end("Not Found");
}

function anonymousAuthContext(overrides = {}) {
  return {
    authenticated: false,
    scopes: [],
    claims: {},
    errorCode: null,
    message: null,
    ...overrides,
  };
}

function extractScopes(payload) {
  if (typeof payload.scope === "string" && payload.scope.trim()) {
    return payload.scope.trim().split(/\s+/);
  }
  if (Array.isArray(payload.scp)) {
    return payload.scp.filter((value) => typeof value === "string");
  }
  if (typeof payload.scp === "string" && payload.scp.trim()) {
    return payload.scp.trim().split(/\s+/);
  }
  return [];
}

function isEmbeddedOauthPath(pathname) {
  return pathname === "/oauth/register" ||
    pathname === "/oauth/authorize" ||
    pathname === "/oauth/token" ||
    pathname === "/oauth/jwks" ||
    isEmbeddedDiscoveryPath(pathname);
}

function isEmbeddedDiscoveryPath(pathname) {
  return pathname === "/oauth/.well-known/openid-configuration" ||
    pathname === "/oauth/.well-known/oauth-authorization-server";
}

async function readJsonBody(req) {
  const rawBody = await readRawBody(req);
  if (!rawBody) {
    return {};
  }
  try {
    return JSON.parse(rawBody);
  } catch (error) {
    throw new Error(`JSON invalido: ${error.message}`);
  }
}

async function readFormBody(req) {
  const rawBody = await readRawBody(req);
  return new URLSearchParams(rawBody);
}

function readRawBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => resolve(body));
    req.on("error", reject);
  });
}

function validateAuthorizeParams(paramsLike) {
  const getParam = (name) =>
    paramsLike instanceof URLSearchParams
      ? normalizeText(paramsLike.get(name), "")
      : normalizeText(paramsLike.get(name), "");

  const clientId = getParam("client_id");
  const redirectUri = getParam("redirect_uri");
  const responseType = getParam("response_type");
  const codeChallenge = getParam("code_challenge");
  const codeChallengeMethod = getParam("code_challenge_method");
  const state = getParam("state");
  const resource = stripTrailingSlash(getParam("resource") || oauthResource);
  const scopes = parseSpaceSeparated(getParam("scope"));
  const recoveredClient = recoverOauthClient({
    clientId,
    redirectUri,
    scope: getParam("scope"),
  });
  const client = oauthClients.get(clientId) ?? recoveredClient;

  if (!client) {
    throw new Error("client_id desconocido.");
  }
  if (responseType !== "code") {
    throw new Error("response_type debe ser code.");
  }
  if (!client.redirect_uris.includes(redirectUri)) {
    throw new Error("redirect_uri no esta registrado para el client.");
  }
  if (!codeChallenge || codeChallengeMethod !== "S256") {
    throw new Error("Este issuer de prueba requiere PKCE con code_challenge_method=S256.");
  }
  if (!resource || resource !== oauthResource) {
    throw new Error("resource invalido para este MCP.");
  }

  const clientScopes = parseSpaceSeparated(normalizeText(client.scope, ""));
  const allowedScopes = [...new Set([...oauthScopes, ...clientScopes])];
  const requestedScopes = scopes.length
    ? scopes.filter((scope) => allowedScopes.includes(scope))
    : [...allowedScopes];
  if (!requestedScopes.length) {
    if (allowedScopes.length) {
      return {
        client,
        redirectUri,
        codeChallenge,
        state,
        resource,
        scopes: [...allowedScopes],
      };
    }
    throw new Error("No llego ningun scope valido.");
  }

  return {
    client,
    client_id: clientId,
    redirect_uri: redirectUri,
    state,
    resource,
    scopes: requestedScopes,
    code_challenge: codeChallenge,
  };
}

function sendAuthorizeValidationError(res, error) {
  const description = error instanceof Error ? error.message : String(error);
  const lowered = description.toLowerCase();
  let oauthError = "invalid_request";
  if (lowered.includes("client_id")) {
    oauthError = "invalid_client";
  } else if (lowered.includes("resource")) {
    oauthError = "invalid_target";
  } else if (lowered.includes("scope")) {
    oauthError = "invalid_scope";
  }
  return sendJson(res, 400, {
    error: oauthError,
    error_description: description,
  });
}

function loadPersistedOauthClients() {
  try {
    if (!existsSync(oauthClientsPath)) {
      return;
    }
    const payload = JSON.parse(readFileSync(oauthClientsPath, "utf8"));
    if (!payload || typeof payload !== "object") {
      return;
    }
    for (const [clientId, client] of Object.entries(payload)) {
      if (!client || typeof client !== "object") {
        continue;
      }
      oauthClients.set(clientId, client);
    }
  } catch (error) {
    console.warn("[auth] no pude cargar oauth-clients.json:", error);
  }
}

function persistOauthClients() {
  try {
    mkdirSync(oauthDataDir, { recursive: true });
    writeFileSync(
      oauthClientsPath,
      JSON.stringify(Object.fromEntries(oauthClients.entries()), null, 2),
      "utf8"
    );
  } catch (error) {
    console.warn("[auth] no pude persistir oauth-clients.json:", error);
  }
}

function recoverOauthClient({ clientId, redirectUri, scope }) {
  if (!clientId || oauthClients.has(clientId)) {
    return oauthClients.get(clientId) ?? null;
  }
  if (!redirectUri.startsWith("https://")) {
    return null;
  }
  const recovered = {
    client_id: clientId,
    client_name: "Recovered ChatGPT Connector",
    redirect_uris: [redirectUri],
    scope: normalizeText(scope, oauthScopes.join(" ")),
    metadata: {
      recovered: true,
    },
  };
  oauthClients.set(clientId, recovered);
  persistOauthClients();
  return recovered;
}

function redirectWithAuthorizationCode(res, authorization) {
  const code = issueAuthorizationCode(authorization);
  const redirectUrl = new URL(authorization.redirect_uri);
  redirectUrl.searchParams.set("code", code);
  if (authorization.state) {
    redirectUrl.searchParams.set("state", authorization.state);
  }
  res.writeHead(302, { location: redirectUrl.toString() });
  res.end();
}

function issueAuthorizationCode(authorization) {
  const code = `code_${randomToken(24)}`;
  oauthCodes.set(code, {
    client_id: authorization.client_id,
    redirect_uri: authorization.redirect_uri,
    code_challenge: authorization.code_challenge,
    resource: authorization.resource,
    scopes: authorization.scopes,
    user: demoUser,
    expires_at: Date.now() + 5 * 60 * 1000,
  });
  return code;
}

function renderAuthorizePage(authorization) {
  const scopeLabel = authorization.scopes.join(", ");
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Authorize demo connector</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #07111f;
        --panel: #0d1b2f;
        --panel-border: rgba(89, 152, 255, 0.18);
        --text: #e6f0ff;
        --muted: #9cb7d9;
        --accent: #2b7fff;
        --accent-strong: #1b5ed1;
        --chip: rgba(43, 127, 255, 0.14);
      }
      body { font-family: "Avenir Next", "Segoe UI", sans-serif; background: radial-gradient(circle at top, #102744 0%, var(--bg) 58%); color: var(--text); padding: 24px; }
      main { max-width: 520px; margin: 0 auto; background: linear-gradient(180deg, rgba(17, 36, 61, 0.96), rgba(10, 21, 37, 0.98)); border: 1px solid var(--panel-border); border-radius: 18px; padding: 24px; box-shadow: 0 18px 60px rgba(0, 0, 0, 0.34); }
      h1 { margin-top: 0; }
      strong { color: var(--muted); }
      button { border: 0; border-radius: 12px; padding: 12px 16px; background: linear-gradient(180deg, var(--accent), var(--accent-strong)); color: white; font-weight: 700; cursor: pointer; }
      code { background: var(--chip); padding: 2px 6px; border-radius: 8px; color: var(--text); }
      p { line-height: 1.45; }
    </style>
  </head>
  <body>
    <main>
      <h1>Authorize demo connector</h1>
      <p><strong>Client:</strong> ${escapeHtml(authorization.client.client_name)}</p>
      <p><strong>User:</strong> ${escapeHtml(demoUser.email)}</p>
      <p><strong>Scopes:</strong> <code>${escapeHtml(scopeLabel)}</code></p>
      <form method="post" action="/oauth/authorize">
        <input type="hidden" name="client_id" value="${escapeHtml(authorization.client_id)}" />
        <input type="hidden" name="redirect_uri" value="${escapeHtml(authorization.redirect_uri)}" />
        <input type="hidden" name="response_type" value="code" />
        <input type="hidden" name="code_challenge" value="${escapeHtml(authorization.code_challenge)}" />
        <input type="hidden" name="code_challenge_method" value="S256" />
        <input type="hidden" name="scope" value="${escapeHtml(scopeLabel.replace(/,\s*/g, " "))}" />
        <input type="hidden" name="resource" value="${escapeHtml(authorization.resource)}" />
        <input type="hidden" name="state" value="${escapeHtml(authorization.state)}" />
        <button type="submit">Authorize as demo user</button>
      </form>
    </main>
  </body>
</html>`;
}

function writeCorsHeaders(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "content-type, authorization, mcp-session-id");
  res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id");
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload, null, 2));
}

function validateConfig() {
  if (!Number.isFinite(port) || port <= 0) {
    throw new Error(`PORT invalido: ${process.env.PORT}`);
  }

  if (!oauthScopes.length) {
    throw new Error("MCP_TEST_OAUTH_SCOPES no puede quedar vacio.");
  }

  if (authMode === "none") {
    return;
  }

  if (!publicBaseUrl.startsWith("https://")) {
    throw new Error("APP_PUBLIC_BASE_URL debe ser HTTPS cuando usas auth optional|required.");
  }
  if (oauthProviderMode === "external" && !oauthIssuerUrl.startsWith("https://")) {
    throw new Error("MCP_TEST_OAUTH_ISSUER_URL debe ser HTTPS cuando usas auth optional|required.");
  }
  if (!oauthResource.startsWith("https://")) {
    throw new Error("MCP_TEST_OAUTH_RESOURCE debe resolver a un identificador HTTPS.");
  }
}

function isMcpPath(pathname) {
  return pathname === mcpPath || pathname.startsWith(`${mcpPath}/`);
}

function isAuthMetadataPath(pathname) {
  return pathname === "/.well-known/oauth-protected-resource";
}

function isUnusedOAuthDiscoveryPath(pathname) {
  return (
    pathname === "/.well-known/openid-configuration" ||
    pathname === "/.well-known/oauth-authorization-server"
  );
}

function normalizePath(value) {
  const trimmed = normalizeText(value, "/mcp");
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeadingSlash.replace(/\/+$/, "") || "/mcp";
}

function normalizeText(value, fallback) {
  const trimmed = typeof value === "string" ? value.trim() : "";
  return trimmed || fallback;
}

function stripTrailingSlash(value) {
  return normalizeText(value, "").replace(/\/+$/, "");
}

function stripLeadingSlash(value) {
  return value.replace(/^\/+/, "");
}

function normalizeAuthMode(value) {
  const normalized = normalizeText(value, "none").toLowerCase();
  if (!["none", "optional", "required"].includes(normalized)) {
    throw new Error(`MCP_TEST_AUTH_MODE no soportado: ${value}`);
  }
  return normalized;
}

function normalizeOAuthProvider(value) {
  const normalized = normalizeText(value, "embedded").toLowerCase();
  if (!["embedded", "external"].includes(normalized)) {
    throw new Error(`MCP_TEST_OAUTH_PROVIDER no soportado: ${value}`);
  }
  return normalized;
}

function normalizeBoolean(value) {
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

function normalizePositiveInteger(value, fallback) {
  const raw = normalizeText(value, "");
  if (!raw) {
    return fallback;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`Entero positivo invalido: ${value}`);
  }
  return parsed;
}

function parseCsv(value) {
  return Array.from(
    new Set(
      String(value)
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    )
  );
}

function parseSpaceSeparated(value) {
  return String(value)
    .trim()
    .split(/\s+/)
    .filter(Boolean);
}

function joinBaseUrl(baseUrl, relativePath) {
  const base = baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
  return new URL(relativePath || ".", base).toString().replace(/\/$/, relativePath ? "" : "/");
}

function randomToken(size = 32) {
  return randomBytes(size).toString("base64url");
}

function verifyPkce(codeVerifier, codeChallenge) {
  if (!codeVerifier || !codeChallenge) {
    return false;
  }
  const digest = createHash("sha256").update(codeVerifier).digest("base64url");
  return digest === codeChallenge;
}

function escapeAuthHeaderValue(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
