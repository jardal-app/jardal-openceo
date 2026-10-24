# Router runbook (TASK-2026-00297)

Start, stop, backup, recovery, key rotation and rollback to native
OpenCode. Every command below names the code it drives; paths are
SPEC §6.1 (`~/.config/free-router`, `~/.local/state/free-router`).
Run the CLI from the repo root as `./.venv/bin/python
openceo-core/model_routing/cli.py …`.

## Start

1. `free-router validate-config` (`config.validate_config`): policy,
   providers and secrets must all read `ok`. A missing secrets file
   or permissions wider than 0600 fail closed (`secrets.py`).
2. `free-router update --providers`, then `free-router update
   --benchmarks` (`refresh.run_update`): publish `catalog.json` and
   `rankings.json` plus immutable `history/` snapshots. A failed
   refresh keeps the previous snapshots byte-identical (T12) and
   appends a `refresh_failed` alert to
   `~/.local/state/free-router/alerts.jsonl`.
3. `free-router status` and `free-router models --profile free-review
   --top 5`: the snapshot ids must match the files just published.
4. Schedule the refresh only via the installer
   (`schedule.main`, dry-run by default):
   `./.venv/bin/python openceo-core/model_routing/schedule.py
   --dry-run` shows the single marked crontab block (Cron B every
   6 h, Cron A daily, both logging to `updater.log`); `--install`
   writes it idempotently. Activating it on the host is
   TASK-2026-00299.

## Stop

There is no router daemon to stop: the CLI and the crons are the
only scheduled surface.

1. `./.venv/bin/python openceo-core/model_routing/schedule.py
   --uninstall` removes the refresh block (`schedule.remove_block`);
   already-absent blocks report `nothing to remove`.
2. In-flight rounds keep their frozen snapshots and expire by TTL;
   nothing needs killing. To free one held slot explicitly:
   `free-router release --run-id … --round … --slot …`.

## Backup

Copy, never move, while the refresh lock is idle
(`update.lock` in the state dir; `run_update` takes it
non-blocking, so a backup never stalls a refresh):

- `~/.config/free-router/`: `policy.yaml`, `providers.yaml`,
  `catalog.json`, `rankings.json`, `history/`, `gateway.frozen`
  when present.
- `~/.local/state/free-router/`: `availability.sqlite`
  (reservations, cooldowns, error registry, audit, findings
  outbox), `alerts.jsonl`, `research_checkpoints/`,
  `updater.log`.
- `openceo-core/.env.free-router.local`: back up separately with
  0600 permissions; never store it next to logs or commit it.

## Recovery

1. Restore the files above, then `free-router validate-config`.
2. Crash recovery is lazy: expired holds read as absent on access
   (`AvailabilityStore.recover` drops them and reports the live
   ones; no CLI exposes it — the store heals on next use).
3. If a refresh died mid-publish, the previous `catalog.json` and
   its `history/` snapshot are intact (atomic temp+fsync+rename in
   `publish.atomic_write_json`); re-run the failed `update`.
4. Review the error registry after any incident: `free-router
   errors --since 24h --summary` (add `--state-dir` when the
   store lives outside the default), then `free-router cost-audit
   --since 24h` (exit 1 on billing evidence or broken receipts).
   Queued improvement findings are listed with `free-router errors
   --sync-findings` (report only; the inbox write happens where a
   store exists, `error_findings.drain_pending`).

## Key rotation

1. Edit `openceo-core/.env.free-router.local` in place, keep 0600,
   keep the `FREE_ROUTER_*` names (`secrets.load_router_secrets`
   refuses wider permissions and ignores unprefixed names, so the
   voice `GROQ_API_KEY` in `openceo-core/.env` is never picked up).
2. `free-router validate-config` must read `ok` for secrets.
3. Secrets load per invocation; restart the host service to be
   certain long-lived workers pick the new file up:
   `scripts/openceo-core-host.sh restart` (then `status`).
4. Never print, log or commit values; recorded fixtures are
   sanitized and a test scans them (`live.scan_for_secrets`).

## Rollback to native OpenCode

Ordered from least to most disruptive; every step is reversible.

1. Reviews: set `review.free_router.enabled: False`
   (`config_loader.py`): the manual
   `catalog/reviewer_models.yaml` list feeds the pool again,
   entirely (`review_wiring.free_router_settings`).
2. Research: set `RESEARCH_FREE_ROUTER_ENABLED=false`
   (`settings.py`): synthesis falls back to the static model
   (`research_wiring.research_router_settings`).
3. OpenCode configs generated for the gateway roll back with
   `external.strip_gateway_provider` (what T20 applies after
   freezing); the native CLI needs no credential, so a paid route
   fails instead of billing (SPEC §13.3).
4. Security alert: `external.apply_security_rollback` freezes new
   gateway generations (`gateway.frozen`, `GatewayFrozenError`
   until `unfreeze_gateway`) and emits a `gateway_security` alert.
5. Scheduled refresh: `--uninstall` (above). The gateway itself
   stays in HOLD (SPEC §14.1, TASK-2026-00298): nothing installs
   or runs LiteLLM from this module.
