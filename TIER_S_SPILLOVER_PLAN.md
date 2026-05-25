# Tier S — Extensión: max_plans alto + Spillover via MCP (MongoDB-first)

## Context

Después de la reescritura inicial de Tier S (Planner → Plan Reviewer → Builder → 3 Reviewers × 2 rondas), el usuario pidió tres mejoras:

1. **Subir `max_plans`** a 15 para que el planner no corte tickets grandes.
2. **Spillover MCP**: si aun así un ticket es demasiado grande, el planner crea sub-tickets en OpenCEO via MCP (en vez de fragmentar más allá del cap) y esos se procesan en su propio ciclo Tier S.
3. **Reorientar las tools MCP**: hoy se llaman `notion_create_ticket*` y crean directamente en Notion, pero la arquitectura cambió: MongoDB es ahora el sistema central y Notion solo se sincroniza desde MongoDB. Hay que **renombrar y reimplementar** las tools para que escriban en MongoDB y dejen que el sync_back ya existente las propague a Notion.

Este plan cubre las 3 piezas. Es código adicional sobre la base ya implementada; no toca el flujo Planner → Reviewer → Builder existente más allá del prompt del planner.

---

## Cambios concretos

### A. Subir `max_plans` de 5 → 15

Tres puntos a tocar:

| Archivo | Línea | Cambio |
|---|---|---|
| `openceo-core/agents/dev/consensus.py` | 326 | `int(tier_s_cfg.get("max_plans", 5))` → `int(tier_s_cfg.get("max_plans", 15))` |
| `openceo-core/config_loader.py` | 106 | `"max_plans": 5,` → `"max_plans": 15,` |
| `projects/jardal-trading/project.yaml` | 16 | `max_plans: 5` → `max_plans: 15` |

Esto es 1 línea por archivo. El cap sigue siendo un guard para evitar bugs (planner devuelve `TOTAL_PLANS: 9999`), pero ya da mucho margen antes de forzar spillover.

### B. Renombrar y reimplementar las 3 tools MCP (MongoDB-first)

**Hoy** en `openceo-core/mcp_server.py:172-257`:
```python
def _register_openceo_tools(server, settings):
    notion = NotionTicketManager(settings)
    def notion_create_ticket(...)       → notion.create_ticket(...)    # ❌ escribe a Notion
    def notion_create_tickets_batch(...) → notion.create_tickets_batch(...)
    def notion_create_pm_tickets_batch(...) → notion.create_tickets_batch(...)
    server.tool(notion_create_ticket, name="notion_create_ticket", ...)
    server.tool(notion_create_tickets_batch, name="notion_create_tickets_batch", ...)
    server.tool(notion_create_pm_tickets_batch, name="notion_create_pm_tickets_batch", ...)
    return ("notion_create_ticket", "notion_create_tickets_batch", "notion_create_pm_tickets_batch")
```

**Cambio: renombrar a `openceo_*` y reapuntar al `TaskService` (MongoDB):**

```python
def _register_openceo_tools(server, settings, task_service: "TaskService"):
    def openceo_create_ticket(
        title: str,
        description: str = "",
        project: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
    ) -> dict[str, Any]:
        # build a TaskCreateRequest and call task_service.create_task(...)
        # task_service ya dispara _fire_notion_sync_back automáticamente
        ...

    def openceo_create_tickets_batch(tickets: str) -> dict[str, Any]:
        # parse JSON array, call task_service.create_task for each, collect results
        ...

    def openceo_create_pm_tickets_batch(
        project: str,
        source_id: str,        # ID del ticket original (anti-loop key)
        tickets: str,
        source: str = "spillover",  # default actualizado
    ) -> dict[str, Any]:
        # parse, validate, create each in MongoDB with tag "spillover_from:{source_id}"
        ...

    server.tool(openceo_create_ticket, name="openceo_create_ticket",
                description="Crea un ticket en OpenCEO (MongoDB). El sync a Notion se dispara automáticamente.")
    server.tool(openceo_create_tickets_batch, name="openceo_create_tickets_batch",
                description="Crea varios tickets en OpenCEO en un lote. Entrada: JSON array. Max 50.")
    server.tool(openceo_create_pm_tickets_batch, name="openceo_create_pm_tickets_batch",
                description="Crea tickets derivados (spillover) desde un ticket padre. Marca tag spillover_from:{source_id}.")
    return ("openceo_create_ticket", "openceo_create_tickets_batch", "openceo_create_pm_tickets_batch")
```

