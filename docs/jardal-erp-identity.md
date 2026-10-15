# Identity federation v1

TASK-2026-00261 moves the human directory to Jardal ERP without changing the
running authority. Local Mongo directory stays the default provider; ERP
federation activates only with full ERP service config plus the ready authority
manifest produced by TASK-2026-00266. No failure mode leaves Core APIs
unauthenticated, and no ERP role becomes a host or personal grant.

`auth/erp_identity.py` implements the `IdentityDelegation` port over the v1 wire
from TASK-2026-00260 (service Bearer, `X-Workspace-Id`, `Idempotency-Key` on
effects). Human login delegates credentials to `POST /api/v1/auth/login`
(anonymously); every request revalidates the user session against
`GET /api/v1/auth/me`. Revoked, expired, deactivated or mismatched sessions are
denied; an unreachable ERP fails closed with 503. Core never stores ERP
passwords and keeps no hash for federated users.

Mapping is stable and fail-closed: ERP user id links to one Core user id in
Mongo `identity_map`, preserving authorship (`involved_user_ids`, history).
Permission mapping follows the SECURITY.md table; unknown ERP capabilities
grant nothing. Enterprise admin never sets Core `is_admin`; host endpoints
(`require_admin`: tmux, dev sessions, voice) stay local-only until the cutover
defines technical grants. Clients receive only `read_tickets` + `client_review`
scoped to granted projects.

While federated, local directory writes (`/api/users`, `/api/groups`,
`/api/auth/register`) return 409 directing to the ERP directory; reads stay for
audit and compatibility. Local users and seeding go dormant, never deleted:
`export_identity_map(db, identities)` takes an explicit bounded list, verifies
each write by reread, rejects personal records and conflicting links, and never
discovers users by sweeping. A federated login whose email already exists
locally without an explicit link is rejected with 409 until the link is
registered, so migrated humans keep one Core id and authorship. Removing local
CRUD, seeding and collections belongs to the cutover (TASK-2026-00266), not to
this ticket.

## Restore plan

- Revert: set `IDENTITY_PROVIDER=local` and restart canonically. Dormant local
  users resume with last-mapped permissions; no data is deleted by federation.
- ERP outage while federated: requests fail closed (503/401), never as admin.
  Local logout always clears cookies even if remote revocation fails; the ERP
  session then expires naturally.
- Startup refuses `IDENTITY_PROVIDER=jardal_erp` without complete config and a
  ready manifest, before recovery, workers or schedulers run.

## Verification

Contract checks live in `tests/test_identity_erp.py` (delegation, mapping,
revocation, host/personal/project negatives, no-admin-fallback, export), plus
the mandatory focused checks from AGENTS.md. Mocks and synthetic fixtures only.
The connected test environment (Owner step 2) must confirm the real ERP `/me`
shape, login from web/mobile against the ERP directory, and revocation
mid-session; any wire mismatch found there is recorded for the ERP session.
