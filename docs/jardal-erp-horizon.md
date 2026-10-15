# Horizon boundary v1

TASK-2026-00264 moves the personal product subset to the private Jardal ERP
domain while Core keeps classification, capacity, the deterministic planner,
execution triggers, conversation and delegation. Mongo stays the default
store; the ERP store activates only with `HORIZON_STORE=jardal_erp` plus
the ready authority manifest and federated identity, since every personal
call carries the user's session token — never the service token, and a
missing token denies closed.

Split by collection, one writer per stage: objectives (+milestones) and
captures live in the ERP; profile, actions, events, plans and triggers stay
in Mongo as execution state, together with every company reader (the
company stays visible by permission, unchanged). `ERPHorizonStore` speaks
the repo's own method names, so the service and the importer run against
either backend; in ERP mode the calls require the request's user session
(authenticated API), and a call for another user is denied. Background
work without a session (triggers, proactive cycle) reads the Mongo
snapshot with a loud stale warning and refuses ERP-subset writes, so the
cycle survives without split brain; user-session delegation for background
awaits the ERP session design.

Migration is explicit per user, never a sweep: export the subset, import it
idempotent by external ids, verify by re-read. No collection is retired
here; that belongs to the cutover. Promotion still travels spec-only
through the common API; personal scope never publishes business events
(the observer excludes it and the publisher refuses it as defense in
depth), and decisions on personal executions keep failing closed.

## Verification

Contract checks live in `tests/test_horizon_erp_264.py` (user-token
calls, store mapping and delegation, gate, idempotent migration with
verify, no-leak guards), plus the untouched Horizon suites and the
mandatory focused checks from AGENTS.md. Synthetic fixtures only, no real
personal data. The connected test environment (Owner step 2) must confirm
the real horizon paths and shapes, one live migration with verify, and
no-leak across API, events, notifications and search before any store
switch. Full absorption of actions, events, plans and triggers awaits the
ERP domain extension in an ERP session.
