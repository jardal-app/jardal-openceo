---
paths:
  - "openceo-core/reliability/**"
---

# Reliability base — reglas por zona

Lo que queda del cazador (épica TASK-2026-00365) tras la poda de TASK-2026-00510: captura de incidentes, supervisor de bucles, plazos, socket del runner, freno de progreso y GlitchTip. El diagnóstico, los tickets y la recuperación los hace ahora el mecánico (`openceo-core/mechanic/`, `.claude/rules/mechanic.md`).

- I/O solo en `capture` (insert Mongo con timeout de 2 s, spool JSONL bajo `.runtime/`, replay), `supervisor` (backoff sleeps), `endpoint` (socket Unix bajo `.runtime/`, spool del runner, ingesta con `late=true`) y `deadlines` (un temporizador asyncio al plazo más próximo más parches fail-open de `attempt_deadline_at`); `guard` sigue puro salvo `work_fingerprint` (solo git de lectura).
- El socket y los spools quedan bajo `.runtime/`, enmascarado en el perfil bwrap de agentes. Los builders sin sandbox comparten usuario y alcanzan el socket: sus eventos son evidencia, nunca autorización.
- `guard.evaluate` es puro y fail-open (estado corrupto o excepción → `continue`); solo cuentan observaciones con `executed=True`. Modo `enforce` en todos los productores: un bucle frenado para como `stalled_no_progress` sin resume y despierta al mecánico.
- DeadlineWatch registra el incidente y despierta al mecánico (`on_missed`); nunca mata procesos.
- Orden del pipeline: clasificación y huellas operan sobre el incidente crudo; el saneamiento corre en último lugar antes de persistir.
- La huella nunca lleva líneas, rutas ni mensaje; el texto libre es evidencia truncada y jamás decide atribución ni severidad. `classification.py` es el único dueño del mapa `reason_code→atribución`; los transitorios se reutilizan de `orchestration/provider_recovery.py`.
- `reliability` es global: ningún `project.yaml` puede fijarlo. La config se valida fail-closed al importar y en cada `validate_project`.
- Integración en bucles: dev A/B/C y genérico evalúan el freno al cierre del intento (`loop_brake`), Tier S en `_park`/`_park_build` (`tier_s_brake`); Tier S nunca importa `loop_brake`. La captura nunca rompe el bucle.
