"""Adaptador research -> Agenda del kernel de schedule global (ticket #564).

Migra el scheduler de research (``research/scheduler.py``) al contrato del kernel
(TASK-2026-00441, Fase 2, fila research de §5.5): la planificacion vive en esta
Agenda (``next_due``/``claim_due``/``run``/``settle``/``recover_at_boot`` con
``boot_id``) y el bucle aislado de ``ResearchScheduler`` deja de arrancar en
produccion. ``ResearchScheduler`` se conserva sin cambios de comportamiento como
arreo del oraculo congelado (``tests/test_research_scheduler.py``).

Lo que se conserva, sin cambios de cadencia, backoff ni admision
===============================================================
* La toma atomica con lease + ``fencing_token`` + ``scheduled_for``.
* El cierre de la ocurrencia (``_complete``) y el reintento acotado con backoff
  (``_retry``): esta Agenda los ejecuta en ``settle`` con las mismas formulas y
  el mismo parche fenced.
* La admision: el handler es el mismo (``ResearchScheduleHandler`` tras el guard
  de ``ResearchScheduleRuntime``); un fallo de ocurrencia vuelve como
  ``Outcome(ok=False)`` y se liquida sin incidente del kernel, igual que el
  scheduler viejo lo registraba solo en el log y lo reintentaba.
* Los productores no cambian: ``ResearchScheduleStore.notify`` sigue siendo el
  aviso, solo que enlazado a ``kernel.wake("research")``.

Lo que si cambia (el motivo de la migracion)
============================================
* La recuperacion al arranque pasa de umbral por edad (900 s) a propiedad de
  arranque: ``recover_at_boot`` libera toda toma de otro ``boot_id`` sin mirar
  la edad (cierra D3 del descubrimiento). Campo aditivo opcional
  ``claimed_boot_id``; colecciones e indices sin cambios.
* La espera la posee el kernel (punto unico); esta Agenda no duerme ni crea
  tareas.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from research.scheduler import (
    DEFAULT_MAX_ATTEMPTS,
    RUN_IDLE,
    backoff_seconds,
    compute_next_run_at,
    execution_key_for,
)
from research.scheduler import _overlap_counters  # mismo paquete: conteo skip/merge
from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["ResearchScheduleAgenda"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResearchScheduleAgenda:
    """Agenda research para el kernel: un lane serial, sin espera propia.

    ``lanes = 1`` conserva la semantica serial del scheduler viejo: una
    ocurrencia en vuelo por proceso. ``run`` puede tardar (el handler admite
    trabajo); el kernel lo aisla en el carril para no bloquear a otras agendas.
    """

    name = "research"
    lanes = 1

    def __init__(
        self,
        *,
        repo: Any,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        coordinator_id: str = "research-scheduler-1",
        lease_seconds: int = 120,
        backoff_base: int = 60,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._handler = handler
        self._coordinator = coordinator_id
        self._lease_seconds = max(1, int(lease_seconds))
        self._backoff_base = max(0, int(backoff_base))
        self._now = now_fn or _utcnow
        self._boot_id: str | None = None

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """Una consulta indexada al vencimiento reclamable mas proximo."""
        return await self._repo.next_research_schedule_due_at()

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Toma atomica de hasta ``limit`` vencidos, sellados con el boot_id.

        Cada toma es el mismo ``find_one_and_update`` condicional del scheduler
        viejo; el kernel garantiza que ``recover_at_boot`` corrio antes, asi que
        ``_boot_id`` ya esta fijado cuando esto se llama.
        """
        if self._boot_id is None or limit <= 0:
            return []
        claims: list[Claim] = []
        for _ in range(limit):
            claimed = await self._repo.claim_next_research_schedule(
                lease_owner=self._coordinator,
                lease_seconds=self._lease_seconds,
                now=now,
                boot_id=self._boot_id,
            )
            if claimed is None:
                break
            schedule_id = claimed.get("schedule_id")
            if not schedule_id:
                continue
            claims.append(
                Claim(
                    item_id=str(schedule_id),
                    boot_id=self._boot_id,
                    fencing_token=int(claimed.get("fencing_token") or 0),
                    payload=claimed,
                )
            )
        return claims

    async def run(self, claim: Claim) -> Outcome:
        """Ejecuta el handler. Un fallo de ocurrencia es ``ok=False``, no incidente.

        El scheduler viejo capturaba toda excepcion del handler, la dejaba en el
        log y reintentaba con backoff; devolver ``Outcome(ok=False)`` preserva
        esa semantica en el contrato del kernel (fallo de dominio ya contabilizado:
        se liquida sin incidente ni backoff del supervisor).
        """
        claimed = claim.payload if isinstance(claim.payload, dict) else {}
        try:
            result = await self._handler(claimed)
        except Exception as exc:  # noqa: BLE001 - un fallo no tumba el carril ni el kernel
            logger.warning(
                "research: fallo el schedule %s", claim.item_id, exc_info=True
            )
            return Outcome(ok=False, error=(str(exc) or type(exc).__name__)[:500])
        return Outcome(ok=True, result=result or {})

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """Un parche fenced: cierre con proxima ocurrencia o reintento acotado.

        Replica ``_complete``/``_retry`` del scheduler viejo con las mismas
        formulas. Deja el item no reclamable (fencing viejo: se ignora) o con
        vencimiento estrictamente futuro; el kernel nunca mueve un vencimiento.
        """
        claimed = claim.payload if isinstance(claim.payload, dict) else None
        if claimed is None or not claim.item_id:
            logger.warning("research: settle sin documento para %r; queda al proximo arranque", claim.item_id)
            return
        moment = self._now()
        if outcome.ok:
            updates = self._complete_updates(claimed, outcome.result, moment)
        else:
            updates = self._retry_updates(claimed, outcome.error, moment)
        patched = await self._repo.patch_research_schedule(
            claim.item_id,
            updates,
            lease_owner=self._coordinator,
            fencing_token=claim.fencing_token,
            now=moment,
        )
        if patched is None:
            logger.debug("research: settle viejo ignorado para %s", claim.item_id)

    async def recover_at_boot(self, boot_id: str) -> int:
        """Recuerda ``boot_id`` PRIMERO y libera toda toma de otro arranque.

        Sin umbral de edad: con instancia unica por flock, cualquier ``running``
        de otro arranque esta huerfano al instante (D3). Un solo catch-up por
        elemento atrasado, como el scheduler viejo.
        """
        self._boot_id = boot_id
        moment = self._now()
        recovered = await self._repo.recover_research_schedules_for_boot(
            boot_id, now=moment
        )
        await self._repo.expire_research_schedules(moment)
        return int(recovered or 0)

    # ------------------------------------------------------------ liquidacion

    def _complete_updates(
        self, claimed: dict[str, Any], result: Any, moment: datetime
    ) -> dict[str, Any]:
        next_run_at = compute_next_run_at(claimed, moment)
        skipped, merged = _overlap_counters(claimed, moment)
        updates: dict[str, Any] = {
            "run_state": RUN_IDLE,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_run_at": moment,
            "last_result": result,
            "last_error": None,
            "execution_key": execution_key_for(claimed),
            "scheduled_for": None,
            "attempts": 0,
            "pending_reconcile": False,
            "next_run_at": next_run_at,
        }
        if skipped:
            updates["skipped_runs"] = int(claimed.get("skipped_runs") or 0) + skipped
        if merged:
            updates["merged_runs"] = int(claimed.get("merged_runs") or 0) + merged
        return updates

    def _retry_updates(
        self, claimed: dict[str, Any], error: str | None, moment: datetime
    ) -> dict[str, Any]:
        """Reintento acotado con backoff. Agotados los intentos, retoma su cadencia."""
        attempts = int(claimed.get("attempts") or 0) + 1
        max_attempts = int(claimed.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        updates: dict[str, Any] = {
            "run_state": RUN_IDLE,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempts": attempts,
            "last_error": error or "unknown",
            "last_run_at": moment,
        }
        if attempts >= max_attempts:
            updates["attempts"] = 0
            updates["scheduled_for"] = None
            updates["pending_reconcile"] = False
            updates["next_run_at"] = compute_next_run_at(claimed, moment)
        else:
            updates["next_run_at"] = moment + timedelta(
                seconds=backoff_seconds(attempts, base=self._backoff_base)
            )
        return updates
