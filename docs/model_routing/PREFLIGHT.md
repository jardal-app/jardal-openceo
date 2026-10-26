# Preflight for the validation epic children (TASK-2026-00297)

Run these checks from the stated checkout before starting each
TASK-2026-00284 child. Every check names the code it exercises;
`free-router` below is `./.venv/bin/python
openceo-core/model_routing/cli.py`.

## TASK-2026-00296 — keys, privacy, first production snapshot

Checkout: main checkout (`/home/develop/development/Jardal/jardal-openceo`).

1. `stat -c '%a' openceo-core/.env.free-router.local` prints `600`
   (`secrets.load_router_secrets` refuses wider modes).
2. `free-router validate-config` reads `ok` for policy, providers
   and secrets (`config.validate_config`).
3. `free-router update --providers` then `free-router update
   --benchmarks`: both exit 0 and publish `catalog.json`,
   `rankings.json` and `history/` snapshots in
   `~/.config/free-router` (`refresh.run_update`).
4. `free-router models --profile free-review --top 3` serves the
   published order; `free-router sources --show-staleness` shows
   every source fresh.
5. `GET /api/v1/key` usage equals the pre-update value
   (`sources.fetch_openrouter_key`,
   `live.assert_usage_unchanged`); every usable deployment keeps
   `free_verified` (`models.is_usable`).

## TASK-2026-00286 — merge feat/model-routing into main

Checkout: main checkout, main branch, clean tree.

1. `git status --short` empty; `git log --oneline main..feat/model-routing`
   lists only the epic commits.
2. After `git merge --no-ff feat/model-routing`:
   `./.venv/bin/python -m pytest tests/test_tier_s_final.py
   tests/test_tier_s_contract.py tests/test_coding_tool_routing.py
   tests/test_tier_s_config_validation.py -q` passes (AGENTS.md),
   plus `python3 -m py_compile` of the touched files.
3. `git push origin main`, then `scripts/openceo-core-host.sh
   restart`, `status` (single instance) and an endpoint smoke
   test; the dynamic ranking flag stays off.
4. Remove the loop only after main is verified: `git worktree
   remove` the model-routing worktree, delete the merged
   `feat/model-routing` branch, the
   `jardal-automatico-model-routing` folder and
   `~/.local/state/jardal-openceo-model-routing-automatico/`.

## TASK-2026-00299 — scheduled refresh on the host

Checkout: main, after TASK-2026-00286 and TASK-2026-00296.

1. `./.venv/bin/python openceo-core/model_routing/schedule.py
   --dry-run` prints the single marked block and changes nothing
   (`schedule.render_crontab_block`, markers `BEGIN/END
   free-router refresh`).
2. `crontab -l` holds no other refresh block (single scheduling
   source; `BASELINE.md` inventory).
3. `--install`, then `crontab -l` shows Cron B every 6 h and Cron A
   daily, both appending to `~/.local/state/free-router/updater.log`.
4. After two scheduled runs: exit 0 lines in `updater.log`, fresh
   `history/` snapshots, no `refresh_failed` alert in
   `~/.local/state/free-router/alerts.jsonl`, and `--uninstall`
   removes the block cleanly.

## TASK-2026-00301 — dev-pool pilot in production

Checkout: main, after TASK-2026-00286 and TASK-2026-00299.

1. A fresh production snapshot exists (`free-router status`
   shows catalog and rankings present).
2. Run one small secret-free tier C ticket: the two OpenCode
   reviewers are the top two eligible `free-review` models of
   distinct families in `native_first` order, with an explicit
   `--model` each (`review_wiring.pick_pair`); when group A is
   spent the failed reviewer restarts whole on the group B reserve.
   Each receipt pins effective model, family, route fields and
   sha256 (SPEC §11.7, addendum §11.7).
3. `free-router cost-audit --since 24h` exits 0 (no billing rows,
   receipts complete); `free-router errors --since 24h --summary`
   shows the fallbacks, if any.
4. Compare valid reviews, 429/quota counts, fallbacks and duration
   against `docs/model_routing/BASELINE.md`.
5. Set `review.free_router.enabled: False` and re-run: the manual
   list feeds the pool again, then restore `True`.

## TASK-2026-00298 — gateway security gate

Checkout: main, after TASK-2026-00295.

1. Confirm the HOLD is intact: no gateway process, no
   `gateway.generated.yaml` deployed anywhere
   (`external.build_gateway_plan` emits a logical contract for
   review only; field names need re-validation against the
   selected LiteLLM version). Native `opencode` routes never enter
   the plan (`native_routes` only, addendum §12).
2. Evaluate the pinned version+digest and the consolidated
   alternatives against SPEC §14.1, then write
   `security-approval.md` with the checklist from the ticket:
   digest from an independent source, non-root container with
   read-only rootfs, localhost only, no Docker socket or host
   mounts, egress allowlist, content logging off, key rotation
   and rollback rehearsed.
3. Rehearse the rollback path offline:
   `external.apply_security_rollback` freezes generation
   (`GatewayFrozenError` until `unfreeze_gateway`) and strips
   the config back to native (T20).

## TASK-2026-00300 — deploy the approved gateway

Checkout: main, only when TASK-2026-00298 approved; otherwise
cancel this ticket citing the Owner order.

1. Deploy on localhost with the approved digest, non-root,
   read-only rootfs, no content logging, only the needed
   `FREE_ROUTER_*` keys from
   `openceo-core/.env.free-router.local`, excluded from public
   Traefik and from automatic updaters.
2. Connect one real free-inference provider first
   (`external.build_gateway_plan` order follows the ranking;
   review aliases carry `num_retries: 0`). Externals only: no
   `opencode` native deployment enters the gateway (addendum §12,
   A01).
3. Verify `/v1/models`, tools and the effective endpoint, then a
   no-paid-calls E2E failover and the rollback to native OpenCode
   (T14 plan, `strip_gateway_provider`).
4. `free-router cost-audit --since 24h` exits 0 and every request
   carries an audited effective model.

## TASK-2026-00302 — research confirmation in production

Checkout: main, after TASK-2026-00286 and TASK-2026-00299.

1. Run one short real investigation (~5 turns) with MCP on a
   non-confidential topic, with one injected fault (never a real
   quota error): sources and checkpoints survive, the managed
   session switches to a compatible free model with `switchModel`
   and resumes from the last checkpoint, no answer or MCP command
   runs twice (`research_wiring.ManagedResearchSession`, T13,
   addendum §11.8).
2. `free-router sessions --active` lists the session while fresh;
   afterwards `free-router errors --since 24h` shows the injected
   fault classified and recorded.
3. OpenRouter credit usage unchanged across the session
   (`live.assert_usage_unchanged`).
