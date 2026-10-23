"""Selective capability probes with budget and TTL (TASK-2026-00289).

Probes spend real free-tier quota, so every runner stays disabled unless
the caller passes ``enabled=True``; production refresh (TASK-2026-00292)
probes only selected candidates. Kinds follow SPEC §7.4: ``chat``,
``streaming``, ``tool_calls`` and ``second_turn``. Prompts are synthetic
constants, answers stay short, and paid features (caching, search,
grounding, reasoning extras) are never requested. Errors carry codes,
never prompts, responses or credentials.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from model_routing import live as router_live
from model_routing.models import Capabilities, RouterPolicy

__all__ = [
    "CHAT_MAX_TOKENS",
    "CHAT_PROMPT",
    "TOOL_MAX_TOKENS",
    "TOOL_PROMPT",
    "PostJson",
    "PostStream",
    "ProbeError",
    "ProbeKind",
    "ProbeResult",
    "ToolCall",
    "build_openai_second_turn",
    "gemini_tool_payload",
    "openai_chat_payload",
    "openai_tool_payload",
    "parse_gemini_function_calls",
    "parse_gemini_text",
    "parse_openai_chat",
    "parse_openai_tool_calls",
    "parse_stream_events",
    "run_probe",
]

ProbeKind = Literal["chat", "streaming", "tool_calls", "second_turn"]

PostJson = Callable[[str, Mapping[str, str], dict], dict]
PostStream = Callable[[str, Mapping[str, str], dict], list]

CHAT_PROMPT = "Reply with exactly this word: ok"
TOOL_PROMPT = "Call test_echo with the word ok."
# Reasoning models spend their first tokens thinking: 16 truncates them to an
# empty answer (verified live 2026-10-23). 256 stays short yet clears that floor.
CHAT_MAX_TOKENS = 256
TOOL_MAX_TOKENS = 256

_USER_AGENT = "free-router-live/1"

_CHAT_URLS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "opencode": "https://opencode.ai/inference/openai/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
}

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_TEST_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "test_echo",
        "description": "Return the input word.",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    },
}

_TEST_TOOL_GEMINI = {
    "name": "test_echo",
    "description": "Return the input word.",
    "parameters": {
        "type": "object",
        "properties": {"word": {"type": "string"}},
        "required": ["word"],
    },
}


class ProbeError(RuntimeError):
    """A probe failed or is disabled; carries codes, never content."""

    def __init__(
        self,
        provider: str,
        kind: str,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        retry_after: str | None = None,
        detail: str = "",
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
        self.detail = detail
        text = f"{provider} probe {kind}: {message}"
        if detail:
            text += f" :: {detail}"
        super().__init__(text)

    @classmethod
    def from_http(
        cls,
        provider: str,
        kind: str,
        status: int,
        safe_url: str,
        retry_after: str | None = None,
        detail: str = "",
    ) -> ProbeError:
        """Map HTTP failures; auth/parse never retry (00291 unifies §9.2)."""
        if status == 401:
            return cls(provider, kind, f"POST {safe_url} -> HTTP 401", http_status=401, detail=detail)
        if status == 403:
            return cls(provider, kind, f"POST {safe_url} -> HTTP 403", http_status=403, detail=detail)
        if status == 429:
            return cls(
                provider, kind, f"POST {safe_url} -> HTTP 429", http_status=429,
                retryable=True, retry_after=retry_after, detail=detail,
            )
        if 500 <= status <= 599:
            return cls(
                provider, kind, f"POST {safe_url} -> HTTP {status}", http_status=status,
                retryable=True, retry_after=retry_after, detail=detail,
            )
        return cls(provider, kind, f"POST {safe_url} -> HTTP {status}", http_status=status, detail=detail)


def _error_body(exc: urllib.error.HTTPError, limit: int = 300) -> str:
    """Truncated server explanation; responses never carry our credentials."""
    if exc.fp is None:
        return ""
    try:
        raw = exc.read(4096)
    except (OSError, ValueError):
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else ""
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class ToolCall:
    """One validated tool call with parsed arguments."""

    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one probe with observed capabilities and token counts."""

    provider: str
    deployment_id: str
    kind: str
    capabilities: Capabilities = field(default_factory=Capabilities)
    observed_at: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


