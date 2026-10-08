"""Autonomous company CEO (autonomy epic TASK-2026-00110, F4 TASK-2026-00119).

The CEO reviews company work on the mandate cadence plus company events,
and decides — through one Muse headless run per review over bounded
compiled state — whether to keep going, create an Objetivo, create a
loose ticket, create a project, escalate, or do nothing. Doing nothing
is a valid outcome. Creation is direct inside the mandate; outside it
the CEO escalates. Anti-bloat is deterministic and runs before anything
persists. Normative contract: ``docs/ARCHITECTURE_REFERENCE_2026.md``
§Proactive CEO.
"""
