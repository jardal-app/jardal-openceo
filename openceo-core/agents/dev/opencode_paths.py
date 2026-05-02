from __future__ import annotations

import shutil
from pathlib import Path

_KNOWN_OPENCODE_PATHS = (
    "/home/ceouser/.opencode/bin/opencode",
    "/home/develop/.opencode/bin/opencode",
)


def resolve_opencode_cli_path(cli_path: str | None) -> str:
    candidates: list[str] = []
    if cli_path:
        candidates.append(cli_path)
        if cli_path == "/home/develop/.opencode/bin/opencode":
            candidates.append("/home/ceouser/.opencode/bin/opencode")
        elif cli_path == "/home/ceouser/.opencode/bin/opencode":
            candidates.append("/home/develop/.opencode/bin/opencode")

    for fallback in _KNOWN_OPENCODE_PATHS:
        if fallback not in candidates:
            candidates.append(fallback)
    candidates.append("opencode")

    for candidate in candidates:
        if "/" not in candidate:
            found = shutil.which(candidate)
            if found:
                return found
            continue
        path = Path(candidate)
        if path.exists() and path.is_file():
            return str(path)

    return cli_path or "opencode"


def normalize_opencode_command(command: list[str]) -> list[str]:
    if not command:
        return command
    normalized = list(command)
    normalized[0] = resolve_opencode_cli_path(str(command[0]))
    return normalized