def _safe_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.split("@")[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _http_post_json(provider: str, kind: str, url: str, headers: Mapping[str, str], payload: dict) -> dict:
    """Real POST with live-harness hygiene; failures name codes only."""
    safe = _safe_url(url)
    merged = dict(headers)
    merged.setdefault("User-Agent", _USER_AGENT)
    merged["Content-Type"] = "application/json"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=merged, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60.0) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise router_live.LiveRateLimited(
                provider, safe, exc.headers.get("Retry-After")
            ) from exc
        raise ProbeError.from_http(
            provider, kind, exc.code, safe, exc.headers.get("Retry-After"), _error_body(exc)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeError(provider, kind, f"POST {safe} failed: {type(exc).__name__}") from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ProbeError(provider, kind, f"POST {safe} returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise ProbeError(provider, kind, f"POST {safe} returned a non-object payload")
    return parsed


def _http_post_stream(provider: str, kind: str, url: str, headers: Mapping[str, str], payload: dict) -> list:
    """Real SSE POST returning decoded event payloads, in order."""
    safe = _safe_url(url)
    merged = dict(headers)
    merged.setdefault("User-Agent", _USER_AGENT)
    merged["Content-Type"] = "application/json"
    body = json.dumps({**payload, "stream": True}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=merged, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=60.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise router_live.LiveRateLimited(
                provider, safe, exc.headers.get("Retry-After")
            ) from exc
        raise ProbeError.from_http(
            provider, kind, exc.code, safe, exc.headers.get("Retry-After"), _error_body(exc)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeError(provider, kind, f"POST {safe} failed: {type(exc).__name__}") from exc
    events: list = []
    try:
        for line in response:
            text = line.decode("utf-8").strip()
            if not text.startswith("data:"):
                continue
            data = text[5:].strip()
            if data == "[DONE]":
                break
            try:
                events.append(json.loads(data))
            except ValueError as exc:
                raise ProbeError(provider, kind, f"POST {safe} returned a bad chunk") from exc
    finally:
        response.close()
    return events


def openai_chat_payload(model: str, text: str = CHAT_PROMPT) -> dict:
    """Minimal deterministic chat request (temperature 0, short answer)."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": CHAT_MAX_TOKENS,
        "temperature": 0,
    }


def openai_tool_payload(model: str, text: str = TOOL_PROMPT) -> dict:
    """Single synthetic tool offered; no paid features requested."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "tools": [_TEST_TOOL_OPENAI],
        "tool_choice": "auto",
        "max_tokens": TOOL_MAX_TOKENS,
        "temperature": 0,
    }


def gemini_tool_payload(text: str = TOOL_PROMPT) -> dict:
    """Gemini request with one function declaration (model goes in the URL)."""
    return {
        "contents": [{"parts": [{"text": text}]}],
        "tools": [{"functionDeclarations": [_TEST_TOOL_GEMINI]}],
        "generationConfig": {"maxOutputTokens": TOOL_MAX_TOKENS, "temperature": 0},
    }


def gemini_chat_payload(text: str = CHAT_PROMPT) -> dict:
    """Minimal Gemini request (model goes in the URL)."""
    return {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"maxOutputTokens": CHAT_MAX_TOKENS, "temperature": 0},
    }


def _usage_counts(response: dict) -> tuple[int | None, int | None]:
    usage = response.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    else:
        meta = response.get("usageMetadata")
        if not isinstance(meta, dict):
            return None, None
        prompt = meta.get("promptTokenCount")
        completion = meta.get("candidatesTokenCount")
    prompt = prompt if isinstance(prompt, int) and prompt >= 0 else None
    completion = completion if isinstance(completion, int) and completion >= 0 else None
    return prompt, completion


def parse_openai_chat(provider: str, response: dict) -> str:
    """Extract non-empty text or fail closed."""
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProbeError(provider, "chat", "chat response has no text") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProbeError(provider, "chat", "chat response text is empty")
    return content


def parse_openai_tool_calls(provider: str, response: dict) -> list[ToolCall]:
    """Validate every tool call: id, name and JSON-object arguments."""
    try:
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls")
    except (KeyError, IndexError, TypeError) as exc:
        raise ProbeError(provider, "tool_calls", "tool response has no message") from exc
    if not isinstance(calls, list) or not calls:
        raise ProbeError(provider, "tool_calls", "tool response carries no tool_calls")
    parsed: list[ToolCall] = []
    for call in calls:
        if not isinstance(call, dict):
            raise ProbeError(provider, "tool_calls", "tool_call is not an object")
        call_id = call.get("id")
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name")
        raw_args = function.get("arguments", "{}")
        if not call_id or not name:
            raise ProbeError(provider, "tool_calls", "tool_call misses id or name")
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except ValueError as exc:
            raise ProbeError(provider, "tool_calls", "tool_call arguments are not JSON") from exc
        if not isinstance(arguments, dict):
            raise ProbeError(provider, "tool_calls", "tool_call arguments are not an object")
        parsed.append(ToolCall(call_id=call_id, name=name, arguments=arguments))
    return parsed


def build_openai_second_turn(
    user_text: str, assistant_message: dict, calls: list[ToolCall], results: list[str]
) -> list[dict]:
    """Follow-up turn echoing each tool_call_id with its synthetic result."""
    if len(calls) != len(results):
        raise ProbeError("openai-shape", "second_turn", "calls and results differ in length")
    messages = [{"role": "user", "content": user_text}, dict(assistant_message)]
    for call, result in zip(calls, results):
        messages.append({"role": "tool", "tool_call_id": call.call_id, "content": result})
    return messages


def parse_gemini_text(provider: str, response: dict) -> str:
    """Join text parts; function-only answers fail the chat probe."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProbeError(provider, "chat", "gemini response has no parts") from exc
    if not isinstance(parts, list):
        raise ProbeError(provider, "chat", "gemini parts are not a list")
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and "functionCall" not in part
    )
    if not text.strip():
        raise ProbeError(provider, "chat", "gemini response text is empty")
    return text


def parse_gemini_function_calls(provider: str, response: dict) -> list[ToolCall]:
    """Validate functionCall parts; Gemini ids fall back to name+index."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProbeError(provider, "tool_calls", "gemini response has no parts") from exc
    if not isinstance(parts, list):
        raise ProbeError(provider, "tool_calls", "gemini parts are not a list")
    parsed: list[ToolCall] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or "functionCall" not in part:
            continue
        call = part["functionCall"]
        if not isinstance(call, dict) or not call.get("name"):
            raise ProbeError(provider, "tool_calls", "functionCall misses its name")
        arguments = call.get("args", {})
        if not isinstance(arguments, dict):
            raise ProbeError(provider, "tool_calls", "functionCall args are not an object")
        call_id = call.get("id") or f"{call['name']}-{index}"
        parsed.append(ToolCall(call_id=str(call_id), name=call["name"], arguments=arguments))
    if not parsed:
        raise ProbeError(provider, "tool_calls", "gemini response carries no functionCall")
    return parsed


def parse_stream_events(provider: str, events: list) -> tuple[str, bool]:
    """Join streamed deltas; complete only with an explicit finish reason."""
    texts: list[str] = []
    finished = False
    for event in events:
        if not isinstance(event, dict):
            raise ProbeError(provider, "streaming", "stream event is not an object")
        try:
            choices = event.get("choices", [])
            delta = choices[0].get("delta", {}) if choices else {}
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise ProbeError(provider, "streaming", "stream event has no choices") from exc
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            texts.append(delta["content"])
        try:
            if choices and choices[0].get("finish_reason"):
                finished = True
        except (KeyError, IndexError, AttributeError, TypeError):
            continue
    return "".join(texts), finished


def _endpoint(provider: str, model: str) -> tuple[str, str]:
    if provider == "gemini":
        return _GEMINI_URL.format(model=model), "gemini"
    try:
        return _CHAT_URLS[provider], "openai"
    except KeyError as exc:
        raise ProbeError(provider, "chat", f"unknown provider: {provider}") from exc


def run_probe(
    provider: str,
    kind: ProbeKind,
    model: str,
    headers: Mapping[str, str],
    *,
    enabled: bool = False,
    messages: list[dict] | None = None,
    post: PostJson | None = None,
    post_stream: PostStream | None = None,
    policy: RouterPolicy | None = None,
) -> tuple[ProbeResult, dict]:
    """Run one probe; disabled, over-budget or failed probes never spend."""
    if not enabled:
        raise ProbeError(provider, kind, "probes are disabled by default")
    if provider == "opencode":
        router_live.assert_no_auth(headers)
    url, family = _endpoint(provider, model)
    if kind == "chat":
        payload = gemini_chat_payload() if family == "gemini" else openai_chat_payload(model)
    elif kind == "tool_calls":
        payload = gemini_tool_payload() if family == "gemini" else openai_tool_payload(model)
    elif kind == "second_turn":
        if family == "gemini" or messages is None:
            raise ProbeError(provider, kind, "second_turn needs OpenAI messages")
        payload = {"model": model, "messages": messages, "max_tokens": CHAT_MAX_TOKENS, "temperature": 0}
    elif kind == "streaming":
        if family == "gemini":
            raise ProbeError(provider, kind, "streaming probe is OpenAI-shaped only")
        payload = openai_chat_payload(model)
    else:
        raise ProbeError(provider, kind, f"unknown probe kind: {kind}")
    budget = router_live.read_budget()
    if budget.used.get(provider, 0) >= router_live.effective_cap(provider, policy):
        raise router_live.BudgetExhausted(
            provider, router_live.effective_cap(provider, policy), budget.used.get(provider, 0)
        )
    sender = post or (lambda u, h, p: _http_post_json(provider, kind, u, h, p))
    if kind == "streaming":
        streamer = post_stream or (lambda u, h, p: _http_post_stream(provider, kind, u, h, p))
        events = streamer(url, headers, payload)
        text, finished = parse_stream_events(provider, events)
        if not finished:
            raise ProbeError(provider, kind, "stream ended without finish_reason")
        if not text.strip():
            raise ProbeError(provider, kind, "stream carried no text")
        raw: dict = {"events": events}
        capabilities = Capabilities(streaming=True)
        counts: tuple[int | None, int | None] = (None, None)
    else:
        raw = sender(url, headers, payload)
        if kind == "chat":
            if family == "gemini":
                parse_gemini_text(provider, raw)
            else:
                parse_openai_chat(provider, raw)
            capabilities = Capabilities()
        elif kind == "tool_calls":
            if family == "gemini":
                parse_gemini_function_calls(provider, raw)
            else:
                parse_openai_tool_calls(provider, raw)
            capabilities = Capabilities(tools=True)
        else:
            parse_openai_chat(provider, raw)
            capabilities = Capabilities()
        counts = _usage_counts(raw)
    router_live.spend(provider, policy=policy)
    result = ProbeResult(
        provider=provider,
        deployment_id=f"{provider}/{model}",
        kind=kind,
        capabilities=capabilities,
        observed_at=datetime.now(timezone.utc).isoformat(),
        input_tokens=counts[0],
        output_tokens=counts[1],
    )
    return result, raw
