#!/usr/bin/env python3
"""Migra el backlog de PENDINGS.md a ERPNext como tickets de negocio.

Auditado el 2026-09-24: varios items figuraban como pendientes pero ya estaban
implementados, y dos estaban absorbidos por otro ticket. Crearlos tal cual habria
metido tickets falsos en el sistema de negocio. El estado de cada uno sale de
verificar el CODIGO, no del texto del markdown.

Idempotente: cada ticket lleva `openceo_ref = "pendings:TICKET-0NN"` con indice unico,
asi que re-ejecutar el script no duplica nada. Verificado con dos corridas.

Los tickets se crean SOLO en ERPNext, no como tareas de Mongo: son backlog de negocio,
no ejecucion. Crearlos en Mongo inundaria la cola de ejecucion con trabajo que todavia
nadie decidio empezar.

Uso:
    ./.venv/bin/python scripts/migrate_pendings_to_erpnext.py --dry-run
    ./.venv/bin/python scripts/migrate_pendings_to_erpnext.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "openceo-core"))

from settings import get_settings  # noqa: E402
from tools.jardal_erp_tickets import JardalERPTicketAdapter  # noqa: E402

PROJECT = "jardal-openceo"

# (id, titulo, estado, tipo, verificacion / descripcion)
# Estado verificado contra el codigo el 2026-09-24. `Completed` y `Cancelled` NO son
# basura: son la razon por la que este backlog dejo de mentir.
ITEMS: list[tuple[str, str, str, str, str]] = [
    ("TICKET-001", "MCP Internal: eliminar notion_create_ticket", "Completed", "Deuda técnica",
     "VERIFICADO: `notion_create_ticket` ya no existe en openceo-core/mcp_server.py "
     "(grep = 0 coincidencias). El MCP interno expone solo DNS y los tres tools "
     "openceo_create_*. Criterio de aceptacion cumplido."),
    ("TICKET-002", "MCP External: migrar a openceo-core", "Open", "Deuda técnica",
     "Componente: apps-sdk-mcp-test -> openceo-core. El directorio apps-sdk-mcp-test/ "
     "SIGUE EXISTIENDO (verificado). Incluye migrar el contenedor Docker y la URL que "
     "expone. Renombrar `execute_workflow` (no siempre ejecuta workflows)."),
    ("TICKET-003", "Secretary: tools de notas rapidas", "Completed", "Feature",
     "VERIFICADO: `secretary.save_note` y `secretary.query_notes` estan registrados en "
     "WorkflowToolRegistry (workflow_service.py:120-121, implementacion en :512)."),
    ("TICKET-004", "Finance: administrador de fortuna", "Open", "Feature",
     "Arqueo de cajas por SaaS contra BTCPay/Lightning/BlueWallet, deteccion de fugas, "
     "tipos de movimiento Lightning (automatico/semiautomatico/manual), centralizacion "
     "en Binance, y separacion de ingresos brutos para el 10% del patrocinador. "
     "Depende del adapter de Finance (TICKET-025/027)."),
    ("TICKET-005", "TOOL_REGISTRY + check_registry", "Completed", "Feature",
     "VERIFICADO: `core.check_registry` registrado en workflow_service.py:108, "
     "implementado en :287. `core.query_workflow_runs` en :110."),
    ("TICKET-006", "YAML Workflow Executor determinista", "Completed", "Feature",
     "VERIFICADO: el executor parsea YAML, resuelve variables y ejecuta steps contra "
     "WorkflowToolRegistry sin LLM. `_execute_steps` + `_resolve_value` existen."),
    ("TICKET-007", "YAML Schema: loops y composicion", "Completed", "Feature",
     "VERIFICADO: `ref`, `when` y `loop` con `python_eval` estan soportados. "
     "`llm_eval` NO se habilita a proposito: workflow_service.py:1507 lanza ValueError "
     "explicando que requiere un evaluador dedicado fuera del modo determinista. "
     "Eso es una decision, no un pendiente."),
    ("TICKET-008", "Workflow Runs: tracking en MongoDB", "Completed", "Feature",
     "VERIFICADO: coleccion `workflow_runs` con indices (db/mongo.py:177-179) y tool "
     "`core.query_workflow_runs` registrado."),
    ("TICKET-009", "Discovery Engine (Tier 2)", "Working", "Feature",
     "PARCIAL: el discovery nivel 1 (CEO) existe y genera propuesta YAML con aprobacion "
     "humana (`discovery_runs`, endpoints /api/discovery-runs). FALTA el ReAct "
     "peer-to-peer completo con claim cross-agent."),
    ("TICKET-010", "Blocked Discovery: estado persistente", "Working", "Feature",
     "PARCIAL: `discovery_runs` persiste con `BLOCKED_TOOL` y el flujo de aprobacion "
     "existe. FALTA: notificar al admin con la especificacion del tool faltante, crear "
     "el ticket al Dev agent al aprobar, y reanudar desde `blocked_at`."),
    ("TICKET-011", "Peer-to-Peer Communication (team_message + team_claim)", "Open", "Feature",
     "Transporte peer-to-peer entre agentes: mesa compartida JSONL por discovery group, "
     "session injection solo a candidatos del grupo (no broadcast), claim atomico por "
     "rename, release y timeout a HITL. Ver el detalle completo en el historial de git "
     "de PENDINGS.md (commit previo a la migracion)."),
    ("TICKET-012", "CEO Discovery: fragmentacion + tiers 0-1.5", "Open", "Feature",
     "Tier 0 (keywords), Tier 1 (check_registry), Tier 1.5 (union de workflows via ref), "
     "analisis de dependencias para fragmentacion paralela vs secuencial."),
    ("TICKET-013", "create_ticket global + parent task tracking", "Open", "Feature",
     "Extender create_ticket con `assigned_to` para cualquier agente y `parent_task` "
     "opcional; task padre en Mongo con subtasks/workflow_runs/discovery_runs como hijos. "
     "NOTA 2026-09-24: la jerarquia de NEGOCIO ahora vive en ERPNext (`Task.parent_task`), "
     "asi que este ticket debe re-leerse contra `docs/ARCHITECTURE_REFERENCE_2026.md` "
     "§Parent Task Tracking antes de implementarse."),
    ("TICKET-014", "Proactive CEO: objectives + autonomous loop", "Cancelled", "Feature",
     "ABSORBIDO por TICKET-026. Crear una coleccion `objectives` paralela a "
     "`horizon_objectives` viola la decision D-006 (un solo motor de objetivos). "
     "El motor existe y sirve a los alcances personal y de empresa. No implementar."),
    ("TICKET-015", "Internal Scheduler: cron runner", "Cancelled", "Feature",
     "ABSORBIDO por TICKET-026. El cron que 'revisa cada minuto' es un barrido periodico, "
     "prohibido por AGENTS.md §5.1. La implementacion correcta ya existe: disparadores "
     "persistidos con `next_run_at` y un solo timer que despierta en el proximo "
     "vencimiento. No implementar."),
    ("TICKET-016", "Refactor estructura de carpetas de agentes", "Open", "Deuda técnica",
     "Migrar de `tools.py` unico por agente a `tools/` por dominio (max 200-300 lineas "
     "por archivo) y agregar `workflows/` por agente. Mantener backward compatibility."),
    ("TICKET-017", "Tier 0 estricto: eliminar patterns amplios", "Open", "Deuda técnica",
     "PARCIAL. `router.py` ya no usa `_DEV_INSTRUCTION_PATTERNS` (0 referencias), pero "
     "**main.py las conserva**: definicion en main.py:2801 y uso en main.py:3053. "
     "Verificado el 2026-09-24. El criterio de aceptacion ('cero falsos positivos por "
     "verbos genericos') no se cumple mientras sigan ahi."),
    ("TICKET-018", "Integrar Workflows YAML con Phase Workflows", "Completed", "Feature",
     "VERIFICADO: el validador YA NO rechaza `human_in_the_loop` "
     "(workflow_service.py:1043 y :1339), `WorkflowRunStatus.BLOCKED_HUMAN` existe, y el "
     "prompt de discovery (:1252) instruye al LLM a usarlo con las permissions "
     "existentes. El texto original de este ticket decia que el validador lo rechazaba: "
     "quedo obsoleto."),
    ("TICKET-019", "Auto-discovery automatico sin flag obligatorio", "Open", "Feature",
     "VERIFICADO: `_should_start_discovery` (workflow_service.py:1395) SIGUE devolviendo "
     "False sin `discover_workflow`, `workflow_discovery`, `use_workflow` o un workflow "
     "ref explicito. El discovery sigue siendo inalcanzable salvo que alguien sepa setear "
     "el flag a mano."),
    ("TICKET-020", "DeepSeek Direct First Layer Gateway (fase 2)", "Working", "Feature",
     "Fase 1 operativa y validada en vivo. FASE 2 PENDIENTE: prompt caching en DeepSeek, "
     "streaming incremental por SSE, descubrimiento dinamico de proyectos sin reinicio, "
     "y compactador de listas de mas de 50 tickets abiertos."),
    ("TICKET-021", "Mobile Walking & Voice Ticket Focus", "Working", "Feature",
     "OPERATIVO: foco persistente (set/get_active_ticket), comandos naturales de cambio "
     "de foco, preguntas con pronombres contextuales, endpoints /api/tasks/focus. "
     "PENDIENTE: consulta por voz de criterios de aceptacion, acciones de revision por "
     "voz, reejecucion con seleccion de provider, y respuestas de audio de 20-30 palabras."),
    ("TICKET-022", "Global Host Tmux Management desde el movil", "Working", "Feature",
     "OPERATIVO: list_all_host_sessions, capture_pane_by_name, send_keys_by_name, "
     "create_generic_session, kill_by_name y los endpoints /api/tmux/sessions. "
     "PENDIENTE: watcher de salud de sesiones criticas y presets de comandos para movil."),
    ("TICKET-023", "Admin Panel (openceo-react) Mission Control", "Open", "Feature",
     "Tres tareas: (1) tab de sesiones tmux con preview del terminal en vivo, "
     "(2) widget de foco activo en el header, (3) responsive mobile de TicketList y "
     "TicketDetail por debajo de 420px."),
    ("TICKET-024", "Mobile App: modo unificado y badge de foco", "Open", "Feature",
     "Tres tareas: (1) modelo unificado (`ceo`) por defecto en TokenExt.kt, (2) chip con "
     "el ticket enfocado y el proyecto activo en VoiceAssistantScreen.kt, (3) reconexion "
     "WebRTC resiliente al cambiar entre Wi-Fi y 4G/5G."),
    ("TICKET-025", "Sistema financiero: ERPNext como registro canonico", "Working", "Feature",
     "FASE 1 COMPLETADA por el repo hermano jardal-erpnext (stack desplegado, "
     "OpenCEO Scope, openceo_ref, usuario API). FASE 3 BLOQUEADA por TICKET-027. "
     "Fases 4-6 pendientes (BTCPay->ERPNext, objetivos, empresas)."),
    ("TICKET-026", "Horizon: fases 3 a 5", "Working", "Feature",
     "HZ-001 a HZ-004 implementadas y commiteadas. PENDIENTE: HZ-005 (frontera financiera "
     "con ERPNext, adapter de solo lectura) y el piloto de delegacion con 3 tickets "
     "reales. El canal de entrega push sigue sin decidir."),
    ("TICKET-027", "Mapa de funciones ERPNext -> agente Finance", "Open", "Feature",
     "ENTREGABLE DEL DUENO. Bloquea la fase 3 de TICKET-025. No debe iniciarse por "
     "iniciativa del agente: lo entrega el dueno cuando el sistema financiero este "
     "definido a su criterio. Debe contener operacion, firma, efecto en ERPNext, "
     "idempotencia, naturaleza y alcance autorizado."),
]


async def migrate(*, dry_run: bool) -> int:
    settings = get_settings()
    adapter = JardalERPTicketAdapter(settings)

    if not adapter.is_enabled:
        print("ERPNEXT_TICKETS_ENABLED=false. Activalo antes de migrar.")
        return 1

    created = 0
    skipped = 0
    failed: list[str] = []

    for ticket_id, title, status, task_type, description in ITEMS:
        ref = f"pendings:{ticket_id}"
        body = f"{description}\n\n---\nOrigen: PENDINGS.md {ticket_id} (migrado 2026-09-24).\nEstado auditado contra el codigo, no contra el markdown."

        existing = await adapter.find_by_ref(ref)
        if existing is not None:
            skipped += 1
            print(f"  = {ticket_id}: ya existe como {existing.get('name')}")
            continue

        if dry_run:
            print(f"  + {ticket_id}: crearia [{status}] {title}")
            created += 1
            continue

        try:
            result = await adapter.create_ticket(
                ref=ref,
                title=title,
                description=body,
                project_key=PROJECT,
                ticket_type=task_type,
            )
            name = (result.get("task") or {}).get("name")
            # El estado se fija despues: create_ticket nace en Open por diseno.
            if status != "Open":
                await adapter.client.update_doc("Task", name, {"status": status})
            created += 1
            print(f"  + {ticket_id}: {name} [{status}]")
        except Exception as exc:  # noqa: BLE001 - el script reporta y sigue
            failed.append(ticket_id)
            print(f"  ! {ticket_id}: {type(exc).__name__}: {exc}")

    await adapter.aclose()
    print(f"\ncreados={created} ya_existian={skipped} fallidos={len(failed)}")
    if failed:
        print("fallidos:", ", ".join(failed))
    return 0 if not failed else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="no escribe nada")
    args = parser.parse_args()
    return asyncio.run(migrate(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
