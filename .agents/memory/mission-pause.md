---
type: reference
description: Misión MIS-8C06FB6C reanudada 2026-11-16, en automático
---

# Misión reanudada (2026-11-16, orden del Owner)

- MIS-8C06FB6C (jardal-erp, shell TASK-2026-00493): `status=active` en Mongo desde 2026-11-16T13:21Z. Pausa 2026-11-15 levantada.
- Seguidores con `autorun=true` por API (funnel despierta dispatcher): #836 Proyecto en marcha, #837 Mi trabajo en cola tras #836.
- Auth (2026-11-16): cuenta local `admin` obsoleta renombrada a `admin-local-legacy` (respaldo /tmp/admin-user-backup.json); admin mapeado por ERP con is_admin restaurado (el mapeo lo deja en false en cada login — hallazgo motor).
- Deuda nombrada: reaper de ejecutores al resolver el ticket (DeadlineWatch solo avisa); login ERP choca con cuentas locales del mismo username.