# Fixtures de research

## `opencode_run_2_0_20.jsonl`

Muestra el JSONL que consume `research/runtimes/opencode.py`.

**Procedencia: SINTETICA, no capturada del binario.** Este entorno no tiene instalado
el CLI de OpenCode (`/home/develop/.opencode/bin/opencode` no existe), asi que no fue
posible capturar una ejecucion real de la version fijada (v2.0.20). La fixture se
construyo a partir de la forma documentada de eventos e incluye a proposito un tipo de
evento desconocido para fijar la tolerancia del parser.

Pendiente registrado en TASK-2026-00062: sustituirla por la salida real de
`opencode run --standalone --agent investigator --format json` de la version fijada,
ejecutada en un host con el CLI y con aislamiento disponible. El parser no debe
presumirse compatible con una version distinta de la fijada.
