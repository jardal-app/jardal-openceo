---
paths:
  - "openceo-core/horizon/**"
  - "openceo-react/**"
---

# Horizon — reglas por zona

Solo para cambios en el cockpit personal del Owner. Spec en `jardal-horizon/README.md`.

- Clasificador determinista antes del LLM: ninguna frase personal llega a tmux como instrucción dev.
- Privacidad en un solo sentido: empresa entra en solo lectura; lo personal nunca sale (sin espejo a Notion).
- Desconocido es `null`, nunca `0`; incertidumbre `unknown`.
- Sin polling ni barridos: triggers persistidos con `next_run_at`, un único timer reactivo.
- Modo degradado por construcción: capacidad, planificación y revisión sin LLM.
- Finanzas vía adapter hacia Jardal ERP; sin floats para dinero ni contabilidad propia.
