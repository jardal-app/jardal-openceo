TASK: Implement ChatGPT MCP Orchestrator Tools for OpenCEO

CONTEXT:
Read README.md and STATUS.md completely before writing any code.
There is already a working MCP server implemented in this repo,
connected to ChatGPT via Apps SDK. Find it, read it completely,
and understand exactly how it is structured, how tools are registered,
and how it connects to the rest of openceo-core. Do not assume paths,
mount points, or patterns — derive everything from the actual code.
Then read openceo-core/router.py, openceo-core/agents/pm/session.py,
openceo-core/agents/secretary/session.py,
openceo-core/agents/finance/session.py,
openceo-core/dev_session_service.py.
Do not write a single line of code before reading all of these.

---

ARCHITECTURAL PRINCIPLE — TWO SEPARATE PATHS:

Path A: ChatGPT + MCP
  - ChatGPT is the intelligence layer: routing, parameter extraction,
    content processing, approval loops
  - OpenCEO is the execution layer: deterministic, no LLM involved
  - ChatGPT calls OpenCEO only when it has everything ready to execute

Path B: OpenWebUI + DeepSeek + LiveKit
  - DeepSeek handles routing fallback inside router.py
  - All existing extraction engines and internal flows work as today
  - Nothing in this path is modified by this task

These paths are mutually exclusive per request. A workflow exposed
in the MCP for Path A must account for the fact that ChatGPT has
already done any intelligence work before calling execute_workflow.
OpenCEO must never redo work that ChatGPT already did. When designing
each workflow's MCP interface, ask: "what has ChatGPT already processed
before this call arrives?" and expose the appropriate direct-execution
variant, not the AI-extraction variant.

This principle applies to every current and future workflow.
Document it as an architectural comment at the top of the MCP tools
file so every future developer understands it before adding new tools.

---

PART 1 — ADD TOOLS TO THE EXISTING MCP SERVER

Do not create new routes, new mount points, or new MCP server instances.
Read the existing MCP implementation and add the following tools
following exactly the same pattern already established in that code.

All tool descriptions must be SHORT. Every token in a tool description
is sent on every ChatGPT request and consumes subscription quota.

--- TOOL 1: get_company_context ---

get_company_context() -> dict

Returns the complete agent and workflow catalog in a single call.
ChatGPT calls this once per conversation and caches the result.

