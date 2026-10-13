---
name: crear-proyecto
description: "Creates a company project end to end (ERPNext identity, git repo, validated project.yaml, vault hub, DNS for web projects). Use when the ticket asks for a new company project."
status: activa
ticket: TASK-2026-00214
date: 2026-10-12
---

# Crear proyecto

First real case of the role model: a known process as an operations
skill. Every step is idempotent — re-running reports `existed` and
duplicates nothing. This skill is the only way to create a project:
the legacy Tier-0 flow retired in TASK-2026-00223.

## Inputs (ticket title, description, params)

- `project_key` (required, `[a-z0-9-]`), `display_name`, `description`.
- `repo` (default: the key), `org` (default: `jardal`).
- `remote_url` (default: the conventional `git@github-jardal` URL).
- `is_web` + `hostname` (FQDN): only web projects get DNS.

## Setup

Run from the ticket folder. The skill runs staged there:

```bash
SKILL=$(readlink -f .codex/skills/crear-proyecto)
REPO=$(dirname $(dirname $(dirname $SKILL)))
PY=$REPO/.venv/bin/python
CLI="$PY $SKILL/scripts/crear_proyecto.py"
REPOS=$(echo "$($CLI roots)" | $PY -c 'import json,sys; print(json.load(sys.stdin)["result"]["repos"])')
```

Effective roots (flags override them; the rehearsal passes doubles):

```bash
$CLI roots
```

## Steps

1. ERPNext identity (boundary tool, holds the business record):
   `openceo_ensure_erpnext_project(project_key, display_name)`.
2. Local repo, one recipe line (`REPOS` from Setup):
   `mkdir -p $REPOS/<repo> && { test -e $REPOS/<repo>/.git || git -C $REPOS/<repo> init -b main -q; } && (git -C $REPOS/<repo> remote add origin <remote_url> 2>/dev/null || git -C $REPOS/<repo> remote set-url origin <remote_url>)`.
3. Templates (exact script, never by hand):
   `$CLI templates --project-key <key> --repo <repo> --display-name "<name>" --org <org> --description "<text>" --remote-url <url>`.
4. Validated config: `$CLI project-config` with the same flags. It
   refuses to finish when `project.yaml` fails validation.
5. Vault entry: `$CLI vault-entry` with the same flags.
6. DNS for web projects only (boundary tool, holds the secret):
   `manage_dns_record(hostname)`. Creation only; deletion needs the
   Owner and arrives with its lock in TASK-2026-00208.

Each CLI call answers JSON (`ok`, `dry_run`, `result`/`error`).
Keep the JSON outputs: they are the run evidence.

## Verify

- ERPNext `Project` exists for the key.
- The repo has the conventional remote and the initial commit.
- `projects/<key>/project.yaml` exists and validates clean.
- The vault hub and its own `README.md` line exist.
- Web only: the hostname resolves.

## Errors

- Bad key or a config/hub naming another project: stop, report the
  CLI error verbatim — never force it.
- Validation errors: report them, do not hand-edit the config.
- MCP failures: report which tool failed and its error.
- Unknown costs stay unknown: spend approval lives in the plan
  (TASK-2026-00207), never in this recipe.
