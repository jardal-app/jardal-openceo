# Agent Scope

## Implementado
- `ceo` (routing básico + deriva gasto ocasional por voz a `finance`)
- `dev` (sesiones productivas con `codex/claude/glm`)
- `secretary` (envío SMTP directo)
- `finance` (workflow determinista `Pago Elfec House` / `Pago Luz Casa`, dry-run hasta tarjeta; escritura ocasional por voz a ERPNext vía `finance.register_expense` (TASK-2026-00032) y `finance.register_income` (TASK-2026-00053); lectura de saldo por Scope (`finance.get_scope_summary`, TASK-2026-00044) y regla del 10% (`finance.get_contribution_10_status`, TASK-2026-00054), ambas solo lectura; en moneda distinta a la de la Company se exige la tasa real o no se contabiliza)

## Fuera de alcance en esta iteración
- `marketing`
- `sales`
- `media`

El foco actual es Dev Agent + Voice Peer Programming.
