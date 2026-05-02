from __future__ import annotations

import re
import time
from uuid import uuid4

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

    alias = match.group("agent").lower()
    mapping = {
        "dev": AgentName.DEV,
        "pm": AgentName.PM,
        "marketing": AgentName.MARKETING,
        "finance": AgentName.FINANCE,
        "secretary": AgentName.SECRETARY,
        "sales": AgentName.SALES,
        "media": AgentName.MEDIA,
    }
    return mapping.get(alias)


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
