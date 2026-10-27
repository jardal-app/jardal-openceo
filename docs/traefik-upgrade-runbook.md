# Runbook — Actualizar Traefik sin romper producción

**Fecha de auditoría:** 2026-09-20 · **Estado: EJECUTADO el 2026-09-20 a las 19:38 UTC** — corre `v3.7.13`, contenedor recreado sin incidentes y parque verificado por sondeos. Bitácora con la evidencia en `DIGEST-logs-2026-09-20.md` (en el directorio del despliegue). El procedimiento se conserva intacto por si hay que repetirlo o aplicarlo a una versión futura.
**Ámbito:** `/home/develop/deployments/jardal-traefik/` (Traefik que enruta los 46 servicios de producción)
**Relacionado:** [`JARDAL_ERP.md`](file:///home/develop/development/Jardal/jardal-openceo/JARDAL_ERP.md) §6 · `AGENTS.md` del propio directorio de despliegue

> **Respuesta corta a "¿tengo que tocar Traefik antes de Jardal ERP?" → NO.** Jardal ERP no necesita ningún cambio de Traefik: se integra con las 3 labels que ya usa toda la producción. La actualización de versión es **una mejora de seguridad independiente**, y este documento existe para que se haga sin riesgo. No hay que hacerlas juntas.

---

## 1. Qué corre hoy (auditado, no asumido)

| Dato | Valor verificado |
|---|---|
| Imagen y versión | `traefik:v3.0` → **3.0.4 "beaufort"**, compilada **2024-07-02** |
| Fuente de verdad | **`/home/develop/deployments/jardal-traefik/docker-compose.yml`** |
| Proyecto compose | `working_dir=/home/develop/deployments/jardal-traefik`, `config_files=<ese mismo compose>` |
| Netns | **`network_mode: host`** (no red bridge; por eso los servicios de `dynamic.yml` van a `127.0.0.1:PUERTO`) |
| Entrypoints | `web=:80`, `websecure=:443`, `livekit-tcp=:7881`, `livekit-udp=:7882/udp`, `ln=:9735` |
| Providers | `docker` (`exposedbydefault=false`) + `file` (`/etc/traefik/dynamic.yml`, `watch=true`) |
| ACME | `jardal-resolver`, **`tlschallenge=true`**, storage `/letsencrypt/acme.json` |
| `acme.json` | host: `./letsencrypt/acme.json` · **728 KB · 59 certificados · `Account` presente · permisos `600`** |
| Daemon Docker | **28.3.3, API 1.51** |
| Auto-updaters | **ninguno** (sin watchtower/diun) → la versión solo cambia si se cambia el tag |
| Rollback | imagen `traefik:v3.0` **presente localmente** |
| Compose | v2.40.3 |

### Convención de labels que ya usa toda la producción (verificada en `openwebui` y `n8n`)

```
traefik.enable=true
traefik.docker.network=traefik-net
traefik.http.routers.<r>.rule=Host(`host.jardal.app`)
traefik.http.routers.<r>.entrypoints=websecure
traefik.http.routers.<r>.tls.certresolver=jardal-resolver
traefik.http.services.<svc>.loadbalancer.server.port=PUERTO
```

No hay middlewares globales aplicados a todos los routers (solo `mongo-express-auth`, `portainer-ratelimit` y los `*-auth` de `dynamic.yml`). Nada se hereda: un servicio nuevo no recibe reglas ajenas.

---

## 2. ️ Dos peligros que no tienen nada que ver con la versión

### 2.1 El repo git miente — y usarlo puede tumbar todo

`/home/develop/development/Jardal/jardal-traefik/docker-compose.yml` está **desactualizado** (2026-02-06): modo bridge con `ports:` en vez de `network_mode: host`, sin `dynamic.yml`, con `ln` pero sin `livekit-*`. El despliegue real es el de `deployments/`.

Ambos directorios se llaman `jardal-traefik`, así que **el project name de compose es el mismo**. Ejecutar `docker compose up -d` **desde el directorio del repo** haría que compose crea que el proyecto es suyo, **recreara el contenedor `traefik` con la configuración vieja** y, al intentar publicar los puertos 80/443 que el Traefik real ya ocupa en modo host, fallara → **caída de los 46 servicios**.

**Regla absoluta: los comandos de este runbook se ejecutan SOLO desde `/home/develop/deployments/jardal-traefik/`. Nunca desde el directorio del repo.** Al final (§7) hay un pendiente para dejar el repo coherente.

### 2.2 Traefik está en bucle de errores ACME (riesgo para el certificado de Jardal ERP)

Baseline medido el 2026-09-20 18:20 UTC — **el 100% de las últimas 2000 líneas de log son `ERR`**:

| Error | Ocurrencias | Causa raíz verificada |
|---|---|---|
| `Unable to obtain ACME certificate for domains [kimi.jardal.app] … 429 rateLimited` | **665** | `kimi.jardal.app` resuelve a **IPs de Cloudflare** (`188.114.96.3`, `188.114.97.3`). Con el challenge **TLS-ALPN**, Let's Encrypt habla con Cloudflare, nunca con este Traefik → **ese certificado no se puede obtener jamás**. Traefik reintenta cada ~12–60 s. |
| `Router uses a non-existent certificate resolver certificateResolver=letsencrypt` | **1328** | Los routers `fugovpn-api@docker` (hosts `api.fugovpn.app` y `auth.fugovpn.app`) y `fugovpn-dev-frontend@docker` (hosts `dev.fugovpn.app` y `dev.jardal.app`) piden un resolver llamado `letsencrypt` **que no existe** (el único es `jardal-resolver`). Además `api.fugovpn.app` también está detrás de Cloudflare, y `dev.jardal.app` **no tiene registro DNS**. |

**Por qué esto importa antes de Jardal ERP:** los 429 de Let's Encrypt son de **cuenta** ("your account is temporarily prevented… for `kimi.jardal.app` **and possibly others**"). Cada reintento consume el presupuesto de *new orders* de la cuenta. Emitir el certificado de `erp.jardal.app` con ese bucle encendido es apostar a que alcanza la cuota.

**Fix (sin reiniciar Traefik, sin downtime):**

1. **Regla derivada, aplicable a todo hostname detrás de Cloudflare: NO debe llevar `certResolver` en Traefik.** El certificado lo pone Cloudflare. Aplicarlo a `kimi`, `fugovpn-api` y `fugovpn-dev-frontend`.
2. `kimi` vive en `dynamic.yml`: quitar `tls: certResolver:` de `kimi-websecure` (o eliminar ese router si no se usa el acceso HTTPS directo). El archivo se recarga solo (`watch=true`), **sin tocar el contenedor**.
3. `fugovpn-*` vive en labels del contenedor: quitar `traefik.http.routers.<r>.tls.certresolver=letsencrypt` y **recrear solo esos contenedores** (no Traefik).
4. Si además se quiere el certificado real para `kimi.jardal.app`, la vía correcta con Cloudflare es **DNS challenge**, no TLS-ALPN (implica cambiar la config ACME: otro cambio, otra ventana).
5. Opcional: los 429 se pueden silenciar mientras tanto sin tocar nada más (no recomendado: el bucle seguiría quemando cuota).

**Criterio de éxito del fix:** `docker logs traefik --tail 300 | grep -c 'ERR'` ≈ **0**, y los 59 certificados siguen intactos.

> **Nota de baseline y una trampa de comandos:** el log sale por **stderr y con códigos de color**, así que `grep ' ERR '` devuelve 0 falsos negativos. El comando correcto es `docker logs traefik --tail 2000 2>&1 | grep -c 'ERR'`. Comparar contra este baseline (100% ERR) es imprescindible: sin él, cualquier verificación posterior al upgrade es ininterpretable.

---

## 3. Por qué actualizar (y por qué no es urgente para Jardal ERP)

| Motivo | Detalle |
|---|---|
| **La línea 3.0 está fuera de soporte** | La tabla oficial de mantenimiento da a 3.0 soporte comunitario **terminado el 2024-07-15**. Lleva ~2 años sin parches. Han salido releases de seguridad para las líneas 2.11/3.6/3.7. |
| **No hace falta para Jardal ERP** | Jardal ERP usa `Host()`, `entrypoints`, `tls.certresolver` y `websocket`: todo existe en 3.0. |
| **Versión objetivo** | **`v3.7.13`** (publicada 2026-09-18, `sha256:1c32e7c368…`). Fijar el **tag de parche**, no `v3.7` ni `v3`. No existe v4 en las etiquetas recientes: no hay salto mayor pendiente. |

---

## 4. Compatibilidad de NUESTRA configuración con 3.7 (verificado, no supuesto)

### Lo que sí aplica a este servidor

| Punto | Estado |
|---|---|
| **Docker API** | v3.6.16+ exige API ≥1.40. Aquí hay **1.51** → ✅ |
| **Opciones deprecadas en uso** | Barrido de todas las labels de todos los contenedores + `dynamic.yml` buscando `proxyprotocol`, `strategy`, `underscoreheaders`, `dnschallenge`, `disablepropagationcheck`, `delaybeforecheck`, `trustforwardheader`, `lbswarm`, `multipathtcp`, `errorrequestheaders` → **NINGUNA**. Sin warnings esperados por deprecación. |
| **BasicAuth** | Desde v3.7.3 un `basicAuth` con `users` vacío **hace fallar el router** (404). Los tres middlewares de este servidor (`mongo-express-auth`, `agentboard-auth`, `kimi-auth`) tienen usuarios definidos → ✅ |
| **`acme.json`** | El formato **no cambia** entre 3.0 y 3.7 → los 59 certificados se conservan y **no se piden certificados nuevos al arrancar** (esto además evita un pico ACME justo tras el reinicio). `cert-dumper --version v2` sigue siendo válido. |
| **`redirectScheme` y `ratelimit`** | Sin cambios documentados entre 3.0 y 3.7 → ✅ |
| **Provider `file`** | Sin cambios propios; `watch=true` sigue funcionando → ✅ |
| **Cambios K8s (RBAC, Gateway API, CRDs, IngressClass)** | No aplican: aquí no hay Kubernetes. |

### Cambios de comportamiento a vigilar durante la verificación (no son errores, pero cambian semántica)

| Versión | Cambio |
|---|---|
| 3.2.1 | `X-Forwarded-Prefix` se elimina si viene de fuente no confiable. |
| 3.3.6 | Sanitización de rutas `/../`, `/./`, `//` activada por defecto (desactivable con `sanitizePath`, no seguro). |
| 3.4.1 | Normalización de la ruta de request (decodificación no reservada, normalización de mayúsculas). |
| 3.5.0 | `traceVerbosity` nuevo; por defecto `minimal` (menos spans que antes). |
| 3.6.4 / 3.6.7 | `entryPoints.<n>.http.encodedCharacters.*` pasa a `true` por defecto. |
| 3.7.7 | `Host(`*`)` pasa a ser catch-all (antes solo un segmento). No se usa. |
| 3.7.9 | Requests HTTP/1 con método `CONNECT` → `501 Not Implemented`. |
| 3.7.12 | Pesos negativos en servicios TCP/UDP ponderados se rechazan. No se usa. |
| 3.7.13 | `Upgrade: h2c`/`HTTP2-Settings` ya no se reenvían. |
| 3.6.14 / 3.4 | Deprecaciones con retiro en **el próximo major**, no en 3.7: `ForwardAuth.trustForwardHeader` y la estrategia HTTP `RoundRobin`. Ninguna usada. |

**Conclusión honesta:** por lectura de documentación **no se puede garantizar al 100%** que no rompa nada — y nadie debería prometerlo. Lo que sí da garantía real es la **validación en seco con la imagen candidata** (§5.3) más un **rollback de un comando** (§5.6). Ese es el mecanismo de seguridad, no la tabla de arriba.

---

## 5. Procedimiento

> Ejecutar **desde** `/home/develop/deployments/jardal-traefik/`. Ventana de baja actividad. No mezclar con el fix de ACME (§2.2) en el mismo paso: primero el fix, después (o en otro momento) el upgrade.

### 5.1 Respaldos (2 minutos, obligatorio)

```bash
cd /home/develop/deployments/jardal-traefik
FECHA=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /home/develop/backups/traefik-$FECHA
cp -a docker-compose.yml dynamic.yml letsencrypt/acme.json /home/develop/backups/traefik-$FECHA/
ls -l /home/develop/backups/traefik-$FECHA/
docker images traefik --format '{{.Repository}}:{{.Tag}} {{.Size}}'   # confirmar traefik:v3.0 local (rollback)
```

### 5.2 Descargar la imagen objetivo ANTES de la ventana

```bash
docker pull traefik:v3.7.13
docker image inspect traefik:v3.7.13 --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}}'
```

### 5.3 Validación en seco con la imagen candidata (el paso que evita la caída)

Levanta **un Traefik candidato descartable** que lee la misma config pero **no toca puertos, ni ACME, ni `acme.json`**. Si la config fuera incompatible, falla aquí y producción ni se entera.

```bash
docker rm -f traefik-validate 2>/dev/null

docker run --rm --name traefik-validate \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /home/develop/deployments/jardal-traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro \
  traefik:v3.7.13 \
    --providers.docker=true \
    --providers.docker.exposedbydefault=false \
    --providers.file.filename=/etc/traefik/dynamic.yml \
    --providers.file.watch=true \
    --entrypoints.web.address=:8081 \
    --entrypoints.websecure.address=:8443 \
    --log.level=INFO
```

**Qué se debe ver (evidencia de que la config es válida):**
- `Configuration loaded from flags`
- `Starting provider *file.Provider` y `Starting provider *docker.Provider`
- `Starting provider *acme.Provider`… **no**, ACME no se pasó a propósito: no debe aparecer nada de ACME.
- Que siga vivo (no `exited`).

**Qué es esperado y NO es un fallo:**
- `Router uses a non-existent certificate resolver` (los routers con `tls.certresolver` no encuentran resolver porque aquí no pasamos ACME). Es la confirmación de que los 46 routers se cargaron.
- `Unable to obtain ACME…` **NO debe aparecer** (sin flags ACME no hay órdenes) → si aparece, se abortó mal el comando y hay que revisarlo.

**Criterio de aborto:** cualquier línea `error while building configuration`, `invalid configuration`, `command error`, o que el contenedor salga. Si pasa: **no seguir**, anotar el mensaje y quedarse en 3.0.

Apagar: `docker rm -f traefik-validate`

### 5.4 El cambio (segundos de corte)

```bash
cd /home/develop/deployments/jardal-traefik
cp -a docker-compose.yml docker-compose.yml.bak-v3.0
# editar la ÚNICA línea:  image: "traefik:v3.0"  ->  image: "traefik:v3.7.13"
# (dejar el resto del archivo idéntico)
grep -n 'image:' docker-compose.yml

date -u; docker compose up -d traefik; date -u
docker compose ps traefik
docker inspect traefik --format '{{.Config.Image}} restarts={{.RestartCount}} started={{.State.StartedAt}}'
```

### 5.5 Verificación (pegar los resultados en el registro de la sesión)

```bash
echo '--- errores (comparar con baseline §2.2: 0 esperado tras el fix de ACME) ---'
docker logs traefik --tail 500 2>&1 | grep -c 'ERR'
docker logs traefik --tail 500 2>&1 | grep 'ERR' | head -20

echo '--- certificados intactos ---'
python3 -c "
import json;d=json.load(open('/home/develop/deployments/jardal-traefik/letsencrypt/acme.json'))
print({k: len(v.get('Certificates') or []) for k,v in d.items()})"   # esperado: 59

echo '--- sondeos HTTP/HTTPS (esperado 200/301/401 según cada caso) ---'
for h in n8n.jardal.app chat.jardal.app api.openceo.jardal.app drive.jardal.app mail.jardal.app portainer.jardal.app objects.jardal.app code.jardal.app agentboard.jardal.app opencode.jardal.app dev.openceo.jardal.app businessintelligenceii.jardal.app mikrosaludsur-api.jardal.app; do
  printf '%-38s %s\n' "$h" "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 https://$h 2>/dev/null)"
done

echo '--- certificado servido por Traefik (debe decir Let'"'"'s Encrypt) ---'
for h in n8n.jardal.app drive.jardal.app; do
  echo "--- $h"; timeout 8 openssl s_client -connect $h:443 -servername $h </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates
done

echo '--- rutas no-HTTP (TCP/UDP): mongo y livekit ---'
timeout 8 openssl s_client -connect mongo.jardal.app:443 -servername mongo.jardal.app </dev/null 2>/dev/null | head -3
```

**Tabla de referencia de los sondeos** (medida hoy antes del cambio; sirve para comparar):

| Host | Vía | Esperado |
|---|---|---|
| `n8n.jardal.app`, `drive.jardal.app`, `mail.jardal.app`, `objects.jardal.app`, `portainer.jardal.app` | labels Docker, websecure | `200`/`301` |
| `chat.jardal.app`, `api.openceo.jardal.app` | labels + Cloudflare Tunnel (`web` sin redirect) | `200` (tunnel) |
| `code.jardal.app`, `opencode.jardal.app`, `agentboard.jardal.app` | **file provider** (`dynamic.yml`) | `200`; `agentboard` exige auth → `401` |
| `businessintelligenceii.jardal.app` | 7 routers con `PathPrefix` sobre un host | `200`/`404` según ruta |
| `mongo.jardal.app`, `livekit` | **TCP/UDP** (`traefik.tcp/udp.*`) | handshake TLS |

### 5.6 Rollback (si cualquier verificación falla)

```bash
cd /home/develop/deployments/jardal-traefik
sed -i 's|image: "traefik:v3.7.13"|image: "traefik:v3.0"|' docker-compose.yml
docker compose up -d traefik
docker inspect traefik --format '{{.Config.Image}}'   # traefik:v3.0
```

Corte esperado del rollback: **segundos**. Los certificados y los 46 routers se recuperan solos porque `acme.json` y las labels no se tocaron. Si el rollback no bastara: restaurar `docker-compose.yml` desde `/home/develop/backups/traefik-$FECHA/` y repetir.

### 5.7 Reglas de seguridad de esta operación

- **No** ejecutar compose desde `/home/develop/development/Jardal/jardal-traefik` (§2.1).
- **No** cambiar el nombre de la red `traefik-net`: la usan 46 servicios por label.
- **No** añadir flags ACME en la validación en seco (§5.3): se evita quemar cuota de Let's Encrypt.
- **No** usar `docker compose down` (tumbaría el proxy y, con él, la entrada de todos los servicios). Solo `up -d traefik`.
- **No** tocar `dynamic.yml` durante el upgrade: si se cambian dos cosas a la vez y algo falla, no se sabe cuál fue.
- **Sí** anotar la hora de inicio/fin y pegar las salidas: eso es la evidencia de que no se rompió nada.

---

## 6. Después del upgrade

1. Verificar que sigue así a las 24 h: `docker logs traefik --since 24h 2>&1 | grep -c 'ERR'` (comparar con el baseline §2.2).
2. Renovación de certificados: los 59 certificados vencen entre nov-2026 y ene-2027; el primer ciclo de renovación real será la prueba de fuego del ACME en 3.7. Anotar la fecha y revisar.
3. **Sincronizar el repo git** (`/home/develop/development/Jardal/jardal-traefik/`) con la configuración real de `deployments/`, o dejar un README que diga que la fuente de verdad es `deployments/`. Hoy el repo es una trampa activa (§2.1).
4. Cerrar los pendientes de §2.2 si se dejaron para después.

---

## 7. Fuera de alcance de este runbook

- **Jardal ERP**: no requiere nada de esto (`JARDAL_ERP.md` §6). Se despliega con las 3 labels ya convencionales.
- **Habilitar el dashboard/API de Traefik** para inspeccionar routers: sería útil para verificar, pero es superficie nueva en producción; se decide aparte. Hoy no está habilitado (`--api.dashboard` y `--api.insecure` están comentados), y por eso la verificación se hace con sondeos reales.
- **DNS challenge** para hostnames detrás de Cloudflare: cambio de configuración ACME, otra ventana.

---

## 8. Fuentes

**Documentación oficial de Traefik:**
- Guía de migración v3 (secciones 3.0→3.7, filtradas a lo que no es Kubernetes): https://raw.githubusercontent.com/traefik/traefik/v3.7/docs/content/migrate/v3.md
- Estados de mantenimiento por versión: https://doc.traefik.io/traefik/deprecation/releases/
- Etiquetas de la imagen (versión vigente y fecha): https://hub.docker.com/v2/repositories/library/traefik/tags?page_size=40&ordering=last_updated

**Auditoría local (2026-09-20, evidencia en §1, §2.2 y §4):** inspección de `docker inspect` del contenedor `traefik` y de los 46 contenedores con labels `traefik.*`; lectura de `/home/develop/deployments/jardal-traefik/{docker-compose.yml,dynamic.yml,AGENTS.md}`; `docker logs traefik --tail 2000`; `openssl s_client` contra hostnames en producción; conteo y dominios de `acme.json` (sin exponer claves).

**No verificado / límites de esta auditoría:** no se obtuvo la lista completa de notas de release 3.3→3.7 más allá de la guía de migración (por eso el runbook valida con la imagen candidata en vez de confiar en la lista); no se probó ninguna versión nueva en este servidor; el efecto exacto de la sanitización de rutas (§4) sobre apps concretas con `//` en URLs (minio, filestash) no se probó — está en la lista de sondeos.