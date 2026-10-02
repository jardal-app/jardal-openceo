"""Pinned library/helper checks without a live session."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from importlib.metadata import version

import research_support
from research.social import SocialError
from research.social_x import XReader, TWSCRAPE_VERSION


class XHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_helper_output_is_read_to_eof(self):
        import json
        from types import SimpleNamespace
        stream = asyncio.StreamReader()
        proc = SimpleNamespace(stdout=stream, stdin=SimpleNamespace(
            write=lambda _body: None, drain=AsyncMock(), close=lambda: None), returncode=0,
            wait=AsyncMock(return_value=0))
        async def emit():
            stream.feed_data(b'{"ok": true, "items": [')
            await asyncio.sleep(0.01)
            stream.feed_data(json.dumps({"text": "one complete post"}).encode() + b']}')
            stream.feed_eof()
        task = asyncio.create_task(emit())
        with patch("research.social_x.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            rows = await XReader().read(path=Path("/synthetic.db"), operation="search", source="test", limit=5)
        await task
        self.assertEqual(rows, [{"text": "one complete post"}])

    async def test_helper_output_limit_is_enforced_before_parse(self):
        from types import SimpleNamespace
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100_001)
        stream.feed_eof()
        proc = SimpleNamespace(stdout=stream, stdin=SimpleNamespace(
            write=lambda _body: None, drain=AsyncMock(), close=lambda: None), returncode=0,
            wait=AsyncMock(return_value=0))
        with patch("research.social_x.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(SocialError, "source_output_limit"):
                await XReader().read(path=Path("/synthetic.db"), operation="search", source="test", limit=5)

    async def test_versioned_empty_pool_is_bounded_and_secret_free(self):
        from importlib.metadata import PackageNotFoundError
        try:
            installed = version("twscrape")
        except PackageNotFoundError:
            installed = None
        if installed != TWSCRAPE_VERSION:
            self.skipTest("optional pinned library not installed")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "accounts.db"
            from twscrape import API
            api = API(str(path), raise_when_no_account=True, wait_timeout=0)
            self.assertEqual(await api.pool.get_all(), [])
            os.chmod(path, 0o600)
            async with asyncio.timeout(5):
                with self.assertRaisesRegex(SocialError, "single_account_required"):
                    await XReader().read(path=path, operation="search", source="from:test", limit=5)

    async def test_no_other_operation_exposed(self):
        for operation in ("publish", "dm", "bookmarks", "feed"):
            with self.assertRaisesRegex(SocialError, "invalid_source"):
                await XReader().read(path=Path("/not/read"), operation=operation, source="test", limit=5)