**Dependency injection**: `_register_openceo_tools` ahora necesita `task_service`. Toca actualizar:
- `_register_all_tools(server, settings)` → `_register_all_tools(server, settings, task_service)` y propagar
- Quien construya el MCP server (probablemente `main.py` o un module bootstrap) tiene que pasar la instancia de `TaskService` ya inicializada.

**Mantener `NotionTicketManager`** intacto en `tools/notion.py` — sigue siendo usado por el sync_back. Solo las **tools MCP** dejan de invocarlo.

**No mantener alias `notion_*`**: rebrand total. Si algún consumidor externo los usaba (e.g. config en `~/.claude/mcp_servers.json` de Claude Code), hay que actualizarlo. Documentar el rename en el commit y en `STATUS.md`.

### C. Spillover en el prompt del planner

Modificar `consensus.py:_planner_prompt` (alrededor de línea 446) para agregar la directiva de spillover y el anti-loop:

```python
def _planner_prompt(self, instruction: str, max_plans: int, paths: _TierSPaths,
                    ticket_id: str, is_spillover: bool) -> str:
    spillover_block = ""
    if not is_spillover:
        spillover_block = (
            f"\n## Spillover (overflow)\n"
            f"If the ticket is so large that you would need MORE than {max_plans} sub-plans:\n"
            f"- Do NOT fragment further. Use the MCP tool `openceo_create_pm_tickets_batch` with:\n"
            f"  - project: (this project)\n"
            f"  - source_id: \"{ticket_id}\"\n"
            f"  - source: \"spillover\"\n"
            f"  - tickets: JSON array of derived sub-tickets, each self-contained\n"
            f"    (include enough context from the original so each runs alone in a new Tier S cycle)\n"
            f"- In CURRENT_PLAN.md, list the derived ticket IDs in a final '## Derived (spillover)' section\n"
            f"  so we have traceability.\n"
            f"- Keep this ticket's TOTAL_PLANS to the largest atomic chunk that fits in {max_plans}.\n"
        )
    else:
        spillover_block = (
            f"\n## Anti-loop\n"
            f"This ticket is itself a spillover from a parent ticket. "
            f"DO NOT create further spillover tickets — fragment your work into <= {max_plans} sub-plans only.\n"
        )

    return (
        f"Read {_CONTEXT_FILENAME} if it exists at the repo root for project context.\n"
        f"Ticket: {instruction}\n\n"
        f"... [resto del prompt actual] ..."
        f"{spillover_block}"
    )
```

**Pasar `ticket_id` e `is_spillover`** desde `execute()`:
- `execute()` recibe `project` y `instruction`, pero no el `task_id`. Hay que extenderlo: en `session.py:_run_critical_consensus` pasar el `task["_id"]` (o equivalente) a `orchestrator.execute(...)`.
- `is_spillover` se determina así: en `task_service`, cuando se crea un ticket via `openceo_create_pm_tickets_batch`, automáticamente se le pone `tag = "spillover_from:{source_id}"`. En `execute()` leemos del config/task si el tag está presente; si sí, `is_spillover = True`.

Esto requiere extender la firma de `execute()`:
```python
async def execute(
    self,
    project: str,
    instruction: str,
    config: dict[str, Any],
    progress: Callable[[str], Awaitable[None]],
    task_id: str | None = None,        # NEW
    is_spillover: bool = False,        # NEW (default False para tickets normales)
) -> ConsensusRunResult:
```

Y en `_run_planner` pasar `task_id` e `is_spillover` al `_planner_prompt`.

### D. Anti-loop a nivel de ingest (failsafe)

El prompt instruye al planner a no crear spillover desde tickets-de-spillover, pero un failsafe extra en código evita bugs:

