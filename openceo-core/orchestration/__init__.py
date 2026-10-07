"""Shared orchestration primitives (autonomy epic TASK-2026-00110, F2 TASK-2026-00141).

One-shot process execution (:mod:`orchestration.live`), the reviewer pool and
review rounds (:mod:`orchestration.review`), review findings
(:mod:`orchestration.findings`), the findings inbox (:mod:`orchestration.inbox`)
and the repo knowledge notes (:mod:`orchestration.knowledge`) live here so both
engines — the Tier S ticket cycle and the company-level objectives engine
(:mod:`objectives`) — import them from one home instead of objectives
reaching into Tier S internals.

Moved here from the Tier S engine (TASK-2026-00141), which keeps only the
single-ticket cycle (cycle, plan, gates, models, stores, telemetry). This
package never imports back into it.
"""

from __future__ import annotations
