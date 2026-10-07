# Fixtures de research

## Procedencia: REAL, capturada del binario de la version fijada

Estas fixtures **no son sinteticas**. Se capturaron de ejecuciones reales de
`/home/develop/.opencode/bin/opencode` **v2.0.20** (la version fijada en
`research/runtimes/opencode.py`) con `opencode run --standalone --format json`, el
2026-10-01, contra el modelo `opencode/muse-spark-1.3-contributor-free`.

El CLI se invoco con `XDG_CONFIG_HOME` apuntando a un directorio propio, de modo que
la unica configuracion cargada sea la del worker. No contienen secretos, claves ni
datos personales: solo eventos del protocolo y texto publico de la respuesta.

### `opencode_run_2_0_20.jsonl`

Ejecucion real correcta y minima. Eventos: `step_start`, `text` (con `part.text`).
Comando:

```
opencode run --standalone --agent investigator --format json \
  --model opencode/muse-spark-1.3-contributor-free \
  "En una frase: que es el protocolo MCP?"
```

### `opencode_run_2_0_20_tools.jsonl`

Ejecucion real con uso de herramienta. Eventos: `step_start`, `text`, `tool_use`,
`step_finish` (secuencia completa de tres pasos). Fija la forma real de `tool_use` y
`step_finish`, que la fixture sintetica anterior inventaba.

### `opencode_run_2_0_20_error.jsonl`

Fallo real del proveedor: `type: error`, `error.type: provider.auth`, `status: 403`,
`message: "OpenCode's free tier can only be used from within OpenCode"`, con **exit
code 1 y stderr vacio**. Es el motivo por el que el runtime tiene que leer el error
del JSONL y no del stderr.

Se reproduce de forma determinista cuando el perfil del agente cierra el worker
(deny por defecto `{"action":"*","resource":"*","effect":"deny"}`, o denegar `read` o
`shell`). Ver `TASK-2026-00056` (documento original adjunto al ticket).

### `opencode_run_2_0_20_agent_not_found.jsonl`

Fallo real del CLI cuando la definicion del agente no es valida para v2.0.20:
`Agent not found: "investigator"`. Se reproduce con el formato antiguo de permisos
(dict `{"bash": "deny"}`) en `opencode.json`: OpenCode **descarta el agente entero**
en vez de avisar del esquema. Fue el defecto real que encontro la captura del
2026-10-01 en `research/worker/profiles.py`.
