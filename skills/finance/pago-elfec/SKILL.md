---
name: pago-elfec
description: "Runs the Elfec House payment practice flow to the card screen without paying, under the Owner lock. Use when the ticket asks to pay Elfec luz casa."
status: activa
ticket: TASK-2026-00221
date: 2026-10-13
inputs:
  - expected_row_label
  - stop_before_card
outputs:
  - practice_result
  - approval_question
---

# Pago Elfec

Practice only: drives the portal to the card form and stops, never
pays. The run passes the Owner lock inside the tool
(`requires_owner`): the tool answers pending and the agent ends
`bloqueado` until the Owner answers; the approval resumes the run.

## Inputs (ticket title, description, params)

- The House row (`expected_row_label: House`, natural alias
  Pago Luz Casa).
- Practice mode is fixed (`stop_before_card: true`).

## Steps

1. The exact run calls `finance.pago_elfec_house` in practice mode.
2. On a lock park, end `bloqueado` with the question id — the
   approval resumes this run and the stored outcome replays.
3. Real payment needs a new explicit Owner decision; this skill
   never confirms a payment.

## Verify

- The run ends in practice mode, or parked for approval with its
  question id.

## Errors

- Denied approval: end `bloqueado` with the denial reason.
- MCP failures: report which tool failed and its error.
