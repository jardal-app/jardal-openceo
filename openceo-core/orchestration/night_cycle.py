"""Explicit night-cycle target (TASK-2026-00247).

Pins the epic/project/order the night runner aims at and picks the next
eligible child deterministically: no global sweeps, no Markdown backlogs,
no competing orchestrator. ``NightTarget`` keeps its last good target
across transient fetch failures and resumes when the API recovers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "NIGHT_EPIC_DEFAULT",
    "NIGHT_ORDER_DEFAULT",
    "NIGHT_PROJECT_DEFAULT",
    "NightTarget",
    "TransientFetchError",
    "select_next_eligible",
]

NIGHT_PROJECT_DEFAULT = "jardal-openceo"
NIGHT_EPIC_DEFAULT = "TASK-2026-00245"
NIGHT_ORDER_DEFAULT = (
    "TASK-2026-00247",
    "TASK-2026-00244",
    "TASK-2026-00246",
)

_CLOSED = frozenset({"completed", "cancelled"})


class TransientFetchError(Exception):
    """The epic/children read failed but may succeed on retry."""


def _name_of(child: Mapping[str, Any]) -> str:
    for key in ("name", "task_id", "ticket"):
        value = str(child.get(key) or "").strip()
        if value:
            return value
    return ""


def _deps_of(child: Mapping[str, Any]) -> list[str]:
    deps = child.get("depends_on") or child.get("blocked_by") or []
    if isinstance(deps, str):
        deps = [d.strip() for d in deps.split(",")]
    return [str(d).strip() for d in deps if str(d).strip()]


def select_next_eligible(
    children: Sequence[Mapping[str, Any]],
    order: Sequence[str] = NIGHT_ORDER_DEFAULT,
) -> dict[str, Any] | None:
    """First open child in ``order`` whose dependencies are closed.

    Accepts business (``Completed``) and execution (``completed``) status
    vocabularies. Children outside ``order`` trail in input order.
    """
    rows = [c for c in children if isinstance(c, Mapping)]
    closed = {
        _name_of(c) for c in rows if str(c.get("status") or "").strip().lower() in _CLOSED
    }
    rank = {name: i for i, name in enumerate(order)}

    def _key(child: Mapping[str, Any]) -> tuple[int, int]:
        name = _name_of(child)
        return (0 if name in rank else 1, rank.get(name, 0))

    for child in sorted(rows, key=_key):
        name = _name_of(child)
        if not name or name in closed:
            continue
        if any(dep not in closed for dep in _deps_of(child)):
            continue
        return dict(child)
    return None


@dataclass
class NightTarget:
    """Epic target that survives transient API outages."""

    project: str = NIGHT_PROJECT_DEFAULT
    epic: str = NIGHT_EPIC_DEFAULT
    order: Sequence[str] = NIGHT_ORDER_DEFAULT
    last_children: list[dict[str, Any]] = field(default_factory=list)
    last_pick: dict[str, Any] | None = None

    def refresh(
        self, fetch: Callable[[], Sequence[Mapping[str, Any]]]
    ) -> dict[str, Any]:
        """Re-read children, pick next; on transient failure keep the target.

        Returns ``{"pick", "stale", "done"}``: ``done`` when nothing is
        eligible after a good read, ``stale`` when the pick comes from the
        last good read because this one failed.
        """
        try:
            children = [dict(c) for c in (fetch() or []) if isinstance(c, Mapping)]
        except TransientFetchError:
            return {"pick": self.last_pick, "stale": True, "done": False}
        self.last_children = children
        self.last_pick = select_next_eligible(children, self.order)
        return {
            "pick": self.last_pick,
            "stale": False,
            "done": self.last_pick is None,
        }
