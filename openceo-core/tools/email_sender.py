from __future__ import annotations

import html
import json
import mimetypes
import smtplib
import ssl
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

from settings import CORE_DIR, Settings


class EmailSenderConfigurationError(RuntimeError):
    """Raised when SMTP settings are missing or malformed."""


@dataclass(frozen=True)
class SMTPAccount:
    email: str
    username: str
    password: str
    header_name: str | None = None
    header_name_es: str | None = None
    header_name_en: str | None = None
    signature_name: str | None = None
    signature_title: str | None = None
    signature_title_es: str | None = None
    signature_title_en: str | None = None


@dataclass(frozen=True)
class SMTPAttachment:
    path: Path
    filename: str
    mime_type: str


class SMTPEmailSender:
    """Shared SMTP sender for operational email workflows."""

    _DEPARTMENT_BY_SENDER = {
        "admin": "Administracion",
        "ceo": "Direccion General",
        "dev": "Desarrollo",
        "marketing": "Marketing",
        "finance": "Finanzas",
        "sales": "Ventas",
        "ventas": "Ventas",
        "media": "Media",
        "hr": "Recursos Humanos",
        "pm": "Project Management",
        "legal": "Legal",
        "support": "Soporte",
    }
    _LANGUAGE_SENDER_ALIASES = {
        "sales@jardal.app": {"es": "ventas@jardal.app", "en": "sales@jardal.app"},
        "ventas@jardal.app": {"es": "ventas@jardal.app", "en": "sales@jardal.app"},
        "support@jardal.app": {"es": "soporte@jardal.app", "en": "support@jardal.app"},
        "soporte@jardal.app": {"es": "soporte@jardal.app", "en": "support@jardal.app"},
        "finance@jardal.app": {"es": "finanzas@jardal.app", "en": "finance@jardal.app"},
        "finanzas@jardal.app": {"es": "finanzas@jardal.app", "en": "finance@jardal.app"},
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._repo_root = CORE_DIR.parent
        self._accounts = self._load_accounts()

    def list_accounts(self) -> list[dict[str, str]]:
        return [
            {
                "email": account.email,
                "display_name": self._header_name_for(account, "es") or account.email,
                "header_name": self._header_name_for(account, "es") or account.email,
            }
            for account in sorted(self._accounts.values(), key=lambda item: item.email)
        ]

    def send_email(
        self,
        *,
        from_email: str | None,
        to: str | list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[Any] | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        if not self._settings.SMTP_HOST.strip():
            raise EmailSenderConfigurationError("SMTP_HOST no está configurado.")

        recipients = self._normalize_addresses(to)
        cc_list = self._normalize_addresses(cc)
        bcc_list = self._normalize_addresses(bcc)
        if not recipients and not cc_list and not bcc_list:
            raise ValueError("Debes indicar al menos un destinatario.")

        requested_sender = self._normalize_sender_key(from_email or self._settings.SMTP_DEFAULT_SENDER)
        sender_key = self._resolve_sender_key(requested_sender, language)
        if not sender_key:
            raise EmailSenderConfigurationError("No hay remitente SMTP por defecto configurado.")

        account = self._accounts.get(sender_key)
        if account is None:
            available = ", ".join(sorted(self._accounts))
            raise EmailSenderConfigurationError(
                f"El remitente '{sender_key}' no está configurado. Disponibles: {available}"
            )

        effective_language = self._resolve_effective_language(language, sender_key)

        message = EmailMessage()
        sender_header = formataddr((self._header_name_for(account, effective_language) or account.email, account.email))
        message["From"] = sender_header
        message["To"] = ", ".join(recipients)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        if reply_to:
            message["Reply-To"] = reply_to.strip()
        message["Subject"] = subject.strip()
        message["Message-ID"] = make_msgid(domain=account.email.split("@", 1)[-1])

        plain_body = body_text.strip()
        if not plain_body:
            raise ValueError("El cuerpo del correo no puede estar vacío.")

        resolved_attachments = self._normalize_attachments(attachments)
        plain_with_signature = self._build_plain_body(account, plain_body, effective_language)
        html_with_signature, inline_logo = self._build_html_body(account, body_html or plain_body, effective_language)

        message.set_content(plain_with_signature)
        message.add_alternative(html_with_signature, subtype="html")
        html_part = message.get_payload()[-1]
        if inline_logo is not None:
            html_part.add_related(
                inline_logo["data"],
                maintype=inline_logo["maintype"],
                subtype=inline_logo["subtype"],
                cid=inline_logo["cid"],
                disposition="inline",
                filename=inline_logo["filename"],
            )

        for attachment in resolved_attachments:
            maintype, subtype = self._split_mime_type(attachment.mime_type)
            message.add_attachment(
                attachment.path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=attachment.filename,
            )

        envelope_to = [*recipients, *cc_list, *bcc_list]
        self._deliver(account=account, message=message, recipients=envelope_to)

        return {
            "status": "sent",
            "from": account.email,
            "to": recipients,
            "cc": cc_list,
            "bcc_count": len(bcc_list),
            "subject": message["Subject"],
            "message_id": message["Message-ID"],
            "smtp_host": self._settings.SMTP_HOST,
            "smtp_port": self._settings.SMTP_PORT,
            "attachments": [attachment.filename for attachment in resolved_attachments],
            "language": effective_language,
        }

    def _deliver(
        self,
        *,
        account: SMTPAccount,
        message: EmailMessage,
        recipients: list[str],
    ) -> None:
        use_ssl = bool(self._settings.SMTP_USE_SSL)
        timeout = max(5, int(self._settings.SMTP_TIMEOUT_SECONDS))

        if use_ssl:
            context = self._build_ssl_context()
            with smtplib.SMTP_SSL(
                host=self._settings.SMTP_HOST,
                port=self._settings.SMTP_PORT,
                timeout=timeout,
                context=context,
            ) as client:
                client.login(account.username, account.password)
                client.send_message(message, from_addr=account.email, to_addrs=recipients)
            return

        with smtplib.SMTP(
            host=self._settings.SMTP_HOST,
            port=self._settings.SMTP_PORT,
            timeout=timeout,
        ) as client:
            if self._settings.SMTP_USE_STARTTLS:
                client.starttls(context=self._build_ssl_context())
            client.login(account.username, account.password)
            client.send_message(message, from_addr=account.email, to_addrs=recipients)

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self._settings.SMTP_SSL_VERIFY:
            return ssl.create_default_context()
        return ssl._create_unverified_context()

    def _load_accounts(self) -> dict[str, SMTPAccount]:
        identities = self._load_identities()
        raw = self._settings.SMTP_ACCOUNTS_JSON.strip()
        if not raw:
            if self._settings.SMTP_USER.strip() and self._settings.SMTP_PASS.strip():
                fallback_email = (self._settings.SMTP_DEFAULT_SENDER or self._settings.SMTP_USER).strip().lower()
                identity = identities.get(fallback_email, {})
                return {
                    fallback_email: SMTPAccount(
                        email=fallback_email,
                        username=self._settings.SMTP_USER.strip(),
                        password=self._settings.SMTP_PASS.strip(),
                        header_name=self._preferred_value(
                            identity.get("header_name"),
                            identity.get("display_name"),
                            fallback_email,
                        ),
                        header_name_es=self._preferred_value(identity.get("header_name_es")),
                        header_name_en=self._preferred_value(identity.get("header_name_en")),
                        signature_name=identity.get("signature_name"),
                        signature_title=identity.get("signature_title"),
                    )
                }
            raise EmailSenderConfigurationError("No hay cuentas SMTP configuradas.")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmailSenderConfigurationError("SMTP_ACCOUNTS_JSON no es JSON válido.") from exc

        if not isinstance(payload, dict):
            raise EmailSenderConfigurationError("SMTP_ACCOUNTS_JSON debe ser un objeto JSON.")

        accounts: dict[str, SMTPAccount] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                raise EmailSenderConfigurationError(f"La cuenta SMTP '{key}' no tiene formato objeto.")

            email = str(value.get("email") or key).strip().lower()
            username = str(value.get("username") or email).strip()
            password = str(value.get("password") or "").strip()
            identity = identities.get(email, {})
            header_name = self._preferred_value(
                identity.get("header_name"),
                identity.get("display_name"),
                value.get("header_name"),
                value.get("display_name"),
            )
            header_name_es = self._preferred_value(identity.get("header_name_es"), value.get("header_name_es"))
            header_name_en = self._preferred_value(identity.get("header_name_en"), value.get("header_name_en"))
            signature_name = self._preferred_value(identity.get("signature_name"), value.get("signature_name"))
            signature_title = self._preferred_value(identity.get("signature_title"), value.get("signature_title"))
            signature_title_es = self._preferred_value(identity.get("signature_title_es"))
            signature_title_en = self._preferred_value(identity.get("signature_title_en"))
            if not email or not username or not password:
                raise EmailSenderConfigurationError(
                    f"La cuenta SMTP '{key}' requiere email, username y password."
                )
            accounts[email] = SMTPAccount(
                email=email,
                username=username,
                password=password,
                header_name=header_name,
                header_name_es=header_name_es,
                header_name_en=header_name_en,
                signature_name=signature_name,
                signature_title=signature_title,
                signature_title_es=signature_title_es,
                signature_title_en=signature_title_en,
            )

        return accounts

    def _load_identities(self) -> dict[str, dict[str, str]]:
        raw_path = self._settings.SMTP_IDENTITIES_PATH.strip()
        if not raw_path:
            return {}

        path = self._resolve_path(raw_path)
        if path is None or not path.is_file():
            return {}

        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise EmailSenderConfigurationError(
                f"SMTP_IDENTITIES_PATH no contiene TOML válido: {path}"
            ) from exc

        accounts = payload.get("accounts")
        if accounts is None:
            return {}
        if not isinstance(accounts, dict):
            raise EmailSenderConfigurationError("El archivo de identidades SMTP debe definir [accounts].")

        identities: dict[str, dict[str, str]] = {}
        for email, data in accounts.items():
            if not isinstance(data, dict):
                raise EmailSenderConfigurationError(
                    f"La identidad SMTP '{email}' debe ser un objeto TOML."
                )
            identities[str(email).strip().lower()] = {
                "header_name_es": self._preferred_value(data.get("header_name_es")),
                "header_name_en": self._preferred_value(data.get("header_name_en")),
                "header_name": self._preferred_value(data.get("header_name"), data.get("display_name")),
                "display_name": self._preferred_value(data.get("display_name")),
                "signature_name": self._preferred_value(data.get("signature_name")),
                "signature_title": self._preferred_value(data.get("signature_title")),
                "signature_title_es": self._preferred_value(data.get("signature_title_es")),
                "signature_title_en": self._preferred_value(data.get("signature_title_en")),
            }
        return identities

    def _build_plain_body(self, account: SMTPAccount, body_text: str, language: str | None) -> str:
        signature_lines = [
            "",
            "--",
            self._signature_name_for(account),
            self._signature_title_for(account, language),
            account.email,
            self._settings.SMTP_SIGNATURE_COMPANY.strip(),
        ]
        return body_text.strip() + "\n" + "\n".join(signature_lines) + "\n"

    def _build_html_body(
        self,
        account: SMTPAccount,
        body_html_or_text: str,
        language: str | None,
    ) -> tuple[str, dict[str, str | bytes] | None]:
        body_fragment = self._ensure_html_fragment(body_html_or_text)
        logo = self._load_inline_logo()
        logo_html = ""
        if logo is not None:
            logo_html = (
                f'<td style="padding-right:16px;vertical-align:top;">'
                f'<img src="cid:{logo["cid"]}" alt="Jardal" '
                f'style="display:block;width:{int(self._settings.SMTP_SIGNATURE_LOGO_WIDTH)}px;height:auto;border:0;" />'
                f"</td>"
            )

        signature_html = (
            '<div style="margin-top:24px;padding-top:16px;border-top:1px solid #d7dce3;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
            "<tr>"
            f"{logo_html}"
            '<td style="vertical-align:top;font-family:Arial,sans-serif;font-size:13px;line-height:1.5;color:#1f2937;">'
            f'<div style="font-weight:700;color:#111827;">{html.escape(self._signature_name_for(account))}</div>'
            f'<div>{html.escape(self._signature_title_for(account, language))}</div>'
            f'<div><a href="mailto:{html.escape(account.email)}" '
            'style="color:#0f5aa6;text-decoration:none;">'
            f"{html.escape(account.email)}</a></div>"
            f'<div style="color:#4b5563;">{html.escape(self._settings.SMTP_SIGNATURE_COMPANY.strip())}</div>'
            "</td>"
            "</tr>"
            "</table>"
            "</div>"
        )

        return self._append_html_footer(body_fragment, signature_html), logo

    def _load_inline_logo(self) -> dict[str, str | bytes] | None:
        raw_path = self._settings.SMTP_SIGNATURE_LOGO_PATH.strip()
        if not raw_path:
            return None
        logo_path = self._resolve_path(raw_path)
        if logo_path is None or not logo_path.is_file():
            return None

        mime_type = mimetypes.guess_type(logo_path.name)[0] or "image/png"
        maintype, subtype = self._split_mime_type(mime_type)
        if maintype != "image":
            return None

        cid = make_msgid(domain="jardal.app")[1:-1]
        return {
            "cid": cid,
            "filename": logo_path.name,
            "data": logo_path.read_bytes(),
            "maintype": maintype,
            "subtype": subtype,
        }

    def _normalize_attachments(self, attachments: list[Any] | None) -> list[SMTPAttachment]:
        if not attachments:
            return []

        normalized: list[SMTPAttachment] = []
        seen: set[str] = set()
        for item in attachments:
            attachment = self._normalize_attachment_item(item)
            if attachment is None:
                continue
            key = str(attachment.path.resolve())
            if key in seen:
                continue
            seen.add(key)
            normalized.append(attachment)
        return normalized

    def _normalize_attachment_item(self, item: Any) -> SMTPAttachment | None:
        if item is None:
            return None

        filename_override: str | None = None
        mime_override: str | None = None
        raw_path: str | None = None

        if isinstance(item, dict):
            raw_path = str(item.get("path") or item.get("file_path") or item.get("filepath") or "").strip()
            filename_override = str(item.get("filename") or "").strip() or None
            mime_override = str(item.get("mime_type") or item.get("content_type") or "").strip() or None
        else:
            raw_path = str(item).strip()

        if not raw_path:
            return None

        file_path = self._resolve_path(raw_path)
        if file_path is None or not file_path.is_file():
            raise FileNotFoundError(f"Adjunto no encontrado: {raw_path}")

        mime_type = mime_override or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        return SMTPAttachment(
            path=file_path,
            filename=filename_override or file_path.name,
            mime_type=mime_type,
        )

    def _resolve_path(self, raw_path: str) -> Path | None:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            return candidate
        repo_candidate = (self._repo_root / candidate).resolve()
        if repo_candidate.exists():
            return repo_candidate
        core_candidate = (CORE_DIR / candidate).resolve()
        if core_candidate.exists():
            return core_candidate
        return candidate.resolve()

    def _department_for(self, email: str) -> str:
        local = email.split("@", 1)[0].strip().lower()
        return self._DEPARTMENT_BY_SENDER.get(local, "Equipo Jardal")

    def _resolve_sender_key(self, requested_sender: str, language: str | None) -> str:
        normalized_language = self._normalize_language(language)
        if not normalized_language:
            return requested_sender
        overrides = self._LANGUAGE_SENDER_ALIASES.get(requested_sender)
        if not overrides:
            return requested_sender
        candidate = overrides.get(normalized_language) or requested_sender
        if candidate in self._accounts:
            return candidate
        return requested_sender

    def _normalize_sender_key(self, value: str) -> str:
        normalized = value.strip().lower()
        if "@" in normalized:
            return normalized
        direct = f"{normalized}@jardal.app"
        if direct in self._accounts:
            return direct
        return normalized

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        if language is None:
            return None
        normalized = str(language).strip().lower()
        if normalized in {"es", "es-es", "español", "espanol", "spanish"}:
            return "es"
        if normalized in {"en", "en-us", "en-gb", "english", "ingles", "inglés"}:
            return "en"
        return None

    def _resolve_effective_language(self, language: str | None, sender_key: str) -> str | None:
        normalized_language = self._normalize_language(language)
        if normalized_language:
            return normalized_language
        local = sender_key.split("@", 1)[0].strip().lower()
        if local in {"ventas", "finanzas", "soporte"}:
            return "es"
        if local in {"sales", "finance", "support"}:
            return "en"
        return "es"

    def _header_name_for(self, account: SMTPAccount, language: str | None) -> str | None:
        normalized_language = self._normalize_language(language)
        if normalized_language == "en":
            return (
                account.header_name_en
                or account.header_name
                or account.header_name_es
                or f"{self._department_for(account.email)} Jardal"
            )
        return (
            account.header_name_es
            or account.header_name
            or account.header_name_en
            or f"{self._department_for(account.email)} Jardal"
        )

    def _signature_name_for(self, account: SMTPAccount) -> str:
        return account.signature_name or account.header_name or account.email

    def _signature_title_for(self, account: SMTPAccount, language: str | None) -> str:
        normalized_language = self._normalize_language(language)
        if normalized_language == "en":
            return (
                account.signature_title_en
                or account.signature_title
                or account.signature_title_es
                or self._department_for(account.email)
            )
        return (
            account.signature_title_es
            or account.signature_title
            or account.signature_title_en
            or self._department_for(account.email)
        )

    @staticmethod
    def _preferred_value(*values: Any) -> str | None:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _split_mime_type(mime_type: str) -> tuple[str, str]:
        if "/" not in mime_type:
            return "application", "octet-stream"
        maintype, subtype = mime_type.split("/", 1)
        return maintype, subtype

    @staticmethod
    def _ensure_html_fragment(value: str) -> str:
        stripped = value.strip()
        lowered = stripped.lower()
        if "<html" in lowered or "<body" in lowered or "<div" in lowered or "<p" in lowered or "<table" in lowered:
            return stripped
        escaped = html.escape(stripped).replace("\n", "<br />")
        return f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#111827;">{escaped}</div>'

    @staticmethod
    def _append_html_footer(body_html: str, footer_html: str) -> str:
        lowered = body_html.lower()
        for marker in ("</body>", "</html>"):
            index = lowered.rfind(marker)
            if index != -1:
                return body_html[:index] + footer_html + body_html[index:]
        return body_html + footer_html

    @staticmethod
    def _normalize_addresses(raw: str | list[str] | None) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            values = [item.strip() for item in raw.split(",")]
        else:
            values = [str(item).strip() for item in raw]
        return [item for item in values if item]
