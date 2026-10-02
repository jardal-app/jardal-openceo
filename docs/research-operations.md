# Operación de Research: social, deep y eventos

La configuración efectiva se edita en `openceo-core/.env`. La política de cada
proyecto solo puede reducir los límites del servidor. Crear un job no crea un
ticket de ERPNext. La activación recurrente se decide al terminar el proyecto;
las pruebas del agente no requieren revisión humana por fase.

Cuando el host está administrado por `openceo-core-host.service`, reiniciar la
unidad de systemd de usuario. El launcher manual y el runner de esa unidad
interpretan `.env` con python-dotenv: comillas, referencias `${KEY}` y saltos
de línea se resuelven como en Settings, sin ejecutar texto del archivo.

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus systemctl --user restart openceo-core-host.service
```

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
máximo cinco items y **una petición GraphQL**; no expone DM, bookmarks ni el feed del
owner. Antes de esa petición la librería genera su identificador de transacción:
baja una página pública de x.com con las cookies y, con sesión válida, los scripts de
la web de X. Cada lectura es un proceso nuevo, sin caché entre lecturas, así que la
red tocada es mayor que una petición. Los artifacts caducan en siete días o antes si
vence el grant. Leerlos revalida autoridad; quedan `restricted` y `declaration`, sin
llamadas al modelo.

La lease compartida en Mongo serializa lectores y setup incluso entre procesos.
Cancelación mata el helper y libera la lease; el helper tiene además un deadline
propio de 20 s para terminar por sí mismo y limpia al empezar los bloqueos de 15
minutos que la librería deja si lo matan. No hay rotación ni espera indefinida.

Causas que devuelve la lectura (HTTP 409 salvo `access_denied`, que es 403):

| Causa | Qué significa y qué hace el servicio |
| --- | --- |
| `login_required` | X ya no acepta la sesión (caducó, cookies mal copiadas o cuenta cerrada). Pausa la cuenta; quien tiene grant ve esta causa y no hay más intentos hasta repetir `cookies`. |
| `challenge_required` | X pide desbloquear la cuenta en un navegador. Misma pausa; lo resuelve la persona. |
| `rate_limited` | X limitó la cuenta. Cooldown de 15 minutos sin contactar a X. |
| `access_restricted` | 403 sin código de error; no se interpreta como sesión caducada. |
| `source_unavailable` / `source_timeout` | X cambió su web, falló la red o se agotó el deadline. No pausa la cuenta. |
| `account_busy` | Otra lectura o setup tiene la lease. Reintentar después. |

Sin grant válido todas las causas se ven como `access_denied`. Si
`RESEARCH_SOCIAL_SESSION_DIR` no es válido (permisos, symlink, dentro de repo,
workspace, evidencias o vault) el servicio arranca igual y solo la ruta social
responde 503; el log indica `unsafe_session_directory`, `session_dir_not_isolated` o
`social_init_failed`.

## Fuentes recurrentes (RSS/Atom)

Antes de suscribirse a una fuente, comprobarla con la sonda de solo lectura. Usa el
lector con guard de egress y el adaptador de vigilancia, lee dos veces para ver si la
fuente responde a la petición condicional y no necesita Mongo, modelo ni flags:

```bash
./.venv/bin/python scripts/research_watch_probe.py --url https://example.org/feed.xml
./.venv/bin/python scripts/research_watch_probe.py --type greenhouse --board TOKEN
./.venv/bin/python scripts/research_watch_probe.py --type lever --board TOKEN
```

Medido el 2026-10-02 contra fuentes públicas reales: el Atom de releases de GitHub, el
RSS 2.0 de `peps.python.org` y el tablero Greenhouse `gitlab` devolvieron items con
título, URL y fecha, y la segunda lectura fue `not_modified` (ETag, y Last-Modified en
el RSS); el tablero Lever `leverdemo` devolvió sus 11 postings y se relee entero, porque
Lever no usa validadores por diseño. Una fuente que pasa aquí la puede leer una
suscripción; encender `RESEARCH_ENABLED` y el scheduler sigue siendo decisión del dueño
y se hace al final del proyecto. El guard de egress bloquea localhost y redes privadas:
la fuente de la prueba recurrente debe ser una URL pública.

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

Un job que no puede publicar su evento (scope o esquema inválidos) queda anotado con
`event_delivery_failed` y no se reintenta; un fallo transitorio conserva
`event_delivery_pending` y se reintenta en el siguiente arranque. La publicación nunca
impide devolver el job terminado ni arrancar el servicio.

## Cierre humano único

Se decide al terminar todo el proyecto y nada de esto se activa antes. Hoy
`projects/jardal-openceo/project.yaml` no tiene bloque `research` y
`RESEARCH_ENABLED=false`; el scheduler no se activa salvo
`RESEARCH_SCHEDULER_ENABLED=true`. Con `RESEARCH_PAID_SPEND_USD=0.00` y sin
`paid_budget_usd` en el proyecto no hay gasto de pago. Encender `RESEARCH_ENABLED` abre
`/api/research/*` a quien tenga `run_research`; no inicia nada recurrente por sí solo.

**Demostración B (recurrencia desplegada).** La persona elige una URL pública de feed
que controle (comprobable con la sonda de arriba) y autoriza poner
`RESEARCH_ENABLED=true` y `RESEARCH_SCHEDULER_ENABLED=true` en `openceo-core/.env` y este
bloque en el proyecto:

```yaml
research:
  enabled: true
  allowed_modes: [quick]
  classification_ceiling: public
```

El agente reinicia la unidad, crea una suscripción acotada, la reinicia de nuevo para
comprobar que identidad, vencimiento y cursor sobreviven, observa un tick, la cancela y
comprueba que deja de adquirir. La persona decide si las flags quedan encendidas.

**Piloto X (hasta cinco posts).** La persona ejecuta `create` y `cookies` con su propia
sesión (login/2FA incluido), acuerda una cuenta o consulta pública y añade
`allowed_account_refs: [ACCOUNT_ID]` al mismo bloque. El agente hace `grant`, una lectura
real, comprueba causas, aislamiento y procedencia, y termina con `retire` y limpieza.
