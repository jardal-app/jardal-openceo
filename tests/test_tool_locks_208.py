"""Locks inside dangerous boundary tools (TASK-2026-00208). No tokens."""

import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from owner_channel import OWNER_QUESTION_PHASE  # noqa: E402
from tool_locks import (  # noqa: E402
    LOCK_WAITING_PREFIX,
    PENDING_STATUS,
    ToolLockError,
    action_fingerprint,
    check_dns_delete_lock,
    check_email_lock,
    check_payment_lock,
    email_allowance,
    is_lock_pending,
    lock_hold_action,
    lock_question_spec,
    lock_source_id,
    lock_waiting_reason,
    pending_lock_result,
)


def _mandate() -> Any:
    from company_mandate import parse_mandate

    return parse_mandate({
        "version": 1,
        "ejes": [],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "0.00",
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            "gasto_nuevo",
            "accion_irreversible",
            "replanificacion_estrategica",
            "cambio_de_mandato",
            "revision_exigida_por_politica",
        ],
    })


class EmailAllowanceTests(unittest.TestCase):
    def test_exact_match_case_insensitive(self) -> None:
        split = email_allowance(["Boss@Example.COM"], ["boss@example.com"])
        self.assertEqual(split, {"allowed": ["Boss@Example.COM"], "blocked": []})

    def test_domain_suffix(self) -> None:
        split = email_allowance(
            ["a@jardal.app", "b@other.com"], ["@jardal.app"])
        self.assertEqual(split["allowed"], ["a@jardal.app"])
        self.assertEqual(split["blocked"], ["b@other.com"])

    def test_empty_allowlist_blocks_everything(self) -> None:
        for allowed in (None, [], ["  "]):
            with self.subTest(allowed=allowed):
                split = email_allowance(["a@x.com"], allowed)
                self.assertEqual(split["allowed"], [])
                self.assertEqual(split["blocked"], ["a@x.com"])

    def test_suffix_without_at_is_exact(self) -> None:
        split = email_allowance(["jardal.app"], ["jardal.app"])
        self.assertEqual(split["allowed"], ["jardal.app"])

    def test_blank_recipients_ignored(self) -> None:
        split = email_allowance(["  ", None], ["@x.com"])
        self.assertEqual(split, {"allowed": [], "blocked": []})

    def test_comma_joined_string_splits_like_sender(self) -> None:
        # Review R1: the sender splits a bare string on commas, so the
        # lock must too — one hidden off-list address must not ride
        # along. (List items stay whole on both sides, like the sender.)
        split = email_allowance(
            "evil@evil.com, a@jardal.app", ["@jardal.app"])
        self.assertEqual(split["allowed"], ["a@jardal.app"])
        self.assertEqual(split["blocked"], ["evil@evil.com"])

    def test_comma_inside_list_element_splits(self) -> None:
        # Review 00008: stricter than the sender on purpose — an
        # element hiding several addresses locks on each one.
        split = email_allowance(
            ["evil@evil.com, a@jardal.app"], ["@jardal.app"])
        self.assertEqual(split["allowed"], ["a@jardal.app"])
        self.assertEqual(split["blocked"], ["evil@evil.com"])


class CheckNonSimpleAddressTests(unittest.TestCase):
    def test_space_joined_and_display_forms_escalate(self) -> None:
        config = {"secretary": {"allowed_recipients": ["@jardal.app"]}}
        for to in ("a@jardal.app b@evil.com",
                   ["Jefe <a@jardal.app>"],
                   ["a@@jardal.app"]):
            with self.subTest(to=to):
                decision = check_email_lock(
                    to=to, project_config=config, mandate=_mandate())
                self.assertTrue(decision["escalate"])

    def test_plain_allowed_still_proceeds(self) -> None:
        config = {"secretary": {"allowed_recipients": ["@jardal.app"]}}
        decision = check_email_lock(
            to=["a@jardal.app"], project_config=config,
            mandate=_mandate())
        self.assertFalse(decision["escalate"])


class CheckEmailLockTests(unittest.TestCase):
    def _config(self, allowed: list[str] | None) -> dict[str, Any]:
        return {"secretary": {"allowed_recipients": allowed or []}}

    def test_all_allowed_proceeds(self) -> None:
        decision = check_email_lock(
            to=["a@jardal.app"], cc="b@jardal.app", bcc=[],
            project_config=self._config(["@jardal.app"]),
            mandate=_mandate(),
        )
        self.assertFalse(decision["escalate"])
        self.assertIsNone(decision["category"])

    def test_one_off_list_escalates_irreversible(self) -> None:
        decision = check_email_lock(
            to=["a@jardal.app", "stranger@evil.com"],
            project_config=self._config(["@jardal.app"]),
            mandate=_mandate(),
        )
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "accion_irreversible")
        self.assertIn("stranger@evil.com", decision["human_action"])
        self.assertEqual(decision["blocked"], ["stranger@evil.com"])

    def test_cc_and_bcc_count(self) -> None:
        for field in ("cc", "bcc"):
            with self.subTest(field=field):
                decision = check_email_lock(
                    to=["a@jardal.app"],
                    **{field: ["x@evil.com"]},  # type: ignore[arg-type]
                    project_config=self._config(["@jardal.app"]),
                    mandate=_mandate(),
                )
                self.assertTrue(decision["escalate"])

    def test_missing_config_escalates_fail_closed(self) -> None:
        for config in (None, {}, {"secretary": None}, {"secretary": {}}):
            with self.subTest(config=config):
                decision = check_email_lock(
                    to=["a@jardal.app"], project_config=config,
                    mandate=_mandate(),
                )
                self.assertTrue(decision["escalate"])

    def test_missing_mandate_still_escalates(self) -> None:
        decision = check_email_lock(
            to=["x@evil.com"],
            project_config=self._config(["@jardal.app"]),
            mandate=None,
        )
        self.assertTrue(decision["escalate"])


class CheckDnsDeleteLockTests(unittest.TestCase):
    def test_always_escalates_irreversible(self) -> None:
        decision = check_dns_delete_lock(
            hostname="x.jardal.app", mandate=_mandate())
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "accion_irreversible")
        self.assertIn("x.jardal.app", decision["human_action"])

    def test_missing_mandate_still_escalates(self) -> None:
        decision = check_dns_delete_lock(
            hostname="x.jardal.app", mandate=None)
        self.assertTrue(decision["escalate"])


class CheckPaymentLockTests(unittest.TestCase):
    def test_payment_with_amount_escalates(self) -> None:
        decision = check_payment_lock(
            summary="Pagar factura", mandate=_mandate(), amount="12.50")
        self.assertTrue(decision["escalate"])

    def test_practice_run_without_amount_still_escalates(self) -> None:
        decision = check_payment_lock(
            summary="Ejecutar el pago Elfec House (practica)",
            mandate=_mandate())
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "accion_irreversible")


class FingerprintTests(unittest.TestCase):
    def test_stable_and_sensitive(self) -> None:
        parts = {"to": ["a@x"], "subject": "s", "body": "b"}
        self.assertEqual(action_fingerprint(parts),
                         action_fingerprint(dict(parts)))
        self.assertEqual(len(action_fingerprint(parts)), 12)
        changed = dict(parts, body="other")
        self.assertNotEqual(action_fingerprint(parts),
                            action_fingerprint(changed))

    def test_source_id_shape(self) -> None:
        self.assertEqual(lock_source_id("t1", "email", "abc123"),
                         "tool-lock:t1:email:abc123")
        self.assertEqual(lock_waiting_reason("dns"), "tool_lock:dns")


