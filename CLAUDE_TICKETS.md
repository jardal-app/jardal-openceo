# Tickets — referencia rápida del proyecto

> **La autoridad es
> [`jardal-knowledge/CLAUDE.md`](../jardal-knowledge/CLAUDE.md)**, que se carga en
> todas las sesiones de Claude Code y Codex de esta máquina. Este archivo es solo la
> referencia corta para pegar en el `AGENTS.md`/`CLAUDE.md` de un proyecto; si algo
> aquí lo contradice, manda el canónico.

## Dónde vive el trabajo

**Los tickets viven en ERPNext**, no en un markdown del repositorio:
<https://erp.jardal.app/app/task>

| Pregunta | Se responde en |
| --- | --- |
| ¿Qué trabajo existe? | **ERPNext** — `Task` (ticket) y `Project` (identidad de proyecto) |
| ¿Cómo se está ejecutando? | **MongoDB** — sesiones, intentos, runs |
| ¿Por qué se diseñó así? | **Vault de Obsidian** (`qmd search` para encontrar) |
| ¿Qué código lo implementa? | **Git** — el repositorio |

Un backlog en markdown **no**: se desactualiza en silencio y solo lo lee quien lo
edita. En `jardal-openceo` ya pasó — al migrar su `PENDINGS.md`, **7 de 27 items
figuraban como pendientes y ya estaban implementados**.

## Reglas

1. **No mantengas `PENDINGS.md`, `TODO.md` ni listas en el README.** Si quieres una
   referencia local, deja **solo el número** (`TASK-2026-000NN`); el estado real se
   consulta en ERPNext.
2. **Busca antes de crear.** Un ticket duplicado es peor que ninguno.
3. **Un ticket es trabajo, no una idea.** Una idea o un hallazgo técnico va al vault
   de Obsidian siguiendo el skill `obsidian-vault-links`.
4. **Registrar no es ejecutar.** `autorun` es `false` por defecto.
5. **Cita el número de ERPNext** (`TASK-2026-000NN`), no el `ticket_number` interno.

## Tools (MCP de OpenCEO)

| Tool | Para qué |
| --- | --- |
| `openceo_search_tickets` | **Empezar por aquí.** Por texto, proyecto o estado |
| `openceo_get_ticket` | Leer uno por `TASK-2026-000NN` o `openceo_ref` |
| `openceo_create_ticket` | Registrar uno |
| `openceo_create_tickets_batch` | Registrar varios (máx. 50) |
| `openceo_create_pm_tickets_batch` | Hijos de un padre (spillover) |

**Enlazar a un ticket existente** se hace con `source_id`: es la clave de
idempotencia, así que registrar dos veces el mismo `(source, source_id)` devuelve el
existente en vez de duplicarlo.

## Al registrar backlog en un proyecto

1. `openceo_search_tickets(project="<project_key>")` para ver qué hay.
2. `openceo_create_ticket(..., project="<project_key>")`. El proyecto se crea en
   ERPNext la primera vez, con `project_key` = el nombre de la carpeta en `projects/`.
