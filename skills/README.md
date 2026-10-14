# Role skills (epic TASK-2026-00187, format TASK-2026-00203)

Single source of every agent's skills: `skills/<role>/<skill>/SKILL.md`
(Agent Skills standard) with optional `scripts/` and `references/` dirs.

## Header

`SKILL.md` opens with frontmatter: the standard `name` (must match the
directory) and `description`, plus the OpenCEO header:

```yaml
---
name: triage
description: Sorts inbox findings into claimed work.
status: borrador        # borrador | activa
ticket: TASK-2026-00203 # creation ticket
date: 2026-10-12
inputs:                 # optional handoff contract (TASK-2026-00211)
  - inbox_slice
outputs:
  - triage_report
evidence:               # required evidence names (TASK-2026-00216)
  - report_path
---
```

## Rules (enforced by `openceo-core/catalog.py`, fail-closed)

- An AI card lists exactly the skills on disk for its role; human cards
  (owner) are exempt.
- Names are unique across roles and never collide with the Muse engine
  skills (`create-skill`, `git`).
- Every `scripts/**/*.py` that is not a test needs a `test_<stem>.py`
  under the skill dir; a script only where exactness or safe repetition
  is needed, never for a one-liner.
- Each skill is exposed once as a `.codex/skills/<skill>` symlink to its
  source dir: Muse discovers project skills only there, and role
  activation keeps every non-role skill off. Never commit a second copy.
- The exposure dir is local (`.codex/` is gitignored), so a fresh clone
  has no links: after adding a skill, recreate them with
  `catalog.sync_skill_links` (the test suite recreates them itself via
  `tests/conftest.py`; the service still needs them on disk to boot).
  The crystallization save step calls it in
  code (TASK-2026-00210); until then, from `openceo-core/` run:

  ```bash
  ../.venv/bin/python -c "from catalog import discover_role_skills, sync_skill_links; print(sync_skill_links(discover_role_skills()))"
  ```

  The scan reads `skills/*/` straight from disk on purpose: `load_catalog`
  fails closed on the card mismatch and the missing link, exactly when the
  operator needs the map (review 00006). List the skill on its card as well,
  or the service still refuses to boot.

## Transfer between roles (TASK-2026-00244)

A skill has exactly one owner: the role dir it lives in. To specialize,
move the dir (`skills/<old>/<skill>/` → `skills/<new>/<skill>/`), update
both cards and profiles (skills coverage is cross-checked both ways),
drop the stale `.codex/skills/<skill>` link and recreate it with
`catalog.sync_skill_links`. The header keeps its creation `ticket` as
history; each role still learns only inside its own scope, and no shared
listing exists — reuse common steps via helpers under `references/` or
`scripts/`, never by copying the recipe.

## Exact skills (no LLM)

A skill dir may carry `workflow.yaml` (same format as `workflows/`): when a
ticket names that skill in `params.skill`, the generic executor
(TASK-2026-00205) runs the YAML in the deterministic engine instead of
invoking the model. Completeness is judged by the run: the workflow reads
the ticket (title, description, params) as its context, and a step that
needs missing data fails the run as `bloqueado`. Unknown tools and HITL
waits propagate exactly as the engine returns them.

## Checks

- `muse skills validate skills/<role>/<skill>` (also runs in the suite).
- Planner: the per-agent index (name + description + status) travels in
  the prompt; a suggested skill must name a real one of its target.

## Handoff between tickets (TASK-2026-00211)

`inputs`/`outputs` declare what the skill receives and delivers (names
from the ticket text and params on the way in; verdict and file names
on the way out). On close, a `hecho` run publishes its files as
private ticket attachments and its data in the execution record
(`params.ticket_outputs`); on launch, the generic executor resolves
the `blocked_by` predecessors and delivers their recorded outputs —
staged files under `upstream/<task_id>/` plus a prompt section (LLM
path) or the `upstream_outputs` params (exact path). See
`openceo-core/orchestration/ticket_handoff.py`.

`evidence` names what the skill's verdict must cite as proof
(TASK-2026-00216): the final review verifies each declared output is
named in the structured result and each required evidence in its
`evidencia` (normalized, case- and separator-blind). A skill needing a
stricter domain check ships `verify.py` with `verify(child, outputs)`
returning the `{ok, missing_artifacts, unmet_criteria}` report; it
runs after the generic check passed, and a broken one parks the
objective instead of passing silently. See
`openceo-core/objectives/delivery_verification.py`.

## Crystallization drafts (TASK-2026-00210)

Agents never write here directly. On close, an agent that ran a
recurrent process leaves `<ticket-dir>/skill_drafts/<skill>/` (SKILL.md
with name + description, optional scripts/references/workflow.yaml);
the save step (`openceo-core/orchestration/crystallization.py`) stamps
the header (status/ticket/date), validates (normative loader, `muse
skills validate`, fenced script tests when scripts changed) and commits
skills + card + profile in a worktree over origin/main with the ticket
number. New enters as `borrador`; reuse from another ticket promotes to
`activa`; past 10 drafts per role the oldest is deleted. Failures
discard the draft with a ticket comment and never touch the outcome.

After pulling crystallization commits, recreate the exposure links in
this checkout (the push lands on origin/main, never here):

```bash
cd openceo-core && ../.venv/bin/python -c "from catalog import discover_role_skills, sync_skill_links; print(sync_skill_links(discover_role_skills()))"
```