class LockQuestionSpecTests(unittest.TestCase):
    def _decision(self) -> dict[str, Any]:
        return {"escalate": True, "category": "accion_irreversible",
                "reason": "external irreversible action",
                "human_action": "Autoriza el borrado."}

    def test_checkpoint_flat_and_action_stored(self) -> None:
        spec = lock_question_spec(
            tool="dns", name="dns_delete",
            question="Borrar el registro DNS de x?",
            action={"hostname": "x.jardal.app"},
            fingerprint="fp1", decision=self._decision(),
            describe={"hostname": "x.jardal.app"},
            project="jardal-openceo", source_id="tool-lock:t:dns:fp1",
        )
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "lock")
        self.assertTrue(block["checkpoint"]["tool_lock"])
        self.assertEqual(block["checkpoint"]["tool"], "dns")
        self.assertEqual(block["checkpoint"]["hostname"], "x.jardal.app")
        self.assertEqual(block["tool"]["name"], "dns_delete")
        self.assertEqual(block["tool"]["action"],
                         {"hostname": "x.jardal.app"})
        self.assertEqual(spec["source_id"], "tool-lock:t:dns:fp1")
        self.assertIn("pregunta-dueno", spec["tags"])

    def test_automatic_decision_raises(self) -> None:
        with self.assertRaises(ToolLockError):
            lock_question_spec(
                tool="dns", name="dns_delete", question="Q?",
                action={}, fingerprint="f",
                decision={"escalate": False}, project="p")

    def test_missing_human_action_raises(self) -> None:
        decision = self._decision()
        decision["human_action"] = "  "
        with self.assertRaises(ToolLockError):
            lock_question_spec(
                tool="dns", name="dns_delete", question="Q?",
                action={}, fingerprint="f", decision=decision,
                project="p")


class LockHoldActionTests(unittest.TestCase):
    def _lock_task(self, **overrides: Any) -> dict[str, Any]:
        task: dict[str, Any] = {
            "task_id": "q1",
            "phase": OWNER_QUESTION_PHASE,
            "params": {"owner_question": {
                "kind": "lock",
                "question": "Q?",
                "human_action": "A.",
                "origin_task_id": "o1",
                "checkpoint": {"tool_lock": True, "tool": "email",
                               "fingerprint": "fp"},
                "tool": {"name": "send_email",
                         "action": {"to": ["a@x"]},
                         "fingerprint": "fp"},
            }},
        }
        task.update(overrides)
        return task

    def test_round_trip(self) -> None:
        held = lock_hold_action(self._lock_task())
        assert held is not None
        self.assertEqual(held["tool"], "email")
        self.assertEqual(held["action"], {"to": ["a@x"]})
        self.assertIsNone(held["execution"])

    def test_execution_marker_read(self) -> None:
        task = self._lock_task()
        task["params"]["owner_question"]["lock"] = {
            "execution": {"status": "executed", "outcome": {"ok": True}}}
        held = lock_hold_action(task)
        assert held is not None
        self.assertEqual(held["execution"]["status"], "executed")

    def test_non_lock_is_none(self) -> None:
        self.assertIsNone(lock_hold_action(None))
        self.assertIsNone(lock_hold_action({}))
        task = self._lock_task()
        task["params"]["owner_question"]["kind"] = "question"
        self.assertIsNone(lock_hold_action(task))
        task = self._lock_task()
        task["params"]["owner_question"]["checkpoint"] = {}
        self.assertIsNone(lock_hold_action(task))


class PendingShapeTests(unittest.TestCase):
    def test_pending_result_shape(self) -> None:
        pending = pending_lock_result(
            tool="email", question_task_id="q1",
            waiting_reason="tool_lock:email", detail="Q?")
        self.assertTrue(pending["ok"])
        self.assertEqual(pending["status"], PENDING_STATUS)
        self.assertTrue(is_lock_pending(pending))
        self.assertIn("bloqueado", pending["guidance"])

    def test_other_results_are_not_pending(self) -> None:
        self.assertFalse(is_lock_pending(None))
        self.assertFalse(is_lock_pending({"status": "sent"}))
        self.assertFalse(is_lock_pending({"ok": True}))


def _lock_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "description": f"Cuerpo de {task_id}",
        "project": "jardal-openceo",
        "status": "in_progress",
        "priority": "medium",
        "ticket_number": None,
        "params": {},
        "updated_at": datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _lock_question(
    task_id: str, tool: str = "email", origin: str | None = "origin-1",
    **overrides: Any,
) -> dict[str, Any]:
    return _lock_task(
        task_id,
        status="waiting",
        phase=OWNER_QUESTION_PHASE,
        waiting_reason=f"{OWNER_QUESTION_PHASE}:lock",
        params={"owner_question": {
            "kind": "lock",
            "question": "Enviar?",
            "human_action": "Autorizar o denegar",
            "origin_task_id": origin,
            "origin_title": None,
            "checkpoint": {"tool_lock": True, "tool": tool,
                           "fingerprint": "fp"},
            "tool": {"name": "send_email", "action": {"to": ["a@x"]},
                     "fingerprint": "fp"},
        }},
        **overrides,
    )