Build dynamically by reading all projects/*/project.yaml files
and hardcoded schema for agents whose workflows live in code not YAML.

For each workflow include:
- name
- description (one line)
- params: each param with type, required bool, and allowed values if enum
- execution_mode: "sync" | "async"
- requires_approval: true | false
  Mark requires_approval=true for any workflow that is irreversible
  or has real-world consequences (payments, emails, external writes).

Expose ALL currently operational workflows read from README.md and
STATUS.md. For pm, expose two variants:
- create_tickets: receives pre-structured tickets array,
  writes directly to Notion. This is the workflow for Path A.
- extract_and_create_tickets: receives raw text, runs AI extraction
  inside OpenCEO. Mark with note: "Path B only. In ChatGPT path
  always use create_tickets."

Return structure:
{
  "agents": {
    "{agent_name}": {
      "description": "...",
      "workflows": {
        "{workflow_name}": {
          "description": "...",
          "execution_mode": "sync|async",
          "requires_approval": true|false,
          "params": {
            "{param_name}": {
              "type": "string|array|boolean",
              "required": true|false,
              "enum": [...] | null,
              "note": "..." | null
            }
          }
        }
      }
    }
  },
  "active_projects": [...]
}

--- TOOL 2: get_user_context ---

get_user_context(user_id: str = "owner") -> dict

Reads from MongoDB user_context collection.
Returns active project, active dev session state, and any running
background jobs for the current user.

{
  "active_project": "string | null",
  "active_dev_session": {
    "session_name": "string",
    "provider": "string",
    "status": "string"
  } | null,
  "background_jobs": [
    {
      "task_id": "string",
      "agent": "string",
      "workflow": "string",
      "status": "string",
      "started_at": "ISO datetime"
    }
  ]
}

--- TOOL 3: execute_workflow ---

execute_workflow(agent: str, workflow: str, params: dict,
                 user_id: str = "owner") -> dict

Executes the specified workflow. Agent and workflow are already
resolved by ChatGPT before this call arrives.
Bypasses router.py and graph.py entirely — no LLM routing.
Calls directly into the appropriate existing service layer function.

For each workflow, call the correct existing service function.
Read the service files to find the right function to call —
do not duplicate logic, do not reimplement anything.

For pm.create_tickets:
  Validate each ticket has at least a title.
  Call notion batch creation directly from tools/notion.py.
  Return {"created": N, "failed": M}

For async workflows return {"task_id": "...", "status": "running"}.
For sync workflows return the result directly.

--- TOOL 4: get_task_status ---

get_task_status(task_id: str) -> dict

Reads from MongoDB tasks collection.
{
  "task_id": "string",
  "status": "queued|running|completed|failed",
  "agent": "string",
  "workflow": "string",
  "summary": "string | null",
  "result": {} | null,
  "created_at": "ISO datetime",
  "updated_at": "ISO datetime"
}

---

PART 2 — UI WIDGETS

Add a widget only when a required param is missing AND the valid options
form a bounded list. In that case render a minimal inline form with
a dropdown or radio group and a Submit button. This replaces one
round-trip message.

Rules:
- If the missing param is free-text: ask in plain text, no widget
- If all params are present in the message: no widget
- For confirmations of irreversible actions: plain text, no widget
- For content review of any kind: conversational plain text, no widget

Implement widgets as self-contained HTML returned under a "widget_html"
key in the tool response. No external dependencies. Under 50 lines
of inline HTML+CSS.

---

PART 3 — CHATGPT SYSTEM PROMPT

Create: openceo-core/agents/ceo/chatgpt_system_prompt.md

This is the system prompt to paste into the ChatGPT Apps SDK config.
Make it self-contained. Write it to produce deterministic CEO behavior.

STARTUP:
On the first message of every conversation, call get_user_context()
and get_company_context(). Cache both. Do not call them again unless
the user asks to refresh.

REQUEST HANDLING:
1. Identify agent and workflow from the catalog.
2. Determine required params.
3. If you can process the user's input yourself to produce a required
   param (structured data, extracted content, assembled payload),
   do that processing first before asking the user for anything.
4. Ask only for params you genuinely cannot derive from the input.
5. For workflows with requires_approval=true, present a summary of
   what you are about to execute and wait for explicit user
   confirmation before calling execute_workflow.
6. For read operations and status checks, execute immediately.
7. For async workflows, confirm the task is running and tell the user
   to ask for status when they want an update.

INTELLIGENCE BEFORE EXECUTION:
You are the intelligence layer. OpenCEO is the execution layer.
If the user provides content that needs to be processed, structured,
or transformed before it can be sent to OpenCEO, do that processing
yourself before calling any tool. Only call execute_workflow when
the payload is fully ready and confirmed. This applies to every
workflow, not just specific ones.

APPROVAL LOOPS:
For workflows marked requires_approval=true, always show the user
what you are about to send and wait for explicit confirmation.
For workflows with large or complex payloads, present a summary,
allow the user to give corrections in natural language, apply them
in the conversation, and only call execute_workflow when the user
explicitly confirms. This is always conversational — no widgets.

TRANSPARENCY:
If the user asks where you are sending something or which tool you
are using, explain exactly: agent, workflow, and params.

LANGUAGE:
Respond in the same language the user writes in.

SCOPE:
Do not handle workflows not present in get_company_context output.
If a workflow does not exist, say so clearly.

---

PART 4 — README UPDATE

Add section: "## ChatGPT MCP Orchestrator"

Document:
- The two-path architecture and why the paths never mix
- The intelligence-before-execution principle
- The approval loop rule for requires_approval=true workflows
- The direct vs extraction principle for PM and why it generalizes
  to any workflow where ChatGPT can pre-process the payload
- The 4 tools: what each does and when ChatGPT calls each
- Reference to openceo-core/agents/ceo/chatgpt_system_prompt.md

---

FILES TO MODIFY OR CREATE:
- MODIFY: existing MCP server file (find it, read it, add tools to it)
- CREATE: openceo-core/agents/ceo/chatgpt_system_prompt.md
- MODIFY: README.md

DO NOT MODIFY:
router.py, graph.py, dev_session_service.py, tmux_runtime.py,
consensus.py, any agent session files, tools/notion.py,
tools/email_sender.py, or any other existing file not listed above.
All new tools must call into existing service functions only.
No logic duplication.