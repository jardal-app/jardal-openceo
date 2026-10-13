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
