"""Presupuesto con reservas atomicas para el servicio research (TASK-2026-00057).

Por que existe
==============
El epico exige que antes de cualquier I/O se reserve coste y margen contra el saldo
**global de la cuenta** y contra un limite de **proyecto/job**, y que esa reserva sea
atomica. Dos escrituras independientes (insertar un evento de uso y ademas reclamar
un job) no protegen un saldo compartido: dos procesos pueden leer el mismo saldo y
gastarlo dos veces.

Mecanismo elegido
=================
Atomicidad **de un solo documento** de Mongo. La coleccion
``research_budget_accounts`` guarda, en un unico documento por cuenta/proveedor:

* ``spent_micro_usd``: gasto ya reconciliado;
* ``ceiling_micro_usd``: techo autorizado (credito conocido + presupuesto pagado
  aprobado); ``None`` = desconocido;
* ``reservations``: lista de reservas vivas (``reserved``/``uncertain``).

La reserva se decide y se aplica en **una sola** ``find_one_and_update`` con
pipeline: el filtro de capacidad, la decision y la mutacion de contadores ocurren
sobre la misma version del documento, asi que dos reservas concurrentes por el
ultimo saldo se serializan y **solo una** procede. No se usa una transaccion
multi-documento (Mongo standalone no la soporta), y por eso tampoco se reparte el
estado en varios documentos: la atomicidad vive dentro del documento.

``research_usage`` es el libro de auditoria append-only, idempotente por
``operation_id``. La autoridad de reservas (cuenta) y el libro de uso estan
separados: primero se registra el uso, despues se retira la reserva. Un crash entre
ambos deja la reserva viva, que es el lado **conservador** del error, y la
reconciliacion lo repara de forma idempotente. No se aplica TTL a la autoridad de
reserva viva.

Dinero: enteros escalados en micro-USD (`research.money`), nunca floats. Un importe
no medido es ``None``, nunca 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from research.money import format_micro

logger = logging.getLogger(__name__)

__all__ = [
    "BudgetError",
    "PaidBudgetDisabled",
    "ReserveDecision",
    "Reservation",
    "ReconcileReport",
    "ResearchBudgetLedger",
    "ResearchPolicyLedger",
    "Settlement",
    "account_key",
]

LIVE_STATES = ("reserved", "uncertain")


class BudgetError(RuntimeError):
    """Fallo de presupuesto que impide continuar con la operacion."""


class PaidBudgetDisabled(BudgetError):
    """Se intento una operacion pagada con presupuesto pagado en cero."""


def account_key(provider: str, account: str = "default") -> str:
    """Clave estable de la cuenta/proveedor que posee el saldo.

    Las cuotas pertenecen a cuenta/equipo: rotar claves del mismo equipo no crea
    saldo, por eso la clave no incluye la clave de API.
    """
    return f"{provider}:{account}"


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    provider: str
    account_id: str
    amount_micro_usd: int
    project_key: str
    research_id: str
    state: str = "reserved"
    created_at: datetime | None = None
    reused: bool = False

    def to_doc(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "amount_micro_usd": int(self.amount_micro_usd),
            "project_key": self.project_key,
            "research_id": self.research_id,
            "state": self.state,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ReserveDecision:
    accepted: bool
    reservation: Reservation | None = None
    reason: str | None = None

    @property
    def reused(self) -> bool:
        return bool(self.reservation and self.reservation.reused)


@dataclass(frozen=True)
class Settlement:
    operation_id: str
    state: str  # committed | released | uncertain
    charged_micro_usd: int | None
    reconciled: bool
    already_settled: bool = False


@dataclass
class ReconcileReport:
    settled: list[Settlement] = field(default_factory=list)
    orphaned: list[dict[str, Any]] = field(default_factory=list)
    examined: int = 0
    accounts: int = 0

    def to_doc(self) -> dict[str, Any]:
        return {
            "accounts": self.accounts,
            "examined": self.examined,
            "settled": [
                {
                    "operation_id": s.operation_id,
                    "state": s.state,
                    "charged_usd": format_micro(s.charged_micro_usd),
                    "reconciled": s.reconciled,
                    "already_settled": s.already_settled,
                }
                for s in self.settled
            ],
            "orphaned": self.orphaned,
        }


class ResearchBudgetLedger:
    """Autoridad de saldo y reservas sobre dos colecciones Mongo.

    ``accounts`` y ``usage`` son colecciones de motor (o cualquier objeto con la misma
    interfaz minima). El ledger no conoce proveedores concretos: recibe el techo
    efectivo ya calculado por la politica, de modo que la politica no puede delegar
    en el ledger la decision de cuanto es "razonable" gastar.
    """

    def __init__(
        self,
        *,
        accounts: Any,
        usage: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._accounts = accounts
        self._usage = usage
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ reserve

    async def reserve(
        self,
        *,
        provider: str,
        amount_micro_usd: int,
        operation_id: str,
        project_key: str,
        research_id: str,
        ceiling_micro_usd: int | None,
        project_limit_micro_usd: int | None = None,
        job_limit_micro_usd: int | None = None,
        allow_unknown_ceiling: bool = False,
        account: str = "default",
    ) -> ReserveDecision:
        """Reserva coste antes del I/O, de forma atomica.

        Rechaza **antes de escribir** una operacion pagada cuando el techo es cero: una
        politica de pago cero no debe llegar a hacer la llamada. Con techo desconocido
        (`None`) tambien rechaza salvo autorizacion explicita.
        """
        amount = int(amount_micro_usd)
        if amount < 0:
            raise BudgetError("Una reserva no puede ser negativa")
        if amount > 0 and ceiling_micro_usd == 0:
            raise PaidBudgetDisabled(
                f"{provider}: presupuesto pagado en cero; no se autoriza una operacion de "
                f"{format_micro(amount)} USD"
            )
        if amount > 0 and ceiling_micro_usd is None and not allow_unknown_ceiling:
            raise PaidBudgetDisabled(
                f"{provider}: techo de cuenta desconocido; un saldo no medido no autoriza gasto"
            )

        key = account_key(provider, account)
        now = self._clock()
        entry = Reservation(
            operation_id=operation_id,
            provider=provider,
            account_id=account,
            amount_micro_usd=amount,
            project_key=project_key,
            research_id=research_id,
            state="reserved",
            created_at=now,
        )
        pipeline = self._reserve_pipeline(
            entry=entry,
            ceiling_micro_usd=ceiling_micro_usd,
            project_limit_micro_usd=project_limit_micro_usd,
            job_limit_micro_usd=job_limit_micro_usd,
            allow_unknown_ceiling=allow_unknown_ceiling,
            now=now,
        )
        # Dos primeras reservas concurrentes de la misma cuenta pueden chocar al
        # insertar el documento (ambas ven que no existe). Reintentar una vez basta:
        # la segunda ya encuentra el documento y aplica el CAS normalmente. Sin este
        # reintento, una reserva legitima fallaria por una carrera de creacion.
        try:
            doc = await self._accounts.find_one_and_update(
                {"_id": key},
                pipeline,
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            logger.debug("Carrera al crear la autoridad de saldo %s; se reintenta", key)
            doc = await self._accounts.find_one_and_update(
                {"_id": key},
                pipeline,
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        if doc is None:
            raise BudgetError(f"No se pudo abrir la autoridad de saldo {key}")

        outcome = doc.get("last_reservation")
        stored = _find_reservation(doc.get("reservations") or [], operation_id)
        if outcome == "accepted" or outcome == "reused":
            live = stored or entry
            return ReserveDecision(
                accepted=True,
                reservation=Reservation(
                    operation_id=live["operation_id"],
                    provider=live.get("provider", provider),
                    account_id=live.get("account_id", account),
                    amount_micro_usd=int(live["amount_micro_usd"]),
                    project_key=live.get("project_key", project_key),
                    research_id=live.get("research_id", research_id),
                    state=live.get("state", "reserved"),
                    created_at=live.get("created_at"),
                    reused=outcome == "reused",
                ),
            )
        return ReserveDecision(accepted=False, reason=outcome or "rejected")

    @staticmethod
    def _reserve_pipeline(
        *,
        entry: Reservation,
        ceiling_micro_usd: int | None,
        project_limit_micro_usd: int | None,
        job_limit_micro_usd: int | None,
        allow_unknown_ceiling: bool,
        now: datetime,
    ) -> list[dict[str, Any]]:
        amount = int(entry.amount_micro_usd)

        if ceiling_micro_usd is None:
            ceiling_ok: Any = {"$literal": bool(allow_unknown_ceiling)}
        else:
            ceiling_ok = {
                "$lte": [
                    {"$add": ["$spent_micro_usd", "$_global", amount]},
                    int(ceiling_micro_usd),
                ]
            }
        project_ok: Any = (
            {"$literal": True}
            if project_limit_micro_usd is None
            else {"$lte": [{"$add": ["$_project", amount]}, int(project_limit_micro_usd)]}
        )
        job_ok: Any = (
            {"$literal": True}
            if job_limit_micro_usd is None
            else {"$lte": [{"$add": ["$_job", amount]}, int(job_limit_micro_usd)]}
        )

        return [
            {
                "$set": {
                    "provider": entry.provider,
                    "account_id": entry.account_id,
                    "currency": "USD",
                    "ceiling_micro_usd": ceiling_micro_usd,
                    "spent_micro_usd": {"$ifNull": ["$spent_micro_usd", 0]},
                    "reservations": {"$ifNull": ["$reservations", []]},
                    "updated_at": now,
                }
            },
            {
                "$set": {
                    "_live": {
                        "$filter": {
                            "input": "$reservations",
                            "as": "r",
                            "cond": {"$in": ["$$r.state", list(LIVE_STATES)]},
                        }
                    },
                    "_dupe": {
                        "$size": {
                            "$filter": {
                                "input": "$reservations",
                                "as": "r",
                                "cond": {"$eq": ["$$r.operation_id", entry.operation_id]},
                            }
                        }
                    },
                }
            },
            {
                "$set": {
                    "_global": {
                        "$sum": {
                            "$map": {
                                "input": "$_live",
                                "as": "r",
                                "in": "$$r.amount_micro_usd",
                            }
                        }
                    },
                    "_project": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$_live",
                                        "as": "r",
                                        "cond": {"$eq": ["$$r.project_key", entry.project_key]},
                                    }
                                },
                                "as": "r",
                                "in": "$$r.amount_micro_usd",
                            }
                        }
                    },
                    "_job": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$_live",
                                        "as": "r",
                                        "cond": {"$eq": ["$$r.research_id", entry.research_id]},
                                    }
                                },
                                "as": "r",
                                "in": "$$r.amount_micro_usd",
                            }
                        }
                    },
                }
            },
            {"$set": {"_ceiling_ok": ceiling_ok, "_project_ok": project_ok, "_job_ok": job_ok}},
            {"$set": {"_ok": {"$and": ["$_ceiling_ok", "$_project_ok", "$_job_ok"]}}},
            {
                "$set": {
                    "reservations": {
                        "$cond": [
                            {"$gt": ["$_dupe", 0]},
                            "$reservations",
                            {
                                "$cond": [
                                    "$_ok",
                                    {"$concatArrays": ["$reservations", [entry.to_doc()]]},
                                    "$reservations",
                                ]
                            },
                        ]
                    },
                    "last_reservation": {
                        "$cond": [
                            {"$gt": ["$_dupe", 0]},
                            "reused",
                            {
                                "$cond": [
                                    "$_ok",
                                    "accepted",
                                    {
                                        "$cond": [
                                            "$_ceiling_ok",
                                            {
                                                "$cond": [
                                                    "$_project_ok",
                                                    {
                                                        "$cond": [
                                                            "$_job_ok",
                                                            "accepted",
                                                            "job_limit",
                                                        ]
                                                    },
                                                    "project_limit",
                                                ]
                                            },
                                            "account_ceiling",
                                        ]
                                    },
                                ]
                            },
                        ]
                    },
                    "updated_at": now,
                }
            },
            {
                "$unset": [
                    "_live",
                    "_dupe",
                    "_global",
                    "_project",
                    "_job",
                    "_ceiling_ok",
                    "_project_ok",
                    "_job_ok",
                    "_ok",
                ]
            },
        ]

    # ------------------------------------------------------------------ settle

    async def commit(self, reservation: Reservation, *, charged_micro_usd: int | None) -> Settlement:
        """Liquida una reserva con el consumo real.

        ``charged_micro_usd=None`` significa **consumo incierto** (timeout ambiguo): la
        reserva se mantiene viva y marcada ``uncertain``; no se devuelve como gasto 0.
        """
        amount = int(reservation.amount_micro_usd)
        if charged_micro_usd is None:
            return await self.mark_uncertain(reservation)
        charged = int(charged_micro_usd)
        if charged < 0:
            raise BudgetError("Un consumo no puede ser negativo")
        if charged > amount:
            # El proveedor puede facturar mas de lo reservado (varias busquedas internas,
            # tokens imprevistos). Se reconoce el exceso en vez de ocultarlo, pero se
            # registra para que la politica ajuste el margen.
            logger.info(
                "Consumo %s supera la reserva para %s (%s > %s)",
                format_micro(charged),
                reservation.operation_id,
                format_micro(charged),
                format_micro(amount),
            )

        wrote = await self._record_usage(
            reservation,
            state="committed",
            charged_micro_usd=charged,
        )
        settled = await self._settle_account(reservation, charged_micro_usd=charged)
        return Settlement(
            operation_id=reservation.operation_id,
            state="committed",
            charged_micro_usd=charged,
            reconciled=True,
            already_settled=not settled,
        )

    async def release(self, reservation: Reservation) -> Settlement:
        """Libera una reserva sin consumo. Idempotente."""
        await self._record_usage(reservation, state="released", charged_micro_usd=0)
        settled = await self._settle_account(reservation, charged_micro_usd=None)
        return Settlement(
            operation_id=reservation.operation_id,
            state="released",
            charged_micro_usd=0,
            reconciled=True,
            already_settled=not settled,
        )

    async def mark_uncertain(self, reservation: Reservation) -> Settlement:
        """Mantiene la reserva viva como consumo incierto (no la devuelve como 0)."""
        now = self._clock()
        await self._record_usage(reservation, state="uncertain", charged_micro_usd=None)
        await self._accounts.update_one(
            {"_id": account_key(reservation.provider, reservation.account_id),
             "reservations.operation_id": reservation.operation_id},
            {"$set": {"reservations.$.state": "uncertain", "updated_at": now}},
        )
        return Settlement(
            operation_id=reservation.operation_id,
            state="uncertain",
            charged_micro_usd=None,
            reconciled=False,
        )

    async def _record_usage(
        self,
        reservation: Reservation,
        *,
        state: str,
        charged_micro_usd: int | None,
    ) -> bool:
        """Libro append-only idempotente por ``operation_id``. Devuelve si era nuevo."""
        now = self._clock()
        doc = {
            "operation_id": reservation.operation_id,
            "provider": reservation.provider,
            "account_id": reservation.account_id,
            "project_key": reservation.project_key,
            "research_id": reservation.research_id,
            "reserved_micro_usd": int(reservation.amount_micro_usd),
            "charged_micro_usd": charged_micro_usd,
            "state": state,
            "recorded_at": now,
        }
        result = await self._usage.update_one(
            {"operation_id": reservation.operation_id},
            {"$setOnInsert": doc, "$set": {"last_state": state}},
            upsert=True,
        )
        return bool(getattr(result, "upserted_id", None))

    async def _settle_account(
        self,
        reservation: Reservation,
        *,
        charged_micro_usd: int | None,
    ) -> bool:
        """Retira la reserva viva; incrementa el gasto solo si realmente existia.

        El filtro exige que la reserva siga viva. Esa condicion es la que hace la
        operacion idempotente sin contadores adicionales: repetirla no incrementa dos
        veces, porque la segunda vez ya no hay nada que retirar.
        """
        update: dict[str, Any] = {
            "$pull": {"reservations": {"operation_id": reservation.operation_id}},
            "$set": {"updated_at": self._clock()},
        }
        if charged_micro_usd:
            update["$inc"] = {"spent_micro_usd": int(charged_micro_usd)}
        result = await self._accounts.update_one(
            {
                "_id": account_key(reservation.provider, reservation.account_id),
                "reservations.operation_id": reservation.operation_id,
            },
            update,
        )
        return bool(getattr(result, "modified_count", 0))

    # --------------------------------------------------------------- reconcile

    async def reconcile(
        self,
        *,
        grace_seconds: int | None = None,
        terminal_job_ids: Iterable[str] | None = None,
        release_orphans: bool = False,
    ) -> ReconcileReport:
        """Repara reservas huerfanas tras un crash, de forma idempotente.

        Una reserva viva con su uso ya registrado se liquida. Sin registro de uso no
        hay forma de saber si el I/O ocurrio, asi que **no se libera por defecto**: se
        marca incierta y se reporta. Liberarla seria contabilizar como gratis un gasto
        que pudo existir. ``release_orphans=True`` exige ademas que el job sea terminal.
        """
        report = ReconcileReport()
        now = self._clock()
        grace = grace_seconds
        terminal = set(terminal_job_ids) if terminal_job_ids is not None else None

        async for doc in self._accounts.find({}):
            report.accounts += 1
            live = [
                r for r in (doc.get("reservations") or [])
                if r.get("state") in LIVE_STATES
            ]
            for raw in live:
                report.examined += 1
                reservation = _reservation_from_doc(doc, raw)
                usage = await self._usage.find_one({"operation_id": reservation.operation_id})
                if usage is not None and usage.get("state") == "committed" and usage.get(
                    "charged_micro_usd"
                ) is not None:
                    await self._settle_account(
                        reservation, charged_micro_usd=int(usage["charged_micro_usd"])
                    )
                    report.settled.append(
                        Settlement(
                            operation_id=reservation.operation_id,
                            state="committed",
                            charged_micro_usd=int(usage["charged_micro_usd"]),
                            reconciled=True,
                        )
                    )
                    continue
                if usage is not None and usage.get("state") == "released":
                    await self._settle_account(reservation, charged_micro_usd=None)
                    report.settled.append(
                        Settlement(
                            operation_id=reservation.operation_id,
                            state="released",
                            charged_micro_usd=0,
                            reconciled=True,
                        )
                    )
                    continue

                created = reservation.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (now - created).total_seconds() if created else 0.0
                if grace is not None and age < grace:
                    continue

                if (
                    release_orphans
                    and (terminal is None or reservation.research_id in terminal)
                ):
                    await self._record_usage(reservation, state="released", charged_micro_usd=0)
                    await self._settle_account(reservation, charged_micro_usd=None)
                    report.settled.append(
                        Settlement(
                            operation_id=reservation.operation_id,
                            state="released",
                            charged_micro_usd=0,
                            reconciled=True,
                        )
                    )
                else:
                    await self._accounts.update_one(
                        {"_id": doc["_id"], "reservations.operation_id": reservation.operation_id},
                        {"$set": {"reservations.$.state": "uncertain", "updated_at": now}},
                    )
                    report.orphaned.append(
                        {
                            "operation_id": reservation.operation_id,
                            "provider": reservation.provider,
                            "account_id": reservation.account_id,
                            "project_key": reservation.project_key,
                            "research_id": reservation.research_id,
                            "reserved_usd": format_micro(reservation.amount_micro_usd),
                            "age_seconds": age,
                            "resolution": "uncertain",
                        }
                    )
        return report

    # ---------------------------------------------------------------- snapshot

    async def snapshot(self, *, provider: str, account: str = "default") -> dict[str, Any]:
        """Vista honesta del saldo: estimado y reconciliado nunca se confunden."""
        doc = await self._accounts.find_one({"_id": account_key(provider, account)}) or {}
        live = [r for r in (doc.get("reservations") or []) if r.get("state") in LIVE_STATES]
        live_reserved = sum(int(r.get("amount_micro_usd") or 0) for r in live)
        uncertain_live = [r for r in live if r.get("state") == "uncertain"]
        spent = int(doc.get("spent_micro_usd") or 0)
        ceiling = doc.get("ceiling_micro_usd")
        return {
            "provider": provider,
            "account": account,
            "currency": "USD",
            "ceiling_usd": format_micro(ceiling) if ceiling is not None else None,
            "ceiling_known": ceiling is not None,
            "spent_usd": format_micro(spent),
            "reserved_usd": format_micro(live_reserved),
            "available_usd": (
                format_micro(max(int(ceiling) - spent - live_reserved, 0))
                if ceiling is not None
                else None
            ),
            # Desconocido no es cero: si hay reservas inciertas, el gasto reconciliado
            # no puede afirmarse.
            "reconciled_usd": None if uncertain_live else format_micro(spent),
            "uncertain_operations": [r.get("operation_id") for r in uncertain_live],
            "live_reservations": len(live),
            "updated_at": doc.get("updated_at"),
        }


class ResearchPolicyLedger:
    """Enforce persisted authority and a shared paid ceiling before provider I/O."""

    def __init__(self, ledger: ResearchBudgetLedger, repo: Any, paid_ceiling: int, deep_ceiling: int) -> None:
        self.ledger, self.repo = ledger, repo
        self.paid_ceiling = min(paid_ceiling, deep_ceiling)

    async def reserve(self, **kwargs: Any) -> ReserveDecision:
        provider = kwargs["provider"]
        if provider not in {"deepseek-research", "serper"}:
            return await self.ledger.reserve(**kwargs)
        job = await self.repo.get_research_job(kwargs["research_id"])
        if (not job or job["project_key"] != kwargs["project_key"]
                or job.get("classification") != "public"
                or provider == "serper" and not job.get("adapters", {}).get("serper")
                or provider == "deepseek-research" and job.get("mode") != "deep"):
            raise PaidBudgetDisabled("Proveedor no autorizado por el job persistido")
        ceiling = min(self.paid_ceiling, int(job.get("paid_budget_micro_usd") or 0))
        paid = await self.ledger.reserve(**{**kwargs, "provider": "deepseek-research", "account": "default",
            "operation_id": "paid:" + kwargs["operation_id"], "ceiling_micro_usd": ceiling,
            "project_limit_micro_usd": ceiling, "job_limit_micro_usd": ceiling})
        if not paid.accepted or paid.reused:
            return ReserveDecision(False, reason="paid_budget_exhausted")
        # There is no invoice reconciliation here. Retain the maximum across phases,
        # jobs and providers; crashes between reservations can only reduce capacity.
        await self.ledger.mark_uncertain(paid.reservation)
        return paid if provider == "deepseek-research" else await self.ledger.reserve(**kwargs)

    async def commit(self, reservation: Reservation, **kwargs: Any) -> Settlement:
        if reservation.operation_id.startswith("paid:"):
            return await self.ledger.mark_uncertain(reservation)
        return await self.ledger.commit(reservation, **kwargs)

    async def release(self, reservation: Reservation) -> Settlement:
        if reservation.operation_id.startswith("paid:"):
            return await self.ledger.mark_uncertain(reservation)
        return await self.ledger.release(reservation)

    async def mark_uncertain(self, reservation: Reservation) -> Settlement:
        return await self.ledger.mark_uncertain(reservation)


def _find_reservation(rows: list[dict[str, Any]], operation_id: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("operation_id") == operation_id:
            return row
    return None


def _reservation_from_doc(account: dict[str, Any], raw: dict[str, Any]) -> Reservation:
    return Reservation(
        operation_id=raw["operation_id"],
        provider=raw.get("provider") or account.get("provider") or "",
        account_id=raw.get("account_id") or account.get("account_id") or "default",
        amount_micro_usd=int(raw.get("amount_micro_usd") or 0),
        project_key=raw.get("project_key") or "",
        research_id=raw.get("research_id") or "",
        state=raw.get("state") or "reserved",
        created_at=raw.get("created_at"),
    )
