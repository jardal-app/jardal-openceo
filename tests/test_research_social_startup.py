"""The X broker is optional at startup: a bad session directory turns off only that route.

Before, a 0755 directory, a symlink in the path or a path inside the repo/vault raised
inside the lifespan and the whole core (voice, tickets, Horizon) failed to start because
of a feature that is off by default.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import research_support
from research.wiring import build_social_broker


class _Collection:
    async def create_index(self, *args, **kwargs):
        return "ok"


def _db(accounts=None):
    return SimpleNamespace(research_social_accounts=accounts or _Collection(),
                           research_social_grants=_Collection(), research_social_artifacts=_Collection())


def _settings(**overrides):
    values = dict(RESEARCH_SOCIAL_SESSION_DIR="", RESEARCH_WORKSPACE_DIR="", RESEARCH_EVIDENCE_DIR="")
    values.update(overrides)
    return SimpleNamespace(**values)


class SocialStartup(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_is_simply_off(self):
        self.assertEqual(await build_social_broker(_settings(), _db()), (None, "not_configured"))

    async def test_valid_directory_builds_the_broker_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "x-sessions"
            broker, reason = await build_social_broker(_settings(RESEARCH_SOCIAL_SESSION_DIR=str(target)), _db())
            self.assertEqual(reason, "ready")
            self.assertIsNotNone(broker)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    async def test_permissive_directory_disables_only_social(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "open"
            target.mkdir()
            os.chmod(target, 0o755)
            result = await build_social_broker(_settings(RESEARCH_SOCIAL_SESSION_DIR=str(target)), _db())
            self.assertEqual(result, (None, "unsafe_session_directory"))

    async def test_directory_inside_repo_workspace_evidence_or_vault_is_rejected_before_creation(self):
        repo_root = Path(research_support.CORE_DIR).parent
        with tempfile.TemporaryDirectory() as root:
            workspace, evidence = Path(root) / "workspace", Path(root) / "evidence"
            for target in (repo_root / "tmp" / "never-created-x", workspace / "x", evidence / "x",
                           Path(root) / "vault" / "x"):
                with self.subTest(target=str(target)):
                    result = await build_social_broker(_settings(
                        RESEARCH_SOCIAL_SESSION_DIR=str(target), RESEARCH_WORKSPACE_DIR=str(workspace),
                        RESEARCH_EVIDENCE_DIR=str(evidence)), _db())
                    self.assertEqual(result, (None, "session_dir_not_isolated"))
                    self.assertFalse(target.exists())

    async def test_initialization_failure_never_raises(self):
        class Broken(_Collection):
            async def create_index(self, *args, **kwargs):
                raise RuntimeError("index conflict")
        with tempfile.TemporaryDirectory() as root:
            result = await build_social_broker(
                _settings(RESEARCH_SOCIAL_SESSION_DIR=str(Path(root) / "x")), _db(accounts=Broken()))
            self.assertEqual(result, (None, "social_init_failed"))


if __name__ == "__main__":
    unittest.main()
