# CLAUDE.md - OpenCEO Developer & Agent Guide

Guía y contrato de arquitectura para agentes y desarrolladores de OpenCEO.

**Vault de este proyecto**: `projects/jardal/jardal-openceo` dentro de
`/home/develop/development/Jardal/jardal-knowledge/vault/` (usar el skill `llm-vault-links`).
Trabajo temporal de tickets: `docs/work/TASK-2026-000NN-tema.md`.

Operativa del repo (F7a): `AGENTS.md` — los CLI del roster lo leen directo; las sesiones del Owner leen este archivo, que lo importa en §8.

Roadmap vigente: épico **TASK-2026-00248**; TASK-2026-00245 y TASK-2026-00187 cerrados; pilotos reales separados en **TASK-2026-00233**. Las sesiones del Owner trabajan por orden directa (§10).

---

## 1. Índice de prácticas

Cómo se trabaja en este proyecto (vault, una nota por área; cada regla con fuente, autoridad y fecha):

- Autonomía y escalado (`practices/autonomia-y-escalado.md`): avanzar solo; el Owner solo donde hace falta un humano.
- Ciclo automático y revisión (`practices/ciclo-automatico-y-revision.md`): bucles sin tokens ni `Pending Review`; revisores del bucle con quórum 1 (distinto del quórum en 2 rondas de Tier S, §5).
- Comunicación y atribución (`practices/comunicacion-y-atribucion.md`): «orden del Owner» solo para sus palabras; traspaso en `STATUS.md`.
- Validación (`practices/validacion.md`): tests con el venv de la raíz; LLM solo con modelo gratuito; confirmar con mutaciones.

Hub: nota del proyecto en el vault; detalle bajo demanda, nunca el vault entero.

---

## 2. Arquitectura y Stack

- **Orquestador**: LangGraph (Python) en `openceo-core/`; API FastAPI `/v1/chat/completions` (host `18765`, docker `8000`)
- **Mecánico** (TASK-2026-00510): `openceo-core/mechanic/`. Lo imprevisto (espera repetida, `needs_owner`, plazo vencido, bucle frenado) despierta un run musej de solo lectura que devuelve acciones de un vocabulario cerrado; determinista solo para invariantes (ARCHITECTURE_REFERENCE §Engine mechanic)
- **Primera capa**: `openceo-core/first_layer.py` (DeepSeek Flash): comprende contexto, responde con personalidad de colega en primera persona plural, cuenta tickets abiertos, guía a perdidos y corre presets (`projects/<proyecto>/project.yaml` o Mongo `project_presets`, opcionalmente en tmux)
- **Datos**: MongoDB (`tasks`, `dev_sessions`, `user_context`, `project_presets`, `task_reviews`, `workflow_runs`); negocio en Jardal ERP (§6)
- **Voz y móvil**: LiveKit + Silero VAD + Groq Whisper + OpenAI TTS (`openceo-voice/`); Android Compose + LiveKit (`openceo-voice-android/`, build con `./gradlew assembleDebug`)
- **Dashboard**: React 19 + Vite 6 + Tailwind v4 (`openceo-react/`)
- **Coding providers**: `musej`/`muse` (muse-spark), `codexf` (`codexj` inactivo desde 2026-11-04), `claudej`, `dsh`, `claudep`, `opencode`, Gemini con `agyd`/`agyj`/`agy2` (roster en `openceo-core/config_loader.py`)

---

## 3. Comandos

```bash
./.venv/bin/python -m pytest tests/ -q   # tests, siempre con el venv de la raíz
scripts/openceo-core-host.sh restart     # rearranque canónico; nunca `python main.py` directo
scripts/openceo-core-host.sh status      # verifica instancia única + dueño del puerto
```

`restart` solo despliega desde un commit limpio: escribe el SHA en
`.runtime/RELEASE` (`{sha, dirty}`) y aborta con cambios rastreados; el
override explícito `OPENCEO_ALLOW_DIRTY=1` arranca dejando `dirty=true`
registrado. El core expone esa identidad en `/health` (`release`) y en el
log de arranque (TASK-2026-00367).

**Instancia única (obligatorio)**: solo un openceo-core por host. El lifespan tiene efectos globales y uvicorn lo ejecuta antes de bindear: una segunda instancia corrompe el estado y muere con `Errno 98`. `main.py` toma un flock en `.runtime/openceo-core-host.lock` y muere con exit 3 si está tomado. No usar el script y la unidad systemd a la vez: `restart`/`status` delegan en `systemctl --user` cuando la unidad está habilitada o activa, y siguen por la vía del script si no.

**systemd (TASK-2026-00370)**: unidad de usuario `openceo-core-host.service` (corre como `develop` con linger; `ExecStart=.../openceo-core-host.sh foreground`). `Restart=on-failure` con `RestartSec=10`, tope `StartLimitBurst=5` en `StartLimitIntervalSec=600`, `RestartPreventExitStatus=3` y `OnFailure=openceo-alert.service`. `ExecStopPost=record-exit.sh` escribe `.runtime/last-exit.json`; ante un fallo (incluido el agotamiento de reinicios) `openceo-alert.sh` avisa sin depender del core (spool `.runtime/alerts.log`, syslog, `OPENCEO_ALERT_WEBHOOK` opcional). Migración en `deploy/systemd/README.md` (`scripts/openceo-systemd-migrate.sh install|uninstall|status`).

