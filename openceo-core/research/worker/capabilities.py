"""Capacidades cortas del worker de research (TASK-2026-00063).

Una capability es la **unica** identidad que el broker acepta de una llamada de
herramienta. No es un Bearer compartido ni un `owner_id` del payload: es un token
opaco, ligado a un job concreto, con operaciones permitidas y vencimiento.

Reglas que implementa
=====================
* El alcance (proyecto, owner, research_id) sale **de la capability**, nunca de los
  argumentos de la herramienta ni de `_meta.sessionID`. Ese campo es correlacion,
  no autorizacion.
* El token se guarda **hasheado**: una copia del almacen no concede acceso.
* La capability se revoca al cancelar o terminar el job, y vence por si sola.
* Cada operacion se autoriza de forma explicita: una capability de descubrimiento no
  sirve para leer.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from research.contracts import Classification

__all__ = [
    "BROKER_OPERATIONS",
    "Capability",
    "CapabilityError",
    "CapabilityScope",
    "CapabilityStore",
    "ExpiredCapability",
    "InvalidCapability",
    "OperationNotAllowed",
    "RevokedCapability",
]

# Operaciones que el broker del worker expone. Son deliberadamente las tres del
# ticket: descubrir, leer y grounding. No hay operacion de escritura ni de tickets.
BROKER_OPERATIONS = ("research_discover", "research_read", "research_grounded_lookup")

DEFAULT_TTL_SECONDS = 900


class CapabilityError(RuntimeError):
    """La capability no autoriza la llamada. El motivo es tipado, no textual."""

    reason = "capability_error"

    def __init__(self, detail: str, *, operation: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.operation = operation

    def to_payload(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail, "operation": self.operation}


class InvalidCapability(CapabilityError):
    reason = "invalid_capability"


class ExpiredCapability(CapabilityError):
    reason = "expired_capability"


class RevokedCapability(CapabilityError):
    reason = "revoked_capability"


class OperationNotAllowed(CapabilityError):
    reason = "operation_not_allowed"


@dataclass(frozen=True)
class CapabilityScope:
    """Alcance efectivo de un job. Es el unico origen de identidad del broker."""

    project_key: str
    owner_id: str
    research_id: str
    classification: Classification = Classification.PUBLIC
    account_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "project_key": self.project_key,
            "owner_id": self.owner_id,
            "research_id": self.research_id,
            "classification": self.classification.value,
            "account_refs": list(self.account_refs),
        }


@dataclass(frozen=True)
class Capability:
    token: str
    scope: CapabilityScope
    operations: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def permits(self, operation: str) -> bool:
        return operation in self.operations

    def to_payload(self) -> dict[str, object]:
        # El token no se serializa: quien lo tiene ya lo tiene; quien no, no debe
        # recibirlo por un canal de diagnostico.
        return {
            "scope": self.scope.to_payload(),
            "operations": sorted(self.operations),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked": self.revoked,
        }


@dataclass
class _StoredCapability:
    digest: str
    capability: Capability
    revoked: bool = False
    revoked_reason: str = ""
    used_operations: list[str] = field(default_factory=list)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CapabilityStore:
    """Almacen de capacidades en memoria del proceso del broker.

    En memoria es suficiente para el arranque: el broker y el worker comparten
    despliegue, y la capability es corta. Persistirla mas alla del job solo ampliaria
    la superficie de un secreto que debe morir con el.
    """

    def __init__(self, *, clock=None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._by_digest: dict[str, _StoredCapability] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = int(ttl_seconds)

    # ---------------------------------------------------------------- emitir

    def issue(
        self,
        *,
        scope: CapabilityScope,
        operations: Iterable[str] = BROKER_OPERATIONS,
        ttl_seconds: int | None = None,
    ) -> Capability:
        allowed = frozenset(operations)
        unknown = allowed - set(BROKER_OPERATIONS)
        if unknown:
            # Una capability no puede nombrar una operacion que el broker no expone:
            # seria una via para ampliar la superficie por configuracion.
            raise OperationNotAllowed(f"Operacion desconocida: {sorted(unknown)}")
        ttl = int(self._ttl if ttl_seconds is None else ttl_seconds)
        if ttl <= 0:
            raise ValueError("Una capability necesita un vencimiento positivo")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        capability = Capability(
            token=token,
            scope=scope,
            operations=allowed,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._by_digest[_digest(token)] = _StoredCapability(digest=_digest(token), capability=capability)
        return capability

    # ------------------------------------------------------------- autenticar

    def authenticate(self, token: str, *, operation: str) -> Capability:
        """Autoriza una operacion concreta. Falla con motivo tipado si no procede."""
        if not token or not isinstance(token, str):
            raise InvalidCapability("Falta la capability", operation=operation)
        stored = self._by_digest.get(_digest(token))
        if stored is None:
            raise InvalidCapability("Capability desconocida", operation=operation)
        capability = stored.capability
        # Comparacion en tiempo constante del token presentado contra el emitido.
        if not hmac.compare_digest(capability.token, token):
            raise InvalidCapability("Capability invalida", operation=operation)
        if stored.revoked or capability.revoked:
            raise RevokedCapability(
                f"Capability revocada: {stored.revoked_reason or 'job terminado'}",
                operation=operation,
            )
        if capability.expires_at <= self._clock():
            raise ExpiredCapability("Capability vencida", operation=operation)
        if operation not in capability.operations:
            raise OperationNotAllowed(
                f"La capability no autoriza {operation}", operation=operation
            )
        stored.used_operations.append(operation)
        return capability

    # --------------------------------------------------------------- revocar

    def revoke(self, token: str, *, reason: str = "revocada explicitamente") -> bool:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return False
        stored.revoked = True
        stored.revoked_reason = reason
        return True

    def revoke_for_job(self, research_id: str, *, reason: str = "job terminado") -> int:
        """Revoca todas las capacidades de un job. Se llama al cancelar o terminar."""
        revoked = 0
        for stored in self._by_digest.values():
            if stored.capability.scope.research_id != research_id:
                continue
            if not stored.revoked:
                stored.revoked = True
                stored.revoked_reason = reason
                revoked += 1
        return revoked

    def purge_expired(self) -> int:
        """Retira capacidades vencidas. No hay barrido de fondo: se llama al operar."""
        now = self._clock()
        stale = [
            digest
            for digest, stored in self._by_digest.items()
            if stored.capability.expires_at <= now
        ]
        for digest in stale:
            self._by_digest.pop(digest, None)
        return len(stale)

    def issued_count(self) -> int:
        return len(self._by_digest)

    def record_for(self, token: str) -> dict[str, object] | None:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return None
        payload = stored.capability.to_payload()
        payload["used_operations"] = list(stored.used_operations)
        payload["revoked_reason"] = stored.revoked_reason
        return payload
