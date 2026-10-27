# Refresh and ranking (TASK-2026-00292)

## Scheduling: one user-crontab source

No catalog/model scheduling existed on the host (BASELINE §2), so the
refresh owns a single marked block in the user crontab (SPEC §12.1
times, host-local; confirm the zone with `timedatectl` at activation):

- Cron B `20 */6 * * *`: `free-router update --providers`
- Cron A `15 2 * * *`: `free-router update --benchmarks`

Mutual exclusion lives in-process: each command takes a non-blocking
`flock` on `update.lock` inside `run_update` (an outer `flock -n`
wrapper on the same file would self-lock every scheduled run, since the
child opens an independent file description). Both lines append to
`updater.log`. Reinstalling replaces the block instead of duplicating
it. Dry-run is the default:

    ./.venv/bin/python openceo-core/model_routing/schedule.py --dry-run
    ./.venv/bin/python openceo-core/model_routing/schedule.py --install
    ./.venv/bin/python openceo-core/model_routing/schedule.py --uninstall

Activation on the host belongs to TASK-2026-00299, not to this ticket.

## What each refresh does

`update --providers`: fetch the five provider catalogs, keep the last
good deployments for dead sources (stale, never empty), resolve
families, unify canonical models across providers, carry the previous
benchmark scores forward for the same canonical models (provider feeds
have no quality data), apply catalog verdicts, then publish
`catalog.json` and `rankings.json` atomically with history snapshots. A
failure before publication keeps the previous snapshots byte-identical
(T12). A providers-only refresh over a catalog without benchmarks ranks
nothing: eligible models wait in `experimental` until Cron A scores
them.

`update --benchmarks`: needs a published catalog first. Fetches every
Artificial Analysis page plus the LMArena splits/rows snapshot,
rebuilds quality, attaches it to the catalog, then publishes both.
Refuses to mix AA index majors (fail-closed, previous kept). One dead
benchmark source degrades to the surviving one; both dead aborts.

## Frozen snapshots and top-3

Consumers (TASK-2026-00293+) load `rankings.json` once per round and
hold the object: publication never mutates a loaded document, so a
refresh mid-round cannot swap models under a running round (T12). Every
profile order carries its catalog snapshot id, sha256, sources, applied
weights/floors, and benchmark versions (T19).

A changed top-3 stays pending until the same challenger repeats on the
next refresh (the reproducible local evaluation of SPEC §8.3); the
previous top-3 keeps serving meanwhile. Entries that left the eligible
set drop from retention with a recorded reason.

## Gates, weights and floors

Hard gates run BEFORE ordering: `free_verified`, `healthy`, tools,
context, public confidentiality, and known TPM/RPM/RPD capacity (floors
unset until §5 limit sources are wired; unknown then fails closed).
Scores are SPEC §8.3 weights over 0..1 percentiles times coverage;
floors are the §18 minima (review 0.65, research 0.60, general 0.45);
drops are an initial hypothesis (0.15/0.20/0.25). Research prefers
128K context and falls back to 64K when nothing qualifies, recording
the applied floor. Policy `profiles:` overrides merge per field.

## Native serve evidence (TASK-2026-00315)

OpenCode Free only serves from the native CLI, so a group A
deployment verifies only with fresh serve evidence: proof the model
served natively at zero cost within the last 7 days
(`SERVE_EVIDENCE_TTL` in `sources.py`).

Producer (`model_routing/serve_evidence.py`, state file
`~/.local/state/free-router/serve-evidence.json`,
`{model_id: iso_timestamp}`): every native zero-cost serve records
one entry, best-effort, never raising. Hooks: `probes.run_native_probe`
on success (cost already verified zero), `review_wiring.record_receipt`
for native-route receipts, `research_wiring` managed/static turns on
completion. Cron B loads the file in `_fetch_providers` and feeds it
to `refresh_source`; a missing or corrupt file means no evidence,
never an error. Stale entries are pruned on write; unknown ids are
kept but ignored (only listed models verify).

Operator: `free-router serve-evidence --model <bare-id>` records one
id manually; bare `free-router serve-evidence` (or `--json`) lists
entries with freshness. Offline test runs may leave synthetic ids in
a dev state file; they never verify because they are not offered.

## Known limits

- If `rankings.json` is missing or corrupt, the next publication
  restarts benchmark provenance (`aa_index_version`, `lmarena_target`)
  from the incoming refresh; per-score sources in the catalog still
  record where each dimension came from.
- `tools` comes from provider-declared metadata (only OpenRouter
  publishes it) or probes; other providers stay out of review/research
  until probed. Declared support gates the ranking; probes verify
  reliability. Catalog `healthy` means listed-now plus verified-free;
  transient serving state lives in the availability store.
- Rankings assume public content: every manual terms record allows only
  `public` today, so routing non-public content to free tiers stays
  forbidden at the consumer layer.
- The live refresh needs a fresh daily budget (providers ~6 OR + 1 per
  other; benchmarks ~4 AA + 2 HF calls); with today's ledger exhausted
  the live tests skip until the UTC rollover.
