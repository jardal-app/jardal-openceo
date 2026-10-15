# Business provider v1

TASK-2026-00260 introduces the HTTP business boundary without changing production authority. ERPNext remains the default provider. Finance, identity, approval events and personal persistence are separate domain consumers; declaring their ports does not implement their integrations.

`business.provider.build_business_work` binds `BusinessWork` to either the existing ERPNext adapter or `business.work.JardalERPWork`. `business.errors` owns error types; ERPNext exports compatibility aliases. MCP capability names and TASK references remain unchanged. No failure selects another provider or creates an execution that may run without its business registration.

## Wire and compatibility

Jardal ERP v1 uses `X-Workspace-Id`, an authenticated bearer credential and `Idempotency-Key` for effects. The service credential is a backend secret, never a browser or Android bundle value. Human federation and service principal issuance are completed by TASK-2026-00261; a synthetic session is used only in the isolated contract smoke.

`business/contracts.py` validates incoming tickets. ERP owns runtime request schemas in `apps/api/src/work-schemas.ts` and TypeScript DTOs in `packages/contracts`. Creation preserves project keys, source references, execution IDs and numbers; it registers `autorun:false`. Business creation is not an execution request. UUID, TASK reference and source ref resolve the same business resource.

Transient transport/5xx failures retry the identical command and key. Access and validation failures are hard failures. A conflicting key with changed business content is rejected. If Core dies after the ERP commit but before Mongo insert, the immutable registration command recovers the original execution identity; changing only a newly allocated execution UUID/number does not duplicate work. If no commit happened, the pending execution stores and explicitly replays the original command.

Transitions carry reason, evidence or the exact human action, plus expected version. Automatic status propagation may reread after an unrelated version change; a changed business state is not overwritten. An unresolved conflict retains the pending intention. Dependencies and open children prevent completion. Execution status and business status retain separate vocabularies.

ISO timestamps are converted to the existing visibility consumer's site time format. Active/closed work projects actual starts, and closed work projects actual ends; original planned timestamps remain separate. Unknown actual dates remain null; the ERPNext repair path does not synthesize facts or overwrite plans for Jardal work. Group rollups compare and write planned spans explicitly, while history uses actual dates. Group progress supports two decimal places. Core retains rollup authority until TASK-2026-00262 verifies the ERP calculation and switches that consumer.

Comments and private attachments use closed operations, version checks and idempotency. Only the author may edit/delete a comment. Binary reads remain authenticated; attachment creation validates names, size and content. ERP stores blobs under its private `FILE_STORAGE_ROOT`; backing up SQL alone cannot recover attachments.

## Link migration and activation

New writes set `params._mirrors.business` with provider, TASK/source reference and business UUID. ERPNext writes also retain its legacy mirror for compatibility. Explicit readers in the dispatcher, objective/mission engine, CEO, Horizon, reporting and MCP badges prefer the canonical link and read legacy links only when necessary. Mongo lookup never matches an obsolete legacy TASK when a canonical link exists.

`business.migration.backfill_business_links(repo, adapter, task_ids)` accepts an explicit bounded list. It verifies source ref, unchanged TASK, project and execution identity, writes the canonical link, rereads each write and preserves the old mirror. It rejects personal records; it never invents business tickets or discovers records by sweeping.

Selecting `BUSINESS_PROVIDER=jardal_erp` requires `JARDAL_ERP_URL`, `JARDAL_ERP_SERVICE_TOKEN`, `JARDAL_ERP_WORKSPACE_ID` and a `BUSINESS_AUTHORITY_FILE`. The private authority manifest must identify schema v1, provider, workspace, origin, Mongo execution database and state `ready`. TASK-2026-00266 must produce it only after migration, reconciliation and recovery gates; the contract smoke does not create one. Startup additionally refuses inconsistent Mongo business links before starting recovery, workers or schedulers.

After activation, the ERPNext webhook rejects even a valid legacy secret. The service callback also checks event source before lookup, runtime mutation or visibility enqueueing. ERPNext remains operational until the actual domain cutover; its code and credentials are not removed by this change.

## Verification

Python contract checks live in `tests/test_business_provider.py`, with targeted legacy/visibility regressions and the mandatory focused checks from AGENTS.md. They use mocks and synthetic fixtures, without models.

`jardal-erp/scripts/openceo-work-smoke.ts` starts an ERP API bound to loopback on a random port and calls `scripts/erp-work-smoke.py` through the repository venv. Supply an explicitly isolated PostgreSQL `DATABASE_URL` ending in `_test` and `OPENCEO_CONTRACT_ROOT` pointing to this checkout. It verifies real HTTP creation/replay/conflict, recovery of source/execution identity, private files, comments, dependency transitions, fractional progress and date compatibility. It never starts a second Core instance. ERP API tests also refuse a database whose name does not end in `_test`.

`projects/jardal-erp/project.yaml` enables technical work in the ERP repository, validates against ConfigLoader, uses the existing owner CLI wrappers and does not create DNS records or deploy services.
