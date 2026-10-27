"""Company-level Objectives (autonomy epic TASK-2026-00110, F2 TASK-2026-00117).

One Objective type for every domain. Identity lives in Jardal ERP (a group Task
with real children); this package owns execution only: the engine
(:mod:`objectives.engine`), the planner contract
(:mod:`objectives.planner_models`) and the company-level wiring
(:mod:`objectives.service`).

Moved out of the Tier S engine package (TASK-2026-00117): dev flows (Tier S
cycles, simple tiers) consume the engine, they no longer contain it. The dev
final review still uses the shared reviewer pool; non-dev objectives close
through deterministic delivery verification. The review machinery both
engines share moved to :mod:`orchestration` (TASK-2026-00141).
"""

from __future__ import annotations
