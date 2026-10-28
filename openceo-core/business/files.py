import base64
import binascii
from typing import Any

from tools.jardal_erp_tickets import AttachmentRejected, assert_attachment_name_allowed, resolve_attachment_path, sanitize_file_name


def read_attachment(settings: Any, *, source_path: str | None, content_base64: str | None, file_name: str | None) -> tuple[bytes, str]:
    if bool(source_path) == bool(content_base64):
        raise AttachmentRejected("Da exactamente una fuente de archivo.", status="invalid_input")
    limit = int(getattr(settings, "JARDAL_ERP_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024))
    if source_path:
        roots = str(getattr(settings, "JARDAL_ERP_ATTACHMENT_ALLOWED_ROOTS", "")).split(",")
        path = resolve_attachment_path(source_path, [root.strip() for root in roots if root.strip()])
        if path.stat().st_size > limit:
            raise AttachmentRejected("El archivo supera el límite permitido.", status="too_large")
        content, name = path.read_bytes(), file_name or path.name
    else:
        if len(content_base64 or "") > (limit + 2) // 3 * 4:
            raise AttachmentRejected("El archivo supera el límite permitido.", status="too_large")
        try:
            content, name = base64.b64decode(content_base64 or "", validate=True), file_name
        except (binascii.Error, ValueError):
            raise AttachmentRejected("Contenido base64 inválido.", status="invalid_input") from None
    if not content or len(content) > limit:
        raise AttachmentRejected("Tamaño de archivo inválido.", status="invalid_input")
    name = sanitize_file_name(name)
    assert_attachment_name_allowed(name)
    return content, name
