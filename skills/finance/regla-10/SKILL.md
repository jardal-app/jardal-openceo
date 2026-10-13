---
name: regla-10
description: "Measures the 10% contribution rule from income accounts without writing anything. Use when the ticket asks about el diezmo, el 10%, or el aporte."
status: activa
ticket: TASK-2026-00221
date: 2026-10-13
---

# Regla 10

Read-only: `finance.get_contribution_10_status` over `GL Entry`.
Eligible income = credits to income accounts in the period;
target = 10%; deterministic `Decimal` arithmetic.

## Inputs (ticket title, description, params)

- The question (`cómo vamos con el diezmo`).
- Optional `date_from` / `date_to` / `cost_center`. Without explicit
  bounds the dictated period wins (`el mes pasado`).

## Steps

1. The exact run measures the rule; no accounts are created.
2. Say "el libro registra ... en cuentas de ingreso", never
   "cobramos": an accrual counts like a collection.
3. Jardal has no contribution account: contributed money reads
   unknown (`needs_input`), never 0. Defining that account is the
   Owner's decision.

## Verify

- The answer carries the measured period and the 10% target.

## Errors

- ERPNext unreachable: report it, never invent figures.
- MCP failures: report which tool failed and its error.
