# Tareas pendientes

**El backlog vive en ERPNext: <https://erp.jardal.app/app/task>**

Este archivo ya no es el backlog. Los tickets se crean y se consultan ahí, no aquí.

---

## Por qué cambió

Hasta el 2026-09-24 este archivo era una lista markdown de 27 items escrita a mano. Tenía dos
problemas: **se desactualizaba en silencio** (al migrar se encontró que 7 items figuraban como
pendientes pero ya estaban implementados, y 2 estaban absorbidos por otro ticket) y **no lo veía
nadie más que quien lo editaba**.

Desde esa fecha el trabajo empresarial pertenece a ERPNext — proyectos, tickets, prioridades,
aprobaciones — y OpenCEO conserva la ejecución. La dirección está en
[`docs/OPENCEO_DIRECTION.md`](docs/OPENCEO_DIRECTION.md) v1.1 y las fronteras en
[`docs/ARCHITECTURE_REFERENCE_2026.md`](docs/ARCHITECTURE_REFERENCE_2026.md) §System Boundaries.

## Migración

Los 27 items se migraron el 2026-09-24 con `scripts/migrate_pendings_to_erpnext.py`, que es
idempotente (`openceo_ref = "pendings:TICKET-0NN"`). **El índice `TICKET-NNN → TASK-2026-NNNNN`
está en [`STATUS.md`](STATUS.md)**, junto con el estado que se auditó para cada uno.

El estado de cada ticket migrado salió de **verificar el código**, no de copiar el markdown. Por
eso hay tickets `Completed` y `Cancelled`: son la constancia de que el backlog viejo mentía.

## Qué NO cambió

- Las decisiones de arquitectura y su razonamiento siguen en `docs/` y en el vault.
- El avance operativo y los traspasos de sesión siguen en [`STATUS.md`](STATUS.md).
- Una receta determinista aprobada sigue viviendo en `workflows/`, versionada en git.
