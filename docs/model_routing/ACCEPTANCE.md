# Router acceptance T01–T20 (TASK-2026-00297)

Battery over simulated providers, injected faults and the live
fixtures the epic recorded. No network leaves loopback; no model
is called. Regenerate with the battery:

```
PYTHONPATH=openceo-core ./.venv/bin/python -c "from pathlib import Path; from model_routing.acceptance import ACCEPTANCE_REPORT_PATH, generate_report_text; import tempfile; text = generate_report_text(Path(tempfile.mkdtemp())); ACCEPTANCE_REPORT_PATH.write_text(text)"
```

## Scenarios

| ID | Expected | Observed | Result |
|---|---|---|---|
| T01 | 429 skips to the next eligible deployment, no paid model | 429 on opencode/alpha-free cooled; served opencode/beta-free | pass |
| T02 | global quota blocks one provider, others continue | openrouter blocked; 7 deployments eligible | pass |
| T03 | failover only past TPM/context/privacy gates | groq tpm=30000 serves; nvidia tpm unknown blocks | pass |
| T04 | no shared family without a recorded exception | duplicate family refused; recorded exception holds slot-c | pass |
| T05 | same base model counts once | excluding example/alpha skips both its routes | pass |
| T06 | only the failed reviewer restarts | free-review-1 rotates to opencode/gamma-free; beta sha intact | pass |
| T07 | block or explicit quorum, never faked independence | NoReviewPair raised; nothing fabricated | pass |
| T08 | round 2 reviews the corrected state | r2 serves opencode/alpha-free on c0ffee2, not c0ffee | pass |
| T09 | final verification before next | second failure exhausts the chain with an audit event | pass |
| T10 | fallback follows live priorities | down example/alpha serves example/beta twice alike | pass |
| T11 | unfree or unknown-price models leave the usable set | unknown/paid refused; 17 recorded :free replay clean | pass |
| T12 | failed refresh keeps the previous snapshot and the round | catalog byte-identical; frozen round untouched | pass |
| T13 | no duplicated tool call, no false success | t1 executed once; checkpoint rebuilds the turn | pass |
| T14 | clear error, native mechanisms still usable | free-review native via opencode/alpha-free with notice | pass |
| T15 | content logging off, secrets never persist | excerpt redacted; registry scan finds no key-like pattern | pass |
| T16 | broken tool-calling leaves MCP/review profiles | epsilon excluded; served opencode/alpha-free | pass |
| T17 | two valid reviews continue on distinct families | pair fam-alpha+fam-beta; unknown dropped | pass |
| T18 | verified reports survive restarts byte-identical | valid receipt reuses; tampered bytes refuse | pass |
| T19 | sources, date, snapshot and motive recorded | snapshot valid with aa 4.3 + agent/latest | pass |
| T20 | freeze new deployments, roll back to native | freeze holds; generation refuses; alert recorded | pass |

## SPEC §16 criteria

| Criterion | Observed | Threshold | Result |
|---|---|---|---|
| paid invocations | 0 | 0 | pass |
| receipts without effective model | 0 | 0 | pass |
| family collisions in strict rounds | 0 | 0 | pass |
| valid published snapshots | 2/2 | 100% | pass |
| quota errors classified | 2/2 | 100% | pass |
| pre-response recovery with a healthy alternative | 5/5 (100%) | ≥95% | pass |
| tool repeats without a check | 0 | 0 | pass |

## Replay of recorded live responses

- sources-401-groq replays as auth
- availability-not-found-openrouter replays as not_found
- research-session-live replays 5 turns with one rotation

## Real provider calls spent by the epic (2026-10-23)

Every call free, minimal and synthetic; generation only where the
ticket acceptance required it. Sources: the builder comments on
each ticket.

| Ticket | Calls | Notes |
|---|---|---|
| TASK-2026-00285 | 0 | F0 without tokens |
| TASK-2026-00287 | 16 | 8 live probes + 8 diagnosed metadata calls |
| TASK-2026-00288 | 0 | no live tests |
| TASK-2026-00289 | 34 | catalogs + chat/tool probes per provider |
| TASK-2026-00290 | 3 | 1 Artificial Analysis + 2 Hugging Face |
| TASK-2026-00291 | 9 | 8 key reads + 1 failover chat |
| TASK-2026-00292 | 17 | metadata-only provider + benchmark updates |
| TASK-2026-00293 | 1 | single fed review under the fence |
| TASK-2026-00294 | 7 | 5 session turns + 2 key reads |
| TASK-2026-00295 | 6 | minimal native OpenCode calls |
| TASK-2026-00297 | 2 | key before/after the battery, no generation |
| **total** | **95** | |

## Provenance

- catalog schema 2, rankings schema 1
- recorded fixtures digest 06e84a1c6fea5b95
- battery GREEN
