---
paths:
  - "openceo-core/mechanic/**"
---

# Mecánico — reglas por zona

Manejador por defecto de lo que el motor no previó (TASK-2026-00510). Contrato en ARCHITECTURE_REFERENCE §Engine mechanic.

- La regla de atasco es una sola y genérica (`stall.streak_outcome`): nunca un umbral, motivo o rama por tipo de espera. Una espera nueva se cubre sola.
- Esperas humanas y autocurables son listas cortas de categorías (`HUMAN_WAIT_PREFIXES`, `SELF_HEALING_PREFIXES`); ampliarlas exige que la espera sea de verdad de una persona o se cure por su propio evento. `needs_owner` no es humano: el mecánico lo mira antes que el Owner.
- El mecánico no cambia nada: devuelve acciones del vocabulario cerrado (`models.ActionKind`) y el core las aplica validadas. Capacidad nueva = herramienta nueva (pocas, estables y sin pérdida de trabajo), nunca una situación nueva.
- Las acciones git nunca descartan trabajo: unstage solo índice, stash recuperable y rechazado con un ticket en marcha sobre ese checkout, index.lock solo si está viejo.
- Presupuesto siempre activo: un run a la vez, por hora, por día y por ticket; agotado el del ticket, el ticket va al Owner (`owner_action`) sin otro run.
- `situation.py` solo lee y sanea (`reliability.sanitize`); el run va cercado, de solo lectura, con insignia readonly `mechanic`.
- El latido es la única excepción al no-polling: una consulta indexada y acotada, 0 tokens salvo hallazgo. Ningún otro barrido.
- Validación de la zona: `./.venv/bin/python -m pytest tests/test_mechanic_510.py -q`, con fakes; sin modelos vivos.
