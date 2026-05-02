# AGENTS.md

Guía para cualquier agente que continúe este proyecto.

## 0) Lectura obligatoria al iniciar

1. `STATUS.md` (estado real y comportamiento vigente)
2. `README.md` (arquitectura y setup general)
3. `openceo-core/main.py` y `openceo-core/dev_session_service.py` (flujo runtime real)

No asumas comportamiento: valida primero en código y, si aplica, en contenedor.

## 1) Objetivo del runtime actual

Evitar que OpenWebUI se "tranque" con turnos largos de `codex/clauded`.

Por diseño vigente:
- `openceo-dev` tiene modo conversación + modo sesión activa.
- La sesión de código se activa solo por intención explícita (ej: `crea una sesión dev`).
- Los turnos dev se ejecutan en **background** (`send_message_async`), con respuesta inmediata.
- El usuario consulta avance con polling (`ya terminó?`, `/session status`, etc.).
- `openceo` (CEO) no debe disparar sesiones dev con mensajes genéricos; solo con intención explícita de sesión.

## 2) Flujo de `openceo-dev` que NO se debe romper

1. Sin sesión activa:
- Responder conversacionalmente local, en español (sin depender de API externa para `openceo-dev`).
- No crear sesión dev automáticamente.

2. Activación explícita:
- Crear/activar sesión dev para el proyecto seleccionado.

3. Sesión activa + instrucción:
- Responder al instante: "ejecución en segundo plano".
- No bloquear HTTP/SSE esperando terminar provider.

4. Sesión activa + consulta de progreso:
- Retornar estado actual (`running|waiting_user|failed|...`) y último resultado cuando exista.

5. Cerrar sesión:
- Permitir volver a modo conversación (`cerrar sesión dev`).

6. Comandos naturales de gestión:
- `selecciona la 2 ...` debe resolver por índice de proyectos (1-based).
- `crea una sesión codex|claude|glm` debe respetar provider solicitado.
- `elimina esa sesión` cancela la sesión activa del chat.
- `elimina todas las sesiones` cancela todas las sesiones abiertas del usuario.
- `que sesiones hay activas` lista sesiones abiertas por proyecto/provider.
- `usa la sesion de <proyecto>` / `selecciona la sesion 2` cambia el foco de sesión del chat.
- Preguntas administrativas (`que proyectos hay`, `status`, `que respondio`) no deben disparar ejecución CLI.
- Para voz/chat, evita responder con UUIDs de sesión salvo que el usuario los pida explícitamente.

## 3) Archivos críticos

- `openceo-core/main.py`
  - Enrutamiento OpenWebUI y comandos de sesión dev.
- `openceo-core/dev_session_service.py`
  - `send_message_async`, workers de background por sesión.
- `openceo-core/db/mongo.py`
  - Estado de sesión activa por chat (`user_context.active_dev_session_id`, etc.).
- `openceo-core/agents/dev/provider_runtime.py`
  - Ejecución CLI real (`communicate` actual, no streaming incremental aún).

## 4) Validación mínima antes de cerrar cambios

1. Compilación Python:
- `python3 -m py_compile openceo-core/main.py openceo-core/dev_session_service.py openceo-core/db/mongo.py`

2. Rebuild y restart:
- `docker compose up -d --build openceo-core`

Alternativa host-mode (sin Docker para `openceo-core`):
- `scripts/openceo-core-host.sh restart`
- Puerto por defecto: `18765` (override con `OPENCEO_HOST_PORT`).
- Para servicio persistente usar `systemd --user` con `deploy/systemd/openceo-core-host.service`.

3. Smoke test OpenWebUI-compatible (`/v1/chat/completions`):
- `crea una sesion dev` -> activa sesión
- instrucción dev -> respuesta inmediata (no bloqueo)
- `ya termino?` o `cual es el status?` -> progreso/resultado
- `que respondio?` -> salida/respuesta final de la sesión
- `que sesiones hay activas?` -> listado natural sin UUIDs largos
- `cerrar sesion dev` -> vuelta a conversación

## 5) Reglas de mantenimiento

- Si cambias flujo runtime, actualiza `STATUS.md` en la misma sesión.
- Prioriza compatibilidad con OpenWebUI (timeouts cortos).
- No introducir regresiones que vuelvan síncrono el peer programming.
- Mantener mensajes y prompts en español, cortos, orientados a voz/chat.

## 5.1) Reglas de eficiencia y escalabilidad

Toda implementación DEBE ser eficiente y pensada para escalar. Reglas absolutas:

- **Prohibido barridos/polling periódicos** contra bases de datos o APIs externas. Usar triggers reactivos desde las operaciones CRUD que originan el cambio.
- **Notion mirror**: se dispara SOLO desde `create_task()` y `run_task()` / `patch_task()`. No existe change stream, ni reconciliación periódica, ni worker de barrido. La reconciliación solo corre una vez al arrancar como safety net para tasks huérfanos.
- **Prohibido duplicar work**: si una operación CRUD ya dispara un side effect (ej: mirror a Notion), ningún watcher/scheduler debe re-procesar el mismo evento.
- **1 API call por operación**: si un create necesita datos extra (ej: Agent, Runtime Status), incluirlos en el mismo POST, no en un PATCH separado inmediatamente después.
- **Sin semáforos artificiales ni paralelismo forzado**: si el diseño necesita throttling, es señal de que el approach está mal. Cada operación debe ser independiente y autocontenida.
- **Escalar a 100k+ registros**: cualquier query o proceso debe funcionar igual con 37 tickets que con 100,000. Prohibido `for task in all_tasks` sobre colecciones completas.

## 6) Pendientes estratégicos

- Streaming real incremental del subprocess (`stdout` -> SSE por chunks).
- Cola opcional de múltiples instrucciones por sesión en modo peer.
- Extender agentes no-dev sin romper el flujo de dev activo.

## 7) Cierre recomendado al terminar jornada

- Ejecutar `elimina todas las sesiones` y luego `cerrar sesion dev`.
- Dejar `selected_project` en el proyecto principal de trabajo (actualmente `jardal-openceo`).
- Confirmar que no haya sesiones abiertas antes de terminar.


- En `jardal-openceo`, provider codex debe usar: `codex exec --skip-git-repo-check --sandbox workspace-write`.
- En Docker, montar también `/home/develop/.codex:/home/ceouser/.codex` para reutilizar login Pro de Codex CLI.
- Si se usa host-mode, no se requieren mounts de credenciales: usa directamente `~/.codex` y `~/.claude` del usuario de la VM.
- OpenWebUI (en `jardal-internal-net`) debe usar base URL `http://172.20.0.1:18765/v1` + `OPENCEO_API_KEY`.
- Si hay timeout desde OpenWebUI al puerto `18765`, revisar firewall host (permitir `172.20.0.0/16 -> tcp/18765`).


# Equipo de Auditoría OpenCEO

## @arquitecto (Model: GLM-5)
- Rol: Ingeniero de software experto en Python, LangGraph y arquitecturas de estado.
- Directiva: Tu enfoque es puramente técnico. Analiza cómo se integran los tool registries y los create_react_agent.

## @analista (Model: Kimi K2.5)
- Rol: Especialista en flujos lógicos, reglas de negocio y consistencia de documentación.
- Directiva: Tu enfoque es buscar contradicciones lógicas entre el README y la nueva propuesta.