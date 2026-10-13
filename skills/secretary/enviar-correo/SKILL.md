---
name: enviar-correo
description: "Drafts and sends email through the locked boundary tool (off-allowlist recipients park for Owner approval). Use when the ticket asks to send or draft an email."
status: activa
ticket: TASK-2026-00220
date: 2026-10-13
---

# Enviar correo

Drafting is guided here; sending always goes through the
`openceo_send_email` boundary tool, which holds the Owner lock:
off-allowlist recipients park the run as `bloqueado` until the Owner
answers, and the approval resumes the ticket. Never send through SMTP
directly, and never keep notes anywhere else: company follow-ups go to
the ticket as comments.

## Inputs (ticket title, description, params)

- `to` (required), `subject` (required), and either `body_text`/`body`
  or a draft brief (`about`, `tone`, key points).
- `from_email` (default: the configured sender), `cc`, `bcc`,
  `reply_to`, `attachments`, `language` (`es`/`en`) when the ticket
  names them.

## Steps

1. Read the ticket (title, description, params) and its attachments.
   Company context comes from the ticket, never from memory.
2. Pick the sender: the ticket's `from_email` when present, else the
   default. Valid senders are listed in the `openceo_send_email` tool
   description — use one exactly, never invent an address. Match the
   language variant the ticket asks for (`ventas@` for `es`, `sales@`
   for `en`, and so on).
3. Draft the body from the ticket: keep the ticket's wording for
   facts, a short subject, and leave the automatic institutional
   signature untouched. Missing `to`, `subject`, or body with no
   brief to draft from: end `bloqueado` naming what is missing —
   never guess recipients.
4. Send with `openceo_send_email` (`to`, `subject`, `body_text`, plus
   `from_email`, `cc`, `bcc`, `reply_to`, `attachments`, `language`,
   and the ticket `ticket`/`project` for the badge binding).
5. Report `hecho` with from/to/subject; on a lock park, end
   `bloqueado` with the question id — the approval resumes this run.

## Verify

- The send answer is `ok`, or a lock park with its question id.
- The ticket carries the outcome (the run publishes it).

## Errors

- Unknown sender or a refused send: report the tool error verbatim.
- Denied approval: end `bloqueado` with the denial reason.
- MCP failures: report which tool failed and its error.
