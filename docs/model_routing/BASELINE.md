# Línea base F0 — Router de modelos gratuitos (TASK-2026-00285)

Fecha: 2026-10-23. Rama `feat/model-routing`. Inventario de solo
lectura, sin generación, sin llamadas a modelos ni a APIs externas.

## 1. Versiones y flags

`opencode --version` → `opencode v2.0.22`
(`~/.opencode/bin/opencode`). Hay un servicio en segundo plano en
`127.0.0.1:4096` (`opencode serve`); los revisores usan
`opencode run --standalone`.

`opencode run --help` (resumen): `--standalone`, `--server`,
`--continue/-c`, `--session/-s`, `--fork`, `--model/-m
provider/modelo#variante`, `--agent`, `--format`, `--file/-f`,
`--title`, `--thinking`, `--auto`.

Esquema de configuración: clásico. `~/.config/opencode/opencode.json`
declara `$schema: https://opencode.ai/config.json` con claves
`permission` y `mcp` (solo nombres de clave inspeccionados, sin
valores). Sin `opencode.json` en el repo. El subcomando `serve`
describe un servidor API v2, pero el fichero de configuración
observado es el formato clásico.

`musej --version` → `Muse Code 1.4.12 (1.4.12-R6909.1)`.
Subcomandos: `resume`, `exec`, `config`, `export`, `trace`,
`model-profile`, `skills`, `voice`, `plugins`, `sandbox`, `schema`,
`serve`, `session-message`, `mcp`, `auth`, `login`, `logout`, `init`.

`agyd --version`, `agyj --version`, `agy2 --version` → `1.4.4` los
tres (`~/.local/bin`). `agy2 --help` (resumen): `--agent`, `--model`,
`--mode`, `--effort`, `--print/-p`, `--output-format`,
`--input-format`, `--continue/-c`, `--sandbox`, más subcomandos
`agent`, `changelog`, etc.

`bwrap --version` → `bubblewrap 0.9.0`. Flags relevantes presentes:
`--unshare-{all,net,ipc,pid,user,uts,cgroup}`, `--share-net`,
`--uid/--gid`, `--hostname`, `--ro-bind/--bind`, `--die-with-parent`.

`python3 --version` y `./.venv/bin/python --version` → `Python 3.12.3`.
Pydantic del venv: `2.11.7`.

## 2. Crons y timers del host

`crontab -l`: 2 entradas, ninguna relacionada con catálogos o
modelos (reinicio diario de `vpn-rest`; refresco cada 15 min del
índice `qmd` del vault).

`systemctl --user list-timers`: 4 timers, ninguno relacionado con
catálogos o modelos (`jardal-burnin-drill`, `launchpadlib-cache-clean`,
`jardal-knowledge-backup`, `jardal-burnin-check`).

## 3. Consumidores actuales de modelos gratuitos

- Free review slots (retirado 2026-11-01, TASK-2026-00428):
  `catalog/reviewer_models.yaml` eliminado; el par free es solo
  dinámico (ranking, si no catálogo live, si no bloqueo ruidoso).
- `openceo-core/orchestration/review.py` (1061 líneas): pool de
  revisores, rondas con quórum, recuperación de slots y telemetría
  `round_telemetry` (`rounds[]` por run: slots, informes válidos,
  hallazgos, intentos con slot/kind/reason/duración).
- `openceo-core/orchestration/provider_recovery.py` (52 líneas):
  reintentos con esperas 300/900/1800 s; `transient_provider_error`
  reconoce 429, 5xx, `provider.quota`, `FreeUsageLimitError`,
  timeouts y cortes de conexión.
- `openceo-core/rate_monitor.py` (746 líneas): `RateMonitor` con
  monitores tmux por proveedor que sondean `/status` (`codex`) y
  `/usage` (`claude`, `muse`); `ProviderStatus` con restante 5h/semanal.
