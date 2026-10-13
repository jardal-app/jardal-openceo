---
name: registrar-gasto
description: "Records an occasional expense already incurred as an idempotent ERPNext Journal Entry. Use when the ticket asks to record a gasto with its amount."
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

# Registrar gasto

Exact when the ticket brings the data: the `workflow.yaml` runs
`finance.register_expense` with the ticket text, the LLM is never
invoked. Booked money only — this never moves funds
(`execute_money_movements=false`).

## Inputs (ticket title, description, params)

- Dictated text with concept, amount and currency
  (`registra gasto personal carne 50 bolivianos`).
- Optional `exchange_rate` (real rate of the operation) when the
  currency is not BOB.

## Steps

1. With data, the exact run handles it: idempotent `Journal Entry`
   (debit expense, credit `Efectivo - J`), `Decimal`, Company
   `Jardal`, Cost Center `Principal - J`.
2. Without a usable amount, end `bloqueado` naming what is missing —
   never guess amounts.
3. Foreign currency without a real rate: end `bloqueado` asking for
   the rate. Never book USD/USDT figures as BOB.

## Verify

- The tool answer is `ok` with its journal entry, or a lock park
  with its question id.
- A retry with the same ref returns the same entry, never a second one.

## Errors

- `no_amount`: report it and ask for the amount.
- `missing_exchange_rate`: report it and ask for the real rate.
- MCP failures: report which tool failed and its error.
