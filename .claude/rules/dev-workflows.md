---
paths:
  - "workflows/**"
  - "openceo-core/agents/dev/**"
  - "openceo-core/objectives/**"
---

# Dev workflows — reglas por zona

Solo para cambios en workflows, planner y Tier S.

- Workflows en `workflows/<agente>/<nombre>.yaml`; pasos `human_in_the_loop` pausan a `waiting` y reanudan por API sin reiniciar.
- El flujo `discover_workflow` no existe; flags legados no deciden nada.
- Planner único: salida `PlannerResult` validada (Pydantic + determinista); normativo en `openceo-core/objectives/routing_policy.md`.
- Tier S se activa con `"coding_tool": "tier_s"`; S se elige cuando varios modelos independientes den un resultado materialmente más correcto, sin cupo por porcentaje.
