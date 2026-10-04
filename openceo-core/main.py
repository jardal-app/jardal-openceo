from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import datetime, timezone
from uuid import uuid4

import json
import re
import time
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, SystemMessage

from agents.dev.session import DevSessionManager
from agents.finance.session import FinanceSessionManager
from agents.pm.session import PMSessionManager
from agents.secretary.session import SecretarySessionManager
from auth.router import auth_router
from auth.middleware import get_current_user, require_permissions
from auth.service import compute_user_permissions, get_groups_for_user, hash_password
from config_loader import ConfigLoader
from db.mongo import MongoRepository
from dev_session_service import DevSessionService
from events import RealtimeHub
from first_layer import FirstLayerGateway
from graph import OpenCEOGraph
from research.consumers import ConsumerRegistry
from research.mcp_facade import build_research_mcp_facade
from research.router import build_research_router
from research.wiring import build_research_service, build_social_broker, close_research_service
from research.subscriptions import ResearchSubscriptions
from research.watch import build_watch_adapters
from research.workflows import ResearchScheduleRuntime, principal_resolver_for
from integrations.openwebui_compat import (
    chat_response,
    extract_latest_user_message,
    parse_agent_hint,
)
from mcp_server import build_global_mcp_mount, build_internal_mcp_mount
from phase_workflows import merge_workflow_templates
from project_service import ProjectService
from qa_review_service import QAReviewService
from rate_monitor import RateMonitor
from router import CEORouter
from scheduler import WaitingTaskScheduler
from settings import get_settings
from storage_r2 import delete_files_sync, delete_file_sync, upload_file_sync
from state import (
    AgentName,
    ChatCompletionRequest,
    DevProvider,
    DevSessionControlRequest,
    DevSessionCreateRequest,
    DevSessionMessageRequest,
    DevSessionStatus,
    DiscoveryApprovalRequest,
    GroupDocument,
    ProjectDocument,
    ProjectSelectionRequest,
    TaskReviewCreateRequest,
    TaskCreateRequest,
    TaskPatchRequest,
    TaskPhaseActionRequest,
    TaskPhaseKimiRequest,
    TaskPhaseRevertRequest,
    TaskPriority,
    TaskReviewAction,
    TaskReviewActorType,
    TaskWorkflowApplyRequest,
    BulkTaskWorkflowApplyRequest,
    TaskStatus,
    TaskSource,
    UserDocument,
    VoicePeerProgrammingRequest,
)
from horizon.models import (
    CaptureChannel,
    CaptureKind as HorizonCaptureKind,
    HorizonCaptureRequest,
    HorizonDelegateRequest,
    HorizonImportRequest,
    HorizonMetricsRequest,
    HorizonPlanRequest,
    HorizonProgressRequest,
    HorizonReviewRequest,
)
from horizon.triggers import HorizonScheduler, HorizonTriggerStore
from horizon.service import HorizonService
from task_service import TaskService
from tools.notion import NotionTicketManager
from tools.erpnext_tickets import ERPNextTicketAdapter
from workflow_service import WorkflowService

logger = logging.getLogger(__name__)

settings = get_settings()
repo = MongoRepository(settings)
config_loader = ConfigLoader(settings)
router = CEORouter(settings)
rate_monitor = RateMonitor(settings)
rate_monitor.set_repo(repo)
dev_step_manager = DevSessionManager(settings, config_loader, rate_monitor=rate_monitor, repo=repo)
finance_step_manager = FinanceSessionManager(settings)
pm_step_manager = PMSessionManager(
    settings,
    config_loader=config_loader,
)
secretary_step_manager = SecretarySessionManager(settings)
graph = OpenCEOGraph(
    router=router,
    config_loader=config_loader,
    dev_session=dev_step_manager,
    finance_session=finance_step_manager,
    secretary_session=secretary_step_manager,
    pm_session=pm_step_manager,
    notion=None,  # set after notion_manager is created
)
realtime_hub = RealtimeHub()
notion_manager = NotionTicketManager(settings) if settings.NOTION_API_TOKEN.strip() else None
graph._notion = notion_manager  # late-bind after creation
# Adapter de tickets hacia ERPNext. No es opcional: ERPNext es el registro de negocio de
# los tickets, asi que construir el cliente siempre. Construirlo NO abre red (el
# httpx.AsyncClient es perezoso); si falta configuracion se avisa fuerte al arrancar.
erpnext_adapter = ERPNextTicketAdapter(settings)
dev_step_manager._consensus._erpnext = erpnext_adapter
if not erpnext_adapter.is_enabled:
    logger.error(
        "ERPNext no esta configurado (%s). La creacion de tickets fallara hasta que lo este.",
        erpnext_adapter.client.config_error(),
    )
project_service = ProjectService(settings=settings, repo=repo, config_loader=config_loader)
workflow_service = WorkflowService(
    settings=settings,
    repo=repo,
    realtime_hub=realtime_hub,
)
dev_session_service = DevSessionService(
    settings=settings,
    repo=repo,
    config_loader=config_loader,
    project_service=project_service,
    realtime_hub=realtime_hub,
    rate_monitor=rate_monitor,
)
qa_review_service = QAReviewService(settings=settings, repo=repo)
task_service = TaskService(
    settings=settings, repo=repo, graph=graph, realtime_hub=realtime_hub,
    notion=notion_manager, rate_monitor=rate_monitor, workflow_service=workflow_service,
    qa_service=qa_review_service, erpnext=erpnext_adapter,
)
qa_review_service.set_task_service(task_service)
pm_step_manager.set_task_service(task_service)
workflow_service.set_task_service(task_service)
# El repositorio avisa de CADA transicion de estado; el servicio decide cuales se
# propagan a ERPNext. Se instala aqui porque el repositorio es anterior al servicio.
repo.set_status_observer(task_service.observe_task_status)
scheduler = WaitingTaskScheduler(
    settings=settings,
    task_service=task_service,
    dev_session_service=dev_session_service,
)
internal_mcp = build_internal_mcp_mount(settings, task_service, erpnext_adapter)
global_mcp = build_global_mcp_mount(settings, task_service, erpnext_adapter)
horizon_service = HorizonService(settings=settings, repo=repo)
# El timer unico de Horizon: duerme hasta el proximo vencimiento y lo despierta
# cada encolado. No es un barrido (AGENTS.md §5.1).
horizon_store = HorizonTriggerStore(repo, settings=settings)
horizon_scheduler = HorizonScheduler(
    repo=repo,
    store=horizon_store,
    handler=horizon_service.handle_trigger,
    settings=settings,
)
workflow_service.set_horizon_service(horizon_service)
task_service.set_horizon_hooks(on_task_terminal=horizon_service.acknowledge_task_result)
horizon_service.set_task_service(task_service)
first_layer_gateway = FirstLayerGateway(
    settings=settings,
    repo=repo,
    project_service=project_service,
    dev_session_service=dev_session_service,
    task_service=task_service,
    horizon_service=horizon_service,
)


async def _run_finance_voice(text: str) -> str | None:
    """Resuelve por voz/chat los flujos deterministas de Finance, sin tickets.

    Ingreso, gasto, saldo y regla del 10%: el registro en ERPNext (o la
    consulta) es el efecto, asi que la app puede apagarse despues. El despacho
    lo decide `agents.finance.intent`, un solo orden para los cuatro flujos.
    Devuelve el resumen breve apto para TTS, o None si no es texto financiero.
    """
    clean = str(text or "").strip()
    try:
        from agents.finance.intent import classify as _classify_finance

        if _classify_finance(clean) is None:
            return None
    except Exception:
        logger.warning("Finance intent classification failed", exc_info=True)
        return None
    task = {
        "task_id": f"finance-voice-{uuid4().hex}",
        "project": settings.DEFAULT_PROJECT,
        "title": clean.splitlines()[0][:120] if clean else "Finanzas por voz",
        "description": clean,
        "attachments": [],
        "priority": TaskPriority.MEDIUM.value,
    }
    try:
        result = await finance_step_manager.run(task, {})
    except Exception as exc:
        logger.warning("Finance voice flow failed: %s", exc)
        return "No pude resolver eso con finanzas ahora mismo. Lo intentamos de nuevo."
    return result.last_response_summary or result.error


def _is_finance_text(text: str) -> bool:
    """Ruteo del agente Finance: delegado en el detector unico de `intent`.

    Incluye la lectura de saldo (a diferencia de la guarda de Horizon, que solo
    reclama escritura y regla del 10%). Un fallo de import degrada a "no es
    finanzas", pero deja registro: el fail-open silencioso ocultaba que el
    libro mayor no se enteraba de una orden.
    """
    try:
        from agents.finance.intent import is_finance_text
    except Exception as exc:  # pragma: no cover - depende del arbol de imports
        logger.warning("Finance intent guard unavailable (%s)", exc)
        return False
    try:
        return bool(is_finance_text(text))
    except Exception:
        logger.warning("Finance intent guard failed", exc_info=True)
        return False


async def _handle_finance_direct(payload: ChatCompletionRequest, message: str) -> dict:
    summary = await _run_finance_voice(_strip_agent_prefix(message))
    return _maybe_stream(payload, summary or "No pude resolver eso con finanzas.")


# El dueno habla con el CEO y el CEO deriva a Finance (TASK-2026-00032).
first_layer_gateway.set_finance_handler(_run_finance_voice)
_PM_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with AsyncExitStack() as stack:
        if internal_mcp is not None:
            await stack.enter_async_context(internal_mcp.lifespan())
        if global_mcp is not None:
            await stack.enter_async_context(global_mcp.lifespan())

        await repo.connect()
        # Research: se construye SIEMPRE, aunque falten claves o CLI, para poder
        # explicar el motivo de indisponibilidad en vez de desaparecer sin traza. La
        # flag de capacidad no modifica el contrato de ERPNext (siempre activo).
        research_service, research_resources = build_research_service(get_settings(), repo)
        app.state.research_service = research_service
        if research_service._deps.events is not None:
            await research_service._deps.events.initialize()
            await research_service.recover_event_delivery()
        app.state.research_consumers = _research_consumers
        # Opcional: si su directorio de sesiones no es valido solo se apaga la ruta social.
        app.state.research_social, _social_reason = await build_social_broker(get_settings(), repo.db)
        _research_available, _research_reason = research_service.available()
        logger.info(
            "Research %s: %s",
            "habilitado" if _research_available else "deshabilitado",
            _research_reason,
        )
        if _research_mcp_app is not None:
            await stack.enter_async_context(_research_mcp_app.router.lifespan_context(app))
        # Broker MCP del worker. Se monta en el mismo lifespan y **antes** de que
        # cualquier job pueda despacharse: el runtime se niega a lanzar un worker sin
        # broker, asi que montarlo mas tarde solo produciria jobs fallidos.
        _research_broker = getattr(research_resources, "broker", None)
        if _research_broker is not None:
            app.mount(_research_broker.path, _research_broker.mount.app)
            await stack.enter_async_context(_research_broker.mount.lifespan())
            logger.info("Broker de research montado en %s", _research_broker.url)
        await repo.mark_interrupted_pm_states()
        await repo.mark_interrupted_tasks()
        await repo.mark_interrupted_dev_sessions()
        await dev_step_manager._consensus.reconcile_interrupted_runs()
        await dev_step_manager._consensus.cleanup_expired_all()
        await dev_session_service.startup_reconcile()
        await scheduler.start()
        # Ojo: dentro de ``lifespan`` hay un ``settings = get_settings()`` mas
        # abajo (bloque de auth) que vuelve ``settings`` local a toda la funcion.
        # Leerlo aqui antes de esa asignacion lanzaria UnboundLocalError, asi que
        # se consulta el ajuste por su cuenta.
        if get_settings().HORIZON_ENABLED:
            # Deja encolada la proxima ocurrencia de cada trabajo recurrente. El
            # espacio personal tiene un solo dueño, asi que se siembra ese.
            with suppress(Exception):
                await horizon_service.ensure_upcoming_triggers(
                    get_settings().HORIZON_OWNER_USER_ID
                )
            await horizon_scheduler.start()
        else:
            logger.info("Horizon deshabilitado: no se programan disparadores")

        # Initialize auth DB on startup
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo import MongoClient
        global _auth_db
        settings = get_settings()
        client = MongoClient(settings.MONGODB_URL)
        _auth_db = client[settings.MONGODB_DATABASE]
        # Ensure indexes
        from pymongo import ASCENDING
        _auth_db.users.create_index("user_id", unique=True)
        _auth_db.users.create_index("username", unique=True)
        _auth_db.users.create_index("email", unique=True)
        _auth_db.groups.create_index("group_id", unique=True)
        _auth_db.groups.create_index("name", unique=True)
        # Store in app state for router access
        app.state.auth_db = _auth_db
        # Create default admin user if needed
        from auth.service import create_default_user_if_needed
        create_default_user_if_needed(_auth_db)

        research_coordinator = ResearchScheduleRuntime(
            repo=repo, workflows=workflow_service, service=research_service,
            settings=settings,
            project_cfg_for=lambda project: config_loader.load_project(project),
            on_admitted=_wake_research_dispatcher,
            # Mismo lector con guard de egress y mismo descubrimiento (con su ledger) que
            # el servicio: la vigilancia no abre clientes ni gasto paralelos.
            adapters=build_watch_adapters(
                research_resources.http_reader,
                max_items=settings.RESEARCH_WATCH_MAX_ITEMS_PER_SOURCE,
            ),
            discover=research_resources.discover,
        )
        app.state.research_schedule_store = research_coordinator.store
        app.state.research_scheduler = research_coordinator.scheduler
        research_subscriptions = ResearchSubscriptions(
            store=research_coordinator.store, repo=repo, settings=settings,
            resolve_principal=principal_resolver_for(repo),
        )
        app.state.research_subscriptions = research_subscriptions
        if _research_consumers is not None:
            # Revocar un consumidor pausa lo que programo en su nombre, de inmediato.
            _loop = asyncio.get_running_loop()

            async def _pause_consumer(consumer_id: str) -> None:
                try:
                    paused = await research_subscriptions.pause_for_consumer(consumer_id, "grant_revoked")
                    logger.info("Consumidor %s revocado: %d suscripciones pausadas", consumer_id, paused)
                except Exception:  # noqa: BLE001 - un fallo aqui no puede perderse en silencio
                    logger.error(
                        "No se pudieron pausar las suscripciones del consumidor revocado %s",
                        consumer_id, exc_info=True,
                    )

            def _pause_consumer_subscriptions(consumer_id: str, _reason: str) -> None:
                asyncio.run_coroutine_threadsafe(_pause_consumer(consumer_id), _loop)

            _research_consumers.add_revocation_listener(_pause_consumer_subscriptions)
        stack.push_async_callback(research_coordinator.stop)
        research_schedules_started = await research_coordinator.start()
        logger.info("Research scheduler %s", "habilitado" if research_schedules_started else "deshabilitado")

        for label, mcp in [("Internal", internal_mcp), ("Global", global_mcp)]:
            if mcp is not None:
                logger.info(
                    "%s MCP enabled at %s (transport=%s, tools=%s, auth=%s)",
                    label,
                    mcp.internal_url,
                    mcp.transport,
                    ",".join(mcp.tool_names),
                    "on" if mcp.auth_enabled else "off",
                )

        yield

        await research_coordinator.stop()
        # Horizon se para antes que el resto para que ninguna escritura suya
        # caiga despues de cerrar el repositorio.
        await horizon_scheduler.stop()
        await scheduler.stop()
        await dev_session_service.shutdown()
        if erpnext_adapter is not None:
            await erpnext_adapter.aclose()
        # Research cierra sus workers y su almacen de evidencias ANTES que el
        # repositorio: una escritura suya despues del cierre quedaria huerfana.
        await close_research_service(research_resources)
        await repo.close()


app = FastAPI(title="OpenCEO Core", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount auth router
app.include_router(auth_router)


# ---------------------------------------------------------------- Research
# Fachada HTTP del investigador compartido (TASK-2026-00056, Entrega A). El servicio
# vive en `app.state.research_service` y se construye en el lifespan; aqui solo se
# expone el router, que resuelve la identidad con el mismo middleware que el resto.
async def _research_dispatch_loop(service: Any) -> None:
    """Despachador unico: drena la cola hasta vaciarla.

    Se despierta al admitir (no hay barrido periodico). Cada `run_once` toma **un**
    job con lease; el bucle sigue mientras queden trabajos encolados, de modo que
    admitir dos jobs no deja al segundo esperando a nadie.
    """
    while True:
        try:
            document = await service.run_once()
        except Exception:  # noqa: BLE001 - un fallo no puede matar el proceso
            logger.warning("El despachador de research fallo", exc_info=True)
            return
        if document is None:
            return


_research_dispatch_task: asyncio.Task | None = None


def _wake_research_dispatcher(_research_id: str):
    """Programa el despacho si no hay uno vivo. Devuelve la corrutina o None."""
    global _research_dispatch_task
    service = getattr(app.state, "research_service", None)
    if service is None:
        return None
    if _research_dispatch_task is not None and not _research_dispatch_task.done():
        # Ya hay un despachador drenando: el trabajo esta persistido y lo vera.
        return None
    _research_dispatch_task = asyncio.ensure_future(_research_dispatch_loop(service))
    return None


app.include_router(build_research_router(
    get_service=lambda request: getattr(request.app.state, "research_service", None),
    get_subscriptions=lambda request: getattr(request.app.state, "research_subscriptions", None),
))

from research.social_router import build_social_router

app.include_router(build_social_router(
    get_broker=lambda request: getattr(request.app.state, "research_social", None),
    project_cfg_for=lambda request, project: config_loader.load_project(project),
    enabled=lambda: get_settings().RESEARCH_ENABLED,
))

# Montura MCP de research, apagada por defecto: su control de acceso son las
# credenciales de consumidor, no el Bearer compartido de las monturas internas.
_research_mcp_app = None
_research_consumers = None
if get_settings().RESEARCH_MCP_ENABLED:
    _research_consumers = ConsumerRegistry()
    _research_mcp_server = build_research_mcp_facade(
        consumers=_research_consumers,
        get_service=lambda: getattr(app.state, "research_service", None),
        get_subscriptions=lambda: getattr(app.state, "research_subscriptions", None),
    )
    _research_mcp_app = _research_mcp_server.http_app(path="/")
    app.mount(get_settings().RESEARCH_MCP_PATH, _research_mcp_app)

# Auth DB accessor for middleware
_auth_db = None


def get_auth_db():
    """Get the auth database instance for middleware."""
    global _auth_db
    if _auth_db is None:
        raise RuntimeError("Auth DB not initialized")
    return _auth_db


if internal_mcp is not None:
    app.mount(internal_mcp.path, internal_mcp.app, name="internal-mcp")
if global_mcp is not None:
    app.mount(global_mcp.path, global_mcp.app, name="global-mcp")


async def verify_api_key(authorization: str = Header(default="")) -> None:
    """Validate Bearer token for /v1/ endpoints. Skips if OPENCEO_API_KEY is empty."""
    api_key = settings.OPENCEO_API_KEY
    if not api_key:
        return
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if authorization[7:] != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "openceo-core"}


# ------------------------- file upload (R2) -------------------------


@app.post("/api/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    current_user: UserDocument = Depends(get_current_user),
) -> list[dict]:
    """Upload one or more files to Cloudflare R2.

    On partial failure already-uploaded objects are rolled back so no orphan
    keys are left behind.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    uploaded: list[dict] = []
    try:
        for upload in files:
            content = await upload.read()
            meta = await asyncio.to_thread(
                upload_file_sync,
                settings,
                file_bytes=content,
                original_name=upload.filename or "unnamed",
                content_type=upload.content_type,
            )
            uploaded.append(meta)
        return uploaded
    except Exception as exc:
        # Rollback: delete everything that succeeded so far
        if uploaded:
            keys = [u["key"] for u in uploaded]
            failed_deletions = await asyncio.to_thread(
                delete_files_sync, settings, keys
            )
            if failed_deletions:
                logger.warning("Rollback left behind keys: %s", failed_deletions)
        logger.error("Upload failed, rolled back %s objects: %s", len(uploaded), exc)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc


@app.delete("/api/upload/{key}")
async def delete_upload(
    key: str,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Delete a single file from R2 by its key."""
    ok = await asyncio.to_thread(delete_file_sync, settings, key)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete file")
    return {"deleted": True, "key": key}


# ------------------------- workflows -------------------------


@app.get("/api/workflows")
async def list_workflows(include_steps: bool = Query(default=False)) -> dict:
    return {
        "workflows": workflow_service.list_workflows(include_steps=include_steps),
        "tools": workflow_service.tool_registry(),
    }


