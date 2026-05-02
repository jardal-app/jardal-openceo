from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_REVIEW_BLOCK_RE = re.compile(
    r"REVIEW_LINKS_START\s*(?P<body>.*?)\s*REVIEW_LINKS_END",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://[^\s<>'\"`)\]]+")
_DASHBOARD_PATH_RE = re.compile(r"(?<![A-Za-z0-9_:-])(/dashboard/[^\s<>'\"`)\]]+)")


@dataclass(frozen=True)
class ReviewLinksExtraction:
    links: list[str]
    explicit_block: bool


def extract_review_links(text: str, base_url: str | None = None) -> ReviewLinksExtraction:
    content = text or ""
    block_match = _REVIEW_BLOCK_RE.search(content)
    if block_match:
        body = block_match.group("body").strip()
        if not body or _is_none_marker(body):
            return ReviewLinksExtraction(links=[], explicit_block=True)

        links: list[str] = []
        for raw_line in body.splitlines():
            candidate = _normalize_explicit_candidate(raw_line, base_url)
            if candidate:
                links.append(candidate)
        return ReviewLinksExtraction(links=_dedupe(links), explicit_block=True)

    return ReviewLinksExtraction(
        links=_dedupe(_extract_fallback_links(content, base_url)),
        explicit_block=False,
    )


def apply_review_links_to_params(
    params: dict | None,
    extraction: ReviewLinksExtraction,
) -> dict | None:
    current = dict(params or {})
    updated = dict(current)
    changed = False

    if extraction.links:
        if updated.get("review_links") != extraction.links:
            updated["review_links"] = extraction.links
            changed = True
        primary = extraction.links[0]
        for key in ("test_url", "client_review_url"):
            if updated.get(key) != primary:
                updated[key] = primary
                changed = True
    elif extraction.explicit_block:
        for key in ("review_links", "test_url", "client_review_url"):
            if key in updated:
                updated.pop(key, None)
                changed = True

    return updated if changed else None


def _extract_fallback_links(text: str, base_url: str | None) -> list[str]:
    links: list[str] = []

    for match in _URL_RE.findall(text):
        cleaned = _clean_candidate(match)
        if not cleaned:
            continue
        if "/dashboard/" not in urlsplit(cleaned).path:
            continue
        links.append(cleaned)

    for match in _DASHBOARD_PATH_RE.findall(text):
        normalized = _normalize_path(match, base_url)
        if normalized:
            links.append(normalized)

    return links


def _normalize_explicit_candidate(line: str, base_url: str | None) -> str | None:
    candidate = line.strip()
    if not candidate:
        return None
    if candidate.startswith("- "):
        candidate = candidate[2:].strip()
    elif candidate.startswith("* "):
        candidate = candidate[2:].strip()
    else:
        candidate = re.sub(r"^\d+\.\s+", "", candidate)
    candidate = _clean_candidate(candidate)
    if not candidate or _is_none_marker(candidate):
        return None
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    if candidate.startswith("dashboard/"):
        candidate = f"/{candidate}"
    if candidate.startswith("/dashboard/"):
        return _normalize_path(candidate, base_url)
    return None


def _normalize_path(path: str, base_url: str | None) -> str | None:
    cleaned = _clean_candidate(path)
    if not cleaned or not cleaned.startswith("/dashboard/"):
        return None
    if not base_url:
        return cleaned
    return f"{base_url.rstrip('/')}{cleaned}"


def _clean_candidate(value: str) -> str:
    return value.strip().strip("`'\"").rstrip(".,;:")


def _is_none_marker(value: str) -> bool:
    return value.strip().upper() == "NONE"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
