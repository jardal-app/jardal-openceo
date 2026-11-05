# OpenCEO

> Referencia completa conservada del README anterior al 2026-09-24. Incluye
> contratos vigentes, propuestas y ejemplos conceptuales; contrastar cada
> comportamiento con el codigo y `STATUS.md`. Para orientacion inicial, usar
> el [README breve](../README.md) y la direccion en
> [OPENCEO_DIRECTION.md](OPENCEO_DIRECTION.md).

OpenCEO is an **Agent Control Plane**: it coordinates AI agents, providers, workers, sessions, queues, schedules, quotas, automation, workflow execution, and technical approvals. Open-source. Self-hosted. LangGraph + FastAPI + n8n + OpenWebUI + MongoDB.

**Jardal ERP coordinates business work. OpenCEO controls its execution. Obsidian preserves engineering knowledge. Git preserves implementation.** Each agent is a competent role; crystallized workflows are deterministic recipes.

> **ARCHITECTURE DOCUMENT — DO NOT ADD STATUS, PROGRESS, OR RUNTIME SNAPSHOTS HERE.**
> This file defines architecture, flows, and interfaces only. Operational tracking belongs in `STATUS.md`.

The ownership boundaries follow [OpenCEO Architecture Direction](OPENCEO_DIRECTION.md), version 1.1. [CLAUDE.md](../CLAUDE.md) describes the developer/runtime contract; [STATUS.md](../STATUS.md) records operational state. Earlier documents that assign business projects and tickets to OpenCEO, or restrict Jardal ERP to finance alone, are superseded **on those ownership boundaries**. This includes section 14 of the [Finance integration contract](finance-jardal-erp-integration-contract.md) and the narrower scope in [JARDAL_ERP.md](../JARDAL_ERP.md). Their compatible financial and deployment constraints still apply.

## System Boundaries

| System | Owns | Answers |
|---|---|---|
| Jardal ERP | Business project identity, tasks/tickets/requests, priorities, users, human business approvals, and business workflows; financial records in its accounting domain | What work exists? |
| OpenCEO | Agent orchestration, providers, workers, sessions, queues, scheduling, quotas, execution workflows, and technical approval gates | How is this work executed? |
| MongoDB | OpenCEO runtime state, execution tasks and attempts, session focus, workflow runs, and execution audit history | What is happening in the runtime? |
| Obsidian vault | Architecture, ADRs, technical decisions, research, incidents, and lessons learned as Markdown files | Why was it designed this way? |
| qmd | A rebuildable keyword index over the vault | Where is the relevant note? |
| Repository search / LSP | Code relationships read from source on demand | What is connected? |
| Git | Source code, versioned project configuration, and technical workflow definitions | What was implemented? |

OpenCEO does not own a second business backlog, accounting ledger, documentation platform, engineering memory database, or code graph. Jira and Notion can remain integration sources or compatibility views; OpenCEO does not replace those products.

### Integration Shape

The diagram describes the target boundaries. Jardal ERP integration requires an explicit adapter contract; this document does not imply that the existing runtime already calls Jardal ERP.

```mermaid
flowchart LR
    Channels[Chat / Voice / Dashboard / MCP clients] --> Core[OpenCEO control plane]
    ERP[Jardal ERP business records] <-->|Business operations and execution evidence| Core
    Core --> Mongo[(MongoDB runtime state)]
    Core --> Runtime[LangGraph / YAML / sessions / workers]
    Runtime --> Agents[Coding and domain agents]
    Agents -->|Operational writes via OpenCEO| Core
    Agents -->|Read and write files| Vault[Obsidian engineering vault]
    Agents -->|Keyword search| Search[qmd]
    Search --> Vault
    Agents -->|Search / LSP / code changes| Repo[Git repositories]
```

Business approval and execution approval are distinct. Jardal ERP owns the human business decision; OpenCEO validates the applicable decision and enforces technical gates such as QA or permission to resume a run. A completed agent run is execution evidence, not automatic business acceptance.

### Distributed Project Identity

The shared integration identifier is **`project_key`**. A project spans Jardal ERP (identity, business tickets and status), Git (code and configuration), Obsidian (architecture and decisions), and OpenCEO/MongoDB (execution state).

`project_key` is an integration contract, not a field rename in the existing API. The runtime still uses `project`, `project.name`, directory names, and `task_id`. An adapter must map these explicitly to Jardal ERP references and the vault project hub, preserving existing identifiers and open sessions. `task_id` identifies OpenCEO execution work; it is not the Jardal ERP ticket ID. Technical subtasks can share one business reference without creating a business ticket for every execution step.

### Read and Write Boundaries

- Agents read the vault as files within their project scope (see Engineering Knowledge and Code Context) and inspect repositories directly. OpenCEO is not a mandatory proxy for every read.
- Knowledge writes go directly to the vault under the `llm-vault-links` skill and pass its checker. They do not require a ticket mutation or an Obsidian MCP.
- Agent-originated operational writes, including ticket creation, status transitions, workflow execution, and approvals, pass through OpenCEO capabilities. The Jardal ERP adapter applies business changes in Jardal ERP; runtime changes remain in MongoDB.
- Human business activity in Jardal ERP reaches OpenCEO through explicit integration events or requests. Reads and knowledge edits must not silently start execution or change business status.
- MCP exposes selected capabilities, not raw database access or unrestricted Jardal ERP APIs. There is no Obsidian MCP or Graphify MCP in this design.

### Compatibility Contract

The modular monolith, LangGraph worker graph, deterministic YAML executor, discovery/crystallization model, Tier S reviews, tmux sessions, voice/chat endpoints, and existing MCP mounts retain their roles. Existing Mongo tasks, local auth, project configuration, and Notion integrations remain usable while adapters are introduced; a documentation change does not migrate their data or remove their APIs.

The integration must preserve `TaskDocument`, task/source idempotency, explicit dev-session activation, immediate background-turn acknowledgement through `send_message_async`, and queryable progress/results. Jardal ERP availability must not become a prerequisite for continuing an already-authorized dev session or reading local engineering knowledge. A failed business write remains a visible integration failure or pending operation; it must not create a competing business record in MongoDB.

---

## Stack

| Component | Technology | Purpose |
|---|---|---|
| Orchestration | LangGraph (Python) | Supervisor-workers graph for the wired runtime (`dev`) plus the generic catalog-agent node |
| API Server | FastAPI | OpenAI-compatible endpoint for OpenWebUI, REST API for dashboard/n8n, WebSocket for real-time |
| Runtime database | MongoDB | Execution tasks, dev sessions, user context, queues, workflow state, and agent metadata |
| Business system of record | Jardal ERP | Business projects, tickets, priorities, users, approvals, workflows, and financial records, connected through scoped adapters |
| Engineering memory | Obsidian-format Markdown vault | Durable engineering knowledge, read and written directly as files |
| Knowledge retrieval | Project-scoped vault reads | Find vault notes within the project folder as described in Engineering Knowledge and Code Context; the keyword index is rebuildable, not a source of truth |
| Code intelligence | Repository search + LSP where supported | Read code relationships on demand, without a stored code graph |
| Automation | n8n (self-hosted, Docker) | External triggers (Jardal ERP and existing Jira, Notion, GitHub sources), webhook normalization, and integration flows |
| Chat/Multimedia | OpenWebUI (self-hosted) | Text chat, file/image/video uploads, basic voice. Connects via OpenAI-compatible API |
| Voice (realtime) | LiveKit Agents + Silero VAD | Fluid voice conversations. Groq Whisper Turbo STT, OpenCEO as LLM backend. Mobile app via LiveKit Android starter |
| MCP Server | FastMCP (inside openceo-core) | Exposes selected OpenCEO and infrastructure capabilities to coding agents; owner-curated, never full backing APIs |
| Notifications | Telegram Bot API | Bidirectional: user sends commands, agents send status updates |
| LLM Observability | Langfuse (self-hosted) | LLM call tracing, token usage per agent, prompt debugging |
| Dashboard | React (custom) | Execution mission control: sessions, queues, runs, quotas, evidence, and technical gates; business records remain in Jardal ERP |
| Browser Automation | Playwright | Browser-based workflows, form filling, authenticated portals |
| Web Search | SearXNG / Brave Search API / provider search API | Agents search the internet for information, then fetch or browse results |
| Coding Tier 1 | Codex CLI + GPT-5.4 | Subscription. Complex features, architectural changes |
| Coding Tier 2 | Claude Code + Opus 4.6 | Subscription. Security, math, logic, critical bugs |
| Coding Tier S | Claude Code + Codex + Gemini via tmux + OpenCode headless review pipeline | Special multi-AI orchestration flow for work whose result gains from independent challenge (any domain) |
| Real-time | WebSocket (FastAPI native) | Dashboard live updates. No Redis needed |
| In-memory cache | Python process memory | Active connections, live agent state. Rebuilds from MongoDB on restart |

OpenCEO core does not require Redis, PostgreSQL, LiteLLM, or aider. Jardal ERP operates its own stack and persistence outside the core; this statement does not constrain Jardal ERP's deployment dependencies.

---

## Architecture Rules

- Crystallized workflows execute as deterministic Python. Zero LLM tokens on step sequencing, flow control, or variable resolution. Workflow discovery was removed (TASK-2026-00215); learning crystallizes into skills.
- LLMs support contextual conversation in `first_layer.py`, routing fallback (`router.py`), document extraction, secretary payload extraction fallback, and the coding CLIs. Crystallized workflow sequencing, conditions, and variable resolution stay deterministic; `llm_eval` is a design extension, not an enabled condition type in the deterministic executor.
- Runtime wiring is explicit. A folder in `openceo-core/agents/` is a portable agent package (paquete de agente), but it becomes executable only when it is registered in `router.py`, `graph.py`, and the relevant service entrypoints.
- The bundled worker graph wires `dev`; enabled catalog agents (operations, secretary, finance, research) run on the generic node.
- Versioned execution configuration lives in YAML (`projects/*/project.yaml`); business project identity lives in Jardal ERP. CEO routing triggers and patterns are read from `routing.agents`. Project is optional for runtime operations using company-level defaults; the Dev agent needs repository context. A missing project never implies personal scope or authorizes a project-sensitive business write.
- Guided chat workflows are deterministic state machines. They accept required inputs in any order, persist whatever the user already provided, and ask only for the missing fields before executing.
- Trigger phrases start a workflow but are not treated as the real payload unless they contain substantive content. Example: a trigger phrase opens the guided flow, but the substantive payload must still arrive before execution.
- Voice/chat UX must guide the user explicitly. The system should not assume the employee already knows the input order or that all required data will arrive in the first turn.
- CEO routing should also recognize substantive workflow payloads when they arrive first. A pasted payload must enter the guided flow for its workflow even if the user never typed the trigger phrase.
- Browser automation uses Playwright-based flows.
- Agents never talk to each other: the agent returns hecho / bloqueado / no_aplica and the single planner re-decides (the peer-to-peer Tier 2 rule was retired 2026-10-11, TASK-2026-00187/00189; see Inter-Agent Communication below). In crystallized workflows (YAML), the executor calls Python functions directly — no inter-agent communication, only sequential tool execution. The CEO initiates tasks (top-down) but does not relay data between active agents.
- User project selection and active dev-session focus live in MongoDB.
- MongoDB is authoritative for OpenCEO execution state, queues, runtime tasks, workflow-local params, and execution dashboard data. Jardal ERP is authoritative for business work. Notion compatibility mirrors do not change either boundary.
- Business status and runtime status are distinct: `completed` in an execution record does not bypass an Jardal ERP business approval. Local users/groups govern access to OpenCEO; they are not a replacement business directory.
- `TaskDocument` is a stable cross-agent contract. Do not add a new top-level task field for every workflow-specific need.
- Promote a task attribute to top-level only when it changes runtime semantics across the system: routing, scheduling, lifecycle gates, idempotency, global filtering, or sync-back to external systems.
- Example: approval gating belongs at top-level (`approval_required`, `approval_state`) because it affects lifecycle, filters, and sync-back. Individual review events do not; they belong in a separate collection linked by `task_id`.
- Use `params` for workflow-local request metadata and small per-task knobs that do not need global semantics.
- Use separate collections for high-churn or history-heavy data such as review logs, comments, approvals, agent-specific drafts, or other append-only workflow state. That data should reference the canonical task by `task_id` instead of inflating the task document indefinitely.
- Integration side effects originate from the CRUD operation or explicit event that caused them. No periodic database/API sweeps, duplicate watchers, or full-collection scans. Queries must be indexed and bounded; retries must retain the same operation identity. Include required creation fields in one API request instead of an immediate follow-up patch.
- Persist execution references and dated summaries, not duplicate business entities, a ledger, vault contents, or generated code graphs. Retrieve only the context needed for the task.

### Agent Packaging Contract

`skill` in this document now means an Agent Skill (`SKILL.md` with optional scripts); the per-agent folder below is an agent package (paquete de agente). See Agentes por rol.

- Every bundled agent-package folder contains `prompt.md`, `tools.py`, `requirements.txt`, and `.env.example`.
- `openceo-core/requirements.txt` now contains only shared runtime dependencies. Agent-specific packages are declared in `openceo-core/agents/{name}/requirements.txt`.
- Bundled local installs use `./scripts/install-agent-requirements.sh` after the shared requirements install.
- For now, the core runtime still loads environment variables centrally from `openceo-core/.env` and `openceo-core/.env.finance.local`. The per-agent `.env.example` files are the source manifests for packaging and marketplace portability.

### Tool Decision Framework

Not every external integration goes through n8n. Each tool is evaluated case by case by the developer at implementation time:

