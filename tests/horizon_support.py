"""Repositorio en memoria para los tests de Horizon.

Replica la semantica que el servicio realmente usa del repositorio Mongo:
claves naturales de upsert, deduplicacion por clave unica y filtro por
propietario. No replica Mongo entero, solo lo que esta en el camino de los
tests; una divergencia aqui se veria como un test que pasa con el fake y falla
en produccion, por eso las claves se nombran igual que en ``db/mongo.py``.
"""

from __future__ import annotations

import copy
from typing import Any


class FakeHorizonRepo:
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}
        self.objectives: dict[str, dict[str, Any]] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.actions: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.triggers: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        # Cuando se fija a un plan_id distinto, ``set_current_horizon_plan``
        # rechaza la publicacion: simula que otro escritor gano la carrera.
        self.stale_plan_wins: str | None = None

    # ------------------------- profile -------------------------

    async def get_horizon_profile(self, user_id: str) -> dict[str, Any] | None:
        self.calls.append("get_horizon_profile")
        row = self.profiles.get(user_id)
        return copy.deepcopy(row) if row else None

    async def upsert_horizon_profile(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("upsert_horizon_profile")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        current = self.profiles.get(payload["user_id"])
        if current:
            payload.setdefault("created_at", current.get("created_at"))
        self.profiles[payload["user_id"]] = payload
        return copy.deepcopy(payload)

    # ------------------------- objectives -------------------------

    async def list_horizon_objectives(
        self,
        user_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append("list_horizon_objectives")
        rows = [
            copy.deepcopy(o)
            for o in self.objectives.values()
            if o.get("user_id") == user_id
            and (not statuses or str(o.get("status")) in statuses)
        ]
        return sorted(rows, key=lambda o: int(o.get("priority") or 100))

    async def get_horizon_objective_by_source_key(
        self,
        user_id: str,
        source_key: str,
    ) -> dict[str, Any] | None:
        if not source_key:
            return None
        for objective in self.objectives.values():
            if objective.get("user_id") == user_id and objective.get("source_key") == source_key:
                return copy.deepcopy(objective)
        return None

    async def upsert_horizon_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("upsert_horizon_objective")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        key = (payload["user_id"], payload.get("source_key"))
        for objective_id, existing in self.objectives.items():
            if (existing.get("user_id"), existing.get("source_key")) == key:
                payload["objective_id"] = existing.get("objective_id")
                payload.setdefault("created_at", existing.get("created_at"))
                self.objectives[objective_id] = payload
                return copy.deepcopy(payload)
        self.objectives[payload["objective_id"]] = payload
        return copy.deepcopy(payload)

    async def update_horizon_milestone(
        self,
        *,
        user_id: str,
        objective_id: str,
        milestone_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        self.calls.append("update_horizon_milestone")
        objective = self.objectives.get(objective_id)
        if not objective or objective.get("user_id") != user_id:
            return None
        for milestone in objective.get("milestones") or []:
            if str(milestone.get("milestone_id")) == milestone_id:
                milestone.update(copy.deepcopy(updates))
                return copy.deepcopy(milestone)
        return None

    # ------------------------- actions -------------------------

    async def create_horizon_action(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_horizon_action")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        if not payload.get("action_id"):
            payload["action_id"] = f"hact_{len(self.actions) + 1}"
        self.actions[payload["action_id"]] = payload
        return copy.deepcopy(payload)

    async def list_horizon_actions(
        self,
        user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.calls.append("list_horizon_actions")
        rows = [
            copy.deepcopy(a)
            for a in self.actions.values()
            if a.get("user_id") == user_id
            and (not statuses or str(a.get("status")) in statuses)
        ]
        rows.sort(key=lambda a: (int(a.get("priority") or 100), str(a.get("created_at") or "")))
        return rows[:limit]

    async def count_horizon_actions(self, user_id: str) -> int:
        return sum(1 for a in self.actions.values() if a.get("user_id") == user_id)

    # ------------------------- captures -------------------------

    async def create_horizon_capture(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_horizon_capture")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        self.captures[payload["capture_id"]] = payload
        return copy.deepcopy(payload)

    async def get_horizon_capture_by_dedupe_key(
        self,
        user_id: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        self.calls.append("get_horizon_capture_by_dedupe_key")
        for capture in self.captures.values():
            if capture.get("user_id") == user_id and capture.get("dedupe_key") == dedupe_key:
                return copy.deepcopy(capture)
        return None

    async def list_horizon_captures(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = [
            copy.deepcopy(c) for c in self.captures.values() if c.get("user_id") == user_id
        ]
        return rows[:limit]

    # ------------------------- events -------------------------

    async def create_horizon_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_horizon_event")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        self.events[payload["event_id"]] = payload
        return copy.deepcopy(payload)

    async def get_horizon_event_by_source(
        self,
        user_id: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        self.calls.append("get_horizon_event_by_source")
        for event in self.events.values():
            if event.get("user_id") == user_id and event.get("source_id") == source_id:
                return copy.deepcopy(event)
        return None

    # ------------------------- planes -------------------------

    async def create_horizon_plan(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_horizon_plan")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        payload.setdefault("is_current", False)
        self.plans[payload["plan_id"]] = payload
        return copy.deepcopy(payload)

    async def get_horizon_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self.plans.get(plan_id)
        return copy.deepcopy(row) if row else None

    async def get_current_horizon_plan(self, user_id: str) -> dict[str, Any] | None:
        self.calls.append("get_current_horizon_plan")
        for plan in self.plans.values():
            if plan.get("user_id") == user_id and plan.get("is_current"):
                return copy.deepcopy(plan)
        # Respaldo por version, igual que el repositorio real: cubre la ventana
        # entre retirar el vigente y marcar el nuevo.
        mine = [p for p in self.plans.values() if p.get("user_id") == user_id]
        if not mine:
            return None
        newest = max(mine, key=lambda p: int(p.get("version") or 0))
        return copy.deepcopy(newest)

    async def list_recent_horizon_plans(
        self, user_id: str, limit: int = 2
    ) -> list[dict[str, Any]]:
        mine = [p for p in self.plans.values() if p.get("user_id") == user_id]
        mine.sort(key=lambda p: int(p.get("version") or 0), reverse=True)
        return [copy.deepcopy(p) for p in mine[: max(1, int(limit))]]

    async def next_horizon_plan_version(self, user_id: str) -> int:
        mine = [int(p.get("version") or 0) for p in self.plans.values() if p.get("user_id") == user_id]
        return (max(mine) + 1) if mine else 1

    async def set_current_horizon_plan(
        self, user_id: str, plan_id: str
    ) -> dict[str, Any] | None:
        self.calls.append("set_current_horizon_plan")
        target = self.plans.get(plan_id)
        if target is None or target.get("user_id") != user_id:
            return None
        # ``stale_plan_wins`` simula la carrera: otro escritor ya publico y esta
        # escritura se descarta en vez de dejar dos planes vigentes.
        if self.stale_plan_wins is not None and self.stale_plan_wins != plan_id:
            return None
        for plan in self.plans.values():
            if plan.get("user_id") == user_id and plan.get("plan_id") != plan_id:
                plan["is_current"] = False
                plan["superseded_by"] = plan_id
        target["is_current"] = True
        target["superseded_by"] = None
        return copy.deepcopy(target)

    async def patch_horizon_plan(
        self, plan_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        plan.update(copy.deepcopy(dict(updates)))
        return copy.deepcopy(plan)

    async def push_horizon_plan_aviso(
        self, plan_id: str, aviso: dict[str, Any], *, keep: int = 20
    ) -> dict[str, Any] | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        avisos = list(plan.get("avisos") or [])
        avisos.append(copy.deepcopy(dict(aviso)))
        plan["avisos"] = avisos[-abs(int(keep)) :]
        return copy.deepcopy(plan)

    # ------------------------- disparadores -------------------------

    async def create_horizon_trigger(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append("create_horizon_trigger")
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        key = payload.get("dedupe_key")
        if key and any(t.get("dedupe_key") == key for t in self.triggers.values()):
            # El indice unico real lanza DuplicateKeyError; el repositorio lo
            # traduce a None. La semantica que importa es la misma.
            return None
        payload.setdefault("attempts", 0)
        payload.setdefault("max_attempts", 3)
        self.triggers[payload["trigger_id"]] = payload
        return copy.deepcopy(payload)

    async def get_horizon_trigger_by_dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        self.calls.append("get_horizon_trigger_by_dedupe")
        for trigger in self.triggers.values():
            if trigger.get("dedupe_key") == dedupe_key:
                return copy.deepcopy(trigger)
        return None

    async def list_due_horizon_triggers(
        self, now: Any, limit: int = 20
    ) -> list[dict[str, Any]]:
        due = [
            copy.deepcopy(t)
            for t in self.triggers.values()
            if t.get("status") == "pending" and _lte(t.get("next_run_at"), now)
        ]
        due.sort(key=lambda t: t.get("next_run_at"))
        return due[: max(1, int(limit))]

    async def next_horizon_trigger_due_at(self) -> Any:
        pending = [t.get("next_run_at") for t in self.triggers.values() if t.get("status") == "pending"]
        return min(pending) if pending else None

    async def mark_horizon_trigger_running(self, trigger_id: str) -> dict[str, Any] | None:
        trigger = self.triggers.get(trigger_id)
        if trigger is None or trigger.get("status") != "pending":
            return None
        trigger["status"] = "running"
        return copy.deepcopy(trigger)

    async def patch_horizon_trigger(
        self, trigger_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        trigger = self.triggers.get(trigger_id)
        if trigger is None:
            return None
        trigger.update(copy.deepcopy(dict(updates)))
        return copy.deepcopy(trigger)

    async def list_horizon_triggers(
        self, user_id: str, *, statuses: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = [
            copy.deepcopy(t)
            for t in self.triggers.values()
            if t.get("user_id") == user_id
            and (not statuses or str(t.get("status")) in statuses)
        ]
        return rows[: max(1, int(limit))]

    async def recover_stale_horizon_triggers(self, cutoff: Any) -> int:
        recovered = 0
        for trigger in self.triggers.values():
            if trigger.get("status") != "running":
                continue
            if not _lte(trigger.get("started_at"), cutoff):
                continue
            trigger["status"] = "pending"
            trigger["attempts"] = int(trigger.get("attempts") or 0) + 1
            trigger["last_error"] = "recovered_after_restart"
            recovered += 1
        return recovered

    async def expire_horizon_triggers(self, now: Any) -> int:
        expired = 0
        for trigger in self.triggers.values():
            if trigger.get("status") != "pending":
                continue
            expires_at = trigger.get("expires_at")
            if expires_at is not None and _lte(expires_at, now):
                trigger["status"] = "expired"
                expired += 1
        return expired

    # ------------------------- acciones (por tarea) -------------------------

    async def get_horizon_action(self, action_id: str) -> dict[str, Any] | None:
        self.calls.append("get_horizon_action")
        action = self.actions.get(action_id)
        return copy.deepcopy(action) if action else None

    async def get_horizon_action_by_task_id(
        self, user_id: str, task_id: str
    ) -> dict[str, Any] | None:
        for action in self.actions.values():
            if action.get("user_id") == user_id and action.get("task_id") == task_id:
                return copy.deepcopy(action)
        return None

    async def patch_horizon_action(
        self, action_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        self.calls.append("patch_horizon_action")
        action = self.actions.get(action_id)
        if action is None:
            return None
        action.update(copy.deepcopy(dict(updates)))
        return copy.deepcopy(action)


def _lte(value: Any, other: Any) -> bool:
    """Compara con la semantica de ``$lte`` para fechas ausentes.

    Un campo ausente no satisface ``$lte`` en Mongo, y esa diferencia importa:
    un disparador sin ``expires_at`` no debe caducar.
    """
    if value is None or other is None:
        return False
    return value <= other


def _matches_exclusion(row: dict[str, Any]) -> bool:
    """Replica la semantica de ``$ne`` de Mongo para el filtro de exclusion.

    Se usa para comprobar que el filtro compuesto por
    ``list_tasks_missing_notion_mirror`` deja fuera el alcance personal. Un
    documento sin la clave satisface ``$ne`` y por tanto sigue siendo elegible.
    """
    params = row.get("params")
    if not isinstance(params, dict):
        return True
    return params.get("horizon_scope") != "personal"


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def limit(self, _count: int) -> "_FakeCursor":
        return self

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row

        return gen()


class _FakeTasksCollection:
    """Captura la consulta y devuelve solo las filas que la satisfacen."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.last_query: dict[str, Any] | None = None

    def find(self, query: dict[str, Any], sort=None) -> _FakeCursor:
        self.last_query = query
        selected = [
            row
            for row in self._rows
            if str(row.get("source") or "") != "notion"
            and "notion_mirror_created" not in (row.get("post_actions_executed") or [])
            and _matches_exclusion(row)
            and not _has_notion_page(row)
        ]
        return _FakeCursor(selected)


def _has_notion_page(row: dict[str, Any]) -> bool:
    params = row.get("params")
    if not isinstance(params, dict):
        return False
    mirrors = params.get("_mirrors")
    if not isinstance(mirrors, dict):
        return False
    notion = mirrors.get("notion")
    if not isinstance(notion, dict):
        return False
    return bool(notion.get("page_id"))


def make_fake_mongo(rows: list[dict[str, Any]]):
    """Subclase minima de MongoRepository para probar el safety net sin Mongo."""
    from db.mongo import MongoRepository

    class _FakeMongo(MongoRepository):
        def __init__(self, tasks_rows: list[dict[str, Any]]) -> None:
            self._tasks = _FakeTasksCollection(tasks_rows)

        @property
        def tasks(self) -> _FakeTasksCollection:
            return self._tasks

    return _FakeMongo(rows)