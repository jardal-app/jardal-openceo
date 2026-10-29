"""Stable v1 fingerprints (design v2 section 6.1).

Fingerprints never embed line numbers, paths or free-text messages, so the
same defect hashes identically across releases and tickets. Changing this
algorithm closes old Problems as superseded.
"""

from __future__ import annotations

import hashlib
import re

from .sanitize import is_in_app_frame

FINGERPRINT_VERSION = 1

MAX_REASON_CODE_CHARS = 120
EXCEPTION_IN_APP_FRAMES = 5
NON_IN_APP_PACKAGES = 3

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{7,}\b")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?\b")
_NUMBER = re.compile(r"\b\d[\d.,:_-]*\b")
_PATH = re.compile(r"(?<![\w@:/-])/(?:[\w.-]+/)*[\w.-]+")


def normalize_reason_code(code: str | None) -> str:
    text = (code or "").lower()
    text = _TIMESTAMP.sub("<time>", text)
    text = _UUID.sub("<id>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text)
    text = _PATH.sub("<path>", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_REASON_CODE_CHARS]


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _in_app_key(frame: dict[str, str]) -> str:
    return f"{frame.get('module') or '?'}:{frame.get('function') or '?'}"


def fingerprint_exception(exc_type: str, frames: list[dict[str, str]],
                          component: str) -> str:
    in_app = [frame for frame in frames or []
              if is_in_app_frame(str(frame.get("file") or ""))]
    if in_app:
        tail = in_app[-EXCEPTION_IN_APP_FRAMES:]
        return _digest([str(exc_type or "?"), str(component)]
                       + [_in_app_key(frame) for frame in tail])
    packages = [str(frame.get("module") or "?").split(".")[0]
                for frame in (frames or [])[:NON_IN_APP_PACKAGES]]
    return _digest([str(exc_type or "?"), str(component)] + packages)


def fingerprint_terminal(kind: str, component: str, phase: str | None,
                         reason_code: str | None, brake: str | None) -> str:
    return _digest([str(kind), str(component), str(phase or "?"),
                    normalize_reason_code(reason_code), str(brake or "?")])


def fingerprint_stall(component: str, phase: str | None,
                      rule: str | None, result_signature: str | None) -> str:
    return _digest([str(component), str(phase or "?"),
                    str(rule or "?").strip().upper(),
                    str(result_signature or "?")])


def fingerprint_invariant(invariant_id: str) -> str:
    return _digest([str(invariant_id).strip().upper()])


def fingerprint_deadline(component: str, deadline_type: str) -> str:
    return _digest([str(component), str(deadline_type)])


def fingerprint_unclean(service_result: str) -> str:
    return _digest([str(service_result or "?").strip().lower()])


def fingerprint_escape(escape_type: str, tier: str | None,
                       review_policy: str | None) -> str:
    return _digest([str(escape_type), str(tier or "?"), str(review_policy or "?")])


def problem_key(scope: str, fingerprint: str,
                version: int = FINGERPRINT_VERSION) -> str:
    return _digest([str(scope), str(fingerprint), str(version)])
