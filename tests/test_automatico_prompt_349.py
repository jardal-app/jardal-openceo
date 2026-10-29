"""El prompt worker debe distinguir READY de CONTINUE (bucle TASK-2026-00349).

16 sesiones worker emitieron TICKET_CONTINUE con "falta revision" en vez de
TICKET_READY, asi el orquestador nunca avanzo a revision/cierre.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import automatico


def _child() -> dict:
    return {"step": "F4", "ticket": "TASK-2026-00349",
            "branch": "auto/00349-reanudar-mision", "deps": [],
            "tests": [], "knowledge": False, "workspace": _REPO}


def _ticket() -> dict:
    return {"phase": "work", "parked": "", "note": "", "failure": "",
            "sessions": 16, "base": {}, "reviews": [], "ready": ""}


def test_work_prompt_orders_ready_when_only_review_or_close_remain():
    prompt = automatico.work_prompt(_child(), _ticket())
    assert "tras tu TICKET_READY" in prompt
    assert "reserva TICKET_CONTINUE para implementación sin terminar" in prompt


def test_ready_marker_advances_work_phase():
    out = "todo en verde\nTICKET_READY helper listo para revisión\n"
    marker, _detail = automatico.last_marker(out)
    assert marker == automatico.WORK_READY
