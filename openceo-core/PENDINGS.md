## Tier S — Pilot Test #1 (Ticket #38 "Add IBKR") — Hallazgos y Correcciones Pendientes

**Estado:** PENDIENTE
**Fecha de la prueba:** 2026-06-13 21:57 UTC → 2026-06-14 14:02 UTC (16+ horas)
**Ticket:** #38 — [FASE 4] Infra de Interactive Brokers (IB Gateway)
**Resultado:** ✅ COMPLETED — 9 sub-plans, 2 rondas de review, código generado y funcional.
**Rate limits alcanzados:** Claude (clauded) y Codex (codexf) en sub-plans 6-9.

### Contexto de la prueba

Primera ejecución real de Tier S (`CriticalConsensusOrchestrator`) en un ticket de producción. El pipeline ejecutó: Planner (Claude/Opus max) → Plan Reviewer (Codex/gpt-5.5 xhigh) → Builder (DeepSeek V4 Pro vía claudep) → 5 revisores por ronda × 2 rondas por sub-plan.

**Revisores:** clauded (planner), codexf (plan-reviewer), gemini (gemini-3.1-pro-preview), kimi27c (opencode-go/kimi-k2.7-code), qwen37 (opencode-go/qwen3.7-max).

**Artefactos generados:** 9 `BUILD_SUMMARY_*.md`, 29 review files, `CURRENT_PLAN.md` (20KB final), `CONTEXT.md` actualizado, código en `brokers/interactive_brokers/`, `connectors/ibkr_connector.py`, `ibkr_smoke_test.py`, tests, docs.

### Bugs corregidos DURANTE la prueba (ya aplicados al código)

| # | Bug | Archivo | Fix |
|---|---|---|---|
| B1 | `_is_codex_update_prompt` y 12 métodos inner functions atrapadas dentro de `_codex_model_line_loading` por error de indentación | `tmux_runtime.py:809-970` | Movido `_codex_model_line_loading` dentro de `TmuxRuntime` como `@staticmethod`; cambiada llamada en línea 798 a `TmuxRuntime._codex_model_line_loading()` |
| B2 | `CODEX_CLI_PATH=/home/develop/.npm-global/bin/codex` no existe en el server | `.env:51` | Cambiado a `/home/develop/.local/bin/codex` |
| B3 | `CODEX_DEFAULT_ARGS` vacío en `.env` — no forzaba modelo | `.env:58` | Agregado `--dangerously-bypass-approvals-and-sandbox -m gpt-5.5 -c model_reasoning_effort=xhigh` |
| B4 | Codex reserve thresholds: 20% (5h) y 5% (weekly) vs Claude 0% | `settings.py:150-151` | Bajado a 0%/0% para igualar con Claude |
| B5 | `providers.codex.command` en `project.yaml` apuntaba a `.npm-global` con `gpt-5.4` | `projects/jardal-trading/project.yaml:41` | Actualizado a `/home/develop/.local/bin/codex` con `gpt-5.5` |

### Hallazgos PENDIENTES de corrección (no aplicados — se necesita sesión separada)

#### P1 — CRÍTICO: AI no persiste las señales al archivo `.tier-s/status`

**Síntoma:** Claude (planner), Codex (plan-reviewer), y DeepSeek (builder) imprimen la señal en el TUI (ej. `PLAN_READY_7`, `PLAN_REVIEWED`, `BUILD_DONE_1`, `FIX_DONE_2_1`, `TICKET_COMPLETE`) pero NO la escriben al archivo `.tier-s/status`. El `completion_check` del orquestador solo lee el archivo, por lo que el pipeline se traba esperando una señal que nunca llega al filesystem.

**Impacto:** Requirió intervención manual (escribir la señal con `echo "SEÑAL" > .tier-s/status`) en ~10 ocasiones durante la prueba. Sin intervención, el pipeline se habría colgado en timeouts de 15 minutos por cada fase.

**Archivos involucrados:**
- `openceo-core/agents/dev/consensus.py` — `_read_signal()`, `_reset_signal_file()`, todos los `completion_check` lambdas
- `openceo-core/agents/dev/tmux_runtime.py` — `send_instruction()`, polling loop

**Posible fix:** Agregar un fallback en `send_instruction()` que escanee el contenido del pane de tmux (`current_content`) buscando el patrón de señal esperado (ej. regex `PLAN_READY_\d+`, `BUILD_DONE_\d+`, `FIX_DONE_\d+_\d+`, `TICKET_COMPLETE`). Si el archivo está vacío pero el TUI contiene la señal, darla por válida y continuar. Esto requiere pasar el patrón de señal esperado como parámetro adicional.