**Local Tool (Python in agent's `tools.py`)** — A simple API call or deterministic workflow used by one agent. The credential manifest belongs in that agent's `.env.example`; in the bundled deployment, the actual values are merged into `openceo-core/.env` or `.env.finance.local`. Examples: a scoped Finance adapter call to Jardal ERP, an agent-only SaaS integration, or skill-local parsing logic. Routine Jardal ERP REST calls belong here, not in a separate microservice or n8n round trip.

**Shared Tool (Python module in `openceo-core/tools/`)** — Multiple agents need the same function. Imported by any agent's `tools.py`. Examples: generate documents from templates, send email via SMTP, or query execution state. Shared business operations still go through the OpenCEO capability that validates policy and calls Jardal ERP; sharing a helper does not grant arbitrary database access.

**n8n Workflow** — External webhooks triggering requests into OpenCEO, or integration flows with retries, delays, and conditional branching that benefit from visual debugging. Examples: Jardal ERP/Jira/GitHub webhook → normalize → POST to OpenCEO, WhatsApp incoming → classify contact → route. Jardal ERP owns business workflow decisions; OpenCEO owns agent execution. n8n must not duplicate either state machine or periodically reconcile whole external datasets.

**Worker PC (browser task)** — Exclusively for platforms with NO usable API requiring browser automation with authenticated Chrome profiles. Examples: send Teams messages, generate content on AI platforms (Gemini web, ChatGPT web).

**Web Search + Browse** — Any agent can search the internet and read results. Two-step pattern: (1) search via SearXNG, Brave Search API, or any provider search API to get URLs, (2) fetch/read the URL content via `web_fetch` or navigate with Playwright (`agent-browser`) for pages that require interaction or JS rendering. Use cases: Finance researches market data, Sales finds competitor info, Secretary gathers context for reports, any agent fills external web forms.

**The key principle:** the developer building a new tool analyzes the specific case — not a blanket rule. A document generation tool used by 3 agents is a shared Python module, NOT an n8n workflow, because sending document data to n8n and back adds latency for no benefit. A WhatsApp incoming webhook IS n8n because OpenCEO cannot receive external webhooks natively.

### Agent Parallelism Rules

| Agent | Parallelism / Constraint |
|---|---|
| Dev | Persistent tmux sessions. Effective limit: one live runtime per project |
| Finance | Stateless per request. Can run multiple deterministic jobs in parallel |
| Secretary | Stateless per request. Can run multiple email tasks in parallel |

---

## Engineering Knowledge and Code Context

The engineering vault lives outside this repo. Placement, links, note shape, and validation are defined by the llm-vault-links skill and the "Vault y mantenimiento del conocimiento" section of the jardal-knowledge README. This section does not duplicate them; follow those sources. Agents access Markdown files directly; Obsidian is an optional viewer, not a runtime dependency.

Retrieve context in this order:

1. Resolve the authorized work and execution state through OpenCEO, with the business reference in Jardal ERP when integrated.
2. Find the project hub using the vault mapping and read the relevant linked notes. Keep every vault read inside that project's folder; when the hub does not link the note directly, search within the project folder only.
3. Read current code relationships with targeted repository search (`rg`) and LSP where the agent supports it.
4. Inspect implementation state with Git and targeted diffs.

Project sessions never run vault-wide searches, the checker without `--project`, or index rebuilds: those belong to vault maintenance, not to project work. The skill forbids them.

Write settled architecture, decisions, incident causes/fixes, research conclusions, and lessons to the vault. Update an existing note on the same subject, connect it to its project hub, cite repository evidence, and validate links. After changing notes, check only the touched notes with the project-scoped checker form documented in the README section above.

The checker must report zero errors and no warnings on touched notes. Task plans, progress logs, transcripts, secrets, build output, copies of code, and copies of existing repository documentation do not belong in the vault. Link to repository contracts such as this README instead of duplicating them.

`CURRENT_PLAN.md`, `BRIEF.md`, feedback files, and session output remain execution artifacts. The legacy per-project `CONTEXT.md` writers were retired in TASK-2026-00143 (no readers remained; session completion lives in Mongo `dev_sessions`, Tier S flows write `HANDOFF.md`). Tier S can also use repository-local artifacts. Retain source documents when migrating knowledge.

Code topology is read on demand and is not copied into the vault or MongoDB. Graphify is outside the agent architecture: no registered Graphify MCP and no dependency on its manually refreshed graph. The repository explains connections; vault notes explain design decisions. If a note disagrees with the code, verify the code and correct the note.

---

## Discovery & Crystallization

> **Removed (2026-10-13, TASK-2026-00215).** Discovery that writes YAML — the Tier 1.5/2 flows below, the DeepSeek autodiscovery proposal/approval loop, the blocked-discovery resume, the `discover_workflow` flags, and the `discovery-runs` API — is gone from the code; what follows is history. The deterministic executor for already-crystallized YAML workflows stays until its owning tickets migrate it. Learning now crystallizes into skills; see Agentes por rol.

OpenCEO had two execution modes for any task:

1. **Deterministic** — a crystallized YAML workflow already exists → Python executor runs it with zero LLM tokens
2. **Discovery** — the flow was unknown → the service proposed it → a human approved → it crystallized into YAML (removed)

The tiers describe the execution design. The full ReAct/claim design below is not a claim that every tier was wired. Discovery could compose only registered capabilities and had to respect Jardal ERP business authority. Approval to crystallize a recipe never granted approval for later business operations.

### Two Levels of Discovery

**Level 1: CEO** — `create_react_agent` with tools: `[call_dev, call_finance, call_secretary, call_operations]` (the `call_pm` tool in the original design retired with the PM agent in TASK-2026-00209). Question answered: **which agents do I need and in what order?**

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

Domain tool names in the following examples illustrate composition, not a live capability catalog. Finance operations must wrap approved Jardal ERP/provider contracts, without implementing accounting inside OpenCEO; money movement is disabled in the initial Finance integration. The live registry determines what can execute.

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

Workflows with iterative processes use `type: loop`. The condition contract distinguishes the deterministic type from the proposed evaluator extension:

| Type | Evaluated by | Example |
|---|---|---|
| `python_eval` | Pure Python | `"{{count}} > 0"` |
| `llm_eval` (design extension only) | A dedicated LLM evaluator would return True/False | `"Is the proposal complete?"` |

The deterministic executor accepts `python_eval` and rejects `llm_eval`; the example below is an extension sketch, not an executable workflow. Such an evaluator could never add steps, change order, authorize a business operation, or replace an approval gate.

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

OpenCEO enforces technical approval gates through task phases and YAML `type: human_in_the_loop` steps. A YAML gate persists `WorkflowRunStatus.BLOCKED_HUMAN`, keeps the task at `status=waiting` with the requested permission/phase, and resumes through `create_phase_action` after approval without replaying completed steps.

Jardal ERP owns business approvals and business workflow transitions. OpenCEO records the reference to an applicable business decision and uses it to gate execution; an OpenCEO QA approval cannot stand in for Jardal ERP business acceptance. Existing dashboard phases such as `pm_approval` and `client_signoff` remain compatibility interfaces until their business decisions are mapped explicitly to Jardal ERP. Technical YAML recipes remain deterministic while paused.

Dashboard phase workflows are configured per project with reusable `workflow_templates`. A ticket stores a snapshot of the selected template so future project changes do not rewrite active work. Since TASK-2026-00118 the *default* attachment skips the human `qa_review` phase unless the project policy mandates human review (`dev.qa.require_human_review` in `project.yaml`); explicitly requested templates or phases keep their human gates:

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

`phase_assignments` adds local QA/client identities as involved users for execution visibility. The existing action route is `POST /api/tasks/{task_id}/phase-action`; QA can open a Kimi review session with `POST /api/tasks/{task_id}/phase-kimi`. Approve advances the execution phase; request changes/reject returns the execution task to `queued` for CEO re-routing while preserving history. Business user identity and approval state require explicit Jardal ERP mapping, not an implicit replacement of local authentication.

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

References over flattening: if Finance improves `cierre_contable`, the composed workflow benefits automatically. Cross-agent workflows are stored under `workflows/ceo/`:

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
    "core.echo":              echo_fn,
}
```

Naming convention: `{agent}.{tool_name}`. Tools reused by multiple agents register under the owning agent (no `global` namespace). The registry abstracts local vs external — a tool wrapping an HTTP call to a microservice looks identical to a local Python function.

### check_registry — Two-Level Query

> Historical: the tool was removed in TASK-2026-00199; the agent catalog replaces it.

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
        "operations":  ["crear_proyecto"],
    }
    """
```

CEO flow for large tasks: `check_registry()` → global summary → reason about matches → `check_registry("finance")` → full detail where it applies. First global summary (avoids missing matches by wrong agent assumption), then detail only where relevant.

> **Retired (2026-10-11, TASK-2026-00199).** `core.check_registry` was removed from the registry; the agent catalog (`catalog/`, `openceo-core/catalog.py`) replaces it. Kept as history.

### LLM Usage Guidelines

With high-limit token plans, the criterion shifts from "can I avoid the LLM?" to "does it add genuine value here?"

