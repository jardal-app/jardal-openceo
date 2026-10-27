# Router acceptance T01–T20 + addendum §11 (TASK-2026-00297, TASK-2026-00309)

Battery over simulated providers, injected faults and the live
fixtures the epic recorded. No network leaves loopback; no model
is called. Regenerate with the battery:

```
PYTHONPATH=openceo-core ./.venv/bin/python -c "from pathlib import Path; from model_routing.acceptance import ACCEPTANCE_REPORT_PATH, generate_report_text; import tempfile; text = generate_report_text(Path(tempfile.mkdtemp())); ACCEPTANCE_REPORT_PATH.write_text(text)"
```

## Scenarios T01–T20

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
| T11 | only paid routes leave the usable set; unknown-price stays usable | unknown-price usable, paid refused; 17 recorded :free replay clean | pass |
| T12 | failed refresh keeps the previous snapshot and the round | catalog byte-identical; frozen round untouched | pass |
| T13 | no duplicated tool call, no false success | t1 executed once; checkpoint rebuilds the turn | pass |
| T14 | clear error, native mechanisms still usable | free-review native via opencode/alpha-free with notice | pass |
| T15 | content logging off, secrets never persist | excerpt redacted; registry scan finds no key-like pattern | pass |
| T16 | broken tool-calling leaves MCP/review profiles | epsilon excluded; served opencode/alpha-free | pass |
| T17 | two valid reviews continue on distinct families | pair fam-alpha+fam-beta; same model twice = two failover entries | pass |
| T18 | verified reports survive restarts byte-identical | valid receipt reuses; tampered bytes refuse | pass |
| T19 | sources, date, snapshot and motive recorded | snapshot valid with aa 4.3 + agent/latest | pass |
| T20 | freeze new deployments, roll back to native | freeze holds; generation refuses; alert recorded | pass |

## Addendum §11 scenarios

| ID | Expected | Observed | Result |
|---|---|---|---|
| A01 | no native route in the gateway; external 403 never retries | gateway serves 4 external rows, 0 native; 403→route_not_authorized no-retry | pass |
| A02 | free-research opens on the best native model | free-research serves opencode/alpha-free (group A rank 1) | pass |
| A03 | spent native falls to the next native alone | 429 on opencode/alpha-free cooled; served opencode/beta-free | pass |
| A04 | no native left crosses to a configured external | opencode blocked by the recorded quota; crossed to openrouter/acme/gamma:free via its configured provider | pass |
| A05 | incompatible context, tools or privacy never serve | good served; small context, no-tools and private dropped with reasons | pass |
| A06 | simultaneous failure keeps distinct families | both labels failed together; gamma+delta serve, a third label refused the held family | pass |
| A07 | failed reviewer restarts whole on the same snapshot | label2 rotates to gamma on the same snapshot; label1 receipt sha intact | pass |
| A08 | history survives switches; tools run once | 3 turns, 1 rotation, checkpoint turn=3, cut stream re-claims call_sky without re-execution | pass |
| A09 | launcher failure observed and routed next turn | launcher session failure observed; next turn routed alpha→beta | pass |
| A10 | only paid routes never serve; unverified serves in ranking order | paid dropped; unverified serves in ranking order | pass |
| A11 | router off returns to native with files intact | router off in both pools; gateway stripped and frozen; 3 files intact; native alpha servable | pass |
| A12 | audit keeps model data; prompts stay out | switch+receipt audit keeps model/family/provider/reason/result; canary, secrets and key-like absent | pass |

## SPEC §16 criteria

| Criterion | Observed | Threshold | Result |
|---|---|---|---|
| paid invocations | 0 | 0 | pass |
| receipts without effective model | 0 | 0 | pass |
| family collisions in strict rounds | 0 | 0 | pass |
| valid published snapshots | 2/2 | 100% | pass |
| quota errors classified | 4/4 | 100% | pass |
| pre-response recovery with a healthy alternative | 7/7 (100%) | ≥95% | pass |
| tool repeats without a check | 0 | 0 | pass |

## Replay of recorded live responses

- sources-401-groq replays as auth
- availability-not-found-openrouter replays as not_found
- research-session-live replays 5 turns with one rotation
- opencode-free-quota-429 replays as daily_quota
- native-instance-catalog replays 81/82 models overlapping
- selector-openrouter-chat replays with credits 0.0 → 0.0
- review-group-b-live replays an external receipt, credits unchanged
- research-managed-session-live replays 3 turns, effective == session model, cost 0
- tui-launcher-session-live replays cost 0 with a watermark

## Real provider calls spent by the epic (2026-10-23–2026-10-25)

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
| TASK-2026-00304 | 3 | 1 native probe + 2x 403 pin, no quota spent |
| TASK-2026-00305 | 3 | 1 external chat + 2 key reads, credits unchanged |
| TASK-2026-00306 | 7 | 1 review + 4 diagnosis reviews + 2 key reads |
| TASK-2026-00307 | 3 | 3 managed turns with one injected 429 |
| TASK-2026-00308 | 3 | 1 unplanned + 1 quota-hit + 1 rerun turn |
| TASK-2026-00309 | 2 | key before/after the battery, no generation |
| **total** | **116** | |

## Provenance

- catalog schema 2, rankings schema 1
- recorded fixtures digest 1d6c1bad657be7e4
- battery GREEN
