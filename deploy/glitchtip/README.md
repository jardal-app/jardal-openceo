# GlitchTip autoalojado (TASK-2026-00371, ap. 16.1-16.3)

Lente humana auxiliar: ver excepciones con stack trace y release.
Nada del pipeline depende de ella; este hijo puede cancelarse sin
afectar al resto de la epica.

## Despliegue (solo en el host, con el Owner)

1. Copiar `.env.example` a `glitchtip.env` con secretos reales.
2. En el Traefik del host, entrypoint `glitchtip-in` ligado a
   `127.0.0.1` (nunca `0.0.0.0`: Docker salta UFW). Sin ese
   entrypoint no se levanta la ingesta.
3. `docker compose --env-file deploy/glitchtip/glitchtip.env -f deploy/glitchtip/compose.yml up -d`.
   El `--env-file` es obligatorio: el compose interpola
   `GLITCHTIP_DB_PASSWORD` y `GLITCHTIP_INGEST_HOST` desde el entorno.
4. UI solo interna (tunel SSH). UI publica solo con allowlist de
   IP del Owner + TLS + autenticacion.

Retencion: 90 dias (`GLITCHTIP_MAX_EVENT_LIFE_DAYS=90` en el
compose). El backup es opcional: telemetria auxiliar.

## SDK (core y runner, ap. 16.3)

- `openceo-core/reliability/error_tracking.py`: `init_error_tracking`
  con la release de RELEASE. Sin `OPENCEO_GLITCHTIP_DSN` queda
  desactivado y nada cambia.
- Requiere `sentry-sdk` (fijado en `openceo-core/requirements.txt`);
  sin el paquete el tracking queda desactivado sin romper nada.
- Opciones fijas: `send_default_pii=False`,
  `include_local_variables=False`, `max_request_body_size="never"`,
  `before_send` con la redaccion de ap. 15.2, sin escribir en Mongo.
- Integracion asyncio habilitada en el arranque; el core la inicia
  en el lifespan y el runner en `drive()`.
- El DSN vive en el `.env` del servicio, nunca en git ni en logs.
- `capture_exception()` devuelve el id que se guarda en
  `glitchtip_event_id` del incidente.

## Verificacion en el host (aceptacion ap. 17 F0)

1. Forzar una excepcion en el arranque: aparece en GlitchTip con
   la release y sin variables locales.
2. `curl` externo a la ruta de ingesta: falla (no expuesta).