**Alternativa:** Hacer que el prompt enviado al AI use una herramienta CLIIntegration/Write en vez de instrucciones en lenguaje natural. Ej: en vez de "Overwrite .tier-s/status with exactly PLAN_READY_7", usar el MCP tool `Write` con path explícito. Pero esto depende de que los AI tengan acceso a MCP tools.

#### P2 — [RESUELTO] Gemini CLI `-y` migrado a Antigravity CLI `agyd`

**Síntoma previo:** El comando `gemini -y -m gemini-3.1-pro-preview` bloqueaba en diálogos de permiso "Allow once? / Allow for this session / No, suggest changes" cada vez que intentaba escribir un archivo de review (`.tier-s/REVIEW-gemini-*-*.md`). El flag `-y` de Gemini CLI solo cubría shell commands pero NO escrituras de archivos.

**Solución aplicada:**
- Migrado a Antigravity CLI usando el wrapper `/home/develop/.local/bin/agyd` (`agy --dangerously-skip-permissions "$@"`).
- Modelo configurado: Sin flag `--model` (utiliza por defecto la última versión Gemini 3.8 Flash High configurada en el CLI).
- Auto-aprueba todas las operaciones de herramientas (`WriteFile`, shell commands, etc.) de forma completamente desatendida.
- Probado exitosamente tanto en ejecuciones directas como en sesiones interactivas `tmux` para revisiones autónomas Tier S.
- Actualizados: `projects/jardal-trading/project.yaml`, `projects/jardal-openceo/project.yaml`, `projects/melanyryr/project.yaml`, `settings.py`, `.env`, `.env.example`, `tmux_runtime.py`.

#### P3 — ALTO: Rate-limit handler no prioriza el `completion_check`

**Síntoma:** En `tmux_runtime.py:send_instruction()`, el polling loop verifica rate-limit ANTES que el completion check. Cuando un AI ya completó su trabajo y escribió `TICKET_COMPLETE` (manualmente al archivo), pero luego usó tokens y volvió a mostrar rate-limit en el TUI, el handler entra en `_handle_rate_limit()` ignorando que la tarea YA está completa.

**Impacto:** Al final de la prueba, el orquestador entró en un segundo ciclo de rate-limit wait de ~24h (porque el reset "2pm UTC" ya había pasado y `_claude_time_to_utc()` lo movió al día siguiente). Toda la orquestación estaba lista (TICKET_COMPLETE en archivo) pero el sistema se negó a terminar. Se tuvo que matar el servidor y marcar la tarea como COMPLETED manualmente en MongoDB.

**Archivos involucrados:** `openceo-core/agents/dev/tmux_runtime.py:230-340`

**Posible fix:** Reordenar las verificaciones en el polling loop de `send_instruction()`:
```python
# 1. Primero verificar completion_check (si ya se cumplió, no importa el rate-limit)
callback_satisfied = self._is_completion_check_satisfied(completion_check)
if callback_satisfied:
    return TmuxTurnResult(ok=True, ...)

# 2. Luego verificar rate-limit
rl = RateMonitor.detect_rate_limit(current_content, provider_name)
if rl is not None:
    ...
```
Esto asegura que si la señal ya está en el archivo, el orquestador termina inmediatamente sin importar el estado del rate-limit del AI.

#### P4 — MEDIO: Rate-limit resume pierde contexto del prompt original

**Síntoma:** `_handle_rate_limit()` en `tmux_runtime.py:382-393` trunca el prompt original a 200 caracteres y envía un mensaje genérico "Continue the task where you left off. You were working on: ..." en vez del prompt específico de la fase (ej. "You are a senior code reviewer (clauded) in round 1 for sub-plan 6..."). El AI no sabe exactamente qué estaba haciendo y puede tomar acciones incorrectas.

**Impacto:** En la fase de finalize, el planner recibió "Continue the task where you left off" en vez del prompt de finalize completo. Esto causó que el planner hiciera un análisis completo de nuevo en vez de solo escribir TICKET_COMPLETE.

**Archivos involucrados:** `openceo-core/agents/dev/tmux_runtime.py:382-395`

**Posible fix:** 
- Guardar el prompt original completo en `_SessionInfo` o pasar `original_prompt` como parámetro adicional a `_handle_rate_limit()`
- Re-send el prompt original completo (no truncado) después del rate-limit wait
- Agregar una nota al inicio: "Rate limit has reset. You were working on the following task. Please continue exactly where you left off:"

