TASK: Add 2 extra OpenCode reviewers (Kimi K2.6 + Qwen 3.6 Plus) to Tier S code-review rounds

RESUMEN (ES):
Hoy Tier S corre 3 reviewers en paralelo reusando sesiones tmux (clauded, codexf,
gemini). Queremos sumar 2 reviewers MÁS usando OpenCode Go: Kimi K2.6 y Qwen 3.6 Plus.
OpenCode NO usa tmux: corre one-shot como en Tier 3 (`opencode run --format json -m <model>`).
Los 5 reviewers corren al mismo tiempo. Sus archivos REVIEW se pasan al builder (claudep)
en la ronda de fixes. La decisión de modelos (Kimi 2.6 + Qwen 3.6 Plus) viene del deep-research
del usuario; NO se investigaron benchmarks adicionales en esta tarea.

================================================================================
CONTEXT — READ FIRST
================================================================================
Read these files completely before writing any code. Do not assume patterns —
derive everything from the actual code.

- openceo-core/agents/dev/consensus.py        (Tier S orchestrator — main change)
- openceo-core/agents/dev/opencode_run.py      (build_run_command — reuse it)
- openceo-core/agents/dev/opencode_models.py   (apply_opencode_model_override — reuse it)
- openceo-core/agents/dev/opencode_paths.py    (normalize_opencode_command — reuse it)
- openceo-core/agents/dev/session.py           (see _step_opencode for the one-shot pattern)
- openceo-core/agents/dev/opencode_session_manager.py (see how OPENCODE_GO_API_KEY is injected)
- openceo-core/config_loader.py                (tier_s defaults, _merge_with_defaults)
- openceo-core/settings.py                     (model slugs + OPENCODE_GO_API_KEY already exist)
- projects/jardal-trading/project.yaml         (the live tier_s project)

DO NOT MODIFY (README is the single source of truth for architecture and is READ-ONLY):
- README.md, tmux_runtime.py, router.py, graph.py, any agent session file other than
  consensus.py, and the live opencode one-shot path in session.py (`_step_opencode`).

================================================================================
ARCHITECTURAL DECISION — WHY ONE-SHOT, NOT TMUX
================================================================================
The 3 existing Tier S reviewers reuse PERSISTENT tmux sessions (planner /
plan-reviewer / gemini-reviewer) because those CLIs are interactive REPLs.
OpenCode is fundamentally stateless/one-shot: every `opencode run` is a fresh
subprocess. This is exactly how Tier 3 already runs it (session.py `_step_opencode`).

Therefore the 2 new reviewers:
- MUST run as one-shot subprocesses (`asyncio.create_subprocess_exec`), NOT tmux.
- MUST NOT be added to `ensure_project_sessions()` / `close_project()` /
  `get_live_output()` (they have no persistent session to manage).
- Run concurrently with the 3 tmux reviewers in the SAME `asyncio.gather`.

REVIEWER MAP (conceptual numbering the user asked for):
- Reviewer 1 = clauded   (claude,  reuses planner session)        — existing
- Reviewer 2 = codexf    (codex,   reuses plan-reviewer session)  — existing
- Reviewer 3 = gemini    (gemini,  reuses gemini-reviewer session)— existing
- Reviewer 4 = kimi26    (opencode-go/kimi-k2.6,   one-shot)      — NEW
- Reviewer 5 = qwen36    (opencode-go/qwen3.6-plus, one-shot)     — NEW
A 6th reviewer later is trivial: append one tuple to the config list (see below).

Review file naming keeps the existing descriptive convention:
  .tier-s/REVIEW-kimi26-{p}-{round}.md
  .tier-s/REVIEW-qwen36-{p}-{round}.md
Last line of each file must be exactly: REVIEW_DONE_{p}_{round}_{label}
(same contract the tmux reviewers already use).

================================================================================
PART 1 — consensus.py  (the core change)
================================================================================

1.1 IMPORTS (top of file)
Add:
  import os
  from .opencode_models import apply_opencode_model_override
  from .opencode_paths import normalize_opencode_command
  from .opencode_run import build_run_command
(`asyncio`, `logging`, `DevProvider` are already imported.)

