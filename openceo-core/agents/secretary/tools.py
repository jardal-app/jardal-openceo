from __future__ import annotations

from typing import Any

from settings import get_settings
from tools.email_sender import SMTPEmailSender


def list_sender_accounts() -> dict[str, Any]:
    sender = SMTPEmailSender(get_settings())
    return {"accounts": sender.list_accounts()}