@app.get("/api/workflow-runs")
async def list_workflow_runs(
    task_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await repo.list_workflow_runs(task_id=task_id, status=status, limit=limit)


@app.get("/api/workflow-runs/{run_id}")
async def get_workflow_run(run_id: str) -> dict:
    run = await repo.get_workflow_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return run


@app.get("/api/discovery-runs")
async def list_discovery_runs(
    task_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await repo.list_discovery_runs(task_id=task_id, status=status, limit=limit)


@app.get("/api/discovery-runs/{discovery_id}")
async def get_discovery_run(discovery_id: str) -> dict:
    discovery = await repo.get_discovery_run(discovery_id)
    if discovery is None:
        raise HTTPException(status_code=404, detail="Discovery run not found")
    return discovery


@app.post("/api/discovery-runs/{discovery_id}/approve")
async def approve_discovery_run(discovery_id: str, payload: DiscoveryApprovalRequest) -> dict:
    try:
        return await workflow_service.approve_discovery_run(
            discovery_id,
            actor_id=payload.actor_id,
            approve=payload.approve,
            comment=payload.comment,
            execute=payload.execute,
            workflow_yaml=payload.workflow_yaml,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


# ------------------------- horizon (espacio personal) -------------------------
# Todas las rutas filtran por el usuario autenticado. El espacio personal no se
# expone a otros usuarios ni a los espejos externos (seccion 9.3).


@app.get("/api/horizon/profile")
async def get_horizon_profile(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    return await horizon_service.ensure_profile(
        current_user.user_id,
        display_name=current_user.display_name,
    )


@app.get("/api/horizon/objectives")
async def list_horizon_objectives(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    objectives = await horizon_service.list_objectives(current_user.user_id)
    return {"objectives": objectives, "count": len(objectives)}


@app.get("/api/horizon/plan")
async def get_horizon_plan(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    return await horizon_service.plan_summary(current_user.user_id)


@app.get("/api/horizon/next-action")
async def get_horizon_next_action(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    return await horizon_service.next_action(current_user.user_id)


@app.post("/api/horizon/capture")
async def create_horizon_capture(
    payload: HorizonCaptureRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    return await horizon_service.capture(
        current_user.user_id,
        payload.text,
        kind=payload.kind,
        channel=payload.channel,
        origin=payload.origin,
    )


@app.post("/api/horizon/progress")
async def create_horizon_progress(
    payload: HorizonProgressRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    # Con un aviso de por medio, la confirmacion cierra la accion que se le
    # mostro al usuario, no la que este vigente ahora (seccion 9.10).
    if payload.aviso_id:
        return await horizon_service.confirm_done(
            current_user.user_id,
            aviso_id=payload.aviso_id,
            text=payload.text,
        )
    return await horizon_service.record_progress(
        current_user.user_id,
        payload.text,
        source_id=payload.source_id,
        objective_id=payload.objective_id,
    )


@app.post("/api/horizon/import")
async def import_horizon_context(
    payload: HorizonImportRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    try:
        return await horizon_service.import_context(
            current_user.user_id,
            source_path=payload.source_path,
            display_name=current_user.display_name,
            force=payload.force,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/horizon/plan/today")
async def get_horizon_plan_today(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Plan vigente del dia. Si no existe, se prepara y se deja disponible."""
    plan = await horizon_service.plan_today(current_user.user_id, channel="api")
    return await horizon_service.deliver_plan(current_user.user_id, plan, channel="api")


@app.post("/api/horizon/plan/day")
async def post_horizon_plan_day(
    payload: HorizonPlanRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Replanifica con la disponibilidad declarada ("hoy solo tengo 15 minutos")."""
    plan = await horizon_service.plan_day(
        current_user.user_id,
        declared_free_minutes=payload.free_minutes,
        exercise_included=payload.exercise_included,
        date_key=payload.date_key,
        trigger_kind="daily_plan",
        source="api",
    )
    return await horizon_service.deliver_plan(current_user.user_id, plan, channel="api")


@app.get("/api/horizon/triggers")
async def list_horizon_triggers(
    statuses: str | None = Query(default=None, description="Estados separados por coma"),
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Trabajo pendiente persistido, para inspeccionar sin adivinar."""
    parsed = [s.strip() for s in (statuses or "").split(",") if s.strip()] or None
    triggers = await horizon_service.list_triggers(current_user.user_id, statuses=parsed)
    return {"triggers": triggers, "count": len(triggers)}


@app.post("/api/horizon/review")
async def post_horizon_review(
    payload: HorizonReviewRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    return await horizon_service.review_objectives(
        current_user.user_id, kind=payload.kind
    )


@app.post("/api/horizon/delegate")
async def post_horizon_delegate(
    payload: HorizonDelegateRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Encarga la siguiente accion personal (o una especifica) a dev en background."""
    return await horizon_service.delegate_action(
        current_user.user_id,
        action_id=payload.action_id,
        project=payload.project,
        instruction=payload.instruction,
        dedupe_key=payload.dedupe_key,
    )


@app.post("/api/horizon/actions/{action_id}/delegate")
async def post_horizon_action_delegate(
    action_id: str,
    payload: HorizonDelegateRequest | None = None,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Encarga una accion concreta a dev en background."""
    p = payload or HorizonDelegateRequest()
    return await horizon_service.delegate_action(
        current_user.user_id,
        action_id=action_id,
        project=p.project,
        instruction=p.instruction,
        dedupe_key=p.dedupe_key,
    )


@app.get("/api/horizon/delivery")
async def get_horizon_delivery(
    action_id: str | None = Query(default=None),
    channel: str = Query(default="api"),
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Obtiene el resultado de una tarea delegada UNA SOLA VEZ (delivered once)."""
    return await horizon_service.deliver_action_result(
        current_user.user_id,
        action_id=action_id,
        channel=channel,
    )


@app.post("/api/horizon/actions/{action_id}/metrics")
async def post_horizon_action_metrics(
    action_id: str,
    payload: HorizonMetricsRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Registra tiempo humano de encargo/supervisión y retrabajo."""
    return await horizon_service.record_delegation_metrics(
        current_user.user_id,
        action_id,
        human_supervision_minutes=payload.human_supervision_minutes,
        human_rework_minutes=payload.human_rework_minutes,
        rework=payload.rework,
        note=payload.note or "",
    )


@app.get("/api/horizon/delegation/summary")
async def get_horizon_delegation_summary(
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Resumen de delegacion efectiva e indicadores humanos (§10.3)."""
    return await horizon_service.get_delegation_summary(current_user.user_id)


# ------------------------- tasks (legacy compatible) -------------------------


@app.post("/api/tasks")
async def create_task(
    payload: TaskCreateRequest,
    current_user: UserDocument = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    data = payload.model_dump()
    data["created_by"] = current_user.user_id
    # Sin `source_id` no hay clave de idempotencia, y un reintento tras un timeout crearia
    # un SEGUNDO ticket de negocio en ERPNext (el `task_id` se genera de nuevo). La cabecera
    # `Idempotency-Key` se convierte en `source_id` sintetico para que el reintento
    # converja al mismo ticket en Mongo y en ERPNext.
    if idempotency_key and not str(data.get("source_id") or "").strip():
        data["source_id"] = idempotency_key.strip()
    return await task_service.create_task(TaskCreateRequest(**data))


@app.post("/api/integrations/erpnext/task")
async def erpnext_task_webhook(
    payload: dict = Body(...),
    x_openceo_secret: str | None = Header(default=None, alias="X-OpenCEO-Webhook-Secret"),
) -> dict:
    """Camino inverso: una decision humana en ERPNext llega al runtime.

    Lo llama el webhook de Frappe cuando alguien **cierra, cancela o borra** un `Task` en
    el desk. No lo llama un cron: AGENTS.md §5.1 prohibe los barridos periodicos, y el
    contrato exige que la actividad humana llegue por eventos explicitos.

    No usa sesion de usuario (quien llama es ERPNext): se autentica con el secreto
    compartido `ERPNEXT_WEBHOOK_SECRET`.
    """
    expected = (settings.ERPNEXT_WEBHOOK_SECRET or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="ERPNEXT_WEBHOOK_SECRET no esta configurado.")
    if not x_openceo_secret or not hmac.compare_digest(x_openceo_secret, expected):
        raise HTTPException(status_code=401, detail="Secreto de webhook invalido.")

    # Frappe manda el documento anidado en `doc`; se acepta tambien plano para poder
    # probar el endpoint a mano y sobrevivir a cambios del formato del webhook.
    doc = payload.get("doc") if isinstance(payload.get("doc"), dict) else payload
    doctype = str(payload.get("doctype") or doc.get("doctype") or "").strip()
    if doctype and doctype != "Task":
        return {"ok": True, "status": "ignored_doctype", "doctype": doctype}

    event = str(payload.get("event") or "").strip()
    deleted = event in ("on_trash", "after_delete") or doc.get("deleted") is True

    return await task_service.apply_business_status(
        task_id=doc.get("openceo_task_id"),
        ref=doc.get("openceo_ref"),
        business_status=doc.get("status"),
        deleted=deleted,
    )


@app.get("/api/tasks")
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=500),
    project: str | None = Query(default=None),
    client_visible: bool | None = Query(default=None),
    assigned_reviewer_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    phase: str | None = Query(default=None),
    workflow_key: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    current_user: UserDocument = Depends(get_current_user),
) -> list[dict]:
    """List tasks. Non-admin users only see tickets they are involved in."""
    query: dict[str, Any] = {}
    if project:
        query["project"] = project
    if client_visible is not None:
        query["client_visible"] = client_visible
    if assigned_reviewer_id:
        query["assigned_reviewer_ids"] = assigned_reviewer_id
    if status:
        query["status"] = status
    if phase:
        query["phase"] = phase
    if workflow_key:
        query["workflow_key"] = workflow_key
    if agent:
        query["$or"] = [{"agent": agent}, {"agent_hint": agent}]

    if not current_user.is_admin:
        query["involved_user_ids"] = current_user.user_id
        if current_user.is_client:
            query["client_visible"] = True

    if query:
        rows = repo.tasks.find(query, sort=[("created_at", -1)]).limit(limit)
        results = [MongoRepository._clean(row) async for row in rows]
        if current_user.is_client:
            for r in results:
                r["status_display"] = "Entregado" if r.get("status") == TaskStatus.COMPLETED.value else "En proceso"
        return results

    return await task_service.list_tasks(limit=limit)


@app.get("/api/tasks/stats")
async def task_stats(
    project: str | None = Query(default=None),
    current_user: UserDocument = Depends(get_current_user),
) -> dict[str, int]:
    """Return dashboard-friendly task counts using Mongo aggregation."""
    query: dict[str, Any] = {}
    if project:
        query["project"] = project

    if not current_user.is_admin:
        query["involved_user_ids"] = current_user.user_id
        if current_user.is_client:
            query["client_visible"] = True

    pipeline = []
    if query:
        pipeline.append({"$match": query})
    pipeline.append(
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "open": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", [TaskStatus.QUEUED.value, TaskStatus.IN_PROGRESS.value]]},
                            1,
                            0,
                        ]
                    }
                },
                "in_review": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$status", TaskStatus.WAITING.value]},
                                    {"$ne": ["$phase", "client_signoff"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "client_waiting": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$status", TaskStatus.WAITING.value]},
                                    {"$eq": ["$phase", "client_signoff"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "completed": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", TaskStatus.COMPLETED.value]},
                            1,
                            0,
                        ]
                    }
                },
            }
        }
    )

    rows = [row async for row in repo.tasks.aggregate(pipeline)]

    if not rows:
        return {
            "total": 0,
            "open": 0,
            "in_review": 0,
            "client_waiting": 0,
            "completed": 0,
        }

    row = rows[0]
    return {
        "total": int(row.get("total") or 0),
        "open": int(row.get("open") or 0),
        "in_review": int(row.get("in_review") or 0),
        "client_waiting": int(row.get("client_waiting") or 0),
        "completed": int(row.get("completed") or 0),
    }


@app.post("/api/tasks/search")
async def search_tasks(
    body: dict[str, Any],
    current_user: UserDocument = Depends(get_current_user),
) -> list[dict]:
    """Search tasks with filters in request body."""
    query: dict[str, Any] = {}

    for field in ("project", "status", "phase", "workflow_key", "agent", "qa_review_required", "qa_review_state", "client_visible"):
        if field in body and body[field] is not None:
            query[field] = body[field]

    if not current_user.is_admin:
        query["involved_user_ids"] = current_user.user_id
        if current_user.is_client:
            query["client_visible"] = True

    limit = int(body.get("limit", 50))
    rows = repo.tasks.find(query, sort=[("created_at", -1)]).limit(limit)
    results = [MongoRepository._clean(row) async for row in rows]

    if current_user.is_client:
        for r in results:
            r["status_display"] = "Entregado" if r.get("status") == TaskStatus.COMPLETED.value else "En proceso"

# ------------------------- Ticket Focus Endpoints -------------------------


@app.get("/api/tasks/focus")
async def get_focused_ticket(user_id: str = Query(default="openwebui")):
    """Get the currently focused ticket for a user."""
    focus = await repo.get_active_ticket(user_id)
    if not focus:
        return {"focused": False, "ticket": None}
    task = await repo.get_task(focus["task_id"])
    return {"focused": True, "focus": focus, "task": task}


@app.post("/api/tasks/{task_id_or_number}/focus")
async def set_focused_ticket(task_id_or_number: str, user_id: str = Query(default="openwebui")):
    """Set the focused ticket for a user by ticket number or task_id."""
    task = None
    if task_id_or_number.isdigit():
        task = await repo.get_task_by_ticket_number(int(task_id_or_number))
    if not task:
        task = await repo.get_task(task_id_or_number)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    t_num = task.get("ticket_number", 0)
    await repo.set_active_ticket(user_id=user_id, ticket_number=t_num, task_id=task["task_id"], project=task["project"])
    return {"status": "ok", "focused_ticket": t_num, "project": task["project"]}


@app.delete("/api/tasks/focus")
async def clear_focused_ticket(user_id: str = Query(default="openwebui")):
    """Clear the focused ticket for a user."""
    await repo.clear_active_ticket(user_id)
    return {"status": "ok", "focused": False}


@app.get("/api/tasks/{task_id}")
async def get_task(
    task_id: str,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        involved = task.get("involved_user_ids") or []
        if current_user.user_id not in involved:
            raise HTTPException(status_code=403, detail="Access denied")
        if current_user.is_client and not task.get("client_visible"):
            raise HTTPException(status_code=403, detail="Access denied")
    return task


@app.patch("/api/tasks/{task_id}")
async def patch_task(
    task_id: str,
    payload: TaskPatchRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    updates = payload.model_dump(exclude_none=True)
    update_keys = set(updates.keys())

    if not current_user.is_admin:
        involved = task.get("involved_user_ids") or []
        if current_user.user_id not in involved:
            raise HTTPException(status_code=403, detail="Access denied")
        if current_user.is_client and not task.get("client_visible"):
            raise HTTPException(status_code=403, detail="Access denied")

        user_permissions = set(current_user.all_permissions or [])
        if current_user.is_client:
            if not update_keys or not update_keys.issubset({"client_evidence"}):
                raise HTTPException(status_code=403, detail="Access denied")
        elif "write_tickets" in user_permissions:
            pass
        elif "qa_review" in user_permissions:
            if not update_keys or not update_keys.issubset({"qa_evidence", "params"}):
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            raise HTTPException(status_code=403, detail="Access denied")

    task = await task_service.patch_task(task_id, updates)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ------------------------- Involved users (generic) -------------------------


@app.post("/api/tasks/{task_id}/users")
async def add_involved_user(
    task_id: str,
    body: dict[str, Any],
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Add a user to involved_user_ids of a ticket."""
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        involved = task.get("involved_user_ids") or []
        if current_user.user_id not in involved:
            raise HTTPException(status_code=403, detail="Access denied")

    history_entry = {
        "action": "user_added",
        "actor_id": current_user.user_id,
        "detail": f"User {user_id} added to ticket",
        "timestamp": datetime.now(timezone.utc),
    }
    await repo.tasks.update_one(
        {"task_id": task_id},
        {
            "$addToSet": {"involved_user_ids": user_id},
            "$push": {"history": history_entry},
            "$set": {"updated_at": datetime.now(timezone.utc), "updated_by": current_user.user_id},
        },
    )
    return {"ok": True, "user_id": user_id}


@app.delete("/api/tasks/{task_id}/users/{user_id}")
async def remove_involved_user(
    task_id: str,
    user_id: str,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Remove a user from involved_user_ids of a ticket."""
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        involved = task.get("involved_user_ids") or []
        if current_user.user_id not in involved:
            raise HTTPException(status_code=403, detail="Access denied")

    history_entry = {
        "action": "user_removed",
        "actor_id": current_user.user_id,
        "detail": f"User {user_id} removed from ticket",
        "timestamp": datetime.now(timezone.utc),
    }
    await repo.tasks.update_one(
        {"task_id": task_id},
        {
            "$pull": {"involved_user_ids": user_id},
            "$push": {"history": history_entry},
            "$set": {"updated_at": datetime.now(timezone.utc), "updated_by": current_user.user_id},
        },
    )
    return {"ok": True, "user_id": user_id}


@app.post("/api/tasks/bulk-add-users")
async def bulk_add_users(
    body: dict[str, Any],
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Bulk add a user to involved_user_ids on multiple tickets."""
    user_id = str(body.get("user_id") or "").strip()
    task_ids = [t for t in (body.get("task_ids") or []) if t]
    if not user_id or not task_ids:
        raise HTTPException(status_code=400, detail="user_id and task_ids required")

    history_entry = {
        "action": "user_added",
        "actor_id": current_user.user_id,
        "detail": f"User {user_id} added to ticket",
        "timestamp": datetime.now(timezone.utc),
    }

    assigned = 0
    failed = 0
    for tid in task_ids:
        try:
            await repo.tasks.update_one(
                {"task_id": tid},
                {
                    "$addToSet": {"involved_user_ids": user_id},
                    "$push": {"history": history_entry},
                    "$set": {"updated_at": datetime.now(timezone.utc), "updated_by": current_user.user_id},
                },
            )
            assigned += 1
        except Exception:
            failed += 1

    return {"assigned": assigned, "failed": failed, "user_id": user_id}


@app.post("/api/tasks/bulk-remove-users")
async def bulk_remove_users(
    body: dict[str, Any],
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Bulk remove a user from involved_user_ids on multiple tickets."""
    user_id = str(body.get("user_id") or "").strip()
    task_ids = [t for t in (body.get("task_ids") or []) if t]
    if not user_id or not task_ids:
        raise HTTPException(status_code=400, detail="user_id and task_ids required")

    history_entry = {
        "action": "user_removed",
        "actor_id": current_user.user_id,
        "detail": f"User {user_id} removed from ticket",
        "timestamp": datetime.now(timezone.utc),
    }

    removed = 0
    failed = 0
    for tid in task_ids:
        try:
            await repo.tasks.update_one(
                {"task_id": tid},
                {
                    "$pull": {"involved_user_ids": user_id},
                    "$push": {"history": history_entry},
                    "$set": {"updated_at": datetime.now(timezone.utc), "updated_by": current_user.user_id},
                },
            )
            removed += 1
        except Exception:
            failed += 1

    return {"removed": removed, "failed": failed, "user_id": user_id}


@app.get("/api/tasks/{task_id}/reviews")
async def list_task_reviews(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    try:
        return await task_service.list_task_reviews(task_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/reviews")
async def create_task_review(task_id: str, payload: TaskReviewCreateRequest) -> dict:
    try:
        return await task_service.create_task_review(task_id, payload)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Task not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/api/tasks/{task_id}/run")
async def run_task(task_id: str) -> dict:
    task = await task_service.run_task(task_id, force=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/tasks/{task_id}/tier-s/answer")
async def answer_tier_s_question(
    task_id: str,
    body: dict[str, Any],
    current_user: UserDocument = Depends(require_permissions("write_tickets")),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin and (
        task.get("project") not in (current_user.allowed_projects or [])
        or current_user.user_id not in (task.get("involved_user_ids") or [])
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    run = await repo.active_tier_s_run(task_id)
    if run is None or not run.get("question"):
        raise HTTPException(status_code=409, detail="No Tier S question is pending")
    answer = str(body.get("answer") or "").strip()
    if not answer or len(answer) > 2000 or "\n" in answer or "\r" in answer:
        raise HTTPException(status_code=400, detail="answer must be one line (1-2000 characters)")
    accepted = await repo.answer_tier_s_question(task_id, answer)
    if accepted is None:
        raise HTTPException(status_code=409, detail="Question already answered or attempt changed")
    async def resume() -> None:
        try:
            await task_service.run_task(task_id, force=True)
        except Exception:
            logger.exception("Tier S answer could not resume task %s", task_id)
            try:
                active = await repo.active_tier_s_run(task_id)
                if active and active.get("run_id") == run["run_id"] and active.get("status") == "resuming":
                    await repo.patch_tier_s_run(run["run_id"], {
                        "status": "waiting", "waiting_reason": "Error al reanudar; relanza el ticket",
                    })
            except Exception:
                logger.exception("Tier S answer recovery failed for task %s", task_id)
    asyncio.create_task(resume())
    return {"run_id": run["run_id"], "status": "resuming"}


@app.get("/api/tier-s/waits")
async def list_tier_s_waits(
    project: str = Query(..., min_length=1),
    current_user: UserDocument = Depends(require_permissions("write_tickets")),
) -> dict:
    if not current_user.is_admin and project not in (current_user.allowed_projects or []):
        raise HTTPException(status_code=403, detail="Access denied")
    runs = await repo.list_tier_s_waits(project)
    return {"project": project, "count": len(runs), "runs": [
        {key: run.get(key) for key in (
            "run_id", "task_id", "session_id", "phase", "subplan", "round",
            "waiting_reason", "question", "updated_at",
        )} for run in runs
    ]}


@app.post("/api/tasks/{task_id}/tier-s/cancel")
async def cancel_tier_s_run(
    task_id: str,
    current_user: UserDocument = Depends(require_permissions("write_tickets")),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin and (
        task.get("project") not in (current_user.allowed_projects or [])
        or current_user.user_id not in (task.get("involved_user_ids") or [])
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    config = config_loader.load_project(str(task.get("project") or ""))
    if not await dev_step_manager._consensus.cancel_run(task_id, config):
        raise HTTPException(status_code=409, detail="No active Tier S attempt")
    await task_service.patch_task(task_id, {"waiting_reason": "Intento TIER S cancelado por el propietario"})
    return {"status": "cancelled"}


@app.post("/api/tasks/{task_id}/workflow")
async def apply_task_workflow(
    task_id: str,
    payload: TaskWorkflowApplyRequest,
    current_user: UserDocument = Depends(require_permissions("write_tickets")),
) -> dict:
    try:
        task = await task_service.apply_phase_workflow_to_task(
            task_id,
            workflow_key=payload.workflow_key,
            workflow_phases=payload.workflow_phases,
            phase_assignments=payload.phase_assignments,
            reset_phase=payload.reset_phase,
            autorun=payload.autorun,
            actor_id=current_user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/tasks/bulk-apply-workflow")
async def bulk_apply_task_workflow(
    payload: BulkTaskWorkflowApplyRequest,
    current_user: UserDocument = Depends(require_permissions("write_tickets")),
) -> dict:
    if not payload.task_ids:
        raise HTTPException(status_code=400, detail="task_ids required")
    return await task_service.bulk_apply_phase_workflow(
        payload.task_ids,
        workflow_key=payload.workflow_key,
        workflow_phases=payload.workflow_phases,
        phase_assignments=payload.phase_assignments,
        reset_phase=payload.reset_phase,
        autorun=payload.autorun,
        actor_id=current_user.user_id,
    )


@app.post("/api/tasks/{task_id}/phase-action")
async def task_phase_action(
    task_id: str,
    payload: TaskPhaseActionRequest,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        if current_user.user_id not in (task.get("involved_user_ids") or []):
            raise HTTPException(status_code=403, detail="Access denied")
        if current_user.is_client and not task.get("client_visible"):
            raise HTTPException(status_code=403, detail="Access denied")

    phases = task.get("workflow_phases") or []
    current_phase = str(task.get("phase") or "")
    phase_doc = next((p for p in phases if isinstance(p, dict) and str(p.get("key") or "") == current_phase), None)
    required_permission = str((phase_doc or {}).get("permission") or "").strip()
    if required_permission and not current_user.is_admin and required_permission not in current_user.all_permissions:
        raise HTTPException(status_code=403, detail=f"Permiso requerido: {required_permission}")

    try:
        return await task_service.create_phase_action(
            task_id,
            actor_id=current_user.user_id,
            actor_type=TaskReviewActorType.CLIENT if current_user.is_client else TaskReviewActorType.INTERNAL,
            action=payload.action,
            comment=payload.comment,
            attachments=payload.attachments,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/phase-revert")
async def task_phase_revert(
    task_id: str,
    payload: TaskPhaseRevertRequest | None = None,
    current_user: UserDocument = Depends(require_permissions("qa_review")),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin and current_user.user_id not in (task.get("involved_user_ids") or []):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        return await task_service.revert_phase_approval(
            task_id,
            actor_id=current_user.user_id,
            actor_type=TaskReviewActorType.INTERNAL,
            comment=(payload.comment if payload else ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/phase-kimi")
async def task_phase_kimi(
    task_id: str,
    payload: TaskPhaseKimiRequest | None = None,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        if current_user.user_id not in (task.get("involved_user_ids") or []):
            raise HTTPException(status_code=403, detail="Access denied")
    try:
        return await qa_review_service.launch_phase_kimi_session(
            task_id=task_id,
            reviewer_id=current_user.user_id,
            phase_key=(payload.phase if payload else None),
            work_dir=(payload.work_dir if payload else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/discover-workflow")
async def discover_task_workflow(task_id: str, body: dict[str, Any] | None = None) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    reason = str((body or {}).get("reason") or "Manual workflow discovery requested.").strip()
    outcome = await workflow_service.start_discovery_for_task(task, reason=reason)
    updated = await task_service.patch_task(
        task_id,
        {
            "status": outcome.status,
            "steps": outcome.steps,
            "waiting_reason": outcome.waiting_reason,
            "last_response_summary": outcome.last_response_summary,
            "params": outcome.params,
        },
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return updated


# ------------------------- QA review -------------------------


@app.post("/api/tasks/{task_id}/qa-approve")
async def qa_approve_task(
    task_id: str,
    reviewer_id: str = Query(default="qa-web"),
    comment: str = Query(default=""),
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Backward-compatible QA approve route."""
    try:
        result = await task_service.create_phase_action(
            task_id,
            actor_id=current_user.user_id if current_user else reviewer_id,
            actor_type=TaskReviewActorType.INTERNAL,
            action=TaskReviewAction.APPROVE,
            comment=comment or "Aprobado por QA",
        )
        return result["task"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/phase-opencode")
async def task_phase_opencode(
    task_id: str,
    payload: TaskPhaseKimiRequest | None = None,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    task = await task_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not current_user.is_admin:
        if current_user.user_id not in (task.get("involved_user_ids") or []):
            raise HTTPException(status_code=403, detail="Access denied")
    try:
        return await qa_review_service.launch_phase_opencode_session(
            task_id=task_id,
            reviewer_id=current_user.user_id,
            phase_key=(payload.phase if payload else None),
            work_dir=(payload.work_dir if payload else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/qa-launch-kimi")
async def qa_launch_kimi(
    task_id: str,
    reviewer_id: str = Query(default="qa-web"),
    work_dir: str | None = Query(default=None),
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    """Backward-compatible Kimi launch route."""
    try:
        return await qa_review_service.launch_phase_kimi_session(
            task_id=task_id,
            reviewer_id=current_user.user_id if current_user else reviewer_id,
            phase_key=None,
            work_dir=work_dir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------- projects -------------------------


@app.post("/api/projects/select")
async def select_project(payload: ProjectSelectionRequest) -> dict:
    try:
        return await project_service.select_project(payload.user_id, payload.project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/selected")
async def get_selected_project(user_id: str = Query(default="default")) -> dict:
    project = await repo.get_selected_project(user_id)
    if project is None:
        try:
            project = await project_service.resolve_project(user_id)
        except ValueError:
            project = None
    return {"user_id": user_id, "project": project}


@app.get("/api/projects/{project_name}/workflow-templates")
async def get_project_workflow_templates(
    project_name: str,
    current_user: UserDocument = Depends(get_current_user),
) -> dict:
    if not current_user.is_admin and project_name not in (current_user.allowed_projects or []):
        raise HTTPException(status_code=403, detail="Access denied")
    row = await repo.projects.find_one({"name": project_name})
    templates = merge_workflow_templates(row.get("workflow_templates") if row else None)
    return {"project": project_name, "workflow_templates": templates}


@app.put("/api/projects/{project_name}/workflow-templates/{workflow_key}")
async def upsert_project_workflow_template(
    project_name: str,
    workflow_key: str,
    body: dict[str, Any],
    current_user: UserDocument = Depends(require_permissions("manage_projects")),
) -> dict:
    templates = dict((body.get("workflow_templates") if isinstance(body.get("workflow_templates"), dict) else {}) or {})
    template = dict(body.get("template") if isinstance(body.get("template"), dict) else body)
    if not template.get("phases"):
        raise HTTPException(status_code=400, detail="Template phases required")
    row = await repo.projects.find_one({"name": project_name})
    if row and isinstance(row.get("workflow_templates"), dict):
        templates = dict(row["workflow_templates"])
    templates[workflow_key] = template
    await repo.projects.update_one(
        {"name": project_name},
        {
            "$set": {
                "name": project_name,
                "display_name": (row or {}).get("display_name", project_name),
                "workflow_templates": templates,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {"created_at": datetime.now(timezone.utc), "description": ""},
        },
        upsert=True,
    )
    return {"project": project_name, "workflow_templates": merge_workflow_templates(templates)}


@app.delete("/api/projects/{project_name}/workflow-templates/{workflow_key}")
async def delete_project_workflow_template(
    project_name: str,
    workflow_key: str,
    current_user: UserDocument = Depends(require_permissions("manage_projects")),
) -> dict:
    await repo.projects.update_one(
        {"name": project_name},
        {
            "$unset": {f"workflow_templates.{workflow_key}": ""},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    row = await repo.projects.find_one({"name": project_name})
    return {"project": project_name, "workflow_templates": merge_workflow_templates((row or {}).get("workflow_templates"))}


# ------------------------- dev sessions -------------------------


@app.post("/api/dev/sessions")
async def create_dev_session(payload: DevSessionCreateRequest) -> dict:
    try:
        return await dev_session_service.create_session(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dev/sessions")
async def list_dev_sessions(
    project: str | None = None,
    user_id: str | None = None,
    status: DevSessionStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    await dev_session_service.reconcile_open_sessions(user_id=user_id, project=project)
    return await dev_session_service.list_sessions(project=project, user_id=user_id, status=status, limit=limit)


@app.get("/api/dev/sessions/{session_id}")
async def get_dev_session(session_id: str) -> dict:
    current = await dev_session_service.get_session(session_id)
    if current is not None and current.get("status") in _OPEN_DEV_SESSION_STATUSES:
        await dev_session_service.reconcile_open_sessions(
            user_id=str(current.get("user_id") or "").strip() or None,
            project=str(current.get("project") or "").strip() or None,
        )
    session = await dev_session_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.post("/api/dev/sessions/{session_id}/message")
async def send_dev_message(session_id: str, payload: DevSessionMessageRequest) -> dict:
    try:
        return await dev_session_service.send_message(session_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/dev/sessions/{session_id}/control")
async def control_dev_session(session_id: str, payload: DevSessionControlRequest) -> dict:
    try:
        return await dev_session_service.control_session(session_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------- voice peer programming -------------------------


@app.post("/api/voice/peer-programming")
async def voice_peer_programming(payload: VoicePeerProgrammingRequest) -> dict:
    try:
        return await dev_session_service.handle_voice_turn(
            user_id=payload.user_id,
            transcript=payload.transcript,
            session_id=payload.session_id,
            project=payload.project,
            provider=payload.provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------- Host Tmux Endpoints -------------------------


@app.get("/api/tmux/sessions")
async def list_host_tmux_sessions():
    """List all tmux sessions on the host system."""
    return dev_session_service._tmux.list_all_host_sessions()


@app.get("/api/tmux/sessions/{name}/pane")
async def get_host_tmux_pane(name: str, lines: int = Query(default=50, ge=1, le=500)):
    """Capture active pane content for a given tmux session."""
    output = dev_session_service._tmux.capture_pane_by_name(name, max_lines=lines)
    if output is None:
        raise HTTPException(status_code=404, detail=f"Tmux session '{name}' not found")
    return {"session": name, "output": output}


@app.post("/api/tmux/sessions/{name}/keys")
async def send_host_tmux_keys(name: str, payload: dict[str, Any]):
    """Send keys to a host tmux session."""
    keys = str(payload.get("keys") or "").strip()
    enter = bool(payload.get("enter", True))
    if not keys:
        raise HTTPException(status_code=400, detail="Keys cannot be empty")
    ok = dev_session_service._tmux.send_keys_by_name(name, keys, enter=enter)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Tmux session '{name}' not found")
    return {"status": "ok", "session": name}


@app.post("/api/tmux/sessions")
async def create_host_tmux_session(payload: dict[str, Any]):
    """Create a new tmux session on the host."""
    name = str(payload.get("name") or "").strip()
    cwd = payload.get("cwd")
    command = payload.get("command")
    if not name:
        raise HTTPException(status_code=400, detail="Session name cannot be empty")
    ok = dev_session_service._tmux.create_generic_session(name, cwd=cwd, command=command)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to create tmux session '{name}'")
    return {"status": "ok", "session": name}


@app.delete("/api/tmux/sessions/{name}")
async def kill_host_tmux_session(name: str):
    """Kill a host tmux session."""
    ok = dev_session_service._tmux.kill_by_name(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Tmux session '{name}' not found")
    return {"status": "ok", "session": name}


# ------------------------- OpenAI compatibility -------------------------


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": "ceo",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "jardal",
            },
            {
                "id": "dev",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "jardal",
            },
            {
                "id": "secretary",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "jardal",
            },
            {
                "id": "finance",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "jardal",
            },
            {
                "id": "pm",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "jardal",
            },
        ],
    }


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(payload: ChatCompletionRequest):
    message = extract_latest_user_message(payload.messages).strip()
    user_id = (payload.user or "openwebui").strip() or "openwebui"

    if not message:
        return _maybe_stream(payload, "Mensaje vacío.")

    if _is_openwebui_meta_prompt(message):
        return _maybe_stream(payload, _openwebui_meta_json_response(message), sanitize=False)

    # --- First Layer Cognitive Gateway (DeepSeek): contextual intent, tickets, tmux, and natural Spanish ---
    if not message.startswith("@") and settings.DEEPSEEK_API_KEY:
        handled, response_text = await first_layer_gateway.process_turn(user_id, message, payload)
        if handled:
            return _maybe_stream(payload, response_text)

    # --- Direct agent models: bypass CEO routing entirely (zero extra tokens) ---
    direct_agent = _resolve_direct_agent(payload.model)
    if direct_agent:
        if direct_agent == "dev":
            return await _handle_openceo_dev_direct(payload, user_id, message)
        if direct_agent == "pm":
            return await _handle_openceo_pm_direct(payload, user_id, message)
        return await _direct_agent_session(payload, user_id, message, direct_agent)

    pm_state = await repo.get_pm_state(user_id)
    if pm_state and not message.startswith("@") and _should_route_to_pm_state(pm_state, message):
        return await _handle_openceo_pm_direct(payload, user_id, message)

    # --- CEO model: full routing with @agent hints and LLM classification ---
    if message.startswith("@"):
        agent_hint = parse_agent_hint(message)
        if agent_hint and agent_hint.value != "dev":
            dispatch_message = _strip_agent_prefix(message)
            if agent_hint == AgentName.PM:
                return await _handle_openceo_pm_direct(payload, user_id, dispatch_message)
            if agent_hint == AgentName.FINANCE and _is_finance_text(dispatch_message):
                # Flujo determinista de finanzas: se resuelve directo, sin ticket.
                return await _handle_finance_direct(payload, dispatch_message)
            task_payload = TaskCreateRequest(
                project=payload.project or settings.DEFAULT_PROJECT,
                source=TaskSource.API,
                title=dispatch_message.splitlines()[0][:120] if dispatch_message else message.splitlines()[0][:120],
                description=dispatch_message or message,
                agent_hint=agent_hint,
                priority=TaskPriority.MEDIUM,
            )
            await task_service.create_task(task_payload)
            return _maybe_stream(payload, _build_task_dispatch_text(agent_hint))

    return await _handle_openceo_ceo_model(payload, user_id, message)


@app.post("/v1/responses", dependencies=[Depends(verify_api_key)], response_model=None)
async def responses(payload: dict[str, Any]):
    """OpenAI Responses API compatibility for OpenWebUI."""
    model = str(payload.get("model") or "ceo")
    user_id = _extract_user_id_from_responses_user(payload.get("user"))
    project = payload.get("project")
    project_name = project.strip() if isinstance(project, str) and project.strip() else None
    stream = bool(payload.get("stream"))

    messages = _openai_messages_from_responses_payload(payload)
    chat_payload = ChatCompletionRequest(
        model=model,
        messages=messages if messages else [{"role": "user", "content": ""}],
        stream=False,
        project=project_name,
        user=user_id,
    )
    chat_result = await chat_completions(chat_payload)
    text = _assistant_text_from_chat_result(chat_result) or "No pude generar una respuesta."

    if stream:
        return StreamingResponse(
            _stream_responses_sse(model=model, text=text),
            media_type="text/event-stream",
        )

    return _build_responses_result(model=model, text=text)


def _extract_user_id_from_responses_user(user: Any) -> str:
    if isinstance(user, str):
        value = user.strip()
        return value or "openwebui"
    if isinstance(user, dict):
        for key in ("id", "user_id", "email", "name"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "openwebui"


def _responses_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").lower()
            text = part.get("text")
            if part_type in {"input_text", "output_text", "text"} and isinstance(text, str):
                snippet = text.strip()
                if snippet:
                    parts.append(snippet)
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return str(content).strip()


def _openai_messages_from_responses_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    user_input = payload.get("input")
    if isinstance(user_input, str):
        text = user_input.strip()
        if text:
            messages.append({"role": "user", "content": text})
    elif isinstance(user_input, list):
        for item in user_input:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").lower() != "message":
                continue

            role = str(item.get("role") or "user").strip().lower()
            if role not in {"system", "user", "assistant", "tool", "developer"}:
                role = "user"

            text = _responses_content_to_text(item.get("content"))
            if not text:
                text = _responses_content_to_text(item.get("text"))
            if not text and role != "assistant":
                continue
            messages.append({"role": role, "content": text})

    if not messages:
        for key in ("message", "prompt", "query"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                messages.append({"role": "user", "content": value.strip()})
                break

    return messages


def _assistant_text_from_chat_result(chat_result: Any) -> str:
    if not isinstance(chat_result, dict):
        return ""
    choices = chat_result.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _is_openwebui_meta_prompt(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if not text.startswith("### Task:"):
        return False
    return "<chat_history>" in text.lower() or "### chat history:" in text.lower()


def _openwebui_meta_json_response(message: str) -> str:
    text = message.lower()
    if '"follow_ups"' in text or "follow-up questions" in text:
        return json.dumps({"follow_ups": []}, ensure_ascii=False)
    if '"title"' in text and "concise" in text:
        return json.dumps({"title": "💬 Sesión Dev"}, ensure_ascii=False)
    if '"tags"' in text or "broad tags" in text:
        return json.dumps({"tags": ["General"]}, ensure_ascii=False)
    return "{}"


def _build_responses_output_item(text: str, output_item_id: str | None = None) -> dict[str, Any]:
    return {
        "id": output_item_id or f"msg_{uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _build_responses_result(
    model: str,
    text: str,
    response_id: str | None = None,
    output_item_id: str | None = None,
) -> dict[str, Any]:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    output_item = _build_responses_output_item(text=text, output_item_id=output_item_id)
    return {
        "id": response_id or f"resp_{uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [output_item],
        "output_text": text,
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "store": False,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "truncation": "disabled",
        "temperature": 1,
        "top_p": 1,
        "usage": usage,
        "done": True,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


async def _stream_responses_sse(model: str, text: str):
    response_id = f"resp_{uuid4().hex}"
    output_item_id = f"msg_{uuid4().hex}"
    output_item = _build_responses_output_item(text=text, output_item_id=output_item_id)
    final_response = _build_responses_result(
        model=model,
        text=text,
        response_id=response_id,
        output_item_id=output_item_id,
    )
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": output_item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
    ]
    if text:
        events.append(
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            }
        )
    events.extend(
        [
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text},
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": output_item,
            },
            {
                "type": "response.completed",
                "response": final_response,
            },
        ]
    )
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _resolve_direct_agent(model: str) -> str | None:
    """Return agent name if the model targets a specific agent, None for CEO routing."""
    normalized = (model or "").strip().lower()
    if not normalized or normalized in {"ceo", "openceo"}:
        return None
    if normalized in {"dev", "secretary", "finance", "pm"}:
        return normalized
    prefix = "openceo-"
    if normalized.startswith(prefix):
        candidate = normalized[len(prefix):]
        if candidate in {"dev", "secretary", "finance", "pm"}:
            return candidate
    return None


def _strip_agent_prefix(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("@"):
        return stripped
    cleaned = re.sub(r"^@[a-zA-Z_]+\b[\s:,-]*", "", stripped, count=1).strip()
    return cleaned or stripped


def _build_task_dispatch_text(agent: AgentName | str) -> str:
    value = agent.value if isinstance(agent, AgentName) else str(agent or "").strip().lower()
    mapping = {
        AgentName.PM.value: "Te paso con PM.",
        AgentName.FINANCE.value: "Lo paso a finanzas.",
        AgentName.SECRETARY.value: "Lo paso a secretaría.",
        AgentName.MARKETING.value: "Lo paso a marketing.",
        AgentName.SALES.value: "Lo paso a ventas.",
        AgentName.MEDIA.value: "Lo paso a media.",
        AgentName.DEV.value: "Lo paso a desarrollo.",
    }
    return mapping.get(value, "Lo pongo en marcha.")


async def _handle_openceo_dev_direct(
    payload: ChatCompletionRequest,
    user_id: str,
    message: str,
) -> dict:
    """Natural dev-agent flow with explicit session orchestration."""
    try:
        project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await dev_session_service.cleanup_runtime_orphans()
    await dev_session_service.reconcile_open_sessions(user_id=user_id)

    text = message.strip()
    project_rows = project_service.list_projects()
    project_names = [p["name"] for p in project_rows]

    open_sessions = await _list_user_open_sessions(user_id)
    active_session_id, _ = await repo.get_active_dev_chat_session(user_id)
    active_session: dict[str, object] | None = None
    if active_session_id:
        candidate = await repo.get_dev_session(active_session_id)
        if candidate and candidate.get("user_id") == user_id and candidate.get("status") in _OPEN_DEV_SESSION_STATUSES:
            active_session = candidate
        else:
            await repo.clear_active_dev_chat_session(user_id)
            active_session_id = None

    if active_session is not None:
        active_message = _strip_active_session_wrapper(message)
        if active_message != message:
            message = active_message
            text = active_message.strip()

    exit_requested, exit_remainder = _consume_return_to_agent_phrase(text)
    if exit_requested:
        if not exit_remainder:
            if active_session is None:
                return _maybe_stream(payload, "Ya estabas fuera de la sesion.")
            await repo.clear_active_dev_chat_session(user_id)
            return _maybe_stream(payload, "Saliste de la sesion.")
        if active_session is not None:
            await repo.clear_active_dev_chat_session(user_id)
            active_session = None
            active_session_id = None
        text = exit_remainder.strip()
        message = exit_remainder

    if active_session is not None and _wants_active_session_output_shortcut(message):
        return _maybe_stream(payload, _build_dev_session_output_text(active_session))

    if active_session is not None:
        return await _send_to_dev_session(
            payload,
            user_id,
            message,
            str(active_session["session_id"]),
            bypass_agent_commands=True,
        )

    routed_project, routed_instruction = _extract_routed_instruction(text, project_names)
    target_from_text = _extract_project_from_text(
        text=text,
        project_names=project_names,
    )

    if _should_dedupe_dev_mutation(text):
        if _is_recent_dev_mutation(user_id=user_id, message=text):
            return _maybe_stream(payload, "Ese mismo mensaje ya lo estoy procesando. Dime otro o espera un momento.")
        _mark_recent_dev_mutation(user_id=user_id, message=text)

    if _wants_delete_all_sessions(message):
        result = await _cancel_user_open_sessions(user_id=user_id, project=None)
        await repo.clear_active_dev_chat_session(user_id)
        return _maybe_stream(payload, result)

    if _wants_delete_single_session(message):
        explicit_selector = _has_explicit_session_selector(message, project_names)
        target_session = _resolve_session_from_text(message, open_sessions, project_names)
        if target_session is None:
            if explicit_selector:
                return _maybe_stream(
                    payload,
                    "No encontre esa sesion abierta. Dilo por numero o proyecto, por ejemplo: elimina la sesion 2.",
                )
            if active_session is not None:
                target_session = active_session
            elif len(open_sessions) == 1:
                target_session = open_sessions[0]
            elif len(open_sessions) > 1:
                return _maybe_stream(
                    payload,
                    "Tienes varias sesiones abiertas. Dime cual cerrar por numero o proyecto, por ejemplo: elimina la sesion 2.",
                )
            else:
                return _maybe_stream(payload, "No encontre sesiones abiertas para eliminar.")

        removed = await _cancel_specific_session(
            user_id=user_id,
            session_id=str(target_session["session_id"]),
            reason="Session canceled from chat command",
        )
        if removed:
            if active_session_id and str(target_session.get("session_id")) == active_session_id:
                await repo.clear_active_dev_chat_session(user_id)
            return _maybe_stream(payload, f"Listo, cancele {_session_label(target_session)}.")
        return _maybe_stream(payload, f"No pude cancelar {_session_label(target_session)}, intenta de nuevo.")

    if _wants_dev_session_stop(message):
        await repo.clear_active_dev_chat_session(user_id)
        msg = (
            "Sesion dev desactivada para este chat. "
            "Seguimos en modo conversacion. Cuando quieras volver a programar, di: crea una sesion dev."
        )
        return _maybe_stream(payload, msg)

    if _wants_restore_last_conversation(message):
        target_session = _resolve_session_from_text(message, open_sessions, project_names)
        if target_session is None and target_from_text:
            target_session = await repo.find_latest_user_open_session(user_id=user_id, project=target_from_text)
        if target_session is None:
            target_session = await repo.find_latest_user_open_session(user_id=user_id, project=project)
        if target_session is None:
            target_session = await repo.find_latest_user_open_session(user_id=user_id, project=None)
        if target_session is None:
            return _maybe_stream(payload, "No encontre una sesion abierta para reestablecer la ultima conversacion.")
        try:
            _, resume_text = await _resume_session_from_command(
                user_id=user_id,
                session=target_session,
                fallback_project=project,
                reason="Resumed from chat command",
                restore_provider_conversation=True,
            )
        except ValueError as exc:
            return _maybe_stream(payload, f"No pude reestablecer {_session_label(target_session)}: {exc}")
        return _maybe_stream(payload, resume_text)

    model_command = _normalize_model_command_from_text(text)
    if model_command is not None:
        return await _handle_model_command(payload, user_id, model_command)

    if _wants_dev_help(text):
        return _maybe_stream(
            payload,
            _build_dev_capabilities_text(
                selected_project=project,
                sessions=open_sessions,
                llm_fallback_enabled=getattr(router, "_llm", None) is not None,
            ),
        )

    if _wants_project_listing(text):
        return _maybe_stream(payload, _build_project_listing_text(project_rows, project))

    if _wants_simple_active_sessions_query(text):
        return _maybe_stream(payload, _build_all_sessions_panorama(open_sessions))

    if _wants_session_listing(text):
        return _maybe_stream(payload, _build_open_sessions_text(open_sessions, active_session_id))

    if _wants_all_sessions_status(text):
        return _maybe_stream(payload, _build_all_sessions_panorama(open_sessions))

    if _wants_project_switch(text):
        switch_project = _extract_project_from_text(text, project_names)
        if switch_project:
            switch_session = await _find_open_or_saved_session_for_project(
                user_id=user_id,
                sessions=open_sessions,
                project=switch_project,
                provider=None,
            )

            if _is_status_question(text):
                # "como va el backend?" → show status without switching focus
                if switch_session:
                    return _maybe_stream(payload, _build_dev_session_progress_text_voice(switch_session))
                return _maybe_stream(payload, f"No hay sesion abierta en {switch_project}.")

            # Action: "vamos al backend" → switch focus
            if switch_session:
                target_project = str(switch_session.get("project") or switch_project)
                await repo.set_active_dev_chat_session(
                    user_id=user_id,
                    session_id=str(switch_session["session_id"]),
                    project=target_project,
                )
                await project_service.select_project(user_id=user_id, project=target_project)
                return _maybe_stream(payload, f"Sesion activa en {target_project}.")
            return _maybe_stream(payload, f"No hay sesion abierta en {switch_project}. Crea una con: crea sesion dev en {switch_project}.")

    if _wants_session_focus(text) and not _wants_dev_session_start(text):
        target_session = _resolve_session_from_text(text, open_sessions, project_names)
        if target_session is None and len(open_sessions) == 1:
            target_session = open_sessions[0]
        if target_session is None and target_from_text:
            target_session = await _find_open_or_saved_session_for_project(
                user_id=user_id,
                sessions=open_sessions,
                project=target_from_text,
                provider=None,
            )
        if target_session is None:
            return _maybe_stream(
                payload,
                "No encontre una sesion abierta para seleccionar. Puedes crear una diciendo: crea una sesion dev.",
            )

        target_project = str(target_session.get("project") or project)
        await repo.set_active_dev_chat_session(
            user_id=user_id,
            session_id=str(target_session["session_id"]),
            project=target_project,
        )
        await project_service.select_project(user_id=user_id, project=target_project)
        return _maybe_stream(payload, f"Sesion activa en {target_project}.")

    if _wants_project_selection(message) and target_from_text and not _wants_dev_session_start(message):
        await project_service.select_project(user_id=user_id, project=target_from_text)
        project = target_from_text
        return _maybe_stream(
            payload,
            (
                f"Proyecto {project} seleccionado. "
                "Si quieres empezar a programar, di: crea una sesion dev."
            ),
        )

    if _wants_dev_session_start(message):
        provider_hint = _extract_provider_from_text(message)
        target = target_from_text or project
        if target != project:
            await project_service.select_project(user_id=user_id, project=target)
            project = target

        session = _find_open_session_for_project(open_sessions, project, provider=provider_hint)
        created_new = False

        if session is None:
            session = await dev_session_service.create_session(
                DevSessionCreateRequest(
                    project=project,
                    user_id=user_id,
                    mode="peer",
                    provider=provider_hint or DevProvider.AUTO,
                    title=f"Sesión dev ({project})",
                    objective="Sesión creada manualmente desde dev. Esperando instrucciones.",
                )
            )
            created_new = True

        await repo.set_active_dev_chat_session(user_id=user_id, session_id=session["session_id"], project=project)

        if created_new:
            msg = f"Sesion creada con exito en {project}."
        else:
            msg = f"Sesion activa en {project}."
        return _maybe_stream(payload, msg)

    if _wants_dev_session_progress(message) or _wants_dev_session_output(message):
        target_session = _resolve_session_from_text(message, open_sessions, project_names)
        if target_session is None and active_session is not None:
            target_session = active_session
        if target_session is None and len(open_sessions) == 1:
            target_session = open_sessions[0]
        if target_session is None:
            running_sessions = [s for s in open_sessions if str(s.get("status") or "") == DevSessionStatus.RUNNING.value]
            if len(running_sessions) == 1:
                target_session = running_sessions[0]
        if target_session is None:
            latest = await repo.find_latest_user_session(user_id=user_id, project=target_from_text or project)
            target_session = latest
        if target_session is not None:
            if _wants_dev_session_output(message):
                return _maybe_stream(payload, _build_dev_session_output_text(target_session))
            return _maybe_stream(payload, _build_dev_session_progress_text(target_session))
        return _maybe_stream(payload, "No encontre sesiones previas para este proyecto.")

    instruction_like_by_pattern = _looks_like_dev_instruction(message)
    llm_intent: dict[str, Any] | None = None
    if not instruction_like_by_pattern:
        llm_intent = await _infer_dev_intent_with_llm(
            message=text,
            project_names=project_names,
            open_sessions=open_sessions,
            has_active_session=active_session is not None,
        )

    if llm_intent:
        intent_name = str(llm_intent.get("intent") or "unknown")
        if intent_name == "help":
            return _maybe_stream(
                payload,
                _build_dev_capabilities_text(
                    selected_project=project,
                    sessions=open_sessions,
                    llm_fallback_enabled=True,
                ),
            )
        if intent_name == "list_projects":
            return _maybe_stream(payload, _build_project_listing_text(project_rows, project))
        if intent_name == "list_sessions":
            return _maybe_stream(payload, _build_open_sessions_text(open_sessions, active_session_id))
        if intent_name == "all_sessions_status":
            return _maybe_stream(payload, _build_all_sessions_panorama(open_sessions))

        if intent_name in {"switch_session", "session_status", "session_output"}:
            target_session: dict[str, object] | None = None
            intent_project = llm_intent.get("project")
            session_index = llm_intent.get("session_index")
            status_only = bool(llm_intent.get("status_only"))

            if isinstance(intent_project, str) and intent_project:
                target_session = await _find_open_or_saved_session_for_project(
                    user_id=user_id,
                    sessions=open_sessions,
                    project=intent_project,
                    provider=None,
                )
            if target_session is None and isinstance(session_index, int):
                target_session = _resolve_session_by_index(open_sessions, session_index)
            if target_session is None and active_session is not None:
                target_session = active_session

            if target_session is None:
                return _maybe_stream(payload, "No encontre una sesion abierta para lo que pediste.")

            if intent_name == "session_output":
                return _maybe_stream(payload, _build_dev_session_output_text(target_session))

            if intent_name == "session_status" or status_only:
                return _maybe_stream(payload, _build_dev_session_progress_text(target_session))

            target_project = str(target_session.get("project") or project)
            await repo.set_active_dev_chat_session(
                user_id=user_id,
                session_id=str(target_session["session_id"]),
                project=target_project,
            )
            await project_service.select_project(user_id=user_id, project=target_project)
            return _maybe_stream(payload, f"Listo, cambie a {target_project}.")

        if intent_name == "close_all_sessions":
            result = await _cancel_user_open_sessions(user_id=user_id, project=None)
            await repo.clear_active_dev_chat_session(user_id)
            return _maybe_stream(payload, result)

        if intent_name == "close_session":
            target_session = None
            intent_project = llm_intent.get("project")
            session_index = llm_intent.get("session_index")
            explicit_selector = False

            if isinstance(intent_project, str) and intent_project:
                explicit_selector = True
                target_session = await _find_open_or_saved_session_for_project(
                    user_id=user_id,
                    sessions=open_sessions,
                    project=intent_project,
                    provider=None,
                )
            if target_session is None and isinstance(session_index, int):
                explicit_selector = True
                target_session = _resolve_session_by_index(open_sessions, session_index)
            if target_session is None:
                if explicit_selector:
                    return _maybe_stream(
                        payload,
                        "No encontre esa sesion abierta. Dilo por numero o proyecto, por ejemplo: elimina la sesion 2.",
                    )
                if active_session is not None:
                    target_session = active_session
                elif len(open_sessions) == 1:
                    target_session = open_sessions[0]
                elif len(open_sessions) > 1:
                    return _maybe_stream(
                        payload,
                        "Tienes varias sesiones abiertas. Dime cual cerrar por numero o proyecto, por ejemplo: elimina la sesion 2.",
                    )
                else:
                    return _maybe_stream(payload, "No encontre una sesion abierta para cerrar.")

            removed = await _cancel_specific_session(
                user_id=user_id,
                session_id=str(target_session["session_id"]),
                reason="Session closed via LLM intent",
            )
            if removed:
                if active_session_id and str(target_session.get("session_id")) == active_session_id:
                    await repo.clear_active_dev_chat_session(user_id)
                return _maybe_stream(payload, f"Listo, cancele {_session_label(target_session)}.")
            return _maybe_stream(payload, f"No pude cancelar {_session_label(target_session)}, intenta de nuevo.")

    # Determine if the user explicitly targets a different project than the active session
    llm_says_instruction = bool(llm_intent and llm_intent.get("intent") == "send_instruction")
    llm_target_project = str(llm_intent.get("project") or "") if llm_intent else ""
    explicit_target = routed_project or ""
    dispatch_message = routed_instruction if routed_project else message
    active_session_project = str(active_session.get("project") or "") if active_session else ""

    # Keep coherence with selected project when there is no explicit reroute.
    if active_session is not None and not explicit_target and active_session_project != project:
        selected_session = _find_open_session_for_project(open_sessions, project, provider=None)
        if selected_session is not None:
            active_session = selected_session
            active_session_project = str(selected_session.get("project") or "")
            await repo.set_active_dev_chat_session(
                user_id=user_id,
                session_id=str(selected_session["session_id"]),
                project=active_session_project,
            )

    if active_session is not None:
        # If user mentions a different project, redirect to that project's session
        if explicit_target and explicit_target != active_session_project:
            redirect_session = await _find_open_or_saved_session_for_project(
                user_id=user_id,
                sessions=open_sessions,
                project=explicit_target,
                provider=None,
            )
            if redirect_session is not None:
                await repo.set_active_dev_chat_session(
                    user_id=user_id,
                    session_id=str(redirect_session["session_id"]),
                    project=str(redirect_session.get("project") or explicit_target),
                )
                return await _send_to_dev_session(payload, user_id, dispatch_message, str(redirect_session["session_id"]))
        return await _send_to_dev_session(payload, user_id, dispatch_message, str(active_session["session_id"]))

    if instruction_like_by_pattern or llm_says_instruction:
        # Prefer explicit routed project; fallback to LLM project; then selected project
        search_project = explicit_target or llm_target_project or project
        implied = await _find_open_or_saved_session_for_project(
            user_id=user_id,
            sessions=open_sessions,
            project=search_project,
            provider=None,
        )
        if implied is not None:
            await repo.set_active_dev_chat_session(
                user_id=user_id,
                session_id=str(implied["session_id"]),
                project=str(implied.get("project") or search_project),
            )
            return await _send_to_dev_session(payload, user_id, dispatch_message, str(implied["session_id"]))

    msg = _build_dev_conversation_fallback(
        message=extract_latest_user_message(payload.messages),
        project=project,
        project_names=project_names,
        open_sessions=open_sessions,
    )
    return _maybe_stream(payload, msg)


async def _handle_openceo_ceo_model(
    payload: ChatCompletionRequest,
    user_id: str,
    message: str,
) -> dict:
    """CEO model routing: classify intent/agent first, then dispatch.

    Important: this path should not force-create dev sessions.
    """
    hint = parse_agent_hint(message)
    hint_text = hint.value if hint else None
    try:
        project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
    except ValueError:
        project = settings.DEFAULT_PROJECT
    project_config = config_loader.load_project(project)
    selected = await router.select_agent(
        title=message.splitlines()[0][:120] if message else "",
        description=message,
        agent_hint=hint_text,
        project_config=project_config,
    )

    if selected == AgentName.DEV and _is_explicit_dev_session_intent(message):
        return await _handle_openceo_dev_direct(payload, user_id, message)
    if selected == AgentName.DEV:
        project_rows = project_service.list_projects()
        text = _build_dev_conversation_fallback(
            message=extract_latest_user_message(payload.messages),
            project=project,
            project_names=[p["name"] for p in project_rows],
            open_sessions=await _list_user_open_sessions(user_id),
        )
        return _maybe_stream(payload, text)
    if selected == AgentName.PM:
        return await _handle_openceo_pm_direct(payload, user_id, _strip_agent_prefix(message))

    if selected == AgentName.FINANCE and _is_finance_text(message):
        # Finanzas por voz/chat: el CEO deriva a Finance y el flujo se resuelve
        # directo en ERPNext, sin ticket por cada movimiento (TASK-2026-00032,
        # TASK-2026-00044, TASK-2026-00053, TASK-2026-00054).
        return await _handle_finance_direct(payload, message)

    task_payload = TaskCreateRequest(
        project=project,
        source=TaskSource.API,
        title=message.splitlines()[0][:120] if message else "Task",
        description=message,
        agent_hint=selected,
        priority=TaskPriority.MEDIUM,
    )
    await task_service.create_task(task_payload)
    return _maybe_stream(payload, _build_task_dispatch_text(selected))


def _is_explicit_dev_session_intent(message: str) -> bool:
    model_command = _normalize_model_command_from_text(message)
    return any(
        (
            _wants_dev_session_start(message),
            _wants_dev_session_stop(message),
            _wants_restore_last_conversation(message),
            _wants_project_listing(message),
            _wants_session_listing(message),
            _wants_dev_session_progress(message),
            _wants_dev_session_output(message),
            _wants_session_focus(message),
            _wants_delete_single_session(message),
            _wants_delete_all_sessions(message),
            _wants_project_selection(message),
            _wants_all_sessions_status(message),
            _wants_project_switch(message),
            _wants_dev_help(message),
            model_command is not None,
        )
    )


async def _direct_agent_session(
    payload: ChatCompletionRequest,
    user_id: str,
    message: str,
    agent: str,
) -> dict:
    """Route message directly to an agent session without CEO classification."""
    if agent == "secretary":
        try:
            project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
        except ValueError:
            project = settings.DEFAULT_PROJECT

        task = {
            "task_id": f"secretary-direct-{uuid4().hex}",
            "project": project,
            "title": message.splitlines()[0][:120] if message else "Secretary task",
            "description": message,
            "attachments": [],
            "priority": TaskPriority.MEDIUM.value,
        }
        result = await secretary_step_manager.run(task)
        if result.last_response_summary:
            return _maybe_stream(payload, result.last_response_summary)
        if result.error:
            return _maybe_stream(payload, result.error)
        if result.steps:
            last_step = result.steps[-1]
            detail = str(last_step.get("detail") or "").strip()
            if detail:
                return _maybe_stream(payload, detail)
        return _maybe_stream(payload, "Secretaría ejecutó la solicitud, pero no devolvió detalle.")

    if agent == "finance":
        try:
            project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
        except ValueError:
            project = settings.DEFAULT_PROJECT

        task = {
            "task_id": f"finance-direct-{uuid4().hex}",
            "project": project,
            "title": message.splitlines()[0][:120] if message else "Finance task",
            "description": message,
            "attachments": [],
            "priority": TaskPriority.MEDIUM.value,
        }
        result = await finance_step_manager.run(task, config_loader.load_project(project))
        if result.last_response_summary:
            return _maybe_stream(payload, result.last_response_summary)
        if result.error:
            return _maybe_stream(payload, result.error)
        if result.steps:
            last_step = result.steps[-1]
            detail = str(last_step.get("detail") or "").strip()
            if detail:
                return _maybe_stream(payload, detail)
        return _maybe_stream(payload, "Finanzas ejecutó la solicitud, pero no devolvió detalle.")

    if agent != "dev":
        # Future agents: marketing, sales, media, etc.
        task_payload = TaskCreateRequest(
            project=payload.project or settings.DEFAULT_PROJECT,
            source=TaskSource.API,
            title=message.splitlines()[0][:120],
            description=message,
            agent_hint=AgentName(agent) if agent in AgentName._value2member_map_ else None,
            priority=TaskPriority.MEDIUM,
        )
        await task_service.create_task(task_payload)
        return _maybe_stream(payload, _build_task_dispatch_text(agent))

    try:
        project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = await repo.find_latest_user_open_session(user_id=user_id, project=project)
    if session is None:
        session = await dev_session_service.create_session(
            DevSessionCreateRequest(
                project=project,
                user_id=user_id,
                mode="peer",
                title=message.splitlines()[0][:100],
                objective="Sesión directa de desarrollo",
            )
        )

    return await _send_to_dev_session(payload, user_id, message, session["session_id"])


async def _send_to_dev_session(
    payload: ChatCompletionRequest,
    user_id: str,
    message: str,
    session_id: str,
    bypass_agent_commands: bool = False,
) -> dict:
    session = await repo.get_dev_session(session_id)
    if session is None:
        await repo.clear_active_dev_chat_session(user_id)
        return _maybe_stream(payload, "La sesion activa ya no existe. Crea o activa otra sesion dev.")

    if not bypass_agent_commands and _wants_dev_session_output(message):
        return _maybe_stream(payload, _build_dev_session_output_text(session))

    if not bypass_agent_commands and _wants_dev_session_progress(message):
        return _maybe_stream(payload, _build_dev_session_progress_text(session))

    if session.get("status") == DevSessionStatus.RUNNING.value:
        return _maybe_stream(payload, _build_dev_session_progress_text(session))

    try:
        updated = await dev_session_service.send_message_async(
            session_id=session_id,
            payload=DevSessionMessageRequest(user_id=user_id, message=message),
        )
    except ValueError as exc:
        error = str(exc)
        lowered = error.lower()
        if "already running" in lowered:
            latest = await repo.get_dev_session(session_id)
            if latest is not None:
                return _maybe_stream(payload, _build_dev_session_progress_text(latest))
            return _maybe_stream(payload, "La sesión activa sigue ejecutando en segundo plano.")
        if any(token in lowered for token in ("no longer exists", "session unavailable", "runtime no longer exists")):
            await repo.clear_active_dev_chat_session(user_id)
            return _maybe_stream(payload, "La sesion activa ya no esta disponible. Crea o activa otra sesion dev.")
        if any(token in lowered for token in ("closed", "not found", "failed", "canceled", "completed")):
            await repo.clear_active_dev_chat_session(user_id)
        raise HTTPException(status_code=400, detail=error) from exc

    if updated.get("status") not in _OPEN_DEV_SESSION_STATUSES:
        await repo.clear_active_dev_chat_session(user_id)

    project_name = str(session.get("project") or "")
    text = (
        f"Instruccion enviada a {project_name}."
        if project_name
        else "Instruccion enviada."
    )
    return _maybe_stream(payload, text)


def _strip_active_session_wrapper(message: str) -> str:
    text = message.strip()
    if not text:
        return message
    for pattern in _ACTIVE_SESSION_WRAPPER_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        stripped = match.group(1).strip()
        if stripped:
            return stripped
    return message


# ------------------------- websockets -------------------------


@app.websocket(settings.WEBSOCKET_PATH)
async def websocket_endpoint(websocket: WebSocket) -> None:
    await realtime_hub.connect(websocket)
    await websocket.send_json({"event": "connected", "payload": {"path": settings.WEBSOCKET_PATH}})

    try:
        async for message in realtime_hub.keepalive_loop(websocket):
            if message.strip().lower() == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        realtime_hub.disconnect(websocket)
    except Exception:
        realtime_hub.disconnect(websocket)


@app.websocket("/ws/peer-programming/{session_id}")
async def peer_programming_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    user_id = websocket.query_params.get("user_id", "default")
    await websocket.send_json({"event": "connected", "session_id": session_id, "user_id": user_id})

    try:
        while True:
            transcript = (await websocket.receive_text()).strip()
            if not transcript:
                continue
            try:
                current = await dev_session_service.get_session(session_id)
                if current is None:
                    await websocket.send_json({"event": "error", "message": "Session not found"})
                    continue

                if _wants_dev_session_progress(transcript):
                    await websocket.send_json(
                        {
                            "event": "status",
                            "session_id": session_id,
                            "message": _build_dev_session_progress_text(current),
                            "status": current.get("status"),
                        }
                    )
                    continue

                if _wants_dev_session_output(transcript):
                    await websocket.send_json(
                        {
                            "event": "assistant",
                            "session_id": session_id,
                            "message": _build_dev_session_output_text(current),
                            "status": current.get("status"),
                        }
                    )
                    continue

                if _wants_restore_last_conversation(transcript):
                    try:
                        _, resume_text = await _resume_session_from_command(
                            user_id=user_id,
                            session=current,
                            fallback_project=str(current.get("project") or ""),
                            reason="Resumed from websocket command",
                            restore_provider_conversation=True,
                        )
                        latest = await dev_session_service.get_session(session_id)
                        await websocket.send_json(
                            {
                                "event": "assistant",
                                "session_id": session_id,
                                "message": resume_text,
                                "status": latest.get("status") if latest else current.get("status"),
                            }
                        )
                    except ValueError as exc:
                        await websocket.send_json({"event": "error", "message": str(exc)})
                    continue

                if current.get("status") == DevSessionStatus.RUNNING.value:
                    await websocket.send_json(
                        {
                            "event": "assistant",
                            "session_id": session_id,
                            "message": _build_dev_session_progress_text(current),
                            "status": current.get("status"),
                        }
                    )
                    continue

                updated = await dev_session_service.send_message_async(
                    session_id,
                    DevSessionMessageRequest(user_id=user_id, message=transcript, channel="voice"),
                )
                await websocket.send_json(
                    {
                        "event": "assistant",
                        "session_id": session_id,
                        "message": (
                            "Instrucción recibida. Ejecutando en segundo plano.\n"
                            "Puedes seguir conversando y preguntar: 'ya terminó?'"
                        ),
                        "status": updated.get("status"),
                    }
                )
            except ValueError as exc:
                await websocket.send_json({"event": "error", "message": str(exc)})
    except WebSocketDisconnect:
        return


_SESSION_START_PATTERNS = [
    re.compile(
        r"\b(crea|crear|inicia|iniciar|abre|abrir|arranca|levanta|lanza|start|create|open)\b.*\b(sesi[oó]n|session)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(sesi[oó]n|session)\b.*\b(crea|crear|inicia|iniciar|abre|abrir|arranca|levanta|lanza|start|create|open)\b",
        flags=re.IGNORECASE,
    ),
]
_SESSION_STOP_PATTERNS = [
    re.compile(r"^\s*(sal|salir|vuelve|volver)\s+(del\s+)?modo\s+dev\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(cierra|cerrar|desactiva|desactivar)\s+(el\s+)?modo\s+dev\??\s*$", flags=re.IGNORECASE),
]
_RESTORE_LAST_CONVERSATION_PATTERNS = [
    re.compile(
        r"^\s*(reestablece|restablece|restaura|recupera|retoma)\s+(la\s+)?[uú]ltima\s+conversaci[oó]n\b.*$",
        flags=re.IGNORECASE,
    ),
]
_PROJECT_LIST_PATTERNS = [
    re.compile(r"^\s*(qu[eé]|que)\s+(proyectos?|projects?)\s+(hay|tengo)\b.*\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(muestra|mu[eé]strame|lista|listar)\s+(los\s+)?(proyectos?|projects?)\b", flags=re.IGNORECASE),
    re.compile(r"^\s*(qu[eé]|que)\s+repos?\s+(hay|tengo)\b.*\??\s*$", flags=re.IGNORECASE),
]
_SESSION_LIST_PATTERNS = [
    re.compile(
        r"^\s*(qu[eé]|que)\s+sesiones?\s+(hay|tengo)?\s*(activas?|abiertas?)?\??\s*$",
        flags=re.IGNORECASE,
    ),
    re.compile(r"^\s*(qu[eé]|que)\s+sesiones?\s+hay\s+activas?\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(lista|listar|muestra|mu[eé]strame)\s+.*\b(sesiones|sessions)\b", flags=re.IGNORECASE),
    re.compile(r"^\s*(en\s+)?qu[eé]\s+proyectos?\s+(est[aá]s|hay)\s+(trabajando|corriendo)\??\s*$", flags=re.IGNORECASE),
]
_SESSION_PROGRESS_PATTERNS = [
    re.compile(r"^\s*(ya\s+)?termin[oó](\s+(la\s+)?sesi[oó]n\s+\d{1,2})?\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(c[oó]mo|como)\s+va(\s+(la\s+)?sesi[oó]n(\s+\d{1,2})?)?\??\s*$", flags=re.IGNORECASE),
]
_SESSION_OUTPUT_PATTERNS = [
    re.compile(r"^\s*(qu[eé]|que)\s+respondi[oó](\s+(la\s+)?sesi[oó]n\s+\d{1,2})?\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(qu[eé]|que)\s+dijo(\s+(la\s+)?sesi[oó]n\s+\d{1,2})?\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(qu[eé]|que)\s+sal[ií]o(\s+(la\s+)?sesi[oó]n\s+\d{1,2})?\??\s*$", flags=re.IGNORECASE),
]
_PROJECT_SELECT_PATTERNS = [
    re.compile(r"\b(selecciona|seleccionar|elige|elegir)\b", flags=re.IGNORECASE),
    re.compile(r"\b(cambia|cambiar)\b.*\b(proyecto|repo|repositorio|project)\b", flags=re.IGNORECASE),
]
_SESSION_FOCUS_PATTERNS = [
    re.compile(
        r"\b(usa|usar|selecciona|seleccionar|enfoca|enfocarse|activa|activar|cambia|cambiar|ingresa|ingresar|entra|entrar|vuelve|volver|retoma|retomar|reanuda|reanudar|resume|restaura|restaurar|continua|continuar)\b.*\b(sesi[oó]n|session)\b",
        flags=re.IGNORECASE,
    ),
]
_DELETE_SINGLE_SESSION_PATTERNS = [
    re.compile(
        r"\b(elimina|eliminar|borra|borrar|cancela|cancelar|termina|terminar|finaliza|finalizar|cierra|cerrar)\b.*\b(sesion(es)?|sesi[oó]n(es)?|sessions?)\b",
        flags=re.IGNORECASE,
    ),
]
_DELETE_ALL_SESSIONS_PATTERNS = [
    re.compile(
        r"\b(elimina|eliminar|borra|borrar|cancela|cancelar|finaliza|finalizar|cierra|cerrar|termina|terminar)\b.*\b(todas|todas las|all|las\s+dos|las\s+\d+|ambas)\b.*\b(sesiones|sessions)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(finaliza|finalizar|cierra|cerrar|termina|terminar)\b.*\b(las\s+dos|las\s+\d+|ambas|todas)\b.*\b(sesiones|sessions)\b",
        flags=re.IGNORECASE,
    ),
]
_SESSION_SWITCH_PATTERNS = [
    re.compile(r"\b(vamos|pasa|cambia|switch)\s+(al?|a la|to)\s+", flags=re.IGNORECASE),
    re.compile(r"\b(como|cómo)\s+(va|anda|esta|está)\s+(el|la|lo de)\s+", flags=re.IGNORECASE),
    re.compile(r"\b(que|qué)\s+tal\s+(el|la|lo de)\s+", flags=re.IGNORECASE),
]
_ALL_SESSIONS_STATUS_PATTERNS = [
    re.compile(r"^\s*(como|cómo)\s+(van|vamos|estamos|andamos)\s*(todas|todo|en general)?\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(estado|resumen)\s+(de\s+)?(todo|todas?|general)\s*\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*resumen\s*\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*panorama\s*\??\s*$", flags=re.IGNORECASE),
]
_SIMPLE_ACTIVE_SESSIONS_PATTERNS = [
    re.compile(r"^\s*hay\s+sesiones?\s+(activas?|abiertas?)\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*tenemos\s+sesiones?\s+(activas?|abiertas?)\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*cu[aá]ntas\s+sesiones?\s+(hay|tengo)?\s*(activas?|abiertas?)\??\s*$", flags=re.IGNORECASE),
]
_RETURN_TO_AGENT_EXACT_PATTERN = re.compile(r"^\s*sal\s+de\s+la\s+sesi[oó]n\b(.*)$", flags=re.IGNORECASE)
_ACTIVE_SESSION_OUTPUT_SHORTCUT_PATTERNS = [
    re.compile(r"^\s*(qu[eé]|que)\s+respondi[oó]\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(qu[eé]|que)\s+dijo\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(qu[eé]|que)\s+sal[ií]o\??\s*$", flags=re.IGNORECASE),
]
_ACTIVE_SESSION_WRAPPER_PATTERNS = [
    re.compile(
        r"^\s*(?:escribe|manda|envia|envía|pregunta|consulta|dile)\s+(?:en|a)\s+(?:la\s+)?sesi[oó]n\s+(?:abierta|activa|actual)\s*[:,\-]?\s*(.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:en|a)\s+(?:la\s+)?sesi[oó]n\s+(?:abierta|activa|actual)\s*[:,\-]?\s*(.+)$",
        flags=re.IGNORECASE,
    ),
]
_DEV_HELP_PATTERNS = [
    re.compile(r"^\s*(ayuda|help)\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(que|qué)\s+(puedes|pod[eé]s)\s+hacer\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(que|qué)\s+funciones?\s+(tienes|hay)\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*(que|qué)\s+comandos?\s+(tienes|hay)\??\s*$", flags=re.IGNORECASE),
]
_MODEL_QUERY_PATTERNS = [
    re.compile(r"^\s*(que|qué)\s+modelo\s+(estas|estás|usas|usa)\??\s*$", flags=re.IGNORECASE),
    re.compile(r"^\s*modelo\s+actual\??\s*$", flags=re.IGNORECASE),
]
_MODEL_SET_PATTERNS = [
    re.compile(
        r"\b(cambia|cambiar|pon|poner|usa|usar|set)\b.*\b(modelo|model)\b(?:\s+a|\s+al|\s*[:=])?\s*([a-zA-Z0-9._:-]{2,64})",
        flags=re.IGNORECASE,
    ),
]
_PROVIDER_CODEX_PATTERN = re.compile(r"\b(codex|gpt)\b", flags=re.IGNORECASE)
_PROVIDER_CLAUDE_PATTERN = re.compile(r"\b(claude|clauded|sonnet|opus)\b", flags=re.IGNORECASE)
_PROVIDER_GLM_PATTERN = re.compile(r"\b(glm)\b", flags=re.IGNORECASE)
_PROVIDER_GEMINI_PATTERN = re.compile(r"\b(gemini|agyd?|antigravity)\b", flags=re.IGNORECASE)
_PROVIDER_OPENCODE_PATTERN = re.compile(r"\b(opencode)\b", flags=re.IGNORECASE)
_DEV_INSTRUCTION_PATTERNS = [
    re.compile(
        r"\b(haz|corrige|arregla|implementa|crea|actualiza|edita|refactoriza|ejecuta|escribe|pregunta|consulta|run|fix|build|test|deploy)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(puedes|podr[ií]as)\s+(hacer|crear|implementar|corregir|arreglar|editar|actualizar|ejecutar|revisar|analizar)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(r"\b(en\s+qu[eé]\s+rama|qu[eé]\s+rama|branch)\b", flags=re.IGNORECASE),
]
_ORDINAL_MAP = {
    "primer": 1,
    "primero": 1, "primera": 1,
    "segundo": 2, "segunda": 2,
    "tercer": 3,
    "tercero": 3, "tercera": 3,
    "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5,
    "sexto": 6, "sexta": 6,
    "septimo": 7, "séptimo": 7, "septima": 7, "séptima": 7,
    "octavo": 8, "octava": 8,
    "noveno": 9, "novena": 9,
    "decimo": 10, "décimo": 10, "decima": 10, "décima": 10,
}
_OPEN_DEV_SESSION_STATUSES = {
    DevSessionStatus.RUNNING.value,
    DevSessionStatus.WAITING_USER.value,
}
_PROJECT_TOKEN_STOPWORDS = {"jardal", "repo", "project", "proyecto", "app", "service", "api"}
_DEV_MUTATION_DEDUPE_DEFAULT_TTL_SECONDS = 6.0
_DEV_MUTATION_DEDUPE_MAX_TTL_SECONDS = 60.0
_RECENT_DEV_MUTATIONS: dict[tuple[str, str], float] = {}
_LLM_DEV_ALLOWED_INTENTS = {
    "help",
    "list_projects",
    "list_sessions",
    "all_sessions_status",
    "switch_session",
    "session_status",
    "session_output",
    "send_instruction",
    "close_session",
    "close_all_sessions",
    "unknown",
}
_VOICE_PLACEHOLDER_PREFIXES = (
    "<frase corta en espanol natural para voz",
    "<respuesta final en espanol natural para voz",
)
_FINAL_MARKED_BLOCK_RE = re.compile(
    r"(?ms)^[^\S\r\n]*[^\w\r\n]*OPENCEO_FINAL_BEGIN::(?P<marker>[^\s]+)[^\w\r\n]*$"
    r"(?P<body>.*?)"
    r"^[^\S\r\n]*[^\w\r\n]*OPENCEO_FINAL_END::(?P=marker)[^\w\r\n]*$"
)


def _wants_dev_session_start(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_START_PATTERNS)


def _wants_dev_session_stop(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if _wants_delete_all_sessions(text) or _wants_delete_single_session(text):
        return False
    return any(pattern.search(text) for pattern in _SESSION_STOP_PATTERNS)


def _wants_restore_last_conversation(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _RESTORE_LAST_CONVERSATION_PATTERNS)


def _wants_dev_session_progress(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_PROGRESS_PATTERNS)


def _wants_dev_session_output(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_OUTPUT_PATTERNS)


def _wants_project_listing(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PROJECT_LIST_PATTERNS)


def _wants_session_listing(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_LIST_PATTERNS)


def _wants_project_selection(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PROJECT_SELECT_PATTERNS)


def _wants_session_focus(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_FOCUS_PATTERNS)


def _wants_delete_single_session(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if _wants_delete_all_sessions(text):
        return False
    return any(pattern.search(text) for pattern in _DELETE_SINGLE_SESSION_PATTERNS)


def _wants_delete_all_sessions(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DELETE_ALL_SESSIONS_PATTERNS)


def _wants_all_sessions_status(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _ALL_SESSIONS_STATUS_PATTERNS)


def _wants_simple_active_sessions_query(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SIMPLE_ACTIVE_SESSIONS_PATTERNS)


def _wants_return_to_agent(message: str) -> bool:
    matched, _ = _consume_return_to_agent_phrase(message)
    return matched


def _consume_return_to_agent_phrase(message: str) -> tuple[bool, str]:
    text = message.strip()
    if not text:
        return False, text

    match = _RETURN_TO_AGENT_EXACT_PATTERN.match(text)
    if not match:
        return False, text

    remainder = str(match.group(1) or "").strip()
    remainder = re.sub(r"^[,.;:!?()\[\]\-]+", "", remainder).strip()
    remainder = re.sub(r"^(?:y|e|luego|then)\b", "", remainder, flags=re.IGNORECASE).strip()
    remainder = re.sub(r"^[,.;:!?()\[\]\-]+", "", remainder).strip()
    return True, remainder


def _wants_active_session_output_shortcut(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _ACTIVE_SESSION_OUTPUT_SHORTCUT_PATTERNS)


def _wants_project_switch(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SESSION_SWITCH_PATTERNS)


def _is_status_question(message: str) -> bool:
    """Check if the switch phrase is a status question ('como va X?') vs action ('vamos al X')."""
    text = message.strip().lower()
    return bool(re.search(r"\b(como|cómo)\s+(va|anda|esta|está)\b", text) or re.search(r"\b(que|qué)\s+tal\b", text))


def _wants_dev_help(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DEV_HELP_PATTERNS)


def _normalize_model_command_from_text(message: str) -> str | None:
    text = message.strip()
    if not text:
        return None

    for pattern in _MODEL_QUERY_PATTERNS:
        if pattern.search(text):
            return "model"

    for pattern in _MODEL_SET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        model = match.group(match.lastindex or 0).strip() if match.lastindex else ""
        if model:
            return f"model {model}"

    return None


def _extract_provider_from_text(text: str) -> DevProvider | None:
    if _PROVIDER_CODEX_PATTERN.search(text):
        return DevProvider.CODEX
    if _PROVIDER_GLM_PATTERN.search(text):
        return DevProvider.GLM
    if _PROVIDER_CLAUDE_PATTERN.search(text):
        return DevProvider.CLAUDE
    if _PROVIDER_GEMINI_PATTERN.search(text):
        return DevProvider.GEMINI
    if _PROVIDER_OPENCODE_PATTERN.search(text):
        return DevProvider.OPENCODE
    return None


def _looks_like_dev_instruction(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if _wants_dev_session_start(text):
        return False
    if _wants_restore_last_conversation(text):
        return False
    if _wants_project_listing(text) or _wants_project_selection(text):
        return False
    if _wants_session_listing(text) or _wants_session_focus(text):
        return False
    if _wants_dev_session_progress(text) or _wants_dev_session_output(text):
        return False
    if _wants_delete_single_session(text) or _wants_delete_all_sessions(text):
        return False
    if _wants_all_sessions_status(text) or _wants_project_switch(text):
        return False
    return any(pattern.search(text) for pattern in _DEV_INSTRUCTION_PATTERNS)


def _extract_project_from_text(text: str, project_names: list[str]) -> str | None:
    lowered = text.lower().strip()
    for name in sorted(project_names, key=len, reverse=True):
        if name.lower() in lowered:
            return name

    best_match: str | None = None
    best_score = 0
    for name in project_names:
        tokens = [tok for tok in re.split(r"[^a-z0-9]+", name.lower()) if tok and len(tok) >= 3]
        tokens = [tok for tok in tokens if tok not in _PROJECT_TOKEN_STOPWORDS]
        if not tokens:
            continue
        score = sum(1 for tok in tokens if re.search(rf"\b{re.escape(tok)}\b", lowered))
        if score > best_score:
            best_score = score
            best_match = name
    if best_match and best_score > 0:
        return best_match

    if not project_names:
        return None

    # 3. Numerical project references: 'proyecto 1', 'proyecto nro 1', 'en el 1', 'sesion en el 2', '#1'
    num_patterns = [
        re.compile(r"\b(?:proyecto|repo|repositorio|project)\s*(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:sesi[oó]n|session)\s+(?:dev\s+)?(?:en|al|para|del?)\s+(?:el\s+)?(?:proyecto\s+)?(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al|para|del?)\s+(?:el\s+)?(?:proyecto\s+)(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al|para|del?)\s+(?:el\s+)(?:n[roº\.]+|n[uú]mero|#)\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al)\s+el\s+(\d{1,2})\b", re.IGNORECASE),
    ]
    for pat in num_patterns:
        m = pat.search(lowered)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= len(project_names):
                return project_names[idx - 1]

    # 4. Ordinal project references: 'primer proyecto', 'el primero', 'segundo', etc.
    for word, index in _ORDINAL_MAP.items():
        if (
            re.search(rf"\b(?:el\s+)?{word}\s+(?:proyecto|repo|repositorio|project)\b", lowered)
            or re.search(rf"\b(?:proyecto|repo|repositorio|project)\s+(?:el\s+)?{word}\b", lowered)
            or re.search(rf"\b(?:en|al|para|del?)\s+(?:el\s+)?{word}\b", lowered)
        ):
            if 1 <= index <= len(project_names):
                return project_names[index - 1]

    return None


def _extract_routed_instruction(message: str, project_names: list[str]) -> tuple[str | None, str]:
    """Extract explicit routing prefix like 'escribe en cebitec ...' from instruction text."""
    text = message.strip()
    if not text:
        return None, message

    for project in sorted(project_names, key=len, reverse=True):
        if not project:
            continue

        escaped = re.escape(project)
        patterns = [
            rf"^\s*(?:escribe|manda|envia|envía|pregunta|consulta|ejecuta|run|dile)\s+(?:en|a)\s+{escaped}\s*[:,\-]?\s*(.+)$",
            rf"^\s*(?:en|a)\s+{escaped}\s*[:,\-]?\s*(.+)$",
        ]
        for raw in patterns:
            match = re.match(raw, text, flags=re.IGNORECASE)
            if not match:
                continue
            stripped = match.group(1).strip()
            if stripped:
                return project, stripped

    return None, message


def _normalize_dedupe_message(message: str) -> str:
    lowered = message.strip().lower()
    lowered = re.sub(r"[^a-z0-9áéíóúñü]+", " ", lowered)
    return " ".join(lowered.split())


def _prune_recent_dev_mutations(now_ts: float) -> None:
    stale = [
        key
        for key, ts in _RECENT_DEV_MUTATIONS.items()
        if now_ts - ts > _DEV_MUTATION_DEDUPE_MAX_TTL_SECONDS
    ]
    for key in stale:
        _RECENT_DEV_MUTATIONS.pop(key, None)


def _dedupe_ttl_for_dev_message(message: str) -> float:
    text = message.strip()
    if not text:
        return _DEV_MUTATION_DEDUPE_DEFAULT_TTL_SECONDS
    if _wants_delete_all_sessions(text) or _wants_delete_single_session(text):
        return 25.0
    if _wants_dev_session_start(text) or _wants_restore_last_conversation(text):
        return 60.0
    if _looks_like_dev_instruction(text):
        return 12.0
    return _DEV_MUTATION_DEDUPE_DEFAULT_TTL_SECONDS


def _is_recent_dev_mutation(user_id: str, message: str) -> bool:
    now_ts = time.time()
    _prune_recent_dev_mutations(now_ts)
    key = (user_id, _normalize_dedupe_message(message))
    ts = _RECENT_DEV_MUTATIONS.get(key)
    if ts is None:
        return False
    ttl = _dedupe_ttl_for_dev_message(message)
    return (now_ts - ts) <= ttl


def _mark_recent_dev_mutation(user_id: str, message: str) -> None:
    now_ts = time.time()
    _prune_recent_dev_mutations(now_ts)
    key = (user_id, _normalize_dedupe_message(message))
    _RECENT_DEV_MUTATIONS[key] = now_ts


def _should_dedupe_dev_mutation(message: str) -> bool:
    text = message.strip()
    if not text:
        return False

    if _wants_dev_session_progress(text) or _wants_dev_session_output(text):
        return False
    if _wants_session_listing(text) or _wants_all_sessions_status(text):
        return False
    if _wants_project_listing(text) or _wants_dev_help(text):
        return False
    if _wants_project_switch(text) or _wants_session_focus(text):
        return False

    if _wants_dev_session_start(text) or _wants_restore_last_conversation(text):
        return True
    if _wants_delete_single_session(text) or _wants_delete_all_sessions(text):
        return True

    model_command = _normalize_model_command_from_text(text)
    if model_command is not None and model_command != "model":
        return True

    return _looks_like_dev_instruction(text)


async def _list_user_open_sessions(user_id: str) -> list[dict[str, object]]:
    sessions = await dev_session_service.list_sessions(user_id=user_id, limit=500)
    open_sessions = [s for s in sessions if s.get("status") in _OPEN_DEV_SESSION_STATUSES]
    return sorted(open_sessions, key=_session_sort_key)


def _session_sort_key(session: dict[str, object]) -> datetime:
    created = session.get("created_at")
    if isinstance(created, datetime):
        return created
    updated = session.get("updated_at")
    if isinstance(updated, datetime):
        return updated
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _find_open_session_for_project(
    sessions: list[dict[str, object]],
    project: str,
    provider: DevProvider | None,
) -> dict[str, object] | None:
    for session in sessions:
        if str(session.get("project") or "") != project:
            continue
        if provider and str(session.get("provider") or "") != provider.value:
            continue
        return session
    return None


async def _find_open_or_saved_session_for_project(
    user_id: str,
    sessions: list[dict[str, object]],
    project: str,
    provider: DevProvider | None = None,
) -> dict[str, object] | None:
    opened = _find_open_session_for_project(sessions, project, provider=provider)
    if opened is not None:
        return opened

    saved_sid = await repo.get_project_session(user_id, project)
    if not saved_sid:
        return None

    candidate = await repo.get_dev_session(saved_sid)
    if candidate and candidate.get("status") in _OPEN_DEV_SESSION_STATUSES:
        if provider and str(candidate.get("provider") or "") != provider.value:
            return None
        return candidate

    await repo.clear_project_session(user_id, project)
    return None


def _resolve_session_from_text(
    message: str,
    sessions: list[dict[str, object]],
    project_names: list[str],
) -> dict[str, object] | None:
    if not sessions:
        return None

    lowered = message.lower()
    if re.search(r"\b(actual|activa|abierta)\b", lowered) and len(sessions) == 1:
        return sessions[0]

    if re.search(r"\b(sesi[oó]n|session)\b", lowered):
        for word, index in _ORDINAL_MAP.items():
            if re.search(rf"\b{word}\b", lowered) and 1 <= index <= len(sessions):
                return sessions[index - 1]

        indexed = re.search(r"\b(?:sesi[oó]n|session|la|el)?\s*(\d{1,2})\b", lowered)
        if indexed:
            index = int(indexed.group(1))
            if 1 <= index <= len(sessions):
                return sessions[index - 1]

    by_project = _extract_project_from_text(message, project_names)
    if by_project:
        selected = _find_open_session_for_project(sessions, by_project, provider=None)
        if selected is not None:
            return selected

    provider = _extract_provider_from_text(message)
    if provider is not None:
        matches = [s for s in sessions if str(s.get("provider") or "") == provider.value]
        if len(matches) == 1:
            return matches[0]

    for word, index in _ORDINAL_MAP.items():
        if re.search(rf"\b{word}\b", lowered) and 1 <= index <= len(sessions):
            return sessions[index - 1]

    indexed = re.search(r"\b(?:sesi[oó]n|session|la|el)?\s*(\d{1,2})\b", lowered)
    if indexed:
        index = int(indexed.group(1))
        if 1 <= index <= len(sessions):
            return sessions[index - 1]

    return None


def _resolve_session_by_index(
    sessions: list[dict[str, object]],
    index: int | None,
) -> dict[str, object] | None:
    if index is None:
        return None
    if index < 1 or index > len(sessions):
        return None
    return sessions[index - 1]


def _has_explicit_session_selector(message: str, project_names: list[str]) -> bool:
    lowered = message.lower()

    if _extract_project_from_text(message, project_names):
        return True

    if _extract_provider_from_text(message) is not None:
        return True

    if re.search(r"\b(sesi[oó]n|session)\b", lowered):
        for word in _ORDINAL_MAP:
            if re.search(rf"\b{word}\b", lowered):
                return True
        if re.search(r"\b(?:sesi[oó]n|session|la|el)?\s*(\d{1,2})\b", lowered):
            return True

    return False


async def _cancel_specific_session(user_id: str, session_id: str, reason: str) -> bool:
    try:
        session = await repo.get_dev_session(session_id)
        await dev_session_service.control_session(
            session_id=session_id,
            payload=DevSessionControlRequest(user_id=user_id, action="cancel", reason=reason),
        )
        # Clean up project_sessions mapping
        if session:
            project = str(session.get("project") or "")
            if project:
                await repo.clear_project_session(user_id, project)
        return True
    except ValueError:
        return False


async def _resume_session_from_command(
    user_id: str,
    session: dict[str, object],
    fallback_project: str,
    reason: str,
    restore_provider_conversation: bool = False,
) -> tuple[dict[str, object], str]:
    session_id = str(session.get("session_id") or "")
    if not session_id:
        raise ValueError("Session not found")

    target_project = str(session.get("project") or fallback_project)
    await dev_session_service.reconcile_open_sessions(
        user_id=user_id,
        project=target_project or None,
    )

    refreshed = await repo.get_dev_session(session_id)
    if refreshed is None:
        raise ValueError("Session not found")
    if refreshed.get("status") not in _OPEN_DEV_SESSION_STATUSES:
        raise ValueError("Esa sesion ya esta cerrada. Usa una abierta o crea una nueva.")

    await repo.set_active_dev_chat_session(
        user_id=user_id,
        session_id=session_id,
        project=target_project,
    )
    await project_service.select_project(user_id=user_id, project=target_project)

    if restore_provider_conversation:
        await dev_session_service.resume_provider_conversation(session_id)
        refreshed = await repo.get_dev_session(session_id)
        if refreshed is None:
            raise ValueError("Session not found")
        return refreshed, f"Sesion activa en {target_project}. Reestableci la ultima conversacion."

    return refreshed, f"Sesion activa en {target_project}. Ya puedes continuar."


async def _cancel_user_open_sessions(user_id: str, project: str | None = None) -> str:
    sessions = await dev_session_service.list_sessions(user_id=user_id, project=project, limit=500)
    open_sessions = [s for s in sessions if s.get("status") in _OPEN_DEV_SESSION_STATUSES]
    if not open_sessions:
        return "No hay sesiones abiertas para cancelar."

    canceled = 0
    failed: list[dict[str, Any]] = []
    for session in open_sessions:
        ok = await _cancel_specific_session(
            user_id=user_id,
            session_id=str(session["session_id"]),
            reason="Canceled from bulk chat command",
        )
        if ok:
            canceled += 1
        else:
            failed.append(session)

    if failed:
        labels = ", ".join(_session_label(s) for s in failed[:3])
        return f"Cancelé {canceled} sesión(es), pero falló la cancelación de {len(failed)} ({labels})."
    return f"Cancelé {canceled} sesión(es) abiertas."


def _build_dev_conversation_fallback(
    message: str,
    project: str,
    project_names: list[str],
    open_sessions: list[dict[str, object]],
) -> str:
    if _wants_project_listing(message):
        rows = [{"name": name} for name in project_names]
        return _build_project_listing_text(rows, project)

    if _wants_session_listing(message):
        return _build_open_sessions_text(open_sessions, active_session_id=None)

    if open_sessions:
        return (
            f"Proyecto actual: {project}. "
            "Puedo seguir conversando o ejecutar una instruccion. "
            "Tambien puedes decir: que sesiones hay activas, crea una sesion dev, o ayuda."
        )

    return (
        f"Proyecto actual: {project}. "
        "No hay sesion dev activa. "
        "Cuando quieras programar, di: crea una sesion dev. Si quieres ver capacidades, di: ayuda."
    )


async def _infer_dev_intent_with_llm(
    message: str,
    project_names: list[str],
    open_sessions: list[dict[str, object]],
    has_active_session: bool,
) -> dict[str, Any] | None:
    llm = getattr(router, "_llm", None)
    if llm is None:
        return None

    compact_sessions = [
        {
            "index": idx + 1,
            "project": str(s.get("project") or ""),
            "provider": str(s.get("provider") or ""),
            "status": str(s.get("status") or ""),
        }
        for idx, s in enumerate(open_sessions[:8])
    ]

    instruction = (
        "Clasifica el mensaje del usuario para el agente dev.\n"
        "No resuelvas la tarea tecnica, solo clasifica.\n"
        "Intents permitidos: help, list_projects, list_sessions, all_sessions_status, switch_session, "
        "session_status, session_output, send_instruction, close_session, close_all_sessions, unknown.\n"
        "Devuelve JSON estricto con claves: intent, project, session_index, status_only.\n"
        "Reglas:\n"
        "- session_status si pide estado/avance/si termino.\n"
        "- session_output si pide la ultima respuesta/salida.\n"
        "- all_sessions_status si pide panorama general de todas.\n"
        "- switch_session si pide cambiar de proyecto/sesion.\n"
        "- send_instruction si parece instruccion tecnica para codex/claude.\n"
        "- close_session si pide cerrar/finalizar/terminar UNA sesion especifica.\n"
        "- close_all_sessions si pide cerrar/finalizar/terminar TODAS o varias sesiones (ej: las dos, ambas, todas).\n"
        "- unknown para charla no tecnica.\n"
        "- project debe ser uno de los proyectos disponibles o null.\n"
        "- session_index es 1-based o null.\n"
        "- status_only=true cuando pide estado de una sesion/proyecto sin cambiar foco."
    )

    context = (
        f"Proyectos disponibles: {project_names}\n"
        f"Sesiones abiertas: {json.dumps(compact_sessions, ensure_ascii=True)}\n"
        f"Hay sesion activa en chat: {has_active_session}\n"
        f"Mensaje del usuario: {message}"
    )

    try:
        response = await llm.ainvoke(
            [
                SystemMessage(content=instruction),
                HumanMessage(content=context),
            ]
        )
    except Exception:
        return None

    parsed = _parse_json_object(str(getattr(response, "content", "") or ""))
    if not isinstance(parsed, dict):
        return None

    intent = str(parsed.get("intent") or "unknown").strip().lower()
    if intent not in _LLM_DEV_ALLOWED_INTENTS:
        return None

    raw_project = str(parsed.get("project") or "").strip()
    project = None
    if raw_project:
        if raw_project in project_names:
            project = raw_project
        else:
            project = _extract_project_from_text(raw_project, project_names)

    session_index: int | None = None
    raw_index = parsed.get("session_index")
    try:
        if raw_index is not None:
            session_index = int(raw_index)
    except (TypeError, ValueError):
        session_index = None

    return {
        "intent": intent,
        "project": project,
        "session_index": session_index,
        "status_only": _coerce_bool(parsed.get("status_only")),
    }


def _build_dev_session_progress_text(session: dict[str, object]) -> str:
    return _build_dev_session_result_text(session)


def _build_dev_session_output_text(session: dict[str, object]) -> str:
    return _build_dev_session_result_text(session)


def _build_dev_session_result_text(session: dict[str, object]) -> str:
    label = _session_label(session)
    status = str(session.get("status") or "unknown")
    last_msg = _best_session_reply(session)
    waiting_reason = str(session.get("waiting_reason") or "").strip()

    if status == DevSessionStatus.RUNNING.value:
        return f"{label} sigue trabajando."

    if last_msg:
        return _voice_friendly(last_msg, max_len=None)

    if status == DevSessionStatus.WAITING_USER.value:
        return f"{label} esta lista para la siguiente instruccion."

    if status == DevSessionStatus.COMPLETED.value:
        return f"{label} ya esta completada."

    if status == DevSessionStatus.FAILED.value:
        detail = _voice_friendly(waiting_reason or "Error no especificado.", max_len=250)
        return f"{label} fallo. Detalle: {detail}"

    if status == DevSessionStatus.CANCELED.value:
        detail = _voice_friendly(waiting_reason or "Cancelada por usuario.", max_len=200)
        return f"{label} fue cancelada. Detalle: {detail}"

    if waiting_reason:
        return f"{label} no dejo salida. Detalle: {_voice_friendly(waiting_reason, 200)}"

    return f"{label} aun no tiene salida registrada."


def _best_session_reply(session: dict[str, object]) -> str | None:
    last_msg = _latest_assistant_message(session)
    if last_msg and not _looks_like_voice_placeholder(last_msg):
        return last_msg

    raw_output = str(session.get("last_output") or "").strip()
    extracted = _extract_latest_marked_reply(raw_output)
    if extracted:
        return extracted

    if last_msg:
        return last_msg
    if raw_output:
        return raw_output
    return None


def _extract_latest_marked_reply(text: str) -> str | None:
    if not text:
        return None

    matches = list(_FINAL_MARKED_BLOCK_RE.finditer(text.replace("\r", "")))
    if not matches:
        return None

    for match in reversed(matches):
        body = _clean_marked_reply_body(match.group("body"))
        if body and not _looks_like_voice_placeholder(body):
            return body
    return None


def _clean_marked_reply_body(body: str) -> str:
    lines = [line.strip() for line in str(body or "").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _looks_like_voice_placeholder(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in _VOICE_PLACEHOLDER_PREFIXES)


def _build_project_listing_text(project_rows: list[dict[str, object]], selected_project: str) -> str:
    if not project_rows:
        return "No hay proyectos configurados."

    lines = ["Proyectos disponibles:"]
    for index, row in enumerate(project_rows, start=1):
        name = str(row.get("name") or f"proyecto-{index}")
        mark = " (seleccionado)" if name == selected_project else ""
        lines.append(f"{index}. {name}{mark}")
    lines.append("Puedes elegir uno o crear sesion diciendo: crea una sesion dev en el proyecto que quieras.")
    return "\n".join(lines)


def _build_open_sessions_text(
    sessions: list[dict[str, object]],
    active_session_id: str | None,
) -> str:
    if not sessions:
        return "No tienes sesiones abiertas."

    lines = ["Sesiones activas:"]
    for index, session in enumerate(sessions[:8], start=1):
        project = str(session.get("project") or "proyecto")
        marker = " (actual)" if active_session_id and str(session.get("session_id")) == active_session_id else ""
        lines.append(f"{index}. {project}{marker}")
    return "\n".join(lines)


def _build_dev_capabilities_text(
    selected_project: str,
    sessions: list[dict[str, object]],
    llm_fallback_enabled: bool,
) -> str:
    lines = [
        f"Proyecto seleccionado: {selected_project}.",
        f"Sesiones abiertas: {len(sessions)}.",
        "Flujos disponibles:",
        "- crear o reutilizar sesion por proyecto y provider (codex, claude, agyd/gemini, opencode, glm)",
        "- ejecutar tickets críticos en modo tier_s cuando el ticket lo pida (coding_tool=tier_s)",
        "- enviar instrucciones en segundo plano sin bloquear chat",
        "- consultar estado, salida, y panorama de todas las sesiones",
        "- cambiar foco entre sesiones por proyecto o por indice",
        "- si una sesion esta enfocada, solo salen localmente: sal de la sesion y que respondio|que dijo|que salio",
        "- usar o entrar a otra sesion por proyecto o por indice",
        "- cancelar una sesion o cancelar todas",
        "- cambiar modelo por sesion con frase natural",
    ]
    if llm_fallback_enabled:
        lines.append("Puedes hablar en frases libres: primero intento patrones y, si no coincide, uso fallback LLM.")
    else:
        lines.append("Puedes hablar natural; si no entiendo, pide ayuda y te doy ejemplos exactos.")
    return "\n".join(lines)


def _voice_friendly(text: str, max_len: int | None = 300) -> str:
    """Strip markdown/backticks, UUIDs, and truncate for TTS output."""
    cleaned = text.replace("\r", "")
    pattern = re.compile(r"([A-Za-zÁÉÍÓÚáéíóúÑñ]{1,16})\n\s*([A-Za-zÁÉÍÓÚáéíóúÑñ]{1,16})")

    def _repair_wrap(match: re.Match[str]) -> str:
        left = match.group(1)
        right = match.group(2)
        if (
            (2 <= len(left) <= 3 and len(right) <= 4)
            or (len(left) >= 5 and len(right) <= 2)
            or (len(left) == 1 and left.lower() not in {"a", "e", "o", "u", "y"} and len(right) <= 4)
        ):
            return f"{left}{right}"
        return f"{left} {right}"

    previous = cleaned
    cleaned = pattern.sub(_repair_wrap, cleaned)
    while cleaned != previous:
        previous = cleaned
        cleaned = pattern.sub(_repair_wrap, previous)
    cleaned = cleaned.replace("`", "").replace("**", "").replace("*", "").replace("#", "")
    cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"^\s*[╭╰│─].*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*Tip:.*$", "", cleaned, flags=re.MULTILINE | re.IGNORECASE)
    cleaned = re.sub(r"^\s*model:.*$", "", cleaned, flags=re.MULTILINE | re.IGNORECASE)
    cleaned = re.sub(r"^\s*directory:.*$", "", cleaned, flags=re.MULTILINE | re.IGNORECASE)
    cleaned = re.sub(r"^\s*gpt-[^\n]*$", "", cleaned, flags=re.MULTILINE | re.IGNORECASE)
    cleaned = re.sub(r"^\s*[›❯>]\s.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*develop@[^\n$]*\$.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r":\s*https?://\S+(?:\s*,\s*https?://\S+)*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"https?://\S+", "", cleaned, flags=re.IGNORECASE)
    # Remove UUIDs
    cleaned = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r":\s*(?=[.,;!?]|$)", "", cleaned)
    normalized_lines: list[str] = []
    current_paragraph: list[str] = []
    for raw_line in cleaned.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", raw_line.strip())
        if not line:
            if current_paragraph:
                normalized_lines.append(" ".join(current_paragraph))
                current_paragraph = []
            if normalized_lines and normalized_lines[-1] != "":
                normalized_lines.append("")
            continue
        if re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)", line):
            if current_paragraph:
                normalized_lines.append(" ".join(current_paragraph))
                current_paragraph = []
            normalized_lines.append(line)
            continue
        current_paragraph.append(line)
    if current_paragraph:
        normalized_lines.append(" ".join(current_paragraph))
    cleaned = "\n".join(normalized_lines).strip()
    if max_len is None:
        return cleaned
    flattened = " ".join(cleaned.split())
    if len(flattened) <= max_len:
        return flattened
    return flattened[: max_len - 3] + "..."


def _finalize_user_facing_text(text: str, sanitize: bool = True) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if not sanitize:
        return raw
    cleaned = _voice_friendly(raw, max_len=None).strip()
    return cleaned or raw


def _session_label(session: dict[str, object]) -> str:
    project = str(session.get("project") or "proyecto")
    provider = _session_runtime_name(session)
    return f"la sesion {provider} en {project}"


def _session_runtime_name(session: dict[str, object]) -> str:
    metadata = session.get("metadata") or {}
    if isinstance(metadata, dict):
        mode = str(metadata.get("execution_mode") or "").strip().lower()
        if mode == "tier_s":
            return "tier_s"
    return str(session.get("provider") or "auto")


def _humanize_session_status(status: str) -> str:
    mapping = {
        DevSessionStatus.RUNNING.value: "trabajando",
        DevSessionStatus.WAITING_USER.value: "lista",
        DevSessionStatus.COMPLETED.value: "completada",
        DevSessionStatus.FAILED.value: "con error",
        DevSessionStatus.CANCELED.value: "cancelada",
    }
    return mapping.get(status, status)


def _compact_status_detail(text: str, max_len: int = 280) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def _build_all_sessions_panorama(sessions: list[dict[str, object]]) -> str:
    return _build_open_sessions_text(sessions, active_session_id=None)


def _natural_join(items: list[str]) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} y {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f" y {cleaned[-1]}"


def _build_dev_session_progress_text_voice(session: dict[str, object]) -> str:
    """Short (~120 char) progress summary for context switching."""
    project = str(session.get("project") or "proyecto")
    status = _humanize_session_status(str(session.get("status") or "unknown"))
    last_msg = _best_session_reply(session) or ""
    if last_msg:
        snippet = _voice_friendly(last_msg, max_len=80)
        return f"{project} esta {status}. {snippet}"
    return f"{project} esta {status}."


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None

    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    candidates: list[str] = []
    if text.startswith("{") and text.endswith("}"):
        candidates.append(text)

    start = text.rfind("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "si", "s"}


async def _handle_model_command(payload: ChatCompletionRequest, user_id: str, message: str) -> dict:
    """Show or change the LLM model used inside clauded/codex sessions."""
    parts = message.split(maxsplit=1)

    try:
        project = await project_service.resolve_project(user_id=user_id, explicit_project=payload.project)
    except ValueError:
        project = settings.DEFAULT_PROJECT

    session = await repo.find_latest_user_open_session(user_id=user_id, project=project)

    if len(parts) == 1:
        # Natural query with no argument -> show current model.
        current = session.get("model_override") if session else None
        if current:
            text = f"Modelo actual de la sesion: {current}."
        else:
            text = (
                "Sin modelo personalizado. Se usa el modelo por defecto del provider. "
                "Para cambiarlo, di por ejemplo: usa el modelo sonnet o cambia el modelo a opus."
            )
        return _maybe_stream(payload, text)

    model_name = parts[1].strip()

    if session is None:
        session = await dev_session_service.create_session(
            DevSessionCreateRequest(
                project=project,
                user_id=user_id,
                mode="peer",
                title=f"Sesión con modelo {model_name}",
                objective="Sesión de desarrollo",
            )
        )

    await repo.patch_dev_session(session["session_id"], {"model_override": model_name})
    await repo.set_active_dev_chat_session(
        user_id=user_id,
        session_id=str(session["session_id"]),
        project=str(session.get("project") or project),
    )
    text = (
        f"Modelo cambiado a {model_name} para {_session_label(session)}. "
        f"Los siguientes turnos usaran --model {model_name}."
    )
    return _maybe_stream(payload, text)


def _maybe_stream(payload: ChatCompletionRequest, text: str, sanitize: bool = True):
    """Return SSE stream if payload.stream=true, otherwise plain JSON."""
    final_text = _finalize_user_facing_text(text, sanitize=sanitize)
    if payload.stream:
        return StreamingResponse(
            _stream_sse(payload.model, final_text),
            media_type="text/event-stream",
        )
    return chat_response(payload.model, final_text).model_dump(mode="json")


async def _stream_sse(model: str, text: str):
    """Yield OpenAI-compatible SSE chunks for a complete text response."""
    chunk_id = f"chatcmpl-{int(time.time())}"
    # Single content chunk with full text
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk)}\n\n"
    # Final chunk with finish_reason
    done_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"

def _latest_assistant_message(session: dict) -> str | None:
    for msg in reversed(session.get("messages", [])):
        if msg.get("role") == "assistant":
            return str(msg.get("content") or "").strip()
    return None


# ------------------------- PM guided interview flow -------------------------

_PM_TOOL_OPTIONS = [
    ("1", "langraph", "Básica (rápida, económica)"),
    ("2", "codex", "Media (Codex CLI)"),
    ("3", "claude", "Máxima potencia (Claude Code)"),
]

_PM_TOOL_REGEX = re.compile(
    r"\b(codex|claude|claude\s*code|deepseek|langraph|langgraph|basica|básica|media|maxima|máxima)\b",
    re.IGNORECASE,
)

_PM_TOOL_WORD_MAP = {
    "codex": "codex",
    "claude": "claude",
    "claude code": "claude",
    "deepseek": "langraph",
    "langraph": "langraph",
    "langgraph": "langraph",
    "basica": "langraph",
    "básica": "langraph",
    "media": "codex",
    "maxima": "claude",
    "máxima": "claude",
}

_PM_CANCEL_RE = re.compile(
    r"^(cancelar?|salir|exit|cancel|volver|regresa)\b",
    re.IGNORECASE,
)
_PM_GUIDED_STEPS = {"initial", "need_project", "need_tool", "need_interview", "confirm"}
_PM_BACKGROUND_STEPS = {"background_running", "background_done", "background_failed"}
_PM_BACKGROUND_PROGRESS_PATTERNS = [
    re.compile(r"^\s*(ya\s+)?termin[oó]\??\s*$", re.IGNORECASE),
    re.compile(r"^\s*(c[oó]mo|como)\s+va\??\s*$", re.IGNORECASE),
    re.compile(r"\b(ya\s+)?termin[oó]\b.*\b(tickets?|entrevista|planificaci[oó]n|requerimiento)\b", re.IGNORECASE),
    re.compile(r"\b(c[oó]mo|como)\s+va\b.*\b(tickets?|entrevista|planificaci[oó]n|requerimiento)\b", re.IGNORECASE),
    re.compile(r"\b(sigue|estado)\b.*\b(tickets?|entrevista|planificaci[oó]n|requerimiento)\b", re.IGNORECASE),
]
_PM_BACKGROUND_OUTPUT_PATTERNS = [
    re.compile(r"\b(qu[eé]|que)\s+tickets?\s+(cre[oó]|gener[oó]|salieron|hay)\b", re.IGNORECASE),
    re.compile(r"\b(qu[eé]|que)\s+extrajo\b", re.IGNORECASE),
    re.compile(r"\b(resultado|resumen)\b.*\b(entrevista|tickets?|planificaci[oó]n)\b", re.IGNORECASE),
]


def _wants_pm_background_progress(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PM_BACKGROUND_PROGRESS_PATTERNS)


def _wants_pm_background_output(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PM_BACKGROUND_OUTPUT_PATTERNS)


def _pm_tool_label(tool: str) -> str:
    return next((label for _, current_tool, label in _PM_TOOL_OPTIONS if current_tool == tool), tool)


def _pm_preview_text(text: str, max_chars: int = 200) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars].rstrip()}..."


def _pm_build_background_ack(project: str, tool: str) -> str:
    return "Listo. Ya mandé a generar los tickets."


def _pm_build_background_progress_text(pm_state: dict[str, Any]) -> str:
    project = str(pm_state.get("project") or "este proyecto")
    tool_label = _pm_tool_label(str(pm_state.get("tool") or "langraph"))
    job = pm_state.get("job") or {}
    status = str(job.get("status") or pm_state.get("step") or "").strip().lower()
    created = int(job.get("tickets_created") or pm_state.get("tickets_created") or 0)
    failed = int(job.get("tickets_failed") or pm_state.get("tickets_failed") or 0)

    if status == "completed":
        parts = [f"La planificación de {project} con {tool_label} ya terminó."]
        if created >= 0:
            parts.append(f"Se crearon {created} ticket(s).")
        if failed:
            parts.append(f"{failed} quedaron con error.")
        return " ".join(parts)

    if status == "failed":
        detail = _voice_friendly(
            str(pm_state.get("error") or job.get("error") or "No pude completar la extracción."),
            max_len=220,
        )
        return f"La planificación de {project} con {tool_label} terminó con error. {detail}"

    return f"La planificación de {project} con {tool_label} sigue trabajando en segundo plano."


def _pm_build_background_output_text(pm_state: dict[str, Any]) -> str:
    job = pm_state.get("job") or {}
    status = str(job.get("status") or pm_state.get("step") or "").strip().lower()
    if status != "completed":
        return _pm_build_background_progress_text(pm_state)

    project = str(pm_state.get("project") or "este proyecto")
    tool_label = _pm_tool_label(str(pm_state.get("tool") or "langraph"))
    created = int(job.get("tickets_created") or pm_state.get("tickets_created") or 0)
    tickets = list(pm_state.get("ticket_summaries") or [])
    lines = [f"La planificación de {project} con {tool_label} ya terminó. Se crearon {created} ticket(s)."]
    if tickets:
        lines.append("Tickets principales:")
        for idx, ticket in enumerate(tickets[:5], 1):
            title = _voice_friendly(str(ticket.get("title") or "Sin título"), max_len=120)
            lines.append(f"{idx}. {title}")
        if len(tickets) > 5:
            lines.append(f"... y {len(tickets) - 5} más.")
    else:
        summary = str(pm_state.get("summary") or "").strip()
        if summary:
            lines.append(_voice_friendly(summary, max_len=260))
    return "\n".join(lines)


def _pm_looks_like_new_request(message: str, project_names: list[str]) -> bool:
    raw = _strip_agent_prefix(str(message or "")).strip()
    if not raw or _PM_CANCEL_RE.match(raw):
        return False
    normalized = " ".join(raw.lower().split())
    if normalized in {"1", "2", "si", "sí", "ok", "okay", "vale", "listo", "confirma", "confirmar"}:
        return False
    if _pm_detect_project_from_text(raw, project_names):
        return True
    if _pm_detect_tool_from_text(raw):
        return True
    if _pm_extract_interview_candidate(raw, project_names):
        return True
    return bool(
        re.search(
            r"\b(entrevista|requerimiento|requerimientos|brief|tickets?|planifica|planificar|extrae|extraer)\b",
            raw,
            flags=re.IGNORECASE,
        )
    )


def _should_route_to_pm_state(pm_state: dict[str, Any], message: str) -> bool:
    step = str(pm_state.get("step") or "initial").strip().lower()
    if step in _PM_GUIDED_STEPS:
        return True
    if step in _PM_BACKGROUND_STEPS:
        return _wants_pm_background_progress(message) or _wants_pm_background_output(message)
    return False


def _pm_detect_tool_from_text(text: str) -> str | None:
    match = _PM_TOOL_REGEX.search(text)
    if match:
        return _PM_TOOL_WORD_MAP.get(match.group(1).lower().strip())
    return None


def _pm_detect_project_from_text(text: str, project_names: list[str]) -> str | None:
    lowered = text.lower()
    for name in project_names:
        if name.lower() in lowered:
            return name
    return None


_AUTORUN_KEYWORDS = re.compile(
    r"\b(autorun|ejecutar|programar|correr|schedule|run\s*now|no\s*esperar)\b",
    re.IGNORECASE,
)


def _pm_detect_autorun_from_text(text: str) -> bool:
    return bool(_AUTORUN_KEYWORDS.search(text))


def _pm_extract_number_choice(text: str, max_n: int) -> int | None:
    stripped = text.strip()
    if stripped.isdigit():
        n = int(stripped)
        if 1 <= n <= max_n:
            return n
    return None


def _pm_build_tool_menu() -> str:
    lines = ["¿Qué herramienta quieres usar para planificar?\n"]
    for num, _, label in _PM_TOOL_OPTIONS:
        lines.append(f"  {num}. {label}")
    lines.append("\nResponde con el número.")
    return "\n".join(lines)


def _pm_build_project_menu(project_names: list[str]) -> str:
    lines = ["¿De qué proyecto es esta entrevista?\n"]
    for i, name in enumerate(project_names, 1):
        lines.append(f"  {i}. {name}")
    lines.append("\nResponde con el número.")
    return "\n".join(lines)


def _pm_build_confirmation(project: str, tool: str, interview_preview: str) -> str:
    tool_label = next((label for num, t, label in _PM_TOOL_OPTIONS if t == tool), tool)
    preview = interview_preview[:200].replace("\n", " ")
    if len(interview_preview) > 200:
        preview += "..."
    return (
        f"Confirma antes de procesar:\n\n"
        f"  Proyecto: **{project}**\n"
        f"  Herramienta: **{tool_label}**\n"
        f"  Entrevista: _{preview}_\n\n"
        f"  1. Confirmar\n"
        f"  2. Cancelar\n\n"
        f"Responde con el número."
    )


def _pm_build_interview_prompt(project: str, tool: str) -> str:
    tool_label = next((label for num, t, label in _PM_TOOL_OPTIONS if t == tool), tool)
    return (
        f"Perfecto. Ya registré el proyecto {project} y la herramienta {tool_label}. "
        "Ahora envíame solo la entrevista o el requerimiento en un solo mensaje."
    )


def _pm_extract_interview_candidate(text: str, project_names: list[str]) -> str | None:
    raw = _strip_agent_prefix(str(text or "")).strip()
    if not raw:
        return None
    if _PM_CANCEL_RE.match(raw):
        return None

    normalized = " ".join(raw.lower().split())
    if normalized in {"si", "sí", "ok", "okay", "vale", "listo", "confirma", "confirmar"}:
        return None
    if raw.isdigit():
        return None

    candidate = normalized
    for project in sorted(project_names, key=len, reverse=True):
        project_name = str(project or "").strip().lower()
        if project_name:
            candidate = re.sub(rf"\b{re.escape(project_name)}\b", " ", candidate, flags=re.IGNORECASE)
    candidate = _PM_TOOL_REGEX.sub(" ", candidate)
    candidate = re.sub(
        r"\b(pm|tool|herramienta|proyecto|cliente|entrevista|requerimiento|requerimientos|brief|documento|tickets?|"
        r"tengo|quiero|quisiera|necesito|paso|pasar|manda|mandar|mando|envia|enviar|envío|procesa|procesar|"
        r"planifica|planificar|gestiona|gestionar|crea|crear|genera|generar|extrae|extraer|usa|usar|"
        r"con|para|de|del|el|la|los|las|un|una|mi|tu|este|esta|esto|es|hay|por|favor)\b",
        " ",
        candidate,
        flags=re.IGNORECASE,
    )
    content_words = [
        word
        for word in re.findall(r"[a-záéíóúñ0-9][a-záéíóúñ0-9_-]*", candidate, flags=re.IGNORECASE)
        if len(word) > 1
    ]
    has_structure = any(token in raw for token in ("\n", ":", ";", "."))

    if len(content_words) >= 2:
        return raw
    if len(content_words) == 1 and (has_structure or len(raw) >= 40):
        return raw
    if len(raw) >= 120:
        return raw
    return None


def _pm_absorb_message(
    pm_state: dict[str, Any],
    message: str,
    project_names: list[str],
    active_step: str,
) -> dict[str, Any]:
    updated = dict(pm_state)

    if not updated.get("project"):
        choice = _pm_extract_number_choice(message, len(project_names)) if active_step == "need_project" else None
        if choice is not None:
            updated["project"] = project_names[choice - 1]
        else:
            detected_project = _pm_detect_project_from_text(message, project_names)
            if detected_project:
                updated["project"] = detected_project

    if not updated.get("tool"):
        choice = _pm_extract_number_choice(message, len(_PM_TOOL_OPTIONS)) if active_step == "need_tool" else None
        if choice is not None:
            updated["tool"] = _PM_TOOL_OPTIONS[choice - 1][1]
        else:
            detected_tool = _pm_detect_tool_from_text(message)
            if detected_tool:
                updated["tool"] = detected_tool

    if not updated.get("interview"):
        interview = _pm_extract_interview_candidate(message, project_names)
        if interview:
            updated["interview"] = interview

    if _pm_detect_autorun_from_text(message):
        updated["autorun"] = True

    return updated


def _pm_next_step(pm_state: dict[str, Any], project_names: list[str]) -> tuple[dict[str, Any], str]:
    updated = dict(pm_state)

    if not updated.get("project") and len(project_names) == 1:
        updated["project"] = project_names[0]

    if not updated.get("project"):
        return updated, "need_project"
    if not updated.get("tool"):
        return updated, "need_tool"
    if not updated.get("interview"):
        return updated, "need_interview"
    return updated, "confirm"


async def _pm_start_background_job(
    user_id: str,
    project: str,
    tool: str,
    interview: str,
    autorun: bool = False,
) -> str:
    job_id = f"pm-job-{uuid4().hex[:12]}"
    pm_state = {
        "step": "background_running",
        "project": project,
        "tool": tool,
        "autorun": autorun,
        "interview_preview": _pm_preview_text(interview, max_chars=240),
        "job": {
            "id": job_id,
            "status": "running",
            "project": project,
            "tool": tool,
            "started_at": datetime.now(timezone.utc),
        },
    }
    await repo.set_pm_state(user_id, pm_state)
    task = asyncio.create_task(
        _pm_run_background_job(
            user_id=user_id,
            job_id=job_id,
            project=project,
            tool=tool,
            interview=interview,
            autorun=autorun,
        ),
        name=job_id,
    )
    _PM_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_PM_BACKGROUND_TASKS.discard)
    await realtime_hub.broadcast(
        "pm_job_updated",
        {
            "user_id": user_id,
            "job_id": job_id,
            "status": "running",
            "project": project,
            "tool": tool,
        },
    )
    return _pm_build_background_ack(project, tool)


async def _pm_run_background_job(
    user_id: str,
    job_id: str,
    project: str,
    tool: str,
    interview: str,
    autorun: bool = False,
) -> None:
    try:
        if tool == "langraph":
            result = await pm_step_manager.extract_tickets(
                interview_text=interview,
                project=project,
                autorun=autorun,
            )
        else:
            result = await pm_step_manager.extract_tickets_subprocess(
                interview_text=interview,
                project=project,
                tool=tool,
                autorun=autorun,
            )
    except Exception as exc:
        logger.exception("PM background job failed unexpectedly: %s", job_id)
        result = None
        error = f"Error inesperado: {exc}"
    else:
        error = result.error if result is not None else "Error no especificado."

    current_state = await repo.get_pm_state(user_id) or {}
    current_job = current_state.get("job") or {}
    if current_job.get("id") != job_id:
        return

    updated = dict(current_state)
    updated["project"] = project
    updated["tool"] = tool
    updated["interview_preview"] = updated.get("interview_preview") or _pm_preview_text(interview, max_chars=240)
    updated.pop("interview", None)

    job_state = dict(current_job)
    job_state["completed_at"] = datetime.now(timezone.utc)

    if result is not None and result.status == TaskStatus.COMPLETED:
        updated["step"] = "background_done"
        updated["summary"] = result.summary
        updated["tickets_created"] = result.tickets_created
        updated["tickets_failed"] = result.tickets_failed
        updated["ticket_summaries"] = list(result.ticket_summaries[:20])
        updated.pop("error", None)
        job_state["status"] = "completed"
        job_state["tickets_created"] = result.tickets_created
        job_state["tickets_failed"] = result.tickets_failed
    else:
        updated["step"] = "background_failed"
        updated["error"] = error or "No pude completar la extracción."
        updated["summary"] = error or "No pude completar la extracción."
        updated["tickets_created"] = 0
        updated["tickets_failed"] = 0
        updated["ticket_summaries"] = []
        job_state["status"] = "failed"
        job_state["error"] = updated["error"]

    updated["job"] = job_state
    await repo.set_pm_state(user_id, updated)
    await realtime_hub.broadcast(
        "pm_job_updated",
        {
            "user_id": user_id,
            "job_id": job_id,
            "status": job_state["status"],
            "project": project,
            "tool": tool,
            "tickets_created": updated.get("tickets_created", 0),
            "tickets_failed": updated.get("tickets_failed", 0),
            "error": updated.get("error"),
        },
    )


async def _handle_openceo_pm_direct(
    payload: ChatCompletionRequest,
    user_id: str,
    message: str,
) -> dict:
    """Guided PM flow: collect project + tool + interview in any order, then extract tickets."""
    project_rows = project_service.list_projects()
    project_names = [p["name"] for p in project_rows]

    # Load existing state
    pm_state = await repo.get_pm_state(user_id) or {}
    step = pm_state.get("step", "initial")

    if step in _PM_BACKGROUND_STEPS:
        if step in {"background_done", "background_failed"} and _PM_CANCEL_RE.match(message.strip()):
            await repo.clear_pm_state(user_id)
            return _maybe_stream(payload, "Listo. Limpié el último resultado de PM.")
        if _wants_pm_background_output(message):
            return _maybe_stream(payload, _pm_build_background_output_text(pm_state))
        if _wants_pm_background_progress(message):
            return _maybe_stream(payload, _pm_build_background_progress_text(pm_state))
        if step in {"background_done", "background_failed"} and _pm_looks_like_new_request(message, project_names):
            pm_state = {}
            step = "initial"
        else:
            return _maybe_stream(payload, _pm_build_background_progress_text(pm_state))

    # Check for cancel
    if _PM_CANCEL_RE.match(message.strip()):
        await repo.clear_pm_state(user_id)
        return _maybe_stream(payload, "Flujo PM cancelado.")

    if step == "confirm":
        choice = _pm_extract_number_choice(message, 2)
        if choice == 2 or _PM_CANCEL_RE.match(message.strip()):
            await repo.clear_pm_state(user_id)
            return _maybe_stream(payload, "Cancelado. Puedes enviar otra entrevista cuando quieras.")

        if choice != 1 and "confirma" not in message.lower() and "si" not in message.lower() and "sí" not in message.lower():
            return _maybe_stream(payload, "Responde 1 para confirmar o 2 para cancelar.")

        project = pm_state["project"]
        tool = pm_state.get("tool", "langraph")
        interview = pm_state.get("interview", "")
        autorun = bool(pm_state.get("autorun", False))
        ack = await _pm_start_background_job(
            user_id=user_id,
            project=project,
            tool=tool,
            interview=interview,
            autorun=autorun,
        )
        return _maybe_stream(payload, ack)

    pm_state = _pm_absorb_message(pm_state, message, project_names, step)
    pm_state, next_step = _pm_next_step(pm_state, project_names)
    pm_state["step"] = next_step
    await repo.set_pm_state(user_id, pm_state)

    if next_step == "need_project":
        return _maybe_stream(payload, _pm_build_project_menu(project_names))
    if next_step == "need_tool":
        return _maybe_stream(payload, _pm_build_tool_menu())
    if next_step == "need_interview":
        return _maybe_stream(payload, _pm_build_interview_prompt(pm_state["project"], pm_state["tool"]))
    if next_step == "confirm":
        return _maybe_stream(
            payload,
            _pm_build_confirmation(pm_state["project"], pm_state["tool"], pm_state.get("interview", "")),
        )

    # Unknown step — reset
    await repo.clear_pm_state(user_id)
    return _maybe_stream(payload, "Sesión PM reiniciada. Envía tu entrevista para comenzar.")


async def _pm_execute_langraph(
    payload: ChatCompletionRequest,
    project: str,
    interview: str,
    autorun: bool = False,
) -> dict:
    """DeepSeek extracts tickets and creates MongoDB tasks (autorun=False by default)."""
    result = await pm_step_manager.extract_tickets(
        interview_text=interview,
        project=project,
        autorun=autorun,
    )
    return _maybe_stream(payload, result.summary)


async def _pm_execute_subprocess(
    payload: ChatCompletionRequest,
    user_id: str,
    project: str,
    tool: str,
    interview: str,
    autorun: bool = False,
) -> dict:
    """Run Claude/Codex in headless subprocess to extract tickets, create MongoDB tasks."""
    result = await pm_step_manager.extract_tickets_subprocess(
        interview_text=interview,
        project=project,
        tool=tool,
        autorun=autorun,
    )
    if result.status == TaskStatus.COMPLETED:
        created_text = (
            f"Se registraron {result.tickets_created} ticket(s) en MongoDB."
            if result.tickets_created >= 0
            else "Los tickets fueron registrados."
        )
        return _maybe_stream(
            payload,
            f"Extracción con {tool} completada en {project}. {created_text}",
        )
    return _maybe_stream(payload, result.summary)


# ------------------------- PM API endpoint for dashboard -------------------------


@app.post("/api/pm/extract")
async def pm_extract_tickets(payload: dict[str, Any]) -> dict:
    """Direct API for dashboard form. No guided flow needed — all params explicit."""
    project = str(payload.get("project") or "").strip()
    if not project:
        raise HTTPException(status_code=400, detail="project is required")

    interview = str(payload.get("interview") or payload.get("description") or "").strip()
    if not interview:
        raise HTTPException(status_code=400, detail="interview text is required")

    tool = str(payload.get("tool") or "langraph").strip().lower()
    if tool not in {"langraph", "claude", "codex"}:
        raise HTTPException(status_code=400, detail="tool must be langraph, claude, or codex")

    autorun = bool(payload.get("autorun", False))

    if not config_loader.has_project(project):
        raise HTTPException(status_code=400, detail=f"project '{project}' not found")

    if tool == "langraph":
        result = await pm_step_manager.extract_tickets(
            interview_text=interview,
            project=project,
            autorun=autorun,
        )
        return {
            "status": result.status.value,
            "tickets_created": result.tickets_created,
            "tickets_failed": result.tickets_failed,
            "ticket_summaries": result.ticket_summaries,
            "task_ids": result.task_ids,
            "error": result.error,
            "summary": result.summary,
        }

    result = await pm_step_manager.extract_tickets_subprocess(
        interview_text=interview,
        project=project,
        tool=tool,
        autorun=autorun,
    )

    return {
        "status": result.status.value,
        "tool": tool,
        "project": project,
        "tickets_created": result.tickets_created,
        "tickets_failed": result.tickets_failed,
        "ticket_summaries": result.ticket_summaries,
        "task_ids": result.task_ids,
        "error": result.error,
        "summary": result.summary if result.status == TaskStatus.COMPLETED else result.error,
    }


@app.get("/api/pm/status")
async def pm_status(user_id: str = Query(default="openwebui")) -> dict:
    pm_state = await repo.get_pm_state(user_id)
    if not pm_state:
        return {"user_id": user_id, "status": "idle"}

    job = pm_state.get("job") or {}
    return {
        "user_id": user_id,
        "step": pm_state.get("step"),
        "status": job.get("status") or pm_state.get("step"),
        "project": pm_state.get("project"),
        "tool": pm_state.get("tool"),
        "interview_preview": pm_state.get("interview_preview"),
        "tickets_created": pm_state.get("tickets_created", 0),
        "tickets_failed": pm_state.get("tickets_failed", 0),
        "ticket_summaries": pm_state.get("ticket_summaries", []),
        "error": pm_state.get("error"),
        "summary": pm_state.get("summary"),
        "job": job,
    }


# ------------------------- Projects API -------------------------


@app.get("/api/projects")
async def list_projects_api(
    current_user: UserDocument = Depends(get_current_user),
) -> list[dict]:
    """List all projects from YAML config plus Mongo overrides."""
    db = get_auth_db()
    db_by_name = {str(r["name"]): r for r in db.projects.find({}).sort("name", 1)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in project_service.list_projects():
        name = str(project.get("name") or "")
        db_row = db_by_name.get(name, {})
        rows.append(
            {
                **project,
                "display_name": db_row.get("display_name") or project.get("title") or name,
                "description": db_row.get("description", project.get("description", "")),
                "workflow_templates": merge_workflow_templates(db_row.get("workflow_templates")),
            }
        )
        seen.add(name)
    for name, db_row in db_by_name.items():
        if name in seen:
            continue
        rows.append(
            {
                "name": name,
                "title": db_row.get("display_name") or name,
                "display_name": db_row.get("display_name") or name,
                "description": db_row.get("description", ""),
                "repo_count": 0,
                "default_provider": "auto",
                "workflow_templates": merge_workflow_templates(db_row.get("workflow_templates")),
            }
        )
    if current_user.is_admin:
        return rows
    allowed = set(current_user.allowed_projects or [])
    return [r for r in rows if r["name"] in allowed]


@app.post("/api/projects")
async def create_project_api(
    body: dict[str, Any],
    current_user: UserDocument = Depends(require_permissions("manage_projects")),
) -> dict:
    """Create a new project."""
    db = get_auth_db()
    name = str(body.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Project name required")
    if db.projects.find_one({"name": name}):
        raise HTTPException(status_code=400, detail="Project already exists")
    doc = ProjectDocument(
        name=name,
        display_name=body.get("display_name", name),
        description=body.get("description", ""),
        workflow_templates=body.get("workflow_templates") if isinstance(body.get("workflow_templates"), dict) else {},
    )
    db.projects.insert_one(doc.model_dump())
    return {"name": name, "display_name": doc.display_name, "workflow_templates": merge_workflow_templates(doc.workflow_templates)}


@app.patch("/api/projects/{project_name}")
async def update_project_api(
    project_name: str,
    body: dict[str, Any],
    current_user: UserDocument = Depends(require_permissions("manage_projects")),
) -> dict:
    """Update a project."""
    db = get_auth_db()
    updates: dict[str, Any] = {}
    if "display_name" in body:
        updates["display_name"] = body["display_name"]
    if "description" in body:
        updates["description"] = body["description"]
    if isinstance(body.get("workflow_templates"), dict):
        updates["workflow_templates"] = body["workflow_templates"]
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        db.projects.update_one(
            {"name": project_name},
            {
                "$set": updates,
                "$setOnInsert": {
                    "project_id": f"prj_{uuid4().hex[:12]}",
                    "name": project_name,
                    "created_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
    row = db.projects.find_one({"name": project_name})
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "name": row["name"],
        "display_name": row.get("display_name", row["name"]),
        "title": row.get("display_name", row["name"]),
        "description": row.get("description", ""),
        "workflow_templates": merge_workflow_templates(row.get("workflow_templates")),
    }


@app.delete("/api/projects/{project_name}")
async def delete_project_api(
    project_name: str,
    current_user: UserDocument = Depends(require_permissions("manage_projects")),
) -> dict:
    """Delete a project."""
    db = get_auth_db()
    result = db.projects.delete_one({"name": project_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}


# ------------------------- Users & Groups API -------------------------


@app.get("/api/users")
async def list_users(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, Any]:
    db = get_auth_db()
    skip = (page - 1) * limit
    users = list(db.users.find({}, {"password_hash": 0}).skip(skip).limit(limit))
    total = db.users.count_documents({})
    # Remove _id from each user
    for u in users:
        u.pop("_id", None)
    return {"data": users, "total": total, "page": page, "limit": limit}


@app.get("/api/users/names")
async def list_user_names(
    current_user: UserDocument = Depends(get_current_user),
) -> dict[str, str]:
    db = get_auth_db()
    users = db.users.find({}, {"user_id": 1, "username": 1, "display_name": 1, "_id": 0})
    return {u["user_id"]: u["username"] for u in users}


@app.post("/api/users")
async def create_user(
    request: Request,
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, Any]:
    db = get_auth_db()
    body = await request.json()
    username = str(body.get("username") or "").strip()
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "").strip()
    if not username or not email or not password:
        raise HTTPException(status_code=400, detail="username, email and password required")
    if db.users.find_one({"username": username}):
        raise HTTPException(status_code=400, detail="Username ya existe")
    if db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email ya existe")

    group_ids = body.get("group_ids") or []
    groups = get_groups_for_user(group_ids, db)
    temp_user = UserDocument(username=username, email=email, group_ids=group_ids)
    user = UserDocument(
        username=username,
        email=email,
        password_hash=hash_password(password),
        display_name=body.get("display_name") or username,
        is_admin=bool(body.get("is_admin")),
        is_client=bool(body.get("is_client")),
        group_ids=group_ids,
        allowed_projects=body.get("allowed_projects") or [],
        all_permissions=compute_user_permissions(temp_user, groups),
    )
    db.users.insert_one(user.model_dump())
    user_dict = user.model_dump()
    user_dict.pop("password_hash", None)
    return user_dict


@app.get("/api/users/{user_id}")
async def get_user(
    user_id: str,
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, Any]:
    db = get_auth_db()
    user = db.users.find_one({"user_id": user_id}, {"password_hash": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    user.pop("_id", None)
    return user


@app.patch("/api/users/{user_id}")
async def update_user(
    user_id: str,
    request: Request,
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, Any]:
    db = get_auth_db()
    body = await request.json()
    allowed = ["display_name", "email", "is_active", "group_ids", "is_admin", "is_client", "allowed_projects", "password"]
    updates = {k: v for k, v in body.items() if k in allowed}

    if "password" in updates:
        password = updates.pop("password", "")
        if password:
            updates["password_hash"] = hash_password(password)

    if "group_ids" in updates:
        groups = get_groups_for_user(updates["group_ids"], db)
        temp_user = UserDocument(
            username="temp",
            email="temp@temp.com",
            group_ids=updates["group_ids"],
        )
        updates["all_permissions"] = compute_user_permissions(temp_user, groups)

    updates["updated_at"] = datetime.now(timezone.utc)
    db.users.update_one({"user_id": user_id}, {"$set": updates})
    user = db.users.find_one({"user_id": user_id}, {"password_hash": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    user.pop("_id", None)
    return user


@app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: str,
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, bool]:
    db = get_auth_db()
    db.users.update_one(
        {"user_id": user_id},
        {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"ok": True}


@app.get("/api/groups")
async def list_groups(
    current_user: UserDocument = Depends(require_permissions("manage_groups")),
) -> list[dict[str, Any]]:
    db = get_auth_db()
    groups = list(db.groups.find({}))
    for g in groups:
        g.pop("_id", None)
    return groups


@app.post("/api/groups")
async def create_group(
    request: Request,
    current_user: UserDocument = Depends(require_permissions("manage_groups")),
) -> dict[str, Any]:
    db = get_auth_db()
    body = await request.json()
    if db.groups.find_one({"name": body.get("name")}):
        raise HTTPException(status_code=400, detail="Nombre de grupo ya existe")
    body.setdefault("display_name", body.get("name", ""))
    group = GroupDocument(**body)
    db.groups.insert_one(group.model_dump())
    return group.model_dump()


@app.get("/api/groups/{group_id}")
async def get_group(
    group_id: str,
    current_user: UserDocument = Depends(require_permissions("manage_groups")),
) -> dict[str, Any]:
    db = get_auth_db()
    group = db.groups.find_one({"group_id": group_id})
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")
    group.pop("_id", None)
    return group


@app.patch("/api/groups/{group_id}")
async def update_group(
    group_id: str,
    request: Request,
    current_user: UserDocument = Depends(require_permissions("manage_groups")),
) -> dict[str, Any]:
    db = get_auth_db()
    body = await request.json()
    allowed = ["name", "display_name", "description", "permissions", "is_default"]
    updates = {k: v for k, v in body.items() if k in allowed}
    updates["updated_at"] = datetime.now(timezone.utc)
    db.groups.update_one({"group_id": group_id}, {"$set": updates})

    # Recompute permissions for all users in this group
    users_in_group = list(db.users.find({"group_ids": group_id}))
    for u in users_in_group:
        user_doc = UserDocument(**u)
        groups = get_groups_for_user(user_doc.group_ids, db)
        new_perms = compute_user_permissions(user_doc, groups)
        db.users.update_one({"user_id": user_doc.user_id}, {"$set": {"all_permissions": new_perms}})

    group = db.groups.find_one({"group_id": group_id})
    if group:
        group.pop("_id", None)
    return group


@app.delete("/api/groups/{group_id}")
async def delete_group(
    group_id: str,
    current_user: UserDocument = Depends(require_permissions("manage_groups")),
) -> dict[str, Any]:
    db = get_auth_db()
    # Only delete if no users assigned
    count = db.users.count_documents({"group_ids": group_id})
    if count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Hay {count} usuarios en este grupo. Reasignalos primero.",
        )
    db.groups.delete_one({"group_id": group_id})
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.OPENCEO_HOST,
        port=settings.OPENCEO_PORT,
        reload=False,
    )
