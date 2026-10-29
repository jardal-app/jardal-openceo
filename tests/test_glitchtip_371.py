"""GlitchTip autoalojado y SDK (TASK-2026-00371, diseno v2 ap. 16.1-16.3).

Sin red, sin DSN real, sin sentry_sdk instalado: el SDK se simula con
un modulo falso y el compose se verifica de forma estatica.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

REPO = Path(__file__).resolve().parents[1]

from reliability import error_tracking as tracking  # noqa: E402


def _install_fake_sentry(capture_result: str = "evt-1",
                         capture_error: Exception | None = None):
    calls: dict = {}

    module = types.ModuleType("sentry_sdk")

    def fake_init(**kwargs):
        calls.update(kwargs)

    def fake_capture():
        if capture_error is not None:
            raise capture_error
        return capture_result

    module.init = fake_init
    module.capture_exception = fake_capture

    integrations = types.ModuleType("sentry_sdk.integrations")
    asyncio_mod = types.ModuleType("sentry_sdk.integrations.asyncio")

    class AsyncioIntegration:
        pass

    asyncio_mod.AsyncioIntegration = AsyncioIntegration
    module.integrations = integrations
    return module, integrations, asyncio_mod, calls


class GlitchtipInitTests(unittest.TestCase):
    def setUp(self) -> None:
        tracking._ACTIVE = False
        self._saved = {
            name: sys.modules.get(name) for name in
            ("sentry_sdk", "sentry_sdk.integrations",
             "sentry_sdk.integrations.asyncio")
        }
        self._env = mock.patch.dict("os.environ", {}, clear=False)
        self._env.start()
        import os
        os.environ.pop(tracking.GLITCHTIP_DSN_ENV, None)
        os.environ.pop(tracking.ENV_ENV, None)

    def tearDown(self) -> None:
        self._env.stop()
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        tracking._ACTIVE = False

    def _use_fake(self, **kwargs):
        module, integrations, asyncio_mod, calls = _install_fake_sentry(
            **kwargs)
        sys.modules["sentry_sdk"] = module
        sys.modules["sentry_sdk.integrations"] = integrations
        sys.modules["sentry_sdk.integrations.asyncio"] = asyncio_mod
        return calls

    def test_without_dsn_is_disabled(self) -> None:
        calls = self._use_fake()
        self.assertFalse(
            tracking.init_error_tracking(release="abc", environment="t"))
        self.assertFalse(tracking.is_active())
        self.assertEqual(calls, {})

    def test_blank_dsn_is_disabled(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "   "
        calls = self._use_fake()
        self.assertFalse(tracking.init_error_tracking(release="abc"))
        self.assertEqual(calls, {})

    def test_without_sdk_is_disabled(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "https://k@host/1"
        sys.modules["sentry_sdk"] = None
        sys.modules.pop("sentry_sdk.integrations", None)
        sys.modules.pop("sentry_sdk.integrations.asyncio", None)
        self.assertFalse(tracking.init_error_tracking(release="abc"))
        self.assertFalse(tracking.is_active())

    def test_init_options_match_design(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "https://k@host/1"
        calls = self._use_fake()
        self.assertTrue(tracking.init_error_tracking(
            release={"sha": "deadbeef", "dirty": False},
            environment="test"))
        self.assertTrue(tracking.is_active())
        self.assertEqual(calls["dsn"], "https://k@host/1")
        self.assertEqual(calls["release"], "deadbeef")
        self.assertEqual(calls["environment"], "test")
        self.assertFalse(calls["send_default_pii"])
        self.assertFalse(calls["include_local_variables"])
        self.assertEqual(calls["max_request_body_size"], "never")
        self.assertIs(calls["before_send"], tracking.scrub_sentry_event)
        self.assertEqual(len(calls["integrations"]), 1)

    def test_release_string_and_default_environment(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "https://k@host/1"
        os.environ.pop(tracking.ENV_ENV, None)
        calls = self._use_fake()
        self.assertTrue(tracking.init_error_tracking(release="cafe01"))
        self.assertEqual(calls["release"], "cafe01")
        self.assertEqual(calls["environment"], "production")

    def test_init_failure_stays_disabled(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "not-a-valid-dsn"
        module, integrations, asyncio_mod, _ = _install_fake_sentry()

        def bad_init(**kwargs):
            raise ValueError("Bad DSN")

        module.init = bad_init
        sys.modules["sentry_sdk"] = module
        sys.modules["sentry_sdk.integrations"] = integrations
        sys.modules["sentry_sdk.integrations.asyncio"] = asyncio_mod
        self.assertFalse(tracking.init_error_tracking(release="r"))
        self.assertFalse(tracking.is_active())


class ScrubTests(unittest.TestCase):
    def test_redacts_text_and_drops_identity(self) -> None:
        event = {
            "message": "boom for a@b.com with sk-abcdefgh1234",
            "user": {"id": "u1", "email": "a@b.com"},
            "request": {
                "url": "https://h/x?token=abc",
                "headers": {"Auth": "x"},
                "cookies": {"s": "y"},
                "data": {"k": "v"},
            },
            "exception": {"values": [{
                "value": "dsn=https://k@sentry/x",
                "stacktrace": {"frames": [
                    {"vars": {"secret": 1}, "function": "f"}]}}]},
            "breadcrumbs": {"values": [
                {"message": "login with password=abc", "data": {"k": "v"}}]},
            "contexts": {"user": {"id": "u"}, "trace": {"id": "t"}},
        }
        out = tracking.scrub_sentry_event(event)
        assert out is not None
        self.assertNotIn("user", out)
        self.assertNotIn("a@b.com", out["message"])
        self.assertNotIn("sk-abcdefgh1234", out["message"])
        self.assertNotIn("headers", out["request"])
        self.assertNotIn("cookies", out["request"])
        self.assertNotIn("data", out["request"])
        self.assertNotIn("k@sentry", out["exception"]["values"][0]["value"])
        frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertNotIn("vars", frame)
        self.assertNotIn("password=abc",
                         out["breadcrumbs"]["values"][0]["message"])
        self.assertNotIn("data", out["breadcrumbs"]["values"][0])
        self.assertNotIn("user", out["contexts"])
        self.assertIn("trace", out["contexts"])

    def test_non_dict_event_is_dropped(self) -> None:
        self.assertIsNone(tracking.scrub_sentry_event("boom"))  # type: ignore[arg-type]

    def test_before_send_accepts_sdk_hint_argument(self) -> None:
        event = {"message": "boom", "user": {"id": "u1"}}
        out = tracking.scrub_sentry_event(event, {"exc": "ValueError"})
        assert out is not None
        self.assertNotIn("user", out)

    def test_context_line_is_scrubbed(self) -> None:
        event = {"exception": {"values": [{
            "value": "boom",
            "stacktrace": {"frames": [{
                "vars": {"secret": 1},
                "context_line": "connect(password=s3cret)",
                "pre_context": ["ok"],
            }]}}]}}
        out = tracking.scrub_sentry_event(event, None)
        assert out is not None
        frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertNotIn("s3cret", frame["context_line"])
        self.assertNotIn("vars", frame)


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        tracking._ACTIVE = False
        self._saved = {
            name: sys.modules.get(name) for name in
            ("sentry_sdk", "sentry_sdk.integrations",
             "sentry_sdk.integrations.asyncio")
        }
        self._patcher = mock.patch.dict("os.environ", {}, clear=False)
        self._patcher.start()
        import os
        os.environ.pop(tracking.GLITCHTIP_DSN_ENV, None)

    def tearDown(self) -> None:
        self._patcher.stop()
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        tracking._ACTIVE = False

    def test_inactive_returns_none(self) -> None:
        self.assertIsNone(tracking.capture_exception())

    def test_active_returns_event_id(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "https://k@host/1"
        module, integrations, asyncio_mod, _ = _install_fake_sentry(
            capture_result="glitch-9")
        sys.modules["sentry_sdk"] = module
        sys.modules["sentry_sdk.integrations"] = integrations
        sys.modules["sentry_sdk.integrations.asyncio"] = asyncio_mod
        self.assertTrue(tracking.init_error_tracking(release="r"))
        self.assertEqual(tracking.capture_exception(), "glitch-9")

    def test_sdk_failure_never_raises(self) -> None:
        import os
        os.environ[tracking.GLITCHTIP_DSN_ENV] = "https://k@host/1"
        module, integrations, asyncio_mod, _ = _install_fake_sentry(
            capture_error=RuntimeError("down"))
        sys.modules["sentry_sdk"] = module
        sys.modules["sentry_sdk.integrations"] = integrations
        sys.modules["sentry_sdk.integrations.asyncio"] = asyncio_mod
        self.assertTrue(tracking.init_error_tracking(release="r"))
        self.assertIsNone(tracking.capture_exception())


class ComposeTests(unittest.TestCase):
    def test_compose_structure_is_deployable(self) -> None:
        import yaml
        compose = yaml.safe_load(
            (REPO / "deploy" / "glitchtip" / "compose.yml").read_text())
        services = compose["services"]
        db_env = services["db"]["environment"]
        self.assertEqual(
            db_env["POSTGRES_PASSWORD"], "${GLITCHTIP_DB_PASSWORD}")
        for name in ("migrate", "web", "worker"):
            url = services[name]["environment"]["DATABASE_URL"]
            self.assertIn("${GLITCHTIP_DB_PASSWORD}", url)
            self.assertNotIn("$${", url)
        self.assertEqual(
            services["migrate"]["command"], "./bin/run-migrate.sh")
        self.assertEqual(
            services["worker"]["command"], "./bin/run-celery-with-beat.sh")
        db_volumes = services["db"]["volumes"]
        self.assertTrue(
            any(str(v).endswith(":/var/lib/postgresql") for v in db_volumes))
        for name, service in services.items():
            self.assertNotIn("ports", service, name)

    def test_compose_is_pinned_internal_and_retained(self) -> None:
        text = (REPO / "deploy" / "glitchtip" / "compose.yml").read_text()
        self.assertIn("postgres:18", text)
        self.assertIn("valkey/valkey:9", text)
        self.assertIn("glitchtip/glitchtip:6", text)
        self.assertIn("GLITCHTIP_MAX_EVENT_LIFE_DAYS: \"90\"", text)
        self.assertIn("glitchtip.env", text)
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("ports:", text)
        self.assertIn("internal: true", text)

    def test_readme_declares_internal_only_access(self) -> None:
        text = (REPO / "deploy" / "glitchtip" / "README.md").read_text()
        self.assertIn("127.0.0.1", text)
        self.assertIn("SSH", text)

    def test_no_real_secrets_in_repo(self) -> None:
        sample = (REPO / "deploy" / "glitchtip" / ".env.example").read_text()
        self.assertIn("cambiar", sample)
        self.assertFalse(
            (REPO / "deploy" / "glitchtip" / "glitchtip.env").exists())


if __name__ == "__main__":
    unittest.main()
