from __future__ import annotations

from typing import Any

from settings import get_settings
from tools.email_sender import SMTPEmailSender


def list_sender_accounts() -> dict[str, Any]:
    sender = SMTPEmailSender(get_settings())
    return {"accounts": sender.list_accounts()}


def send_email(
    *,
    from_email: str | None,
    to: str | list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    reply_to: str | None = None,
    attachments: list[str] | list[dict[str, str]] | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    sender = SMTPEmailSender(get_settings())
    return sender.send_email(
        from_email=from_email,
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        attachments=attachments,
        language=language,
    )
