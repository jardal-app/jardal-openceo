# Tickets — sección para copiar en cada proyecto

> **Cómo usar este archivo**: pégalo (o su sección «Reglas») en el `AGENTS.md` o `CLAUDE.md`
> de cada proyecto, o déjalo referenciado. Es el contrato que hace que un agente sepa dónde
> vive el trabajo sin que nadie se lo explique en cada sesión.

## Dónde vive el trabajo

**Los tickets viven en ERPNext**, no en un markdown del repositorio:
<https://erp.jardal.app/app/task>

| Pregunta | Se responde en |
| --- | --- |
| ¿Qué trabajo existe? | **ERPNext** — `Task` (ticket) y `Project` (identidad de proyecto) |
| ¿Cómo se está ejecutando? | **MongoDB** — sesiones, intentos, runs |
| ¿Por qué se diseñó así? | **Vault de Obsidian** (`qmd search` para encontrar) |
| ¿Qué código lo implementa? | **Git** — el repositorio |

Los cuatro son dominios distintos y ninguno sustituye a otro. Una decisión de arquitectura **no**
es un ticket; un ticket **no** es una nota de conocimiento.

## Reglas

1. **No mantengas un backlog en markdown.** Ni `PENDINGS.md`, ni `TODO.md`, ni listas en el
   README. Un backlog en markdown se desactualiza en silencio y solo lo ve quien lo edita. En
   `jardal-openceo` esto ya pasó: al migrar su `PENDINGS.md`, **7 de 27 items figuraban como
   pendientes y ya estaban implementados**, y 2 estaban absorbidos por otro ticket.
2. **Busca antes de crear.** Un ticket duplicado es peor que ninguno: dos personas trabajan lo
   mismo y ninguna sabe de la otra. Usa `openceo_search_tickets` primero.
3. **Un ticket es trabajo, no una idea.** Si es una idea o un hallazgo técnico, va al vault de
   Obsidian (siguiendo el skill del vault). Si es trabajo que alguien tiene que hacer, va a
   ERPNext.
4. **Registrar no es ejecutar.** Por defecto un ticket se **registra** y no arranca nada.
   Ejecutar es una decisión explícita (`autorun: true`).
5. **El número de ticket que se cita en conversación es el de ERPNext** (`TASK-2026-00025`).
   El `ticket_number` interno de OpenCEO solo existe si el ticket llegó a ejecutarse.

## Cómo se usan (MCP interno de OpenCEO)

Estos tools están en el **MCP interno** (`/internal/mcp`), que OpenCEO inyecta en las sesiones
gestionadas. El **MCP global** (`/internal/mcp-global`) solo tiene DNS: si estás en una sesión
manual fuera de OpenCEO, no los tendrás.

| Tool | Para qué |
| --- | --- |
| `openceo_search_tickets` | **Empezar por aquí.** Busca por texto, proyecto o estado. Devuelve el ticket de negocio y, si ya se ejecutó, su estado de ejecución |
| `openceo_get_ticket` | Leer uno concreto por `TASK-2026-000NN` o por su `openceo_ref` |
| `openceo_create_ticket` | Registrar un ticket nuevo |
| `openceo_create_tickets_batch` | Registrar varios de una vez (máx. 50) |
| `openceo_create_pm_tickets_batch` | Hijos de un ticket padre (spillover) |

### El flujo que se espera de un agente

```
1. openceo_search_tickets(query="login google", project="melanyryr")
      ├─ hay un ticket que lo cubre  -> enlaza tu trabajo a él, no crees otro
      └─ no hay nada                 -> openceo_create_ticket(...)
```

**Enlazar trabajo a un ticket existente** se hace con `source_id`: es la clave de idempotencia,
así que registrar dos veces el mismo `(source, source_id)` devuelve el ticket existente en vez de
duplicarlo. Úsalo siempre que tu trabajo tenga un identificador natural (el número de issue, el
hash del commit, el id de la conversación).

### `autorun`

| Valor | Qué pasa |
| --- | --- |
| `false` (**por defecto**) | El ticket se registra y **queda esperando**. Nada arranca solo |
| `true` | Además se lanza la ejecución. Consume cuota de proveedores y hace trabajo real en el repo |

Pide `autorun: true` **solo** cuando el usuario te haya pedido ejecutar, no cuando estés
organizando backlog.

## Preguntas frecuentes

**¿Puedo buscar tickets como con el CLI de JIRA?**
Sí: `openceo_search_tickets` busca por texto en el asunto, filtra por proyecto y por estado, y
devuelve el estado de ejecución cuando existe. Es el equivalente al `jira search` que echas de
menos — está limitado a las capacidades expuestas, no a SQL libre, y esa frontera es deliberada:
el agente no puede escribir a ciegas en el sistema de negocio.

**¿Y el vault de Obsidian y `qmd`?**
Son para **conocimiento**, no para trabajo. El reparto está en `docs/OPENCEO_DIRECTION.md`:
ERPNext coordina el trabajo, Obsidian conserva la memoria de ingeniería, Git la implementación,
OpenCEO la ejecución. Un agente lee el vault y el repo directamente; **no** pasa por OpenCEO para
leer conocimiento. No hay MCP de Obsidian ni de Grafos a propósito.

**¿Qué pasó con Notion?**
Sigue encendido como espejo de compatibilidad mientras ERPNext se asienta, pero deja de ser la
autoridad. Su eliminación se evalúa en un ticket aparte.

**¿Por qué un ticket muestra `openceo_ticket_number: 0`?**
Cero significa "sin número de OpenCEO": es un ticket de negocio que se registró pero nunca llegó
a ser tarea de ejecución. Los números de OpenCEO empiezan en 1.

## Al registrar backlog en un proyecto nuevo

1. `openceo_search_tickets(project="<project_key>")` para ver qué hay.
2. Registra con `openceo_create_ticket(..., project="<project_key>")`. El proyecto se crea en
   ERPNext la primera vez, con `project_key` = el nombre de la carpeta en `projects/`.
3. **No** dejes una lista en markdown como "registro". Si quieres una referencia local, deja
   **solo el número** (`TASK-2026-000NN`) — el estado real se consulta en ERPNext.
