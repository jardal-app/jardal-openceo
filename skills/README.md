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
  `catalog.sync_skill_links`. The crystallization save step calls it in
  code (TASK-2026-00210); until then, from `openceo-core/` run:

  ```bash
  ../.venv/bin/python -c "from catalog import load_catalog, load_all_role_skills, sync_skill_links; c = load_catalog(); print(sync_skill_links(load_all_role_skills(c)))"
  ```

## Checks

- `muse skills validate skills/<role>/<skill>` (also runs in the suite).
- Planner: the per-agent index (name + description + status) travels in
  the prompt; a suggested skill must name a real one of its target.
