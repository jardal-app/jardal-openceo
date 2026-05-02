from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_SHELL_DONE_PREFIX = "OPENCEO_OPENCODE_DONE::"


@dataclass
class OpenCodeParsedOutput:
    session_id: str | None
    final_text: str
    raw_text: str
    error_text: str | None


def build_run_command(
    base_command: list[str],
    prompt: str | None,
    cwd: str | None = None,
    session_id: str | None = None,
    title: str | None = None,
    agent_name: str | None = None,
    command_name: str | None = None,
) -> list[str]:
    if not base_command:
        raise ValueError("OpenCode base command is empty")
    if not command_name and not (prompt or "").strip():
        raise ValueError("OpenCode run requires a prompt or command name")

    command = [base_command[0], "run", "--format", "json", *base_command[1:]]
    if cwd:
        command.extend(["--dir", cwd])
    if session_id:
        command.extend(["--session", session_id])
    elif title:
        command.extend(["--title", title])
    if agent_name:
        command.extend(["--agent", agent_name])
    if command_name:
        command.extend(["--command", command_name])
    if prompt:
        command.append(prompt)
    return command


def build_run_shell_command(
    base_command: list[str],
    prompt: str | None,
    cwd: str | None = None,
    session_id: str | None = None,
    title: str | None = None,
    agent_name: str | None = None,
    command_name: str | None = None,
) -> tuple[str, str]:
    sentinel = f"{_SHELL_DONE_PREFIX}{uuid4().hex[:12]}"
    command = build_run_command(
        base_command=base_command,
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        title=title,
        agent_name=agent_name,
        command_name=command_name,
    )

    shell_command = (
        f"{shlex.join(command)}; "
        f"printf '\\n{sentinel}:%s\\n' \"$?\""
    )
    return shell_command, sentinel


def parse_run_output(content: str) -> OpenCodeParsedOutput:
    session_id: str | None = None
    text_parts: list[str] = []
    error_parts: list[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not session_id:
            candidate = str(event.get("sessionID") or "").strip()
            if candidate:
                session_id = candidate

        event_type = str(event.get("type") or "").strip().lower()
        part = event.get("part") or {}
        if not isinstance(part, dict):
            part = {}

        if not session_id:
            candidate = str(part.get("sessionID") or "").strip()
            if candidate:
                session_id = candidate

        if event_type == "error":
            error = event.get("error") or {}
            message = ""
            if isinstance(error, dict):
                data = error.get("data") or {}
                if isinstance(data, dict):
                    message = str(data.get("message") or "").strip()
                if not message:
                    message = str(error.get("message") or error.get("name") or "").strip()
            else:
                message = str(error).strip()
            if message:
                error_parts.append(message)
            continue

        if event_type != "text":
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type != "text":
            continue
        text = str(part.get("text") or "").strip()
        if text:
            text_parts.append(text)

    final_text = text_parts[-1] if text_parts else ""
    raw_text = "\n".join(text_parts).strip()
    return OpenCodeParsedOutput(
        session_id=session_id,
        final_text=final_text,
        raw_text=raw_text,
        error_text="\n".join(error_parts).strip() or None,
    )


def extract_response_sentinel_exit_code(content: str, sentinel: str) -> int | None:
    if not content or not sentinel:
        return None
    match = re.search(rf"{re.escape(sentinel)}:(\d+)", content)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def has_response_sentinel(content: str, sentinel: str) -> bool:
    return extract_response_sentinel_exit_code(content, sentinel) is not None


def read_session_id(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8", errors="replace").strip()
    return value or None


def write_session_id(path: Path, session_id: str) -> None:
    path.write_text(f"{session_id}\n", encoding="utf-8")