- Adaptador OpenCode de research:
  `openceo-core/research/runtimes/opencode.py` (+
  `openceo-core/research/service.py`, `openceo-core/research/wiring.py`).
  Ajustes en `settings.py`: `RESEARCH_OPENCODE_CLI_PATH`
  (por defecto `.opencode/bin/opencode` del repo),
  `RESEARCH_OPENCODE_VERSION_PIN="2.0.20"` (instalado: 2.0.22),
  `RESEARCH_OPENCODE_TEST_MODEL="opencode/muse-spark-1.3-contributor-free"`,
  `RESEARCH_OPENCODE_PROD_MODEL="meta/muse-spark-1.3-contributor"`,
  `RESEARCH_OPENCODE_TIMEOUT_SECONDS=300`.

## 4. Línea base: métricas previas con un comando único

Comando (solo lectura, agregados acotados, sin credenciales en salida):

```bash
./.venv/bin/python -c "
import sys, json
sys.path.insert(0, 'openceo-core')
from pymongo import MongoClient
import settings as s
st = s.Settings()
db = MongoClient(st.MONGODB_URL, serverSelectionTimeoutMS=5000)[st.MONGODB_DATABASE]
runs = db.tier_s_runs.estimated_document_count()
print('tier_s_runs:', runs)
for r in db.tier_s_runs.aggregate([{'\$group': {'_id': '\$status', 'n': {'\$sum': 1}}}]): print(' run-status', r['_id'], r['n'])
print('runs-with-rounds:', db.tier_s_runs.count_documents({'rounds.0': {'\$exists': True}}))
d = db.tier_s_runs.find_one({'rounds.0': {'\$exists': True}}, {'_id': 0, 'rounds': 1})
for x in (d or {}).get('rounds', []): print(' round r=%s slots=%s valid=%s attempts=%s' % (x.get('r'), x.get('slots'), x.get('valid_reports'), len(x.get('attempts') or [])))
durs = sorted(round((r['completed_at']-r['created_at']).total_seconds()) for r in db.tier_s_runs.find({'completed_at': {'\$exists': True}}, {'_id': 0, 'completed_at': 1, 'created_at': 1}).limit(25))
print('completed-durations-s:', durs)
print('task_reviews:', db.task_reviews.estimated_document_count())
for r in db.task_reviews.aggregate([{'\$group': {'_id': {'a': '\$action', 's': '\$approval_state_after'}, 'n': {'\$sum': 1}}}, {'\$sort': {'n': -1}}]): print(' review', r['_id'], r['n'])
"
```

Salida resumida (2026-10-23):

- `tier_s_runs`: 23 docs. Estados: superseded 17, cancelled 2,
  converted 2, failed 1, waiting 1.
- Solo 1 run con `rounds[]`: ronda 1, slots
  `[dsh, muse-review, qwen-glm, qwen-max]`, 3 informes válidos,
  0 intentos detallados.
- Duraciones completadas (s): 1521, 4215, 36200 (3 runs).
- `task_reviews`: 178 docs (trazabilidad de aprobación humana, no
  métricas por slot): approve→approved 102, request_changes 41,
  resto 35 entre pendientes y comentarios.

Presupuesto real consumido en F0: 0 llamadas (aceptación sin tokens).

## 5. Límites: lo que no pudo verificarse

- No existe serie previa de fallos ni de 429/cuota por slot: ningún
  `rounds[]` almacenado trae `attempts[]` (el código actual sí lo
  emite; los runs guardados son anteriores). La autoridad de
  disponibilidad y el registro de errores (TASK-2026-00291) crearán
  esa serie.
- `muse-review` cuenta como slot en la única ronda guardada aunque no
  cuenta para el quórum; las duraciones por slot no están registradas.
- `opencode models` no sirve como fuente de catálogo en v2.0.22
  (lista vacía según el estado del servicio; detalle ya registrado en
  el ticket, comentario del 2026-10-23).
- Pin de research (`2.0.20`) frente a instalado (`2.0.22`): pendiente
  de decidir si se sube el pin (fuera de F0; posible insumo para
  TASK-2026-00289).
- El comando de métricas lee Mongo local (`openceo`, `localhost`);
  si el servicio no está disponible, la línea base no es reproducible
  hasta restaurarlo. No se consultaron ejecuciones de otros proyectos.
