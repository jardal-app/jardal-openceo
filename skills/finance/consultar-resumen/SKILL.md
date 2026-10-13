---
name: consultar-resumen
description: "Reads balance and spending by scope from GL Entry without writing anything. Use when the ticket asks cuánto se gastó, el saldo, or a resumen."
status: activa
ticket: TASK-2026-00221
date: 2026-10-13
---

# Consultar resumen

Read-only: `finance.get_scope_summary` over `GL Entry` through the
dedicated reader. Never creates or edits documents.

## Inputs (ticket title, description, params)

- Optional `date_from` / `date_to` (explicit bounds win).
- Else dictated text with a period (`cuánto gastamos este mes`):
  bounds derive from the words (hoy, ayer, esta semana, este mes,
  el mes pasado, este año). Without period words the read is
  historic and the answer says so.

## Steps

1. With explicit bounds, the exact run reads that range.
2. With dictated text, the exact run derives the bounds first.
3. Report with cutoff date, Company, currency, Cost Center,
   measured period and applied filters. Warn when rows fell out
   by currency or the row limit made the total partial.

## Verify

- The answer names the measured period, never a silent historic total.

## Errors

- ERPNext unreachable: report it, never invent figures.
- MCP failures: report which tool failed and its error.
