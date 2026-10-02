"""Credenciales de consumidor del servicio research (TASK-2026-00066).

Por que no sirve el Bearer de las monturas internas
==================================================
`mcp_server.py` protege el **transporte** con un Bearer compartido y una restriccion de
loopback. Eso controla quien alcanza el socket, pero **no identifica al owner**: todos
los consumidores de esa montura son el mismo principal. Publicar ahi herramientas
multiowner supondria una identidad que no existe.

Por eso el servicio define sus propias credenciales de consumidor: un token opaco,
revocable, con un `owner_id` explicito (el principal en cuyo nombre actua) y un
conjunto de proyectos y operaciones concedidos. El token se guarda **hasheado** y el
owner **no** viaja nunca en los argumentos de la herramienta ni en `_meta.sessionID`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "CONSUMER_OPERATIONS",
    "DEFAULT_CONSUMER_OPERATIONS",
    "ConsumerAuthError",
    "ConsumerForbidden",
    "ConsumerGrant",
    "ConsumerRegistry",
]

# Operaciones que un consumidor puede tener concedidas. No hay operacion de escritura de
# tickets. Las de suscripcion (TASK-2026-00069) programan trabajo recurrente, asi que se
# conceden de forma explicita: no forman parte del conjunto por defecto.
#   subscribe            crear una suscripcion
#   subscriptions        listar, leer y ver los eventos de las propias
#   manage_subscription  editar, pausar, reanudar y cancelar las propias
CONSUMER_OPERATIONS = (
    "submit", "get", "cancel", "subscribe", "subscriptions", "manage_subscription", "events",
)
DEFAULT_CONSUMER_OPERATIONS = ("submit", "get", "cancel")

DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class ConsumerAuthError(RuntimeError):
    """El consumidor no esta autenticado. Motivo tipado, no textual."""

    reason = "unauthenticated"
    status_code = 401

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def to_payload(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


class ConsumerForbidden(ConsumerAuthError):
    """El consumidor esta autenticado pero no tiene el grant pedido."""

    reason = "forbidden"
    status_code = 403


@dataclass(frozen=True)
class ConsumerGrant:
    """Alcance concedido a un consumidor. Todo lo demas se deriva de aqui."""

    consumer_id: str
    owner_id: str
    projects: frozenset[str]
    operations: frozenset[str]
    issued_at: datetime
    expires_at: datetime | None = None

    def allows(self, operation: str, *, project_key: str | None = None) -> bool:
        if operation not in self.operations:
            return False
        if project_key is None:
            return True
        return "*" in self.projects or project_key in self.projects

    def require(self, operation: str, *, project_key: str | None = None) -> None:
        if not self.allows(operation, project_key=project_key):
            target = f" sobre {project_key}" if project_key else ""
            raise ConsumerForbidden(
                f"El consumidor no tiene la operacion {operation}{target}"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "consumer_id": self.consumer_id,
            "owner_id": self.owner_id,
            "projects": sorted(self.projects),
            "operations": sorted(self.operations),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class _StoredConsumer:
    digest: str
    grant: ConsumerGrant
    revoked: bool = False
    revoked_reason: str = ""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ConsumerRegistry:
    """Alta y verificacion de consumidores. En memoria, revocable en caliente."""

    def __init__(self, *, clock=None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._by_digest: dict[str, _StoredConsumer] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = int(ttl_seconds)
        self._revocation_listeners: list[Callable[[str, str], Any]] = []

    def add_revocation_listener(self, listener: Callable[[str, str], Any]) -> None:
        """Avisa ``(consumer_id, motivo)`` al revocar. Lo usan las suscripciones.

        El registro es en memoria; lo duradero es la cuenta del owner. Pero revocar un
        consumidor en este proceso debe detener de inmediato lo que programo en su nombre.
        """
        self._revocation_listeners.append(listener)

    def _notify_revoked(self, consumer_id: str, reason: str) -> None:
        for listener in list(self._revocation_listeners):
            try:
                listener(consumer_id, reason)
            except Exception:  # noqa: BLE001 - un oyente roto no puede impedir la revocacion
                logger.warning("Oyente de revocacion fallo", exc_info=True)

    def register(
        self,
        *,
        consumer_id: str,
        owner_id: str,
        projects: Iterable[str],
        operations: Iterable[str] = DEFAULT_CONSUMER_OPERATIONS,
        ttl_seconds: int | None = None,
    ) -> str:
        """Alta de consumidor. Devuelve el token **una sola vez**."""
        if not str(consumer_id or "").strip():
            raise ValueError("El consumidor necesita un identificador")
        if not str(owner_id or "").strip():
            # Sin owner explicito, la credencial no puede actuar en nombre de nadie.
            raise ValueError("El consumidor necesita un owner_id explicito")
        granted = frozenset(operations)
        unknown = granted - set(CONSUMER_OPERATIONS)
        if unknown:
            raise ValueError(f"Operacion desconocida: {sorted(unknown)}")
        if not granted:
            raise ValueError("Un consumidor necesita al menos una operacion")
        project_set = frozenset(str(p) for p in projects if str(p or "").strip())
        if not project_set:
            raise ValueError("Un consumidor necesita al menos un proyecto")
        ttl = int(self._ttl if ttl_seconds is None else ttl_seconds)
        if ttl <= 0:
            raise ValueError("La credencial necesita un vencimiento positivo")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        grant = ConsumerGrant(
            consumer_id=consumer_id,
            owner_id=owner_id,
            projects=project_set,
            operations=granted,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._by_digest[_digest(token)] = _StoredConsumer(digest=_digest(token), grant=grant)
        return token

    def authenticate(self, token: str | None) -> ConsumerGrant:
        if not token or not isinstance(token, str):
            raise ConsumerAuthError("Falta la credencial de consumidor")
        stored = self._by_digest.get(_digest(token))
        if stored is None:
            raise ConsumerAuthError("Credencial de consumidor desconocida")
        if not hmac.compare_digest(_digest(token), stored.digest):
            raise ConsumerAuthError("Credencial de consumidor invalida")
        if stored.revoked:
            raise ConsumerAuthError(
                f"Credencial revocada: {stored.revoked_reason or 'sin motivo'}"
            )
        expires = stored.grant.expires_at
        if expires is not None and expires <= self._clock():
            raise ConsumerAuthError("Credencial de consumidor vencida")
        return stored.grant

    def revoke(self, token: str, *, reason: str = "revocada explicitamente") -> bool:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return False
        if stored.revoked:
            return True  # idempotente: ya estaba revocada y ya se aviso
        stored.revoked = True
        stored.revoked_reason = reason
        # Un consumidor puede tener varias credenciales (rotacion). Solo cuando no le queda
        # ninguna viva pierde lo que programo; revocar una vieja no detiene al consumidor.
        if not self._has_live_credential(stored.grant.consumer_id):
            self._notify_revoked(stored.grant.consumer_id, reason)
        return True

    def _has_live_credential(self, consumer_id: str) -> bool:
        now = self._clock()
        return any(
            stored.grant.consumer_id == consumer_id and not stored.revoked
            and (stored.grant.expires_at is None or stored.grant.expires_at > now)
            for stored in self._by_digest.values()
        )

    def revoke_consumer(self, consumer_id: str, *, reason: str = "consumidor revocado") -> int:
        revoked = 0
        for stored in self._by_digest.values():
            if stored.grant.consumer_id != consumer_id or stored.revoked:
                continue
            stored.revoked = True
            stored.revoked_reason = reason
            revoked += 1
        if revoked:
            self._notify_revoked(consumer_id, reason)
        return revoked

    def purge_expired(self) -> int:
        now = self._clock()
        stale = [
            digest
            for digest, stored in self._by_digest.items()
            if stored.grant.expires_at is not None and stored.grant.expires_at <= now
        ]
        for digest in stale:
            self._by_digest.pop(digest, None)
        return len(stale)

    def count(self) -> int:
        return len(self._by_digest)

    def record_for(self, token: str) -> dict[str, object] | None:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return None
        payload = stored.grant.to_payload()
        payload["revoked"] = stored.revoked
        payload["revoked_reason"] = stored.revoked_reason
        return payload
