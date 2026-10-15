# Finance boundary v1

TASK-2026-00263 moves the ledger to Jardal ERP while Core keeps
interpretation, classification, Decimal policy and explanation. The ERPNext
DocType flows stay intact and default; the ERP contract activates only with
`FINANCE_PROVIDER=jardal_erp` plus the ready authority manifest. No money
moves: `execute_money_movements=false` still holds everywhere.

Supported capabilities (real flows only; the historic §4 map is not claimed):
`finance.register_expense`, `finance.register_income`,
`finance.get_scope_summary`, `finance.get_contribution_10_status`.
`finance.pago_elfec_house` stays on ERPNext, out of this ticket's evidence.

Writes send closed operation DTOs (decimal text amount, verbatim currency,
owner, nature, resolved account and counterpart, legacy cost center,
explicit scope only when migrated, date, concept, remark, stable ref,
effective rate and evidence when the currency differs). No Scope=Cost Center
equivalence is ever asserted: scope travels null until an explicit migration
maps the real data state. USDT and USD stay distinct; without a confirmed
rate nothing is written. Retries reuse the same key; the ERP answers with
operation, entry, state, duplicate flag and refs.

Reads reuse the Core algorithms over mapped dated entries (same totals,
by-account, vouchers, excluded counts and honesty notes). Without entries,
scope degrades to aggregates with an explicit note and the 10% rule reports
`needs_input` with its reason — never a false zero. The 10% rule computes
once, in Core, over dated ERP entries. Reconciliation filters
(currency, account, cutoff) and `as_of` travel on every read; matching
itself lives in the ERP module.

## Verification

Contract checks live in `tests/test_finance_erp_263.py` (DTOs, record
paths/keys/duplicate/timeout-replay/hard-failures, scope over entries and
aggregates, contribution over entries and unknowns, provider gating,
reconciliation filters), plus the untouched ERPNext finance suites and the
mandatory focused checks from AGENTS.md. Mocks and synthetic fixtures only.
The connected test environment (Owner step 2) must confirm the real
finance paths and shapes, one live income/expense round-trip with replay,
and equivalent summaries before any provider switch.
