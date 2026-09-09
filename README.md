# OpenCEO

Multi-agent enterprise orchestrator. Open-source. Self-hosted. LangGraph + FastAPI + n8n + OpenWebUI + MongoDB.

**"OpenCEO doesn't simulate corporate bureaucracy — it eliminates it."** Fewer agents, fewer tokens, faster results. Each agent is a competent role, not a job title. Workflows are deterministic recipes, not LLM conversations.

> **ARCHITECTURE DOCUMENT — DO NOT ADD STATUS, PROGRESS, OR RUNTIME SNAPSHOTS HERE.**
> This file defines architecture, flows, and interfaces only. Operational tracking belongs in `STATUS.md`.

---

## Stack

| Component | Technology | Purpose |
|---|---|---|
| Orchestration | LangGraph (Python) | Supervisor-workers graph for the explicitly wired runtime (`ceo`, `dev`, `pm`, `finance`, `secretary`) |
| API Server | FastAPI | OpenAI-compatible endpoint for OpenWebUI, REST API for dashboard/n8n, WebSocket for real-time |
| Database | MongoDB | Tasks, dev sessions, user context, browser queues, and agent metadata |
| Automation | n8n (self-hosted, Docker) | External triggers (Jira, Notion, GitHub webhooks), complex multi-step flows, WhatsApp inbound classification |
| Chat/Multimedia | OpenWebUI (self-hosted) | Text chat, file/image/video uploads, basic voice. Connects via OpenAI-compatible API |
| Voice (realtime) | LiveKit Agents + Silero VAD | Fluid voice conversations. Groq Whisper Turbo STT, OpenCEO as LLM backend. Mobile app via LiveKit Android starter |
| MCP Server | FastMCP (inside openceo-core) | Exposes selected infrastructure tools to Claude Code / Codex CLI. Owner controls which tools are exposed |
| Notifications | Telegram Bot API | Bidirectional: user sends commands, agents send status updates |
| LLM Observability | Langfuse (self-hosted) | LLM call tracing, token usage per agent, prompt debugging |
| Dashboard | React (custom) | Mission control frontend |
| Browser Automation | Playwright | Browser-based workflows, form filling, authenticated portals |
| Web Search | SearXNG / Brave Search API / provider search API | Agents search the internet for information, then fetch or browse results |
| Coding Tier 1 | Codex CLI + GPT-5.4 | Subscription. Complex features, architectural changes |
| Coding Tier 2 | Claude Code + Opus 4.6 | Subscription. Security, math, logic, critical bugs |
| Coding Tier S | Claude Code + Codex + Gemini via tmux + OpenCode headless review pipeline | Special multi-AI review flow for high-stakes tickets |
| Real-time | WebSocket (FastAPI native) | Dashboard live updates. No Redis needed |
| In-memory cache | Python process memory | Active connections, live agent state. Rebuilds from MongoDB on restart |

Not used: Redis, PostgreSQL, LiteLLM, aider.

---

## Architecture Rules

- Crystallized workflows execute as deterministic Python. Zero LLM tokens on step sequencing, flow control, or variable resolution. Discovery mode uses LLM to find unknown flows — but discovered flows crystallize into YAML and become deterministic thereafter.
- LLMs are used where they add genuine intelligence: (1) CEO routing fallback when config rules do not decide (`router.py`), (2) discovery mode — `create_react_agent` discovers unknown flows from primitives, (3) `llm_eval` conditions inside workflow loops (True/False evaluator, not orchestrator), (4) structure extraction from documents (bids, contracts, PDFs), (5) secretary email payload extraction fallback, and (6) the coding CLIs themselves (`codex`, `claude`, `gemini`, `opencode`, `glm`). LLMs are NOT used for: executing crystallized workflows, routing known intents, deciding step order in YAML, or replacing deterministic logic.
- Runtime wiring is explicit. A folder in `openceo-core/agents/` is a portable skill package, but it becomes executable only when it is registered in `router.py`, `graph.py`, and the relevant service entrypoints.
- The bundled worker graph wires `dev`, `pm`, `finance`, and `secretary`.
- Project configuration lives in YAML (`projects/*/project.yaml`). CEO routing triggers and patterns are read from `routing.agents`. Project is optional — tasks without a project use company-level defaults. Project context is required only when the Dev agent needs a specific repository.
- Guided chat workflows are deterministic state machines. They accept required inputs in any order, persist whatever the user already provided, and ask only for the missing fields before executing.
- Trigger phrases start a workflow but are not treated as the real payload unless they contain substantive content. Example: `tengo una entrevista` starts PM guidance, but it does not count as the interview text.
- Voice/chat UX must guide the user explicitly. The system should not assume the employee already knows the input order or that all required data will arrive in the first turn.
- CEO routing should also recognize substantive workflow payloads when they arrive first. A pasted interview or requirements block must enter the guided PM flow even if the user never typed the trigger phrase.
- Browser automation uses Playwright-based flows.
- Inter-agent communication is **peer-to-peer** during Tier 2 discovery. Agents pass results directly to the next agent without CEO as relay. In crystallized workflows (YAML), the executor calls Python functions directly — no inter-agent communication, only sequential tool execution. The CEO initiates tasks (top-down) but does not relay data between active agents.
- User project selection and active dev-session focus live in MongoDB.
- MongoDB is the canonical runtime store for OpenCEO state, queues, tasks, workflow-local params, and dashboard data. External systems such as Notion are projections or mirrors, not the authoritative source of truth.
- `TaskDocument` is a stable cross-agent contract. Do not add a new top-level task field for every workflow-specific need.
- Promote a task attribute to top-level only when it changes runtime semantics across the system: routing, scheduling, lifecycle gates, idempotency, global filtering, or sync-back to external systems.
- Example: approval gating belongs at top-level (`approval_required`, `approval_state`) because it affects lifecycle, filters, and sync-back. Individual review events do not; they belong in a separate collection linked by `task_id`.
- Use `params` for workflow-local request metadata and small per-task knobs that do not need global semantics.
- Use separate collections for high-churn or history-heavy data such as review logs, comments, approvals, agent-specific drafts, or other append-only workflow state. That data should reference the canonical task by `task_id` instead of inflating the task document indefinitely.

### Skill Packaging Contract

- Every bundled skill folder contains `prompt.md`, `tools.py`, `requirements.txt`, and `.env.example`.
- `openceo-core/requirements.txt` now contains only shared runtime dependencies. Agent-specific packages are declared in `openceo-core/agents/{name}/requirements.txt`.
- Bundled local installs use `./scripts/install-agent-requirements.sh` after the shared requirements install.
- For now, the core runtime still loads environment variables centrally from `openceo-core/.env` and `openceo-core/.env.finance.local`. The per-agent `.env.example` files are the source manifests for packaging and marketplace portability.

### Tool Decision Framework

Not every external integration goes through n8n. Each tool is evaluated case by case by the developer at implementation time:

