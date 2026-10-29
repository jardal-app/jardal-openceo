"""GlitchTip via Sentry SDK (TASK-2026-00371, diseno v2 ap. 16.3).

Lente humana auxiliar: las excepciones llegan con stack trace y release.
Nada del pipeline depende de ella: sin DSN queda desactivada y el
sistema sigue funcionando. Sin red ni Mongo en este modulo.
"""

from __future__ import annotations

import os

from .sanitize import sanitize_message

GLITCHTIP_DSN_ENV = "OPENCEO_GLITCHTIP_DSN"
ENV_ENV = "OPENCEO_ENV"
DEFAULT_ENVIRONMENT = "production"

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "ENV_ENV",
    "GLITCHTIP_DSN_ENV",
    "capture_exception",
    "init_error_tracking",
    "is_active",
    "scrub_sentry_event",
]

_ACTIVE = False


def is_active() -> bool:
    return _ACTIVE


def _scrub_frames(frames: object) -> None:
    if not isinstance(frames, list):
        return
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        frame.pop("vars", None)
        for key in ("pre_context", "post_context"):
            value = frame.get(key)
            if isinstance(value, list):
                frame[key] = [sanitize_message(str(line)) for line in value]


def scrub_sentry_event(event: dict) -> dict | None:
    """Redaccion de diseno ap. 15.2 para el evento Sentry/GlitchTip.

    Falla cerrada: sin dict valido devuelve None (el SDK descarta).
    Quita usuario, adjuntos de request y variables locales; redacta
    textos libres con el mismo saneado de la captura.
    """
    if not isinstance(event, dict):
        return None
    event.pop("user", None)
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = sanitize_message(message)
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("headers", None)
        request.pop("cookies", None)
        request.pop("data", None)
        url = request.get("url")
        if isinstance(url, str):
            request["url"] = sanitize_message(url)
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                if isinstance(value, str):
                    item["value"] = sanitize_message(value)
                stacktrace = item.get("stacktrace")
                if isinstance(stacktrace, dict):
                    _scrub_frames(stacktrace.get("frames"))
    threads = event.get("threads")
    if isinstance(threads, dict):
        values = threads.get("values")
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    stacktrace = item.get("stacktrace")
                    if isinstance(stacktrace, dict):
                        _scrub_frames(stacktrace.get("frames"))
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        values = breadcrumbs.get("values")
        if isinstance(values, list):
            for crumb in values:
                if not isinstance(crumb, dict):
                    continue
                crumb_message = crumb.get("message")
                if isinstance(crumb_message, str):
                    crumb["message"] = sanitize_message(crumb_message)
                crumb.pop("data", None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        for name in ("user", "device", "os", "runtime"):
            contexts.pop(name, None)
    return event


def _coerce_release(release: object) -> str | None:
    if isinstance(release, dict):
        sha = release.get("sha")
        return str(sha) if sha else None
    if isinstance(release, str) and release:
        return release
    return None


def init_error_tracking(
    release: object = None,
    environment: str | None = None,
) -> bool:
    """Activa el SDK si hay DSN; si no, queda desactivado sin romper nada.

    Lee el DSN solo de la variable OPENCEO_GLITCHTIP_DSN (va en el .env
    del servicio, nunca en git ni en logs). La release sale de RELEASE.
    Devuelve True solo cuando el SDK queda inicializado.
    """
    global _ACTIVE
    dsn = os.environ.get(GLITCHTIP_DSN_ENV, "").strip()
    if not dsn:
        _ACTIVE = False
        return False
    try:
        import sentry_sdk
    except ImportError:
        _ACTIVE = False
        return False
    try:
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        integrations = [AsyncioIntegration()]
    except ImportError:
        integrations = None
    options = {
        "dsn": dsn,
        "release": _coerce_release(release),
        "environment": environment
        or os.environ.get(ENV_ENV, DEFAULT_ENVIRONMENT),
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_sentry_event,
    }
    if integrations is not None:
        options["integrations"] = integrations
    sentry_sdk.init(**options)
    _ACTIVE = True
    return True


def capture_exception() -> str | None:
    """Captura la excepcion activa y devuelve su id para glitchtip_event_id.

    Solo util con el SDK activo; nunca lanza ni escribe en Mongo.
    """
    if not _ACTIVE:
        return None
    try:
        import sentry_sdk
        return sentry_sdk.capture_exception()
    except BaseException:  # noqa: BLE001 - capturada: el pipeline sigue
        return None
