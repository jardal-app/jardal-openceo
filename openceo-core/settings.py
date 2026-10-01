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

    # CEO routing LLM
    DEEPSEEK_API_KEY: str = ""
    CEO_LLM_MODEL: str = "deepseek-v4-flash"
    CEO_LLM_BASE_URL: str = "https://api.deepseek.com/v1"

    # Dev coding providers
    CODEX_CLI_PATH: str = "/home/develop/.npm-global/bin/codex"
    CLAUDE_CODE_PATH: str = "clauded"
    GEMINI_CLI_PATH: str = "/home/develop/.local/bin/agyd"
    OPENCODE_CLI_PATH: str = "/home/develop/.opencode/bin/opencode"
    GLM_CLI_PATH: str = "clauded"

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
    DEV_CONTEXT_MAX_CHARS: int = 20000

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

    # ERPNext — adapter del agente Finance y de tickets.
    # Secretos en openceo-core/.env.finance.local (git-ignored). Todos los campos
    # llevan default: un campo sin default rompe Settings() y con ella toda la suite.
    #
    # NO hay flag de encendido/apagado y es deliberado: ERPNext es el registro de negocio
    # de los tickets, asi que un modo que los cree solo en Mongo produciria tickets que el
    # sistema de negocio nunca vera, y la reconciliacion no los recupera. La resiliencia
    # ante un ERPNext caido la da la marca `erpnext_pending`, no un interruptor.
    ERPNEXT_URL: str = ""
    ERPNEXT_USER: str = ""
    ERPNEXT_API_KEY: str = ""
    ERPNEXT_API_SECRET: str = ""
    ERPNEXT_TIMEOUT_SECONDS: float = 5.0
    ERPNEXT_CONNECT_TIMEOUT_SECONDS: float = 2.0
    ERPNEXT_TICKETS_PROJECT_COMPANY: str = "Jardal"
    # Secreto compartido que ERPNext debe enviar en sus webhooks de vuelta (camino inverso).
    ERPNEXT_WEBHOOK_SECRET: str = ""

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
    MCP_GLOBAL_PATH: str = "/internal/mcp-global"
    MCP_SERVER_TRANSPORT: str = "streamable-http"
    MCP_SERVER_INTERNAL_HOST: str = "127.0.0.1"
    MCP_SERVER_ALLOWED_IPS: str = "127.0.0.1/32,::1/128"
    MCP_SERVER_API_KEY: str = ""

    # Rate limit management
    RATE_MAX_CONCURRENT_TICKETS: int = 3
    RATE_CODEX_RESERVE_5H: float = 0.0
    RATE_CODEX_RESERVE_WEEKLY: float = 0.0
    RATE_CLAUDE_RESERVE_5H: float = 0.0
    RATE_CLAUDE_RESERVE_WEEKLY: float = 0.0
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
    WAITING_POLL_SECONDS: int = 60
    PROJECTS_DIR: str = "projects"
    PROJECT_CONTEXTS_DIR: str = str(CORE_DIR.parent / ".runtime" / "project-context")
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

    # Fachada MCP de research. Nace **apagada**: montarla expone un endpoint nuevo cuyo
    # control de acceso son las credenciales de consumidor (no el Bearer compartido de
    # las monturas internas, que protege el transporte pero no identifica al owner).
    # Activarla es una decision explicita de despliegue.
    RESEARCH_MCP_ENABLED: bool = False
    RESEARCH_MCP_PATH: str = "/internal/mcp-research"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
