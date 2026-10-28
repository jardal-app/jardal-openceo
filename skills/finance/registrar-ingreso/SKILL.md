---
name: registrar-ingreso
description: "Records already-collected income as an idempotent Jardal ERP Journal Entry. Use when the ticket asks to record an ingreso with its amount."
status: activa
ticket: TASK-2026-00221
date: 2026-10-13
inputs:
  - dictated_text
  - exchange_rate
outputs:
  - journal_entry
evidence:
  - journal_entry
---

# Registrar ingreso

Exact when the ticket brings the data: the `workflow.yaml` runs
`finance.register_income` with the ticket text, the LLM is never
invoked. Mirror of registrar-gasto with the entry reversed (debit
`Efectivo - J`, credit the income account). Collected money only —
no invoices, no fund moves.

## Inputs (ticket title, description, params)

- Dictated text with concept, amount and currency
  (`registra ingreso sueldo 6200 bolivianos`).
- Optional `exchange_rate` when the currency is not BOB.

## Steps

1. With data, the exact run handles it: idempotent `Journal Entry`
   against the real Jardal income accounts, never invented ones.
2. Without a usable amount, end `bloqueado` naming what is missing.
3. Income needs a real collection signal (cobro). Bare `pagaron` and
   nouns with complements (`honorarios del contador`) are expenses,
   not income. Questions never book.

## Verify

- The tool answer is `ok` with its journal entry.
- A retry with the same ref returns the same entry, never a second one.

## Errors

- `no_amount`: report it and ask for the amount.
- `missing_exchange_rate`: report it and ask for the real rate.
- MCP failures: report which tool failed and its error.