1.2 NEW CONSTANT  (after `_REVIEW_LABELS`)
  # OpenCode one-shot reviewers (no tmux). (label, opencode model slug)
  # Default set; override via dev.tier_s.opencode_reviewers in project.yaml.
  # An explicit empty list in config disables OpenCode reviewers.
  _DEFAULT_OPENCODE_REVIEWERS: tuple[tuple[str, str], ...] = (
      ("kimi26", "opencode-go/kimi-k2.6"),
      ("qwen36", "opencode-go/qwen3.6-plus"),
  )

1.3 _normalized_config()  — parse opencode_reviewers
Inside `_normalized_config`, after the roles loop, parse the new key. Semantics:
- key ABSENT            -> use _DEFAULT_OPENCODE_REVIEWERS (both reviewers on)
- key present as []     -> disabled (no OpenCode reviewers)
- key present as list   -> use the provided [{label, model}, ...]

  raw_oc = tier_s_cfg.get("opencode_reviewers", None)
  if raw_oc is None:
      opencode_reviewers = list(_DEFAULT_OPENCODE_REVIEWERS)
  elif isinstance(raw_oc, list):
      opencode_reviewers = []
      for item in raw_oc:
          if isinstance(item, dict):
              label = str(item.get("label", "")).strip()
              model = str(item.get("model", "")).strip()
              if label and model:
                  opencode_reviewers.append((label, model))
  else:
      opencode_reviewers = list(_DEFAULT_OPENCODE_REVIEWERS)

Add to the returned dict:  "opencode_reviewers": opencode_reviewers

1.4 _run_review_round()  — launch tmux + opencode reviewers in parallel
This method currently (a) deletes stale review files for `_REVIEW_LABELS`,
(b) builds tmux review tasks, (c) gathers, (d) warns if ok_count < 3.
Change it to ALSO handle the opencode reviewers. Pass the raw `config` in so the
opencode command can be resolved (see 1.5). Update the call site (see 1.7).

  - Stale cleanup: also unlink paths.review(label, p, round_) for each
    (label, _model) in cfg["opencode_reviewers"].
  - Build opencode tasks:
        oc_reviewers = cfg["opencode_reviewers"]
        api_key = self._settings.OPENCODE_GO_API_KEY or os.environ.get("OPENCODE_GO_API_KEY", "")
        if oc_reviewers and not api_key:
            logger.warning("OpenCode reviewers configured but OPENCODE_GO_API_KEY is empty; skipping them")
            oc_reviewers = []
        oc_tasks = [
            self._one_opencode_review(label, model, p, round_, paths, cfg, config)
            for label, model in oc_reviewers
        ]
  - tmux_tasks stays as today (built from _REVIEW_LABELS).
  - results = await asyncio.gather(*tmux_tasks, *oc_tasks, return_exceptions=True)
  - ok_count = sum(1 for r in results if r is True)
  - expected = len(_REVIEW_LABELS) + len(oc_reviewers)
  - if ok_count < expected: logger.warning("Review round %d sub-plan %d: %d/%d reviewers completed",
        round_, p, ok_count, expected)
  - return ok_count

