"""CEO wake for out-of-process writers (D5, ticket #580).

Contract: the core exposes POST /api/ceo/wake guarded by an exclusive
machine Bearer (Owner option A, fail-closed, ``require_admin`` and the
ERP auth untouched); the route calls ``kernel.wake("ceo")``. The
``resume_pedido`` writer persists first (``save_ceo_trigger``) and
pokes after, best-effort: a dead core or a failed poke loses nothing
and the report carries ``woken: False``.

Logic/contract only, with doubles; no live model calls (Owner order
2026-10-14). ``main`` is imported as in the auth inventory suite.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from ops.resume_pedido import (  # noqa: E402
    CEO_WAKE_BEARER_ENV,
    CEO_WAKE_BASE_URL_ENV,
    request_ceo_wake,
    resume_failed_pedido,
)
from state import TaskStatus  # noqa: E402

BEARER = "test-machine-Bearer token-580"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _failed_pedido(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "pedido-344", "project": "proj", "agent": "ceo",
        "source": "api", "title": "Pedido Shop necesita checkout",
        "description": "Descripcion", "attachments": [],
        "priority": "high", "status": TaskStatus.FAILED.value,
        "steps": [],
        "params": {
            "planner_attempts": 3,
            "planner_last_invalid": "{bad json}",
            "shop": "keep-me",
        },
        "error": "planner failed 3x: materiality",
        "waiting_reason": None,
        "resume_at": None,
    }
    task.update(overrides)
    return task


class _FakeRepo:
    def __init__(self, task: dict[str, Any] | None,
                 trigger: Any = "default") -> None:
        self.task = dict(task) if task else None
        if trigger == "default":
            trigger = {
                "next_scheduled_at": datetime(2026, 11, 4,
                                              tzinfo=timezone.utc),
                "pending_reasons": [],
            }
        self.trigger = dict(trigger) if trigger is not None else None
        self.events: list[str] = []
        self.saves: list = []

    async def get_task(self, task_id: str):
        if self.task is None or task_id != self.task["task_id"]:
            return None
        return dict(self.task)

    async def patch_task(self, task_id: str, updates: dict):
        self.events.append("patch_task")
        assert self.task is not None
        self.task.update(updates)
        return dict(self.task)

    async def get_ceo_trigger(self):
        return dict(self.trigger) if self.trigger is not None else None

    async def save_ceo_trigger(self, doc: dict) -> None:
        self.events.append("save_ceo_trigger")
        self.saves.append(dict(doc))
        self.trigger = dict(doc)


class _FakeKernel:
    def __init__(self) -> None:
        self.woken: list[str] = []

    def wake(self, agenda: str) -> None:
        self.woken.append(agenda)


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WakeRouteTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, bearer: str) -> mock._patch:
        return mock.patch.object(
            main, "settings",
            SimpleNamespace(OPENCEO_CEO_WAKE_BEARER=bearer))

    async def test_verifier_rejects_when_secret_unconfigured(self) -> None:
        with self._settings(""):
            with self.assertRaises(HTTPException) as ctx:
                await main.verify_ceo_wake_bearer("Bearer anything")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_verifier_rejects_missing_bearer(self) -> None:
        with self._settings(BEARER):
            with self.assertRaises(HTTPException) as ctx:
                await main.verify_ceo_wake_bearer("")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_verifier_rejects_wrong_bearer(self) -> None:
        with self._settings(BEARER):
            with self.assertRaises(HTTPException) as ctx:
                await main.verify_ceo_wake_bearer("Bearer wrong")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_verifier_accepts_correct_bearer(self) -> None:
        with self._settings(BEARER):
            await main.verify_ceo_wake_bearer(f"Bearer {BEARER}")

    async def test_route_wakes_ceo(self) -> None:
        kernel = _FakeKernel()
        with mock.patch.object(main.app.state, "schedule_kernel", kernel,
                               create=True):
            report = await main.wake_ceo_agenda({"agenda": "ceo"})
        self.assertEqual(report, {"ok": True, "agenda": "ceo"})
        self.assertEqual(kernel.woken, ["ceo"])

    async def test_route_rejects_other_agendas_without_waking(self) -> None:
        kernel = _FakeKernel()
        with mock.patch.object(main.app.state, "schedule_kernel", kernel,
                               create=True):
            with self.assertRaises(HTTPException) as ctx:
                await main.wake_ceo_agenda({"agenda": "dispatcher"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(kernel.woken, [])

    async def test_route_rejects_missing_agenda(self) -> None:
        kernel = _FakeKernel()
        with mock.patch.object(main.app.state, "schedule_kernel", kernel,
                               create=True):
            with self.assertRaises(HTTPException) as ctx:
                await main.wake_ceo_agenda({})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(kernel.woken, [])

    async def test_route_reports_kernel_down(self) -> None:
        with mock.patch.object(main.app.state, "schedule_kernel", None,
                               create=True):
            with self.assertRaises(HTTPException) as ctx:
                await main.wake_ceo_agenda({"agenda": "ceo"})
        self.assertEqual(ctx.exception.status_code, 503)


class WakeClientTests(unittest.TestCase):
    def _clear_env(self) -> None:
        saved = {
            key: os.environ.pop(key, None)
            for key in (CEO_WAKE_BASE_URL_ENV, CEO_WAKE_BEARER_ENV)
        }
        self.addCleanup(lambda: os.environ.update(
            {k: v for k, v in saved.items() if v is not None}))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)

    def test_skips_without_config_and_no_network(self) -> None:
        self._clear_env()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not dial")):
            self.assertFalse(request_ceo_wake())

    def test_posts_agenda_with_bearer(self) -> None:
        seen: dict[str, Any] = {}

        def _fake_open(request, timeout=None):
            seen["request"] = request
            seen["timeout"] = timeout
            return _FakeResponse(status=200)

        with mock.patch("urllib.request.urlopen", _fake_open):
            self.assertTrue(request_ceo_wake(
                base_url="http://core:18765/", bearer=BEARER))

        request = seen["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url,
                         "http://core:18765/api/ceo/wake")
        self.assertEqual(json.loads(request.data), {"agenda": "ceo"})
        headers = dict(request.header_items())
        self.assertEqual(headers.get("Authorization"), f"Bearer {BEARER}")
        self.assertEqual(seen["timeout"], 2.0)

    def test_non_2xx_maps_to_false(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResponse(status=503)):
            self.assertFalse(request_ceo_wake(
                base_url="http://core:18765", bearer=BEARER))

    def test_network_error_maps_to_false(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            self.assertFalse(request_ceo_wake(
                base_url="http://core:18765", bearer=BEARER))

    def test_failed_poke_never_logs_the_secret(self) -> None:
        with self.assertLogs("ops.resume_pedido", level="DEBUG") as captured:
            self.assertFalse(request_ceo_wake(
                base_url="http://127.0.0.1:9", bearer="super-secret-bearer",
                timeout=0.05))
        self.assertNotIn("super-secret-bearer",
                         "\n".join(captured.output))


class ResumeWakeCycleTests(unittest.IsolatedAsyncioTestCase):
    def _clear_env(self) -> None:
        saved = {
            key: os.environ.pop(key, None)
            for key in (CEO_WAKE_BASE_URL_ENV, CEO_WAKE_BEARER_ENV)
        }
        self.addCleanup(lambda: os.environ.update(
            {k: v for k, v in saved.items() if v is not None}))
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)

    async def test_persists_first_then_wakes(self) -> None:
        repo = _FakeRepo(_failed_pedido())
        calls: list[str] = []

        def _waker() -> bool:
            calls.append("wake")
            assert repo.saves, "wake ran before save_ceo_trigger"
            return True

        report = await resume_failed_pedido(
            repo, "pedido-344", now=NOW, waker=_waker)

        self.assertTrue(report["ok"])
        self.assertTrue(report["woken"])
        self.assertEqual(repo.events,
                         ["patch_task", "save_ceo_trigger"])
        self.assertEqual(calls, ["wake"])
        self.assertEqual(repo.saves[-1]["next_scheduled_at"], NOW)
        self.assertEqual(report["verified_status"], TaskStatus.QUEUED.value)

    async def test_failed_wake_loses_nothing(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        report = await resume_failed_pedido(
            repo, "pedido-344", now=NOW, waker=lambda: False)

        self.assertTrue(report["ok"])
        self.assertFalse(report["woken"])
        self.assertEqual(repo.task["status"], TaskStatus.QUEUED.value)
        self.assertEqual(repo.saves[-1]["next_scheduled_at"], NOW)

    async def test_raising_wake_loses_nothing(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        def _boom() -> bool:
            raise ConnectionError("core down")

        report = await resume_failed_pedido(
            repo, "pedido-344", now=NOW, waker=_boom)

        self.assertTrue(report["ok"])
        self.assertFalse(report["woken"])
        self.assertEqual(repo.saves[-1]["next_scheduled_at"], NOW)

    async def test_async_waker_is_supported(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        async def _waker() -> bool:
            return True

        report = await resume_failed_pedido(
            repo, "pedido-344", now=NOW, waker=_waker)
        self.assertTrue(report["woken"])

    async def test_dry_run_never_pokes(self) -> None:
        repo = _FakeRepo(_failed_pedido())

        def _boom() -> bool:
            raise AssertionError("dry_run must not poke")

        report = await resume_failed_pedido(
            repo, "pedido-344", now=NOW, dry_run=True, waker=_boom)

        self.assertTrue(report["ok"])
        self.assertFalse(report["woken"])
        self.assertEqual(repo.events, [])
        self.assertEqual(repo.saves, [])

    async def test_default_waker_without_config_skips_network(self) -> None:
        self._clear_env()
        repo = _FakeRepo(_failed_pedido())
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not dial")):
            report = await resume_failed_pedido(
                repo, "pedido-344", now=NOW)
        self.assertTrue(report["ok"])
        self.assertFalse(report["woken"])
        self.assertEqual(repo.saves[-1]["next_scheduled_at"], NOW)

    async def test_refusals_carry_woken_false(self) -> None:
        cases = [
            (_failed_pedido(status=TaskStatus.WAITING.value), "not_failed"),
            (_failed_pedido(coding_tool="tier_b"), "not_a_pedido"),
        ]
        for task, reason in cases:
            with self.subTest(reason=reason):
                repo = _FakeRepo(task)
                report = await resume_failed_pedido(
                    repo, "pedido-344", now=NOW)
                self.assertFalse(report["ok"])
                self.assertEqual(report["reason"], reason)
                self.assertFalse(report["woken"])
                self.assertEqual(repo.saves, [])
        repo = _FakeRepo(_failed_pedido())
        report = await resume_failed_pedido(repo, "missing", now=NOW)
        self.assertEqual(report["reason"], "task_not_found")
        self.assertFalse(report["woken"])


if __name__ == "__main__":
    unittest.main()
