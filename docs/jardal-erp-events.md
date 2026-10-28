# Event bridge v1

TASK-2026-00262 connects execution and business decisions with Jardal ERP.
Business owns decisions and group progress; Core owns gates and runs. The
bridge operates only with provider `jardal_erp`; the legacy ERPNext inverse
webhook alias was removed in TASK-2026-00267 and its HITL scope
(TASK-2026-00133) stays untouched.

Inbound (`POST /api/integrations/jardal-erp/events`, secret
`JARDAL_ERP_WEBHOOK_SECRET`, 409 unless provider is jardal_erp) accepts a
closed type set: `approval.decision`, `task.status_changed`,
`execution.requested`, `task.progress_changed`. The contract envelope carries
event id, schema version, origin, workspace, aggregate id and version,
timestamps, correlation/causation and actor; unknown types report ignored,
structural faults reject, origin/workspace faults deny. A durable inbox keyed
by event id plus a per-aggregate cursor make double delivery, reversed order,
concurrent races (claim) and restarts converge: duplicates and stale versions
never re-apply an effect.

Decisions route through `apply_owner_decision`, so answering from ERP equals
answering from the cockpit (approve resumes once, deny fails orderly — the
00133 semantics, reused, not duplicated). Decisions bind by referenced
execution, whether Core requested the approval or ERP policy created it;
exact-once holds per decision id across event ids. Expired, superseded and
invalid decisions never resume. A valid decision with no answerable wait
stays pending; redelivery retries it (same event id after a failure, or a
new event id with the same decision id). Core never polls for it.

Business closes go through `apply_business_status` for jardal_erp with a
close gate: open QA, owner question, escalation, human review, blocked run or
open `blocked_by` records the business state and enqueues visibility without
forcing the execution. Adoption honors the 00156 owner order: only an
explicit `execution.requested` assigned to `JARDAL_ERP_OPENCEO_USER` adopts,
idempotent by order id, without creating a duplicate business ticket;
human or unassigned work never adopts, without exception.

Outbound, every successful Jardal status push also publishes one execution
event (`accepted/started/paused/finished/human_needed` plus evidence
references) to `POST /api/v1/integrations/openceo/events` with the entry id
as key. Entries persist pending before delivery and move to sent, retry or
failed; a bounded startup drain recovers crash leftovers and
`replay_erp_outbox_entry` replays one entry by id. No scheduler and no
ticket sweep. Approvals Core requests travel through `JardalERPApproval`
and are recorded per task so only their decisions apply.

Rollup and Gantt stay Core-owned (`ROLLUP_PROVIDER=core`): group progress
from ERP is recorded as observed and never overwrites the Core rollup. The
switch to `erp` requires step 2 to verify the ERP calculation first.

## Verification

Contract checks live in `tests/test_erp_events_262.py` (envelope, inbox
dedup/stale/claim, decision exact-once/expiry/unknown, adoption rule, close
gates, progress guard, outbox drain/retry/replay, approvals port, provider
gating), plus the mandatory focused checks from AGENTS.md. Mocks and
synthetic fixtures only. The connected test environment (Owner step 2) must
confirm the real approvals/events shapes, one live approval resuming once,
live adoption, and the ERP Gantt calculation before any rollup switch.
