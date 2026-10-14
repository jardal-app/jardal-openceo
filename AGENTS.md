# AGENTS.md — Contrato operativo raíz (Tier S F7a)

Repo-root instructions. Every CLI in the Tier S roster reads this file
directly; prompts never paste it.

Sesiones del Owner: lee CLAUDE.md.

### Layout

- `openceo-core/` — orchestrator (Python, FastAPI, LangGraph), tests in `tests/`
- `openceo-core/agents/dev/tier_s/` — Tier S engine (F1–F7): single-ticket
  cycle, plan, gates, models, stores, telemetry
- `openceo-core/orchestration/` — shared orchestration both engines import:
  one-shot executors, review pool/rounds, findings, inbox, knowledge
- `openceo-core/objectives/` — company objective engine (execution state, planner models)
- `openceo-core/company_mandate.py` — company mandate + owner escalation policy
- `openceo-core/ops/` — deterministic project-creation library behind the operations skill
- `openceo-react/` — dashboard (React 19 + Vite + Tailwind v4)
- `openceo-voice/`, `openceo-voice-android/` — realtime voice + Android app
- `workflows/` — deterministic Tier-0 YAML workflows
- `catalog/` — single agent catalog (index + cards + staff); loader `openceo-core/catalog.py` (Pydantic, fail-closed, boot gate)
- `projects/<project>/project.yaml` — per-project config (validated)
- `docs/work/TASK-2026-000NN-tema.md` — temporary ticket research (delete after)

### Python

- Always use the root venv: `./.venv/bin/python -m pytest …`
- Never create venvs in `/tmp`; never force-install into system python
- Compile-check touched files: `python3 -m py_compile <files>`

### Validate before finishing

```bash
./.venv/bin/python -m pytest tests/test_tier_s_final.py tests/test_tier_s_contract.py \
  tests/test_coding_tool_routing.py tests/test_tier_s_config_validation.py -q
```

Full suite (~8 min): `./.venv/bin/python -m pytest tests/ -q`

### Service (host mode)

```bash
scripts/openceo-core-host.sh restart|status
```

### Tier S executor contract

**Scope: this contract applies ONLY when your prompt names you a Tier S executor (builder/reviewer). Owner sessions follow CLAUDE.md §9 instead; executors never follow §9.**

- Work on the checked-out branch; NEVER commit, push, rebase, stash, or
  change branches. Only the orchestrator commits (squash) and merges.
- Orchestrator files live in `.openceo/` (PLAN, FINDINGS, FIX, HANDOFF,
  status; objectives engine: `objectives/<id>/`, `PLANNER_RESULT.json`,
  `OBJECTIVE_CLOSURE.md`). `.openceo/` is the orchestrator-wide scratch
  (TASK-2026-00136). Read them; write only the artifact your prompt names.
- Gates are run by the orchestrator; report failures with file + line.
- Reviewers: read-only worktrees, reports on stdout in §16.4 format (specified in the review request).
- One ticket = one session; no transcripts or diffs in prompts.

### Standing rules

- No polling or Mongo/API sweeps: react to CRUD events only.
- ERPNext owns business state (`Task`/`Project`); Mongo owns execution.
  Never create Mongo-only tickets: internal creation goes through the
  ticket API (ERPNext first). Bounded exception: Horizon personal tasks
  are not company tickets and stay Mongo-only (no business mirror);
  any work on a company project/repo goes to ERPNext first.
- No secrets, credentials, or personal data in notes or temp docs.
- Conventions over configuration: mirror sibling code, reuse its helpers.
- Owner rule (2026-10-14; current epic TASK-2026-00245): validation uses
  only simple, targeted logic/contract/communication tests with mocks,
  simulated executors/CLIs, or non-generative echo/help/version checks.
  No validation calls to live models, extensive integrated rehearsals
  (including simulated ones), or full objective/mission/Tier S test runs.
  Run the mandatory focused checks above; do not require the full suite
  to close this epic. These limits concern tests, not CLI invocations
  needed to implement authorized work. They supersede the component-test
  token allowance from completed epic TASK-2026-00187. Its pilots remain
  separate in TASK-2026-00233 (TASK-2026-00219 and TASK-2026-00224), only
  with `valida` in a dedicated Owner session and completed dependencies,
  never through the current coding loop (CLAUDE.md §11).
