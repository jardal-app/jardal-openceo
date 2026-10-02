# Operación de Research: social, deep y eventos

La configuración efectiva se edita en `openceo-core/.env`. La política de cada
proyecto solo puede reducir los límites del servidor. Crear un job no crea un
ticket de ERPNext. La activación recurrente se decide al terminar el proyecto;
las pruebas del agente no requieren revisión humana por fase.

## Lectura de X

Instalar la dependencia opcional con el Python del proyecto:

```bash
./.venv/bin/pip install -r openceo-core/research/requirements-social.txt
./.venv/bin/python scripts/research_social_account.py create --owner USER_ID
./.venv/bin/python scripts/research_social_account.py cookies --owner USER_ID --account ACCOUNT_ID
./.venv/bin/python scripts/research_social_account.py grant --owner USER_ID --account ACCOUNT_ID --source 'from:CUENTA'
```

La entrada de cookies es local y oculta. El operador aporta su propia sesión
autorizada y resuelve login/2FA; no se solicitan contraseñas ni se automatizan
challenges. No pegar cookies en chat, `.env`, argumentos o tickets.

`RESEARCH_SOCIAL_SESSION_DIR` debe estar fuera de repo, workspace, evidencias y
vault: directorio 0700, una SQLite 0600 por cuenta. No incluirlo en backups de
evidencias/vault; el servicio no crea copias de sesiones. Expirar o revocar niega
acceso; la eliminación local explícita usa `retire` con los mismos owner/account.

El proyecto debe conceder `allowed_account_refs` y estar habilitado para Research.
La identidad HTTP necesita `run_research`; el grant corresponde a `http:USER_ID`,
proyecto, fuente exacta y operación. `POST /api/research/social/read` obtiene como
máximo cinco items, una página y un request; no expone DM, bookmarks ni el feed del
owner. Los artifacts caducan en siete días o antes si vence el grant. Leerlos
revalida autoridad; quedan `restricted` y `declaration`, sin llamadas al modelo.

La lease compartida en Mongo serializa lectores y setup incluso entre procesos.
Login/challenge detienen la cuenta; rate limit impone cooldown; cancelación mata
el helper y libera la lease. No hay rotación ni espera indefinida.

## Deep

Deep usa fases HTTP controladas por el servicio, sin llamadas auxiliares de CLI.
Requiere `RESEARCH_DEEP_API_KEY`, `RESEARCH_DEEP_MODEL`, precios máximos de entrada y
salida por millón de tokens y `RESEARCH_DEEP_ACCOUNT_CEILING_USD`. La clave es una
configuración explícita; puede referenciar `${DEEPSEEK_API_KEY}` en el `.env`.
Sin ruta privada compatible, solo se admiten fases públicas.

También se exige presupuesto efectivo positivo: mínimo entre
`RESEARCH_PAID_SPEND_USD` y `research.paid_budget_usd` del proyecto. Se reserva antes
del HTTP contra la cuenta compartida y el job; un fallo ambiguo conserva la
reserva. No hay topups ni reintentos del modelo. Las tarifas estiman un máximo;
la factura reconciliada queda `null` sin medición del proveedor.

Hasta dos conjuntos researcher, verifier con contexto nuevo, una corrección y
revisión final. Cada fase consume el mismo límite de pasos/deadline y libera su
slot; quick y deep comparten un máximo de dos llamadas activas. Extractos e ids
deben pertenecer a la evidencia adquirida. Un veredicto del modelo es una opinión
con soporte comprobable; no prueba independencia ni certeza. Contradicciones o
soporte insuficiente producen `partial`.

La muestra de aceptación pública es reproducible sin activar producción:

```bash
./.venv/bin/python scripts/research_deep_probe.py --use-approved-budget --output /tmp/research-pilot-NUEVO
```

Los diez casos usan Mongo y evidencia separados; las reservas del modelo usan
la cuenta real compartida. Revisar todos los claims contra los blobs guardados,
registrar denominadores y separar estimación de reconciliación. Las pruebas con
fixtures comprueban contratos, no calidad real.

## Proveedores opcionales

Brave, Serper y Firecrawl se eligen por los adapters habilitados en la política;
una denegación/fallo no activa otro proveedor. El broker resuelve esa autoridad
desde el job persistido, no desde argumentos del modelo. No se envían consultas
privadas a estos proveedores públicos.

Serper requiere saldo medido en `RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES`.
Firecrawl v2 requiere `RESEARCH_FIRECRAWL_ACCOUNT_CREDITS` y una ventana medida en
`RESEARCH_FIRECRAWL_CREDIT_WINDOW`; su autoridad de créditos nativos es compartida
en Mongo y está separada de los dólares. Cada intento debita antes del I/O y un
fallo conserva incertidumbre. No hay renovación automática de ventanas ni compra.

Para extracción se necesita `research.read_provider: firecrawl` además del adapter.
Se validan la URL inicial y la devuelta; el proveedor no acredita su cadena remota
completa de DNS/redirecciones, así que se conserva un coverage gap y el informe
es `partial`. El lector HTTP sigue siendo el predeterminado.

Probes explícitos de una consulta/lectura, sin modelo ni flags de producción:

```bash
./.venv/bin/python scripts/research_provider_probe.py --provider serper --use-credit
./.venv/bin/python scripts/research_provider_probe.py --provider firecrawl --use-credit
```

## Eventos y exportación

`GET /api/research/events?project_key=...&after=...` requiere `read_research`.
`openceo_research_events` necesita un grant MCP explícito `events` (fuera de los
permisos por defecto). El cursor se limita a proyecto/owner; un id no autoriza.

El outbox atómico guarda payload y cursor juntos; recuperación/replay no ejecutan
el modelo. Las correcciones crean otra versión y conservan historial. Al leer se
revalida retención de los blobs y frescura; la evidencia caducada ya no respalda
claims. `Last-Modified` sirve para HTTP condicional, nunca como fecha de publicación.

`research.export.export_conclusion` es una operación local separada y explícita:
requiere mapping exacto del proyecto, owner y conclusiones públicas revisadas como
durables. El runtime no monta el vault. Seguir `obsidian-vault-links`, enlazar el
hub y comprobar únicamente las notas creadas con `--project` y rutas explícitas.
