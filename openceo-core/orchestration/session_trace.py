"""Content-free trace of a claude role session (TASK-2026-00443).

The sandbox mounts the claude config dirs read-only, so the CLI never kept a
transcript and a run left no record of what it did. A per-run directory is
bound over each ``<config>/projects``; after the run the transcript shrinks
to counts (tools, executables, turns, tokens, effort) before the run's
scratch is removed, so no file content or tool output is stored.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from orchestration.agent_sandbox import ensure_sandbox_scratch

#: Claude config dirs (claude, claudej, claudep) whose ``projects/`` the CLI
#: writes transcripts into.
CLAUDE_CONFIG_DIRS = (".claude", ".claude-j", ".claude-deepseek")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens")
_HEAD = re.compile(r"^[a-z][a-z0-9._-]*$")
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:_-]*$")
#: Runners whose subcommand says what ran (``pnpm test``, ``git log``).
_RUNNERS = frozenset({"pnpm", "npm", "npx", "yarn", "bun", "git", "uv",
                      "make", "cargo", "go", "python", "python3", "node"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")


def prepare(workdir: Path, home: Path) -> tuple[Path, list[list[str]]]:
    """Fresh trace dir and the bwrap ops binding it over each projects dir."""
    trace = ensure_sandbox_scratch(workdir) / f"trace-{uuid.uuid4().hex}"
    trace.mkdir(mode=0o700)
    ops = [["--bind", str(trace), str(home / name / "projects")]
           for name in CLAUDE_CONFIG_DIRS
           if (home / name / "projects").is_dir()]
    return trace, ops


def command_heads(command: str) -> list[str]:
    """Executables of a shell command (a runner keeps its subcommand).

    Arguments, paths and ``VAR=value`` words never survive: the trace must
    not carry secrets or file names.
    """
    heads = []
    for segment in _SEGMENT_SPLIT.split(str(command or "")):
        words = [word for word in segment.split() if "=" not in word]
        if not words:
            continue
        head = Path(words[0]).name
        if not _HEAD.match(head):
            heads.append("?")
        elif (head in _RUNNERS and len(words) > 1
              and _SUBCOMMAND.match(words[1])):
            heads.append(f"{head} {words[1]}")
        else:
            heads.append(head)
    return heads


def summarize(trace: Path) -> dict[str, Any]:
    """Counts of the transcripts under ``trace``; ``{}`` when none exist."""
    tools: Counter[str] = Counter()
    commands: Counter[str] = Counter()
    usage_by_message: dict[str, dict[str, int]] = {}
    models: set[str] = set()
    efforts: set[str] = set()
    for path in sorted(trace.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            # One line per content block: usage repeats per message id.
            message_id = str(message.get("id") or entry.get("requestId")
                             or len(usage_by_message))
            usage = message.get("usage")
            if isinstance(usage, dict):
                usage_by_message[message_id] = {
                    key: int(usage.get(key) or 0) for key in _USAGE_KEYS}
            if message.get("model"):
                models.add(str(message["model"]))
            if entry.get("effort"):
                efforts.add(str(entry["effort"]))
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "?")
                tools[name] += 1
                if name == "Bash":
                    command = (block.get("input") or {}).get("command", "")
                    commands.update(command_heads(command))
    if not usage_by_message and not tools:
        return {}
    return {
        "turns": len(usage_by_message),
        "tools": dict(tools.most_common()),
        "commands": dict(commands.most_common(20)),
        "tokens": {key: sum(item[key] for item in usage_by_message.values())
                   for key in _USAGE_KEYS},
        "models": sorted(models),
        "effort": sorted(efforts),
    }
