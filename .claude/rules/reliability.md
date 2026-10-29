---
paths:
  - "openceo-core/reliability/**"
---

# Reliability hunter base — reglas por zona

Solo para el paquete base del cazador (epic TASK-2026-00365, diseño v2).

- Sin I/O en este paquete, salvo `guard.work_fingerprint` (solo git de lectura: `rev-parse`, `diff`, `ls-files`). Mongo, spool, socket y worker los owns hijos posteriores.
- `guard.evaluate` es puro y fail-open (estado corrupto o excepción → `continue`); solo cuentan observaciones con `executed=True` y el llamador resuelve el modo por productor.
- Orden del pipeline: clasificación y huellas operan sobre el incidente crudo; el saneamiento corre en último lugar antes de persistir (sus frames son strings).
- `schema_version` en todo documento; `fingerprint_version` forma parte de la clave del Problem. Cambiar el algoritmo cierra los anteriores como `superseded`, nunca se reescribe.
- La huella nunca lleva líneas, rutas ni mensaje; el texto libre es evidencia truncada y jamás decide atribución ni severidad.
- `classification.py` es el único dueño de A1–A10 y del mapa `reason_code→atribución` (versionado). Los transitorios se reutilizan de `orchestration/provider_recovery.py`, no se duplican.
- `reliability` es global: ningún `project.yaml` puede fijarlo. La config se valida fail-closed al importar y en cada `validate_project`.
- Invariantes de código que la config no puede cambiar: tickets del cazador con `autorun=false`, gates de admisión e integración, sin texto de negocio de otros proyectos en alcance `engine`, diagnóstico sin herramientas, y el componente `reliability` nunca se autodiagnostica.