En `openceo-core/mcp_server.py` dentro de `openceo_create_pm_tickets_batch`, antes de crear los tickets, verificar que el ticket padre (`source_id`) NO sea ya un spillover:

```python
def openceo_create_pm_tickets_batch(project, source_id, tickets, source="spillover"):
    parent = task_service.get_task_sync(source_id)
    if parent and any(t.startswith("spillover_from:") for t in (parent.get("tags") or [])):
        return {
            "ok": False,
            "status": "anti_loop",
            "message": f"Ticket {source_id} ya es un spillover. No se permite nested spillover.",
        }
    # ... continuar
```

(Si `task_service` no tiene método sync, usar `asyncio.run_in_executor` o exponer un método helper. Detalle de implementación menor.)

### E. Configuración MCP del planner (`clauded` + `claudep`)

Para que el planner pueda llamar `openceo_create_pm_tickets_batch`, los wrappers necesitan tener el MCP server `openceo-internal` registrado.

**Verificación primero**: leer `~/.claude/.claude.json` (clauded) y `~/.claude-deepseek/.claude.json` (claudep) y confirmar que tienen un bloque:
```json
{
  "mcpServers": {
    "openceo-internal": {
      "type": "http",
      "url": "http://127.0.0.1:18765/internal/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

Backups en `~/.claude/backups/.claude.json.backup.*` muestran que clauded **lo tuvo** configurado en algún momento, pero conviene confirmar el estado actual.

**Si falta**: agregar al runner script (`scripts/openceo-core-host-runner.sh`) o a un hook de bootstrap un paso que escriba/actualice esos JSONs usando `build_mcp_provider_config(settings)` (ya existe en `mcp_server.py:137`). Algo como:

```python
# en bootstrap o un script standalone
from mcp_server import build_mcp_provider_config
cfg = build_mcp_provider_config(settings)
for dir_path in ["~/.claude", "~/.claude-deepseek"]:
    path = Path(dir_path).expanduser() / ".claude.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.setdefault("mcpServers", {}).update(cfg["mcpServers"])
    path.write_text(json.dumps(existing, indent=2))
