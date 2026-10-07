# OpenCEO

OpenCEO coordina agentes de IA, sesiones de desarrollo, colas y workflows. Es el plano de control de la ejecucion: recibe trabajo por chat, voz, dashboard o MCP, lo enruta y conserva su estado operativo. El backend combina FastAPI, LangGraph y MongoDB; la app de voz usa LiveKit y el dashboard usa React.

La direccion de arquitectura asigna el trabajo empresarial y la contabilidad a ERPNext, el estado de ejecucion a OpenCEO/MongoDB, el codigo y la configuracion a Git, y el conocimiento de ingenieria al vault de Obsidian. **Esa separacion describe el objetivo de integracion; no implica que el runtime actual ya este conectado a ERPNext.** Para saber que funciona hoy, consultar [STATUS.md](STATUS.md) y el codigo.

## Orientacion rapida

| Buscas | Lee |
| --- | --- |
| Estado actual, servicios y validaciones recientes | [STATUS.md](STATUS.md) |
| Tickets y trabajo pendiente | [ERPNext](https://erp.jardal.app/app/task) — `PENDINGS.md` es solo un puntero |
| Instrucciones para agentes y comandos de desarrollo | [CLAUDE.md](CLAUDE.md) |
| Dueno de cada dominio y direccion de integracion | [docs/OPENCEO_DIRECTION.md](docs/OPENCEO_DIRECTION.md) |
| Arquitectura extensa, protocolos y ejemplos del README anterior | [referencia completa](docs/ARCHITECTURE_REFERENCE_2026.md) |
| Configurar y arrancar el backend | [docs/SETUP.md](docs/SETUP.md) |
| Configurar proyectos y providers | `projects/*/project.yaml` (validado por `config_loader.py`; los proyectos existentes son la plantilla) |
| Finance y ERPNext | [decision](docs/decision-2026-09-19-sistema-financiero-erpnext.md), [contrato del adapter](docs/finance-erpnext-integration-contract.md) y [propuesta de instalacion](ERPNEXT.md) |
| Horizon y su contexto personal | [jardal-horizon/README.md](jardal-horizon/README.md) |
| Voz y app Android | [VOICE_GUIDE.md](VOICE_GUIDE.md), [openceo-voice/README.md](openceo-voice/README.md) y [Android](openceo-voice-android/README.md) |
| Guia visual del dashboard | [DESIGN.md](DESIGN.md) |
| Conocimiento tematico para futuras sesiones | [hub del vault](../jardal-knowledge/vault/projects/jardal/jardal-openceo/jardal-openceo.md) |

La [referencia completa](docs/ARCHITECTURE_REFERENCE_2026.md) conserva las 2.064 lineas del README anterior, mas una advertencia de lectura. Incluye decisiones, disenos futuros, ejemplos y detalles de implementacion: abrir solo la seccion pertinente. El codigo y `STATUS.md` prevalecen cuando describa un comportamiento o estado antiguo. El vault es una ayuda de navegacion en esta VM; los contratos detallados permanecen en `docs/` para no depender del vault.

## Mapa del sistema

- `openceo-core/main.py` expone HTTP, WebSocket, el endpoint compatible con OpenAI y los mounts MCP.
- `openceo-core/first_layer.py` interpreta el contexto conversacional de chat y voz; `router.py` y `graph.py` conectan los agentes ejecutables de forma explicita.
- `openceo-core/task_service.py` gestiona tareas de ejecucion; `workflow_service.py` propone, aprueba y ejecuta recetas YAML deterministas de `workflows/`.
- `openceo-core/dev_session_service.py` y `agents/dev/` gobiernan sesiones tmux, providers y revision Tier S. Los planes, briefs y feedback de esas sesiones son artefactos de ejecucion.
- `openceo-core/horizon/` implementa el contexto personal del CEO, clasificacion, planificacion y delegacion; `scheduler.py` y `rate_monitor.py` coordinan trabajo programado y cuotas.
- `openceo-core/mcp_server.py` publica herramientas seleccionadas; `openceo-core/tools/` contiene funciones compartidas y `agents/` los paquetes propios de cada agente.
- `openceo-core/db/` y MongoDB guardan estado de ejecucion. `projects/*/project.yaml` configura repositorios, providers, routing y presets; `CONTEXT.md` persiste por compatibilidad con sesiones existentes.
- `openceo-react/` es el dashboard; `openceo-voice/` y `openceo-voice-android/` son el puente de voz y el cliente movil.

La [referencia completa](docs/ARCHITECTURE_REFERENCE_2026.md) desarrolla discovery, Tier S, limites de cuota, MCP, colecciones Mongo, ciclo de tareas y estructura del proyecto. Este mapa solo indica donde empezar a leer y que modulo inspeccionar para una tarea concreta.

## Reglas que cruzan el proyecto

1. **Separar fuentes de verdad.** ERPNext es la autoridad empresarial prevista; MongoDB conserva ejecuciones y sesiones. No construir un segundo backlog, ledger, vault o grafo de codigo en OpenCEO. `project_key` sera el identificador de integracion y no reemplaza automaticamente `project` ni `task_id` actuales.
2. **Respetar el estado real.** El README y la referencia incluyen contratos objetivo. Verificar implementacion en codigo y estado en `STATUS.md` antes de afirmar que un adapter, tool o fase ya esta disponible.
3. **Ejecutar recetas conocidas de forma determinista.** YAML define pasos y mapeo de datos; discovery con LLM propone flujos nuevos y requiere aprobacion para cristalizarlos. La aprobacion de una receta no autoriza por si misma cada escritura empresarial posterior.
4. **Mantener identidad y aprobaciones.** Los reintentos conservan la misma identidad para evitar efectos duplicados. Una ejecucion tecnica completada no sustituye una aprobacion humana de negocio. Finance no ejecuta movimientos externos mientras `execute_money_movements=false`.
5. **Hacer el runtime reactivo.** Los cambios nacen de eventos, operaciones CRUD o timers con proximo vencimiento; no introducir barridos periodicos de Mongo o APIs. Los turnos dev responden enseguida y continuan en segundo plano.
6. **Proteger contexto y privacidad.** El contexto personal de Horizon no se refleja a Notion; desconocido (`null`) no es cero medido. Credenciales quedan fuera de Git y del vault. Los `CONTEXT.md` usados por el runtime no se eliminan por tener notas en Obsidian.
7. **Registrar herramientas con alcance.** Un agente nuevo necesita registro explicito en el runtime. Los tools locales viven con su agente, los compartidos en `openceo-core/tools/`, y MCP expone solo capacidades autorizadas. El codigo se consulta con busqueda/LSP; el vault guarda el motivo de las decisiones.

## Trabajo en este repositorio

Para instalar y arrancar, seguir [docs/SETUP.md](docs/SETUP.md) y las plantillas `.env.example`. Para validar un cambio, usar los tests y comandos del componente afectado; `./scripts/openceo-core-host.sh status` comprueba el servicio en modo host. `CLAUDE.md` documenta comandos adicionales para Python, Horizon y Android.

Al cambiar un contrato, actualizar el documento detallado que lo define y esta portada solo si cambia el mapa o una regla transversal. Los tickets y el trabajo pendiente viven en [ERPNext](https://erp.jardal.app/app/task); el avance operativo y los traspasos de sesión van a `STATUS.md`. Las conclusiones duraderas se enlazan desde el [hub del vault](../jardal-knowledge/vault/projects/jardal/jardal-openceo/jardal-openceo.md), sin copiar el codigo ni los documentos del repo.

Licencia: MIT.