**Local Tool (Python in agent's `tools.py`)** — A simple API call or deterministic workflow used by one agent. The credential manifest belongs in that agent's `.env.example`; in the bundled deployment, the actual values are merged into `openceo-core/.env` or `.env.finance.local`. Examples: agent-specific billing flow, agent-only SaaS integration, skill-local parsing logic.

**Shared Tool (Python module in `openceo-core/tools/`)** — Multiple agents need the same function. Imported by any agent's `tools.py`. Examples: generate documents from templates (Secretary, Legal, HR), send email via SMTP (Sales, Secretary, Support), send Telegram notifications (all agents), MongoDB queries (PM, HR, Finance).

**n8n Workflow** — External webhooks triggering tasks into OpenCEO, or complex multi-step orchestration with retries, delays, and conditional branching that benefits from visual debugging. Examples: Jira/GitHub webhook → normalize → POST to OpenCEO, WhatsApp incoming → classify contact → route, complex CRM sync with retry logic.

**Worker PC (browser task)** — Exclusively for platforms with NO usable API requiring browser automation with authenticated Chrome profiles. Examples: send Teams messages, generate content on AI platforms (Gemini web, ChatGPT web).

**Web Search + Browse** — Any agent can search the internet and read results. Two-step pattern: (1) search via SearXNG, Brave Search API, or any provider search API to get URLs, (2) fetch/read the URL content via `web_fetch` or navigate with Playwright (`agent-browser`) for pages that require interaction or JS rendering. Use cases: Finance researches market data, Sales finds competitor info, Secretary gathers context for reports, any agent fills external web forms.

**The key principle:** the developer building a new tool analyzes the specific case — not a blanket rule. A document generation tool used by 3 agents is a shared Python module, NOT an n8n workflow, because sending document data to n8n and back adds latency for no benefit. A WhatsApp incoming webhook IS n8n because OpenCEO cannot receive external webhooks natively.

### Agent Parallelism Rules

| Agent | Parallelism / Constraint |
|---|---|
| Dev | Persistent tmux sessions. Effective limit: one live runtime per project |
| PM | Guided state in Mongo per chat. Extraction runs in background and status stays queryable by chat or dashboard |
| Finance | Stateless per request. Can run multiple deterministic jobs in parallel |
| Secretary | Stateless per request. Can run multiple email tasks in parallel |

---

## Discovery & Crystallization

OpenCEO has two execution modes for any task:

1. **Deterministic** — a crystallized YAML workflow already exists → Python executor runs it with zero LLM tokens
2. **Discovery** — the flow is unknown → `create_react_agent` discovers it → human approves → it crystallizes into YAML

### Two Levels of Discovery

**Level 1: CEO** — `create_react_agent` with tools: `[call_pm, call_finance, call_secretary, call_dev]`. Question answered: **which agents do I need and in what order?**

**Level 2: Internal Agent** — `create_react_agent` with domain-specific primitives (`binance_api`, `bank_transfer`, etc.). Activated only when the agent receives a subtask it has not crystallized. Question answered: **how do I combine my primitives to accomplish this?**

### State Passing Between Tools (ReAct)

During discovery, the LLM is the "glue": it reads the previous tool's output from message history, extracts relevant data by inference, and passes them as parameters to the next tool. Tools must return **flat, explicit outputs** to minimize extraction errors:

```python
# Correct
return {"usd_amount": 95000, "status": "completed"}

# Avoid
return {"data": {"financial": {"amount": 95000}}}
```

### Workflow Resolution Tiers

Escalating tiers. Each tier is costlier than the previous and activates only if the previous did not resolve.

```
task arrives at CEO
      ↓
Tier 0: Keywords → direct match to known workflow? → deterministic executor (0 tokens)
      ↓ no match
Tier 1: check_registry → CEO consults compact catalog → match to 1 workflow? → execute (1 + N LLM calls)
      ↓ no unique match, but several workflows together cover it
Tier 1.5: Workflow union → CEO connects existing workflows via references → combined YAML
      ↓ no match, not even partial
Tier 2: Discovery + claim → create_react_agent discovers the flow from primitives
      ↓
Human reviews and approves
      ↓
Saved as YAML
      ↓
Next time: deterministic executor, zero LLM
```

| Tier | Mechanism | Tokens |
|---|---|---|
| 0 | Existing keywords | 0 |
| 1 | check_registry compact + detail of N candidates | 1 + N CEO calls |
| 1.5 | Workflow union with mappings | light CEO discovery |
| 2 | Full discovery + cross-agent claim | N calls per agent |

### Crystallized YAML Workflows

Captures: step sequence + data mapping between steps.

```yaml
workflow: crypto_reembolso
steps:
  - id: step1
    tool: finance.binance_api
    params:
      action: "sell"
      coin: "{{task.coin}}"
      amount: "{{task.amount}}"

  - id: step2
    tool: finance.bank_transfer
    params:
      amount: "{{step1.usd_received}}"
      to: "{{task.client_email}}"

  - id: step3
    tool: secretary.send_email
    params:
      to: "{{task.client_email}}"
      body: "Transfer {{step2.transfer_id}} processed"
```

The Python executor resolves `{{stepN.field}}` variables with real outputs. Zero tokens. Zero LLM. Deterministic.

### Workflow Schema Extensions

#### Loops

Workflows with iterative processes use `type: loop` with two condition types:

| Type | Evaluated by | Example |
|---|---|---|
| `python_eval` | Pure Python | `"{{count}} > 0"` |
| `llm_eval` | LLM (True/False only) | `"Is the proposal complete?"` |

Critical distinction: LLM as **evaluator** (answers True/False within a YAML condition) vs LLM as **orchestrator** (decides what steps to run). The LLM **cannot** add steps, change order, or skip steps. The YAML is the recipe. The LLM only answers questions within it.

```yaml
- id: step2
  type: loop
  max_iterations: 15
  condition:
    type: llm_eval
    prompt: "Does this draft meet all checklist items?"
    input: "{{step2.draft}} + {{step1.checklist}}"
  state: current_draft
  steps:
    - tool: sales.draft_section
    - tool: sales.self_review
    - tool: legal.review_clauses
      when: "{{step2b.legal_pending}} > 0"
```

The schema:

```
YAML schema (designed once)
    ├── type: sequence    → linear flows
    ├── type: loop        → iterations with condition
    │     ├── condition: python_eval
    │     └── condition: llm_eval
    └── when: "{{expr}}"  → conditional steps within any type
```

#### Human Approval

Human approval is not a YAML step. Dashboard-facing approvals are modeled on the task itself with `status=waiting` plus a business `phase` such as `qa_review`, `pm_approval`, or `client_signoff`.

YAML workflows stay deterministic technical recipes. If a workflow needs human approval before a side effect, the ticket phase workflow must place the task in a human phase before the YAML workflow executes.

Dashboard phase workflows are configured per project with reusable `workflow_templates`. A ticket stores a snapshot of the selected template so future project changes do not rewrite active work:

```json
{
  "workflow_templates": {
    "dev.default": {
      "agent": "dev",
      "default": true,
      "phases": ["dev_work", "qa_review", "completed"]
    },
    "dev.client_visible": {
      "agent": "dev",
      "phases": ["dev_work", "qa_review", "client_signoff", "completed"]
    }
  }
}
```

At task level the generic fields are:

```json
{
  "workflow_key": "dev.default",
  "workflow_phases": [
    {"key": "dev_work", "type": "agent", "agent": "dev"},
    {"key": "qa_review", "type": "human", "permission": "qa_review"},
    {"key": "completed", "type": "terminal", "status": "completed"}
  ],
  "phase": "qa_review",
  "phase_index": 1,
  "phase_assignments": {"qa_review": ["usr_qa"]}
}
```

`phase_assignments` is the generic way to add the QA/client users as involved users for visibility. Human approval routes are `POST /api/tasks/{task_id}/phase-action`; QA can open a Kimi review session with `POST /api/tasks/{task_id}/phase-kimi`. Approve advances to the next phase; request changes/reject returns the ticket to `queued` for CEO re-routing while preserving history.

#### Workflow Composition (Tier 1.5)

The CEO builds a top-level YAML that **references** existing crystallized workflows — not flattened copies:

```yaml
workflow: cierre_mes_completo
steps:
  - id: step1
    ref: finance.cierre_contable
    params:
      periodo: "{{task.periodo}}"
    output: datos_contables

  - id: step2
    ref: finance.reporte_mensual
    params:
      input: "{{steps.datos_contables}}"
    output: reporte

  - id: step3
    ref: secretary.notificacion
    params:
      reporte: "{{steps.reporte}}"
```

References over flattening: if Finance improves `cierre_contable`, the composed workflow benefits automatically. Cross-agent workflows are stored under `ceo/`:

```
workflows/
  finance/
    cierre_contable.yaml
    reporte_mensual.yaml
  secretary/
    notificacion_clientes.yaml
  ceo/
    cierre_mes_completo.yaml
    crypto_reembolso.yaml
```

### Tool Registry

Dictionary mapping `agent.tool_name` → Python function.

```python
TOOL_REGISTRY = {
    "finance.binance_api":    binance_api_fn,
    "finance.bank_transfer":  bank_transfer_fn,
    "secretary.send_email":   send_email_fn,
    "pm.create_ticket":       create_ticket_fn,
}
```

Naming convention: `{agent}.{tool_name}`. Tools reused by multiple agents register under the owning agent (no `global` namespace). The registry abstracts local vs external — a tool wrapping an HTTP call to a microservice looks identical to a local Python function.

### check_registry — Two-Level Query

```python
@tool
def check_registry(agent: str | None = None) -> dict:
    """Returns available crystallized workflows.

    No agent: compact summary of all agents and their workflows.
    With agent: full detail of that agent only.

    Example (no filter):
    {
        "finance":   ["cierre_contable", "reporte_mensual", "pago_elfec"],
        "secretary": ["notificacion_clientes", "send_email"],
        "pm":        ["create_tickets", "sprint_planning"],
    }
    """
```

CEO flow for large tasks: `check_registry()` → global summary → reason about matches → `check_registry("finance")` → full detail where it applies. First global summary (avoids missing matches by wrong agent assumption), then detail only where relevant.

### LLM Usage Guidelines

With high-limit token plans, the criterion shifts from "can I avoid the LLM?" to "does it add genuine value here?"

**Where LLM adds value:**
1. Qualitative condition evaluator (`llm_eval` in loops)
2. Structure extraction from documents (bids, contracts, PDFs)
3. Anomaly detection in inputs before executing crystallized workflows
4. Normalization between tools when output/input formats don't match exactly
5. YAML validation before crystallization
6. Anywhere the technical criterion justifies it — genuine intelligence that determinism cannot cover

**Where LLM does NOT add value:**
- Deciding step order in crystallized workflows → YAML defines it
- Choosing which tool when already determined → executor knows
- Intent routing → CEO handles (Tier 0 keywords + Tiers 1-2 if escalated)
- Identical repetitive processes → crystallized workflow gives more consistency

Rule: high limits are not infinite. Use LLM where it contributes genuine intelligence, not as a substitute for deterministic logic.

### Blocked Discovery — Missing Tool Flow

When discovery detects a missing tool, it does not fail or restart. It saves state to MongoDB:

```json
{
  "discovery_id": "abc123",
  "status": "BLOCKED_TOOL",
  "blocked_at": "step2",
  "reason": "tool network.mikrotik_api does not exist in registry",
  "context": {
    "steps_completed": ["step1"],
    "current_output": "...everything produced so far..."
  }
}
```

When the tool becomes available, discovery continues from `blocked_at` with full context.

Human approval flow: system notifies admin → admin approves → Dev agent implements and registers → discovery continues. If admin rejects, the discovery and run close definitively (`CANCELED_ADMIN`). The task is marked as unresolved with the rejection reason. No automatic retry — if admin changes their mind, the task must be re-sent manually.

No separate QA step — re-continuing the discovery from `blocked_at` IS the test case.

### Workflow Runs — Storage

| What | Where | Why |
|---|---|---|
| Workflow definition (recipe) | YAML files | git-versioned, editable by Dev agent |
| Workflow run (execution) | MongoDB | runtime, mutable, queryable, survives restarts |

Query tool for workflow runs — the LLM formulates the query based on the request:

```python
@tool
def query_workflow_runs(filters: dict) -> list:
    """Query workflow runs in MongoDB.

    Filterable schema:
    - status: "PENDING" | "RUNNING" | "BLOCKED_TOOL" | "COMPLETED" | "FAILED" | "CANCELED"
    - workflow: workflow name (e.g., "propuesta_licitacion")
    - agent: responsible agent (e.g., "finance", "sales")
    - created_at: creation date (ISO format)

    Example: {"status": "BLOCKED_TOOL", "agent": "sales"}
    """
    return list(db.workflow_runs.find(filters))
```

The schema in the tool description guarantees the LLM formulates consistent queries. Read-only queries — no effect on workflow execution.

### CEO Discovery — Fragmentation & Matching

The CEO analyzes data dependencies to determine parallelism:
- "update accounting **and** notify clients" → parallel (independent)
- "update accounting **then** prepare reports with that data" → sequential (dependency)

#### Four Fragmentation Cases

| Fragmentation | Coverage | Result |
|---|---|---|
| Parallel | all with match | independent tickets, zero LLM in execution |
| Parallel | some without match | tickets for matched + discovery only for the orphan |
| Sequential | all with match | dynamic YAML connecting existing workflows with mappings |
| Sequential | some without match | discovery for orphan step, blocks until resolved |

#### Parallel vs Sequential — Different Mechanisms

**Parallel** (independent fragments): CEO uses `assign_task` per fragment. Each agent works independently. No shared YAML — they are loose subtasks.

**Sequential** (data-dependent fragments): CEO builds a workflow YAML with steps and mappings. Tier 1.5 if it can join existing workflows via references. Tier 2 if it needs discovery. The executor runs in order.

They do not mix: either parallel subtasks or sequential steps.

#### Task Management Levels

| Who assigns | For whom | What type |
|---|---|---|
| PM | Dev agent | development tickets (features, bugs, sprints) |
| CEO | any agent | operational enterprise subtasks |

#### Connection with Tiers

- **Tier 0/1**: CEO knows who does it → `assign_task` direct to agent
- **Tier 2**: CEO doesn't know who can → puts on shared table → claim model

### `create_ticket` — Global Tool

Extended from PM-only to support any agent:

```python
@tool
def create_ticket(
    title: str,
    description: str,
    assigned_to: str,       # "finance", "secretary", "dev", "sales", etc.
    parent_task: str | None,
    priority: str | None,
) -> dict:
    """Creates an operational ticket assignable to any agent."""
```

PM always uses `assigned_to="dev"` (its scope does not change). CEO uses `assigned_to` with any agent.

### Parent Task Tracking

Every large task that fragments (parallel or sequential) generates a **parent task** in MongoDB. Subtasks, workflow_runs, or discovery_runs are children referencing the parent.

```
Parent task: "close the month"  (MongoDB, system level)
  │
  ├── Parallel case:
  │   ├── ticket: "accounting close"      → assigned_to: finance
  │   ├── ticket: "monthly report"        → assigned_to: finance
  │   └── ticket: "notify clients"        → assigned_to: secretary
  │
  ├── Sequential case:
  │   └── workflow_run: cierre_mes_completo → references the YAML
  │
  └── Discovery case:
      └── discovery_run: discovery state (BLOCKED_TOOL, etc.)
```

The parent task always exists — it gives visibility of total progress. Children can be of different types depending on how the CEO resolved the task.

#### Notion Sync

Notion uses a **Relation property** pointing to the same database. Flat in the DB but linked via Relation. The sync creates Relations automatically when `parent_task_id` has a value. Notion allows filtering by `parent_task` to see all children of a task.

---

## Routing And Workflow Resolution

- CEO routing in `openceo-core/router.py`: direct hint, `@agent` prefix, project-config routing rules from `project.yaml`, then optional cheap LLM fallback. If no project is provided, company-level defaults apply and routing falls through to LLM fallback or `default_agent`.
- Worker execution in `openceo-core/graph.py`: `dev`, `pm`, `finance`, and `secretary` are dispatched explicitly. Other routes escalate as unsupported.
- Finance-specific workflow resolution in `openceo-core/agents/finance/session.py`: deterministic matching against project config workflows and built-in aliases.
- Secretary-specific action extraction in `openceo-core/agents/secretary/session.py`: JSON or structured-text parsing first, optional LLM fallback second.

---

## ChatGPT MCP Orchestrator

OpenCEO now has two separate execution paths:

- Path A: ChatGPT + MCP. ChatGPT is the intelligence layer. It chooses the workflow, extracts and structures params, runs approval loops, and only then calls OpenCEO for deterministic execution.
- Path B: OpenWebUI + DeepSeek + LiveKit. OpenCEO keeps the existing routing, extraction, and guided flows in `router.py`, `graph.py`, and the agent sessions.

These paths never mix inside the same request. A workflow exposed in MCP must be the direct-execution variant for Path A, not the AI-extraction variant that Path B uses internally.

### Intelligence Before Execution

- ChatGPT must preprocess any user content it can already understand before calling `execute_workflow`.
- OpenCEO should not repeat extraction, routing, or content processing that ChatGPT already completed.
- PM is the reference example: in ChatGPT path, use `pm.create_tickets` with structured tickets. Only use the extraction variant if the user explicitly wants OpenCEO to do the extraction itself.
- This same direct-vs-extraction rule generalizes to every future workflow where ChatGPT can produce a ready-to-run payload.

### Approval Loops

- Any workflow with `requires_approval=true` must be summarized back to the user before execution.
- ChatGPT waits for explicit confirmation before calling `execute_workflow`.
- This applies to payments, emails, Notion writes, and any other real-world or irreversible external action.

### MCP Tools

Absolute architecture law:

- Business capabilities remain workflow-first. UI is never a reason to promote a business capability to a new first-level MCP tool.
- MCP Apps belong to the presentation layer only. They must wrap or assist an existing execution contract; they must not redefine the business contract.
- The stable execution surface for business actions remains the workflow catalog behind `execute_workflow`.
- Any exception used for prototyping must be documented as transitional and must not be treated as the target architecture.

Core execution tools:

- `get_company_context`: returns the live agent/workflow catalog plus active projects. ChatGPT should call this once at conversation start and cache it. For any actionable request or capability question, this catalog is the mandatory discovery source before ChatGPT decides what OpenCEO can or cannot do. If a compatible workflow exists, discovery should continue into approval/execution rather than end in a local-only draft. When the workflow is already known and the remaining gaps are confirmable/selectable by the connector, ChatGPT should move into `execute_workflow` instead of keeping the whole remaining loop manually in chat.
- `get_user_context`: returns the current user's active project, active dev session, and running background jobs. ChatGPT should call this on demand only, when the request depends on current state such as active project, active dev session, pending work, or task status. `active_project` is advisory context, not a silent default for project-sensitive write workflows.
- `execute_workflow`: runs one already-resolved workflow directly, without using the CEO router or graph fallback path. This is the single execution entrypoint for real OpenCEO actions once ChatGPT has already discovered and prepared the correct workflow. The intended behavior is MCP-first: when the cached catalog already shows a compatible connected workflow, ChatGPT should prefer executing it over local-only behavior and, for approval-required side effects, move into a compact approval step instead of stopping at a full drafted artifact. Once the workflow is known, `execute_workflow` is also the right place to let the connector resolve the last confirmable/selectable params through `needs_input` or MCP Apps.
- `get_task_status`: reads one async task from MongoDB so ChatGPT can report progress later.
- PM safety rule: `pm.create_tickets` and other project-sensitive writes must not fall back silently to `active_project`. If the project was not explicitly provided, the connector should force confirmation or UI selection.
- `pm.create_tickets` is the correct public business name on the external MCP path: it registers one canonical Mongo task per ticket with `autorun=false`, keeps it in the manual dev queue, and lets the global mirror worker synchronize the minimum human-facing copy in the background.

### MCP Apps Presentation

- The external MCP connector should use MCP Apps standard for presentation. Do not build new ChatGPT-only widgets for this path.
- This UI layer remains optional and must not become a runtime dependency or architectural foundation. The durable contract stays MCP tools + deterministic workflows + `structuredContent`.
- If the UI disappears, the workflow must still be operable through plain tool calls and text fallback.
- The first standard app now hangs from `execute_workflow` for `pm.create_tickets`, where it is used to review the PM batch and confirm project selection before canonical Mongo registration and automatic Notion mirroring.
- Client compatibility depends on MCP Apps support in the host. When a client does not support it, the connector falls back to text and structured data.
- Host-specific APIs such as `widgetState`, `requestModal()`, `requestClose()`, OpenAI tool invocation strings, or ChatGPT file upload bridges are intentionally out of scope for this connector.
- MCP Apps bind presentation to MCP tools, not to internal workflow identifiers. The architectural response is to attach presentation to an existing execution tool, not to invent new business tools.

### UI Line

- Every future MCP App should inherit the host theme first through MCP Apps host styles, theme, and fonts.
- Default visual direction: dark neutral surfaces, grayscale hierarchy, restrained spacing, and subtle rounding.
- Avoid bright multi-color gradients, decorative hero sections, playful illustrations, glassmorphism, or white cards.
- Prefer operator-style layouts: single-column forms, compact status chips, quiet borders, and one sparse accent reserved for the primary action.
- If host variables are absent, use this neutral fallback palette: background `#212121`, surface `#2a2a2a`, elevated surface `#303030`, border `#3f3f46`, primary text `#ececec`, muted text `#a1a1aa`, accent `#10a37f`.

The intended ChatGPT-path behavior is documented in `openceo-core/agents/ceo/chatgpt_system_prompt.md`, but the live external connector guidance exposed to ChatGPT comes primarily from the tool metadata in `apps-sdk-mcp-test/server.js`.

---

## Agents

### CEO / Supervisor

Role: Receives all tasks and routes them to the correct worker agent + project.

Routing behavior:
- Message contains `@agent_name` prefix → route directly when the alias exists.
- No prefix → config-driven routing rules from `project.yaml routing.agents`, then optional cheap LLM fallback (`DEEPSEEK_API_KEY`) only if the config rules did not decide.
- The router understands `dev`, `pm`, `finance`, `secretary`, `sales`, `marketing`, and `media`; the worker graph dispatches the wired runtime workers.

Administrative commands (owner only, deterministic, zero tokens):
- `/status` and the OpenWebUI-compatible project/session commands handled in `main.py`

### Dev & DevOps Agent

Role: Codes, deploys, maintains CI/CD, manages all software projects.

Flow:
- Peer programming runtime with persistent tmux sessions
- Explicit session activation from OpenWebUI-compatible chat
- Providers: `codex`, `claude`, `gemini`, `opencode`, `glm`
- `tier_s` / `critical_consensus` multi-AI review flow with Claude lead + Codex/Gemini/OpenCode reviewers
- Owner-curated internal MCP tools injected into coding providers during sessions

Two modes:
- Peer programming: synchronous via LiveKit voice or OpenWebUI.
- Autonomous: async. Full ticket delegation with notification on completion.

Project memory: Each project has a `CONTEXT.md` file that the Dev Agent reads before starting and updates after completing each task.

### PM Agent

Role: Extract actionable tickets from client interviews and requirements documents.

Flow:
- Guided multi-turn chat flow via OpenWebUI: absorbs project, tool, and interview in any order; asks numbered menus or prompts only for missing info; confirmation starts extraction in background
- Intent-only phrases such as `tengo una entrevista` or `quiero planificar requerimientos` start the PM workflow but do not become the stored interview content
- CEO also routes substantive requirement/interview payloads directly into PM, so employees can paste the interview first and the chat will still guide the missing fields
- If the interview arrives before project or tool, PM keeps it in `pm_state` and continues guiding until the missing fields are complete
- When only the interview is missing, PM explicitly says which project and tool are already registered and asks for only the interview, so the user does not think they must repeat everything
- After confirmation, PM answers immediately, keeps the job state in `pm_state`, and the employee can ask naturally `ya terminó` or `qué tickets creó` without blocking the chat
- Three extraction engines: `langraph` (DeepSeek direct — cheap/fast), `codex` (Codex CLI headless + MCP), `claude` (Claude Code headless + MCP)
- Tool selection cascade: `params.pm_tool` → `project.yaml pm.default_tool` → hardcoded `langraph`
- DeepSeek extraction: single LLM call with structured JSON output → `create_tickets_batch()` direct Python call (no MCP overhead)
- Claude/Codex extraction: headless CLI subprocess with MCP `notion_create_pm_tickets_batch`
- Direct API endpoint: `POST /api/pm/extract` accepts `{project, interview, tool}` — deterministic, no guided flow, ready for dashboard forms
- Dashboard status endpoint: `GET /api/pm/status?user_id=...` returns the current PM background state for the chat user
- PM state persisted in MongoDB (`user_context.pm_state`) so multi-turn flow survives between messages and dashboard polling can read the same job state
- Model name in OpenWebUI: `openceo-pm` or `pm`

### Secretary Agent

Role: Email sending and lightweight coordination tasks.

Flow:
- Parses email payloads from JSON, structured text, or optional LLM fallback
- Uses shared SMTP sender wrappers in `agents/secretary/tools.py`
- Supports sender account selection, `language=es|en`, reply-to, CC/BCC, and file attachments
- Uses institutional signature rendering from `tools/email_sender.py`

### Finance Agent

Role: Deterministic browser-assisted payment workflows.

Flow:
- Deterministic Playwright workflows for browser-based payment portals
- Automation navigates the portal, selects the oldest pending invoice, reaches the card screen, and stops before any sensitive confirmation
- Finance secrets are intentionally split into `openceo-core/.env.finance.local`

---

## Inter-Agent Communication: Peer-to-Peer

OpenCEO uses **peer-to-peer** communication between agents during Tier 2 discovery — not lead-centric relay.

### Why Not Lead-Centric

In lead-centric architecture, everything passes through the CEO: Finance finishes → returns to CEO → CEO decides to call Secretary → Secretary executes. Each relay consumes a full LLM cycle = tokens + unnecessary latency.

### Peer-to-Peer Model

Agents communicate directly when the task requires it:

```
CEO detects complex task
      ↓
CEO initiates: Finance + Secretary (parallel or sequence)
      ↓
Finance finishes → passes result direct to Secretary (without returning to CEO)
      ↓
Secretary generates report with Finance data
      ↓
CEO receives final result
```

### Where Peer-to-Peer Applies

1. **Discovery chains**: Finance discovers next step is a report → passes directly to Secretary
2. **Cross-agent unblocking**: Finance is blocked waiting for data → asks Secretary directly
3. **Error/fallback**: Agent A fails → asks Agent B directly if it can cover the missing data
4. **Long task sync**: agents synchronize state between themselves without spending CEO cycles

### Where It Does NOT Apply

- **Crystallized workflows (YAML)**: the executor calls Python functions directly — no inter-agent communication, only sequential tool execution
- **Task initiation**: the CEO decides which agents participate and gives them the objective — that is top-down. Once active, communication between them is peer-to-peer

### Claim Model — Tier 2 Discovery

When an agent (or CEO) needs capabilities it doesn't have:

```
Finance cannot complete its task alone
      ↓
Finance puts subtask on shared table:
  "generate a report with this financial data"
      ↓
Each agent evaluates against ITS OWN tools:
  Finance:   "can I? my tools are binance_api, bank_transfer... no"
  Secretary: "can I? my tools are send_email, generate_doc... yes" → CLAIM
      ↓
Secretary executes with its primitives
```

**Atomic claim** prevents double-claim: temp file → fsync → rename → fsync(dir). First wins.

| Edge case | What happens |
|---|---|
| Nobody claims | timeout → escalates to human |
| Two agents claim | atomic claim, first wins |
| Wrong agent claims | tries, fails, releases → another takes it |

**Where claim applies:** only Tier 2 (discovery with primitives, peer-to-peer cross-agent coordination). Tiers 0, 1, 1.5: NO claim — CEO resolves directly.

### Implementation Reference

Inspired by OpenCode team functions:
- `team_message`: direct agent → agent message
- `team_claim`: atomic subtask claim from shared pool (prevents double-claim)
- Messages in JSONL append-only (O(1) write) + session injection (no polling)

---

## Proactive CEO — Autonomous Objectives

Everything else in this architecture is **reactive**: a task arrives, the system processes it. This layer makes OpenCEO **proactive**: the CEO generates work without anyone asking.

### Hierarchy

```
Objectives (MongoDB)
      ↓ CEO generates tasks automatically
Tasks (existing system)
      ↓ tiers 0-2
Workflows / Discovery
      ↓
Execution
```

Objectives are NOT tasks. Objectives generate tasks.

### Objective Schema (MongoDB)

```json
{
  "objective_id": "obj_001",
  "description": "Reduce operational costs by 15% this quarter",
  "priority": "high",
  "deadline": "2026-06-30",
  "progress": 0.35,
  "status": "active",
  "max_active_tickets": 3,
  "kpis": [
    {"metric": "monthly_cost", "target": 5000, "current": 5800}
  ]
}
```

### CEO Autonomous Loop — Two Triggers

**Periodic** (e.g., every Monday, daily at 8am): CEO reviews active objectives → deadline near + low progress? → creates specific tickets.

**Reactive** (when a workflow completes): CEO evaluates → did this advance an objective? → updates progress → needs more? → creates next ticket. Objective met? → marks complete.

### Anti-Bloat Rules

1. **Max N active tickets per objective** — no new tickets if existing ones are incomplete
2. **Concrete action required** — not "improve finances" but "generate Q1 expense report and compare with Q4"
3. **Check existing tickets first** — no duplicates
4. **Measurable KPIs** — ticket must move the indicator. If not, it's not created
5. **Scope check** — ticket must align with the objective. If it deviates, discard

### Flow

```
Active objectives in MongoDB
      ↓
CEO loop (periodic or reactive)
      ↓
Reviews progress vs KPIs vs deadline
      ↓
Enough active tickets? → don't create more
Missing concrete progress? → create ticket with specific action
      ↓
Ticket enters normal flow (tiers 0-2)
      ↓
Workflow executes
      ↓
Result updates objective progress
      ↓
Loop continues until objective met or deadline
```

---

## Internal Scheduler — Recurring Tasks & Reminders

OpenCEO has its own scheduler independent of n8n. Users create schedules via natural language (chat or voice).

### Schedule Schema (MongoDB)

```json
{
  "schedule_id": "sch_001",
  "cron": "0 8 * * 4",
  "workflow": "secretary.send_telegram",
  "params": {"user": "jorge", "message": "buy meat"},
  "project": null,
  "status": "active"
}
```

### Single Type: recurring_workflow

No separate types for reminders vs recurring workflows. A reminder is a single-step workflow (`secretary.send_telegram`). Everything uses the same mechanism.

### Natural Language Creation

```
"remind me every Thursday at 8am to buy meat"
→ CEO parses: cron=0 8 * * 4, workflow=secretary.send_telegram

"schedule salary payments on the first of each month"
→ CEO parses: cron=0 0 1 * *, workflow=finance.pago_sueldos

"sync business revenue every night at midnight"
→ CEO parses: cron=0 0 * * *, workflow=finance.sync_ingresos
```

If the CEO doesn't find a crystallized workflow, it enters the normal tiers. If a new workflow is discovered and crystallized, the schedule points to it.

### Cron Runner

Internal process inside FastAPI — checks every minute:

```python
async def cron_runner():
    while True:
        now = datetime.now()
        schedules = db.schedules.find({"status": "active"})
        for s in schedules:
            if cron_matches(s["cron"], now):
                execute_workflow(s["workflow"], s.get("params"), s.get("project"))
        await asyncio.sleep(60)
```

### Personal vs Enterprise

The difference is only `project`:
- `project: null` → personal (reminders, external work tasks)
- `project: "jardal-openceo"` → enterprise

Same scheduler, with or without project context.

---

## Tier S (Multi-AI Peer Review)

For high-stakes tickets (systems handling real money, critical infrastructure, complex algorithms, anything where a single bug has severe consequences), the Dev Agent activates a multi-AI peer review workflow. Claude Code acts as lead programmer. Codex and Gemini act as persistent tmux-based reviewers. OpenCode remains a single reviewer slot from Claude's perspective, but internally it runs a headless multi-model review pipeline and compiles its own consensus into `FEEDBACK-opencode.md`. The Dev Agent (`dev_session_service.py` for chat sessions, `session.py` for legacy task runs) orchestrates everything deterministically — it follows workflow steps, it does not think or generate content.

### Principles

- Claude Code does ALL intellectual work: planning, decomposing tickets into scripts, coding, generating review context (BRIEF.md), reading feedback, and correcting code.
- The Dev Agent is a simple orchestrator (DeepSeek-based). It launches tmux sessions, sends commands via `send-keys`, polls for completion signals, and cleans files between cycles. It never generates code, context, or decisions.
- Reviewers receive a role-primed prompt that establishes them as elite senior engineers. Their feedback is unidirectional toward Claude — they write it, Claude reads it and decides what to accept or reject internally. There is no RESOLUTION.md, no back-and-forth, no bidirectional conversation.
- Each script goes through exactly 2 review iterations. Iteration 1 is full review. Iteration 2 is validation of corrections.
- Claude/Codex/Gemini tmux sessions stay alive for the entire ticket to preserve conversation context across scripts.
- OpenCode preserves context differently: each internal reviewer and the compiler persist their own `sessionID` from JSON output and reuse it with `--session` across iterations and scripts within the same Tier S ticket.

### File Structure

Files live in the real repository root selected for the project (for example `/home/develop/development/Jardal/jardal-openceo`). OpenCode also uses a transient internal artifacts directory for its own reviewer fan-out.

```
{project_repo_root}/
├── CONTEXT.md              # permanent project context, updated after each ticket
├── CURRENT_PLAN.md         # Claude creates at ticket start: objective + ordered list of scripts
├── BRIEF.md                # Claude generates after each script: context + plan + script path for reviewers
├── .consensus-status       # orchestration signal file written by Claude
├── FEEDBACK-codex.md       # reviewer feedback, cleaned before each iteration cycle
├── FEEDBACK-gemini.md
├── FEEDBACK-opencode.md
├── .opencode-team-review/  # transient OpenCode reviewer artifacts per step/iteration
├── .opencode-session-opencode-reviewer-*.id
├── .opencode-session-opencode-compiler.id
└── ...                     # code lives where it belongs, never copied
```

### tmux Sessions — Independent Per Project

Each coding tool keeps an independent project session. Claude, Codex, and Gemini are real tmux conversations. OpenCode keeps a project tmux shell for symmetry with the rest of the runtime, but its Tier S reviews run as headless `opencode run` subprocesses and do not depend on typing into the tmux pane.

```
dev-{project}-claude        # lead programmer, always active
dev-{project}-codex         # reviewer
dev-{project}-gemini        # reviewer
dev-{project}-opencode      # OpenCode project shell; review work is executed headlessly
```

One session per tool per project. `tmux ls` shows all sessions cleanly. Kill one reviewer without affecting others: `tmux kill-session -t dev-{project}-codex`. Kill all for a project: iterate over the 4 session names. Ticket completion and session closure are different events: finishing a Tier S ticket does not automatically destroy the project sessions, but explicitly closing or canceling the dev session does tear down the 4 tmux sessions for that project. When that happens, OpenCode also deletes its persisted `.opencode-session-*.id` files so the next Tier S session starts with a fresh internal review context.

### Completion Signals

Each phase uses a unique signal that the Dev Agent polls for in the corresponding file. Signals are designed to avoid false positives across iterations:

- Claude after implementing + generating BRIEF.md: `.consensus-status = STEP_COMPLETE_{step_number}` (e.g. `STEP_COMPLETE_1`)
- Reviewers after writing feedback: `REVIEW_DONE_{step_number}_{iteration}` (e.g. `REVIEW_DONE_1_1` for step 1, iteration 1)
- Claude after correcting from feedback: `.consensus-status = CORRECTION_DONE_{step_number}_{iteration}` (e.g. `CORRECTION_DONE_1_1`)
- Ticket closure: `.consensus-status = TICKET_COMPLETE`

Step number increments per script. Iteration is 1 or 2. This guarantees the Dev Agent never confuses a signal from a previous cycle with the current one.

### Reviewer System Prompt

Codex and Gemini receive a role-establishing prompt that maximizes review quality without constraining their thinking to specific categories. OpenCode uses a different mechanism: five headless reviewers with a shared structure but model-specific `Suggested strength areas`, `context_focus`, and `specialty`, all defined in `openceo-core/agents/dev/consensus.py`. Those strengths are hints, not scope limits.

```
You are one of the most experienced senior software engineers in the world.
You have mass-scale production experience, decades of debugging the hardest systems,
and an obsessive attention to detail. Your reputation is built on finding the problems
that everyone else misses. You do not let anything slide. You do not give superficial reviews.
You treat every review as if the code will run unsupervised in a high-stakes production
environment where failures have real consequences.

Read BRIEF.md for the full context: the project objective, the implementation plan,
and the path to the script under review. Open the script and review it with absolute rigor.

If it is perfect, say it is perfect. If it has problems, tear it apart. Be specific:
identify the exact location, explain the problem, and suggest the fix.
Do not hold back. Do not be polite about bad code.
Write your complete analysis in FEEDBACK-{name}.md.
When you are finished, write REVIEW_DONE_{step}_{iteration} as the last line.
```

This prompt does three things: (1) assigns an elite identity that the AI will try to live up to, (2) gives complete freedom to find any type of problem without limiting to a checklist, (3) demands specificity so the feedback is actionable, not vague.

### Flow — Step by Step

**Phase 0 — Dev Agent launches sessions.**
Creates the 4 independent project sessions. Positions each in the project directory. Launches Claude Code in the claude session. Launches Codex/Gemini in their reviewer sessions with their reviewer system prompt loaded. Keeps the OpenCode project shell available, but OpenCode Tier S review execution itself happens later as headless subprocesses. Claude/Codex/Gemini sessions stay alive for the duration of the ticket; OpenCode context persists via saved `sessionID` files.

**Phase 1 — Claude plans, decomposes, and implements first script.**
Dev Agent sends to claude session:

```
Read CONTEXT.md.
Ticket: {ticket_description}

Create CURRENT_PLAN.md with:
- The ticket objective
- An ordered list of scripts to implement, one per step
- For each script: what it does, what it depends on, what risks you see

Implement ONLY the first script from the plan.

After implementing, generate BRIEF.md with:
- The ticket objective (from the ticket description)
- Your plan (from CURRENT_PLAN.md)
- The path to the script you just wrote (NOT the code itself, just the path so reviewers open it)
- The relevant sections from CONTEXT.md that a reviewer would need to understand the script

When everything is ready, overwrite .consensus-status with STEP_COMPLETE_1 and print STEP_COMPLETE_1
```

Claude generates everything: the plan, the code, and the BRIEF.md. The Dev Agent only waits for the signal.

**Phase 2 — Dev Agent triggers reviewers (iteration 1).**
Dev Agent detects `STEP_COMPLETE_{N}`. Deletes the 3 feedback files (ensures clean state). Sends Codex and Gemini their review instruction via tmux. For OpenCode, it launches a headless internal review pipeline:

- batch 1: `GLM-5 + Kimi K2.5`
- batch 2: `MiniMax M2.5 + MiniMax M2.7`
- `DeepSeek Reasoner` runs in parallel from the beginning
- once the 5 reviewer artifacts exist, a standalone MiniMax M2.7 compiler deduplicates them into `FEEDBACK-opencode.md`

Codex and Gemini still receive the classic prompt:

```
Read BRIEF.md and open the script indicated there. Review it completely.
Write your analysis in FEEDBACK-{name}.md.
When finished, write REVIEW_DONE_{step}_1 as the last line.
```

From Claude's perspective there are still only 3 reviewer outputs: `FEEDBACK-codex.md`, `FEEDBACK-gemini.md`, and `FEEDBACK-opencode.md`.

**Phase 3 — Dev Agent waits.**
Polls for `REVIEW_DONE_{step}_1` in each of the 3 feedback files. Codex and Gemini are monitored through their feedback files as before. OpenCode waits for its internal pipeline to finish and for `FEEDBACK-opencode.md` to end with the expected signal. Each OpenCode subprocess can run up to 10 minutes before forced termination.

**Phase 4 — Claude reads feedback and corrects.**
Dev Agent sends to claude session:

```
Reviewers finished. Read FEEDBACK-codex.md, FEEDBACK-gemini.md, FEEDBACK-opencode.md.
Correct what you consider valid in the script.
When done, overwrite .consensus-status with CORRECTION_DONE_{step}_1 and print it
```

Claude reads all feedback, decides internally what to accept or reject, and applies corrections directly to the code. No documentation of what was accepted or rejected — the corrected code is the resolution.

**Phase 5 — Iteration 2 (validation pass).**
Dev Agent detects `CORRECTION_DONE_{step}_1`. Deletes the 3 feedback files again. Sends Codex/Gemini their second-pass review prompt and re-runs the OpenCode headless pipeline on the corrected target. OpenCode reuses saved `sessionID`s for its internal reviewers and compiler so it can keep context across the ticket while still executing each review as a standalone subprocess.
No new BRIEF.md is generated. Reviewers already have full context from iteration 1. They just re-read the corrected script.

Dev Agent waits for `REVIEW_DONE_{step}_2`, then sends to Claude:

```
Second review completed. Read FEEDBACK-codex.md, FEEDBACK-gemini.md, FEEDBACK-opencode.md.
Apply final corrections if needed.
When done, overwrite .consensus-status with CORRECTION_DONE_{step}_2 and print it
```

**Phase 6 — Next script.**
Dev Agent sends to claude session:

```
Script {N} completed with 2 review iterations.
Update CURRENT_PLAN.md marking this step as done.
Implement the next script from the plan.
After implementing, generate a new BRIEF.md for the new script.
When ready, overwrite .consensus-status with STEP_COMPLETE_{N+1} and print it
```

Feedback files are cleaned at the start of the next iteration cycle. BRIEF.md is overwritten by Claude with the context for the new script. Repeat from Phase 2 for each script in the plan.

**Phase 7 — Ticket closure.**
After the last script completes both iterations, Dev Agent sends to Claude:

```
All scripts completed. Update CONTEXT.md with what was implemented.
Overwrite .consensus-status with TICKET_COMPLETE and print TICKET_COMPLETE.
```

Dev Agent closes the ticket, sends notifications. The 4 tmux sessions stay alive for the next ticket on the same project (preserving context). If the user later closes or cancels the dev session, OpenCEO tears down those tmux sessions and, for the OpenCode reviewer slot, also removes the persisted `.opencode-session-opencode-reviewer-*.id` and `.opencode-session-opencode-compiler.id` files.

### Who Decomposes Large Tickets

The PM or a human creates tickets at the functional level ("implement trading bot", "design database schema", "build payment pipeline"). Claude as lead programmer receives the functional ticket and decomposes it into individual scripts inside CURRENT_PLAN.md. This is a technical decision — the PM does not need to know which files are needed. Claude decides the architecture and the implementation order. Each script in the plan goes through the full 2-iteration review cycle independently.

### Configuration in project.yaml

```yaml
dev:
  coding_tool: codex | claude | tier_s
  tier_s:
    enabled: true
    iterations: 2
    timeout_minutes: 15
    max_steps: 12
    lead: claude
    reviewers:
      - codex
      - gemini
      - opencode
    keep_sessions_alive: true
```

The `coding_tool` field replaces the old `coding_tier` as the runtime default. The owner selects which tool to use per project: `codex` for Codex CLI, `claude` for Claude Code, or `tier_s` for the multi-AI review flow. The legacy alias `critical_consensus` is still accepted for backward compatibility. In chat, an explicit provider request like `crea una sesión codex` still overrides the project default for that session.

For the legacy task queue only, an explicit `coding_tier: tier3` still has a meaning: it maps to a one-shot OpenCode run using `opencode run --format json --agent build`. That legacy tier mapping does not modify the persistent `openceo-dev` chat sessions.

```yaml
pm:
  default_tool: langraph | claude | codex
```

The `pm.default_tool` sets the default extraction engine for ticket extraction from interviews. `langraph` = DeepSeek direct (cheapest, covers 90% of cases). `claude`/`codex` = headless CLI subprocess with MCP. Can be overridden per-request via `params.pm_tool` or by mentioning the tool in the chat message.

---

## Rate Limit Management

Claude Code and Codex CLI run on subscription-based rate limits with two tiers: a **5-hour rolling window** and a **weekly cap**. When multiple tickets queue up (30+ from a single sprint), OpenCEO must maximize usage without burning everything at once and without blocking the system when a limit hits mid-task.

### Design Principles

- **OpenCEO owns the intelligence of waiting.** The coding CLIs do not know about queues, concurrency, or reserves. OpenCEO decides when to launch, when to pause, and when to resume.
- **Never kill tmux sessions for rate limits.** When a CLI hits its limit, the TUI stays alive but blocked. The Dev Agent detects this, waits, and resumes in the same session.
- **Dedicated monitor sessions** (`dev-monitor-codex`, `dev-monitor-claude`) check limits without interfering with working sessions.
- **All times normalized to UTC.** The server is UTC-4 but all internal tracking is UTC.

### Provider Limit Tracking

`rate_monitor.py` manages two independent tmux sessions purely for polling CLI limit status:

```
dev-monitor-codex     → sends /status, parses "XX% left (resets HH:MM)"
dev-monitor-claude    → sends /usage, parses "XX% used", inverts to remaining
```

Parsed data is cached in-memory (refreshed every 5 minutes or after each ticket completion) and persisted to MongoDB `agent_status` collection:

```python
ProviderStatus:
    provider: str           # "codex" | "claude"
    remaining_5h: float     # 0.0 to 1.0 (percentage remaining)
    remaining_weekly: float # 0.0 to 1.0
    resets_5h_at: datetime  # UTC
    resets_weekly_at: datetime  # UTC
```

Gemini and OpenCode have no subscription limits tracked — they are treated as always available.

### Reserve Policy

Each provider has configurable reserve thresholds. A ticket can only launch if remaining quota exceeds the reserve:

| Provider | 5h Reserve | Weekly Reserve | Rationale |
|---|---|---|---|
| Claude | 0% | 0% | OpenCEO owns Claude completely. No manual use. |
| Codex | 20% | 5% | Keep headroom for manual owner use. |

### Concurrency Slots

```
RATE_MAX_CONCURRENT_TICKETS = 3

Tier S ticket = 3 slots (uses Claude + Codex + reviewers simultaneously)
Normal ticket = 1 slot
```

A ticket can launch only if `used_slots + needed_slots <= max_concurrent`.

### Pre-Launch Gating

When `TASK_AUTORUN=true` and a new ticket arrives:

```
1. Check concurrency: are slots available?
2. Check provider limits: remaining > reserve for the required provider(s)?
3. If both pass → run immediately
4. If either fails → status="queued", resume_at=earliest_reset_time
5. Scheduler retries queued tickets every WAITING_POLL_SECONDS
6. After every ticket completion → refresh limits → try launching queued tickets
```

Tier S tickets check both Claude (lead) and Codex (reviewer) limits before launching. Both must pass.

### Mid-Task Rate Limit Handling

When a coding tool hits its limit during execution, the TUI stays alive but shows a rate limit message. The polling loop in `tmux_runtime.py` detects this:

**Claude hit:**
```
1. Detect "What do you want to do?" + "Stop and wait for limit to reset"
2. Send "1" to select the wait option
3. Parse reset time from "Resets Xam/pm (UTC)"
4. Sleep until reset + 2 min safety margin
5. Send continuation prompt: "Continue the task where you left off..."
6. Resume normal polling
```

**Codex hit:**
```
1. Detect "You've reached your 5-hour message limit"
2. Parse "Try again in Xh Xm"
3. Sleep until reset + 2 min safety margin
4. Press Enter to resume
5. Resume normal polling
```

The flow is recursive: if a second rate limit triggers after resuming, the same handler runs again.

### Crash Recovery (Exception Case)

Only for the rare case where the process actually dies (shell prompt appears instead of TUI):

```
1. detect shell prompt via capture-pane (regex: user@host:path$)
2. Codex: run "codex resume {saved_session_id}" in the same tmux session
3. Claude: relaunch CLI, send "Continue working, read CURRENT_PLAN.md and .consensus-status"
```

Session IDs for Codex are captured from `/status` output at monitor initialization and saved in MongoDB for crash recovery.

### Configuration

```bash
# Rate limit management
RATE_MAX_CONCURRENT_TICKETS=3
RATE_CODEX_RESERVE_5H=0.20
RATE_CODEX_RESERVE_WEEKLY=0.05
RATE_CLAUDE_RESERVE_5H=0.0
RATE_CLAUDE_RESERVE_WEEKLY=0.0
RATE_TIER_S_WEIGHT=3
RATE_NORMAL_WEIGHT=1
RATE_STATUS_STALE_SECONDS=300
```

### Files

- `openceo-core/rate_monitor.py` — All monitoring, parsing, gating, and detection logic
- `openceo-core/task_service.py` — Pre-launch gating + post-completion hook
- `openceo-core/scheduler.py` — Periodic retry of queued tickets
- `openceo-core/agents/dev/tmux_runtime.py` — Mid-task detection + wait-and-resume in polling loop
- `openceo-core/agents/dev/consensus.py` — Pre-launch check for Tier S

---

## MCP Server — Three Types

OpenCEO has three MCP scopes:

| Type | Scope | Consumer | Example |
|---|---|---|---|
| **MCP Internal** | Inside OpenCEO | Agents during coding sessions | Claude Code / Codex CLI use DNS, Notion tools |
| **MCP Global** | Entire VM | Any project on this machine | Infrastructure tools (DNS) available everywhere |
| **MCP External** | Outside OpenCEO | External clients (ChatGPT, other LLMs) | ChatGPT calls `secretary.send_email` via MCP |

The MCP External exposes tools from the TOOL_REGISTRY (primitives and workflows) to external consumers. Tool naming follows the `agent.tool_name` convention from the registry.

### Internal + Global: Coding Agent Infrastructure Tools

OpenCEO exposes two MCP mount points **inside `openceo-core`** (same process, same port) for Claude Code and Codex CLI to use during coding sessions. The split keeps infrastructure tools universally available while scoping project-specific tools to OpenCEO sessions only.

### Dual Mount Architecture

| Mount point | Default path | Scope | Tools |
|---|---|---|---|
| **Global MCP** | `/internal/mcp-global` | Entire VM — any project on this machine | Infrastructure only (DNS) |
| **Internal MCP** | `/internal/mcp` | OpenCEO-managed coding runtimes only | Infrastructure + OpenCEO-specific (DNS, Notion) |

**Why two endpoints?** Claude Code and Codex CLI load MCP tool schemas on every request. If Notion tools are exposed globally, they pollute the context when working on projects that use Jira or other ticketing systems. The global MCP carries only universal DevOps tools (DNS). The internal MCP adds OpenCEO-specific tools (Notion tickets) and is injected only into OpenCEO-managed coding runtimes via `build_mcp_provider_env()` or explicit MCP config.

Both endpoints share the same security layer (loopback IP restriction + optional Bearer token), the same FastMCP transport, and the same `mcp_server.py` registration functions.

### Design Principles

- **Runs inside openceo-core process.** Same Python process, same `openceo-core/.env`. No extra container, no extra port.
- **Owner-curated tool list.** NOT every workflow or API is exposed. Only infrastructure tools that coding agents need during DevOps tasks. The owner explicitly decides which tools appear in `mcp_server.py`. This keeps the MCP tool list small and avoids bloating the coding agent's context window.
- **Shared Python functions.** The MCP tools call the same functions in `openceo-core/tools/` that LangGraph agents can also import. One implementation, two access paths.

### Exposed Tools

**Global MCP** (`/internal/mcp-global`) — configured in Claude Code / Codex CLI settings for the whole VM:

| Tool | Purpose |
|---|---|
| `manage_dns_check_or_create(hostname)` | Cloudflare DNS: create/verify A record. Required before Traefik deploy. |

**Internal MCP** (`/internal/mcp`) — injected only into OpenCEO-managed coding runtimes:

| Tool | Purpose |
|---|---|
| `manage_dns_check_or_create(hostname)` | Cloudflare DNS: create/verify A record. Required before Traefik deploy. |
| `notion_create_ticket(title, ...)` | Create 1 ticket in Notion. Min input: title. Supports description, project, priority, status, tags, due_date. |
| `notion_create_tickets_batch(tickets)` | Create up to 50 tickets in one call. JSON array of objects with same fields as single create. Used by PM for sprint decomposition. |

**Design rule for MCP tool descriptions:** keep them as short as possible. Every token in a tool description is sent on every request to the coding CLI. Verbose descriptions waste subscription limits. The function parameter schema already documents the structure — no need for a separate discovery tool.

### What is NOT exposed

Everything else. Email, WhatsApp, CRM, payments, documents — these are agent workflows, not coding infrastructure. Notion tools are limited to ticket creation only — listing, querying, and lifecycle management happen through LangGraph APIs directly, not MCP. If a coding agent needs something new, the developer adds it explicitly to `mcp_server.py`.

### How coding agents connect

- **Global MCP:** Claude Code and Codex CLI are configured in their per-user settings (e.g., `~/.claude/settings.json`) to connect to `http://127.0.0.1:{port}/internal/mcp-global/`. Available in every project on the VM.
- **Internal MCP:** Injected automatically into OpenCEO-managed coding runtimes via environment variables (`OPENCEO_INTERNAL_MCP_URL`, `OPENCEO_INTERNAL_MCP_BEARER_TOKEN`) or explicit MCP config. No manual configuration needed — `build_mcp_provider_env()` / `build_mcp_provider_config()` handle it.

---

## WhatsApp Routing

Single WhatsApp number. Classification happens in n8n BEFORE reaching OpenCEO. Zero tokens.

```
WhatsApp message arrives at n8n
  → n8n looks up sender phone number in MongoDB:
      - Role = "internal" → POST /api/tasks with agent_hint=null (CEO routes)
      - Role = "client" → keyword classifier (support vs sales)
      - Not found → POST /api/tasks with agent_hint=sales (new lead)
```

Clients NEVER reach the CEO directly. Only internal contacts can interact with the CEO.

---

## Per-Project Configuration

Every project has a YAML config that fully defines its workflow. OpenCEO reads this and executes — no project-specific code exists in OpenCEO core.

```yaml
project:
  name: string
  repos:
    - name: string
      url: string
      branch_base: string

  sources:
    - type: jira | notion | github | telegram | voice | whatsapp
      trigger: n8n_webhook

  dev:
    coding_tool: codex | claude | tier_s
    branch_pattern: string
    auto_push: boolean
    create_pr: boolean
    tier_s:
      iterations: 2
      timeout_minutes: 15
      max_scripts_per_step: 1
      lead: claude
      reviewers:
        - codex
        - gemini
        - opencode
      keep_sessions_alive: true
    qa:
      playwright: required | optional | skip
      confidence_threshold: float
      max_retries: integer

  routing:
    llm_fallback: boolean
    default_agent: dev | pm | finance | secretary | sales | marketing | media
    agents:
      pm:
        triggers: ["entrevista", "requerimientos", "crear tickets"]
        patterns:
          - "\\b(?:crear|generar|extraer)\\s+tickets?\\b"
      secretary:
        triggers: ["correo", "reunion"]
        patterns: []
      finance:
        triggers: ["pagar factura", "pago luz"]
        patterns: []

  sales:
    workflows:
      - name: licitacion
        description: "Process bid/tender documents"
        triggers: ["licitacion", "licitación", "pliego", "propuesta", "tender"]
        steps:
          - action: analyze_document
            type: llm
          - action: generate_quote
            type: llm
            template: templates/cotizacion.md
          - action: compile_documents
            type: deterministic
          - action: send_via_email
            type: local_tool
            tool: send_email

  # Other agents follow the same pattern with local_tool, shared_tool, or n8n_workflow step types

  post_actions:
    - type: n8n_workflow
      workflow_id: string
      payload: object
    - type: browser_task
      platform: string
      action: string
    - type: next_agent
      agent: string
      workflow: string
      payload: object
    - type: webhook
      url: string
      payload: object

  notifications:
    - channel: telegram | whatsapp
      events: [completed, failed, escalated]
      message: string
```

Adding a new project = creating `projects/{name}/project.yaml` + `projects/{name}/CONTEXT.md`.

Adding a new business process = updating project YAML for the workers that already consume that config.

---

## Task Lifecycle Flow

- API/n8n-style tasks enter through `POST /api/tasks`.
- OpenWebUI-compatible chat and LiveKit voice enter through `POST /v1/chat/completions`.
- `router.py` selects an agent from config-driven rules plus optional LLM fallback; `graph.py` executes `dev`, `pm`, `finance`, or `secretary`, and escalates unsupported workers.
- Dev peer sessions can run long turns asynchronously in the background; PM guided chat also runs extraction in the background after confirmation, while finance and secretary remain deterministic request/response flows.
- On startup, Mongo-backed tasks are reconciled and tmux-backed dev sessions are reattached when possible.
- Tasks with `source` + `source_id` are idempotent: a second POST with the same pair returns the existing task. Enforced by a partial unique index in MongoDB.
- **Notion mirror on `task_created`:** canonical tasks created through `TaskService` now schedule an automatic Notion clone. Mongo remains canonical; Notion is a human-facing mirror.
- **Notion sync-back:** when a Notion-sourced task changes status (`in_progress`, `completed`, `failed`, etc.), `task_service.py` writes back to the originating Notion page via `tools/notion.py`. Properties updated: `Runtime Status`, `OpenCEO Ref`, `Runtime Summary`, `Last Sync At`, `Agent`. Sync-back is fire-and-forget and never blocks task execution. Controlled by `NOTION_SYNC_BACK_ENABLED`.
- Architecture law: MongoDB is the canonical task/work queue. Notion should only mirror the subset of fields needed for human visibility.
- Current exception: legacy PM extraction paths in OpenWebUI still create Notion pages directly and do not yet materialize one canonical Mongo `tasks` record per created PM ticket. The external MCP path `pm.create_tickets` is now Mongo-first.

---

## Dev Agent — Step-by-Step Session Flow

Persistent tmux runtime with explicit provider sessions. Deterministic Python orchestrates the flow around the coding CLI.

1. **Branch Setup** — checkout, pull, create feature branch.
2. **Code** — send task description. Coding tool uses CLAUDE.md/skills + MCP tools (DNS, Docker) if needed. If `coding_tool` is `tier_s`, execute the Tier S flow instead of steps 2-4. In the legacy one-shot ticket runner, `coding_tier: tier3` maps to `OpenCode build` for that ticket only.
3. **QA** — conditional on config. Run tests, retry on fail, escalate if exhausted.
4. **Push + PR** — conditional on config and confidence threshold.
5. **Update CONTEXT.md** — ticket-level closeout. If the user then closes the dev session, OpenCEO tears down the tmux runtime; in Tier S it also purges persisted OpenCode `sessionID` files so the next session starts clean.
6. **Post-actions + notifications** — handled by OpenCEO core.

---

## MongoDB Collections

- **tasks** — Canonical OpenCEO work queue and runtime task tracking. Supports `parent_task_id` for task fragmentation hierarchy
- **dev_sessions** — tmux-backed dev peer/autonomous sessions
- **user_context** — selected project + active dev-session focus per user/chat
- **agent_status** — provider quota and health data
- **browser_tasks** — reserved for browser/worker style queues
- **workflow_runs** — runtime execution state for crystallized workflows. Status: `PENDING`, `RUNNING`, `BLOCKED_TOOL`, `COMPLETED`, `FAILED`, `CANCELED`
- **discovery_runs** — discovery process state. Status includes `BLOCKED_TOOL`, `CANCELED_ADMIN`. Stores `blocked_at`, context for resume
- **objectives** — CEO autonomous objectives with KPIs, deadlines, and progress tracking
- **schedules** — recurring task definitions with cron expressions. Personal (`project: null`) or enterprise (`project: "name"`)
- **workflow_state** — Generic flexible storage for workflow-specific data that does not justify its own collection. Semi-structured: fixed envelope (`owner_type`, `owner_id`, `workflow`, `scope`, `kind`, `key`, `tags`, `expires_at`) with free-form `data` payload. Used for intermediate results, snapshots, agent notes, contact attempts, scraped content, and any state that varies per workflow. NOT for canonical runtime state, dashboard queries, or cross-agent filtering — those belong in structured collections.

### Data Modeling Strategy

OpenCEO uses three storage layers. Every piece of persistent data must go into exactly one.

#### Three Storage Layers

**Layer 1 — Structured collections** (the existing ones: `tasks`, `workflow_runs`, `schedules`, `user_context`, `agent_status`, `objectives`, etc.). These have implicit schemas enforced by the code that reads and writes them. They support indexed queries, dashboard rendering, cross-agent filtering, and lifecycle gating. Data here participates in runtime logic.

**Layer 2 — `workflow_state` collection.** A single generic collection with a fixed envelope and free-form `data` payload. Any workflow can store anything here without creating a new collection. The envelope provides enough structure for basic queries (`owner_id`, `workflow`, `kind`, `key`, `tags`) while `data` holds whatever the workflow needs.

**Layer 3 — Markdown files** (`CONTEXT.md`, `CURRENT_PLAN.md`, `BRIEF.md`). Narrative context that LLMs read as part of their prompt. Not queryable. Not runtime state. Human-readable project memory.

#### `workflow_state` Envelope Schema

```json
{
  "_id": "ws_001",
  "owner_type": "task | workflow_run | project | schedule",
  "owner_id": "task_123",
  "workflow": "agent.workflow_name",
  "scope": "run | global",
  "kind": "snapshot | attempt_log | search_result | extracted_data | agent_note",
  "key": "entity:unique-identifier",
  "status": "active | archived | expired",
  "tags": [],
  "data": {},
  "created_at": "2026-03-22T10:00:00Z",
  "updated_at": "2026-03-22T10:00:00Z",
  "expires_at": null,
  "version": 1
}
```

`scope=run` means the data belongs to a specific workflow execution and can be cleaned up after completion. `scope=global` means the data persists across executions of the same workflow (e.g., a list of already-contacted companies that must survive between daily runs). `expires_at` enables automatic TTL cleanup for temporary data. `kind` is a free string — workflows define their own kinds. `key` provides deduplication within a workflow+kind combination.

Recommended indexes: `{workflow: 1, kind: 1, key: 1}`, `{owner_type: 1, owner_id: 1}`, `{expires_at: 1}` (TTL index).

#### Decision Framework — Where Does New Data Go?

When implementing a new workflow or tool that needs to persist data, the developer (human or AI agent) must answer these questions in order:

**Question 1: Does the data fit in `params` inside an existing `tasks` or `workflow_runs` document?** If the data is small, workflow-local metadata that does not need independent queries — yes, use `params`. This is already the documented pattern: `params` is for workflow-local request metadata and small per-task knobs that do not need global semantics. Examples: country filter for a search, retry interval, preferred contact channel, template name.

**Question 2: Does the data represent an entity with its own lifecycle that multiple workflows or agents will query independently?** If yes — this is a candidate for a new structured collection. But do NOT create it immediately. First check: does a structured collection for this domain already exist? Only create a new collection when a genuinely new domain entity appears that cannot map to any existing collection and will be queried frequently with indexed filters by multiple consumers. Start in `workflow_state` first — graduate to a structured collection only when the graduation criteria (below) are all met.

**Question 3: Does the data need complex indexed queries across all instances (e.g., "all entities with status X and last_updated older than Y")?** If yes — it should be in a structured collection, not `workflow_state`. The generic collection works for basic lookups (`workflow` + `kind` + `key`) but is not optimized for arbitrary cross-field queries on `data.*`. When you find yourself needing `db.workflow_state.find({"data.status": "awaiting", "data.last_updated": {$lt: ...}})` across thousands of documents regularly, that data has graduated to its own collection.

**Question 4: None of the above — the data is intermediate, variable, or workflow-specific?** Use `workflow_state`. This is the default for: search results, scraped page content, form submission snapshots, extracted structured data from websites, attempt logs before a structured collection exists, intermediate computation results, agent notes and observations, any state that only the owning workflow reads.

#### When `workflow_state` Graduates to a Structured Collection

A domain starts in `workflow_state`. When it grows to meet ALL of these criteria, extract it into its own collection:

1. Multiple workflows or agents query this data independently (not just the workflow that created it).
2. You need indexed queries on fields inside `data` that run frequently (dashboards, scheduled jobs, deduplication).
3. The entity has a lifecycle independent of any single workflow run (it persists, mutates, and is referenced long after the originating task completes).
4. The volume justifies dedicated indexes (hundreds or thousands of documents with regular filter/sort queries).

Example graduation path: a workflow starts by storing its domain-specific records in `workflow_state` with an appropriate `kind`. Over time, other workflows begin querying the same data, dashboards need filtered views, and the volume grows. When all four graduation criteria above are met, the developer creates a dedicated structured collection for that domain entity and migrates the data out of `workflow_state`. This is the correct moment — not before, and not preemptively at design time.

#### Anti-Patterns

- **One collection per business case.** A new workflow does NOT get its own collection. It uses `workflow_state` or, if a structured collection for that domain entity already exists (because it graduated), the existing collection with appropriate type/category discrimination.
- **Querying `data.*` fields heavily in `workflow_state`.** If you're building indexes on `data.status` or `data.last_contact` inside `workflow_state`, the data should be in its own collection.
- **Storing canonical runtime state in `workflow_state`.** Task status, workflow execution state, approval gates — these belong in `tasks` and `workflow_runs`. The generic collection is for supplementary data.
- **Creating both `memory` and `artifacts` and `workflow_state`.** One generic collection is enough. Use `kind` to discriminate between snapshots, notes, documents, and blobs. Multiple generic collections is the same sprawl problem with a different name.
- **Skipping `params` and going straight to `workflow_state`.** If the data is 3-5 small fields that only matter to this specific task execution, it belongs in `params` on the task or workflow_run document.

#### Markdown vs MongoDB

Markdown files are for narrative context that LLMs read as part of their working memory. They are NOT queryable state. The existing pattern — `CONTEXT.md` for project memory, `CURRENT_PLAN.md` for current ticket plan, `BRIEF.md` for reviewer context — is correct and should not expand to cover operational state. If the question is "can I filter, count, or update this atomically?" the answer is MongoDB, not a file.

External systems such as Notion may mirror these tasks, but they should not replace MongoDB as the canonical runtime store.

---

## n8n Integration

Used for **external webhook triggers** and **complex multi-step flows** — not for simple API calls.

**Inbound contract:** external systems can normalize into `POST /api/tasks`.
**Outbound contract:** complex multi-step flows with retry, delays, or conditional branching.

**Rule: n8n is for receiving external webhooks and orchestrating complex multi-step flows. Simple API calls go directly in agent tools (local or shared).**

---

## LiveKit Voice — Realtime Voice Chat

LiveKit replaces OpenWebUI for voice interactions:

```
Mobile app (LiveKit Android starter) or web
  → WebRTC → LiveKit Server (self-hosted Docker)
    → Silero VAD (detects end of speech, <1ms)
    → Groq Whisper Large v3 Turbo (~50ms for 3s audio, $0.04/hr)
    → OpenCEO /v1/chat/completions
    → TTS (Kokoro, ElevenLabs, or OpenAI-compatible)
  → WebRTC → Audio back to mobile
```

OpenWebUI remains for: text chat, sending images/files, conversation history. Both connect to the same OpenCEO backend.

---

## Modular Monolith — Not Microservices

Each agent is a Python package within the same project. No microservices unless there is a real technical reason.

### Agent Folder Structure

```
openceo-core/agents/
  finance/
    __init__.py
    tools/
      trading.py          # binance_api, sell_crypto, buy_crypto
      budgets.py          # calculate_budget, split_income
      payments.py         # bank_transfer, pago_elfec
      reporting.py        # generate_report, monthly_close
    workflows/
      cierre_contable.yaml
      crypto_reembolso.yaml
  hr/
    __init__.py
    tools/
      evaluation.py       # evaluate_employee, performance_review
      recruitment.py      # post_job, screen_candidate
      social.py           # publish_facebook, coordinate_marketing
    workflows/
      hiring_flow.yaml
  secretary/
    __init__.py
    tools/
      email.py            # send_email, list_accounts
      documents.py        # generate_doc, export_pdf
    workflows/
      notificacion_clientes.yaml
  ceo/
    workflows/            # cross-agent workflows
      cierre_mes_completo.yaml
```

Each tool file has max 200-300 lines. An agent with 200 tools distributes them across 10-15 files organized by domain.

### When to Extract a Microservice

| Real technical reason | Example | Decision |
|---|---|---|
| Independent 24/7 process | Trading bot monitoring prices | Microservice |
| Different language | ML model in Rust | Microservice |
| Heavy compute that blocks | Video/image processing | Microservice |
| Just more Python functions | 200 finance tools | Modular monolith |
| Many workflows | 50 HR YAMLs | Modular monolith |

### TOOL_REGISTRY Absorbs Both Cases

```python
TOOL_REGISTRY = {
    # Local — direct Python function
    "finance.calculate_budget": calculate_budget_fn,

    # External — wrapper calling the microservice
    "finance.binance_api": binance_api_fn,  # internally makes HTTP to trading bot
}
```

The executor, discovery, and claim do not know or care if the tool is local or calls an external service. The abstraction is at function level, not service level.

---

## Project Structure

```
openceo/
├── README.md
├── STATUS.md
├── deploy/
│   └── systemd/
├── docs/
│   ├── AGENTS.md
│   ├── PROJECTS.md
│   ├── ROADMAP_PHASE1_PHASE2.md
│   └── SETUP.md
├── openceo-core/                     # Backend: FastAPI + OpenWebUI-compatible API + MCP
│   ├── .env.example
│   ├── .env.finance.local.example
│   ├── Dockerfile
│   ├── main.py                       # FastAPI + MCP server in same process
│   ├── dev_session_service.py        # Persistent tmux sessions for dev peer programming
│   ├── graph.py                      # Explicitly wired worker graph
│   ├── state.py                      # Pydantic models and enums
│   ├── router.py                     # CEO routing logic
│   ├── config_loader.py              # Reads/validates project.yaml
│   ├── project_service.py
│   ├── task_service.py
│   ├── scheduler.py
│   ├── rate_monitor.py               # Rate limit tracking, gating, and mid-task detection
│   ├── events.py
│   ├── mcp_server.py                 # Internal MCP endpoint for coding agents
│   ├── settings.py
│   ├── requirements.txt              # Shared runtime deps only
│   │
│   ├── tools/                        # Shared tools reused across skills
│   │   ├── cloudflare.py             # DNS management
│   │   ├── email_sender.py           # SMTP email sending
│   │   └── __init__.py
│   │
│   ├── agents/                       # Each agent = self-contained skill package
│   │   ├── ceo/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   ├── dev/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── requirements.txt
│   │   │   ├── .env.example
│   │   │   ├── session.py
│   │   │   ├── consensus.py
│   │   │   ├── tmux_runtime.py
│   │   │   └── opencode_run.py
│   │   ├── pm/
│   │   │   └── session.py            # PMSessionManager — ticket extraction from interviews
│   │   ├── finance/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── session.py
│   │   │   ├── workflows.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   ├── secretary/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── session.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   ├── sales/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   ├── marketing/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   ├── media/
│   │   │   ├── prompt.md
│   │   │   ├── tools.py
│   │   │   ├── requirements.txt
│   │   │   └── .env.example
│   │   └── __init__.py
│   │
│   ├── db/
│   │   ├── mongo.py
│   │   └── __init__.py
│   │
│   ├── config/
│   │   └── email_identities.toml
│   └── integrations/
│       ├── telegram_bot.py
│       ├── openwebui_compat.py
│       └── n8n_client.py
│
├── openceo-dashboard/
├── openceo-voice/
├── openceo-voice-android/
├── openceo-worker/
├── projects/                         # Per-project YAML configs + CONTEXT.md
└── scripts/
    ├── install-agent-requirements.sh
    ├── openceo-core-host.sh
    ├── openceo-core-host-runner.sh
    ├── setup.sh
    ├── backup.sh
    └── health_check.sh
```

---

## Environment Configuration

```bash
# Shared core
OPENCEO_API_KEY=
MONGODB_URL=mongodb://user:pass@localhost:27017
MONGODB_DATABASE=openceo
OPENWEBUI_URL=http://openwebui:8080
N8N_URL=http://jardal-n8n:5678
N8N_API_KEY=

# Shared infrastructure tools
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_DNS_DEFAULT_TARGET=91.99.9.106
CLOUDFLARE_DNS_DEFAULT_PROXIED=false
CLOUDFLARE_DNS_DEFAULT_TTL=1

# CEO + secretary fallback LLM
DEEPSEEK_API_KEY=
CEO_LLM_MODEL=deepseek-v4-flash
CEO_LLM_BASE_URL=https://api.deepseek.com/v1

# Dev skill
CODEX_CLI_PATH=/home/develop/.npm-global/bin/codex
CLAUDE_CODE_PATH=clauded
GEMINI_CLI_PATH=/home/develop/.local/bin/agyd
OPENCODE_CLI_PATH=/home/develop/.opencode/bin/opencode
GLM_CLI_PATH=clauded

CODEX_DEFAULT_ARGS=--dangerously-bypass-approvals-and-sandbox -m gpt-5.4 -c model_reasoning_effort=xhigh
CLAUDE_DEFAULT_ARGS=--dangerously-skip-permissions --model opus --effort high
GEMINI_DEFAULT_ARGS=
OPENCODE_DEFAULT_ARGS=-m deepseek/deepseek-reasoner
OPENCODE_GO_API_KEY=
OPENCODE_MODEL_OVERRIDE=
OPENCODE_GO_MODEL_GLM5=opencode-go/glm-5
OPENCODE_GO_MODEL_KIMI_K25=opencode-go/kimi-k2.5
OPENCODE_GO_MODEL_MINIMAX_M25=opencode-go/minimax-m2.5
OPENCODE_GO_MODEL_MINIMAX_M27=opencode-go/minimax-m2.7
GLM_DEFAULT_ARGS=--dangerously-skip-permissions --model glm-4.5

DEV_EXECUTION_MODE=cli
DEV_TURN_TIMEOUT_SECONDS=1800
DEV_IDLE_TIMEOUT_SECONDS=45
DEV_MAX_CONTEXT_MESSAGES=60
DEV_CONTEXT_MAX_CHARS=20000
TMUX_POLL_INTERVAL_MS=300
TMUX_TURN_TIMEOUT_SECONDS=600
TMUX_STABILITY_CHECKS=3

# Shared SMTP tool
SMTP_HOST=
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USE_STARTTLS=false
SMTP_SSL_VERIFY=false
SMTP_TIMEOUT_SECONDS=30
SMTP_DEFAULT_SENDER=
SMTP_ACCOUNTS_JSON=
SMTP_IDENTITIES_PATH=config/email_identities.toml
SMTP_SIGNATURE_COMPANY=Jardal - Bolivia
SMTP_SIGNATURE_LOGO_PATH=../Isotipo.png
SMTP_SIGNATURE_LOGO_WIDTH=104

# Rate limit management
RATE_MAX_CONCURRENT_TICKETS=3
RATE_CODEX_RESERVE_5H=0.20
RATE_CODEX_RESERVE_WEEKLY=0.05
RATE_CLAUDE_RESERVE_5H=0.0
RATE_CLAUDE_RESERVE_WEEKLY=0.0
RATE_TIER_S_WEIGHT=3
RATE_NORMAL_WEIGHT=1
RATE_STATUS_STALE_SECONDS=300

# Internal MCP
MCP_SERVER_ENABLED=true
MCP_SERVER_PATH=/internal/mcp
MCP_GLOBAL_PATH=/internal/mcp-global
MCP_SERVER_TRANSPORT=streamable-http
MCP_SERVER_INTERNAL_HOST=127.0.0.1
MCP_SERVER_ALLOWED_IPS=127.0.0.1/32,::1/128
MCP_SERVER_API_KEY=

# Server
OPENCEO_HOST=0.0.0.0
OPENCEO_PORT=8000
WEBSOCKET_PATH=/ws
```

Notes:
- `openceo-core/.env.example` is the shared runtime template.
- Finance secrets belong in `openceo-core/.env.finance.local`, not in source files.
- Each `openceo-core/agents/{name}/.env.example` file documents the skill-local credential contract for packaging and marketplace export.

---

## Deployment

```bash
git clone https://github.com/your-org/openceo.git && cd openceo
cp openceo-core/.env.example openceo-core/.env  # Edit with your values
cp openceo-core/.env.finance.local.example openceo-core/.env.finance.local  # optional, only if you will use finance
python3 -m venv .venv
source .venv/bin/activate
pip install -r openceo-core/requirements.txt
./scripts/install-agent-requirements.sh
python -m playwright install chromium   # optional but required for finance on host installs
cd openceo-core && python main.py
```

Docker build note:
- `openceo-core/Dockerfile` now installs shared requirements plus every bundled `agents/*/requirements.txt`.
- If a bundled skill installs Playwright, Docker also installs Chromium during build.

LiveKit Voice:
- `cd openceo-voice && cp .env.example .env`
- `docker compose up -d --build`
- `docker compose --profile agent up -d --build` si también quieres levantar el worker LiveKit

LiveKit Android: Open `openceo-voice-android/` in Android Studio → Build → Run.

---

## Adding a New Agent

1. Create `openceo-core/agents/{name}/` with `prompt.md`, `tools.py`, `requirements.txt`, and `.env.example`.
2. Keep only skill-local Python packages in that folder's `requirements.txt`. Shared dependencies stay in `openceo-core/requirements.txt`.
3. Declare only that skill's credentials/config in `.env.example`.
4. If the skill should be executable in the bundled runtime, wire it explicitly in `router.py`, `graph.py`, and the relevant service/runtime code. A folder alone is not enough.
5. If the skill should run inside the bundled deployment, merge its env keys into `openceo-core/.env` or add dedicated env loading support.

## Adding a New Project

1. Create `projects/{name}/project.yaml` + `CONTEXT.md`. No code changes.

## Adding a New Business Process

1. Add workflow entries to project YAML only for runtimes that already resolve them.
2. Keep workflow definitions inside the agent/project config that consumes them.

## Adding a New Tool

1. Evaluate: local (one agent), shared (multiple agents), n8n (webhooks/complex), or Worker PC (no API).
2. Shared → create in `openceo-core/tools/`, import in agents.
3. Local → add to agent's `tools.py`.
4. n8n → create workflow, call via `n8n_client.py`.
5. MCP-exposed (for coding agents) → add one line to `mcp_server.py`. Owner decides.

---

## VM Restart Recovery

1. MongoDB persists canonical OpenCEO runtime state. External mirrors such as Notion keep their own copies separately.
2. Interrupted tasks are marked accordingly on startup reconcile.
3. tmux-backed dev sessions can be reattached by `dev_session_service.startup_reconcile()`.
4. WebSocket clients reconnect normally.

---

## Development Rules

- **README.md is an architecture document.** It defines flows, functionalities, and design decisions. NEVER add operational tracking here. That information belongs in `STATUS.md`.
- Keep `README.md`, `STATUS.md`, and the affected skill manifests in sync whenever architecture, install contracts, or runtime behavior changes.
- Do not put planning or tracking notes in `README.md`.
- If you change the dev runtime flow, re-run the smoke checklist from `AGENTS.md` before closing the task.

---

## License

MIT
