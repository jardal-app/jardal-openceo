from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


CORE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(CORE_DIR / ".env", CORE_DIR / ".env.finance.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API authentication (Bearer token for /v1/ endpoints)
    OPENCEO_API_KEY: str = ""

    # Machine Bearer exclusive to POST /api/ceo/wake (D5, ticket #580).
    # Out-of-process writers present it to poke the schedule kernel.
    # Empty disables the route fail-closed; one value per environment,
    # revocable by rotation, never logged or sent to browsers.
    OPENCEO_CEO_WAKE_BEARER: str = ""

    # CEO routing LLM
    DEEPSEEK_API_KEY: str = ""
    CEO_LLM_MODEL: str = "deepseek-flash"
    CEO_LLM_BASE_URL: str = "https://api.deepseek.com/v1"

    # Dev coding providers
    CODEX_CLI_PATH: str = "/home/develop/.npm-global/bin/codex"
    CODEX_ACCOUNT_CLI_PATH: str = "/home/develop/.local/bin/codexf"
    CLAUDE_CODE_PATH: str = "clauded"
    GEMINI_CLI_PATH: str = "/home/develop/.local/bin/agyd"
    OPENCODE_CLI_PATH: str = "/home/develop/.opencode/bin/opencode"
    GLM_CLI_PATH: str = "clauded"
    MUSE_CLI_PATH: str = "/home/develop/.local/bin/musej"

    MUSE_DEFAULT_ARGS: str = "--model muse-spark-1.3-contributor --reasoning-effort max --yolo"
    CODEX_DEFAULT_ARGS: str = "--dangerously-bypass-approvals-and-sandbox -m gpt-5.4 -c model_reasoning_effort=xhigh"
    CLAUDE_DEFAULT_ARGS: str = "--dangerously-skip-permissions --model opus --effort high"
    GEMINI_DEFAULT_ARGS: str = ""
    OPENCODE_DEFAULT_ARGS: str = "-m opencode-go/kimi-k2.6"
    OPENCODE_GO_API_KEY: str = ""
    OPENCODE_MODEL_OVERRIDE: str = ""
    OPENCODE_GO_MODEL_GLM5: str = "opencode-go/glm-5"
    OPENCODE_GO_MODEL_GLM51: str = "opencode-go/glm-5.1"
    OPENCODE_GO_MODEL_KIMI_K25: str = "opencode-go/kimi-k2.5"
    OPENCODE_GO_MODEL_KIMI_K26: str = "opencode-go/kimi-k2.6"
    OPENCODE_GO_MODEL_MINIMAX_M25: str = "opencode-go/minimax-m2.5"
    OPENCODE_GO_MODEL_MINIMAX_M27: str = "opencode-go/minimax-m2.7"
    OPENCODE_GO_MODEL_MIMO_V25_PRO: str = "opencode-go/mimo-v2.5-pro"
    OPENCODE_GO_MODEL_QWEN37_MAX: str = "opencode-go/qwen3.7-max"
    OPENCODE_GO_MODEL_DEEPSEEK_V4_PRO: str = "opencode-go/deepseek-v4-pro"
    GLM_DEFAULT_ARGS: str = "--dangerously-skip-permissions --model glm-4.5"
    KIMI_CLI_PATH: str = "kimi"
    KIMI_DEFAULT_ARGS: str = ""
    OPENCEO_WEB_BASE_URL: str = "https://opencode.jardal.app"

    DEV_EXECUTION_MODE: str = "cli"  # cli | mock
    DEV_TURN_TIMEOUT_SECONDS: int = 1800
    DEV_IDLE_TIMEOUT_SECONDS: int = 45
    DEV_MAX_CONTEXT_MESSAGES: int = 60

    # tmux runtime
    TMUX_POLL_INTERVAL_MS: int = 300
    TMUX_TURN_TIMEOUT_SECONDS: int = 600
    TMUX_STABILITY_CHECKS: int = 3

    # MongoDB
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DATABASE: str = "openceo"

    # Integrations
    N8N_URL: str = ""
    N8N_API_KEY: str = ""
    OPENWEBUI_URL: str = ""
    NOTION_API_TOKEN: str = ""
    NOTION_API_BASE_URL: str = "https://api.notion.com/v1"
    NOTION_API_VERSION: str = "2026-03-11"
    NOTION_TICKETS_PARENT_KIND: str = "data_source"  # data_source | database | page
    NOTION_TICKETS_PARENT_ID: str = ""
    NOTION_TICKETS_DATA_SOURCE_NAME: str = ""
    NOTION_TICKETS_TITLE_PROPERTY: str = ""
    NOTION_TICKETS_PROPERTY_MAP_JSON: str = ""
    NOTION_TICKETS_DEFAULT_STATUS: str = ""
    NOTION_TICKETS_DEFAULT_PRIORITY: str = ""
    NOTION_TICKETS_DEFAULT_SOURCE: str = "OpenCEO"
    NOTION_TICKETS_TEMPLATE_ID: str = ""
    NOTION_TICKETS_USE_DEFAULT_TEMPLATE: bool = False
    NOTION_TICKETS_APPEND_DESCRIPTION_TO_BODY: bool = True
    NOTION_MIRROR_ENABLED: bool = True
    NOTION_SYNC_BACK_ENABLED: bool = True
    NOTION_SYNC_BACK_PROPERTY_MAP_JSON: str = ""

    # Jardal ERP — adapter del agente Finance y de tickets.
    # Secretos en openceo-core/.env.finance.local (git-ignored). Todos los campos
    # llevan default: un campo sin default rompe Settings() y con ella toda la suite.
    #
    # NO hay flag de encendido/apagado y es deliberado: Jardal ERP es el registro de negocio
    # de los tickets, asi que un modo que los cree solo en Mongo produciria tickets que el
    # sistema de negocio nunca vera, y la reconciliacion no los recupera. La resiliencia
    # ante un Jardal ERP caido la da la marca `jardal_erp_pending`, no un interruptor.
    # Solo "jardal_erp" (adapter con manifiesto). El valor "erpnext" se retiro
    # en TASK-2026-00267: `build_business_work` lo rechaza.
    BUSINESS_PROVIDER: str = "jardal_erp"
    BUSINESS_AUTHORITY_FILE: str = ""
    JARDAL_ERP_URL: str = ""
    JARDAL_ERP_SERVICE_TOKEN: str = ""
    JARDAL_ERP_WORKSPACE_ID: str = ""
    # Directorio humano: "local" (Mongo, defecto) o "jardal_erp" (federado).
    # El cambio exige config ERP completa + manifiesto de autoridad listo (266);
    # nunca deja APIs sin autenticación ni convierte ERP-admin en acceso host.
    IDENTITY_PROVIDER: str = "local"
    # Eventos ERP (00262): secreto del webhook entrante, usuario OpenCEO para
    # adopción por asignación (orden 00156) y autoridad de rollup ("core"
    # hasta verificar el cálculo ERP; "erp" solo tras ese corte).
    JARDAL_ERP_WEBHOOK_SECRET: str = ""
    JARDAL_ERP_OPENCEO_USER: str = "openceo@jardal.app"
    ROLLUP_PROVIDER: str = "core"
    # Finanzas (00263): "erpnext" == adapter Frappe (DocTypes, defecto) o
    # "jardal_erp" (contrato financiero, exige manifiesto de autoridad listo
    # como identidad/eventos). 00267 conserva el defecto hasta verificar el
    # contrato financiero contra el ERP vivo.
    FINANCE_PROVIDER: str = "erpnext"
    # Horizon personal (00264): "mongo" (defecto) o "jardal_erp" (dominio
    # privado ERP para objetivos/capturas; exige manifiesto + identidad
    # federada, pues cada llamada usa la sesión del usuario).
    HORIZON_STORE: str = "mongo"

    # Credenciales del adapter Frappe legado (solo finance: FINANCE_PROVIDER).
    # 00267 las conserva; todo lo demas usa JARDAL_ERP_*.
    ERPNEXT_URL: str = ""
    ERPNEXT_USER: str = ""
    ERPNEXT_API_KEY: str = ""
    ERPNEXT_API_SECRET: str = ""
    JARDAL_ERP_TIMEOUT_SECONDS: float = 5.0
    JARDAL_ERP_CONNECT_TIMEOUT_SECONDS: float = 2.0
    JARDAL_ERP_TICKETS_PROJECT_COMPANY: str = "Jardal"
    # Zona IANA del sitio Jardal ERP (`exp_*` son naive en hora local). Defecto
    # verificado en vivo (00147): UTC-4 fijo en toda la ventana observada.
    JARDAL_ERP_SITE_TIMEZONE: str = "America/Caracas"
    # Adjuntos de tickets (TASK-2026-00079). Dos limites, uno por lado:
    # los bytes que aceptamos subir y las raices locales de las que se puede leer un
    # archivo. La raiz es el "alcance autorizado" del ticket: fuera de ella, se rechaza.
    JARDAL_ERP_ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024
    JARDAL_ERP_ATTACHMENT_ALLOWED_ROOTS: str = "/home/develop/development/Jardal"

    # Shared infrastructure tools
    CLOUDFLARE_API_TOKEN: str = ""
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_DNS_DEFAULT_TARGET: str = "91.99.9.106"
    CLOUDFLARE_DNS_DEFAULT_PROXIED: bool = False
    CLOUDFLARE_DNS_DEFAULT_TTL: int = 1

    # Cloudflare R2 Storage
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_ENDPOINT_URL: str = ""
    R2_BUCKET_NAME: str = ""
    R2_PUBLIC_BASE_URL: str = ""

    # Shared email tool
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USE_SSL: bool = True
    SMTP_USE_STARTTLS: bool = False
    SMTP_SSL_VERIFY: bool = False
    SMTP_TIMEOUT_SECONDS: int = 30
    SMTP_DEFAULT_SENDER: str = ""
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_ACCOUNTS_JSON: str = ""
    SMTP_IDENTITIES_PATH: str = str(CORE_DIR / "config" / "email_identities.toml")
    SMTP_SIGNATURE_COMPANY: str = "Jardal - Bolivia"
    SMTP_SIGNATURE_LOGO_PATH: str = str(CORE_DIR.parent / "Isotipo.png")
    SMTP_SIGNATURE_LOGO_WIDTH: int = 104

    # Finance browser automation
    FINANCE_BROWSER_HEADLESS: bool = True
    FINANCE_BROWSER_SLOW_MO_MS: int = 0
    FINANCE_ARTIFACTS_DIR: str = str(CORE_DIR.parent / ".runtime" / "finance")
    ELFEC_LOGIN_URL: str = "https://ecommerce.elfec.com/login"
    ELFEC_PAYMENT_URL: str = "https://ecommerce.elfec.com/pagos/add/"
    ELFEC_TIMEOUT_MS: int = 45000
    ELFEC_HOUSE_USERNAME: str = ""
    ELFEC_HOUSE_PASSWORD: str = ""
    ELFEC_CARD_NUMBER: str = ""
    ELFEC_CARD_EXPIRY: str = ""

    # CORS
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://localhost:18765,https://dev.openceo.jardal.app,https://openceo.jardal.app,https://api.openceo.jardal.app"

    # Server
    OPENCEO_HOST: str = "0.0.0.0"
    OPENCEO_PORT: int = 8000
    WEBSOCKET_PATH: str = "/ws"
    MCP_SERVER_ENABLED: bool = True
    MCP_SERVER_PATH: str = "/internal/mcp"
    MCP_SERVER_TRANSPORT: str = "streamable-http"
    MCP_SERVER_INTERNAL_HOST: str = "127.0.0.1"
    MCP_SERVER_ALLOWED_IPS: str = "127.0.0.1/32,::1/128"
    MCP_SERVER_API_KEY: str = ""

    # Rate limit management
    RATE_MAX_CONCURRENT_TICKETS: int = 7
    RATE_CODEX_RESERVE_5H: float = 0.0
    RATE_CODEX_RESERVE_WEEKLY: float = 0.0
    RATE_CLAUDE_RESERVE_5H: float = 0.0
    RATE_CLAUDE_RESERVE_WEEKLY: float = 0.0
    RATE_MUSE_RESERVE_5H: float = 0.0
    RATE_MUSE_RESERVE_WEEKLY: float = 0.0
    RATE_TIER_S_WEIGHT: int = 3
    RATE_NORMAL_WEIGHT: int = 1
    RATE_STATUS_STALE_SECONDS: int = 300  # 5 min

    # JWT Authentication
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24 horas
    JWT_REFRESH_EXPIRE_DAYS: int = 7
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "admin1234"

    # Internal runtime behavior
    TASK_AUTORUN: bool = True
    # Objective children dispatch (design §9.3, default true = children
    # enqueue themselves). Set false to hold a pilot's planned tickets
    # queued (autorun=false) so conversion can be verified first; release
    # them later via POST /api/tasks/{task_id}/run.
    TIER_S_OBJECTIVE_CHILD_AUTORUN: bool = True
    # F3 dispatcher (TASK-2026-00113): events + one conditional backup
    # trigger. No polling: WAITING_POLL_SECONDS is retired.
    DISPATCHER_BACKUP_SECONDS: int = 300
    DISPATCHER_MAX_ACTIVE_PER_PROJECT: int = 1
    DISPATCHER_MAX_ACTIVE_PER_OBJECTIVE: int = 6
    # F4 CEO loop (TASK-2026-00119): reactive reviews debounce. Each
    # reactive review costs a headless run, so wakes inside the window
    # accumulate reasons for the next review instead of storming.
    CEO_REACTIVE_MIN_INTERVAL_SECONDS: int = 3600
    PROJECTS_DIR: str = "projects"
    # F6 (TASK-2026-00115): roots for end-to-end project creation. Repos live
    # as siblings of the OpenCEO checkout; the vault lives in jardal-knowledge.
    PROJECT_REPOS_ROOT: str = str(CORE_DIR.parent.parent)
    VAULT_ROOT: str = str(CORE_DIR.parent.parent / "jardal-knowledge" / "vault")
    DEFAULT_PROJECT: str = "example-project"
    WORKFLOWS_DIR: str = str(CORE_DIR.parent / "workflows")

    # Horizon: capacidad personal del CEO.
    # El espejo externo de datos personales nace deshabilitado y la fraccion de
    # capacidad es un techo, no un objetivo: el codigo la recorta igualmente.
    HORIZON_ENABLED: bool = True
    HORIZON_TIMEZONE: str = "America/La_Paz"
    HORIZON_CAPACITY_FRACTION: float = 0.70
    HORIZON_PERSONAL_MIRROR_ENABLED: bool = False
    HORIZON_SOURCE_PATH: str = ""

    # Propietario unico del espacio personal. La voz y el chat no traen
    # identidad autenticada (el cliente de chat manda su propio nombre y la app
    # movil no manda ninguno), asi que sin un dueño configurado una captura por
    # voz se guardaria bajo una identidad que el dashboard nunca consulta y el
    # espacio personal pareceria vacio sin que nada fallara.
    HORIZON_OWNER_USER_ID: str = "usr_8c9941ae0bc9"

    # Horarios locales (America/La_Paz) del ciclo proactivo.
    HORIZON_DAILY_PLAN_TIME: str = "06:00"
    HORIZON_CLOSE_DAY_TIME: str = "21:00"
    HORIZON_WEEKLY_REVIEW_TIME: str = "18:00"

    # Reintentos acotados de los disparadores (seccion 9.2 punto 4).
    HORIZON_TRIGGER_MAX_ATTEMPTS: int = 3
    HORIZON_TRIGGER_BACKOFF_SECONDS: int = 60

    # --- Research (investigador compartido, TASK-2026-00056 Entrega A) ---
    # Todos los defaults son seguros: el servicio nace apagado y **ningun** adaptador
    # paga por defecto. La ausencia de claves no rompe `Settings()` ni el arranque;
    # produce degradacion estructurada (missing_config), no un fallback implicito.
    #
    # El gasto pagado por defecto es 0 y no hay compras/recargas automaticas. El saldo
    # de una cuenta que no se conoce es `null` (cadena vacia), nunca un credito gratis
    # confirmado. Los importes se declaran como cadena decimal en USD porque
    # pydantic-settings no debe convertir dinero a float en la frontera de entorno.
    RESEARCH_ENABLED: bool = False

    # Rutas de estado. El runtime no monta el vault ni los homes personales; estas
    # carpetas son el workspace/almacen propios del servicio.
    RESEARCH_STATE_DIR: str = str(CORE_DIR.parent / ".runtime" / "research")
    RESEARCH_EVIDENCE_DIR: str = str(CORE_DIR.parent / ".runtime" / "research" / "evidence")
    RESEARCH_WORKER_DIR: str = str(CORE_DIR.parent / ".runtime" / "research" / "worker")
    RESEARCH_WORKSPACE_DIR: str = str(CORE_DIR.parent / ".runtime" / "research" / "workspaces")

    # Broker MCP del worker. El worker no recibe claves de buscadores: habla **solo**
    # con este endpoint, que aplica egress, politica y ledger. Sin URL configurada el
    # runtime se niega a lanzar el worker en vez de ejecutarlo sin adquisicion.
    RESEARCH_BROKER_URL: str = ""
    RESEARCH_BROKER_BEARER: str = ""
    # Montura del broker dentro de la propia aplicacion. Apagada por defecto, igual
    # que la fachada MCP de consumidores: abrir un endpoint es una decision de
    # despliegue. Al activarla sin URL explicita, el servicio sirve el broker en esta
    # ruta y el runtime del worker la usa por loopback.
    RESEARCH_BROKER_ENABLED: bool = False
    RESEARCH_BROKER_PATH: str = "/internal/mcp-research-broker"
    # Redes desde las que se acepta al worker del broker. Por defecto solo loopback; un
    # worker en contenedor llega desde la subred de su red dedicada y hay que declararla.
    RESEARCH_BROKER_ALLOWED_IPS: str = "127.0.0.1/32,::1/128"

    # Aislamiento del worker. `container` es el unico backend que puede filtrar el egress
    # por destino: su red dedicada deja al broker como unico servicio interno y abre solo
    # los hosts del modelo. Vacio = deteccion automatica (bwrap preferido).
    RESEARCH_ISOLATION_BACKEND: str = ""
    RESEARCH_WORKER_IMAGE: str = "openceo-research-worker:latest"
    RESEARCH_WORKER_NETWORK: str = "openceo-research-egress"
    # Gateway de esa red (p. ej. 172.31.240.1). Sin el no hay filtro aplicable y el plan
    # lo declara (`egress_restricted=False`) en vez de fingirlo.
    RESEARCH_WORKER_NETWORK_GATEWAY: str = ""
    # Hosts del modelo a los que el worker puede salir (lista blanca, separada por comas).
    RESEARCH_WORKER_EGRESS_HOSTS: str = "opencode.ai"
    # UID:GID con el que corre el CLI dentro del contenedor (el usuario del host), para
    # que el estado del job no quede con dueño root.
    RESEARCH_WORKER_RUN_AS: str = ""

    # Concurrencia global y por recurso (techos iniciales, ajustables tras medir).
    RESEARCH_MAX_ACTIVE_JOBS: int = 2
    RESEARCH_MAX_ACTIVE_MODEL_CALLS: int = 2
    RESEARCH_MAX_ACTIVE_BROWSERS: int = 1
    RESEARCH_ACCOUNT_CONCURRENCY: int = 1

    # Limites iniciales por modo (seccion 9 del documento del epico).
    RESEARCH_QUICK_MAX_SEARCHES: int = 3
    RESEARCH_QUICK_MAX_PAGES: int = 5
    RESEARCH_QUICK_MAX_STEPS: int = 8
    RESEARCH_QUICK_MAX_SECONDS: int = 120
    RESEARCH_DEEP_MAX_SEARCHES: int = 15
    RESEARCH_DEEP_MAX_PAGES: int = 25
    RESEARCH_DEEP_MAX_STEPS: int = 30
    RESEARCH_DEEP_MAX_SECONDS: int = 900

    # Retencion propuesta, subordinada a la politica de cada fuente (seccion 11.2).
    RESEARCH_PUBLIC_RETENTION_DAYS: int = 7
    RESEARCH_WATCH_RETENTION_DAYS: int = 90
    RESEARCH_REPORT_RETENTION_DAYS: int = 180
    # Una reserva viva sin uso registrado se conserva (conservadora) hasta que la
    # reconciliacion pueda dictaminar; este plazo solo separa lo "recien iniciado".
    RESEARCH_ORPHAN_RESERVATION_GRACE_SECONDS: int = 300

    # Politica economica por defecto.
    RESEARCH_PAID_SPEND_USD: str = "0.00"
    # Optional X session state, outside repository/model workspace/evidence/vault.
    RESEARCH_SOCIAL_SESSION_DIR: str = ""
    # Deep uses direct, controlled HTTP phases. Explicit opt-in; no CEO key reuse.
    RESEARCH_DEEP_API_KEY: str = ""
    RESEARCH_DEEP_MODEL: str = ""
    RESEARCH_DEEP_INPUT_USD_PER_MILLION: str = ""
    RESEARCH_DEEP_OUTPUT_USD_PER_MILLION: str = ""
    RESEARCH_DEEP_ACCOUNT_CEILING_USD: str = "0.00"
    RESEARCH_AUTO_TOPUPS: bool = False

    # Brave: proveedor habitual. El credito mensual publicado es una referencia
    # fechada, no saldo medido; su ausencia se representa con cadena vacia.
    RESEARCH_BRAVE_ENABLED: bool = True
    RESEARCH_BRAVE_API_KEY: str = ""
    RESEARCH_BRAVE_ACCOUNT_CEILING_USD: str = "5.00"
    RESEARCH_BRAVE_MONTHLY_CREDIT_IS_KNOWN: bool = True
    RESEARCH_BRAVE_PAID_OVERAGE: bool = False

    # Serper: complemento mientras tenga saldo. La cuota inicial publicada no se
    # presume renovable ni comprable por consulta suelta.
    RESEARCH_SERPER_ENABLED: bool = True
    RESEARCH_SERPER_API_KEY: str = ""
    RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES: str = ""
    RESEARCH_SERPER_PAID_PURCHASE: bool = False

    # Firecrawl: adaptador **opcional**. 600/200/200 son objetivos configurables
    # sobre su saldo real, no tres cuotas independientes.
    RESEARCH_FIRECRAWL_ENABLED: bool = True
    RESEARCH_FIRECRAWL_API_KEY: str = ""
    RESEARCH_FIRECRAWL_ACCOUNT_CREDITS: str = ""
    RESEARCH_FIRECRAWL_CREDIT_WINDOW: str = ""
    RESEARCH_FIRECRAWL_PLAN: str = "free"
    RESEARCH_FIRECRAWL_PAID_TOPUPS: bool = False
    RESEARCH_FIRECRAWL_CREDIT_TARGET_EXTRACTION: int = 600
    RESEARCH_FIRECRAWL_CREDIT_TARGET_DISCOVERY: int = 200
    RESEARCH_FIRECRAWL_CREDIT_TARGET_RESERVE: int = 200

    # Grounding de Meta (Muse): capacidad pagada, apagada por defecto.
    RESEARCH_MUSE_GROUNDING_ENABLED: bool = False
    RESEARCH_MUSE_GROUNDING_MONTHLY_BUDGET_USD: str = "0.00"
    RESEARCH_META_API_KEY: str = ""
    RESEARCH_META_BASE_URL: str = "https://api.meta.ai/v1"
    RESEARCH_META_MODEL: str = "muse-spark-1.3-contributor"

    # Runtime OpenCode del investigador. La ruta de prueba del gateway opencode y la
    # ruta de produccion Meta son credenciales y modelos distintos: no se presume
    # equivalencia entre ellas.
    RESEARCH_OPENCODE_CLI_PATH: str = str(CORE_DIR.parent / ".opencode" / "bin" / "opencode")
    RESEARCH_OPENCODE_VERSION_PIN: str = "2.0.20"
    RESEARCH_OPENCODE_TEST_MODEL: str = "opencode/muse-spark-1.3-contributor-free"
    RESEARCH_OPENCODE_PROD_MODEL: str = "meta/muse-spark-1.3-contributor"
    RESEARCH_OPENCODE_TIMEOUT_SECONDS: int = 300

    # Router de modelos gratuitos del investigador (TASK-2026-00294). Apagado
    # hasta el piloto de validacion (TASK-2026-00301); encenderlo con snapshot
    # free-research valido activa el ranking, sin snapshot usa el estatico.
    RESEARCH_FREE_ROUTER_ENABLED: bool = False
    RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS: float = 30.0

    # Fachada MCP de research. Nace **apagada**: montarla expone un endpoint nuevo cuyo
    # control de acceso son las credenciales de consumidor (no el Bearer compartido de
    # las monturas internas, que protege el transporte pero no identifica al owner).
    # Activarla es una decision explicita de despliegue.
    RESEARCH_MCP_ENABLED: bool = False
    RESEARCH_MCP_PATH: str = "/internal/mcp-research"

    # Scheduler persistido de research (TASK-2026-00068). El timer **no** sondea: sin
    # vencimiento espera indefinidamente (`RESEARCH_SCHEDULER_MAX_SLEEP_SECONDS = 0`
    # significa sin tope, que es el defecto correcto). Fijar un tope solo se justifica
    # como red de seguridad explicita, nunca para reproducir el barrido que las reglas
    # permanentes de AGENTS.md prohiben.
    RESEARCH_SCHEDULER_ENABLED: bool = False
    RESEARCH_SCHEDULER_LEASE_SECONDS: int = 120
    RESEARCH_SCHEDULER_MAX_ATTEMPTS: int = 3
    RESEARCH_SCHEDULER_BACKOFF_SECONDS: int = 60
    RESEARCH_SCHEDULER_STALE_RUNNING_SECONDS: int = 900
    RESEARCH_SCHEDULER_BATCH_LIMIT: int = 20
    RESEARCH_SCHEDULER_DEADLINE_SECONDS: int = 300
    # 0 o negativo = sin tope (espera indefinida).
    RESEARCH_SCHEDULER_MAX_SLEEP_SECONDS: int = 0

    # Retencion del **documento** de un job terminal (TASK-2026-00069): TTL de campo
    # que solo escribe la transicion a un estado terminal. 0 = no caduca. La dedup de
    # ocurrencias no depende de este documento.
    RESEARCH_JOB_RETENTION_DAYS: int = 180

    # Suscripciones y vigilancia (TASK-2026-00069). Suscribirse no gasta: estos topes
    # acotan cuanto trabajo puede programar un owner antes de que la politica del
    # proyecto y el libro de gasto decidan cada llamada.
    RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER: int = 10
    RESEARCH_SUBSCRIPTIONS_MIN_INTERVAL_SECONDS: int = 900
    # Items por lote que un tick admite como job (alineado con las paginas de quick) y
    # paginas de una fuente que un tick lee antes de declarar cobertura incompleta.
    RESEARCH_WATCH_MAX_ITEMS_PER_TICK: int = 5
    RESEARCH_WATCH_MAX_PAGES_PER_TICK: int = 3
    RESEARCH_WATCH_MAX_ITEMS_PER_SOURCE: int = 200
    # Descubrimiento: cadencia y busquedas por dia **independientes** de las de
    # investigacion, para que una suscripcion habladora no consuma el saldo comun.
    RESEARCH_DISCOVERY_MIN_INTERVAL_SECONDS: int = 21600
    RESEARCH_DISCOVERY_DAILY_SEARCH_CAP: int = 2


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
