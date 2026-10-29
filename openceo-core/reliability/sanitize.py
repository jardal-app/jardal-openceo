"""Capture-time redaction (design v2 section 15.2).

Only allowlisted fields survive; free text is truncated to 500 characters
with secrets redacted. Fail-closed: unknown fields are dropped and absolute
paths are redacted unless proven inside the repo.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

MAX_MESSAGE_CHARS = 500
MAX_EVIDENCE_REFS = 10

ENGINE_FIELDS = frozenset({
    "exc_type", "frames", "reason_code", "guard_rule", "brake",
    "invariant_id", "project_key", "task_id", "run_id", "attempt_id",
})

CONTEXT_FIELDS = frozenset({"business_ref", "message", "evidence_refs"})

_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|"
    r"secret|client[_-]?secret|password|passwd|pwd|bearer|private[_-]?key|"
    r"session[_-]?token)\b(\s*[:=]\s*)([^\s,;\"']+)"
)
_BARE_TOKEN = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|xox[bap]-?[A-Za-z0-9-]{8,})")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_CREDS = re.compile(r"(\w+://)[^\s/@]+@")
_DSN = re.compile(r"(?i)\b(dsn\s*[:=]\s*)([^\s,;\"']+)")
_ABS_PATH = re.compile(r"(?<![\w@:/-])/(?:[\w.-]+/)+[\w.-]+|(?<![\w@:/-])/(?:etc|home|root|tmp|var|opt|usr|mnt|data)(?:/[\w.-]+)*")

_IN_APP_PREFIX = "openceo-core/"
_RUNNER_FILE = "automatico.py"


def _normalized_path(file: str) -> str:
    return (file or "").replace("\\", "/").lstrip("./")


def is_in_app_frame(file: str) -> bool:
    normalized = _normalized_path(file)
    if PurePosixPath(normalized).name == _RUNNER_FILE:
        return True
    return f"/{_IN_APP_PREFIX}" in f"/{normalized}"


def is_motor_frame(file: str) -> bool:
    return f"/{_IN_APP_PREFIX}" in f"/{_normalized_path(file)}"


def sanitize_message(text: str, repo_root: str | None = None) -> str:
    redacted = _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text or "")
    redacted = _BARE_TOKEN.sub("[REDACTED]", redacted)
    redacted = _URL_CREDS.sub(lambda m: m.group(1) + "[CREDS]@", redacted)
    redacted = _DSN.sub(lambda m: m.group(1) + "[REDACTED]", redacted)
    redacted = _EMAIL.sub("[EMAIL]", redacted)
    root = (repo_root or "").replace("\\", "/").rstrip("/")

    def _path(match: re.Match[str]) -> str:
        if root and PurePosixPath(match.group(0)).is_relative_to(PurePosixPath(root)):
            return match.group(0)
        return "[PATH]"

    redacted = _ABS_PATH.sub(_path, redacted)
    return redacted[:MAX_MESSAGE_CHARS]


def sanitize_frames(frames: list[dict[str, str]]) -> list[str]:
    kept: list[str] = []
    for frame in frames or []:
        module = str(frame.get("module") or "?")
        function = str(frame.get("function") or "?")
        if is_in_app_frame(str(frame.get("file") or "")):
            kept.append(f"{module}:{function}:{frame.get('line') or '?'}")
        else:
            kept.append(module.split(".")[0])
    return kept


def sanitize_incident(payload: dict, repo_root: str | None = None) -> dict:
    engine_in = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
    context_in = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    engine = {key: engine_in[key] for key in ENGINE_FIELDS if key in engine_in}
    if "frames" in engine and isinstance(engine["frames"], list):
        engine["frames"] = sanitize_frames(engine["frames"])
    if "reason_code" in engine:
        engine["reason_code"] = str(engine["reason_code"])[:120]
    message = sanitize_message(str(context_in.get("message") or ""), repo_root)
    refs = context_in.get("evidence_refs")
    refs = [str(ref) for ref in refs[:MAX_EVIDENCE_REFS]] if isinstance(refs, list) else []
    context = {"message": message, "evidence_refs": refs}
    if context_in.get("business_ref") is not None:
        context["business_ref"] = str(context_in["business_ref"])
    return {"engine": engine, "context": context}