#### P5 — MEDIO: OpenCode headless reviewers no generaron reviews

**Síntoma:** Los revisores `kimi27c` (opencode-go/kimi-k2.7-code) y `qwen37` (opencode-go/qwen3.7-max) produjeron **0 archivos de review** durante toda la prueba. Se ejecutaron vía `_one_opencode_review()` en `consensus.py` pero nunca escribieron archivos `.tier-s/REVIEW-{label}-*-*.md`.

**Impacto:** Los sub-plans 6-9 solo tuvieron ~3 revisores efectivos en vez de 5. Con los rate-limits de Claude/Codex, esto se redujo aún más (a veces solo 1 revisor: gemini).

**Archivos involucrados:**
- `openceo-core/agents/dev/consensus.py:744-790` — `_one_opencode_review()`
- `openceo-core/agents/dev/opencode_models.py` — `apply_opencode_model_override()`
- `.env` — `OPENCODE_GO_API_KEY`

**Posible fix:**
- Verificar logs de OpenCode para ver si los subprocesses fallaron silenciosamente
- Revisar si `apply_opencode_model_override()` está generando el comando correcto para los modelos `opencode-go/kimi-k2.7-code` y `opencode-go/qwen3.7-max`
- Verificar que `OPENCODE_GO_API_KEY` esté activa y tenga créditos disponibles
- Agregar logging de fallos en `_one_opencode_review()` — actualmente solo usa `logger.warning` cuando hay excepción, pero si el proceso termina sin output válido no se registra

#### P6 — MEDIO: No hay mecanismo de alerta/notificación cuando el orquestador se bloquea

**Síntoma:** El `CriticalConsensusOrchestrator` no tiene ningún callback o notificación para alertar a un humano cuando algo va mal. Los únicos mecanismos de recuperación son timeouts (15 min por fase) y rate-limit wait-and-resume. Si el sistema se bloquea (ej. señal no escrita, rate-limit loop, reviewer trabado en diálogo de permisos), simplemente espera — sin avisar.

**Impacto:** La prueba duró 16+ horas cuando pudo haber tomado ~2-3. Cada fase bloqueada requirió monitoreo manual constante para detectar el problema e intervenir.

**Archivos involucrados:**
- `openceo-core/agents/dev/consensus.py` — `execute()`, `_send_participant_instruction()`
- `openceo-core/agents/dev/session.py` — `_run_critical_consensus()`

**Posible fix:**
- Agregar un `progress` callback con niveles de severidad (`info`, `warning`, `blocked`, `needs_help`)
- En `send_instruction`, si el completion_check no se cumple después de N ciclos de polling (ej. 5 minutos), emitir `needs_help` con el estado actual
- Integrar con Telegram/WebSocket para notificar al operador
- Agregar un endpoint `/api/tasks/{id}/unblock` que permita al operador forzar señales manualmente desde la UI sin necesidad de SSH
- Considerar un "dead man's switch": si el orquestador lleva más de X minutos en la misma fase sin progreso detectable (archivo de señal sin cambios), enviar alerta

### Lecciones aprendidas (para CONTEXT.md)

1. **Siempre verificar paths de binarios antes de lanzar Tier S** — `CODEX_CLI_PATH`, `CLAUDE_CODE_PATH`, `codexf`, `claudep`, `clauded` deben existir y ser ejecutables.
2. **Rate limits son el principal enemigo de orquestaciones largas** — 9 sub-plans × 2 rondas × 5 revisores = 90 revisiones. Con Claude/Codex con ~3h de autonomía real, se necesita monitoreo de cuota o reducir rounds/reviewers.
3. **El Plan Reviewer es la fase más valiosa** — expandió el plan de 7 a 9 sub-plans con detalles críticos que el planner inicial omitió.
4. **Las señales manuales funcionan pero no escalan** — se necesita el fix P1 antes de correr tickets más grandes.
5. **Los timeouts de 15 min son razonables pero los rate-limit waits de 24h son inaceptables** — el fix P3 es urgente.
6. **Gemini CLI con `-y` no era suficiente para modo autónomo (P2)** — Migrado a Antigravity CLI (`agyd`) con `--dangerously-skip-permissions`, resolviendo totalmente los bloqueos de permisos.
7. **El builder (DeepSeek V4 Pro) fue el más confiable** — nunca rate-limiteó, siempre escribió build summaries correctamente, y manejó bien la ausencia de reviews ("No review files found ✅").