class _LockRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return deepcopy(row) if row is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.update(deepcopy(updates))
        return deepcopy(task)

    async def create_task_review(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.reviews.append(deepcopy(doc))
        return deepcopy(doc)

    async def latest_workflow_run_for_task(
        self, task_id: str
    ) -> dict[str, Any] | None:
        return None

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for row in self.tasks.values():
            if row.get("source") == source \
                    and row.get("source_id") == source_id:
                return deepcopy(row)
        return None

    async def find_task_by_business_name(
        self, name: str
    ) -> dict[str, Any] | None:
        for row in self.tasks.values():
            mirrors = (row.get("params") or {}).get("_mirrors") or {}
            business = mirrors.get("erpnext") or {}
            if business.get("name") == name:
                return deepcopy(row)
        return None

    async def list_tasks_blocked_by(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return []


class _LockHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del payload
        self.events.append(event)


class _LockConfig:
    def __init__(self, project_config: dict[str, Any] | None = None) -> None:
        self._project_config = project_config or {}

    def load_project(self, project: str | None) -> dict[str, Any]:
        del project
        return deepcopy(self._project_config)

    def load_company_mandate(self) -> Any:
        return _mandate()


def _lock_service(repo: _LockRepo,
                  project_config: dict[str, Any] | None = None):
    from settings import Settings
    from task_service import TaskService

    return TaskService(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        repo=repo,  # type: ignore[arg-type]
        graph=MagicMock(),
        realtime_hub=_LockHub(),  # type: ignore[arg-type]
        workflow_service=None,
        config_loader=_LockConfig(project_config),
    )


def _stub_create_question(service, repo: _LockRepo,
                           created: list[dict[str, Any]]):
    async def fake_create(payload: Any) -> dict[str, Any]:
        task_id = f"q{len(repo.tasks) + 1}"
        raw_source = getattr(payload, "source", "api")
        doc = _lock_task(
            task_id, status="queued",
            title=getattr(payload, "title", f"Ticket {task_id}"),
            description=getattr(payload, "description", ""),
            source=getattr(raw_source, "value", raw_source),
            source_id=getattr(payload, "source_id", None),
            params=deepcopy(getattr(payload, "params", None) or {}),
        )
        repo.tasks[task_id] = doc
        created.append(doc)
        return deepcopy(doc)

    service.create_task = fake_create  # type: ignore[method-assign]


class CheckLockStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_without_question(self) -> None:
        repo = _LockRepo()
        service = _lock_service(repo)
        seen = await service.check_lock_state("tool-lock:o:email:fp")
        self.assertEqual(seen["state"], "none")

    async def test_pending_approved_executed_denied(self) -> None:
        repo = _LockRepo()
        repo.tasks["qp"] = _lock_question(
            "qp", source="api", source_id="tool-lock:o:email:p")
        approved = _lock_question(
            "qa", source="api", source_id="tool-lock:o:email:a")
        approved["params"]["owner_question"]["decision"] = {
            "decision": "approved"}
        repo.tasks["qa"] = approved
        executed = _lock_question(
            "qe", source="api", source_id="tool-lock:o:email:e")
        executed["params"]["owner_question"]["decision"] = {
            "decision": "approved"}
        executed["params"]["owner_question"]["lock"] = {
            "execution": {"status": "executed",
                          "outcome": {"status": "sent"}}}
        repo.tasks["qe"] = executed
        denied = _lock_question(
            "qd", source="api", source_id="tool-lock:o:email:d")
        denied["params"]["owner_question"]["decision"] = {
            "decision": "denied"}
        repo.tasks["qd"] = denied
        service = _lock_service(repo)
        self.assertEqual(
            (await service.check_lock_state("tool-lock:o:email:p"))["state"],
            "pending")
        self.assertEqual(
            (await service.check_lock_state("tool-lock:o:email:a"))["state"],
            "approved")
        seen = await service.check_lock_state("tool-lock:o:email:e")
        self.assertEqual(seen["state"], "executed")
        self.assertEqual(seen["outcome"], {"status": "sent"})
        self.assertEqual(
            (await service.check_lock_state("tool-lock:o:email:d"))["state"],
            "denied")

    async def test_non_lock_source_is_none(self) -> None:
        repo = _LockRepo()
        other = _lock_task("o", source="api",
                           source_id="tool-lock:o:email:x")
        repo.tasks["o"] = other
        service = _lock_service(repo)
        seen = await service.check_lock_state("tool-lock:o:email:x")
        self.assertEqual(seen["state"], "none")

    async def test_store_errors_propagate_fail_closed(self) -> None:
        # Review R1: a forged "none" would re-escalate decided work.
        repo = _LockRepo()

        async def _boom(source: str, source_id: str) -> None:
            raise RuntimeError("mongo down")

        repo.find_task_by_source = _boom  # type: ignore[method-assign]
        service = _lock_service(repo)
        with self.assertRaises(RuntimeError):
            await service.check_lock_state("tool-lock:o:email:x")


class RequestToolLockTests(unittest.IsolatedAsyncioTestCase):
    def _decision(self) -> dict[str, Any]:
        return {"escalate": True, "category": "accion_irreversible",
                "reason": "external irreversible action",
                "human_action": "Autorizar o denegar."}

    async def test_creates_question_and_parks_origin(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", params={"returns_from": "planner"})
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        pending = await service.request_tool_lock(
            tool="email", name="send_email", question="Enviar a x?",
            action={"to": ["x@evil.com"]}, fingerprint="fp1",
            decision=self._decision(), project="jardal-openceo",
            origin_task_id="origin-1", origin_title="T",
        )
        self.assertTrue(is_lock_pending(pending))
        self.assertEqual(len(created), 1)
        question = await repo.get_task(pending["question_task_id"])
        assert question is not None
        self.assertEqual(question["status"], "waiting")
        block = question["params"]["owner_question"]
        self.assertEqual(block["kind"], "lock")
        self.assertEqual(block["origin_task_id"], "origin-1")
        self.assertEqual(block["tool"]["action"], {"to": ["x@evil.com"]})
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "waiting")
        self.assertEqual(origin["waiting_reason"], "tool_lock:email")
        self.assertEqual(origin["params"]["owner_question_id"],
                         pending["question_task_id"])
        self.assertEqual(origin["params"]["returns_from"], "planner")

    async def test_repeat_replays_without_duplicating(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        first = await service.request_tool_lock(
            tool="dns", name="dns_delete", question="Borrar?",
            action={"hostname": "x"}, fingerprint="fp",
            decision=self._decision(), project="jardal-openceo",
            origin_task_id="origin-1",
        )
        second = await service.request_tool_lock(
            tool="dns", name="dns_delete", question="Borrar?",
            action={"hostname": "x"}, fingerprint="fp",
            decision=self._decision(), project="jardal-openceo",
            origin_task_id="origin-1",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(first["question_task_id"],
                         second["question_task_id"])
        self.assertTrue(is_lock_pending(second))

    async def test_executed_replays_outcome(self) -> None:
        repo = _LockRepo()
        executed = _lock_question(
            "qe", source="api", source_id="tool-lock:o:email:e")
        executed["params"]["owner_question"]["decision"] = {
            "decision": "approved"}
        executed["params"]["owner_question"]["lock"] = {
            "execution": {"status": "executed",
                          "outcome": {"status": "sent", "from": "a"}}}
        repo.tasks["qe"] = executed
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        replayed = await service.request_tool_lock(
            tool="email", name="send_email", question="Enviar?",
            action={"to": ["a@x"]}, fingerprint="e",
            decision=self._decision(), project="jardal-openceo",
            origin_task_id="o", source_id="tool-lock:o:email:e",
        )
        self.assertEqual(replayed, {"status": "sent", "from": "a"})
        self.assertEqual(created, [])

    async def test_denied_reports_denial(self) -> None:
        repo = _LockRepo()
        denied = _lock_question(
            "qd", source="api", source_id="tool-lock:o:email:d")
        denied["params"]["owner_question"]["decision"] = {
            "decision": "denied"}
        repo.tasks["qd"] = denied
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        result = await service.request_tool_lock(
            tool="email", name="send_email", question="Enviar?",
            action={}, fingerprint="d", decision=self._decision(),
            project="jardal-openceo",
            source_id="tool-lock:o:email:d",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "denied")
        self.assertEqual(created, [])

    async def test_missing_project_is_invalid(self) -> None:
        repo = _LockRepo()
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        result = await service.request_tool_lock(
            tool="dns", name="dns_delete", question="Borrar?",
            action={}, fingerprint="f", decision=self._decision(),
            project="  ",
        )
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(created, [])

    async def test_concurrent_duplicate_replays_winner(self) -> None:
        # Review R1: the loser of the unique-index race replays the
        # winner instead of failing.
        from pymongo.errors import DuplicateKeyError

        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        real_create = service.create_task
        calls = {"n": 0}

        async def flaky_create(payload: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                winner = _lock_question(
                    "qw", source="api",
                    source_id="tool-lock:origin-1:dns:fp")
                repo.tasks["qw"] = winner
                raise DuplicateKeyError("winner took it")
            return await real_create(payload)

        service.create_task = flaky_create  # type: ignore[method-assign]
        try:
            pending = await service.request_tool_lock(
                tool="dns", name="dns_delete", question="Borrar?",
                action={"hostname": "x"}, fingerprint="fp",
                decision=self._decision(), project="jardal-openceo",
                origin_task_id="origin-1",
            )
        finally:
            service.create_task = real_create  # type: ignore[method-assign]
        self.assertTrue(is_lock_pending(pending))
        self.assertEqual(pending["question_task_id"], "qw")

    async def test_terminal_origin_stands_alone(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1", status="completed")
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        pending = await service.request_tool_lock(
            tool="dns", name="dns_delete", question="Borrar?",
            action={"hostname": "x"}, fingerprint="fp",
            decision=self._decision(), project="jardal-openceo",
            origin_task_id="origin-1",
        )
        self.assertTrue(is_lock_pending(pending))
        question = await repo.get_task(pending["question_task_id"])
        assert question is not None
        self.assertIsNone(
            question["params"]["owner_question"]["origin_task_id"])
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "completed")


class _FakeSmtp:
    def __init__(self, settings: Any) -> None:
        del settings
        self.sent: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(dict(kwargs))
        to = kwargs.get("to") or []
        return {
            "status": "sent",
            "from": kwargs.get("from_email") or "ops@jardal.app",
            "to": to if isinstance(to, list) else [to],
            "subject": kwargs.get("subject"),
            "message_id": f"<{len(self.sent)}@jardal.app>",
        }

    def _normalize_attachments(self, items: Any) -> list[Any]:
        from pathlib import Path
        from types import SimpleNamespace

        out: list[Any] = []
        for item in items or []:
            raw = item.get("path") if isinstance(item, dict) else item
            path = Path(str(raw))
            if not path.is_file():
                raise FileNotFoundError(f"Adjunto no encontrado: {raw}")
            out.append(SimpleNamespace(path=path, filename=path.name))
        return out


class _FakeDns:
    def __init__(self, settings: Any) -> None:
        del settings
        self.deleted: list[str] = []

    def delete(self, hostname: str) -> dict[str, Any]:
        self.deleted.append(hostname)
        return {"ok": True, "hostname": hostname, "deleted": 1}


class ToolSendEmailTests(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> dict[str, Any]:
        return {"secretary": {"allowed_recipients": ["@jardal.app"]}}

    async def test_allowed_recipient_sends(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            result = await service.tool_send_email(
                from_email=None, to=["a@jardal.app"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(created, [])
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "in_progress")

    async def test_allowed_send_runs_off_loop(self) -> None:
        import asyncio

        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender), \
                patch("asyncio.to_thread",
                      side_effect=asyncio.to_thread) as threaded:
            await service.tool_send_email(
                from_email=None, to=["a@jardal.app"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertTrue(threaded.await_count >= 1)

    async def test_off_list_escalates_without_sending(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            result = await service.tool_send_email(
                from_email=None, to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertTrue(is_lock_pending(result))
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(created), 1)

    async def test_origin_resolves_by_business_name(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", params={"_mirrors": {"erpnext": {
                "name": "TASK-2026-00999"}}})
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            result = await service.tool_send_email(
                from_email=None, to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", ticket="TASK-2026-00999",
            )
        self.assertTrue(is_lock_pending(result))
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "waiting")

    async def test_named_but_unresolvable_origin_is_invalid(self) -> None:
        # Review R1: silently orphaning a named origin would let the
        # run complete above the hold.
        repo = _LockRepo()
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        result = await service.tool_send_email(
            from_email=None, to=["x@evil.com"],
            subject="Hola", body_text="cuerpo",
            project="jardal-openceo", ticket="TASK-2026-00000",
        )
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(created, [])
        result = await service.tool_dns_delete(
            hostname="x", project="jardal-openceo",
            origin_task_id="ghost",
        )
        self.assertEqual(result["status"], "invalid_input")

    async def test_fingerprint_covers_full_action(self) -> None:
        # Review R1: same to/subject/body but different attachment or
        # sender must not replay the first outcome.
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            first = await service.tool_send_email(
                from_email="a@jardal.app", to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
            second = await service.tool_send_email(
                from_email="b@jardal.app", to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertNotEqual(first["question_task_id"],
                            second["question_task_id"])
        self.assertEqual(len(created), 2)

    async def test_fingerprint_splits_distribution(self) -> None:
        # Review 00008: the same set under another field is a different
        # approved action, not a replay.
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            first = await service.tool_send_email(
                from_email=None, to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
            second = await service.tool_send_email(
                from_email=None, to=[], bcc=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertNotEqual(first["question_task_id"],
                            second["question_task_id"])
        self.assertEqual(len(created), 2)

    async def test_question_shows_what_owner_approves(self) -> None:
        # Review R1: from, body, reply_to and attachments render.
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo, self._config())
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            pending = await service.tool_send_email(
                from_email="a@jardal.app", to=["x@evil.com"],
                subject="Hola", body_text="cuerpo secreto",
                reply_to="r@jardal.app",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        question = await repo.get_task(pending["question_task_id"])
        assert question is not None
        checkpoint = question["params"]["owner_question"]["checkpoint"]
        self.assertEqual(checkpoint["from"], "a@jardal.app")
        self.assertIn("cuerpo secreto", checkpoint["body"])
        self.assertEqual(checkpoint["reply_to"], "r@jardal.app")
        self.assertIn("cuerpo secreto", question["description"])


class ToolDnsDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_escalates(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        result = await service.tool_dns_delete(
            hostname="x.jardal.app", project="jardal-openceo",
            origin_task_id="origin-1",
        )
        self.assertTrue(is_lock_pending(result))
        self.assertEqual(len(created), 1)
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["waiting_reason"], "tool_lock:dns")


class ToolElfecPayTests(unittest.IsolatedAsyncioTestCase):
    async def test_pay_escalates(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = _lock_service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        result = await service.tool_elfec_pay(
            workflow={"id": "pago_elfec_house"},
            project="jardal-openceo", origin_task_id="origin-1",
        )
        self.assertTrue(is_lock_pending(result))
        self.assertEqual(len(created), 1)
        question = await repo.get_task(result["question_task_id"])
        assert question is not None
        held = lock_hold_action(question)
        assert held is not None
        self.assertEqual(held["tool"], "elfec")


class ApplyLockDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_sends_once_and_resumes(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:email",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1")
        question["params"]["owner_question"]["tool"] = {
            "name": "send_email",
            "action": {"from_email": None, "to": ["x@evil.com"],
                       "subject": "Hola", "body_text": "cuerpo"},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            result = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_lock_resume")
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(sender.sent[0]["to"], ["x@evil.com"])
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "queued")
        stored = await repo.get_task("q1")
        assert stored is not None
        held = lock_hold_action(stored)
        assert held is not None
        execution = held["execution"] or {}
        self.assertEqual(execution.get("status"), "executed")
        self.assertEqual(execution["outcome"]["status"], "sent")

    async def test_approve_rerun_replays_without_resending(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="in_progress", project="jardal-openceo")
        service = _lock_service(
            repo, {"secretary": {"allowed_recipients": []}})
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            pending = await service.tool_send_email(
                from_email=None, to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
            self.assertTrue(is_lock_pending(pending))
            service.set_dispatcher_wake(lambda: None)
            decided = await service.apply_owner_decision(
                pending["question_task_id"], approve=True,
                actor_id="owner", comment="ok")
            self.assertEqual(decided["route"], "owner_question_lock_resume")
            replayed = await service.tool_send_email(
                from_email=None, to=["x@evil.com"],
                subject="Hola", body_text="cuerpo",
                project="jardal-openceo", origin_task_id="origin-1",
            )
        self.assertEqual(replayed["status"], "sent")
        self.assertEqual(len(sender.sent), 1)

    async def test_second_approve_skips_execution(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:dns",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1", tool="dns", origin="origin-1")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns_delete",
            "action": {"hostname": "x.jardal.app"},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        manager = _FakeDns(None)
        with patch("tools.cloudflare.CloudflareDNSManager",
                   lambda settings: manager):
            first = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
            self.assertEqual(first["route"], "owner_question_lock_resume")
            # Crash between execution and resume: origin still waiting
            # on q1, execution recorded. The H3 retry resumes without
            # re-deleting.
            await repo.patch_task("origin-1", {
                "status": "waiting",
                "params": {"owner_question_id": "q1"},
            })
            retryable = await service._owner_question_retry_due(
                await repo.get_task("q1"))
            self.assertTrue(retryable)
            second = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="retry")
        self.assertEqual(second["route"], "owner_question_lock_resume")
        self.assertEqual(manager.deleted, ["x.jardal.app"])

    async def test_approve_without_origin_closes(self) -> None:
        repo = _LockRepo()
        question = _lock_question("q1", tool="dns", origin=None)
        question["params"]["owner_question"]["tool"] = {
            "name": "dns_delete",
            "action": {"hostname": "x.jardal.app"},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        manager = _FakeDns(None)
        with patch("tools.cloudflare.CloudflareDNSManager",
                   lambda settings: manager):
            result = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
        self.assertEqual(result["route"], "owner_question_lock_close")
        self.assertEqual(manager.deleted, ["x.jardal.app"])

    async def test_deny_fails_origin_with_bloqueado(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:elfec",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1", tool="elfec", origin="origin-1")
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_objective_service(MagicMock())
        service.set_dispatcher_wake(lambda: None)
        result = await service.apply_owner_decision(
            "q1", approve=False, actor_id="owner", comment="no pagues")
        self.assertEqual(result["route"], "owner_question_lock_deny")
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        verdict = (origin.get("params") or {}).get("agent_result") or {}
        self.assertEqual(verdict.get("resultado"), "bloqueado")

    async def test_execution_failure_holds_for_retry(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:email",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1")
        question["params"]["owner_question"]["tool"] = {
            "name": "send_email",
            "action": {"from_email": None, "to": ["x@evil.com"],
                       "subject": "Hola", "body_text": "cuerpo"},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)

        class _BoomSmtp(_FakeSmtp):
            def send_email(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("smtp down")

        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: _BoomSmtp(None)):
            result = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
        self.assertEqual(result["route"], "owner_question_lock_held")
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "waiting")
        stored = await repo.get_task("q1")
        assert stored is not None
        held = lock_hold_action(stored)
        assert held is not None
        self.assertIsNone(held["execution"])

    async def test_elfec_approval_runs_practice_leaf(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:elfec",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1", tool="elfec", origin="origin-1")
        question["params"]["owner_question"]["tool"] = {
            "name": "elfec_pay",
            "action": {"workflow": {"id": "pago_elfec_house"}},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        calls: list[dict[str, Any]] = []

        def fake_practice(settings: Any,
                          workflow: Any) -> dict[str, Any]:
            calls.append({"settings": settings, "workflow": workflow})
            return {"ok": True, "practice_mode": True,
                    "summary": "practica lista"}

        with patch("agents.finance.elfec.run_elfec_house_payment_practice",
                   fake_practice):
            result = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
        self.assertEqual(result["route"], "owner_question_lock_resume")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["workflow"], {"id": "pago_elfec_house"})

    async def test_stale_lock_snapshot_skips_execution(self) -> None:
        # Review R1: the decision re-reads the marker instead of
        # trusting the pre-record snapshot.
        from owner_channel import owner_answer_record

        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:dns",
            params={"owner_question_id": "q1"})
        question = _lock_question("q1", tool="dns", origin="origin-1")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns_delete",
            "action": {"hostname": "x.jardal.app"},
            "fingerprint": "fp",
        }
        question["params"]["owner_question"]["lock"] = {
            "execution": {"status": "executed",
                          "outcome": {"ok": True}}}
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        stale = {"tool": "dns", "name": "dns_delete",
                 "fingerprint": "fp",
                 "action": {"hostname": "x.jardal.app"},
                 "execution": None}
        manager = _FakeDns(None)
        with patch("tools.cloudflare.CloudflareDNSManager",
                   lambda settings: manager):
            route = await service._apply_lock_decision(
                dict(question), stale, origin_id="origin-1",
                approve=True,
                record=owner_answer_record(
                    approve=True, actor_id="owner", comment="ok",
                    decided_at="2026-10-12T12:00:00+00:00"),
                summary="s",
            )
        self.assertEqual(route, "owner_question_lock_resume")
        self.assertEqual(manager.deleted, [])

    async def test_first_approval_does_not_steal_second_hold(self) -> None:
        # Review R1: the origin re-parked under Q2 is not resumed by Q1.
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task(
            "origin-1", status="waiting",
            waiting_reason="tool_lock:elfec",
            params={"owner_question_id": "q2"})
        question = _lock_question("q1", tool="dns", origin="origin-1")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns_delete",
            "action": {"hostname": "x.jardal.app"},
            "fingerprint": "fp",
        }
        repo.tasks["q1"] = question
        service = _lock_service(repo)
        service.set_dispatcher_wake(lambda: None)
        manager = _FakeDns(None)
        with patch("tools.cloudflare.CloudflareDNSManager",
                   lambda settings: manager):
            result = await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="ok")
        self.assertEqual(result["route"], "owner_question_lock_close")
        self.assertEqual(manager.deleted, ["x.jardal.app"])
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "waiting")
        self.assertEqual(origin["params"]["owner_question_id"], "q2")


class AttachmentSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, repo: _LockRepo):
        return _lock_service(
            repo, {"secretary": {"allowed_recipients": []}})

    async def test_swap_between_request_and_approval_refuses(self) -> None:
        # Review R1 TOCTOU: what the Owner approved is what sends.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.txt"
            path.write_text("original", encoding="utf-8")
            repo = _LockRepo()
            repo.tasks["origin-1"] = _lock_task("origin-1")
            service = self._service(repo)
            created: list[dict[str, Any]] = []
            _stub_create_question(service, repo, created)
            sender = _FakeSmtp(None)
            with patch("tools.email_sender.SMTPEmailSender",
                       lambda settings: sender):
                pending = await service.tool_send_email(
                    from_email=None, to=["x@evil.com"],
                    subject="Hola", body_text="cuerpo",
                    attachments=[str(path)],
                    project="jardal-openceo", origin_task_id="origin-1",
                )
                self.assertTrue(is_lock_pending(pending))
                question = await repo.get_task(
                    pending["question_task_id"])
                assert question is not None
                hashes = (question["params"]["owner_question"]["tool"]
                          ["action"]["_attachment_hashes"])
                self.assertEqual(len(hashes), 1)
                path.write_text("swapped", encoding="utf-8")
                service.set_dispatcher_wake(lambda: None)
                result = await service.apply_owner_decision(
                    pending["question_task_id"], approve=True,
                    actor_id="owner", comment="ok")
            self.assertEqual(result["route"], "owner_question_lock_held")
            self.assertEqual(sender.sent, [])

    async def test_unchanged_attachments_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.txt"
            path.write_text("original", encoding="utf-8")
            repo = _LockRepo()
            repo.tasks["origin-1"] = _lock_task("origin-1")
            service = self._service(repo)
            created: list[dict[str, Any]] = []
            _stub_create_question(service, repo, created)
            sender = _FakeSmtp(None)
            with patch("tools.email_sender.SMTPEmailSender",
                       lambda settings: sender):
                pending = await service.tool_send_email(
                    from_email=None, to=["x@evil.com"],
                    subject="Hola", body_text="cuerpo",
                    attachments=[str(path)],
                    project="jardal-openceo", origin_task_id="origin-1",
                )
                service.set_dispatcher_wake(lambda: None)
                result = await service.apply_owner_decision(
                    pending["question_task_id"], approve=True,
                    actor_id="owner", comment="ok")
            self.assertEqual(result["route"], "owner_question_lock_resume")
            self.assertEqual(len(sender.sent), 1)

    async def test_transient_snapshot_error_fails_fast(self) -> None:
        # Review 00008: a silent {} would hold every approval forever.
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = self._service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)

        class _FlakySmtp(_FakeSmtp):
            def _normalize_attachments(self, items: Any) -> list[Any]:
                raise PermissionError("io flaky")

        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: _FlakySmtp(None)):
            with self.assertRaises(PermissionError):
                await service.tool_send_email(
                    from_email=None, to=["x@evil.com"],
                    subject="Hola", body_text="cuerpo",
                    attachments=["/tmp/whatever.txt"],
                    project="jardal-openceo", origin_task_id="origin-1",
                )
        self.assertEqual(created, [])

    async def test_missing_attachment_fails_fast(self) -> None:
        repo = _LockRepo()
        repo.tasks["origin-1"] = _lock_task("origin-1")
        service = self._service(repo)
        created: list[dict[str, Any]] = []
        _stub_create_question(service, repo, created)
        sender = _FakeSmtp(None)
        with patch("tools.email_sender.SMTPEmailSender",
                   lambda settings: sender):
            with self.assertRaises(FileNotFoundError):
                await service.tool_send_email(
                    from_email=None, to=["x@evil.com"],
                    subject="Hola", body_text="cuerpo",
                    attachments=["/nonexistent/doc.txt"],
                    project="jardal-openceo", origin_task_id="origin-1",
                )
        self.assertEqual(created, [])


class _ParkingGraph:
    """Simulates a run that calls a locked tool mid-run (MCP path)."""

    def __init__(self, repo: _LockRepo, outcome: dict[str, Any]) -> None:
        self._repo = repo
        self._outcome = outcome
        self.calls = 0

    async def execute(self, task: dict[str, Any],
                      agent_hint: str | None = None) -> dict[str, Any]:
        del agent_hint
        self.calls += 1
        await self._repo.patch_task(task["task_id"], {
            "status": "waiting",
            "waiting_reason": "tool_lock:dns",
            "params": {"owner_question_id": "q1",
                       "returns_from": "planner"},
        })
        return deepcopy(self._outcome)


class RunTaskLockFunnelTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, repo: _LockRepo, outcome: dict[str, Any]):
        from settings import Settings
        from task_service import TaskService

        return TaskService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=repo,  # type: ignore[arg-type]
            graph=_ParkingGraph(repo, outcome),  # type: ignore[arg-type]
            realtime_hub=_LockHub(),  # type: ignore[arg-type]
            notion=None,
            rate_monitor=None,
        )

    async def test_mid_run_park_wins_over_completion(self) -> None:
        # Explicit agent: these funnel tests predate the single planner;
        # since TASK-2026-00204 an undecided ticket on an unwired
        # service fails fast instead of running.
        repo = _LockRepo()
        repo.tasks["t1"] = _lock_task(
            "t1", status="queued", agent_hint="operations")
        outcome = {
            "selected_agent": "operations",
            "status": "completed",
            "steps": [],
            "last_response_summary": "agent hecho",
            "error": None,
            "params": {"agent_result": {"resultado": "hecho"}},
        }
        service = self._service(repo, outcome)
        service.set_dispatcher_wake(lambda: None)
        result = await service.run_task("t1")
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["waiting_reason"], "tool_lock:dns")
        self.assertEqual(result["params"]["owner_question_id"], "q1")
        self.assertEqual(
            result["params"]["agent_result"]["resultado"], "hecho")
        self.assertEqual(result["params"]["returns_from"], "planner")

    async def test_mid_run_park_wins_over_failure(self) -> None:
        repo = _LockRepo()
        repo.tasks["t1"] = _lock_task(
            "t1", status="queued", agent_hint="operations")
        outcome = {
            "selected_agent": "operations",
            "status": "failed",
            "steps": [],
            "last_response_summary": "agent bloqueado",
            "error": "locked",
            "params": {"agent_result": {"resultado": "bloqueado"}},
        }
        service = self._service(repo, outcome)
        service.set_dispatcher_wake(lambda: None)
        result = await service.run_task("t1")
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["waiting_reason"], "tool_lock:dns")
        self.assertIsNone(result.get("completed_at"))


class RunTaskStaleDiscardTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_mid_run_discards_stale_outcome(self) -> None:
        from settings import Settings
        from task_service import TaskService

        repo = _LockRepo()
        repo.tasks["t1"] = _lock_task(
            "t1", status="queued", agent_hint="operations")

        async def behavior(task: dict[str, Any]) -> dict[str, Any]:
            await repo.patch_task(task["task_id"], {
                "status": "queued",
                "waiting_reason": None,
                "params": {"owner_answer": {
                    "decision": "approved",
                    "at": "2026-10-12T12:00:01+00:00",
                }},
            })
            return {
                "selected_agent": "operations",
                "status": "failed",
                "steps": [],
                "last_response_summary": "stale bloqueado",
                "error": "locked",
            }

        class _Graph:
            async def execute(self, task: dict[str, Any],
                              agent_hint: str | None = None):
                del agent_hint
                return await behavior(task)

        service = TaskService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=repo,  # type: ignore[arg-type]
            graph=_Graph(),  # type: ignore[arg-type]
            realtime_hub=_LockHub(),  # type: ignore[arg-type]
            notion=None,
            rate_monitor=None,
        )
        service.set_dispatcher_wake(lambda: None)
        result = await service.run_task("t1")
        self.assertEqual(result["status"], "queued")
        stored = await repo.get_task("t1")
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertNotEqual(
            stored.get("last_response_summary"), "stale bloqueado")

    async def test_rerun_carrying_old_answer_still_lands(self) -> None:
        from settings import Settings
        from task_service import TaskService

        repo = _LockRepo()
        repo.tasks["t1"] = _lock_task(
            "t1", status="queued", agent_hint="operations",
            params={"owner_answer": {
                "decision": "approved",
                "at": "2026-10-12T12:00:00+00:00",
            }},
        )

        class _Graph:
            async def execute(self, task: dict[str, Any],
                              agent_hint: str | None = None):
                del task, agent_hint
                return {
                    "selected_agent": "operations",
                    "status": "completed",
                    "steps": [],
                    "last_response_summary": "hecho tras aprobar",
                    "error": None,
                }

        service = TaskService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=repo,  # type: ignore[arg-type]
            graph=_Graph(),  # type: ignore[arg-type]
            realtime_hub=_LockHub(),  # type: ignore[arg-type]
            notion=None,
            rate_monitor=None,
        )
        service.set_dispatcher_wake(lambda: None)
        result = await service.run_task("t1")
        self.assertEqual(result["status"], "completed")


class RegistryLockTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self, task_service: Any):
        from settings import Settings
        from workflow_service import WorkflowToolRegistry

        return WorkflowToolRegistry(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            task_service=task_service,
        )

    async def test_secretary_send_delegates_to_service(self) -> None:
        task_service = MagicMock()
        task_service.tool_send_email = AsyncMock(
            return_value={"status": "pending_approval"})
        registry = self._registry(task_service)
        result = await registry.call(
            "secretary.send_email",
            {"to": ["x@evil.com"], "subject": "s", "body_text": "b"},
            {"task": {"task_id": "t1", "project": "jardal-openceo"}},
        )
        self.assertEqual(result["status"], "pending_approval")
        kwargs = task_service.tool_send_email.await_args.kwargs
        self.assertEqual(kwargs["origin_task_id"], "t1")
        self.assertEqual(kwargs["project"], "jardal-openceo")

    async def test_secretary_send_without_service_is_unavailable(self) -> None:
        registry = self._registry(None)
        result = await registry.call(
            "secretary.send_email",
            {"to": ["x@evil.com"], "subject": "s", "body_text": "b"},
            {"task": {"task_id": "t1", "project": "jardal-openceo"}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")

    async def test_elfec_delegates_to_service(self) -> None:
        task_service = MagicMock()
        task_service.tool_elfec_pay = AsyncMock(
            return_value={"status": "pending_approval"})
        registry = self._registry(task_service)
        result = await registry.call(
            "finance.pago_elfec_house",
            {"workflow": {"id": "pago_elfec_house"}},
            {"task": {"task_id": "t1", "project": "jardal-openceo"}},
        )
        self.assertEqual(result["status"], "pending_approval")
        kwargs = task_service.tool_elfec_pay.await_args.kwargs
        self.assertEqual(kwargs["origin_task_id"], "t1")


class _EngineRepo(_LockRepo):
    def __init__(self) -> None:
        super().__init__()
        self.runs: dict[str, dict[str, Any]] = {}

    async def create_workflow_run(
        self, doc: dict[str, Any]
    ) -> dict[str, Any]:
        self.runs[doc["run_id"]] = deepcopy(doc)
        return deepcopy(doc)

    async def patch_workflow_run(
        self, run_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        run.update(deepcopy(updates))
        return deepcopy(run)


class EnginePendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_step_blocks_run_as_waiting(self) -> None:
        from settings import Settings
        from workflow_service import WorkflowService, WorkflowToolRegistry

        repo = _EngineRepo()
        repo.tasks["t1"] = _lock_task("t1", status="in_progress")
        registry = WorkflowToolRegistry(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
        )

        async def _held(**kwargs: Any) -> dict[str, Any]:
            return pending_lock_result(
                tool="elfec", question_task_id="q9",
                waiting_reason="tool_lock:elfec", detail="hold")

        registry.register("test.lock_tool", _held)
        engine = WorkflowService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=repo,  # type: ignore[arg-type]
            realtime_hub=_LockHub(),  # type: ignore[arg-type]
            tool_registry=registry,
        )
        workflow = {"workflow": "test.lock", "agent": "finance",
                    "steps": [{"id": "pay", "tool": "test.lock_tool",
                               "params": {}}]}
        outcome = await engine.execute_task_workflow(
            dict(repo.tasks["t1"]), workflow)
        self.assertEqual(outcome.status, "waiting")
        self.assertEqual(outcome.waiting_reason, "tool_lock:elfec")
        self.assertEqual(outcome.params.get("owner_question_id"), "q9")
        run = repo.runs[outcome.params["_workflow"]["run_id"]]
        self.assertEqual(run["status"], "blocked_tool")

    async def test_denied_step_fails_run(self) -> None:
        # Review R1: later steps must not act as if the locked
        # action had happened.
        from settings import Settings
        from workflow_service import WorkflowService, WorkflowToolRegistry

        repo = _EngineRepo()
        repo.tasks["t1"] = _lock_task("t1", status="in_progress")
        registry = WorkflowToolRegistry(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
        )

        async def _denied(**kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "status": "denied",
                    "reason": "El dueno denego esta accion."}

        registry.register("test.denied_tool", _denied)
        engine = WorkflowService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=repo,  # type: ignore[arg-type]
            realtime_hub=_LockHub(),  # type: ignore[arg-type]
            tool_registry=registry,
        )
        workflow = {"workflow": "test.deny", "agent": "finance",
                    "steps": [{"id": "pay", "tool": "test.denied_tool",
                               "params": {}}]}
        outcome = await engine.execute_task_workflow(
            dict(repo.tasks["t1"]), workflow)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("denego", outcome.error or "")


class SecretarySessionLockTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, sender: _FakeSmtp | None = None):
        from agents.secretary.session import SecretarySessionManager
        from settings import Settings

        sender = sender if sender is not None else _FakeSmtp(None)
        with patch("agents.secretary.session.SMTPEmailSender",
                   lambda settings: sender):
            manager = SecretarySessionManager(
                Settings(_env_file=None))  # type: ignore[call-arg]
        manager._mandate = lambda: _mandate()  # type: ignore[method-assign]
        return manager

    def _task(self, to: list[str]) -> dict[str, Any]:
        import json as _json

        return {
            "task_id": "t1",
            "title": "Enviar correo",
            "description": _json.dumps({
                "action": "send_email",
                "to": to,
                "subject": "Hola",
                "body_text": "cuerpo",
            }),
            "project": "jardal-openceo",
        }

    async def test_allowed_sends_off_loop(self) -> None:
        import asyncio

        sender = _FakeSmtp(None)
        manager = self._manager(sender)
        with patch("asyncio.to_thread",
                   side_effect=asyncio.to_thread) as threaded:
            output = await manager.run(
                self._task(["a@jardal.app"]),
                {"secretary": {"allowed_recipients": ["@jardal.app"]}},
            )
        self.assertEqual(output.status.value, "completed")
        self.assertTrue(threaded.await_count >= 1)
        self.assertEqual(len(sender.sent), 1)

    async def test_allowed_sends_without_service(self) -> None:
        sender = _FakeSmtp(None)
        manager = self._manager(sender)
        output = await manager.run(
            self._task(["a@jardal.app"]),
            {"secretary": {"allowed_recipients": ["@jardal.app"]}},
        )
        self.assertEqual(output.status.value, "completed")
        self.assertEqual(len(sender.sent), 1)

    async def test_off_list_waits_with_question(self) -> None:
        sender = _FakeSmtp(None)
        manager = self._manager(sender)
        service = MagicMock()
        service.tool_send_email = AsyncMock(return_value=pending_lock_result(
            tool="email", question_task_id="q3",
            waiting_reason="tool_lock:email", detail="hold"))
        manager.set_task_service(service)
        output = await manager.run(
            self._task(["x@evil.com"]),
            {"secretary": {"allowed_recipients": ["@jardal.app"]}},
        )
        self.assertEqual(output.status.value, "waiting")
        self.assertEqual(output.waiting_reason, "tool_lock:email")
        self.assertEqual(output.owner_question_id, "q3")
        self.assertEqual(sender.sent, [])

    async def test_off_list_without_service_fails_closed(self) -> None:
        sender = _FakeSmtp(None)
        manager = self._manager(sender)
        output = await manager.run(
            self._task(["x@evil.com"]),
            {"secretary": {"allowed_recipients": ["@jardal.app"]}},
        )
        self.assertEqual(output.status.value, "failed")
        self.assertIn("TaskService", output.error or "")
        self.assertEqual(sender.sent, [])

    async def test_denied_fails(self) -> None:
        manager = self._manager()
        service = MagicMock()
        service.tool_send_email = AsyncMock(return_value={
            "ok": False, "status": "denied", "reason": "no"})
        manager.set_task_service(service)
        output = await manager.run(
            self._task(["x@evil.com"]),
            {"secretary": {"allowed_recipients": []}},
        )
        self.assertEqual(output.status.value, "failed")
        self.assertEqual(output.error, "no")


