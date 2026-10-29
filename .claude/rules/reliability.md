---
paths:
  - "openceo-core/reliability/**"
---

# Reliability hunter base — reglas por zona

Solo para el paquete base del cazador (epic TASK-2026-00365, diseño v2).

- I/O solo en `capture` (insert Mongo con timeout de 2 s, spool JSONL bajo `.runtime/`, replay), `supervisor` (backoff sleeps) y `endpoint` (socket Unix bajo `.runtime/`, spool del runner, ingesta con `late=true`); `guard` sigue puro salvo `work_fingerprint` (solo git de lectura: `rev-parse`, `diff`, `ls-files`). El worker lo ownea TASK-2026-00378.
- El socket y los spools quedan bajo `.runtime/`, enmascarado en el perfil bwrap de agentes. Los builders sin sandbox (`musej --disable-sandbox`) comparten usuario y alcanzan el socket: sus eventos son evidencia, nunca autorización.
- `guard.evaluate` es puro y fail-open (estado corrupto o excepción → `continue`); solo cuentan observaciones con `executed=True` y el llamador resuelve el modo por productor.
- Orden del pipeline: clasificación y huellas operan sobre el incidente crudo; el saneamiento corre en último lugar antes de persistir (sus frames son strings).
- `schema_version` en todo documento; `fingerprint_version` forma parte de la clave del Problem. Cambiar el algoritmo cierra los anteriores como `superseded`, nunca se reescribe.
- La huella nunca lleva líneas, rutas ni mensaje; el texto libre es evidencia truncada y jamás decide atribución ni severidad.
- `classification.py` es el único dueño de A1–A10 y del mapa `reason_code→atribución` (versionado). Los transitorios se reutilizan de `orchestration/provider_recovery.py`, no se duplican.
- `reliability` es global: ningún `project.yaml` puede fijarlo. La config se valida fail-closed al importar y en cada `validate_project`.
- Invariantes de código que la config no puede cambiar: tickets del cazador con `autorun=false`, gates de admisión e integración, sin texto de negocio de otros proyectos en alcance `engine`, diagnóstico sin herramientas, y el componente `reliability` nunca se autodiagnostica.
- Integración en bucles (`reliability/loop_brake.py`, TASK-2026-00375): dev A/B/C y genérico evalúan el freno al cierre del intento y guardan `progress_guard` en los mismos `params` que el checkpoint/veredicto (una escritura atómica del funnel). La captura nunca rompe el bucle; los stores mock/sintéticos se saltan sin spool. Solo el orquestador emite hitos (§4.3).
- Tier S (`reliability/tier_s_brake.py`, TASK-2026-00376): mismo contrato con productor `tier_s` e hitos propios (plan-ready, build-done, gate-fixed, review-report, finding-resolved); la huella sale de `guard.work_fingerprint` (working tree incluido, `.openceo/` excluido). El ciclo evalúa en `_park`/`_park_build`, persiste `params` en cada park y para en enforce como `stalled_no_progress` sin resume. Tier S nunca importa `loop_brake`.