1.5 NEW METHOD — _one_opencode_review()
One-shot OpenCode reviewer. Reuses _resolve_command (already supports
DevProvider.OPENCODE), the model override helper, and build_run_command.
Success = the REVIEW file ends with the expected signal (same rule as tmux
reviewers via _review_file_done). Never raises; returns bool.

  async def _one_opencode_review(
      self,
      reviewer_label: str,
      model: str,
      p: int,
      round_: int,
      paths: _TierSPaths,
      cfg: dict[str, Any],
      config: dict[str, Any],
  ) -> bool:
      rpath = paths.review(reviewer_label, p, round_)
      expected_signal = f"REVIEW_DONE_{p}_{round_}_{reviewer_label}"
      try:
          base_command, env_vars, cwd = self._resolve_command(DevProvider.OPENCODE, config)
          base_command = apply_opencode_model_override(base_command, model)
          base_command = normalize_opencode_command(base_command)
          argv = build_run_command(
              base_command=base_command,
              prompt=self._code_reviewer_prompt(reviewer_label, p, round_, paths),
              cwd=cwd,
              title=f"tier-s review {reviewer_label} p{p} r{round_}",
              agent_name="build",
          )
          env = {**os.environ, **env_vars}
          api_key = self._settings.OPENCODE_GO_API_KEY or os.environ.get("OPENCODE_GO_API_KEY", "")
          if api_key:
              env["OPENCODE_GO_API_KEY"] = api_key

          process = await asyncio.create_subprocess_exec(
              *argv,
              stdin=asyncio.subprocess.DEVNULL,
              stdout=asyncio.subprocess.PIPE,
              stderr=asyncio.subprocess.PIPE,
              cwd=cwd,
              env=env,
          )
          try:
              await asyncio.wait_for(process.communicate(), timeout=cfg["timeout_seconds"])
          except asyncio.TimeoutError:
              try:
                  process.kill()
                  await process.wait()
              except Exception:
                  pass
              logger.warning("OpenCode reviewer %s timed out (sub-plan %d round %d)",
                             reviewer_label, p, round_)
              # If the file was already written with the signal, still count it.
              return self._review_file_done(rpath, expected_signal)

          return self._review_file_done(rpath, expected_signal)
      except Exception:
          logger.warning("OpenCode reviewer %s failed (sub-plan %d round %d)",
                         reviewer_label, p, round_, exc_info=True)
          return False

NOTE: `_resolve_command` already returns (command, env_vars, cwd) for
DevProvider.OPENCODE using dev.providers.opencode.command (or OPENCODE_CLI_PATH +
OPENCODE_DEFAULT_ARGS). `build_run_command` produces:
  opencode run --format json -m <model> --dir <cwd> --title <t> --agent build <prompt>
The model override is applied BEFORE build_run_command so `-m` carries the
per-reviewer model. Reviewers only WRITE the REVIEW markdown file; the prompt
(reused from _code_reviewer_prompt) already forbids modifying source code.

1.6 _builder_fix_prompt()  — feed the new review files to the builder (claudep)
The builder must read ALL review files, including the 2 OpenCode ones.
Pass the opencode reviewer list into the prompt builder.

  - Change _run_builder_fix to call:
        self._builder_fix_prompt(p, round_, paths, cfg["opencode_reviewers"])
  - Change the signature/body:
        def _builder_fix_prompt(self, p, round_, paths, opencode_reviewers):
            labels = [label for label, _ in _REVIEW_LABELS] + [label for label, _ in opencode_reviewers]
            review_files = [f"{_TIER_S_DIR}/REVIEW-{label}-{p}-{round_}.md" for label in labels]
            file_list = "\n".join(f"- {f} (if exists)" for f in review_files)
            ... (rest of the prompt unchanged)
  The "(if exists)" wording already makes missing files tolerable, so a reviewer
  that failed/timed out does NOT block the fix round.

1.7 CALL SITE — _execute_impl()
Update the review-round call to thread `config` through:
  reviews_done = await self._run_review_round(sessions, p, r, paths, cfg, progress, config)
(Adjust _run_review_round signature to accept `config`.)

================================================================================
PART 2 — config_loader.py  (defaults + merge sanity)
================================================================================
In `_merge_with_defaults`, add `opencode_reviewers` to the default tier_s block
so the shape is documented and discoverable:

  "tier_s": {
      "review_rounds": 2,
      "max_plans": 15,
      "timeout_minutes": 15,
      "roles": { ... unchanged ... },
      "opencode_reviewers": [
          {"label": "kimi26", "model": "opencode-go/kimi-k2.6"},
          {"label": "qwen36", "model": "opencode-go/qwen3.6-plus"},
      ],
  },

VERIFY THE MERGE SEMANTICS: confirm whether `_merge_with_defaults` deep-merges or
shallow-merges nested dicts/lists. The single source of truth for the default
must remain `_normalized_config` (key-absent -> _DEFAULT_OPENCODE_REVIEWERS).
Make sure a project that sets its own `opencode_reviewers` list is NOT silently
overridden by the defaults, and that an explicit `[]` in a project still reaches
`_normalized_config` as `[]` (disabled), not replaced by the default. If the
merge would clobber an explicit list, fix the merge or keep the default ONLY in
`_normalized_config` and drop it from config_loader.

