# ERPNext — qué instalar (módulos, apps y topología)

**Fecha:** 2026-09-20 · **Estado:** propuesta de instalación — **no desplegado**
**Alcance:** elección de módulos/apps y topología mínima del sistema financiero de Horizon
**Se relaciona con:** [`docs/decision-2026-09-19-sistema-financiero-erpnext.md`](file:///home/develop/development/Jardal/jardal-openceo/docs/decision-2026-09-19-sistema-financiero-erpnext.md) · ticket TASK-2026-00025 en ERPNext · `jardal-horizon/README.md` §4.7 y §9.9

> **Este documento decide qué instalar. No decide contabilidad.** El contenido del plan de cuentas boliviano y las alícuotas tributarias se definen con criterio contable local, fuera de OpenCEO y fuera de este archivo.

---

## 1. La pregunta y la respuesta corta

**Pregunta del dueño:** ERPNext trae muchos módulos y apps internas; ¿vale la pena levantar el stack completo en Docker o no hace falta?

**Respuesta, en dos partes:**

1. **Apps: no.** Se instala **solo `erpnext`** (el núcleo). Nada de `crm`, `helpdesk`, `lms`, `drive`, `wiki`, `gameplan`, `raven` — duplican OpenCEO y violan la regla *"adoptar el complemento, nunca el duplicado"*. Consecuencia práctica: **no hace falta construir una imagen propia**; sirve la imagen prebuilt oficial, que ya contiene exactamente frappe + erpnext.
2. **Contenedores: sí, el stack estándar.** El "stack completo" oficial no es un exceso de opciones: son ~9 contenedores que **son** la topología mínima soportada (web, backend, db, 2 redis, workers, scheduler, websocket). **No existe un perfil "slim" oficial** y el único recorte documentado (consolidar colas de workers) ahorra poco y obliga a editar el compose sin respaldo upstream. Recomendación: **levantarlo tal como viene**, fijando versión.

El "no instalar todo" se decide en las **apps**, no en los contenedores.

---

## 2. Vocabulario que evita el error de fondo: app ≠ módulo

| Término | Qué es | Cómo se cambia |
|---|---|---|
| **App** | Paquete Python instalable en el bench (`apps.txt`). Ej: `erpnext`, `hrms`, `payments` | `bench get-app` + `bench --site <sitio> install-app <app>` |
| **Módulo** | Espacio de nombres **dentro** de una app, materializado como registro `Module Def`. Ej: `Accounts`, `Stock` | **No se instala ni se desinstala por separado**: llega con la app. `modules.txt` es una lista fija |

Traducción práctica: **los módulos no se eligen**. ERPNext trae 21 módulos siempre, y lo único que el dueño decide es **qué apps añadir** encima. El asistente de configuración inicial, además, no instala módulos.

---

## 3. Los 21 módulos que ERPNext trae de fábrica

Lista idéntica en v15 y v16 (`erpnext/modules.txt`): `Accounts`, `Buying`, `Selling`, `Stock`, `Manufacturing`, `Assets`, `Projects`, `CRM`, `Support`, `Quality Management`, `Maintenance`, `Subcontracting`, `Regional`, `Setup`, `Utilities`, `Portal`, `ERPNext Integrations`, `Communication`, `Telephony`, `Bulk Transaction`, `EDI`.

En v16, la app declara `Telephony`, `Communication`, `EDI` y `Bulk Transaction` como **sin navegación propia** (`code_only_modules`).

### Qué usa Horizon y qué no

| Módulo | ¿Sirve al registro financiero? | Veredicto |
|---|---|---|
| **Accounts** | Sí — partida doble, asientos, saldos, centros de costo | **Núcleo** |
| **Buying** | Sí — gastos y compras (proveedor, factura de compra, pago) | **Núcleo** |
| **Selling** | Sí — ingresos (cliente, factura de venta, cobro) | **Núcleo** |
| **Assets** | Sí — registro de bienes (servidores, equipo) y depreciación | **Núcleo opcional** |
| **Regional** | Localizaciones y plantillas de plan de cuentas | **Relevante, pero Bolivia no existe (§7)** |
| **Setup / Utilities / Portal / ERPNext Integrations / Communication / Telephony / Bulk Transaction / EDI** | Andamiaje interno o sin navegación propia | Ignorar |
| **Stock · Manufacturing · Subcontracting · Quality Management · Maintenance** | Inventario y producción — no existen en este alcance | **No usar** |
| **Projects** | **Desde el 2026-09-24 sí se usa**: ERPNext pasó a ser dueño del trabajo empresarial y los tickets viven en `Task` (con `Project` como identidad de proyecto). Ya no duplica a OpenCEO, que conserva solo la **ejecución**. Véase `docs/OPENCEO_DIRECTION.md` v1.1 y `docs/ARCHITECTURE_REFERENCE_2026.md` §System Boundaries | **Usar `Task` y `Project`** |
| **CRM** | Duplica tickets/CRM de OpenCEO | **No usar** |
| **Support** | **Reservado, no descartado**: `Issue` es para trabajo **entrante** con reloj de SLA (un cliente o ayudante reporta algo). No se usa todavía; se activa cuando exista ese canal | **Reservar** |

**No hay interruptor por módulo.** `Show/Hide Modules` ya no se soporta desde v15. Estos módulos llegan con la app y simplemente **no se usan**; la disciplina real está en **no adoptar las apps que los amplían** (§5). Si en el sitio aparecen ocupando navegación, se resuelve con roles/workspaces, no con "desinstalación".

---

## 4. Dominios del asistente inicial: qué son y qué **no** son

`Domain Settings` permite mantener activos solo los dominios de negocio relevantes. Documentado explícitamente:

- **No es una frontera de seguridad.**
- **No borra nada**: *"Do not treat domain selection as deletion"*; los registros existentes permanecen. Reversible: se vuelve a activar el dominio y se recarga el Desk.
- **No activa código que no está instalado** (`Domain Settings cannot activate code that is not installed`).
- Un **dominio** es contexto de negocio; un **módulo** agrupa funcionalidad: *"they overlap, but they are not interchangeable"*.

**Hallazgo a tener presente (inferencia, no doc):** el propio app `erpnext` en v15/develop **no declara el hook `domains`** — el hook vive en apps de dominio (`frappe/hospitality`, `frappe/agriculture`, ambas archivadas). Conclusión operativa: **no confiar en Domain Settings para ocultar módulos**; con la instalación propuesta (solo `erpnext`) es probable que la lista de dominios activos esté vacía, y eso es correcto.

Los checkboxes de "módulos que planeas implementar" del asistente v16 alimentan **solo telemetría** (`capture_user_persona`); no cambian módulos en el servidor. Y la selección posterior es modificable en Domain Settings.

---

## 5. Apps del ecosistema Frappe: qué instalar y qué no

Ninguna de estas apps es un "módulo interno": son paquetes separados que se añaden al bench y se instalan **en el sitio**.

| App | Licencia | Compatibilidad | Qué añade | Veredicto |
|---|---|---|---|---|
| **`erpnext`** | GPL-3.0 | v15 / v16 | El núcleo completo | **Instalar solo esto** |
| `payments` | MIT | version-15 / version-16 | Enlaces a pasarelas (Stripe, PayPal, Razorpay…) | Después, **solo si** se cobran pasarelas online |
| `hrms` (Frappe HR) | GPL-3.0 | version-15 / version-16 | RR.HH., nómina, rendición de gastos | Después, **en la etapa de nómina** |
| `insights` | AGPL-3.0 | version-3 → frappe v15/v16 | BI sobre el libro contable | Después, **solo si** hace falta analítica; trae dependencias pesadas (`duckdb`, `pandas`, `lxml`) y pide `server_script_enabled` |
| `print_designer` | AGPL-3.0 | sin `frappe-dependencies` declaradas | Diseño de formatos de impresión | Opcional (mejora facturas; los formatos existen sin él) |
| `lending` | GPL-3.0 | develop apunta a frappe/erpnext ≥17 — **v15/v16 sin verificar** | Préstamos | No ahora |
| `crm` | AGPL-3.0 | frappe ≥16 ≤17 (**sin v15**) | CRM | **No** — duplica OpenCEO |
| `helpdesk` | AGPL-3.0 | frappe ≥16 ≤18 y **requiere `telephony`** | Mesas de ayuda | **No** — duplica OpenCEO |
| `lms` | AGPL-3.0 | version-15/16 y **requiere `payments`** | Cursos | **No** — duplica OpenCEO |
| `drive` | AGPL-3.0 (beta) | frappe ≥15 ≤17 | Gestión documental | **No** — el propio README avisa que no sea el único almacenamiento |
| `wiki` | MIT | main (ramas hasta version-14) | Wiki | **No** |
| `gameplan`, `raven` | AGPL-3.0 | frappe ≥16 / ≥15 ≤17 | Alternativas a Discuss | **No** |
| `webshop` | GPL-3.0 | apunta a frappe/erpnext ≥17 — **sin verificar** | E-commerce | **No** — duplicado |
| `education`, `non_profit` | — | frappe ≥17 / archivada | Colegio / ONG | **No** |
| `books` | AGPL-3.0 | — | **App de escritorio (Electron + SQLite)**, no de servidor | No aplica |
| `frappe/health` | GPL-3.0 | — | Redirige a `earthians/marley`; ya no es de la organización Frappe | No aplica |

### Reglas de decisión

1. **Instalar una app es parte de la imagen, no del runtime.** *"Installing apps into a running container is not supported… Runtime changes are lost on container recreation"*. Añadir `hrms` o `payments` después exige **reconstruir la imagen** (o usar la capa `layered` con `apps.json` como secreto de BuildKit) y redesplegar. Es barato y controlado, pero **no es un clic**: por eso conviene instalarlas cuando exista la necesidad, no "por si acaso".
2. **La imagen prebuilt instala solo frappe + erpnext** y no acepta `apps.json`. Como la decisión es no añadir apps, **la imagen prebuilt es suficiente**: cero builds propios.
3. **Desinstalar es posible** (`bench --site <s> uninstall-app <app>`, con `--dry-run`/`--yes`/`--force`) y **destruye los datos de esa app** (hace backup antes). Además `bench remove-app` la saca del bench, pero **el código sigue en la imagen hasta reconstruir**. No es una decisión barata de revertir: otra razón para instalar de a una, con necesidad demostrada.

---

## 6. Topología Docker: por qué "completo" es aquí lo correcto

### Lo que realmente contiene el stack oficial

Base (`compose.yaml`) = **7 servicios, todos de la misma imagen**:

| Servicio | Rol | ¿Obligatorio? |
|---|---|---|
| `configurator` | One-shot: `bench set-config` (db, redis, socketio) y escribe `apps.txt`; el resto espera a que termine | Sí (y termina) |
| `backend` | gunicorn (`GUNICORN_WORKERS=2`, threads 4, timeout 120) | Sí |
| `frontend` | nginx; es el único que debe entrar a Traefik | Sí |
| `websocket` | `socketio.js` — transporte en tiempo real | Solo por tiempo real |
| `queue-short` | `bench worker --queue short,default` | Consolidable |
| `queue-long` | `bench worker --queue long,default,short` | Sí |
| `scheduler` | `bench schedule` — encola los trabajos periódicos | Sí |

**MariaDB y Redis no están en el archivo base**, son overrides: `compose.mariadb.yaml` añade `db` (`mariadb:11.8`) y `compose.redis.yaml` añade `redis-cache` + `redis-queue` (`redis:8.6-alpine`). Extras opcionales: `compose.migrator.yaml` (`bench --site all migrate` al arrancar) y `compose.backup-cron.yaml` (Ofelia, `bench --site all backup` cada `${BACKUP_CRONSTRING:-@every 6h}`).

### El único recorte documentado, y por qué no vale la pena

- **Workers:** la documentación sí bendice combinar colas (`bench worker --queue short,default`). El compose ya viene redundante: `queue-long` consume `long,default,short`, que es **la unión de las tres colas**, así que quitar `queue-short` mantiene cobertura completa. Pero: el ahorro de RAM es marginal en un host con 20 GB disponibles, no existe override oficial "slim" (hay que editar el compose a mano) y **un solo worker hace que un trabajo largo demore los cortos** — precisamente lo que la separación de colas evita. **Alternativa soportada:** `bench worker-pool --num-workers` (2 por defecto).
- **Realtime (`websocket`):** no se puede quitar sin costo. Es el único transporte de `publish_realtime`; el apagador soportado es `disable_async` en `site_config.json`, y con eso se pierden notificaciones, chat, actualización en vivo y barras de progreso. Además `frontend` declara `depends_on: websocket` y una variable `SOCKETIO` dura: quitarlo es cirugía no soportada.
- **Redis:** **deben quedar dos.** El `configurator` apunta `redis_socketio` a `REDIS_QUEUE`; no hay override de un solo Redis.
- **Scheduler:** **obligatorio** si se quieren trabajos periódicos (depreciación, facturas recurrentes, recordatorios). Quitarlo los desactiva en silencio.

**Veredicto:** levantar el stack estándar **sin cambios** (7 base + db + 2 redis). La razón no es "por si acaso", es que el recorte no está soportado y no compra nada medible.

### Ajustes obligatorios por el host real

| Punto | Detalle |
|---|---|
| **No usar `compose.proxy.yaml` / `compose.https.yaml`** | Levantan su **propio** Traefik en 80/443 y chocan con el nuestro (`traefik:v3.0`). |
| **`compose.multi-bench.yaml` asume otra red** | Espera una red **externa llamada literalmente `traefik-public`** (y `mariadb-network`). Nuestras redes son `traefik-net` y `jardal-internal-net`. Opciones: crear/aliasar la red, o escribir nuestro compose con etiquetas propias. |
| **`SITES` está deprecado → `SITES_RULE`** | Es una **expresión de regla de Traefik v3**; las listas separadas por coma de v2 ya no funcionan. Nuestro Traefik es v3, así que esto aplica directo. |
| **Puerto del `frontend`** | Es el servicio que expone Traefik. Confirmar su puerto interno en el compose real antes de fijar `loadbalancer.server.port`. |
| **DNS** | Vía el MCP `manage_dns_check_or_create` (ya previsto en TICKET-025 fase 1). |
| **Una sola imagen para todos los roles** | El split `erpnext-worker` / `erpnext-nginx` / `frappe-socketio` está retirado: *"All the containers now use same image"*. |

**Actualizar Traefik no hace falta para esto.** Se auditó el Traefik real (2026-09-20): corre `v3.0` (3.0.4, jul-2024) desde `/home/develop/deployments/jardal-traefik/` en `network_mode: host`, y **ERPNext se integra con las 3 labels que ya usa toda la producción** — sin cambios en el proxy. La actualización de versión (línea 3.0 sin soporte desde jul-2024) es una **mejora de seguridad independiente**, con procedimiento validado y rollback, en [`docs/traefik-upgrade-runbook.md`](file:///home/develop/development/Jardal/jardal-openceo/docs/traefik-upgrade-runbook.md). Ahí se documenta además un hallazgo que **sí** conviene resolver antes de pedir el certificado de ERPNext: un bucle de errores ACME que consume la cuota *de cuenta* de Let's Encrypt.

### Versión

Etiquetas empujadas el 2026-09-17: `v16`/`version-16` → **v16.35.0**; `v15`/`version-15` → **v15.121.3**.

**Recomendación: `v16`, fijando el tag de parche exacto (`frappe/erpnext:v16.35.0`)**, para que un `pull` no cambie versión en silencio. Razones: es la línea vigente, y las apps del ecosistema que podrían hacer falta después (`payments`, `hrms`) tienen rama `version-16`; `crm`/`helpdesk` directamente exigen frappe ≥16. El tamaño de imagen reportado por el registro para ese tag es ~860 MiB (el footprint extraído es mayor).

---

## 7. Qué crear en el sitio después de instalar (no es elegir módulos)

El despliegue decide la topología; el **sitio** es donde vive el modelo de datos. Mínimo para Horizon:

1. **Company `Personal`** con moneda base **BOB**, y **USD** como segunda moneda (multimoneda habilitada).
2. **Cost Center tree por proyecto** — hoy: `personal`, `jardal-openceo`, `jardal-trading`, `jardal-vpn`, `melanyryr`, `cebitec`, `docencia/remoto`.
3. **Plan de cuentas boliviano — hay que construirlo, no existe plantilla.** Verificado en el propio repo de ERPNext: en `chart_of_accounts/verified` y `chart_of_accounts/unverified` **no hay ninguna entrada `bo_`**. Existen `ar`, `br`, `co`, `gt`, `mx`, `ni`, `el` (verificadas) y `cl`, `cr`, `ec`, `hn`, `pa`, `pe`, `uy`, `ve` (no verificadas). **No hay una app de localización boliviana** en la organización Frappe. El contenido tributario (IVA, IT, RC-IVA, retenciones) **no se fija en este documento**: requiere criterio contable local.
4. **Decidir al inicio la dimensión para proyectos que crucen empresas.** `Cost Center` y `Project` están asociados a una company ([frappe/erpnext#45625](https://github.com/frappe/erpnext/issues/45625)); un proyecto que cruce dos empresas necesita una **dimensión custom sin campo company**. Es más barato decidirlo antes del primer asiento que migrar después.

### Nota abierta — el nivel de registro que se busca (sin analizar aquí)

Dejada por el dueño el **2026-09-20** para una **sesión futura específica**. **No se analizó a propósito** y este documento no la resuelve.

- **No se busca contabilidad formal con impuestos** — para eso está la contadora.
- Lo que se quiere registrar: **empresas**, **proyectos personales** y **proyectos de la empresa**; **cuánto se gastó en cada uno**; **objetivos de compra y cuánto dinero falta**; **cómo distribuir el dinero** dentro del sistema financiero; **gastos de hogar, personales y de servicios**.
- **Pregunta abierta:** ¿ERPNext soporta ese nivel de registro? Otra IA dijo que sí y el dueño sospecha que puede ser alucinación.

**Estado: sin verificar.** El criterio para la sesión futura es contrastarlo contra el modelo real de ERPNext (company / centro de costo / proyecto / dimensiones / presupuesto) **antes** de aceptar o descartar cualquier respuesta de otra IA. No se da por válido.

---

## 8. Backups, disco y la obligación de medir

- Backups: `bench --site all backup` (o `--with-files`), **dentro del volumen `sites`**; el cron oficial (Ofelia) corre cada 6 h. Los backups **se acumulan** en ese volumen: hay que podarlos, no solo generarlos.
- **Los assets no viven en el volumen** — están en la capa de la imagen (symlink). El volumen `sites` contiene configs, sitio, archivos y backups.
- **Montar `sites` y `db-data` como bind-mount** (`compose.bind-mount-sites.yaml`) para poder **ver y acotar** el crecimiento.
- **Medición obligatoria (TICKET-025 fase 1):** *la estimación no sustituye la medición*. No existen cifras oficiales de RAM por servicio ni de disco por sitio; lo que hay:
  - `frappe_docker` dice "al menos 4 GB" — pero eso es **guía de desarrollo**, no de producción.
  - Frappe Cloud estima **~400 MB por bench** (2 workers gunicorn + 1 set de workers) y advierte ojo con servidores de 4 GB totales.
  - Doc de framework: "al menos 2 vCPU" y "al menos 8 GB de RAM", con bufferpool ≈60% de la RAM (**asume servidor dedicado de base de datos**, que no es este caso).
  - El "2 GB mínimo / 4 GB recomendado" que circula es **folclore de foro, no oficial**.

**Medición del host (2026-09-20, propia):** 20 GB de RAM disponibles (10 usados, 14 en caché), swap 3.4 GB en uso, **130 GB libres en `/` (55% usado)**, 57 contenedores corriendo. El disco ya **no** es la restricción que se registró el 2026-09-19 (entonces: 55 GB libres, 82% usado). Tras desplegar, se reporta el consumo real en reposo y bajo carga.

---

## 9. Frontera con OpenCEO: nada de esto se mezcla con OpenCEO

**Regla:** el sistema financiero se construye y se opera **fuera de OpenCEO** — su despliegue, su plan de cuentas, sus workflows, sus apps y sus automatizaciones viven en el ciclo de vida del producto ERPNext. OpenCEO **no** construye contabilidad, **no** replica asientos y **no** almacena dinero en floats: guarda **referencias y resúmenes con fecha de corte** (Horizon §4.7).

Lo único que cruza la frontera es **el mapa de funciones**: la superficie de herramientas que el agente Finance podrá invocar. Ese mapa es un entregable **futuro** del dueño (registrado como TASK-2026-00027 en ERPNext); los nombres que hoy figuran en TICKET-025 fase 3 son **candidatos provisionales, no un contrato**:

`finance.register_movement` · `finance.get_balance` · `finance.get_project_spend` · `finance.get_goal_progress` · `finance.list_pending` · `finance.register_tithe`

Con dos condiciones ya decididas que el mapa debe respetar:

1. **Idempotencia obligatoria:** campo custom `openceo_ref` con **índice único**, para que un reintento no duplique el asiento. Cada escritura tiene identidad.
2. **`execute_money_movements` permanece en `false`** (Horizon §9.9) hasta que la conciliación esté probada. Preparar una operación, contabilizarla y confirmar que se ejecutó son estados distintos.

| Necesidad de Horizon | Dueño | Qué consume OpenCEO |
|---|---|---|
| Saldos y asientos por proyecto | ERPNext | Cifra con fecha de corte y referencia |
| "Cuánto falta" de un objetivo | OpenCEO `horizon_objectives`; la cifra es de ERPNext | Solo el número |
| Objetivos, tareas, plan del día | OpenCEO | Nada de ERPNext |
| Crypto (BTC / Lightning) | BTCPay Server | Sincroniza **hacia** ERPNext; no se duplica |
| Tickets | MongoDB → espejo Notion | **Notion nunca toca dinero** |

---

## 10. Pendiente al cierre de este documento

1. **Desplegar** con la topología de §6 y **medir** RAM/disco reales (§8).
2. **Construir el plan de cuentas boliviano** (§7.3) con criterio contable local.
3. **Decidir la dimensión** para proyectos multi-empresa antes del primer asiento (§7.4).
4. **Entregar el mapa de funciones** (§9) cuando el sistema esté definido; recién entonces se implementa el adapter del agente Finance (TASK-2026-00027 en ERPNext).
5. **Cerrar el espejo a Notion** antes de importar datos financieros (`NOTION_MIRROR_ENABLED`, fase 0 de TICKET-025).

---

## 11. Fuentes

**Verificado en código fuente de ERPNext/Frappe:**
- [erpnext/modules.txt (develop)](https://raw.githubusercontent.com/frappe/erpnext/develop/erpnext/modules.txt) · [version-15](https://raw.githubusercontent.com/frappe/erpnext/version-15/erpnext/modules.txt) — lista de módulos, idéntica en ambas ramas
- [erpnext/hooks.py (develop)](https://raw.githubusercontent.com/frappe/erpnext/develop/erpnext/hooks.py) — `code_only_modules`; **sin hook `domains`**
- [frappe/domain_settings.py](https://raw.githubusercontent.com/frappe/frappe/develop/frappe/core/doctype/domain_settings/domain_settings.py) · [frappe/boot.py](https://raw.githubusercontent.com/frappe/frappe/develop/frappe/boot.py) — de dónde salen los dominios
- [patch delete_ecommerce_doctypes.py](https://raw.githubusercontent.com/frappe/erpnext/develop/erpnext/patches/v15_0/delete_ecommerce_doctypes.py) — E-commerce salió del núcleo hacia `webshop`
- [chart_of_accounts/verified](https://github.com/frappe/erpnext/tree/develop/erpnext/accounts/doctype/account/chart_of_accounts/verified) · [unverified](https://github.com/frappe/erpnext/tree/develop/erpnext/accounts/doctype/account/chart_of_accounts/unverified) — **no existe plantilla `bo_`** (verificado directamente)
- READMEs de apps: [payments](https://raw.githubusercontent.com/frappe/payments/develop/README.md) · [insights](https://raw.githubusercontent.com/frappe/insights/develop/README.md) · [drive](https://raw.githubusercontent.com/frappe/drive/develop/README.md) · [books](https://raw.githubusercontent.com/frappe/books/master/README.md) · [frappe/health → earthians/marley](https://github.com/frappe/health)
- [frappe/commands/site.py](https://raw.githubusercontent.com/frappe/frappe/develop/frappe/commands/site.py) · [scheduler.py](https://raw.githubusercontent.com/frappe/frappe/develop/frappe/commands/scheduler.py) — instalación/desinstalación y workers

**Docker / despliegue (frappe_docker, rama `main`):**
- [compose.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/compose.yaml) · [compose.mariadb.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.mariadb.yaml) · [compose.redis.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.redis.yaml) · [compose.backup-cron.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.backup-cron.yaml) · [compose.migrator.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.migrator.yaml) · [compose.multi-bench.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.multi-bench.yaml) · [compose.proxy.yaml](https://raw.githubusercontent.com/frappe/frappe_docker/main/overrides/compose.proxy.yaml)
- [Docker immutability](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/01-getting-started/02-docker-immutability.md) — apps en la imagen, no en runtime
- [Build setup](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/02-setup/02-build-setup.md) · [Overrides](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/02-setup/05-overrides.md) · [Single server example](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/02-setup/07-single-server-example.md)
- [Traefik v3 migration](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/06-migration/02-traefik-v3-migration.md) — `SITES` → `SITES_RULE`
- [Migrate from multi-image](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/06-migration/01-migrate-from-multi-image-setup.md) — una sola imagen
- [Development docs](https://raw.githubusercontent.com/frappe/frappe_docker/main/docs/05-development/01-development.md) — el "4 GB" es guía de desarrollo
- [production Containerfile](https://raw.githubusercontent.com/frappe/frappe_docker/main/images/production/Containerfile) · [layered Containerfile](https://raw.githubusercontent.com/frappe/frappe_docker/main/images/layered/Containerfile)

**Documentación:**
- [Domain Settings](https://docs.frappe.io/erpnext/domain-settings) · [Show/Hide modules](https://docs.frappe.io/erpnext/show-hide-modules) · [Apps (framework)](https://docs.frappe.io/framework/user/en/basics/apps) · [Bench commands](https://docs.frappe.io/framework/user/en/bench/frappe-commands)
- [Background jobs](https://docs.frappe.io/framework/user/en/api/background_jobs) · [site_config](https://docs.frappe.io/framework/user/en/basics/site_config) · [DB/hardware](https://docs.frappe.io/framework/user/en/database-optimization-hardware-and-configuration) · [Frappe Cloud — guidelines de servidor](https://docs.frappe.io/cloud/servers/guidelines-for-choosing-a-server-plan)
- [Docker Hub — tags de frappe/erpnext](https://hub.docker.com/v2/repositories/frappe/erpnext/tags?page_size=100&ordering=last_updated) · [tag v16.35.0](https://hub.docker.com/v2/repositories/frappe/erpnext/tags/v16.35.0)

**Marcado como no verificado en la investigación:** compatibilidad v15/v16 de `lending` y `webshop`; cifras oficiales de RAM por servicio o de disco por sitio (no existen); requisitos S3/Redis de `drive`; y la ausencia de dominios activos en un sitio v16 stock (inferencia a partir del código, no afirmación de la documentación).