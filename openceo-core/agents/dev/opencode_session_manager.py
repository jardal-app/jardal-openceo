from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from agents.dev.opencode_paths import resolve_opencode_cli_path
from settings import Settings

logger = logging.getLogger(__name__)

_DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")


class OpenCodeSessionManager:
    """Create OpenCode sessions for QA review via ``opencode run``.

    Launches the CLI with the full QA context, captures the session ID
    from the first JSON event, kills the subprocess, then removes any
    partial assistant response from SQLite so the web UI shows the session
    as "ready for user input" instead of stuck in a loading state.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def _resolve_cli_path(self) -> str:
        return resolve_opencode_cli_path(self._settings.OPENCODE_CLI_PATH)

    async def create_qa_session(
        self,
        work_dir: str,
        title: str,
        context_prompt: str,
        model: str = "opencode-go/kimi-k2.6",
    ) -> dict[str, Any]:
        """Create an OpenCode QA session.

        1. Run ``opencode run`` with the full context prompt.
        2. Capture sessionID from the first JSON line.
        3. Kill the process immediately.
        4. Remove any partial assistant messages from SQLite so the
           session loads cleanly in the web UI.
        """
        cli_path = self._resolve_cli_path()
        session_title = title[:180] if title else "QA Review"

        command = [
            cli_path, "run", "--format", "json",
            "-m", model,
            "--dir", work_dir,
            "--title", session_title,
            "--agent", "build",
            context_prompt,
        ]

        from orchestration.agent_sandbox import (
            cleanup_sandbox_scratch, plan_agent,
        )
        overlay = {
            "OPENCODE_GO_API_KEY": self._settings.OPENCODE_GO_API_KEY or os.environ.get("OPENCODE_GO_API_KEY", ""),
        }
        # The QA session must land in the live opencode state: rw carve-out.
        plan = plan_agent(argv=command, workspace=work_dir,
                          base=os.environ, overlay=overlay,
                          config_args=[cli_path], opencode_state_rw=True)

        logger.info("Creating OpenCode QA session: work_dir=%s title=%s", work_dir, session_title)

        process = await asyncio.create_subprocess_exec(
            *plan.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=plan.cwd,
            env=plan.env,
        )

        session_id: str | None = None
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="replace"))
                    sid = str(event.get("sessionID") or "").strip()
                    if sid:
                        session_id = sid
                        break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        except asyncio.TimeoutError:
            logger.warning("OpenCode session bootstrap timed out")
        finally:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=10)
            except Exception:  # noqa: BLE001, S110
                pass
            cleanup_sandbox_scratch(work_dir)

        if not session_id:
            raise ValueError("Failed to extract sessionID from OpenCode output")

        # Remove partial assistant response so web UI doesn't show loading forever
        self._clear_assistant(session_id)

        url = self._build_session_url(work_dir, session_id)
        logger.info("OpenCode QA session created: session_id=%s url=%s", session_id, url)

        return {"session_id": session_id, "url": url}

    # ------------------------------------------------------------------
    # SQLite — only DELETE, no fragile INSERTs
    # ------------------------------------------------------------------

    def _clear_assistant(self, session_id: str) -> None:
        """Delete any assistant message + parts created before the kill."""
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.execute(
                "DELETE FROM part WHERE session_id=? AND message_id IN "
                "(SELECT id FROM message WHERE session_id=? AND json_extract(data, '$.role')!='user')",
                (session_id, session_id),
            )
            conn.execute(
                "DELETE FROM message WHERE session_id=? AND json_extract(data, '$.role')!='user'",
                (session_id,),
            )
            conn.commit()
            conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to clear assistant for %s: %s", session_id, e)

    def _build_session_url(self, work_dir: str, session_id: str) -> str:
        encoded = base64.urlsafe_b64encode(work_dir.encode()).decode().rstrip("=")
        base = self._settings.OPENCEO_WEB_BASE_URL or "https://opencode.jardal.app"
        base = base.rstrip("/")
        return f"{base}/{encoded}/session/{session_id}"
