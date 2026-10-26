# External interfaces (TASK-2026-00295)

Contract for consumers outside OpenCEO: `automatico.py` of other repos
and interactive OpenCode. Everything here works without importing
OpenCEO code: a versioned JSON snapshot plus four CLI commands over the
shared store (TASK-2026-00291). The gateway stays in security HOLD
(SPEC §14.1): the generator emits a logical LiteLLM contract for review
only, never a deployment, and only for external providers — native
`opencode` routes are never gateway deployments (addendum v1.2 §12,
TASK-2026-00304). While the gateway is on HOLD, group A serves natively
and group B serves through its provider configured in OpenCode
(addendum v1.2 §3.4, TASK-2026-00305).

## Consumer snapshot

`export_consumer_snapshot` publishes the contract (default readers use
their own copy; the file is plain JSON, `schema_version: 2`):

- `generated_at`, `generator`, `rankings_sha256`, `catalog_snapshot`:
  provenance of the frozen ranking this snapshot describes.
  `rankings_sha256` hashes the whole rankings document, so it moves
  with every republication; `eligible_sha256` is the semantic reload signal.
- `gateway`: `base_url` (default `http://127.0.0.1:4000/v1`), `backend`
  (`litellm`), `eligible_sha256` (reload only when it moves), `frozen`
  (T20: no new gateway deployments, serve native).
- `profiles.<name>.routes`: ranking order flattened to one row per
  route: `rank`, `canonical_model_id`, `family_id`, `deployment_id`,
  `provider`, `route_type`, `allowed_client`, `provider_id`, `model_id`
  (v2, addendum §6), `gateway_alias` (profile alias, or
  `review-candidate-NNN` inside `free-review`; null for native-only
  routes), `native_cli_model` (the `opencode run -m` id, set for
  `opencode` provider routes only).
- `profiles.<name>.native_notice`: what serves natively while the
  gateway is unavailable.

Generation is fail-closed: a ranked route missing from the catalog, no
longer usable, or on an unmapped provider refuses the export instead of
publishing a partial contract. Unknown `schema_version` values refuse
to load.

## Reserve CLI

External coordinators share `availability.sqlite` (family uniqueness
holds across processes, T04):

    free-router reserve --run-id <id> --round <n> --slot <s> \
        --canonical <canonical> --family <family> --deployment <dep>
    free-router release --run-id <id> --round <n> --slot <s>
    free-router cooldowns
    free-router reservations --run-id <id> --round <n>

A taken slot or family exits 1 with the conflict spelled out. All four
take `--state-dir` (tests must point it at a tmp dir) and `--json`.

## OpenCode config

`build_opencode_config(schema)` emits a standalone provider entry named
`free-gateway` with the `free-research`, `free-general` and `free-review`
models, in exactly one grammar (SPEC §13.2, never mixed): `classic`
(`provider`/`npm`/`options`) or `v2` (`providers`/`package`/`settings`).
`detect_opencode_schema` reads the grammar from an existing document and
refuses mixed ones; documents without either key default to `classic`,
the grammar of the installed host (BASELINE §1, opencode v2.0.22).

Install by merging the generated provider into the user config
(`merge_gateway_provider`); roll back to native OpenCode by stripping
it (`strip_gateway_provider`). The generated entry references
`FREE_GATEWAY_KEY` by name only; no credential is ever written.

## Gateway absent (T14) and security freeze (T20)

T14: when the gateway is unavailable, sessions report the failure in
plain words and keep serving per policy without it: native group A
routes plus group B routes through their provider configured in
OpenCode; paid routes are never touched as a fallback.
`plan_consumer_routes` encodes this: `gateway_available=False` yields
native `opencode run -m` commands with a notice naming what stays
blocked and what group B needs installed.

T20: on a gateway security advisory, `apply_security_rollback` freezes
new deployments (`gateway.frozen` in the config dir, with reason and
timestamp) and rolls the OpenCode config back to native-only. While the
marker stands, gateway generation refuses (`GatewayFrozenError`); the
snapshot keeps describing the frozen state with `frozen: true`.
`unfreeze_gateway` lifts the marker after the advisory clears.