**Where LLM adds value:**
1. Qualitative evaluation where a dedicated evaluator is justified (`llm_eval` remains a design extension, disabled in deterministic loops)
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
    - status: "pending" | "running" | "blocked_tool" | "blocked_human" | "completed" | "failed" | "canceled"
    - workflow: workflow name (e.g., "propuesta_licitacion")
    - agent: responsible agent (e.g., "finance", "sales")
    - created_at: creation date (ISO format)

    Example: {"status": "blocked_tool", "agent": "sales"}
    """
    return list(db.workflow_runs.find(filters).sort("created_at", -1).limit(100))
```

The tool schema and deterministic validation constrain filters. Queries must use indexed fields, bounded results, and pagination when more results are needed. Reading execution history has no effect on workflow execution.

> **Retired (2026-10-11, TASK-2026-00187 / TASK-2026-00189).** CEO Discovery fragmentation and Tier 0–1.5 matching are retired; every pedido goes through the single planner (TASK-2026-00204). Kept as history; superseded by Agentes por rol.

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
| Planner | Dev agent | development tickets (features, bugs, sprints) |
| CEO | any agent | operational enterprise subtasks |

#### Connection with Tiers

- **Tier 0/1**: CEO knows who does it → `assign_task` direct to agent
- **Tier 2**: CEO doesn't know who can → puts on shared table → claim model

### `create_ticket` — Global Tool

Capability sketch shared by the planner and CEO, not a new registered MCP signature:

```python
@tool
def create_ticket(
    title: str,
    description: str,
    assigned_to: str,       # "finance", "secretary", "dev", "sales", etc.
    parent_task: str | None,
    priority: str | None,
) -> dict:
    """Requests business work through OpenCEO for an assigned agent."""
```

CEO uses `assigned_to` with any agent. (The retired PM agent always used `assigned_to="dev"`; TASK-2026-00209 removed it.)

In the target integration, OpenCEO validates the request and creates the business ticket in Jardal ERP through an adapter, then associates execution work with its reference. Pure execution subtasks stay in the runtime. The ticket capability and task APIs are the compatibility entrypoints (the `pm.create_tickets` workflow named here retired with the PM agent in TASK-2026-00209); writing a Mongo task alone does not claim successful Jardal ERP registration.

### Parent Task Tracking

Execution fragmentation uses a **parent execution task** in MongoDB. Subtasks or `workflow_runs` reference that parent and retain the business reference. This technical hierarchy does not replace Jardal ERP's business task hierarchy. (`discovery_runs` retired with TASK-2026-00215; orphan rows were left unmigrated and no code reads them.)

```
Parent execution: "close the month"  (MongoDB, linked to Jardal ERP work)
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

Business hierarchy is separate and lives in Jardal ERP: group Tasks (`is_group`), real children (`parent_task`, `lft`/`rgt` tree), and the `depends_on` table. No Jardal ERP pre-configuration is needed — the DocType already brings these fields; the work is OpenCEO-side: closed semantic adapter operations, `parent_task`/`depends_on` parameters on ticket creation (so the planner, the CEO, and sessions link at creation), and a `blocked_by` ↔ `depends_on` mapping with topological state moves (Jardal ERP answers 417 while dependencies are open).

> Implemented by TASK-2026-00013 (2026-10-06): `create_group_task`, `convert_to_group`, `link_child`, `set_dependencies` in `tools/jardal_erp_tickets.py`; content-based 417 classification (`classify_417`); `pending_status` retry for dependency-blocked pushes. Linking a child auto-adds a `depends_on` row on the parent (Frappe-side), so a group cannot complete with open children.

#### Group Progress and Gantt Dates (Derived Visibility)

Group `%`, real start/end dates, deterministic estimates for open tickets, and the group span are DERIVED from business state — never backlog. Frappe computes none of this (verified live): it leaves group progress at 0, keeps leaf progress at 100 after a reopen, never sets `completed_on`, and never reschedules dependents — but it does enforce child-end ≤ parent-end (417 `InvalidDates`), so date writers extend ancestors first and retry once.

Rules live in `openceo-core/visibility.py` (pure, zero LLM); I/O in closed adapter operations (`list_children`, `update_work_dates`, `update_group_rollup`, `list_closed_with_dates`) plus `work_dates` riding the same PUT as the status transition. Recalculation is event-driven through a dedicated visibility queue and worker (push, explicit transition, inverse webhook, creation), always compare-before-write so echoes never write; Jardal ERP down parks a `pending_visibility` mirror flag for the startup reconcile. Real dates overwrite estimates, estimates never overwrite explicit due dates (externally set dues are adopted into the mirror), and `exp_*` convert through `JARDAL_ERP_SITE_TIMEZONE` (site-local naive datetimes).

> Implemented by TASK-2026-00146 + TASK-2026-00147 (2026-10-08): `visibility.py`, `JardalERPDateBoundError`, `_refresh_visibility` in `TaskService`, one-shot backfill verified by reread.

#### External References and Notion Compatibility

Jardal ERP holds the business hierarchy; OpenCEO holds execution dependencies and evidence. A Notion compatibility view may represent parent/child links with a Relation property, but it is not the authority for either hierarchy. Do not assume the conceptual parent relation is implemented by every adapter or duplicate one technical step as a business ticket in each external system.

---

## Routing And Workflow Resolution

- Chat/voice first enters `main.py`. For messages without an `@` prefix and with the gateway configured, `FirstLayerGateway` resolves contextual intents, ticket/session focus, presets, and Horizon before fallback routing. Horizon's deterministic classification precedes the gateway's LLM call.
- CEO routing in `openceo-core/router.py`: direct hint, `@agent` prefix, project-config routing rules from `project.yaml`, then optional cheap LLM fallback. If no project is provided, company-level defaults apply and routing falls through to LLM fallback or `default_agent`.
- Worker execution in `openceo-core/graph.py`: `dev` keeps the only explicit worker node; enabled catalog agents run on the generic node. Other routes escalate as unsupported.
- Finance runs as a catalog agent (TASK-2026-00221): five exact skills over the registry tools, and voice/chat naming the skill through the first layer.
- Research runs as a catalog agent (TASK-2026-00222): the `investigar` recipe over two bridge tools that admit and poll under a per-ticket consumer.
- Secretary-specific action extraction in `openceo-core/agents/secretary/session.py`: JSON or structured-text parsing first, optional LLM fallback second.

Jardal ERP integration supplies authorized work and business references to these paths; it does not replace routing, introduce another agent runtime, or turn knowledge lookup into a dev instruction.

---

## ChatGPT MCP Orchestrator

OpenCEO now has two separate execution paths:

- Path A: ChatGPT + MCP. ChatGPT is the intelligence layer. It chooses the workflow, extracts and structures params, runs approval loops, and only then calls OpenCEO for deterministic execution.
- Path B: OpenWebUI + DeepSeek + LiveKit. OpenCEO keeps the existing routing, extraction, and guided flows in `router.py`, `graph.py`, and the agent sessions.

These paths never mix inside the same request. A workflow exposed in MCP must be the direct-execution variant for Path A, not the AI-extraction variant that Path B uses internally.

### Intelligence Before Execution

- ChatGPT must preprocess any user content it can already understand before calling `execute_workflow`.
- OpenCEO should not repeat extraction, routing, or content processing that ChatGPT already completed.
- Ticket batches are the reference example: in ChatGPT path, use the batch capability with structured tickets (the `pm.create_tickets` workflow in the original direction retired with the PM agent in TASK-2026-00209). Only use the extraction variant if the user explicitly wants OpenCEO to do the extraction itself.
- This same direct-vs-extraction rule generalizes to every future workflow where ChatGPT can produce a ready-to-run payload.

### Approval Loops

- Any workflow with `requires_approval=true` must be summarized back to the user before execution.
- ChatGPT waits for explicit confirmation before calling `execute_workflow`.
- This applies to payments, emails, Notion writes, and any other real-world or irreversible external action.

Chat confirmation is an execution authorization, not a substitute for an Jardal ERP business approval required by the operation. The adapter must verify the business decision and preserve its reference before allowing the side effect.

### MCP Tools

Capability boundary:

- Expose semantic operations such as `ticket.create()`, `ticket.transition()`, `execution.start()`, `execution.status()`, and `approval.request()`. These names are the direction document's capability vocabulary, not a claim that new tools with those exact names are registered.
- Preserve the external connector's workflow-first contract below. A semantic capability can be implemented by an existing workflow behind `execute_workflow`; adding a raw Jardal ERP CRUD tool or a second write path is not required.
- Only the curated registry/catalog defines callable tools. Do not expose arbitrary DocTypes, unrestricted database queries, or complete infrastructure APIs.

External connector contract:

- Business capabilities remain workflow-first. UI is never a reason to promote a business capability to a new first-level MCP tool.
- MCP Apps belong to the presentation layer only. They must wrap or assist an existing execution contract; they must not redefine the business contract.
- The stable execution surface for business actions remains the workflow catalog behind `execute_workflow`.
- Any exception used for prototyping must be documented as transitional and must not be treated as the target architecture.

Core execution tools:

- `get_company_context`: returns the live agent/workflow catalog plus active projects. ChatGPT should call this once at conversation start and cache it. For any actionable request or capability question, this catalog is the mandatory discovery source before ChatGPT decides what OpenCEO can or cannot do. If a compatible workflow exists, discovery should continue into approval/execution rather than end in a local-only draft. When the workflow is already known and the remaining gaps are confirmable/selectable by the connector, ChatGPT should move into `execute_workflow` instead of keeping the whole remaining loop manually in chat.
- `get_user_context`: returns the current user's active project, active dev session, and running background jobs. ChatGPT should call this on demand only, when the request depends on current state such as active project, active dev session, pending work, or task status. `active_project` is advisory context, not a silent default for project-sensitive write workflows.
- `execute_workflow`: runs one already-resolved workflow directly, without using the CEO router or graph fallback path. This is the single execution entrypoint for real OpenCEO actions once ChatGPT has already discovered and prepared the correct workflow. The intended behavior is MCP-first: when the cached catalog already shows a compatible connected workflow, ChatGPT should prefer executing it over local-only behavior and, for approval-required side effects, move into a compact approval step instead of stopping at a full drafted artifact. Once the workflow is known, `execute_workflow` is also the right place to let the connector resolve the last confirmable/selectable params through `needs_input` or MCP Apps.
- `get_task_status`: reads one async task from MongoDB so ChatGPT can report progress later.
- Batch safety rule: ticket-batch creation and other project-sensitive writes must not fall back silently to `active_project`. If the project was not explicitly provided, the connector should force confirmation or UI selection.
- Batch creation goes through `openceo_create_pm_tickets_batch` (Owner badge only; keeps its public name) with its structured batch contract. Its compatibility implementation registers one Mongo execution task per ticket with `autorun=false`. Jardal ERP-backed business creation belongs behind the same capability once integrated; neither the frontend nor the agent writes a second business backlog directly.

### MCP Apps Presentation

- The external MCP connector should use MCP Apps standard for presentation. Do not build new ChatGPT-only widgets for this path.
- This UI layer remains optional and must not become a runtime dependency or architectural foundation. The durable contract stays MCP tools + deterministic workflows + `structuredContent`.
- If the UI disappears, the workflow must still be operable through plain tool calls and text fallback.
- The batch-review app hangs from `execute_workflow` for the ticket-batch workflow, reviewing the batch and confirming project selection before registration. (The original direction named the PM app over `pm.create_tickets`; the PM agent retired in TASK-2026-00209.) The existing Mongo/Notion path is a compatibility implementation; business persistence can move to an Jardal ERP adapter without making the UI its owner.
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

Role: Receives authorized work and routes execution to the correct worker agent + project. It uses Jardal ERP business context, relevant vault knowledge, and repository evidence without taking ownership of those domains.

Routing behavior:
- Message contains `@agent_name` prefix → route directly when the alias exists.
- No prefix → config-driven routing rules from `project.yaml routing.agents`, then optional cheap LLM fallback (`DEEPSEEK_API_KEY`) only if the config rules did not decide.
- The router understands `dev`, `finance`, `secretary`, `operations`, `research`, and `ceo`; the worker graph dispatches the wired runtime workers.

Administrative commands (owner only, deterministic, zero tokens):
- `/status` and the OpenWebUI-compatible project/session commands handled in `main.py`

### Horizon — personal and strategic capability of the CEO

Horizon is **not a separate agent** and has no runtime of its own. It is the
personal context the CEO consumes: mission, objectives, milestones, capacity,
captured ideas and the next action. Specification:
[jardal-horizon/README.md](../jardal-horizon/README.md) v2.1. Implementation: `openceo-core/horizon/`.

Horizon preserves private personal planning and the CEO's mandate. It is not the
business backlog, engineering vault, or accounting system. Business work it
requests follows the Jardal ERP/OpenCEO boundary; personal context is not exported
by default. Delivery phases and operational tracking belong in `STATUS.md` and
TASK-2026-00026 en Jardal ERP.

Storage (owner-scoped, indexed in `db/mongo.py`):
`horizon_profile`, `horizon_objectives`, `horizon_captures`, `horizon_actions`,
`horizon_events`, `horizon_plans`, `horizon_triggers`. The personal plan and
persisted triggers reuse the core runtime rather than adding a separate planner
or runtime for company work.

Hierarchy: `mission → objective → milestone → action → result`. Objectives are
**not** tickets — only the action links an execution `task_id`, so a personal
commitment may have no repository.

Conversational intents (in `first_layer.py`, Spanish, voice-friendly):
`horizon_capture`, `horizon_next_action`, `horizon_progress`,
`horizon_plan_query`, plus capacity, daily planning, review, delegation, delivery,
and metrics. REST lives under `/api/horizon/*`; the personal dashboard consumes
that contract. Delegation reuses background dev execution and persists result
delivery separately from agent completion.

Four rules this module enforces, all of which are easy to regress:

1. **A deterministic classifier runs before the LLM call.** With an active dev
   session, the first-layer prompt treats any phrase as `dev_instruction` and
   writes it to tmux; a personal note must never land there. Running before the
   model also keeps capture, calculation and plan lookup working when the AI
   budget is exhausted (spec §9.7).
2. **Personal scope never reaches an external mirror.** There are two paths to
   Notion and both filter it: `TaskService._task_needs_notion_mirror` and
   `MongoRepository.list_tasks_missing_notion_mirror` (startup reconcile). New
   Jardal ERP adapters must preserve this isolation; the shared engineering vault
   must not become an automatic destination for private personal captures.
3. **Unknown is `null` with uncertainty `unknown` — never `0`.** "Recorded
   minutes" and "usable minutes" are different measurements; don't seed one with
   the other. Financial records live in Jardal ERP. Finance uses `Decimal` and
   explicit currencies under its adapter contract; any Horizon monetary summary
   must respect its integer-minor-unit contract, retain provenance and a cutoff
   date, and never use floats or become a local ledger.
4. **Import only tightens the mandate.** Re-reading the specification may make
   policy stricter, never more permissive (capacity ceiling 70%, one active
   growth goal, 6 ready actions, 1 in progress; external mirror and money
   execution stay off).

Personal data is imported from the specification as versioned private data with
provenance — never hardcoded in Python (`horizon/importer.py`).

### Dev & DevOps Agent

Role: Codes, deploys, maintains CI/CD, and executes technical work in configured repositories. Jardal ERP owns business project management.

Flow:
- Peer programming runtime with persistent tmux sessions
- Explicit session activation from OpenWebUI-compatible chat
- Providers: `codex`, `claude`, `gemini`, `opencode`, `glm`
- `tier_s` / `critical_consensus` multi-AI review flow with Claude lead + Codex/Gemini/OpenCode reviewers
- Owner-curated internal MCP tools injected into coding providers during sessions

Two modes:
- Peer programming: conversational via LiveKit voice or OpenWebUI, with explicit session activation and background turns through `send_message_async`. HTTP/SSE acknowledges without waiting for the coding provider; progress and final output remain queryable.
- Autonomous: async ticket delegation through the execution queue. Result delivery uses the configured channel and does not require the originating chat/voice connection to remain open.

Without an active session, `openceo-dev` retains local Spanish conversation. Generic conversation and administrative queries do not authorize starting code execution. Explicit provider choice, project/session selection, progress queries, and session close/cancel remain part of the session contract.

Project knowledge comes from the Obsidian vault and current repository evidence. The legacy `CONTEXT.md` runtime memory was retired in TASK-2026-00143; plans, review context, and completion signals keep their execution roles.

### PM Agent (retired, TASK-2026-00209)

Role, formerly: extract actionable tickets from client interviews and requirements documents. Retired together with marketing, sales, and media: the enum value, router aliases, catalog cards, graph node, session package, `pm_state`, and the `pm.default_tool` / `params.pm_tool` cascade are gone. Requirement payloads route to the pedido flow instead: `POST /api/pm/extract` accepts `{project, interview}` and registers one `undecided` ticket for the Owner to route; `GET /api/pm/status` no longer exists. The internal batch capability keeps its public name `openceo_create_pm_tickets_batch` (Owner-only now that the service exception retired) and the older name `notion_create_pm_tickets_batch` may appear in legacy prompts. The guided state machine, the three extraction engines, and the `openceo-pm` OpenWebUI model name live only in Git history.

### Secretary Agent

Role: Email sending and lightweight coordination tasks.

Flow:
- Parses email payloads from JSON, structured text, or optional LLM fallback
- Uses shared SMTP sender wrappers in `agents/secretary/tools.py`
- Supports sender account selection, `language=es|en`, reply-to, CC/BCC, and file attachments
- Uses institutional signature rendering from `tools/email_sender.py`

### Finance Agent

Role: Interpret financial intent and invoke scoped, deterministic tools against Jardal ERP. Jardal ERP owns documents, ledger, balances, budgets, and reconciliation; OpenCEO owns orchestration, policy enforcement, execution gates, and dated references/summaries.

The [Finance integration contract](finance-jardal-erp-integration-contract.md) defines the adapter within `agents/finance/`, with direct REST calls and closed semantic inputs. It is not a separate financial microservice. The contract's older assignment of business projects/tickets to OpenCEO is superseded by [the direction document](OPENCEO_DIRECTION.md); its financial rules remain applicable.

- Use `Decimal`, explicit currency, cutoff dates, and Jardal ERP document references. Do not replicate a ledger or live balances in MongoDB.
- Preserve operation identity across retries; add Jardal ERP fields only when a real workflow needs them.
- Keep `execute_money_movements=false` for the initial integration. Recording an already-confirmed movement is distinct from executing an external transfer, and an HTTP success is not proof of money movement.
- Jardal ERP owns business approvals; OpenCEO checks them and applies its technical gates. No arbitrary DocType/method API is exposed to an LLM.

Existing browser-workflow compatibility:
- Deterministic Playwright workflows for browser-based payment portals
- Automation navigates the portal, selects the oldest pending invoice, reaches the card screen, and stops before any sensitive confirmation
- Finance secrets are intentionally split into `openceo-core/.env.finance.local`

---

> **Retired (2026-10-11, TASK-2026-00187 / TASK-2026-00189).** Peer-to-peer agent communication and the Claim Model (never implemented) are retired; the agent returns hecho / bloqueado / no_aplica and the planner re-decides (TASK-2026-00202). Kept as history; superseded by Agentes por rol. Supersedes TASK-2026-00009 and TASK-2026-00011 (both Cancelled).

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

The CEO acts autonomously under a company mandate approved once by the Owner (2026-10-06, TASK-2026-00110; epic closed, historical base): it emits company work as pedidos the single planner shapes (ticket or objective), without per-ticket approval. Doing nothing is a valid outcome. The Owner steps in only where a human is required: new spend, an irreversible external action, a strategic replanning, a mandate change, or a policy-mandated human review — never plans. Autonomy never transfers ownership of business work from Jardal ERP and never authorizes an unrestricted stream of new tickets. The governing contract for new work is Agentes por rol (epic TASK-2026-00187) below; this section keeps the 00110 machinery its tickets have not replaced yet.

> **Single company Objective, single planner (amends "One engine, two scopes", 2026-10-06, TASK-2026-00111).** Identity lives in Jardal ERP: a group Task with real children (`parent_task`) and `depends_on`. Mongo `objectives` holds execution state only (capsule, cycles, replans, final review, telemetry) — never a business backlog. One global planner (Muse Code headless, one fresh run per cycle, `PlannerResult` validated with Pydantic + deterministic rules) is the only component that decides which tickets exist for an Objective. A deterministic dispatcher (0 tokens) launches ready tickets on events, with a single conditional backup trigger while tickets wait for a slot. Work is persisted with `next_run_at` and awakened reactively, never by per-minute database sweeps.

Normative sources: vault decision `decisions/2026-10-06-objetivo-unico-ceo-autonomo` (why + history), `openceo-core/objectives/routing_policy.md` (runtime policy the planner reads: core always in the prompt, per-agent detail on demand, split by TASK-2026-00201), and Agentes por rol below with vault decision `decisions/2026-10-11-agentes-por-rol-planner-unico` (epic TASK-2026-00187 contract). Behaviour the code does not have yet is marked `pendiente: TASK-2026-00NNN` below.

### Glossary

- **Objetivo** = a company objective. The only unqualified use of "objective" in business context. Objectives are NOT tasks. Objectives generate tasks.
- **objetivos personales** = Horizon's private objectives (`mission → objective → milestone → action → result`, `HorizonObjective` and related models in `openceo-core/horizon/models.py`). Always qualified; actions link execution by `task_id` and are never flattened into company tickets. Capacity, uncertainty, and owner scope remain explicit.
- **mandato de empresa** = the company mandate: optimization principles, axes with optional KPIs, budget, limits, review cadence, escalation policy. Distinct from Horizon's personal mandate.

### Identity and execution state

One Objetivo corresponds to one project/repo. Its identity is an Jardal ERP group Task; children are real Jardal ERP Tasks (`parent_task`) and ordering is `depends_on` (mapped from OpenCEO `blocked_by`; Jardal ERP answers 417 while dependencies are open, so states move in topological order, children first). Mongo `objectives` carries only execution state. Four entries feed the same engine: the CEO (a pedido inside the mandate, shaped by the single planner), the owner (voice, chat, or Jardal ERP), a ticket that outgrows its session (`convert_ticket`: the ticket itself becomes the group Task, no new record), and automatic promotion from Horizon. A ticket already inside an Objetivo decomposes within it; loose tickets go through the common API without fabricating an objective. Business priorities and approvals come from Jardal ERP, and financial measurements come from Finance's dated Jardal ERP reads.

> Implemented: TASK-2026-00013 (adapter group ops, no Jardal ERP pre-configuration needed), TASK-2026-00117 (engine outside Tier S in `openceo-core/objectives/`; one parent ticket per objective that IS the ERP group; top-down/API/MCP/voice entries; deterministic admission limits; delivery verification with dev-pool / deterministic review by domain; inverse group propagation). Corrected by TASK-2026-00175 (non-dev delivery needs a registered deterministic verifier per domain — none registered today, so unverified non-dev work parks as `capability_gap`; failed evidence or no verified delivery parks as `data_gaps`; cancelled/failed children are history, never proof). and TASK-2026-00121 (Horizon promotion: explicit company touch per action, company work promoted to ticket or objective Jardal ERP-first with the work spec only, escalation only on mandate signals, personal work stays private, owner capacity never brakes promotion).

### Single planner

Muse Code headless, one fresh run per cycle over bounded OpenCEO-compiled state, returns `PlannerResult` JSON; Pydantic + deterministic validation, then OpenCEO persists the valid tickets. The CEO decides *whether*, *when*, and *how much* (mandate, limits, anti-bloat); the planner decides *which tickets*; Python enforces quotas and resources. The Claude/Codex plan of a Tier S ticket is implementation planning for that ticket, not a second planner; neither is Horizon's deterministic day order.

> Implemented by TASK-2026-00112 (Muse headless → `PlannerResult`, `openceo-core/objectives/planner_models.py`). For objectives in a money-sensitive project (TASK-2026-00161) the prompt also carries a PROJECT TIER GUIDE section: prefer `tier_s` for money/algorithm work when the S justification applies, with A/B/C eligible under their normal justification — guidance, never a forced tier. Pedido runs carry the same section (TASK-2026-00204). Corrected by TASK-2026-00176 (concurrent-cycle isolation: per-objective exclusive outputs under `.openceo/objectives/<id>/` — planner and closer verdicts alike, the legacy global files unread — plus a per-objective single flight, so same-objective triggers coalesce and distinct objectives plan in parallel). The policy ships inside the core as a core file (`openceo-core/objectives/routing_policy.md`, TASK-2026-00181) plus per-agent detail (`openceo-core/objectives/routing_policy.d/`, TASK-2026-00201; today only `dev.md` with DEV ROUTING §18–§35). At boot the service checks every policy file and logs the core path, size and the composed fingerprint (ERROR if one is missing, empty, or the core is over the prompt budget, which would cut off its tail), the prompt carries the core plus an index of staged detail paths (staged per cycle under `.openceo/objectives/<id>/policy/`, the file channel the fenced planner sees; reading the dev file is mandatory to choose the tier), and every planning run stores `policy_sha` in `planning_runs` (12 hex of the composed core + details; plain hashing, never part of the prompt; TASK-2026-00184).

### Company mandate and escalation

The mandate is a versioned YAML beside `projects/<project>/project.yaml`: optimization principles the CEO and planner prompts carry (`principios`), axes with optional KPIs + data source, money/quota budget, active-work limits, CEO review cadence, and the escalation policy. The owner edits it by telling Horizon or the CEO ("nuevo eje: …"); an agent applies the change and git keeps history. The CEO never edits its own mandate. The owner is asked only for: new spend over the mandate threshold; an irreversible external action; strategic replanning; a mandate change; a policy-mandated human review. Consuming already-paid subscription quota is not new spend. Credentials are not an escalation: credentials, sessions and connection strings reachable from the session are ordinary inputs, checked in every accessible source first; a secret found in none of them blocks the ticket in `Working` naming the exact secret, and surfaces in the Horizon cockpit as blocked — never as a decision. Notice arrives as one Jardal ERP ticket in `Pending Review` with the exact human action, via the "Requiere humano" tray and Horizon, once.

> Parked objectives self-recover (TASK-2026-00481). Every park persists a `recovery` block (`attempts`, `due_at`, `last_reason`, `exhausted`); one event-driven timer (`objectives/recovery_scheduler.py`, woken by the park and sleeping to the nearest due time, cap 1 h) claims it with a Mongo compare-and-set and replans with a RECOVERY CONTEXT section, so a capability_gap/blocked/erp_error/config park no longer waits for a manual restart or a child close. Two attempts, then a recovery ticket in `jardal-openceo` (`coding_tool: tier_c`, musej, normal gates/merge); at most two ticket rounds per objective (`ticket_round` survives every park) and round 2 carries why round 1 failed. A recovery close that merged a fix never replans on the code that parked the mission (TASK-2026-00483): it parks the objective as `recovery_deploy` and the same timer restarts the service once it is idle (no autorun ticket in progress, no planner in flight, live checkout clean on `main`), through a transient user unit with `KillMode=process` so the restart survives its launcher; the boot that runs the fix resumes the objective without spending a retry, and after `deploy_max_wait_minutes` (default 120) it resumes on the running code instead of waiting forever. A diagnosis-only close resumes at once. Parks with `pending_plan`, owner questions or terminal status never schedule, and a closed objective is never resurrected. Tests: `tests/test_objective_recovery_481.py`, `tests/test_recovery_deploy_483.py`.

> Implemented by TASK-2026-00114 (`projects/company_mandate.yaml` + `openceo-core/company_mandate.py`: validated load, `requires_owner`, owner-gated writer, escalation parked in waiting/Pending Review), TASK-2026-00118 (autonomy by default: `waiting` pushes `Pending Review` only with a nameable human action — pending approval, owner escalation, phase waits; the default dev template skips human QA unless `dev.qa.require_human_review`; objective parks are operational reasons, `max_tickets` defers or completes; explicit discovery/HITL approvals and external `autorun=false` defaults stay) and TASK-2026-00142 (five categories: credentials out of escalation; missing-secret report parks in Working with a `params.secret_block` marker the cockpit reads as blocked).

### CEO loop

Scheduled review (mandate cadence) plus reactive review (objective closed or blocked, `capability_gap`, incident, research, deferred findings, KPI change) over the same persisted-trigger engine as Horizon and research. Each review ends in exactly one of: keep going, emit a pedido, escalate, close a mission, or do nothing; the single planner gives the pedido its form. A pedido links the mandate with a measured KPI or, when none is measured, a written justification. Missing capabilities are declared as `capability_gap`, never silently worked around. Business acceptance and transitions remain Jardal ERP decisions; agent completion and delivery to the user remain distinct events.

> Implemented by TASK-2026-00119 (`openceo-core/ceo/`: `models.py` CEODecision contract, `review.py` signal compile + one headless run + validate + act once + publish, `loop.py` scheduled/ reactive loop over one persisted trigger, `service.py` production binding; `ceo_reviewer` role in the global `ceo` block, `CEO_DECISION.json` in the orchestrator scratch). Scheduled review follows the mandate cadence (interval-based, no timezone); reactive review wakes on objective parked/closed (engine `ceo_wake`) and failed/escalated company executions (task funnel, personal scope excluded), debounced (`CEO_REACTIVE_MIN_INTERVAL_SECONDS`). Research results, deferred findings and KPI deltas compile as bounded signals every review; their future event emitters call `loop.wake(reason)` — no fake sources. New work links a declared KPI measured this review when one exists (closed v1 vocabulary: `objectives.active`, `erpnext.open/working/pending_review`; finance sources stay unknown until HZ-005), else a written justification (TASK-2026-00193). Without a valid mandate the review escalates deterministically and never invokes the model; there is deliberately no on/off flag — the mandate is the switch. CEO business effects flow only through sanctioned channels (Jardal ERP-first creation, `create_owner_escalation`); if a future CEO ever closes or cancels business work directly, it MUST use `TaskService.transition_ticket`, never a direct adapter write. Reshaped by TASK-2026-00213: the review emits pedidos (strategy) and the single planner decides their form (see Agentes por rol).

### Horizon cockpit

Horizon is the owner's primary interface: "qué sigue" and the daily brief combine the personal plan with what the CEO advanced (objectives, closed tickets), what is blocked (failed/escalated plus Working tickets parked by a missing secret), and what awaits a decision. The decisions tray reads the same source as the "Requiere humano" view (waiting tickets plus pending/changes-requested QA), filtered to waits that name a human action; the owner answers by voice, mobile, or API and the answer is applied (a blocked YAML run resumes or fails, a human phase decides, a standalone owner-escalation records the decision and closes to Completed). Escalations group into the brief except urgent ones (cost deadline, blocking); grouped items deliver once, urgent ones re-surface until answered. Company data enters Horizon read-only; nothing personal leaves. The day plan still runs without any LLM.

> Implemented by TASK-2026-00120 (`horizon/cockpit.py`, `HorizonService.cockpit/company_status/pending_decisions/answer_decision`, `TaskService.apply_owner_decision`, `/api/horizon/cockpit|decisions|decisions/*/answer`, voice intents `horizon_brief/decisions/decide`). Since TASK-2026-00119, company status also merges the latest published CEO review (`ceo` section, None when no review ran yet). Push delivery stays pending (TASK-2026-00026).

### Create project end-to-end

Project creation is an operations skill run (`skills/operations/crear-proyecto/SKILL.md`): the Jardal ERP `Project` (boundary tool, idempotent by `project_key`), the local git repo with the conventional remote URL (recipe line; provisioning the GitHub remote stays out — it needs a credential), the initial commit with README/CLAUDE.md/AGENTS.md-symlink/.gitignore/docs/work plus the validated `projects/<key>/project.yaml` mirror (exact CLI over `openceo-core/ops/create_project.py`, refusing to finish on validation errors), the vault hub plus its own `vault/README.md` line, and the Cloudflare subdomain for web projects (boundary tool). Every step is idempotent — re-running reports `existed` and duplicates nothing. Spend approval lives in the plan (TASK-2026-00207), never in the recipe; needs to create a project route to operations, never a `capability_gap`. Retired (2026-10-13, TASK-2026-00223): the Tier-0 workflow and its `ops.*` tools. Kept as history.

> Implemented by TASK-2026-00115 (`openceo-core/ops/create_project.py`) and TASK-2026-00214 (skill); TASK-2026-00223 retired the Tier-0 workflow and its `ops.*` tools.

### Dispatcher

Deterministic, 0 tokens: wakes on ticket created/ready, ticket closed/failed/cancelled (frees a slot), dependency resolved, quota reset (persisted trigger), and service start (bounded reconciliation). While ready tickets wait for a slot it keeps a single persisted backup trigger (e.g. every 5 min); with an empty queue there is no timer. Admission checks active limits (global, per project, per objective), Claude/Codex quota and reservations for Tier S (A/B tickets that would invade them wait as `waiting(quota_reserved)`, never silently drop to C), free resources, the per-repo lock, and lease/fencing against double dispatch.

> Implemented by TASK-2026-00113 (`openceo-core/dispatcher.py`: `Dispatcher` with a coalesced wake event plus one conditional persisted backup trigger in `dispatcher_backup`; atomic conditional-update claim against concurrent dispatch cycles — crash recovery is startup `mark_interrupted_tasks` plus the bounded reconcile, and explicit manual `POST /run` stays outside the claim by design; typed `waiting_reason` taxonomy; `RateMonitor.admission_for_ticket` with Tier S reserves; limit/repo parks are event-held without `resume_at`, only quota parks carry the reset instant; "not cancelled in Jardal ERP" is structural via the inverse webhook, with no live Jardal ERP reads per ticket). Corrected by TASK-2026-00177 (cancel fencing: `run_task` guards entry — force relaunches completed/converted, never cancelled — re-reads after the project lock, and discards results/failures when the ticket was cancelled mid-run; `patch_task` atomically refuses any status move out of cancelled; the Tier S cycle fences parks and the pre-merge close; a material failure on a cancelled ticket skips record + replan). Corrected by TASK-2026-00180 (a cancel landing — desk webhook, explicit transition, objective cascade — requeues direct dependents like a finish, so event-held `blocked_by` parks never strand).

Loose tickets (no `objective_id`) without an explicit decision are pedidos: the run pre-stage plans them with the single planner before executing (TASK-2026-00204, `openceo-core/objectives/engine.py` (`plan_pedido`) over the same `objective_planner` role, `PlannerResult` horizon [1,1], decision patched with provenance, spend held and quota re-checked for decided A/B/S). The keyword classifier and the dispatcher peek are retired (they lived here since TASK-2026-00137/00148/00171/00178). Explicit dev without a tier runs the safe C default (policy §25); undecided work waits `routing_missing` fail-closed. A money-sensitive project (TASK-2026-00161, `dev.routing.money_sensitive` with optional `money_domain`/`money_stake` vocabulary) feeds the planner prompt — objective and pedido alike — with a PROJECT TIER GUIDE section guiding toward `tier_s` for money/algorithm work without forcing it; an explicit `coding_tool` always wins, and other projects are unaffected.

### Anti-bloat rules (deterministic, before persist)

1. **Max N active tickets per objective** — no new tickets if existing ones are incomplete
2. **Concrete action required** — not "improve finances" but "generate Q1 expense report and compare with Q4"
3. **Check existing tickets first** — no duplicates
4. **Mandate link** — the pedido moves a measured indicator, or carries why in words
5. **Scope check** — the ticket must align with the Objetivo; if it deviates, discard

### Delivery vs outcome

An Objetivo closes on its delivery criteria, after the final review (`complete_candidate` never closes directly). The business outcome (KPI) is evaluated by the CEO afterwards; closing every ticket does not prove a metric moved. Final review by domain: dev = the current pool over the `objective/<id>` branch; non-dev = verification against criteria and artefacts. The closer judges every consolidated finding exactly once: a finding still without a verdict after the bounded retry parks the objective (`closer_no_verdict`) with the attempts as evidence — a missing verdict is never FALSO and the reviewed SHA does not advance (TASK-2026-00170). The final-review budget (`max_final_reviews`, default 2) never parks an objective: once spent, a clean dev delivery closes on the deterministic checks with `delivery.review = budget_spent` (the last round's fixes passed their own ticket reviews), and open work keeps the objective active until its own closes re-plan; at boot an orphaned review with open work replans whatever the budget (TASK-2026-00468).

### Safe tiers and material failure

Tier C is the default for specified work a single top-model session can own (TASK-2026-00148): no oracle required to select it. A/B leave the default only with `why_not_c` (what breaks in C); S keeps its consensus pair. All four executors are top-tier models — letters order the work, never the models, and no tier is cheap, free, or weaker. A declared qualifying oracle freezes before the build on any simple tier (refs + content hash + type); the orchestrator — never the builder — verifies it, and a builder touching a protected ref fails the ticket instead of closing it (`builder_tests` never trigger protection). A material simple-tier failure resets the candidate, preserves evidence, updates the objective and invokes the planner exactly once; there is no automatic C→B→A→S staircase and no unchanged same-tier retry. Promoted findings record deterministic SHA→ticket→objective attribution when git allows it; without attribution the Tier C late-failure rate stays unknown, never invented. The planner/objective telemetry keeps the tier mix (`tier_mix`) and each decided pedido keeps its provenance, as information only — no threshold derives from them. The orchestrator also RUNS the frozen oracle (TASK-2026-00178): the executable types run through the project's `dev.oracle_cmd` as a required post-build gate with the gates' retries (hashes are re-verified after every retry turn); without `dev.oracle_cmd`, or when a `preexisting_tests` oracle is already red on the base branch, the oracle is hash-protected only and the ticket steps say so; with mixed refs the gate runs the resolved ones and the steps name the rest as not verified (TASK-2026-00167). The Tier C chain (muse, dsh, claudep) is availability, not selection: the link that ran each ticket is recorded (`executor_link` in params, `by_link` and per-record in the Tier C telemetry) so the primary link can be contrasted with the fallbacks. The quota reserves are quota for the Tier S family: A/B admission and the C primary link honor them, the S cycle, its arbiter and the final-review arbiter consume into them down to zero (`can_launch(honor_reserve=False)`). A parked S gate resumes at the reset of the blocking window — one window exhausted, its own reset; both, the later (TASK-2026-00166). The per-ticket review switch is orthogonal to the tier (TASK-2026-00249): A/B/C review on by default and `review: off` keeps tier, builder and gates while skipping reviewer invocations; the planner decides it per ticket by risk and every off carries `review_reason` (TASK-2026-00492), while an explicit user instruction still wins; S always reviews and rejects off with `review_conflict`, invalid input waits with `review_invalid`, and the admitted mode freezes into `params.review_effective` (`{mode, origin}`) so resumes keep it.

> Implemented by TASK-2026-00116 (`agents/dev/tier_oracle.py`: freeze/verify; `session.py`: oracle gate + `_material_simple_failure`; `objectives/engine.py`: `record_material_failure`, Tier C telemetry, planner prompt section; `orchestration/inbox.py` + `cycle.py`: §31 attribution + late failures) and recalibrated by TASK-2026-00148 (C default, `why_not_c`, uniform §24, `tier_mix` + classifier telemetry). Corrected by TASK-2026-00178 (S by orchestration value in any domain, reserve semantics, B without an oracle-absence door, oracle execution via `dev.oracle_cmd`, `executor_link`, params persistence) and TASK-2026-00167 (mixed refs: unresolved named as not verified in the steps).

### Flow

```
Company mandate (rules approved once)
      ↓
CEO review (scheduled + reactive) → pedido | escalate | nothing
      ↓
Objetivo — identity: Jardal ERP group Task (+ real children, depends_on)
           execution: Mongo `objectives`
      ↓
Single planner (Muse headless → PlannerResult → deterministic validation)
      ↓ 1–3 tickets
Dispatcher (deterministic, 0 tokens): events + conditional backup
      ↓ limits · quotas/reservations · resources · lease
Execution: dev (S/A/B/C) · research · workflows
      ↓
result → replan only if something changed → final review → close
      ↓
Only if policy requires: Pending Review + tray + Horizon → OWNER
```

The objective close notification fires once, after the terminal state is persisted: `run_task` notifies post-persist from the session's stashed close context (Tier S notifies inline and the funnel skips it, so no double replan), and the closing ticket itself never counts as open work (TASK-2026-00169). Corrective children created by the final close carry the same minimum delivery contract as planner children — dev agent hint, acceptance, and the anchored repro/fix artefacts — so the fixed child passes the next delivery check instead of parking it in data gaps (TASK-2026-00173). A Horizon-linked promotion stub is tierless by construction: the single objective planner adopts it with a `completes_task_id` entry (patch by task_id, promotion provenance preserved, never recreated), a planning failure degrades to the pre-adoption wait, and retrying the promotion re-drives the plan while the stub stays tierless (TASK-2026-00174).

---

## Agentes por rol — single planner, role agents

Contract of epic TASK-2026-00187 (planner único, agentes por rol con skills y tools de frontera). It governs all new work; TASK-2026-00110 is closed and stays as the historical base. Normative sources: this section, vault decision `decisions/2026-10-11-agentes-por-rol-planner-unico` (why), and the epic's private design attachment. `openceo-core/objectives/routing_policy.md` is the core the prompt always carries; per-agent detail (today `openceo-core/objectives/routing_policy.d/dev.md`, DEV ROUTING §18–§35) is staged per cycle and read on demand (TASK-2026-00201).

### Vocabulary (epic wording, not reopened)

- **Owner**: the person who owns the company (never "dueño" ni "presidente" in the system).
- **el personal**: people with a role in a project (noun). Horizon's adjectival "personal" (contexto personal, objetivos personales, espacio personal) is private context and stays; qualify when one sentence could mix both.
- **misión, objetivo, ticket**: mission (above objectives, chained by the CEO strategy), company objective, ticket.
- **pedido**: work with no decided agent yet.
- **agente**: a headless CLI engine plus a role profile.
- **skill**: SKILL.md with optional scripts (Agent Skills standard).
- **borrador**: an unconfirmed skill.
- **workflow**: the exact part of a skill.
- **playbook**: a ticket recipe across agents.
- **tool de frontera**: a boundary tool.
- **no_aplica**: the agent returns the work to the planner.

### Single planner

One planning function of the CEO decides every pedido, loose or objective child: ticket or objective, agent, suggested skill and, for dev, the tier. Explicit requests (Owner, approved playbook, validated plan) are validated against the catalog only. No separate router; no keyword routing; no silent reclassification. (TASK-2026-00204: `objectives/pedido.py` detects pedidos — loose work with no coding_tool, hint, target, objective or workflow marker; `plan_pedido` runs the same headless planner over the pedido alone and patches the validated decision (re-decisions away from dev clear the tier and dev-only params); the run pre-stage plans before executing, holds declared spend and stops decided A/B/S queued so the next dispatch re-admits with the exact tier; invalid plans back off 5/15/30 min and fail past 3 tries; capability_gap converts via `convert_ticket` (which plans internally); unplannable pedidos fail fast instead of stranding. Result model TASK-2026-00198: target_agent is any enabled catalog agent — dev, owner, staff names — skill is an optional suggestion for any agent, coding_tool stays the dev-only tier. Tests: `tests/test_single_planner_204.py`. The CEO review is the strategy side of the same function (TASK-2026-00213, 5th action TASK-2026-00218): five actions (`keep_going`, `crear_pedido`, `escalate`, `do_nothing`, `close_mission`), and `crear_pedido` carries need plus mandate link only — known project, measured KPI or written justification, optional mission chain, Owner's or proposed acceptance, declared costs, optional new-project key — never form, agent, skill or tier; validation keeps duplicates (open titles plus active-objective goals, both blind fail-closed), mandate, project and mission (unknown/inactive/cancelled-shell fail closed), while form limits apply where the form materializes (objective admission, ticket dispatch). Tests: `tests/test_ceo_review.py`.)

### Single catalog and role profile

One catalog in git: agents, the Owner, staff, each agent's boundary tools and each role's skills. A short index always loads; each agent's detail loads on demand. Agent = headless CLI engine (Muse Code default, OpenCode second) + role profile (card, its skills, its tools, none of the engine's extras) + per-ticket folder. The role is defined once, engine-neutral; the executor translates it per engine. (TASK-2026-00191, TASK-2026-00195, TASK-2026-00197, TASK-2026-00200.)

Format (TASK-2026-00191): `catalog/index.yaml` (version + one line per agent: what it does and does not do), one card per agent under `catalog/agents/` (type ai/human, engine and profile, boundary-tool box, skills dir and skills, typical inputs/outputs, enabled with reason + ticket when disabled), and `catalog/staff.yaml` (roster, empty until TASK-2026-00225). Departments (TASK-2026-00244): `index.yaml` declares `departments:` once; AI cards reference one by name while the owner stays department-less (human channel, never executor). Departments group responsibilities for the planner; they grant no permissions and add no directors or routers. A new employee dispatches from its card alone: `router.resolve_selected_agent` maps manual aliases first, then any catalog name to itself, and `graph._after_ceo_route` sends enabled AI catalog names to the generic node — no enum member, manual alias or route edit (R6 retired). Content language is English: the planner reads it. State: dev, owner, operations, secretary, finance and research enabled (operations with its first skill in TASK-2026-00214, secretary with its send-email skill in TASK-2026-00220, finance with its five exact finance skills in TASK-2026-00221, research with its investigar recipe in TASK-2026-00222); pm, marketing, sales and media retired. The CEO and the planner are not catalog executors. `openceo-core/catalog.py` loads it with Pydantic and fails closed (no partial catalog; every problem named; `ceo`/`planner` reserved); `catalog_fingerprint` is the 12-hex SHA-256 planning runs store, and an invalid catalog refuses service startup via the `main.py` lifespan gate. Boundary-tool names and skill directories validate shape only until TASK-2026-00199/00203 create them. Tests: `tests/test_agent_catalog_191.py`.

Profiles (TASK-2026-00197): `catalog/profiles/<role>.yaml` holds the engine-neutral role (card, rules, allowed skills, output schema, engine) for dev (shared by the builder, the closer and the Tier C link), the planner and ceo principals, operations (enabled in TASK-2026-00214), secretary (enabled in TASK-2026-00220), finance (enabled in TASK-2026-00221), and research (enabled in TASK-2026-00222). Cards point at their profile; the loader cross-checks name, engine and skills. `orchestration/role_muse.py` translates a profile into a Muse run: argv extras (reminders, foreign context, session persistence and workflow/subagent delegation off; the card rides `run.developer_prompt`), a per-run `settings.json` overlay (skill activation all-off but the role's, enumerated live; the MCP map replaced by the badged server) bind-mounted over the motor settings by the fence, and a one-shot badge (ticket-bound agent, readonly planner/ceo, project-scoped service closer) minted per run and revoked after. Interactive dev sessions get the dev profile with a 7-day service badge (TTL-only revocation; relaunches rebind it). The isolated reviewer stays plain muse. The motor is pinned (`MUSE_PINNED_VERSION`, no auto-update in the fence); the echo smoke test replays the baseline and every profile and fails below nothing but its acceptance (prompt under 60%, no foreign skills/tools, extras off). Tests: `tests/test_role_profile_197.py`.

Translators (TASK-2026-00200): the same profile renders per run engine, chosen by the run provider (the profile file is never rewritten to switch engines). `orchestration/role_claude.py` renders a Claude run (the card via `--append-system-prompt`, explicit `mcp__<server>__<tool>` box names plus file/shell builtins via `--allowedTools`, the badged server via `--mcp-config` + `--strict-mcp-config` over a per-run overlay); `orchestration/role_opencode.py` renders an OpenCode `agent.<role>` entry (card + rules + skills prompt, skill allowlist, badged remote MCP map replacing the Owner's) selected with `--agent`. The live executor dispatches profile runs by provider (unknown providers refuse, never render the wrong motor); Tier A runs the dev profile through the Claude translator and Tier B carries the ticket MCP token via a per-run codex config override (rules travel in the workspace `AGENTS.md` everywhere). Equivalence tests: `tests/test_role_translators_200.py`; live verification lands in the epic Acceptance. Engine selection (TASK-2026-00246): `orchestration/engines.py` maps the five engines (muse-code, claude-code, opencode, codex-cli, agy) to provider, model default, base argv and prompt channel; a card names an engine plus, for agy only, one of its three Owner logins, and `params.engine`/`params.engine_account` override per ticket with documented precedence and no silent fallback. `orchestration/role_codex.py` carries the ticket badge as a `-c` MCP override (Tier B pattern); `orchestration/role_agy.py` renders `--print` plus a per-run overlay of the account's `mcp_config.json` with the badged server. Each run records its engine in `params._engine_attempt` and re-resolves next time; recovery never hot-swaps. Tests: `tests/test_cli_engines_246.py`.

Consumers (TASK-2026-00195): the planner registry derives from the catalog (AI agents only); the enum, the router aliases and the wired graph nodes validate against it at boot, fail-closed (`catalog.find_consumer_mismatches`, enforced in the `main.py` lifespan). R6 retired with TASK-2026-00244: catalog names resolve through the explicit vocabulary by construction, so an enabled AI agent needs no enum member and no manual alias; the router still never reclassifies by text. The graph agent gate (`graph.resolve_agent_gate`) routes wired names by name and parks unknown, disabled, human or `ceo` targets in `routing_missing` with their reason — never reclassifies, never fails silently. The last disabled-but-wired agent (pm) was removed with its legacy session in TASK-2026-00209. Tests: `tests/test_agent_catalog_consumers_195.py`.

Generic execution (TASK-2026-00205): an enabled AI agent with a muse-code profile runs on the generic graph node (`orchestration/generic_agent.py`): exact skills first (the skill dir carries `workflow.yaml` and the ticket names it in `params.skill` — the YAML engine runs it, the LLM is never invoked), else one fenced Muse run with the role spec, the ticket badge, the ticket folder and the verdict schema. The gate admits enabled unwired AI names and still parks disabled, human, untranslated-engine or unknown names; each migration flips its own agent (enum member + router alias + route mapping; operations is first, others follow in 00220/00221/00222). Verdicts map per 00202 (missing verdict stays legacy, never a failure — unless the output was truncated, which fails closed as bloqueado since a head-cut parse is untrustworthy; a crash rewrites a hecho claim to bloqueado so the planner loop still runs); unknown-tool waits (commented on Jardal ERP as blocked-tool, review 00006) and HITL waits propagate as the engine returns them. The orchestrator posts the run verdict on the business mirror (`run_task` publishes the node's `result_comment`); agents never change states. The ticket folder survives the run for deliverable evidence (lifecycle cleanup is future work). Tests: `tests/test_generic_agent_205.py`.

### Agent isolation

No agent sees secrets or leaves its folder. Env is an allowlist (`child_env`: locale/terminal/identity only, secret-pattern backstop on the base; explicit overlay wins as operator intent — motor auth, ticket badges, per-slot env). Files are a bubblewrap fence (`orchestration/agent_sandbox.py` over the research `BubblewrapBackend`, extended with agent kwargs): system read-only, motor/toolchain paths read-only, the ticket workspace read-write, private /tmp, own pid namespace, shared network (motors and the local MCP need it; egress stays unrestricted and declared). Unlisted paths are simply not mounted: ssh keys, browser/cloud credential stores, pasted token files, sibling checkouts, the vault, foreign /tmp. Nested service env is masked by a pruned walk: every `.env*` file (except `*.example`-style docs, which stay readable) and every `.runtime` dir under the workspace or a mounted config root reads empty — including `openceo-core/.env` when the workspace is the checkout (review H1). Home mounts are narrow (`~/.config/muse`, `~/.config/opencode`, `~/.local/share/claude|uv|claude-skill-wrappers`, never whole `~/.config` or `~/.local/share`). No bwrap, no run (`IsolationUnavailable`, fail-closed). Per-project exception (TASK-2026-00481, Owner order; confined by TASK-2026-00483): `dev.isolation: host` runs the builder of tiers A/B/C and the Tier S builder host-confined, because some project tests need Docker, package stores and caches the fence does not provide (see Execution environments below). The motor keeps its real `$HOME`, so its own auth/config reads work unchanged; writable motor state it needs is tmpfs or a per-run snapshot (dsh headless profile, agy state JSONs). Repo-bound roles (planner, CEO, closer, builder) run fenced in the checkout they read; reviewers run fenced in worktrees (main `.git` bound read-only so git works); generic catalog-agent runs use an empty per-run ticket dir (`.runtime/agent-work/`, removed after); quota monitors run fenced in persistent per-provider dirs (pre-fence panes reaped on first touch). Config-root mounts skip the workspace and its ancestors (a config path inside the checkout must never shadow it read-only) and recreate symlinked motors. Declared extra roots (TASK-2026-00214) are the exception with a rule: skilled runs mount the repo read-only, creation agents mount their creation roots writable, shallow first with the workspace rebound read-write when covered, every extra root secret-masked like the workspace. Carve-outs, all documented in code: opencode QA binds the live opencode state read-write (the session must land there); the dsh harness binds read-only as motor code. Residuals for the pilots: codex/qwen/agyd/opencode/agy/kimi real-run writes (startup-tested only); live and monitor pane shells keep the dirty interactive env (the CLI child is fenced, shell escapes inherit it). (TASK-2026-00192 + review follow-ups. Tests: `tests/test_agent_isolation_192.py`; validated with fenced `muse`/`claudej`/`dsh` runs.)

### Execution environments (decision, TASK-2026-00483)

Where an agent runs is declared per project and per role, never improvised by the agent. Three levels:

1. **sandbox** (bwrap fence above, the default): every read-only role (planner, reviewers, closer, CEO), every generic catalog agent (research, finance, secretary, operations: they write documents in a ticket folder and never build; the Python packages a skill script needs come per run through `uv run` from its PEP 723 header, never installed on the host, see `skills/README.md`, TASK-2026-00485) and every builder of a project that does not opt out.
2. **host-confined** (`dev.isolation: host`): the builder sees the whole host like a host process (toolchain, Docker socket for testcontainers, pnpm/bun/pip caches, real home) through `plan_host_agent`: a transparent bwrap (`--dev-bind / /`) that still binds the role overlays (badge-scoped MCP box and skills instead of the global engine config), the per-run trace dirs and `--die-with-parent`, under a user namespace with `no_new_privs`. sudo, apt and setuid helpers cannot run, so nothing is installed on the system; dependencies live in the repo (lockfile) and in the user's caches. The env still goes through the allowlist. For trusted internal repos on the host stack (Node 24 + pnpm/bun, Python 3, Go) whose tests need Docker: today `jardal-openceo` and `jardal-erp`.
3. **container** (designed, built with the first project that needs it, TASK-2026-00484): a per-project dev image declared by the repo (`.devcontainer/Dockerfile`), based on the host's Ubuntu LTS so the engine CLIs mount read-only from the host as the research worker already does (`research/runtimes/worker.Dockerfile`). Builder, gates and oracle run in that image with the worktree bind-mounted at its host path, the run's own HOME and the engine auth mounted, caches in named volumes per project, cpu/memory/pids limits, no Docker socket (service dependencies come from compose sidecars the orchestrator starts, or from Sysbox when tests must start containers), labelled images pruned to the last two per project. For any project whose toolchain is not the host stack (PHP, Java/Gradle, .NET, Rust, Flutter/Android) and for third-party or client code that must not see the Owner's home.

Onboarding rule: a new project stays `sandbox` until its tests need Docker or a toolchain outside the sandbox; then `host` if it is an internal repo on the host stack that runs no live production process from its checkout (the trading bots do, so `jardal-trading` never goes host), otherwise `container`. Per-project Docker images for the current projects were rejected: all six are on the host stack, so N images would duplicate the same toolchain (1-3 GB each plus caches), add a rebuild-on-lockfile cycle and still need the Docker socket for testcontainers, without making any build more reliable. Simple tiers branch in place in the live checkout, so a ticket waits (`repo_busy`, 15 min) while someone else's uncommitted tracked changes sit there, instead of carrying them into its branch.

### Boundary tools

A tool exists only if it meets at least one condition: (a) it uses a secret the agent must not hold; (b) it enforces a lock that does not depend on the LLM obeying; (c) it writes to the business record under its own rules. Everything else is terminal plus skills. Each agent sees all its role's tools on any ticket; the ticket token is a badge (which agent, on which ticket) enforced server-side; no anonymous access to MCP or REST; dangerous tools carry their lock inside; agents comment and attach on their ticket; code changes states. (TASK-2026-00196, TASK-2026-00199, TASK-2026-00208.)

Identity (TASK-2026-00196): `openceo-core/mcp_identity.py` issues opaque ticket tokens (SHA-256 stored, kinds owner/agent/readonly/service; planner and ceo are read-only principals, never catalog agents) and a FastMCP middleware filters `tools/list` to the grant's box and blocks out-of-box calls; calls with no badge get the default project-management box (`ANONYMOUS_BOX`: ticket tools, DNS, Jardal ERP project — the agent-specific tools need a badge), while a present but invalid badge answers 401 on every method of `/internal/mcp` (logged with client host, never the token), and every call lands in the `mcp_calls` audit log. The box resolves from the agent's catalog card (`boundary_tools`; dev reads its ticket and comments/attaches on it, owner sees all 26, badge-bearing calls enforce their catalog box and ticket scope); agent badges bind to the Jardal ERP mirror name (`TASK-2026-NNNNN`), never to the Mongo uuid, which the issuer refuses (TASK-2026-00214); badges die on terminal execution (funnel hook, best-effort) plus a TTL backstop. The shared `MCP_SERVER_API_KEY` is retired on the internal mount; the research broker keeps its own bearer and consumers. PM extraction keeps working through a per-run service badge (single tool, project-scoped, sunset in TASK-2026-00209); interactive dev sessions run under the dev role profile with a project-scoped service badge (TASK-2026-00197). Tests: `tests/test_mcp_identity_196.py`.

Registry and surface (TASK-2026-00199): the YAML registry keeps 16 builtin tools plus the 3 conditional research ticks (removed so far: `pm.*`, `core.check_registry`, `cloudflare.*`, the secretary notes and `ops.*`). The internal mount exposes 26 boundary tools: the ticket/objective set, one DNS tool (`manage_dns_record`, create/delete), email with the sender list in its description, the Jardal ERP project ensure, the five finance tools (practice-only Elfec), and the two research bridge tools (submit/get under the ticket's consumer). Each description documents its criterion; `catalog.MCP_BOUNDARY_TOOLS` is the canonical set — unknown card boxes fail the load and unknown registrations fail the mount build. `openceo_create_pm_tickets_batch` answers only to the owner badge (the service exception retired with the PM agent in TASK-2026-00209). The one-shot notes migration lives in `orchestration/notes_migration.py` (personal to Horizon capture, company to the vault or the named ticket; dry-run by default). Finance runs as a catalog agent (TASK-2026-00221): its box holds the ticket tools plus the five finance tools, and its five skills run exact over the registry; refused inputs fail the run as `invalid_input` with the refused output on the record, never a silent hecho. Tests: `tests/test_boundary_tools_199.py`, `tests/test_secretary_agent_220.py`, `tests/test_finance_agent_221.py`, `tests/test_research_agent_222.py`.

Locks (TASK-2026-00208): email to off-allowlist recipients, DNS deletion and Elfec runs pass `requires_owner` inside the tool (`tool_locks.py`, pure) and escalate as `lock` owner-questions; the tool answers pending and the agent ends bloqueado. The approval executes the stored action exactly once (guarded by the execution marker on the question), then resumes the origin; re-runs replay the stored outcome. A mid-run lock park wins over the run outcome in the funnel, and a YAML step held on a lock blocks its run as `blocked_tool` instead of completing. Resuming re-runs the origin from scratch (established engine semantic): locked steps replay their stored outcome, earlier unlocked steps re-execute. Agent badge calls bind their ticket/project params to the grant; service badges never call lockables; anonymous callers may only request a DNS deletion with a project (never an origin ticket) — email and Elfec need a badge. Per-project `secretary.allowed_recipients` (exact or `@domain`) is the email allowlist, fail-closed when absent. Email attachments snapshot their sha256 at request time and refuse to send on mismatch. Tests: `tests/test_tool_locks_208.py`.

REST half (TASK-2026-00188): every write route except the login-flow trio (login/logout/refresh, allowlisted in the test) carries session, permission, admin (`require_admin`, tmux/dev/voice only), API-key (`/v1`) or webhook-secret auth — the inventory test fails on any new anonymous write route or mount. The port binds 0.0.0.0 for the Docker networks and UFW restricts it to those plus localhost (verified in the ticket with ss + ufw, not internet-reachable). Tests: `tests/test_rest_auth_inventory_188.py`.

### Agent result

The agent ends hecho, bloqueado or no_aplica, plus the reason. When it cannot, it describes the need (it never asks for tools it cannot see) and the work returns to the same planner once. No extra tool verifiers. (TASK-2026-00202: the verdict schema is `objectives/agent_result.schema.json`, parsed from the agent output with `muse` runs shaped by `--output-schema`; an objective child that ends bloqueado or no_aplica returns once to the same planner — code tags the relaunch with `params.returns_from` — and a second unresolved return parks the objective as `capability_gap` for the CEO review, reaching the Owner only through the mandate. Loose tickets return the same single way (TASK-2026-00204): the first verdict replans the pedido and requeues it with the new decision, and a second unresolved verdict stays failed with its return history for the CEO reactive review. Tests: `tests/test_agent_result_202.py`, `tests/test_single_planner_204.py`.)

Builder work survives its verdict (TASK-2026-00478, found when a tier_b builder implemented UI work, closed `bloqueado` because the global suite needed Docker, and the simple tiers reset and dropped the branch). The sandbox has no Docker by design and the orchestrator, not the builder, runs the gates on the host, so `agents/dev/builder_contract.py` states both in the builder prompts (simple tiers and Tier S): the project gates and oracle command, the missing Docker, and that a check the sandbox cannot run is no impediment (`hecho` plus what could not run in `evidencia`). A `bloqueado` that leaves work in the tree gets one continuation turn in the same run (tree, handoff, previous verdict, environment facts; the same resume-by-prompt path as a provider retry, no tmux or session persistence): `hecho` goes on to the gates, `bloqueado` again — or a turn that errors or answers no verdict — leaves the original verdict standing. Before any discard the simple tiers commit the candidate to a branch named blocked/, declined/ or failed/ plus the work branch name (handoff included), record it in `params.kept_work` and name it in the evidence and in the planner return's `necesidad`; Tier S already checkpoints its branch. The review waits that resume (`review_no_quorum`, review pool/router and gates config errors) checkpoint the built branch instead of dropping it, and a restored checkpoint tells the builder not to redo its work. Tests: `tests/test_blocked_candidate_478.py`.

Engineering quality bar (TASK-2026-00492, found when MIS-4804F506 shipped a hand-rolled UI: no stylesheet, a home-made router on `history.replaceState`, brand-prefixed components and a "3D" view without Three.js). The CEO and planner had contracted "reuse existing web", "restyle only", "no new framework" and "minimal diff" over a base without CSS or router; no builder rendered the result; the design document the goal named was never committed. Now `builder_contract.ENGINEERING_STANDARD` rides every builder prompt (simple tiers and Tier S): standard maintained libraries of the stack instead of hand-written substitutes, repo conventions that never perpetuate a substandard base, conventional names, results verified the way users meet them (a browser such as agent-browser for interfaces, real requests for APIs, apply-and-revert for migrations) and behavior tests that never pin implementation details. The Tier S planner never decides "no new dependencies" alone. Routing policy §44 and the planner's ENGINEERING QUALITY guide forbid shortcut constraints, translate brand words into engineering terms, require inputs committed on the base and favor cohesive tickets; the CEO describes outcomes, not shortcuts; reviewers (text-only, no screenshots) focus on user-visible breakage; the objective final review asks for production grade. Mandate principles qualify reuse ("cuando cumple el estándar profesional") and add production quality. The mission brief travels whole: `objectives.missions.mission_brief_lines` (2000 goal and 6000 numbered-criteria characters, overflow counted, never silent) feeds the CEO MISSIONS section, the mission pedido and a MISSION block in every mission-linked objective planner prompt; before, the CEO saw 300 and 400 characters and the objective planner none. Tests: `tests/test_engineering_quality_492.py`.

### Skills, drafts and crystallization

SKILL.md recipe always present; a script only where exactness or safe repetition is needed, never for a one-liner; the workflow is the exact part of a skill; each agent loads only its own skills. Internal crystallization without Owner approval: on ticket close the agent judges recurrence and leaves a draft; reusing the draft on another ticket turns it into a skill. The save step is code: everything new enters as a draft, a draft promotes only from another ticket, at most 10 drafts per role (oldest deleted), an agent touches only its role's skills, everything in git with the ticket number. (TASK-2026-00210: the agent writes `skill_drafts/<skill>/` in its ticket folder following the role card line plus the generic prompt block; `orchestration/crystallization.py` runs fire-and-forget after completed/failed closes — escalated closes stay out — stamping the header itself, validating each draft with the normative loader plus `muse skills validate` and fenced script tests when scripts changed, then committing card + profile + skills in a dedicated worktree over origin/main with the ticket number and pushing at once with rebase retries, serialized by a blocking flock; failures discard the draft with a ticket comment and never touch the outcome. The engine has no memory surface left on: role runs already persist nothing — `--no-session-log`, `--no-foreign-personal-context`, reminders/delegation/workflows off, pinned by 00197 — so learned content can only land in skills. Dev has no draft line yet: its runs live in worktrees where a drafts dir would pollute the diff. After pulling crystallization commits the Owner checkout recreates exposure links with `catalog.sync_skill_links`, since the push lands on origin/main, not the checkout. Tests: `tests/test_crystallization_210.py`, including real-validate/fenced-tests integration.)

Format (TASK-2026-00203): `skills/<role>/<skill>/SKILL.md` with optional scripts/ and references/ dirs; the header carries status (borrador/activa), the creation ticket, the date and the optional handoff contract (declared `inputs`/`outputs`, TASK-2026-00211; every shipped skill declares both, see `skills/README.md`). A skill transfers by moving its dir and updating both cards/profiles plus the exposure link (TASK-2026-00244): single owner by location, creation ticket kept as history, each role learning only in its scope. `openceo-core/catalog.py` loads them fail-closed: an AI card lists exactly what is on disk, every script has its sibling test, names are unique across roles, each skill is exposed once via a `.codex/skills/` symlink — the only project skill dir Muse discovers — while role activation keeps every other skill off, and profiles load every card skill (review 00005). Exposure links are local state (`.codex/` is gitignored): `catalog.sync_skill_links` recreates them, see `skills/README.md`. Profiles add the engine skills they use (`create-skill` everywhere, `git` for dev; builder and closer share the dev profile). The planner receives the per-agent skill index in its prompt and a suggested skill must name a real one of its target, active or draft. Tests: `tests/test_role_skills_203.py`; `muse skills validate` runs in the suite.

Verification (TASK-2026-00216): non-dev objectives close on evidence, not on completion alone. `objectives/delivery_verification.py` checks each completed non-dev child generically — hecho verdict, every declared skill output cited in the structured result, every required `evidence` name cited in its `evidencia` (normalized, case- and separator-blind) — and a skill needing a stricter domain check ships `verify.py` (`verify(child, outputs)` returning the 00175 report; runs after the generic check passed, broken means gap). The generic prompt shows the agent its skill's declared outputs and evidence; exact runs cite their workflow step outputs in the synthesized verdict. `service.py` registers the generic check per enabled AI agent; domains outside the catalog still park as capability_gap and missing evidence as data_gaps. Tests: `tests/test_nondev_verification_216.py`.

Handoff (TASK-2026-00211): the planner's frozen INPUTS text never carries real stage outputs, so `orchestration/ticket_handoff.py` does. On close, a COMPLETED run with a hecho verdict publishes before the patch (one patch, deterministic pre-dispatch): verdict files inside the ticket folder become private mirror attachments, texts and evidence stay data, and the record lands in `params.ticket_outputs` (20 files / 256 KiB each; symlinks, hidden, staged-upstream and draft paths never collect; every failure is marked, never raised). On launch, the generic executor resolves the `blocked_by` predecessors from completed rows only and delivers everything recorded — verified files staged under `upstream/<task_id>/` (sha-checked) plus a prompt section on the LLM path, `upstream_outputs` params on the exact path. The plan's order is the delivery map until playbooks exist (TASK-2026-00217); no fuzzy match against the plan's free-text inputs. Tests: `tests/test_ticket_handoff_211.py`, including the two-chained-tickets acceptance with scripted executors.

### Playbooks

Ticket recipes across agents, a reference for the planner, never rigid. One stage is one ticket when it has its own owner, deliverable, failure or approval; one step goes inside a workflow when it is mechanical. (TASK-2026-00217: `objectives/playbooks.py`. On close the planner judges recurrence from the deterministic brief — goal, completed stages with agent/skill/outputs, acceptance as stop — and a recurrent judgement opens one owner question, kind `playbook`, with the drafted recipe (stages, order, inputs/outputs, stop); the same pipeline runs on Owner request via POST /api/objectives/{id}/playbook-request. Approval saves `playbooks/<name>/playbook.yaml` in git over a dedicated origin/main worktree with the approval reference (failed save keeps the question open); updates version the file. Loading is fail-open for the prompt index; the planner follows approved playbooks by default (`playbook` + `playbook_deviations` in PlannerResult, validated against approved names), and `proposed_playbook_update` opens the update question. Tests: `tests/test_playbooks_217.py`.)

### Human channel

The Owner only ever receives: the 5 mandate categories (new spend, irreversible action, strategic replanning, mandate change, policy-mandated review); questions only they can answer from their own data; the acceptance-criteria proposal when they gave none, without blocking work; playbook and new-boundary-tool approvals. Never plans. (TASK-2026-00206: every owner need is one owner-question ticket in waiting/owner_question → Pending Review with the exact human action, answered from the cockpit, by voice, or from the Jardal ERP desk; approve requeues the origin with the owner text, deny fails it with a bloqueado verdict that returns once to the planner, and new_tool approval creates the dev ticket first. Kinds question/lock/new_tool/playbook in `owner_channel.py`; Tier S asks via `cycle.ask_owner`; the planner proposes via `PlannerResult.proposed_tools`. Resumed runs read `params.owner_answer` in their prompt (dev + Tier S planner); a recorded decision is honored on retry, and hook producers clear the default phase template. Tests: `tests/test_owner_channel_206.py`. Mandate TASK-2026-00193; spend gate TASK-2026-00207: declared plan spend is checked against the mandate before anything persists or launches — approval launches the stored plan, denial returns the motive to the planner. Tests: `tests/test_plan_spend_207.py`.)

### Missions over objectives

The Owner or Horizon gives a mission; the CEO strategy chains objectives until it holds. Spend declared in a plan asks approval before launch (TASK-2026-00207). (TASK-2026-00218: a mission is one Jardal ERP group Task whose children are objective groups, plus a Mongo `missions` document; entries API/MCP/voice/chat/Horizon through `TaskService.create_mission`; creation wakes the CEO; mission pedidos are born under the mission group and convert linked; each close records the ledger and wakes the CEO, who emits the next pedido or `close_mission` (5th review action: active mission with acceptance, every objective terminal, at least one closed, evidence cited); without Owner criteria the CEO proposes in its first pedido acceptance, adoption asks through the human channel without blocking (48h deadline, silence or «analízalo tú» confirms), and answers adjust/confirm/dispute; the cockpit shows progress plus aggregated cost. Tests: `tests/test_missions_218.py`. No cancel-convergence flow in v1: a desk-cancelled mission shell fails mission ops closed instead of chaining under it.)

### Measured cost

Every per-ticket execution reports one run record (tokens in/out/cached, duration, engine) and `run_task` folds it onto the ticket's stored `cost`; the objective aggregates its tickets, and the mission its objectives, for the CEO strategy, the planner and the cockpit. Unknown is null, never 0: token totals stay null until a run measures them and the aggregation stays null until a run exists. Money stays null until a dated finance reader links Jardal ERP money to a ticket or project. Cost is information only: no threshold, tier or dispatch decision derives from it. Planner and CEO planning runs are not ticket runs and are not recorded yet. (TASK-2026-00212: `objectives/ticket_cost.py` normalizes the heterogeneous engine shapes; generic LLM and exact-skill runs (0 measured tokens, the LLM never runs), simple-tier sessions (code step plus gate/handoff fix turns) and Tier S session deltas report `cost_runs`; legacy nodes report nothing until migrated. Tests: `tests/test_ticket_cost_212.py`.)

### What stays deterministic code

Dispatch, quotas, dependencies, states, schema and deliverable validation, locks, mandate limits, privacy, exact scripts and dev gates. AI decides and does the work; code guards it.

### Memory outside the engine

Skills and playbooks in git; knowledge in the vault; work in Jardal ERP; execution in Mongo. Switching engines is one config line. Hermes is not an engine, CrewAI is not adopted, tmux stays for live sessions only.

---

## Internal Scheduler — Recurring Tasks & Reminders

Scheduling agent execution belongs to OpenCEO, independent of n8n. The reference mechanism is Horizon's persisted triggers and reactive timer in `openceo-core/horizon/triggers.py`. Broader recurring-workflow support must use the same scheduling principles under a single global reactive schedule: one kernel controls all execution wake-ups, while each domain (dispatcher tickets, Horizon, research, CEO loop) keeps its own agenda of future tasks that wakes through it — never an isolated timer of its own (TASK-2026-00440); Jardal ERP retains business workflow scheduling within its own domain.

### Schedule Schema (MongoDB)

Conceptual recurring-workflow envelope, not an additional implemented collection or API:

```json
{
  "schedule_id": "sch_001",
  "cron": "0 8 * * 4",
  "next_run_at": "2026-10-01T08:00:00Z",
  "workflow": "ceo.horizon_review",
  "params": {"horizon_review_kind": "weekly"},
  "scope": "personal",
  "user_id": "owner",
  "project": null,
  "status": "active"
}
```

### Single Type: recurring_workflow

A reminder and a recurring execution use the same mechanism: a persisted due time and a registered workflow. Delivery uses an explicitly configured channel; an illustrative notification tool is not evidence that the channel exists.

### Natural Language Creation

```
"revisa mis objetivos cada jueves a las ocho"
→ resolve owner, timezone, mandate, and registered review workflow
→ persist the next due trigger

"prepara el informe del ticket de Jardal ERP el lunes"
→ resolve business reference and execution authorization
→ schedule a registered reporting workflow with explicit scope
```

If the CEO doesn't find a crystallized workflow, it enters the normal tiers. If a new workflow is discovered and crystallized, the schedule points to it.

### Reactive Timer

1. A CRUD operation or explicit request persists a trigger with `next_run_at` and a deduplication identity, then wakes the timer.
2. The timer reads the next due time through an indexed query and sleeps until then or until another enqueue wakes it.
3. It claims a bounded set of due jobs, executes the registered workflow, records the result, and persists the next occurrence when appropriate.
4. A bounded startup recovery handles interrupted work. No periodic full-collection scan, external API sweep, or second watcher repeats the originating side effect.

The `WaitingTaskScheduler` retry loop was retired by TASK-2026-00113 (the F3 dispatcher in `openceo-core/dispatcher.py` follows this same reactive pattern); it is not the pattern for new Jardal ERP, knowledge, or recurring-work integrations. The dispatcher backup trigger and the Horizon, research and CEO timers converge into that single global kernel; no new isolated timer (TASK-2026-00440).

### Personal vs Enterprise

Scope and ownership must be explicit. `project: null` can mean company-level work and does not imply personal data. Personal triggers retain the owner's privacy rules; business execution carries the applicable Jardal ERP reference and `project_key` mapping. Both use the same scheduling principles without sharing private context.

---

## Tier S (Multi-AI Peer Review)

Work whose result gains materially from orchestrating independent model families goes through Tier S: independent planning, plan challenge, one builder session, pool review with family quorum and arbitration of disputed CRITICAL findings. The criterion is that expected value, never a domain (TASK-2026-00178, policy §19): real money, security boundaries and critical infrastructure are examples, as are silent-failure logic, public contracts, privacy or compliance boundaries, ambiguous specifications, algorithm or strategy correctness, and any ticket whose owner asks for maximum assurance or multi-model orchestration. S is not rationed by percentage; quota admission paces it. The previous flow (persistent tmux reviewers, OpenCode compiler, `consensus.py`, per-project `coding_tool`/`tier_s` keys, `critical_consensus`) was removed with the v2 merge `2be8e4f` (2026-10-05) and lives only in Git history.

- **Engine:** `openceo-core/agents/dev/tier_s/` (v2, single-ticket cycle). Shared executors, review pool/rounds, findings, inbox and knowledge live in `openceo-core/orchestration/` (TASK-2026-00141). Cycle roles and reviewer slots live in the global config (`config_loader.py`); checked-in projects carry no routing or Tier S overrides.
- **Design:** `docs/work/TIER_S_DESIGN_FINAL.md` v2 (TASK-2026-00085): one ticket = one cycle = one builder session; tiers `tier_s/a/b/c`; non-blocking plan review.
- **Routing:** `coding_tool` per ticket (top-level only). DEV tier semantics (C default, `why_not_c` for A/B) and the uniform oracle rule are normative in `openceo-core/objectives/routing_policy.d/dev.md`.
- **Large work:** decomposition belongs to the single objective planner (§Proactive CEO; TASK-2026-00112/00117), not to Tier S.
- **Review quorum (TASK-2026-00162):** reviewer reports are parsed tolerant to prose and strict on findings: a line that opens with a severity must be a full §16.4 header, line anchors keep their literal quote, and every finding is still verified against git. One round machinery (`orchestration/review.py`, `run_quorum_round`) serves ticket rounds and the objective final review: delayed recovery with ranked reserves (below); without recovery configuration, two waves with one retry per slot. Timeout retries get ×1.5 their original limit. Every attempt lands in `task_reviews` with its kind and the finding text. A no-quorum park checkpoints the run, and the next launch resumes it at review, reusing the plan, the responses and the handoff (no new planning or build). Retries back off 15/30/60 min, then park as `needs_owner`. A new run supersedes the task's other open runs, and terminal tasks and boot close the rest. The final review targets the objective's integration ref, never the live checkout.
- **Review pool (TASK-2026-00250):** one logical `gemini` slot (family gemini, account selectable by config among agyd/agyj/agy2 — the account IS the command, resolved and fail-closed by `review.with_resolved_command`, shared by selection and the live executor) plus the dynamic free pair `free-review-1/2` (00428 retired the `mimo`/`nemotron` static slots and the yaml reserves; reviewers run `opencode run -m <id>` with router-fed IDs). Effective models: the agy account default (versions never pinned; verified agy 1.3.5 flags) and the router-fed `-m` IDs (opencode v2.0.22 has no effort flag). Every attempt records slot/account/model/family; availability stays reactive (generic `model not found` disables a slot, quota/error/timeout never fabricate quorum) with no new monitor, polling or balancing. S keeps its top-3, rounds and routing; the A/B/C round prefers these families explicitly (00251). Minimal fenced connectivity probes on 2026-10-19 returned valid reports for agyd, agyj, agy2 and OpenCode DeepSeek; these probes do not establish sustained quota availability.
- **Reviewer recovery (Owner hotfix, 2026-10-19):** S, A/B/C and objective final review share `review.review.recovery`: three attempts per slot, at least 300 seconds between retries and a 43,200-second round deadline. HTTP 429/5xx, transport failures and timeouts retry; malformed reports get one retry, and disabled models, authentication failures and write attempts do not loop. Valid reports remain in the active round; a failed reviewer does not cancel its siblings. After the primary attempts, a failed reviewer restarts alone on the next eligible router target over the same snapshot and prompt (§11.6); the yaml reserve waves were retired in 00428. Daily benchmark research/update remains external, with no new periodic polling. The executor requests JSON for OpenCode/AGY reviewers and rejects error envelopes even on exit 0. AGY accounts get private writable state mountpoints even when history/tmp/projects do not yet exist; host account files remain unchanged. After the deadline, the existing no-quorum wait/checkpoint policy applies. With the cascade on, `cascade.relax_after_seconds` (default 300) bounds that wait: the grace starts at the first independent valid report while the strict quorum is missing; when it ends, an untried muse-review joins (A/B/C rounds plan no extras), and as soon as the relaxed quorum holds the round cancels the reviewers still running and closes relaxed, audited. A strict quorum inside the grace wins and the strict-quorum path is unchanged (Owner order 2026-11-04, TASK-2026-00469).
- **Review cascade (TASK-2026-00310):** `review.review.cascade` (off by default = classic quorum; `enabled` rolls back by one flip) orders selection free → own → metered with a deterministic spend guard, then a final relaxed quorum where `extra`/builder-family reports count, else blocks with tier/denial reasons on the round note. Tiers: free (dynamic OpenCode-native pair), own flat-rate (Gemini/AGY plan, never a free tier; `muse-review` extra), metered (`dsh` on an unmetered estimate, paid OpenRouter last). The paid slot (`openrouter-paid`, `cascade_only`, hardened credential handoff, model `openrouter/openai/gpt-oss-20b`) fires only inside the auto-ranked paid top-5 (`paid.json`, cheapest-first with a quality sink, refreshed with each catalog update; static top-3 fallback; critical tickets arm a premium proven-quality list instead, cap `1.00`, degrading to cheap without benchmark data — paid stays the last resort either way) and a hard per-review cap (`0.50`) at or under the mandate `gasto_nuevo_sin_preguntar` bar (`10.00`, the prepaid credit size — never new money), with worst-case spend accrued per attempt; anything above the bar escalates instead of spending. Every outcome carries its tier, metered attempts their guard receipt, and relaxed-counted reports are flagged; degradation is automatic and every step lands on the round note.
- **Simple review round (TASK-2026-00251):** a reviewed A/B/C ticket runs build → gates → one quorum round over the common pool (three reviewers, dynamic free pair plus Gemini preferred, no extras, builder family excluded, quorum of two valid reports from independent families) → builder fix verdicts (`F<n>: CORREGIDO|DESCARTADO`, two turns, then the owner) → final gates → single squash. Attempts run on detached read-only worktrees at a snapshot commit and persist under scope `simple`; the `params.review_round` checkpoint (phase, snapshot shas, reports, findings, resolutions) reuses valid reports on same-snapshot resume and never counts another snapshot's. Review=off keeps tier/builder/gates with zero reviewer invocations, preflight, quota or auth touched. S is untouched (plan review, two rounds, arbitration); the simple round is not S-equivalent. The `MEJORA_TIER_S` proposal (private attachment on TASK-2026-00248) stays context only: this epic adopts default-on-with-off plus the pool, not mandatory A/B/C review, extra S rounds, or arbitration redesign.
- **Free-model router (TASK-2026-00283, addendum v1.2 in `docs/FREE_MODEL_ROUTING_SPEC_ADDENDUM_v1.2.md`):** `openceo-core/model_routing/` is the only authority for free-model catalog, per-profile ranking, availability and reservations. Selection is `native_first`: group A (OpenCode Free, native only — an external client gets HTTP 403) first, then group B (zero-cost externals configured in OpenCode); crossing groups switches the OpenCode session model, and there is no single HTTP alias over both. The A/B/C/S review pools take the top two eligible `free-review` models of distinct verified families for their OpenCode slots (active by Owner decision; without a valid snapshot the pair comes from a degraded live-catalog order, else the round blocks loud — 00428 retired the yaml fallback, and `review.free_router.enabled: False` runs the non-free waves only), each with an explicit `--model` and whole-reviewer restart onto the next eligible model, crossing to group B when A is spent; research runs managed `free-research` sessions with `switchModel` and checkpoints (`RESEARCH_FREE_ROUTER_ENABLED=false` reverts to the static model); interactive use is the `opencode-free-research` launcher (`free-router tui-launch`/`tui-check`, next-turn fallback only — a hand-opened TUI is not covered). Rounds, quorum, arbitration and the Gemini/AGY slot are unchanged. Runtime paths are `~/.config/free-router/` (policy, providers, catalog, rankings, history) and `~/.local/state/free-router/` (availability store, error registry, alerts, checkpoints, logs); the `free-router` CLI owns status, refresh, reservations and audits, and `model_routing.acceptance` owns the T01–T20 plus addendum §11 acceptance battery (`docs/model_routing/ACCEPTANCE.md`, runbook `docs/model_routing/RUNBOOK.md`). The gateway stays in security HOLD and is externals-only: nothing installs or runs LiteLLM, and no native `opencode` deployment ever enters `gateway.generated.yaml`.

Superseded by TASK-2026-00138: the numeric tiers (`tier1`→B, `tier2`→A, `tier3`/`tier4`→C), the LLM tier selector, the MCP `coding_tier` parameter, the `dev.coding_tier` default and every deprecated routing source were retired once the migration ran dry. `coding_tool` (tier_s/a/b/c) is the only routing; the previous sentence about `coding_tier: tier3` mapping to a one-shot OpenCode run no longer applies and lives only in Git history.

Retired by TASK-2026-00209: the `pm.default_tool` setting (extraction engine `langraph` | `claude` | `codex`, overridable per-request via `params.pm_tool`) is gone with the PM agent. It lives only in Git history.

---

## Rate Limit Management

Claude Code and Codex CLI run on subscription-based rate limits with two tiers: a **5-hour rolling window** and a **weekly cap**. When multiple tickets queue up (30+ from a single sprint), OpenCEO must maximize usage without burning everything at once and without blocking the system when a limit hits mid-task.

Quotas and execution admission remain OpenCEO responsibilities, including for work linked to Jardal ERP. The following describes the existing provider/runtime mechanisms, including legacy retry polling. It does not authorize periodic database or API scans in new integrations: new queue wakeups must originate from execution events, quota-reset deadlines, and persisted triggers. Do not introduce artificial throttling to compensate for duplicate work.

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
   (Tier S consumes into the reserve down to zero; Tier A/B wait as
   quota_reserved instead — TASK-2026-00113)
3. If both pass → run immediately
4. If either fails → status="queued" + typed waiting_reason; quota waits
   carry resume_at=earliest_reset_time, limit waits are event-held
5. The F3 dispatcher retries on events (created/closed/failed/cancelled/
   dependency-resolved) plus one conditional backup trigger — no polling
6. After every ticket completion → refresh limits → wake the dispatcher
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
- `openceo-core/task_service.py` — Queue events (create/finish/requeue/webhook) wake the dispatcher
- `openceo-core/dispatcher.py` — Event-driven dispatch + conditional backup trigger (F3)
- `openceo-core/agents/dev/tmux_runtime.py` — Mid-task detection + wait-and-resume in polling loop
- `openceo-core/orchestration/live.py` — Pre-launch check for Tier S (v2)

---

## MCP Server — Internal and External

OpenCEO has two MCP surfaces:

| Type | Scope | Consumer | Example |
|---|---|---|---|
| **MCP Internal** | Local host | Managed coding agents and manual CLI sessions | Curated DNS and OpenCEO ticket capabilities |
| **MCP External** | Outside OpenCEO | External clients (ChatGPT, other LLMs) | ChatGPT selects a registered workflow and calls `execute_workflow` |

MCP exposes a curated capability surface backed by shared services and the workflow registry, not an automatic export of every primitive. Internal registry names follow `agent.tool_name`; the external connector preserves `get_company_context`, `get_user_context`, `execute_workflow`, and `get_task_status`. Vault access and qmd remain direct filesystem/command access outside MCP.

### Internal: Coding Agent Infrastructure Tools

One stateless HTTP mount at `/internal/mcp/` serves both managed runtimes and manual host CLIs (TASK-2026-00231). The redundant global mount has been removed. Loopback IP restrictions remain; without a badge the server advertises the 17-tool `ANONYMOUS_BOX`, while an explicit valid badge selects its own box and an invalid badge answers 401.

### Design Principles

- **Runs inside openceo-core process.** Same Python process, same `openceo-core/.env`. No extra container, no extra port.
- **Owner-curated tool list.** Only selected infrastructure and OpenCEO capabilities appear in `mcp_server.py`. A backing Jardal ERP API, workflow primitive, or database collection is not automatically a public MCP tool.
- **Shared Python functions.** The MCP tools call the same functions in `openceo-core/tools/` that LangGraph agents can also import. One implementation, two access paths.

### Exposed Tools

The internal mount registers 26 boundary tools (`catalog.MCP_BOUNDARY_TOOLS`; research submit/get in TASK-2026-00222), of which 17 are available without a token. Each description documents the criterion that justifies it: (a) secret, (b) lock, (c) business record.

| Tool | Purpose |
|---|---|
| `manage_dns_record(hostname, action)` | Cloudflare DNS: create (idempotent) or delete an A record. Required before Traefik deploy. |
| `openceo_create_ticket(title, ...)` | Register a ticket through `TaskService`, with source identity and runtime policy. |
| `openceo_create_tickets_batch(tickets)` | Register a structured JSON batch through the same service. |
| `openceo_create_pm_tickets_batch(project, source_id, tickets, ...)` | Register a PM batch with explicit project and source context. Owner badge only (the 00199 service exception retired with the PM agent in TASK-2026-00209). |
| `openceo_search_tickets(query, project, status, limit)` | Read business tickets in Jardal ERP before creating new ones. |
| `openceo_get_ticket(identifier, limit)` | Read one ticket in full: fields, execution status, comments and attachments. |
| `openceo_create_objective(project, goal, ...)` | Create a company objective whose children the planner creates. |
| `openceo_transition_ticket(ticket, to_status, ...)` | Move a ticket's business status under the global policy. |
| `openceo_transition_tickets_batch(transitions)` | Apply several status transitions, each verified by reread. |
| `openceo_update_ticket(ticket, subject, description, priority, due_date)` | Edit a ticket's content fields. Never status, dependencies or execution. |
| `openceo_add_ticket_comment(ticket, content)` | Comment on a ticket; idempotent by text and author. |
| `openceo_update_ticket_comment(comment_id, content)` | Edit any comment, whoever wrote it. |
| `openceo_delete_ticket_comment(comment_id, ticket)` | Delete a comment. |
| `openceo_attach_ticket_file(ticket, source_path \| content_base64, file_name, private)` | Attach a file; private by default, deduplicated by content. |
| `openceo_get_ticket_attachment(attachment, include_content)` | Read one attachment: metadata, download URL, optional small content. |
| `openceo_update_ticket_attachment(attachment, ticket, file_name, private)` | Rename an attachment or change its privacy. |
| `openceo_delete_ticket_attachment(attachment, ticket)` | Delete an attachment. |
| `openceo_send_email(to, subject, body_text, ...)` | Send an operational email from a configured sender (listed in its description). |
| `openceo_ensure_business_project(project_key, ...)` | Ensure the Jardal ERP Project identity, idempotent by key. |
| `openceo_register_expense(text, openceo_ref, ...)` | Register an occasional expense in Jardal ERP from free text. `openceo_ref` is required so retries never double-post. |
| `openceo_register_income(text, openceo_ref, ...)` | Register collected income in Jardal ERP from free text. `openceo_ref` is required so retries never double-post. |
| `openceo_scope_summary(...)` | Balance/expense summary by scope from the ledger (read-only). |
| `openceo_contribution_10_status(...)` | 10% contribution rule status from the ledger (read-only). |
| `openceo_elfec_house_payment(...)` | Elfec house payment in practice mode: never pays. |

These names reflect `openceo-core/mcp_server.py`. Their existing Mongo/Notion behavior is a compatibility implementation, not an Jardal ERP adapter. Legacy `notion_create_*` references do not authorize direct writes around OpenCEO. The direction's `ticket.create()`/`ticket.transition()` vocabulary describes semantics; it does not rename deployed tools by documentation alone.

#### Ticket thread: edits, comments and attachments (TASK-2026-00079)

Verified against the live site (Frappe 16.35.0 / Jardal ERP 16.36.0, compañía Jardal, 2026-10-01) with the API principal `openceo@jardal.app`, whose roles decide what is possible here (`Projects User` + `Accounts User`, plus `OpenCEO API` added the same day — see below):

- **One read gives the whole ticket.** `openceo_get_ticket` returns fields + execution status + comments + attachments in a single call. A separate `openceo_get_ticket_thread` existed for one day and was folded in (owner decision, 2026-10-01): two tools to understand one ticket cost the agent an extra step on every turn.
- **Writing the thread uses the desk's own methods, and one deliberate exception.** Creating a comment (`frappe.desk.form.utils.add_comment`), reading the timeline (`get_docinfo`), uploading (`upload_file`) and deleting an attachment (`remove_attach`) all check permission against the **ticket**, which is why `File` enters the client's DocType allowlist only for the rename/privacy update. Editing a comment tries the official `update_comment` first — which Frappe restricts to the author — and falls back to `PUT /api/resource/Comment`; deleting a comment has no public Frappe method at all and goes through `DELETE /api/resource/Comment`.
- **That fallback needs a role, and the owner granted it (2026-10-01).** `Comment` has no `All` role permission: stock Frappe gives it only to `System Manager` / `Website Manager`, so without a grant an agent could neither edit someone else's comment nor delete any. A dedicated role **`OpenCEO API`** was created in Jardal ERP with `read`/`write`/`create`/`delete` on `Comment` (permlevel 0, `desk_access=0`, `export=0`) and assigned to `openceo@jardal.app` alongside `Projects User` + `Accounts User`. Because that role opens `Comment` **site-wide**, the adapter re-imposes the boundary Frappe's method used to provide: before writing or deleting, it reads the comment and requires `comment_type == "Comment"`, `reference_doctype == "Task"`, a readable ticket, and — for deletes — that the comment belongs to the ticket the caller named.
- **Attachments are private by default**, capped at `JARDAL_ERP_ATTACHMENT_MAX_BYTES` (10 MiB), read from `JARDAL_ERP_ATTACHMENT_ALLOWED_ROOTS`, with the name sanitized to a single path-free segment and executable/scripted extensions (`.html`, `.svg`, `.sh`, `.exe`, `.js`, …) rejected — a scripted attachment served from Jardal ERP's own origin is stored XSS once it is public. No URL ingestion: `file_url` is only ever a site-relative path, so there is no SSRF surface.
- **Idempotency, because `upload_file` always creates a new `File` document** (its `content_hash` reuses the file on disk, not the record): attaching is keyed on `md5(content)` + ticket, and a retry returns the existing attachment. Comments are keyed on author + normalized text. The creating POSTs are sent with a **single attempt**, so the client never duplicates on its own retry.
- **Ticket editing excludes status.** State transitions belong to the execution (or the desk) and are tracked by `status_pushed`; writing them here would bypass that bookkeeping and could bounce against the `on_update` webhook. A comment or attachment never changes status, priority, dependencies or execution.
- **`description` accepts plain text or the HTML the field already holds.** Plain text is escaped and newlines become `<br>`; text that already contains a tag is passed through, because escaping it would show the existing markup as literal text. Frappe sanitizes the HTML on save (verified live: `<script>` and `javascript:` hrefs are stripped, formatting is kept), so this is not an XSS opening.
- **Responses stay short**: the ticket view is six fields, and attachment listings omit fields the source does not return rather than asserting `null`.

**Design rule for MCP tool descriptions:** keep them as short as possible. Every token in a tool description is sent on every request to the coding CLI. Verbose descriptions waste subscription limits. The function parameter schema already documents the structure — no need for a separate discovery tool. A test enforces this: each new ticket-thread description stays under 220 characters and 32 words.

### What is NOT exposed

The curated email and finance tools require an explicit badge and invoke their owning services. No unrestricted Jardal ERP DocType access, Mongo write interface, Obsidian MCP, or Graphify MCP is exposed. A new capability must be explicitly registered and preserve its approval and idempotency policies.

### How coding agents connect

`scripts/sync-mcp-config.py` synchronizes installed user profiles on backend startup, preserving unrelated providers and MCP servers. Every manual profile uses `openceo-internal` at `http://127.0.0.1:{port}/internal/mcp/` with no default MCP token or authorization headers. Active project overrides in Claude are removed so its user configuration also works in isolated worktrees.

| CLI profiles | User configuration | HTTP URL field |
|---|---|---|
| `clauded` / `claude` | `~/.claude.json` | `url` |
| `claudej` | `~/.claude-j/.claude.json` | `url` |
| `claudep` / `claudeg` | `~/.claude-deepseek/.claude.json` | `url` |
| `musej` / `muse` | `~/.config/muse/settings.json` | `url` |
| `codexf` / `codex` | `~/.codex/config.toml` | `url` |
| `codexj` | `~/.codex-j/config.toml` | `url` |
| `opencode` | `~/.config/opencode/opencode.json` | `url` (`oauth = false`) |
| `qwen` | `~/.qwen/settings.json` | `httpUrl` |
| `agyd` / `agy` | `~/.gemini/config/mcp_config.json` | `serverUrl` |
| Gemini CLI, when configured | `~/.gemini/settings.json` | `httpUrl` |

Managed runs continue to receive explicit per-run service or agent badges through their role profile or provider configuration. `build_mcp_provider_env()` carries location only; `build_mcp_provider_config()` is anonymous unless an explicit badge is supplied. The retired PM Codex path used to override `mcp_servers.openceo-internal.bearer_token_env_var` for its own service badge; it is gone with the PM agent (TASK-2026-00209).

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

This routing sketch uses the existing local contact lookup. Under the target boundary, Jardal ERP owns business users and contacts; any local lookup is an explicit mapping/projection for channel routing, not a second CRM. Local OpenCEO authentication remains in force.

---

## Per-Project Configuration

Each repository-backed execution project has YAML configuration for providers, routing, technical workflows, and post-actions. OpenCEO reads it without project-specific code in core. Jardal ERP owns the business project and workflow; the vault owns its engineering knowledge.

The configuration below documents existing execution concepts. `project_key` is mapped to the existing project name at the integration boundary; adding an Jardal ERP source or new mapping fields requires an implemented and validated adapter, not just another YAML value. The runtime's `TaskSource` enum does not yet include `erpnext`.

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
    branch_pattern: string
    auto_push: boolean
    create_pr: boolean
    qa:
      playwright: required | optional | skip
      confidence_threshold: float
      max_retries: integer

  # Retired (TASK-2026-00204), rejected by validation when present:
  # the top-level `routing:` block (llm_fallback / default_agent /
  # triggers / patterns), `dev.coding_tool`, `dev.tier_s` and
  # `dev.tiers`. Routing is per-task and the planner decides; Tier S
  # roles and the review pool are global (config_loader defaults).

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

Adding an execution project means configuring `projects/{name}/project.yaml` and linking the same `project_key` to its Jardal ERP business identity, repository, and vault hub where applicable. Existing local projects remain usable before that mapping is integrated. No per-project memory file is created as a second engineering knowledge base.

Adding an execution process means defining a technical workflow for a registered worker. Business workflow rules and approvals belong in Jardal ERP; project YAML selects and configures how OpenCEO executes authorized work.

---

## Task Lifecycle Flow

### Target Business-to-Execution Contract

1. A request identifies existing Jardal ERP work, or asks an OpenCEO capability to create it. Resolve `project_key`, business reference, actor, priority, and required approvals explicitly.
2. The adapter validates the request and retains a stable operation identity. A business ticket reference, an incoming event/operation ID, and an OpenCEO `task_id` serve different purposes; retries must not create new work.
3. OpenCEO associates a runtime task with the business reference and routes execution through the existing graph, workflow executor, or explicitly activated dev session. Business approval and technical admission both apply where required.
4. Agents obtain relevant vault notes and code context directly. A run stores its state and evidence in MongoDB; it does not copy the vault or the business database.
5. Completion emits evidence and an idempotent adapter update for the associated Jardal ERP work. Business acceptance remains subject to its workflow; partial failures retain traceable retry state instead of reporting a successful business transition.
6. Knowledge discovered during execution is written directly to linked, checked vault notes. This does not itself create a ticket, advance a business status, or launch another run.

The Jardal ERP adapter and event mapping are integration contracts to implement, not additional endpoints or source values already accepted by the runtime.

### Existing Entry Points and Compatibility

- API/n8n-style tasks enter through `POST /api/tasks`.
- OpenWebUI-compatible chat and LiveKit voice enter through `POST /v1/chat/completions`.
- `router.py` selects an agent from config-driven rules plus optional LLM fallback; `graph.py` executes `dev` or a catalog agent on the generic node, and escalates unsupported workers.
- Dev peer sessions can run long turns asynchronously in the background, while finance and secretary run as catalog agents (finance voice names the exact skill through the first layer).
- On startup, Mongo-backed tasks are reconciled and tmux-backed dev sessions are reattached when possible.
- Tasks with `source` + `source_id` are idempotent: a second POST with the same pair returns the existing task. Enforced by a partial unique index in MongoDB.
- **Notion mirror on creation:** `TaskService.create_task()` schedules the minimum human-facing mirror from the originating CRUD operation; run/patch paths drive relevant updates. This is a compatibility projection of execution work. Private Horizon tasks are excluded.
- **Notion sync-back:** when a Notion-sourced task changes status (`in_progress`, `completed`, `failed`, etc.), `task_service.py` writes back to the originating Notion page via `tools/notion.py`. Properties updated: `Runtime Status`, `OpenCEO Ref`, `Runtime Summary`, `Last Sync At`, `Agent`. Sync-back is fire-and-forget and never blocks task execution. Controlled by `NOTION_SYNC_BACK_ENABLED`.
- Mirror reconciliation runs once at startup as a bounded safety net for orphaned tasks. No Notion change stream, periodic mirror scan, or second watcher reprocesses CRUD events.
- MongoDB is the canonical **execution** queue; Jardal ERP is the target business system of record. Notion mirrors only the fields needed for compatibility and human visibility.
- Legacy PM extraction paths that write directly to Notion are compatibility exceptions, not the pattern for new integrations. The internal `openceo_create_*` paths enter through OpenCEO task registration (the external `pm.create_tickets` workflow retired with the PM agent in TASK-2026-00209). Preserve consumers during adapter changes and verify each path rather than assuming that documentation migrated it.

---

## Dev Agent — Step-by-Step Session Flow

Persistent tmux runtime with explicit provider sessions. Deterministic Python orchestrates the flow around the coding CLI.

1. **Branch Setup** — checkout, pull, create feature branch.
2. **Code** — send task description. Coding tool uses CLAUDE.md/skills + MCP tools (DNS, Docker) if needed. If `coding_tool` is `tier_s`, execute the Tier S flow instead of steps 2-4.
3. **QA** — conditional on config. Run tests, retry on fail, escalate if exhausted.
4. **Push + PR** — conditional on config and confidence threshold.
5. **Record execution result and knowledge** — preserve existing context-file closeout where required by the runtime; write durable engineering conclusions to the vault through its skill. If the user then closes the dev session, OpenCEO tears down the tmux runtime; in Tier S it also purges persisted OpenCode `sessionID` files so the next session starts clean.
6. **Post-actions + notifications** — handled by OpenCEO core.

---

## MongoDB Collections

- **tasks** — Canonical OpenCEO execution queue and runtime task tracking. Business identity/status belongs in Jardal ERP; execution hierarchy and references must use explicit contracts rather than copying the business backlog
- **dev_sessions** — tmux-backed dev peer/autonomous sessions
- **user_context** — selected project + active dev-session focus per user/chat
- **agent_status** — provider quota and health data
- **browser_tasks** — reserved for browser/worker style queues
- **workflow_runs** — Runtime execution state for crystallized workflows, including `blocked_human` for a paused approval step. Persisted status values follow `WorkflowRunStatus` in `state.py`
- **discovery_runs** — Retired with TASK-2026-00215 (orphan rows unmigrated; no code reads them). Was: proposals, approval/rejection, and discovery execution state under `DiscoveryRunStatus`
- **task_reviews** — Append-only technical review history linked by `task_id`; does not replace Jardal ERP business approval history
- **projects**, **project_presets** — Local execution configuration/overrides and presets; business project identity is mapped from Jardal ERP
- **users**, **groups** — Local authentication and OpenCEO permissions; business user ownership remains Jardal ERP's, with explicit identity mapping
- **horizon_profile**, **horizon_objectives**, **horizon_captures**, **horizon_actions**, **horizon_events** — Private, owner-scoped CEO context and action/result links, not business or engineering memory replicas
- **horizon_plans**, **horizon_triggers** — Personal capacity plans and persisted reactive triggers; reuse this planning mechanism instead of adding parallel `objectives`/`schedules` engines
- **workflow_state** (design envelope below) — Optional supplementary execution state when `params` is insufficient, not a claim that a generic collection/service is already wired. No business masters, durable engineering notes, accounting ledger, or code graph belongs here

### Data Modeling Strategy

Choose the owning system before choosing a storage format: business records go to Jardal ERP, durable engineering knowledge to Obsidian, implementation/configuration to Git, and execution state to OpenCEO. The following three patterns apply **inside the execution domain**, not to every piece of company data.

#### Three Storage Layers

**Layer 1 — Structured collections** (`tasks`, `workflow_runs`, `user_context`, `agent_status`, `task_reviews`, the owner-scoped `horizon_*` collections, etc.). Schemas are enforced by their models and services. They support indexed queries, execution dashboards, cross-agent filtering, and lifecycle gating. References to external work do not make these collections owners of that work.

**Layer 2 — Supplementary `workflow_state` design.** A fixed envelope with workflow-local `data` for bounded intermediate outputs, attempt logs, and rebuildable snapshots when `params` is insufficient. The envelope supports basic lookups (`owner_id`, `workflow`, `kind`, `key`, `tags`). It is not permission for workflows to persist arbitrary business entities or build a memory database.

**Layer 3 — Execution artifacts** (`CURRENT_PLAN.md`, `BRIEF.md`, and feedback). Narrative material for the current task/review. Durable architecture and decisions belong in linked vault notes; implementation contracts remain versioned in Git. Neither type of Markdown replaces structured execution state.

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

`scope=run` means the data belongs to one execution and can be cleaned up afterward. `scope=global` means cross-run execution bookkeeping, such as a processed external-event marker, not global business ownership. This storage scope is separate from personal/business privacy scope. `expires_at` enables TTL cleanup; `kind` distinguishes runtime payloads and `key` provides lookup/deduplication identity within a workflow and kind. Business references and dated snapshots must remain traceable to Jardal ERP.

Recommended indexes: `{workflow: 1, kind: 1, key: 1}`, `{owner_type: 1, owner_id: 1}`, `{expires_at: 1}` (TTL index).

#### Decision Framework — Where Does New Data Go?

When implementing a new workflow or tool that needs to persist data, the developer (human or AI agent) must answer these questions in order:

**Ownership first:** Is this business data, durable engineering knowledge, or code structure? Use Jardal ERP, the vault, or on-demand repository inspection respectively. Only execution data proceeds through the questions below. A frequent query or dashboard requirement does not transfer domain ownership to MongoDB.

**Question 1: Does the data fit in `params` inside an existing `tasks` or `workflow_runs` document?** If the data is small, workflow-local metadata that does not need independent queries — yes, use `params`. This is already the documented pattern: `params` is for workflow-local request metadata and small per-task knobs that do not need global semantics. Examples: country filter for a search, retry interval, preferred contact channel, template name.

**Question 2: Is this a runtime entity with its own lifecycle that multiple workflows or agents query independently?** First reuse an existing structured collection and service. Add a new one only when execution semantics and indexed access justify it; supplementary state can use the `workflow_state` pattern. Business entities still belong in Jardal ERP even when multiple agents use them.

**Question 3: Does the data need complex indexed queries across all instances (e.g., "all entities with status X and last_updated older than Y")?** If yes — it should be in a structured collection, not `workflow_state`. The generic collection works for basic lookups (`workflow` + `kind` + `key`) but is not optimized for arbitrary cross-field queries on `data.*`. When you find yourself needing `db.workflow_state.find({"data.status": "awaiting", "data.last_updated": {$lt: ...}})` across thousands of documents regularly, that data has graduated to its own collection.

**Question 4: None of the above — the data is intermediate, variable, or workflow-specific?** Use the supplementary-state pattern with bounded size and retention: input snapshots, attempt logs, and intermediate computation results. Retain source references. A settled debugging discovery belongs in the vault; a customer or financial record belongs in Jardal ERP.

#### When `workflow_state` Graduates to a Structured Collection

Supplementary runtime data may grow into its own execution collection when all these criteria apply:

1. Multiple workflows or agents query this data independently (not just the workflow that created it).
2. You need indexed queries on fields inside `data` that run frequently (dashboards, scheduled jobs, deduplication).
3. The entity has a lifecycle independent of any single workflow run (it persists, mutates, and is referenced long after the originating task completes).
4. The volume justifies dedicated indexes (hundreds or thousands of documents with regular filter/sort queries).

Example: worker execution-attempt metadata may eventually need shared indexed queries and a dedicated collection. A customer list, business ticket store, ledger, or engineering knowledge graph must not follow this path into MongoDB; volume does not change its owning system.

#### Anti-Patterns

- **One collection per business case.** Business data belongs in Jardal ERP. A new workflow reuses execution collections or supplementary state rather than creating its own business database.
- **Querying `data.*` fields heavily in `workflow_state`.** If you're building indexes on `data.status` or `data.last_contact` inside `workflow_state`, the data should be in its own collection.
- **Storing canonical runtime state in `workflow_state`.** Task status, workflow execution state, approval gates — these belong in `tasks` and `workflow_runs`. The generic collection is for supplementary data.
- **Creating `memory`, code-graph, or document-replica collections.** The vault and repository retain those responsibilities. Supplementary runtime storage must not grow into another knowledge system.
- **Skipping `params` and going straight to `workflow_state`.** If the data is 3-5 small fields that only matter to this specific task execution, it belongs in `params` on the task or workflow_run document.

#### Markdown vs MongoDB

Vault Markdown preserves the reasoning behind decisions and is discoverable through qmd. Repository Markdown defines versioned contracts or local execution artifacts. MongoDB supports atomic execution updates and indexed runtime queries; Jardal ERP supports business records and their lifecycle. Choose by responsibility, not merely by whether a query is needed.

Notion may mirror execution summaries for compatibility. Jardal ERP is the business authority, not another mirror; neither replaces MongoDB's responsibility for resumable execution state.

---

## n8n Integration

Used for **external webhook triggers** and **complex multi-step flows** — not for simple API calls.

**Inbound contract:** external systems can normalize into `POST /api/tasks`.
**Outbound contract:** complex multi-step flows with retry, delays, or conditional branching.

Jardal ERP events must map to explicit, idempotent OpenCEO operations. Do not introduce periodic API synchronization, duplicate CRUD side effects, or a parallel business workflow engine. Routine adapter reads/writes use scoped Python tools directly.

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

This applies to OpenCEO's execution code. Jardal ERP and the engineering vault are independent systems with their own responsibilities, not modules to reproduce inside the monolith. Adapters stay in the owning agent, or in shared tools when there are multiple consumers.

### Agent Folder Structure

Illustrative organization for growing agent packages; the concrete repository layout follows in the next section. Finance files contain adapter/policy functions, not a second accounting implementation.

```
openceo-core/agents/
  finance/
    __init__.py
    tools/
      queries.py          # scoped Jardal ERP balance, budget, and obligation reads
      records.py          # permitted document registration through Jardal ERP
      payments.py         # prepare/confirm operations under the Finance contract
      reporting.py        # dated summaries with source references
    jardal_erp_client.py   # scoped deterministic HTTP adapter (integration design)
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
    "core.echo": echo_fn,

    # External — scoped adapter calling Jardal ERP (integration design)
    "finance.get_scope_summary": get_scope_summary_fn,
}
```

The executor, discovery, and claim do not know or care if the tool is local or calls an external service. The abstraction is at function level, not service level.

---

## Project Structure

```
openceo/
├── README.md
├── CLAUDE.md                         # Developer/runtime contract
├── STATUS.md
├── deploy/
│   └── systemd/
├── docs/
│   ├── OPENCEO_DIRECTION.md          # Domain ownership and integration direction
│   ├── finance-jardal-erp-integration-contract.md
│   └── SETUP.md
├── openceo-core/                     # Backend: FastAPI + OpenWebUI-compatible API + MCP
│   ├── .env.example
│   ├── .env.finance.local.example
│   ├── Dockerfile
│   ├── main.py                       # FastAPI + MCP server in same process
│   ├── first_layer.py                # Contextual chat/voice gateway
│   ├── dev_session_service.py        # Persistent tmux sessions for dev peer programming
│   ├── graph.py                      # Explicitly wired worker graph
│   ├── state.py                      # Pydantic models and enums
│   ├── router.py                     # CEO routing logic
│   ├── config_loader.py              # Reads/validates project.yaml
│   ├── project_service.py
│   ├── task_service.py
│   ├── workflow_service.py           # Deterministic execution + discovery proposals
│   ├── horizon/                      # Private CEO context, planning, and triggers
│   ├── dispatcher.py                 # Event-driven dispatch + backup trigger (F3)
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
│   ├── agents/                       # Each agent = self-contained agent package
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
│   │   │   ├── tier_s/                   # Tier S v2 single-ticket cycle
│   │   │   ├── tmux_runtime.py
│   │   │   └── opencode_run.py
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
├── openceo-react/                    # Execution dashboard + private personal view
├── openceo-voice/
├── openceo-voice-android/
├── openceo-worker/
├── projects/                         # Versioned execution project configuration
├── workflows/                        # Crystallized technical recipes in YAML
└── scripts/
    ├── install-agent-requirements.sh
    ├── openceo-core-host.sh
    ├── openceo-core-host-runner.sh
    ├── setup.sh
    ├── backup.sh
    └── health_check.sh
```

Outside this repository: Jardal ERP owns business persistence and its deployment; `jardal-knowledge/vault/` owns engineering memory and its qmd index. Neither is embedded into MongoDB or copied into the project tree.

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
CEO_LLM_MODEL=deepseek-flash
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
- Free-router secrets belong in openceo-core/.env.free-router.local (0600, `FREE_ROUTER_*` names only, template `openceo-core/.env.free-router.local.example`); never in openceo-core/.env, where `GROQ_API_KEY` is the voice key.
- Each `openceo-core/agents/{name}/.env.example` file documents the skill-local credential contract for packaging and marketplace export.
- Jardal ERP adapter credentials and settings follow the Finance/integration contract when implemented; documentation alone does not enable an adapter or add accepted runtime fields. Keep them scoped to permitted operations, outside Git and the vault.
- Vault paths, qmd setup, and the link checker are documented in the [engineering memory setup](../../jardal-knowledge/README.md), not added as a new MCP service.

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

These commands deploy OpenCEO, not Jardal ERP or the vault. Jardal ERP retains an independent deployment lifecycle. Host coding agents use direct filesystem access to the vault; containerized agents need explicitly configured access to that same vault and its approved tooling, not an internal copy of the knowledge base.

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
6. Keep business operations behind OpenCEO capabilities backed by Jardal ERP, engineering knowledge in the vault, and code relationships in repository search/LSP. A new agent does not gain its own business or memory database.

## Adding a New Project

1. For business work, identify the Jardal ERP project and stable `project_key`; preserve the explicit mapping to the local execution name.
2. Create `projects/{name}/project.yaml` with repository/provider/routing configuration. Existing local-only execution remains compatible until an Jardal ERP adapter is integrated.
3. Link the project to its vault hub using `llm-vault-links`; reuse existing knowledge and validate links. New projects get no `CONTEXT.md`: status goes to `STATUS.md` and work to Jardal ERP tickets (preset `sync_context`).

## Adding a New Business Process

1. Define the business process, priority, and human approvals in Jardal ERP.
2. Define its technical execution recipe in the YAML/config consumed by the registered OpenCEO worker; use existing routing and discovery contracts.
3. Connect business references and transitions through a scoped, idempotent OpenCEO adapter. Do not expose arbitrary Jardal ERP APIs or duplicate its workflow state.

## Adding a New Tool

1. Evaluate: local (one agent), shared (multiple agents), n8n (webhooks/complex), or Worker PC (no API).
2. Shared → create in `openceo-core/tools/`, import in agents.
3. Local → add to agent's `tools.py`.
4. n8n → create workflow, call via `n8n_client.py`.
5. MCP-exposed (for coding agents) → explicitly register the curated capability in `mcp_server.py`, backed by the owning service. Keep schemas, policy, and idempotency intact.
6. Knowledge access → direct files and qmd under the vault skill. Code relationships → repository search/LSP. Neither needs a new OpenCEO proxy tool.

---

## VM Restart Recovery

1. MongoDB persists canonical OpenCEO execution state. Jardal ERP persists business records independently; the vault and Git retain knowledge and implementation. Notion compatibility views remain projections.
2. Interrupted tasks are marked accordingly on startup reconcile.
3. tmux-backed dev sessions can be reattached by `dev_session_service.startup_reconcile()`.
4. WebSocket clients reconnect normally.
5. Adapters recover pending operations by stable reference without replaying successful business side effects. Recovery uses bounded startup reconciliation and explicit events, not periodic full-dataset scans.

---

## Development Rules

- **README.md is an architecture document.** It defines flows, functionalities, and design decisions. NEVER add operational tracking here. That information belongs in `STATUS.md`.
- Follow [OPENCEO_DIRECTION.md](OPENCEO_DIRECTION.md) for domain ownership. Preserve execution contracts when introducing Jardal ERP adapters or replacing compatibility context files.
- Record durable engineering conclusions in the vault using `llm-vault-links`, with valid links and a passing checker. Keep code topology in the repository and business records in Jardal ERP.
- Keep `README.md`, `STATUS.md`, and the affected skill manifests in sync whenever architecture, install contracts, or runtime behavior changes.
- Do not put planning or tracking notes in `README.md`.
- If you change the dev runtime flow, re-run the smoke checklist from `CLAUDE.md` §9 before closing the task.

---

## License

MIT
