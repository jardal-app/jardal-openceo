# AGENTS.md — jardal-openceo (operative)

Repo-root instructions. Every CLI in the Tier S roster reads this file
directly; prompts never paste it.

## Layout

- `openceo-core/` — orchestrator (Python, FastAPI, LangGraph), tests in `tests/`
- `openceo-core/agents/dev/tier_s/` — Tier S engine (F1–F7): cycle, review
  rounds, findings inbox, objectives, knowledge
- `openceo-react/` — dashboard (React 19 + Vite + Tailwind v4)
- `openceo-voice/`, `openceo-voice-android/` — realtime voice + Android app
- `workflows/` — deterministic Tier-0 YAML workflows
- `projects/<project>/project.yaml` — per-project config (validated)
- `docs/work/TASK-2026-000NN-tema.md` — temporary ticket research (delete after)

## Python

- Always use the root venv: `./.venv/bin/python -m pytest …`
- Never create venvs in `/tmp`; never force-install into system python
- Compile-check touched files: `python3 -m py_compile <files>`

## Validate before finishing

```bash
./.venv/bin/python -m pytest tests/test_tier_s_final.py tests/test_tier_s_contract.py \
  tests/test_coding_tool_routing.py tests/test_tier_s_config_validation.py -q
```

Full suite (~8 min): `./.venv/bin/python -m pytest tests/ -q`

## Service (host mode)

```bash
scripts/openceo-core-host.sh restart|status
```

## Tier S executor contract

- Work on the checked-out branch; NEVER commit, push, rebase, stash, or
  change branches. Only the orchestrator commits (squash) and merges.
- Orchestrator files live in `.tier-s/` (PLAN, FINDINGS, FIX, HANDOFF,
  status). Read them; write only the artifact your prompt names.
- Gates are run by the orchestrator; report failures with file + line.
- Reviewers: read-only worktrees, reports on stdout in §16.4 format.
- One ticket = one session; no transcripts or diffs in prompts.

## Standing rules

- No polling or Mongo/API sweeps: react to CRUD events only.
- ERPNext owns business state (`Task`/`Project`); Mongo owns execution.
  Never create Mongo-only tickets: internal creation goes through the
  ticket API (ERPNext first).
- No secrets, credentials, or personal data in notes or temp docs.
- Conventions over configuration: mirror sibling code, reuse its helpers.