================================================================================
PART 3 — projects/jardal-trading/project.yaml  (the live tier_s project)
================================================================================
Under `dev.tier_s:` add:

    opencode_reviewers:
      - label: kimi26
        model: opencode-go/kimi-k2.6
      - label: qwen36
        model: opencode-go/qwen3.6-plus

The existing `dev.providers.opencode` block already points at the right CLI
(/home/develop/.opencode/bin/opencode); the per-reviewer model override replaces
its `-m` value at runtime, so the model in that block is irrelevant for reviews.

================================================================================
PART 4 — Environment prerequisite
================================================================================
OpenCode Go needs `OPENCODE_GO_API_KEY`. It is read from settings
(settings.OPENCODE_GO_API_KEY, falls back to the process env). Confirm it is set
in `.env` / the host-mode environment, exactly like the QA path
(opencode_session_manager.py) already does. If it is empty, the orchestrator
skips the OpenCode reviewers with a warning (Part 1.4) and Tier S still completes
with the 3 tmux reviewers — it must NOT crash.

================================================================================
PART 5 — Tests
================================================================================
Add tests/test_tier_s_opencode_reviewers.py covering:
- _normalized_config: key absent -> 2 default reviewers; explicit [] -> disabled;
  custom list -> parsed verbatim; malformed items dropped.
- _builder_fix_prompt: output lists REVIEW-kimi26-... and REVIEW-qwen36-... plus
  the 3 existing tmux review files, all with "(if exists)".
- argv construction: apply_opencode_model_override + build_run_command yields a
  command containing `run`, `--format`, `json`, `-m`, `opencode-go/kimi-k2.6`,
  `--agent`, `build` (mirror the style of tests/test_opencode_runner.py; mock the
  subprocess — do not actually call the CLI).
- _run_review_round expected-count math: ok_count threshold is
  len(_REVIEW_LABELS) + len(opencode_reviewers) (not hardcoded 3).
Run: source .venv/bin/activate (or .venv/bin/python -m pytest) and run the dev
test suite (test_opencode_runner.py, test_dev_session_markers.py, the new file).

================================================================================
ACCEPTANCE CRITERIA
================================================================================
1. A Tier S review round launches 5 reviewers in parallel: 3 tmux + 2 OpenCode
   one-shot (Kimi K2.6 + Qwen 3.6 Plus).
2. Each OpenCode reviewer writes .tier-s/REVIEW-<label>-<p>-<round>.md ending in
   REVIEW_DONE_<p>_<round>_<label>; no tmux session is created for them.
3. The builder fix round reads all 5 review files (existing 3 + new 2).
4. With OPENCODE_GO_API_KEY unset, Tier S completes using the 3 tmux reviewers and
   logs a warning — no crash.
5. A failed/timed-out OpenCode reviewer is tolerated (counts as not-ok, "(if
   exists)" keeps the fix round running).
6. Setting opencode_reviewers: [] in a project disables both; adding a 3rd tuple
   adds a 6th reviewer with no other code change.
7. New + existing dev tests pass.

================================================================================
OUT OF SCOPE / NOTES
================================================================================
- Model choice (Kimi K2.6 + Qwen 3.6 Plus) is fixed per the user's deep-research.
  Do NOT swap models or fabricate benchmark numbers.
- OpenCode reviewers won't appear in get_live_output()/dev_session_service tmux
  regex (no persistent session) — their progress is the REVIEW file + the
  orchestrator's progress() messages. Live-tailing the REVIEW file is a possible
  future nicety, not part of this task.
- README.md is read-only; do not edit it. If desired, note the change in
  STATUS.md / PENDINGS.md only.

FILES TO MODIFY OR CREATE:
- MODIFY: openceo-core/agents/dev/consensus.py
- MODIFY: openceo-core/config_loader.py
- MODIFY: projects/jardal-trading/project.yaml
- CREATE: tests/test_tier_s_opencode_reviewers.py
- VERIFY: .env has OPENCODE_GO_API_KEY (no code change, just confirm)
