# UI boundary v1

TASK-2026-00265 prepares the OpenCEO side for the ERP-owned UI and mobile
client. People UIs (tickets, inbox, reviews, Horizon, admin) and the mobile
client move to the ERP app; the voice server, LiveKit/STT/TTS/first_layer
and the execution APIs stay in OpenCEO. Nothing is retired here: builds,
routes and deploys stay until step 2 proves parity.

Compatibility surface: the Core endpoints the ERP UI may rely on during
the transition (auth, tasks, groups, users, projects, horizon reads and
writes, upload), frozen in `tests/test_ui_compat_265.py` by app
introspection. No new Core route was added, so the auth inventory is
untouched.

Client auth: cookie session stays primary; non-cookie ERP clients use
`Authorization: Bearer` with a Core access token, or — once federated —
with the ERP user session (introspected, linked users only, no silent
enrollment). The `heaven` placeholder never authenticates anywhere.
`POST /api/auth/login` also exchanges `{erp_token}` for the Core mapping
and real JWTs, honoring the explicit-link rule for clashing emails.

Voice token contract (frozen): `POST /api/voice/token` with
`{user_id, room?, project?, model?}` answers `{token, url}`; `/health`
reports degraded without LiveKit keys. The mobile client keeps its
application id, signature and deep links; the ERP session owns the app,
push and reconnection behavior. The open token endpoint and the token
server CORS posture stay as they are during the transition; hardening is
a follow-up, not this cut.

Retirement gates (not executed): directed tours (permissions,
tickets/hours, client review, Horizon, voice background/reconnect, push),
mobile update with signature, deep links and login verified, then
activate `openceo-react/public/_redirects.erp` (replace `{{ERP_UI}}`) and
only then remove the legacy builds, routes and deploys.

## Verification

Contract checks live in `tests/test_ui_compat_265.py` (frozen surface,
Bearer and exchange auth, bundle with no secrets, voice token shape),
plus the auth suites and the mandatory focused checks from AGENTS.md.
Mocks and synthetic fixtures only. Step 2 runs the live tours against
the ERP UI before any removal.