class FinanceSessionLockTests(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> dict[str, Any]:
        return {"finance": {"workflows": [{
            "id": "pago_elfec_house",
            "name": "Pago Elfec House",
            "handler": "elfec_house_payment",
            "triggers": ["pago elfec house"],
        }]}}

    def _manager(self):
        from unittest.mock import Mock

        from agents.finance.session import FinanceSessionManager

        return FinanceSessionManager(Mock())

    async def test_elfec_waits_with_question(self) -> None:
        manager = self._manager()
        service = MagicMock()
        service.tool_elfec_pay = AsyncMock(return_value=pending_lock_result(
            tool="elfec", question_task_id="q4",
            waiting_reason="tool_lock:elfec", detail="hold"))
        manager.set_task_service(service)
        output = await manager.run(
            {"task_id": "t1", "title": "pago elfec house",
             "project": "jardal-openceo"},
            self._config(),
        )
        self.assertEqual(output.status.value, "waiting")
        self.assertEqual(output.waiting_reason, "tool_lock:elfec")
        self.assertEqual(output.owner_question_id, "q4")

    async def test_elfec_without_service_fails_closed(self) -> None:
        manager = self._manager()
        output = await manager.run(
            {"task_id": "t1", "title": "pago elfec house",
             "project": "jardal-openceo"},
            self._config(),
        )
        self.assertEqual(output.status.value, "failed")
        self.assertIn("TaskService", output.error or "")

    async def test_elfec_denied_fails(self) -> None:
        manager = self._manager()
        service = MagicMock()
        service.tool_elfec_pay = AsyncMock(return_value={
            "ok": False, "status": "denied", "reason": "no"})
        manager.set_task_service(service)
        output = await manager.run(
            {"task_id": "t1", "title": "pago elfec house",
             "project": "jardal-openceo"},
            self._config(),
        )
        self.assertEqual(output.status.value, "failed")


class GraphLockNodeTests(unittest.IsolatedAsyncioTestCase):
    # TASK-2026-00220 retired the secretary node; the secretary lock now
    # travels the generic agent path (see test_secretary_agent_220.py).
    def _graph(self, session: Any):
        from graph import OpenCEOGraph

        graph = OpenCEOGraph.__new__(OpenCEOGraph)
        graph._finance_session = session
        return graph

    def _state(self) -> dict[str, Any]:
        return {
            "task_id": "t1",
            "project": "jardal-openceo",
            "title": "T",
            "description": "D",
            "attachments": [],
            "priority": "medium",
            "project_config": {},
            "steps": [],
        }

    async def test_finance_waiting_passes_reason_and_params(self) -> None:
        from unittest.mock import Mock

        from agents.finance.session import FinanceExecutionOutput
        from state import TaskStatus

        session = Mock()
        session.run = AsyncMock(return_value=FinanceExecutionOutput(
            status=TaskStatus.WAITING,
            last_response_summary="hold",
            waiting_reason="tool_lock:elfec",
            owner_question_id="q4",
        ))
        out = await self._graph(session)._finance_node(self._state())  # type: ignore[arg-type]
        self.assertEqual(out["status"], "waiting")
        self.assertEqual(out["waiting_reason"], "tool_lock:elfec")
        self.assertEqual(out["params"], {"owner_question_id": "q4"})


class ValidateSecretaryAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _loader(self):
        from config_loader import ConfigLoader
        from settings import Settings

        return ConfigLoader(
            Settings(_env_file=None, PROJECTS_DIR=str(self.root)))  # type: ignore[call-arg]

    def _write(self, body: str) -> None:
        path = self.root / "proj"
        path.mkdir(parents=True, exist_ok=True)
        (path / "project.yaml").write_text(body, encoding="utf-8")

    def test_valid_allowlist_passes(self) -> None:
        self._write("dev:\n  default_provider: codex\n"
                    "secretary:\n  allowed_recipients:\n"
                    "    - '@jardal.app'\n    - 'a@x.com'\n")
        self.assertEqual(self._loader().validate_project("proj"), [])

    def test_absent_allowlist_passes(self) -> None:
        self._write("dev:\n  default_provider: codex\n")
        self.assertEqual(self._loader().validate_project("proj"), [])

    def test_malformed_shapes_reported(self) -> None:
        cases = [
            "dev:\n  default_provider: codex\nsecretary: no\n",
            "dev:\n  default_provider: codex\n"
            "secretary:\n  allowed_recipients: '@jardal.app'\n",
            "dev:\n  default_provider: codex\n"
            "secretary:\n  allowed_recipients:\n    - '  '\n",
            "dev:\n  default_provider: codex\n"
            "secretary:\n  allowed_recipients:\n    - 7\n",
        ]
        for body in cases:
            with self.subTest(body=body):
                self._write(body)
                errors = self._loader().validate_project("proj")
                self.assertEqual(len(errors), 1)
                self.assertIn("secretary", errors[0])

    def test_caps_reported(self) -> None:
        entries = "\n".join(f"    - 'a{i}@x.com'" for i in range(129))
        self._write("dev:\n  default_provider: codex\n"
                    f"secretary:\n  allowed_recipients:\n{entries}\n")
        errors = self._loader().validate_project("proj")
        self.assertEqual(len(errors), 1)
        self._write("dev:\n  default_provider: codex\n"
                    "secretary:\n  allowed_recipients:\n"
                    f"    - '{'a' * 250}@x.com'\n")
        errors = self._loader().validate_project("proj")
        self.assertEqual(len(errors), 1)


class LockableBindingTests(unittest.IsolatedAsyncioTestCase):
    async def _grant(self, box: set[str]):
        from catalog import AgentCard, AgentType
        from mcp_identity import MemoryTokenStore, TokenIssuer

        store = MemoryTokenStore()
        card = AgentCard.model_validate({
            "name": "operations",
            "type": AgentType.AI,
            "engine": "muse-code",
            "profile": None,
            "boundary_tools": sorted(box),
            "skills_dir": "skills/operations",
            "skills": [],
            "inputs": [],
            "outputs": [],
            "enabled": True,
            "disabled_reason": None,
            "decided_by_ticket": None,
        })
        issuer = TokenIssuer(store, {"operations": card})
        token = await issuer.issue_agent(
            "operations", "TASK-2026-00208", "proj")
        return await issuer.authenticate(token)

    async def test_delete_binds_own_ticket_and_project(self) -> None:
        from mcp_identity import McpDenied

        grant = await self._grant({"manage_dns_record"})
        grant.check("manage_dns_record", {
            "hostname": "x", "action": "delete",
            "ticket": "TASK-2026-00208", "project": "proj"})
        with self.assertRaises(McpDenied):
            grant.check("manage_dns_record", {
                "hostname": "x", "action": "delete",
                "ticket": "TASK-2026-00999", "project": "proj"})
        with self.assertRaises(McpDenied):
            grant.check("manage_dns_record", {
                "hostname": "x", "action": "delete",
                "ticket": "TASK-2026-00208", "project": "other"})
        with self.assertRaises(McpDenied):
            grant.check("manage_dns_record",
                        {"hostname": "x", "action": "delete"})

    async def test_create_keeps_box_rule(self) -> None:
        grant = await self._grant({"manage_dns_record"})
        grant.check("manage_dns_record", {"hostname": "x"})
        grant.check("manage_dns_record",
                    {"hostname": "x", "action": "create"})

    async def test_email_and_elfec_bind_identity(self) -> None:
        from mcp_identity import McpDenied

        grant = await self._grant(
            {"openceo_send_email", "openceo_elfec_house_payment"})
        grant.check("openceo_send_email", {
            "to": ["a@x"], "subject": "s", "body_text": "b",
            "ticket": "TASK-2026-00208", "project": "proj"})
        grant.check("openceo_elfec_house_payment", {
            "ticket": "TASK-2026-00208", "project": "proj"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_send_email", {
                "to": ["a@x"], "subject": "s", "body_text": "b"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_elfec_house_payment", {})

    async def test_service_badges_cannot_call_lockables(self) -> None:
        # Review R1: service badges carry no ticket to park.
        from mcp_identity import (
            McpDenied,
            MemoryTokenStore,
            TokenIssuer,
        )

        store = MemoryTokenStore()
        issuer = TokenIssuer(store, {})
        token = await issuer.issue_service(
            "closer", "proj", ("manage_dns_record",
                               "openceo_send_email"))
        grant = await issuer.authenticate(token)
        with self.assertRaises(McpDenied):
            grant.check("manage_dns_record", {
                "hostname": "x", "action": "delete",
                "project": "proj"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_send_email", {
                "to": ["a@x"], "subject": "s", "body_text": "b",
                "project": "proj"})
        grant.check("manage_dns_record",
                    {"hostname": "x", "project": "proj"})


class McpLockDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, task_service: Any):
        from fastmcp import Client, FastMCP

        from mcp_server import _register_infra_tools, _register_openceo_tools
        from settings import Settings

        server: Any = FastMCP("locks-test")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        _register_infra_tools(server, settings, task_service)
        _register_openceo_tools(server, settings, task_service, None)
        return Client(server)

    def _fake_service(self) -> Any:
        service = MagicMock()
        service.tool_dns_delete = AsyncMock(return_value={
            "ok": True, "status": "pending_approval"})
        service.tool_send_email = AsyncMock(return_value={
            "ok": True, "status": "pending_approval"})
        service.tool_elfec_pay = AsyncMock(return_value={
            "ok": True, "status": "pending_approval"})
        return service

    async def test_delete_delegates_to_service(self) -> None:
        service = self._fake_service()
        async with await self._client(service) as client:
            result = await client.call_tool(
                "manage_dns_record",
                {"hostname": "x.jardal.app", "action": "delete",
                 "project": "proj", "ticket": "TASK-2026-00208"})
        self.assertEqual(result.data["status"], "pending_approval")
        kwargs = service.tool_dns_delete.await_args.kwargs
        self.assertEqual(kwargs["hostname"], "x.jardal.app")
        self.assertEqual(kwargs["ticket"], "TASK-2026-00208")

    async def test_delete_without_service_is_unavailable(self) -> None:
        from fastmcp import Client, FastMCP

        from mcp_server import _register_infra_tools
        from settings import Settings

        server: Any = FastMCP("locks-test")
        _register_infra_tools(
            server, Settings(_env_file=None), None)  # type: ignore[call-arg]
        async with Client(server) as client:
            result = await client.call_tool(
                "manage_dns_record",
                {"hostname": "x.jardal.app", "action": "delete"})
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["status"], "unavailable")

    async def test_email_and_elfec_delegate_to_service(self) -> None:
        service = self._fake_service()
        async with await self._client(service) as client:
            email = await client.call_tool(
                "openceo_send_email",
                {"to": ["a@x"], "subject": "s", "body_text": "b",
                 "ticket": "TASK-2026-00208"})
            elfec = await client.call_tool(
                "openceo_elfec_house_payment", {})
        self.assertEqual(email.data["status"], "pending_approval")
        self.assertEqual(elfec.data["status"], "pending_approval")
        self.assertEqual(
            service.tool_send_email.await_args.kwargs["ticket"],
            "TASK-2026-00208")


class AnonymousLockHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_cannot_name_origin_ticket(self) -> None:
        import httpx
        from fastmcp import FastMCP

        from catalog import load_catalog
        from mcp_identity import (
            MCPIdentityMiddleware,
            MemoryAuditSink,
            MemoryTokenStore,
            TokenIssuer,
        )
        from mcp_server import (
            _register_infra_tools,
            build_mounted_mcp_server,
        )
        from settings import Settings

        server: Any = FastMCP("locks-anon-test")
        service = MagicMock()
        service.tool_dns_delete = AsyncMock(return_value={
            "ok": True, "status": "pending_approval"})
        _register_infra_tools(
            server, Settings(_env_file=None), service)  # type: ignore[call-arg]
        issuer = TokenIssuer(MemoryTokenStore(), load_catalog().agents)
        audit = MemoryAuditSink()
        server.add_middleware(MCPIdentityMiddleware(issuer, audit))
        mount = build_mounted_mcp_server(
            path="/internal/mcp",
            name="locks-anon-test",
            server=server,
            bearer_token=None,
            stateless_http=True,
            json_response=True,
            authenticator=issuer,
        )
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "manage_dns_record",
                              "arguments": {
                                  "hostname": "x.jardal.app",
                                  "action": "delete",
                                  "project": "jardal-openceo",
                                  "ticket": "TASK-2026-00208"}}}
        transport = httpx.ASGITransport(app=mount.app)  # type: ignore[arg-type]
        async with mount.lifespan():
            async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://t") as client:
                response = await client.post("/", json=payload, headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                })
        self.assertEqual(response.status_code, 200)
        self.assertIn("isError", response.text)
        self.assertIn("cannot name an origin ticket", response.text)
        service.tool_dns_delete.assert_not_awaited()
