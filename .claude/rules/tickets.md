---
paths:
  - "openceo-core/task_service.py"
  - "openceo-core/qa_review_service.py"
  - "openceo-core/db/mongo.py"
  - "openceo-core/integrations/**"
---

# Tickets — reglas por zona

Solo para cambios en creación, transición o espejo de tickets Jardal ERP.

- `openceo_ref` es la clave de idempotencia (nullable, única); un reintento usa el mismo valor.
- `status_pushed` guarda el vocabulario de OpenCEO, nunca el de Jardal ERP; y `str(TaskStatus.QUEUED)` no es `'queued'`: usar `.value`.
- Nunca un `create_document` genérico hacia Jardal ERP: solo operaciones semánticas cerradas con allowlist de DocTypes.
- Sin flag de encendido/apagado, y es deliberado: apagado se creaban tickets solo en Mongo que el negocio nunca veía; la resiliencia la da la marca `jardal_erp_pending`, no un interruptor.
- La consola de `bench` decodifica stdin como latin-1: usar escapes en scripts ASCII.
