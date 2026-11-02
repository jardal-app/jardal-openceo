"""Claude role runs leave a content-free trace (TASK-2026-00443).

Contract fixed here: the executor binds a per-run dir over the claude
``projects/`` dirs and stores only counts from the transcript (tools,
executables, turns, deduplicated tokens, effort); arguments, file contents
and tool output never reach the run metrics.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration import session_trace  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402

SECRET = "sk-live-do-not-store"


def _assistant(message_id: str, block: dict, tokens: int = 10) -> str:
    return json.dumps({
        "type": "assistant", "requestId": f"req-{message_id}",
        "effort": "max",
        "message": {"id": message_id, "model": "claude-opus-5-5",
                    "content": [block],
                    "usage": {"input_tokens": 1, "output_tokens": tokens,
                              "cache_read_input_tokens": 100,
                              "cache_creation_input_tokens": 5}}})


class CommandHeadTests(unittest.TestCase):
    def test_heads_keep_executables_and_runner_subcommands_only(self) -> None:
        cases = {
            "cd apps/web && pnpm vitest run --reporter=dot":
                ["cd", "pnpm vitest"],
            f"TOKEN={SECRET} curl -H x https://h": ["curl"],
            "python3 manage.py test": ["python3"],
            "node script.js": ["node"],
            "git log -5 | head -n 3": ["git log", "head"],
            "./node_modules/.bin/tsc -b": ["tsc"],
            "Weird$cmd arg": ["?"],
        }
        for command, heads in cases.items():
            with self.subTest(command=command):
                self.assertEqual(session_trace.command_heads(command), heads)


class SummaryTests(unittest.TestCase):
    def test_counts_tools_dedupes_usage_and_drops_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp)
            (trace / "proj").mkdir()
            lines = [
                _assistant("m1", {"type": "thinking", "thinking": SECRET}),
                _assistant("m1", {"type": "tool_use", "name": "Bash",
                                  "input": {"command": "pnpm test --x"}}),
                _assistant("m1", {"type": "tool_use", "name": "Read",
                                  "input": {"file_path": "/a/.env"}}),
                json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": SECRET}]}}),
                _assistant("m2", {"type": "text", "text": SECRET}, 7),
                "not json",
            ]
            (trace / "proj" / "s.jsonl").write_text(
                "\n".join(lines), encoding="utf-8")
            summary = session_trace.summarize(trace)
        self.assertEqual(summary["turns"], 2)
        self.assertEqual(summary["tools"], {"Bash": 1, "Read": 1})
        self.assertEqual(summary["commands"], {"pnpm test": 1})
        self.assertEqual(summary["tokens"]["output_tokens"], 17)
        self.assertEqual(summary["tokens"]["cache_read_input_tokens"], 200)
        self.assertEqual(summary["effort"], ["max"])
        self.assertNotIn(SECRET, json.dumps(summary))
        self.assertNotIn(".env", json.dumps(summary))

    def test_no_transcript_means_no_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(session_trace.summarize(Path(tmp)), {})

    def test_prepare_binds_existing_projects_dirs_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, work = Path(tmp) / "home", Path(tmp) / "work"
            (home / ".claude-j" / "projects").mkdir(parents=True)
            work.mkdir()
            trace, ops = session_trace.prepare(work, home)
            self.assertTrue(trace.is_dir())
            self.assertTrue(trace.is_relative_to(work / ".openceo" / "sandbox"))
            self.assertEqual(ops, [["--bind", str(trace),
                                    str(home / ".claude-j" / "projects")]])


class ExecutorWiringTests(unittest.IsolatedAsyncioTestCase):
    async def _extra_ops(self, provider: str) -> list:
        spec = {"provider": provider, "account": "x", "model": "m",
                "command": ["true"], "headless_args": [],
                "prompt_via": "argv"}
        executor = ProcessExecutor(roles={"planner": spec})
        with tempfile.TemporaryDirectory() as tmp, \
                patch("orchestration.live.plan_agent",
                      side_effect=ValueError("stop")) as planned:
            await executor._run_once(  # noqa: SLF001
                "planner", "p", Path(tmp), 5)
        return planned.call_args.kwargs["extra_ops"]

    async def test_claude_roles_bind_a_trace_dir(self) -> None:
        ops = await self._extra_ops("claude")
        self.assertTrue(ops)
        self.assertTrue(all(op[0] == "--bind" and "/trace-" in op[1]
                            and op[2].endswith("/projects") for op in ops))

    async def test_other_providers_run_untraced(self) -> None:
        self.assertEqual(await self._extra_ops("codex"), [])

    async def test_trace_summary_reaches_run_usage(self) -> None:
        import os
        from types import SimpleNamespace
        spec = {"provider": "claude", "account": "claudej", "model": "opus",
                "command": ["true"], "headless_args": [],
                "prompt_via": "argv"}
        executor = ProcessExecutor(roles={"arbiter": spec})
        with tempfile.TemporaryDirectory() as tmp:
            fake = SimpleNamespace(argv=["true"], cwd=tmp,
                                   env=dict(os.environ))
            with patch("orchestration.live.plan_agent", return_value=fake), \
                    patch("orchestration.live.session_trace.summarize",
                          return_value={"turns": 3}):
                result = await executor._run_once(  # noqa: SLF001
                    "arbiter", "p", Path(tmp), 5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.usage["trace"], {"turns": 3})
