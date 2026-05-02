from __future__ import annotations


class TelegramBotClient:
    """Placeholder adapter for Telegram Bot API integration."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    async def send_message(self, text: str) -> None:
        _ = (text, self.token, self.chat_id)
        # Implement this in phase 7.