---

## 4. Reglas de sesión

1. **Sin barridos ni polling**: prohibido el polling contra Mongo o APIs en loops; todo cambio es reactivo por eventos CRUD. Única excepción: el latido del mecánico (una consulta indexada cada `heartbeat_minutes`, 0 tokens salvo hallazgo).
2. **Respuesta inmediata en dev**: turnos de código en background, HTTP/SSE instantáneo; 1 call de API por operación.
3. **`musej` es el `muse` de dev por defecto** (`--yolo --disable-sandbox --disable-approval --approval-mode never --trust-workspace`, van los últimos y no se pueden reescribir encima); **`muse` pelado es sesión aislada** (sandbox ON, para revisores/auditorías).
4. **Memoria de motores**: no se usa; la automática de Claude Code está apagada en este repo (`.claude/settings.json`) y en las ejecuciones que lanza OpenCEO. Lo personal va a `CLAUDE.local.md` (sin versionar), nunca al vault.
5. **Credenciales y paths**: host usa `~/.codex`, `~/.claude`, `/home/develop/.local/bin/agyd`; `gemini` corre con `agyd`.
6. **Sin parches por caso**: un atasco o fallo de ejecución nuevo no recibe su `if`; lo atiende el mecánico. Solo se toca código para una clase de problema que el mecánico no puede resolver (TASK-2026-00510, tras la orden del Owner del 2026-11-05: «reestructura rapidamente para que sea proactivo»).

Reglas por zona del código: `.claude/rules/` (frontmatter `paths`); procedimientos repetibles: skills en git; decisiones de arquitectura: `docs/ARCHITECTURE_REFERENCE_2026.md`.

---

## 5. Workflows, planner y Tier S

Workflows deterministas (0 tokens) en `workflows/<agente>/<nombre>.yaml`; pasos `human_in_the_loop` pausan a `waiting` y reanudan por API. Planner único global: salida `PlannerResult` validada; normativo en `openceo-core/objectives/routing_policy.md`. Dev Tier S (`"coding_tool": "tier_s"`): planner, plan-reviewer no bloqueante, builder/closer one-shot, árbitro y revisores con quórum en 2 rondas. Detalle por zona en `.claude/rules/dev-workflows.md`.

---

## 6. Tickets: Jardal ERP es el registro de negocio

El trabajo empresarial pertenece a Jardal ERP; OpenCEO conserva la **ejecución** (dirección en `docs/OPENCEO_DIRECTION.md`). `TaskService.create_task()` crea el ticket de negocio antes de persistir en Mongo (ramas: éxito, `jardal_erp_pending` por timeout sin `autorun`, fallo duro 4xx sin persistir); un webhook propaga el inverso. `completed` es condicional (con `qa_review_required` pendiente empuja `Pending Review`); `openceo_ref` es la clave de idempotencia; `Working` solo con ejecución real (`autorun` explícito; legacy sin el campo equivale a `True`). Detalle operativo por zona en `.claude/rules/tickets.md`.

---

## 7. Horizon (cockpit personal del Owner)

Horizon no es un agente: es el contexto personal/estratégico del CEO (misión → objetivos → hitos → acción → resultado). Spec en `jardal-horizon/README.md`, código en `openceo-core/horizon/`. Reglas críticas: clasificador determinista antes del LLM, privacidad en un solo sentido (personal nunca sale), desconocido es `null` nunca `0`, sin polling, degradado sin LLM. Reglas por zona en `.claude/rules/horizon.md`.

---

## 8. Contrato operativo raíz (Tier S F7a)

@AGENTS.md

Los CLI del roster leen `AGENTS.md` directamente. El resto de este archivo es el manual de las sesiones del Owner.

---

## 9. Cierre de sesión (regla del Owner, 2026-09-28)

Una sesión no termina con el código listo: termina con el ticket cerrado en su ciclo completo. En orden:

1. **Merge a `main` + push** (`git checkout main && git merge --no-ff <rama> && git push origin main`).
Orden permanente del Owner: en sesiones del Owner, el push fast-forward al upstream con commits ordenados por el Owner está pre-autorizado; no pedir palabra por push. No aplica a ejecutores Tier S (solo el orquestador commitea/mergea).
2. **Servicio reiniciado** (`scripts/openceo-core-host.sh restart` + `status` + smoke).
3. **Ticket en su estado final** (`Completed` con `evidence`; `Pending Review` solo con `human_action` que únicamente el Owner puede dar; `Working` con `reason` si sigue abierto; cancelar trabajo del Owner exige citar su orden). Borrar la rama tras el merge.
4. **Docs temporales eliminados** (`docs/work/TASK-2026-000NN-tema.md` tras el checklist; informar en una línea).
5. **Registro durable**: `STATUS.md`, ticket y vault (solo hechos reutilizables, con el número de ticket).

---

## 10. Palabras clave retiradas (`sigue`/`valida`/`audita`)

Retiradas por orden del Owner (2026-10-17): sin receta que las atienda; las sesiones trabajan por orden directa y el roadmap vive en Jardal ERP.