```

Para `codexf` (Codex CLI) y `gemini -y` la registración MCP es distinta — fuera del scope inicial; si el usuario lo necesita en esos también, se hace en un PR aparte. Por ahora basta con `clauded` (planner) y `claudep` (builder); ellos son los que harían spillover.

---

## Lista de archivos a tocar

| Archivo | Tipo de cambio |
|---|---|
| `openceo-core/agents/dev/consensus.py` | (1) Default `max_plans` 5→15. (2) Extender firma de `execute()` con `task_id` y `is_spillover`. (3) Modificar `_planner_prompt` para incluir spillover block. (4) Propagar `task_id`/`is_spillover` desde `_run_planner`. |
| `openceo-core/agents/dev/session.py` | En `_run_critical_consensus`, leer `task_id` y `is_spillover` (del task doc / tags) y pasarlos a `orchestrator.execute(...)`. |
| `openceo-core/config_loader.py` | Default `max_plans` 5→15. |
| `openceo-core/mcp_server.py` | (1) Renombrar 3 tools `notion_*` → `openceo_*`. (2) Reimplementar para usar `task_service.create_task`. (3) Agregar anti-loop en `openceo_create_pm_tickets_batch`. (4) Extender `_register_openceo_tools` y `_register_all_tools` para recibir `task_service`. |
| Punto de wiring del MCP (probablemente `main.py` donde se construye `FastMCP`) | Pasar `task_service` a `_register_all_tools`. |
| `projects/jardal-trading/project.yaml` | `max_plans: 5` → `max_plans: 15`. |
| `scripts/openceo-core-host-runner.sh` (o nuevo `scripts/sync-mcp-config.py`) | Escribir/actualizar `mcpServers.openceo-internal` en `~/.claude/.claude.json` y `~/.claude-deepseek/.claude.json` al boot. |
| `STATUS.md` | Anotar el rename de tools MCP (breaking change para clientes externos). |

---

## Edge cases / consideraciones

1. **Sync inmediato a Notion**: hoy `task_service.create_task` ¿dispara `_fire_notion_sync_back` automáticamente al crear, o solo en cambios de estado? Verificar y, si no, agregar el call en la creación inicial para que el ticket aparezca en Notion sin esperar al loop.
2. **Project name mapping**: el planner pasa un `project` string a la tool MCP. Validar que coincide con un proyecto registrado en OpenCEO (o aceptar el del ticket padre como default).
3. **Costos**: con `max_plans = 15`, un ticket extremo es 15 builds + 15×2×3 = 90 reviews = ~105 turns. Cada turn ~600s timeout. Es una sesión Tier S potencialmente de horas. **Recomendado**: ajustar `timeout_minutes` por proyecto si se espera tickets así de grandes, o aceptar que esos tickets corran overnight.
4. **Persistencia de spillover**: cuando el planner crea sub-tickets, anotarlo en `CURRENT_PLAN.md` (sección "## Derived (spillover)") es importante para auditoría. El prompt ya lo pide.
5. **Si el MCP server cae mid-spillover**: la tool retornará `ok: false` y el planner debe manejarlo. El prompt podría decir: "si la llamada MCP falla, intenta de nuevo una vez; si falla otra vez, escribe el problema en CURRENT_PLAN.md y continúa fragmentando hasta `max_plans` lo mejor posible."
6. **Backwards compat de las tools renombradas**: si OTROS consumidores (workflows, otros agents) llaman `notion_create_ticket`, los rompemos. Buscar grep `notion_create_ticket\|notion_create_tickets_batch\|notion_create_pm_tickets_batch` en todo el repo antes de borrarlas; si hay callers, o se migran o se mantienen como aliases delegadores temporales.

---

## Verification

1. **Defaults sanity** — arrancar OpenCEO, abrir una shell Python, importar config_loader y verificar que `cfg["dev"]["tier_s"]["max_plans"] == 15`.
2. **MCP tools** — `curl -sH "Authorization: Bearer $OPENCEO_API_KEY" http://127.0.0.1:18765/internal/mcp/tools/list` (o el endpoint MCP equivalente) y confirmar que aparecen `openceo_create_ticket`, `openceo_create_tickets_batch`, `openceo_create_pm_tickets_batch`. Las viejas `notion_*` NO deben aparecer.
3. **Creación via MCP** — desde el planner (clauded) en un repl interactivo, invocar la tool y verificar que: (a) aparece un task nuevo en MongoDB collection `tasks`, (b) en ~10s aparece en Notion, (c) tiene el tag `spillover_from:{source_id}` si fue via `openceo_create_pm_tickets_batch`.
4. **Anti-loop** — crear un task con tag `spillover_from:X`, invocar `openceo_create_pm_tickets_batch(source_id=ese_task_id, ...)`, esperar `ok: false, status: anti_loop`.
5. **Spillover E2E** — ticket gigante con instrucción de "hace 20 features"; planner declara `TOTAL_PLANS: 15` y crea 5 sub-tickets de spillover via MCP. Después de TICKET_COMPLETE del padre, los 5 sub-tickets aparecen en la cola y se procesan en sus propios ciclos Tier S.
6. **Configuración MCP de clauded/claudep** — `cat ~/.claude/.claude.json | jq '.mcpServers."openceo-internal"'` retorna el bloque correcto; mismo check en `~/.claude-deepseek/.claude.json`.

---

## Orden sugerido de PRs

1. **PR-1 (trivial)**: subir `max_plans` 5→15 en los 3 archivos. Ship inmediato.
2. **PR-2 (refactor MCP)**: rename `notion_*` → `openceo_*` + reimplementación a MongoDB-first + dependency injection del TaskService. Tocar el wiring de FastMCP. Antes de mergear, asegurar que ningún caller interno usa los nombres viejos (grep).
3. **PR-3 (spillover)**: extender `execute()` con `task_id`/`is_spillover`, modificar `_planner_prompt`, agregar anti-loop en `openceo_create_pm_tickets_batch`, actualizar STATUS.md.
4. **PR-4 (opcional)**: script de bootstrap que escribe `mcpServers.openceo-internal` en `~/.claude/.claude.json` y `~/.claude-deepseek/.claude.json` al boot del runner.
