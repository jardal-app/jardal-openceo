"""Integration gate over protected routes, anti-Goodhart (design v2 §9.4).

Targeted unit tests with ERP fakes, a synthetic git repo and stubbed
orchestrators. No network, no CLI, no LLM (epic TASK-2026-00365,
ticket TASK-2026-00389).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import protected_routes as routes  # noqa: E402
from reliability.config import (  # noqa: E402
    RELIABILITY_DEFAULTS,
    validate_reliability_config,
)

FILES = ["openceo-core/reliability/guard.py", "openceo-core/main.py"]
DIGEST = routes.diff_digest(FILES, b"fake-diff-bytes")


def _future(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _live_approval(digest: str = DIGEST, verdict: str = "approved",
                   actor: str = "human") -> dict[str, Any]:
    return {
        "id": "apr-diff-1",
        "status": verdict,
        "resource": {"id": routes.approval_resource_for(digest)},
        "version": 0,
        "decision": {"verdict": verdict,
                     "actor": {"kind": actor, "id": "owner-1"}},
        "expires_at": _future(),
    }


class FakeApprovals:
    """Injectable ERP read surface; counts reads, optional failure."""

    def __init__(self, live: Any = None, *, fail: bool = False) -> None:
        self.live = live
        self.fail = fail
        self.reads = 0

    async def get_approval(self, identifier: str) -> dict[str, Any]:
        self.reads += 1
        if self.fail:
            raise RuntimeError("ERP down")
        assert identifier == "apr-diff-1"
        return self.live


class FakeAdapter(FakeApprovals):
    """Request + read surface used by the orchestrator seams."""

    def __init__(self, live: Any = None, *, fail: bool = False,
                 request_fail: bool = False) -> None:
        super().__init__(live, fail=fail)
        self.request_fail = request_fail
        self.requests = 0

    async def request_approval(
            self, request: dict[str, Any], *, key: str) -> dict[str, Any]:
        self.requests += 1
        if self.request_fail:
            raise RuntimeError("ERP down")
        assert request["resource"].endswith(DIGEST) or DIGEST[:16] in key
        return {"id": "apr-diff-1", "status": "pending"}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class ProtectedListTests(unittest.TestCase):
    def test_list_is_versioned_and_covers_design(self) -> None:
        self.assertEqual(routes.LIST_VERSION, 1)
        for entry in ("openceo-core/reliability/",
                      "openceo-core/reliability/config.py",
                      "openceo-core/reliability/protected_routes.py",
                      "openceo-core/objectives/agent_result.py",
                      "openceo-core/agents/dev/tier_s/models.py",
                      "openceo-core/orchestration/review.py",
                      "openceo-core/orchestration/provider_recovery.py",
                      "openceo-core/company_mandate.py",
                      "projects/*/project.yaml",
                      "automatico.py"):
            self.assertIn(entry, routes.PROTECTED_ROUTES)

    def test_gate_off_by_default_and_config_valid(self) -> None:
        self.assertIn("routes_gate", RELIABILITY_DEFAULTS)
        self.assertFalse(routes.routes_gate_enabled())
        self.assertEqual(validate_reliability_config(RELIABILITY_DEFAULTS),
                         [])

    def test_matching_shapes(self) -> None:
        self.assertEqual(
            routes.touched_protected(
                ["openceo-core/reliability/guard.py"]),
            ["openceo-core/reliability/"])
        self.assertEqual(
            routes.touched_protected(
                ["openceo-core/agents/dev/tier_s/models.py"]),
            ["openceo-core/agents/dev/tier_s/models.py"])
        self.assertEqual(
            routes.touched_protected(
                ["projects/jardal-openceo/project.yaml"]),
            ["projects/*/project.yaml"])
        self.assertEqual(routes.touched_protected(["automatico.py"]),
                         ["automatico.py"])
        self.assertEqual(routes.touched_protected(
            ["openceo-react/src/app.tsx", "docs/work/x.md"]), [])
        self.assertEqual(routes.touched_protected(None), [])
        self.assertEqual(routes.touched_protected("nope"), [])

    def test_digest_stable_and_content_sensitive(self) -> None:
        self.assertEqual(routes.diff_digest(FILES, b"fake-diff-bytes"),
                         DIGEST)
        self.assertEqual(
            routes.diff_digest(list(reversed(FILES)), b"fake-diff-bytes"),
            DIGEST)
        self.assertNotEqual(routes.diff_digest(FILES, b"other"), DIGEST)
        self.assertNotEqual(
            routes.diff_digest(["openceo-core/main.py"], b"fake-diff-bytes"),
            DIGEST)

    def test_approval_resource_and_human_action(self) -> None:
        self.assertEqual(routes.approval_resource_for(DIGEST),
                         f"reliability:diff:{DIGEST}")
        action = routes.human_action_for(
            DIGEST, ["openceo-core/reliability/", "automatico.py"])
        self.assertTrue(action.startswith(f"Aprobar el diff {DIGEST}, "
                                          "que modifica "))
        self.assertIn("openceo-core/reliability/", action)
        self.assertIn("automatico.py", action)

    def test_request_builds_idempotent_key(self) -> None:
        request, key = routes.build_approval_request(
            DIGEST, ["automatico.py"])
        self.assertEqual(request["resource"],
                         f"reliability:diff:{DIGEST}")
        self.assertIn(DIGEST[:16], key)


class CheckIntegrationTests(unittest.TestCase):
    def test_outside_list_allows_without_reads(self) -> None:
        approvals = FakeApprovals(_live_approval())
        verdict = _run(routes.check_integration(
            ["openceo-react/src/app.tsx"], b"diff", approvals=approvals))
        self.assertEqual(
            verdict, {"allowed": True, "reason": "no_protected_routes",
                      "routes": []})
        self.assertEqual(approvals.reads, 0)

    def test_protected_without_approval_blocks(self) -> None:
        verdict = _run(routes.check_integration(
            FILES, b"fake-diff-bytes", approvals=FakeApprovals()))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "no_approval")
        self.assertEqual(verdict["digest"], DIGEST)
        self.assertEqual(verdict["human_action"],
                         f"Aprobar el diff {DIGEST}, que modifica "
                         "openceo-core/reliability/")

    def test_other_hash_does_not_authorize(self) -> None:
        other = "0" * 64
        approvals = FakeApprovals(_live_approval(digest=other))
        verdict = _run(routes.check_integration(
            FILES, b"fake-diff-bytes", approval_id="apr-diff-1",
            approvals=approvals))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "stale_approval")

    def test_matching_human_approval_allows(self) -> None:
        approvals = FakeApprovals(_live_approval())
        verdict = _run(routes.check_integration(
            FILES, b"fake-diff-bytes", approval_id="apr-diff-1",
            approvals=approvals))
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approved")
        self.assertEqual(approvals.reads, 1)

    def test_agent_decision_expired_and_unreadable_block(self) -> None:
        for live, reason in (
                (_live_approval(actor="agent"), "agent_decision"),
                ({**_live_approval(),
                  "expires_at": (datetime.now(timezone.utc)
                                 - timedelta(days=1)).isoformat()},
                 "approval_expired"),
                (_live_approval(verdict="denied"), "approval_not_approved")):
            verdict = _run(routes.check_integration(
                FILES, b"fake-diff-bytes", approval_id="apr-diff-1",
                approvals=FakeApprovals(live)))
            self.assertFalse(verdict["allowed"])
            self.assertEqual(verdict["reason"], reason)
        verdict = _run(routes.check_integration(
            FILES, b"fake-diff-bytes", approval_id="apr-diff-1",
            approvals=FakeApprovals(_live_approval(), fail=True)))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_unverifiable")
        verdict = _run(routes.check_integration(
            FILES, b"fake-diff-bytes", approval_id="apr-diff-1",
            approvals=None))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_unverifiable")

    def test_request_never_raises(self) -> None:
        self.assertIsNone(_run(routes.request_diff_approval(
            None, DIGEST, ["automatico.py"])))
        self.assertIsNone(_run(routes.request_diff_approval(
            FakeAdapter(request_fail=True), DIGEST, ["automatico.py"])))
        created = _run(routes.request_diff_approval(
            FakeAdapter(_live_approval()), DIGEST, ["automatico.py"]))
        self.assertEqual(created, {"id": "apr-diff-1",
                                   "status": "pending"})


class CollectDiffTests(unittest.TestCase):
    def test_collect_diff_on_synthetic_repo(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = str(Path(tmp))
            env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": tmp,
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "init", "-q"], cwd=repo, env=env,
                           check=True)
            target = Path(tmp) / "openceo-core" / "reliability"
            target.mkdir(parents=True)
            (target / "guard.py").write_text("v1\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo,
                           env=env, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  env=env, capture_output=True,
                                  text=True, check=True).stdout.strip()
            (target / "guard.py").write_text("v2\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, env=env,
                           check=True)
            subprocess.run(["git", "commit", "-qm", "v2"], cwd=repo,
                           env=env, check=True)
            files, diff = routes.collect_diff(repo, base, "HEAD")
            self.assertEqual(files, ["openceo-core/reliability/guard.py"])
            self.assertIn(b"v2", diff)
            self.assertEqual(routes.touched_protected(files),
                             ["openceo-core/reliability/"])


class FakeRoutesModule:
    """Stand-in for reliability.protected_routes behind orchestrator seams."""

    def __init__(self, *, enabled=True, touched=(),
                 allowed=True, fail_check=False) -> None:
        self._enabled = enabled
        self._touched = list(touched)
        self._allowed = allowed
        self._fail = fail_check
        self.collections = 0
        self.requests = 0

    def routes_gate_enabled(self) -> bool:
        return self._enabled

    def touched_protected(self, files: Any) -> list[str]:
        return list(self._touched)

    def collect_diff(self, repo: str, base: str, head: str) -> Any:
        self.collections += 1
        return (["openceo-core/reliability/guard.py"], b"diff-bytes")

    def diff_digest(self, files: Any, diff: bytes) -> str:
        return DIGEST

    async def request_diff_approval(
            self, adapter: Any, digest: str, touched: list[str],
            *, business_ref: str | None = None) -> dict[str, Any] | None:
        self.requests += 1
        if adapter is None:
            return None
        return {"id": "apr-diff-1"}

    async def check_integration(
            self, files: Any, diff: bytes, *, approval_id: Any = None,
            approvals: Any = None) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("boom")
        if self._allowed:
            return {"allowed": True, "reason": "approved",
                    "digest": DIGEST, "routes": self._touched,
                    "approval_id": "apr-diff-1"}
        return {"allowed": False, "reason": "no_approval",
                "digest": DIGEST, "routes": self._touched,
                "human_action": f"Aprobar el diff {DIGEST}, que modifica "
                                f"{', '.join(self._touched)}"}


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(repo="/tmp/repo", base_sha="base",
                           final_sha="head", ref="TASK-2026-00001",
                           ticket={"params": {}})


def _tier_s_check(cycle: Any, fake: FakeRoutesModule, adapter: Any,
                  files: Any = FILES) -> Any:
    import reliability
    from agents.dev.tier_s import cycle as tier_s_cycle

    with unittest.mock.patch.object(
            reliability, "protected_routes", fake):
        return asyncio.run(
            tier_s_cycle.TierSCycle._protected_routes_check(
                cycle, _ctx(), list(files), "head"))


def _new_cycle(adapter: Any) -> Any:
    from agents.dev.tier_s import cycle as tier_s_cycle

    cycle = tier_s_cycle.TierSCycle.__new__(tier_s_cycle.TierSCycle)
    cycle._routes_gate_adapter = adapter
    return cycle


class TierSSeamTests(unittest.TestCase):
    def test_off_skips_without_git_or_erp(self) -> None:
        fake = FakeRoutesModule(enabled=False)
        cycle = _new_cycle(FakeAdapter(_live_approval()))
        self.assertIsNone(_tier_s_check(cycle, fake, None))
        self.assertEqual(fake.collections, 0)

    def test_untouched_allows_without_reads(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=[])
        adapter = FakeAdapter(_live_approval())
        self.assertIsNone(_tier_s_check(_new_cycle(adapter), fake, adapter))
        self.assertEqual(adapter.requests, 0)

    def test_approved_diff_allows(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=[
            "openceo-core/reliability/"], allowed=True)
        adapter = FakeAdapter(_live_approval())
        self.assertIsNone(_tier_s_check(_new_cycle(adapter), fake, adapter))
        self.assertEqual(fake.requests, 1)

    def test_protected_diff_parks_with_human_action(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=[
            "openceo-core/reliability/"], allowed=False)
        adapter = FakeAdapter(_live_approval())
        detail = _tier_s_check(_new_cycle(adapter), fake, adapter)
        self.assertIn("protected_routes: Aprobar el diff", detail)
        self.assertIn(DIGEST, detail)
        self.assertIn("apr-diff-1", detail)

    def test_no_adapter_blocks_fail_closed(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=["automatico.py"],
                                allowed=False)
        detail = _tier_s_check(_new_cycle(None), fake, None)
        self.assertIn("protected_routes: Aprobar el diff", detail)
        self.assertNotIn("apr-diff-1", detail)

    def test_check_error_blocks_merge(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=[
            "openceo-core/reliability/"], fail_check=True)
        detail = _tier_s_check(
            _new_cycle(FakeAdapter(_live_approval())), fake, None)
        self.assertEqual(detail, "protected_routes: gate error, "
                                 "merge withheld")


def _session_check(manager: Any, fake: FakeRoutesModule,
                   adapter: Any) -> Any:
    import reliability
    from agents.dev import session as dev_session

    branch = SimpleNamespace(repo="/tmp/repo", base="main",
                             branch="auto/1-x", base_sha="base",
                             ref="TASK-2026-00001")
    with unittest.mock.patch.object(
            reliability, "protected_routes", fake):
        return asyncio.run(
            dev_session.DevSessionManager._protected_routes_check(
                manager, branch, list(FILES), "head", "TASK-2026-00001"))


def _new_manager(adapter: Any) -> Any:
    from agents.dev import session as dev_session

    manager = dev_session.DevSessionManager.__new__(
        dev_session.DevSessionManager)
    manager._routes_gate_adapter = adapter
    return manager


class SessionSeamTests(unittest.TestCase):
    def test_off_and_untouched_allow(self) -> None:
        manager = _new_manager(FakeAdapter(_live_approval()))
        self.assertIsNone(_session_check(
            manager, FakeRoutesModule(enabled=False),
            manager._routes_gate_adapter))
        untouched = FakeRoutesModule(enabled=True, touched=[])
        self.assertIsNone(_session_check(manager, untouched,
                                         manager._routes_gate_adapter))

    def test_blocked_waits_with_human_action(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=["automatico.py"],
                                allowed=False)
        detail = _session_check(
            _new_manager(FakeAdapter(_live_approval())), fake, None)
        self.assertIn("protected_routes: Aprobar el diff", detail)
        self.assertIn("automatico.py", detail)

    def test_approved_allows(self) -> None:
        fake = FakeRoutesModule(enabled=True, touched=["automatico.py"],
                                allowed=True)
        adapter = FakeAdapter(_live_approval())
        self.assertIsNone(_session_check(_new_manager(adapter), fake,
                                         adapter))


class AutomaticoGateTests(unittest.TestCase):
    def test_off_applies_nothing(self) -> None:
        import automatico

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {"phase": "close", "note": "",
                                  "base": {"openceo": "abc"}}
        status = automatico.routes_gate_status(child, ticket)
        self.assertFalse(status["applies"])
        self.assertEqual(status["reason"], "gate_off")
        self.assertEqual(automatico.routes_gate_close_entry(child, ticket),
                         "")
        self.assertNotIn("routes_verdict", ticket)
        self.assertEqual(automatico.routes_gate_verify(child, ticket),
                         (True, ""))
        self.assertEqual(automatico.routes_gate_prompt(child, ticket), [])

    def test_close_entry_blocks_then_prompt_parks(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {"phase": "close", "note": "",
                                  "base": {"openceo": "abc"}}
        status = {"applies": True, "reason": "protected",
                  "touched": ["automatico.py"], "digest": DIGEST, "files": 1}
        with mock.patch.object(automatico, "routes_gate_status",
                               return_value=status), \
            mock.patch.object(automatico, "routes_gate_approval_io",
                               return_value=None):
            note = automatico.routes_gate_close_entry(child, ticket)
        self.assertIn("NO hagas merge ni push", note)
        self.assertIn("TICKET_OWNER", note)
        self.assertEqual(ticket["routes_digest"], DIGEST)
        self.assertNotIn("routes_approval_id", ticket)
        prompt = automatico.routes_gate_prompt(child, ticket)
        self.assertEqual(len(prompt), 1)
        self.assertIn("SIN aprobación verificada", prompt[0])
        self.assertIn(f"Aprobar el diff {DIGEST}", prompt[0])
        module = sys.modules["reliability.protected_routes"] \
            if "reliability.protected_routes" in sys.modules else routes
        with unittest.mock.patch.object(
                module, "collect_diff",
                return_value=(sorted(FILES), b"fake-diff-bytes")), \
            mock.patch.object(automatico, "_routes_gate_enabled",
                              return_value=True):
            ok, why = automatico.routes_gate_verify(child, ticket)
        self.assertFalse(ok)
        self.assertIn("SIN APROBACIÓN VERIFICADA".lower(),
                      why.lower())

    def test_close_entry_allowed_then_verify_passes(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {"phase": "close", "note": "",
                                  "base": {"openceo": "abc"}}
        status = {"applies": True, "reason": "protected",
                  "touched": ["automatico.py"], "digest": DIGEST, "files": 1}
        live = _live_approval()
        module = sys.modules["reliability.protected_routes"] \
            if "reliability.protected_routes" in sys.modules else routes

        def fake_io(op: str, payload: dict) -> dict | None:
            if op == "request":
                return {"id": "apr-diff-1", "status": "pending"}
            return live

        with mock.patch.object(automatico, "routes_gate_status",
                               return_value=status), \
            mock.patch.object(automatico, "routes_gate_approval_io",
                               side_effect=fake_io), \
            mock.patch.object(module, "verify_approval",
                               wraps=module.verify_approval):
            note = automatico.routes_gate_close_entry(child, ticket)
            self.assertIn("verificada por el runner", note)
            self.assertEqual(ticket["routes_approval_id"], "apr-diff-1")
            prompt = automatico.routes_gate_prompt(child, ticket)
            self.assertIn("con aprobación apr-diff-1 verificada", prompt[0])
            # integrated diff matches the approved digest: green
            with mock.patch.object(
                    module, "collect_diff",
                    return_value=(sorted(FILES), b"fake-diff-bytes")), \
                mock.patch.object(automatico, "_routes_gate_enabled",
                                  return_value=True):
                self.assertEqual(automatico.routes_gate_verify(child, ticket),
                                 (True, ""))

    def test_verify_rejects_drift(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {
            "phase": "close", "note": "", "base": {"openceo": "abc"},
            "routes_verdict": {"digest": DIGEST, "routes": ["automatico.py"],
                               "approval_id": "apr-diff-1",
                               "human_action": "Aprobar el diff x"},
            "routes_digest": DIGEST, "routes_approval_id": "apr-diff-1"}
        module = sys.modules["reliability.protected_routes"] \
            if "reliability.protected_routes" in sys.modules else routes
        with mock.patch.object(
                module, "collect_diff",
                return_value=(["automatico.py"], b"changed-bytes")), \
            mock.patch.object(automatico, "_routes_gate_enabled",
                              return_value=True):
            ok, why = automatico.routes_gate_verify(child, ticket)
        self.assertFalse(ok)
        self.assertIn("deriva", why)

    def test_verify_without_verdict_blocks_protected(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {"phase": "close", "note": "",
                                  "base": {"openceo": "abc"}}
        module = sys.modules["reliability.protected_routes"] \
            if "reliability.protected_routes" in sys.modules else routes
        with mock.patch.object(
                module, "collect_diff",
                return_value=(["automatico.py"], b"late-protected-bytes")), \
            mock.patch.object(automatico, "_routes_gate_enabled",
                              return_value=True):
            ok, why = automatico.routes_gate_verify(child, ticket)
        self.assertFalse(ok)
        self.assertIn("Aprobar el diff", why)
        self.assertIn("automatico.py", why)

    def test_verify_without_verdict_allows_unprotected(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {"phase": "close", "note": "",
                                  "base": {"openceo": "abc"}}
        module = sys.modules["reliability.protected_routes"] \
            if "reliability.protected_routes" in sys.modules else routes
        with mock.patch.object(
                module, "collect_diff",
                return_value=(["openceo-core/main.py"], b"plain-bytes")), \
            mock.patch.object(automatico, "_routes_gate_enabled",
                              return_value=True):
            self.assertEqual(automatico.routes_gate_verify(child, ticket),
                             (True, ""))

    def test_verify_gate_off_allows_despite_verdict(self) -> None:
        import automatico
        import unittest.mock as mock

        child = {"ticket": "TASK-2026-00389", "branch": "auto/x",
                 "workspace": Path("/x")}
        ticket: dict[str, Any] = {
            "phase": "close", "note": "", "base": {"openceo": "abc"},
            "routes_verdict": {"digest": DIGEST, "routes": ["automatico.py"],
                               "approval_id": "apr-diff-1",
                               "human_action": "Aprobar el diff x"}}
        self.assertFalse(RELIABILITY_DEFAULTS.get("routes_gate", {}).get(
            "enabled", False))
        with mock.patch.object(automatico, "_routes_gate_enabled",
                               return_value=False):
            self.assertEqual(automatico.routes_gate_verify(child, ticket),
                             (True, ""))


if __name__ == "__main__":
    unittest.main()

