"""El espejo Jardal ERP no pierde transiciones en silencio (TASK-2026-00486).

Dos huecos dejaban tickets desincronizados para siempre, probados aqui con
falsos, sin red ni Mongo:

- un rechazo duro (`JardalERPError`) en `_push_status` solo logueaba: sin
  `pending_status`, la reconciliacion de arranque jamas lo veia;
- un restart con el push en la cola en memoria lo vaciaba sin huella, y la
  consulta de reconciliacion solo miraba nombre vacio o `pending_status`
  (caso real: TASK-2026-00471, `completed` perdido en el restart de las 03:08).
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from tools.jardal_erp_client import JardalERPValidationError  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_ABSENT = object()


class _StubRepo:
    """Repo en memoria: solo lo que tocan el push y la reconciliacion."""

    def __init__(self, rows):
        self._rows = {row["task_id"]: copy.deepcopy(row) for row in rows}
        self.patch_calls = []

    async def get_task(self, task_id):
        row = self._rows.get(task_id)
        return copy.deepcopy(row) if row is not None else None

    async def patch_task(self, task_id, updates):
        self.patch_calls.append((task_id, copy.deepcopy(updates)))
        row = self._rows.setdefault(task_id, {"task_id": task_id})
        row.update(copy.deepcopy(updates))
        return copy.deepcopy(row)

    async def list_tasks_needing_jardal_erp_sync(self, limit=200):
        return [copy.deepcopy(row) for row in self._rows.values()][:limit]


class _PushAdapter:
    provider = "jardal_erp"
    is_enabled = True

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def update_status(self, name, status, task=None, work_dates=None):
        self.calls.append({"name": name, "status": status})
        if self.error is not None:
            raise self.error
        return {"ok": True}


def _service(adapter, repo):
    from task_service import TaskService

    service = TaskService.__new__(TaskService)
    service._settings = SimpleNamespace(
        TASK_AUTORUN=False,
        HORIZON_PERSONAL_MIRROR_ENABLED=False,
        JARDAL_ERP_SITE_TIMEZONE=None,
    )
    service._business = adapter
    service._repo = repo
    service._jardal_erp_queue = asyncio.Queue()
    return service


def _task(task_id, status, *, pushed, pending=_ABSENT, name="TASK-1"):
    mirror = {
        "name": name,
        "ref": "api:1",
        "status_pushed": pushed,
        "provider": "jardal_erp",
    }
    if pending is not _ABSENT:
        mirror["pending_status"] = pending
    return {
        "task_id": task_id,
        "status": status,
        "params": {"_mirrors": {"business": mirror}},
    }


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def _mirror(repo, task_id):
    return repo._rows[task_id]["params"]["_mirrors"]["business"]


class HardRejectParksPendingTests(unittest.TestCase):
    def test_rechazo_duro_deja_pending_para_la_reconciliacion(self):
        repo = _StubRepo([_task("t1", "completed", pushed="in_progress")])
        adapter = _PushAdapter(error=JardalERPValidationError("Task Type roto"))
        service = _service(adapter, repo)
        run(service._push_status({"task_id": "t1", "name": "TASK-1", "status": "completed"}))
        self.assertEqual(len(adapter.calls), 1)
        mirror = _mirror(repo, "t1")
        self.assertEqual(mirror["pending_status"], "completed")
        self.assertEqual(mirror["status_pushed"], "in_progress")


class ReconcileHealsLostPushTests(unittest.TestCase):
    def test_transicion_perdida_en_restart_se_reencola(self):
        repo = _StubRepo([_task("t1", "completed", pushed="in_progress")])
        adapter = _PushAdapter()
        service = _service(adapter, repo)
        repaired = run(service.startup_reconcile_jardal_erp())
        self.assertEqual(repaired, 1)
        self.assertEqual(
            _drain(service._jardal_erp_queue),
            [{"task_id": "t1", "name": "TASK-1", "status": "completed"}],
        )
        self.assertEqual(adapter.calls, [])

    def test_estado_no_empujable_se_limpia_sin_http(self):
        repo = _StubRepo([_task("t9", "failed", pushed="queued")])
        adapter = _PushAdapter()
        service = _service(adapter, repo)
        run(service.startup_reconcile_jardal_erp())
        self.assertEqual(_drain(service._jardal_erp_queue), [])
        self.assertEqual(adapter.calls, [])
        mirror = _mirror(repo, "t9")
        self.assertEqual(mirror["status_pushed"], "failed")
        self.assertIsNone(mirror.get("pending_status"))

    def test_estado_ya_empujado_no_se_toca(self):
        repo = _StubRepo([_task("t2", "completed", pushed="completed", pending=None)])
        adapter = _PushAdapter()
        service = _service(adapter, repo)
        run(service.startup_reconcile_jardal_erp())
        self.assertEqual(_drain(service._jardal_erp_queue), [])
        self.assertEqual(adapter.calls, [])
        self.assertEqual(repo.patch_calls, [])

    def test_pending_existente_se_reencola(self):
        repo = _StubRepo([_task("t3", "completed", pushed="queued", pending="completed")])
        adapter = _PushAdapter()
        service = _service(adapter, repo)
        run(service.startup_reconcile_jardal_erp())
        self.assertEqual(
            _drain(service._jardal_erp_queue),
            [{"task_id": "t3", "name": "TASK-1", "status": "completed"}],
        )


class ReconciliationQueryTests(unittest.TestCase):
    def _query(self):
        import horizon_support

        repo = horizon_support.make_fake_mongo([])
        run(repo.list_tasks_needing_jardal_erp_sync())
        return repo.tasks.last_query

    def test_detecta_estados_nunca_empujados(self):
        query = self._query()
        exprs = []

        def visit(node):
            if isinstance(node, dict):
                if "$expr" in node:
                    exprs.append(node["$expr"])
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(query.get("$or"))
        self.assertTrue(
            any(
                expr.get("$ne") == ["$status", "$params._mirrors.business.status_pushed"]
                for expr in exprs
            ),
            "la consulta debe casar status != status_pushed",
        )

    def test_mantiene_privacidad_y_sin_backfill(self):
        query = self._query()
        self.assertEqual(query["params.horizon_scope"], {"$ne": "personal"})
        for branch in query["$or"]:
            business = branch.get("params._mirrors.business")
            legacy = branch.get("params._mirrors.erpnext")
            self.assertTrue(
                business == {"$exists": True} or legacy == {"$exists": True},
                "cada rama exige un espejo previo: sin backfill de historicos",
            )


if __name__ == "__main__":
    unittest.main()
