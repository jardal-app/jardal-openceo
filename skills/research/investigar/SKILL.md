---
name: investigar
description: "Runs a grounded investigation through the ticket research bridge (submit, poll, attach the brief). Use when the ticket asks a research question."
status: activa
ticket: TASK-2026-00222
date: 2026-10-13
inputs:
  - question
  - purpose
  - mode
  - seed_urls
  - idempotency_key
  - ticket
  - project
outputs:
  - research_brief
  - research_id
evidence:
  - research_id
  - brief_path
---

# Investigar

Questions are answered here; the investigation itself runs through
the `openceo_research_submit` / `openceo_research_get` boundary tools,
which act under this ticket's consumer. Never call the research
service or its providers directly: their credentials stay server-side.
The finished brief is attached to the ticket and the run returns to
the planner with `hecho`.

## Inputs (ticket title, description, params)

- `question` (required): what to answer. `purpose` (required): the
  decision this research unlocks. `mode` (`quick` unless the ticket
  says otherwise), `seed_urls` (known sources), `idempotency_key`
  (reuse on retries of the same request).
- Always pass this ticket's `ticket` and `project`: the badge binds
  the investigation to them.

## Steps

1. Read the ticket (title, description, params) and the attachment
   links it carries; the ticket text carries the question. A vague
   question with no decidable purpose: end `bloqueado` naming what
   is missing — never guess the question.
2. Admit with `openceo_research_submit` (`purpose`, `question`,
   plus `mode`, `seed_urls`, `idempotency_key` when the ticket
   names them). Keep the returned `research_id`.
3. Poll with `openceo_research_get` until the job is terminal.
   A `not_found` for an id this run admitted: end `bloqueado`
   with the tool error verbatim.
4. Write the brief from the returned evidence only: verified
   facts, vendor claims, conflicts, uncertainties, and source
   references. Mark anything unverified as such.
5. Attach the brief with `openceo_attach_ticket_file` (private)
   and end `hecho` naming the attachment.
6. The question is outside research (a build, a payment, an
   approval): end `no_aplica` with the reason — the planner
   reassigns.

## Verify

- The brief cites only sources the job returned.
- The ticket carries the brief as an attachment.
- The run publishes `hecho`, `bloqueado`, or `no_aplica`.

## Errors

- `unavailable` (service off) or `forbidden` (project without
  research): end `bloqueado` naming which one.
- `validation_error` or `conflict`: report the tool error verbatim.
- MCP failures: report which tool failed and its error.
