# Agent Scope

## Implementado
- `ceo` (routing básico + deriva gasto ocasional por voz a `finance`)
- `dev` (sesiones productivas con `codex/claude/glm`)
- `secretary` (envío SMTP directo)
- `finance` (workflow determinista `Pago Elfec House` / `Pago Luz Casa`, dry-run hasta tarjeta; registro ocasional de gastos por voz a ERPNext vía `finance.register_expense`, TASK-2026-00032)

## Fuera de alcance en esta iteración
- `marketing`
- `sales`
- `media`

El foco actual es Dev Agent + Voice Peer Programming.
