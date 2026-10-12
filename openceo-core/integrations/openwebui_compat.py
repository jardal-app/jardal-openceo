from __future__ import annotations

import re
import time
from uuid import uuid4

from router import CEORouter
from state import (
    AgentName,
    ChatCompletionChoice,
    ChatCompletionResponse,
    OpenAIMessage,
)

AGENT_PREFIX = re.compile(r"^@(?P<agent>[a-zA-Z_]+)\b")


def extract_latest_user_message(messages: list[OpenAIMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def parse_agent_hint(text: str) -> AgentName | None:
    match = AGENT_PREFIX.match(text.strip())
    if not match:
        return None
    return CEORouter.AGENT_ALIASES.get(match.group("agent").lower())


def chat_response(model: str, text: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid4().hex}",
        created=int(time.time()),
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=OpenAIMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
    )
