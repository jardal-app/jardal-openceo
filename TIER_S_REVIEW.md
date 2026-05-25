# Tier S refactor — observaciones de revisión

Resumen del estado: la reescritura compila y la estructura del flujo (Planner → Plan Reviewer → Builder → 3 Reviewers × 2 rondas) refleja correctamente el plan acordado. Sin embargo, hay **2 bugs que romperán en runtime** y varios issues secundarios (handling de errores inconsistente, código muerto, código zombie de la versión legacy). Corregir lo marcado como 🔴 es obligatorio antes de probar; el resto puede irse en commits de limpieza.

---

## 🔴 BUGS QUE ROMPEN EL FLUJO (hay que arreglar antes de ejecutar)

### B1. `consensus.py:232` — `close_session` con kwarg inválido
**Síntoma:** `TypeError: close_session() got an unexpected keyword argument 'session_key'` al cerrar un proyecto Tier S.

`tmux_runtime.close_session` está definido como:
```python
# openceo-core/agents/dev/tmux_runtime.py:609
def close_session(self, project: str, tmux_session_name: str | None = None) -> None:
```

La primera kwarg es **`project`** (en el contrato interno, ese parámetro recibe el `session_key`). Pero el código nuevo llama:
```python
# openceo-core/agents/dev/consensus.py:232 (close_project)
self._tmux.close_session(
    session_key=f"tier_s::{project}::{role}",   # ❌ debe ser project=
    tmux_session_name=f"dev-{safe_project}-{safe_role}",
)
```

**Fix:** cambiar `session_key=` por `project=`. Nota: `dev_session_service.py:437` ya lo hace bien con `project=f"tier_s::..."`; sigue ese patrón.

### B2. `consensus.py:409` — `_run_planner` lanza `RuntimeError` en vez de retornar `ConsensusRunResult(ok=False, ...)`
**Síntoma:** si el planner no firma `PLAN_READY_N`, el orchestrator NO devuelve un resultado estructurado — propaga una excepción cruda hacia `DevSessionManager._run_critical_consensus`. El usuario verá un mensaje opaco en lugar del summary user-friendly que todos los otros paths producen.

```python
# consensus.py:405-409
signal = self._read_signal(paths.status_path)
match = _PLAN_READY_RE.match(signal)
if match:
    return int(match.group(1))
raise RuntimeError(f"Planner did not produce PLAN_READY signal. ...")  # ❌
```

**Fix:** cambiar la firma de `_run_planner` para que retorne `int | None`, y en `execute()` hacer:
```python
n_plans = await self._run_planner(...)
if n_plans is None:
    return self._lead_failure(
        turn=...,  # capturar el turn dentro de _run_planner y propagarlo
        signal_path=paths.status_path,
        summary="Planner no confirmó PLAN_READY_N. Tier S no puede continuar.",
    )
```
O alternativamente envolver todo `execute()` en un try/except global que convierta cualquier excepción en `ConsensusRunResult(ok=False, error=str(exc))`.

---

## 🟡 INCONSISTENCIAS DE MANEJO DE ERRORES

### I1. `consensus.py:443-454` — `_run_plan_reviewer` no chequea la señal `PLAN_REVIEWED`
Hoy solo loggea warning si `turn.ok is False`. Si el `completion_check` falla por timeout pero `turn.ok` queda `True` (que es el comportamiento del fallback de timeout de `send_instruction`), no hay log ni warning al usuario.

**Recomendación:** después de `send_instruction`, leer la señal y, si no es `PLAN_REVIEWED`, emitir un `await progress("⚠️ Plan reviewer no firmó PLAN_REVIEWED — sigo con el plan original.")` para que el usuario lo sepa.

### I2. `consensus.py:635-651` — `_run_finalize` no chequea `TICKET_COMPLETE`
Mismo patrón: retorna `turn` sin validar la señal, y `execute()` retorna `ConsensusRunResult(ok=True, ...)` sin importar si el plan se marcó como done o `CONTEXT.md` se actualizó. Un finalize fallido se reporta como éxito.

**Recomendación:** validar la señal; si no aparece, retornar `ok=False` (o al menos `ok=True` con un `summary` que diga "plan no marcado como done, revisar manualmente").

### I3. `consensus.py:171-173` — `_run_builder_fix` solo loggea warning
El plan acordó "espera `FIX_DONE_p_r`" como gate. Hoy si el builder no firma, el loop continúa a la siguiente ronda con el código a medio fixear. Eso puede compounding-fallar en la ronda 2.

**Recomendación:** o bien tratar como `ok=False` y retornar; o bien notificar al usuario via `progress()` y registrar en el summary final.

### I4. `execute()` sin `try/except` global
Cualquier excepción no manejada (e.g. `ValueError: Unsupported Tier S provider` en `ensure_project_sessions._make`, el `RuntimeError` del bug B2, fallos de tmux, etc.) burbujea cruda. El contrato del método dice que retorna `ConsensusRunResult` — debería honrarlo siempre.

**Recomendación:** envolver el cuerpo de `execute()` en `try/except Exception as exc` y convertir a `ConsensusRunResult(ok=False, summary="...", error=str(exc))`.

---

## 🟡 CÓDIGO MUERTO / ZOMBIE (de la versión legacy)

### Z1. `consensus.py:30-32` — regexes definidos pero nunca usados
```python
_BUILD_DONE_RE = re.compile(r"^BUILD_DONE_(\d+)$")
_FIX_DONE_RE = re.compile(r"^FIX_DONE_(\d+)_(\d+)$")
_REVIEW_DONE_RE = re.compile(r"^REVIEW_DONE_(\d+)_(\d+)_(\S+)$")
```
El código usa comparación de string directa (`== f"BUILD_DONE_{p}"`) y `endswith()` en `_review_file_done`. **Borrar los tres regexes.**

### Z2. `consensus.py:5,8,9` — imports no usados
- `import os` (sin uso)
- `import time` (sin uso)
- `from dataclasses import dataclass, field` → `field` no se usa, dejar solo `dataclass`

### Z3. `dev_session_service.py:1222` y `main.py:3378` — checks de `legacy_mode == "critical_consensus"` sin ningún writer
El código que escribía `session_doc.metadata["execution_mode_legacy"] = "critical_consensus"` (dev_session_service.py:139 viejo) fue removido en este PR. Los reads de `execution_mode_legacy` quedaron huérfanos. **Borrarlos** (eran solo para migración de sesiones viejas; si las hay en MongoDB serán nuevas a partir de este deploy).

### Z4. `dev_session_service.py:1228-1235` — `_resolve_provider` lee key inexistente
```python
tier_cfg = (config.get("dev", {}).get("tier_s", {}) or {})
lead = str(tier_cfg.get("lead", "claude")).strip().lower()
```
La key `tier_s.lead` ya no existe en el nuevo shape. Funcionalmente cae al default `"claude"`, pero es semánticamente incorrecto. **Reemplazar por:**
```python
roles = tier_cfg.get("roles", {}) or {}
planner_cfg = roles.get("planner", {}) or {}
lead = str(planner_cfg.get("provider", "claude")).strip().lower()
```

### Z5. `config_loader.py:106` — flag `enabled` vestigial
El nuevo `is_enabled()` solo chequea `coding_tool == "tier_s"`. El `tier_s.enabled` que sigue en el default ya no se lee en ningún lado. Eliminar `"enabled": False` del default para no inducir a configurar algo que se ignora.

---

## 🟡 OBSERVACIONES MENORES

### M1. `consensus.py:411-431` — `_planner_prompt` apunta al `paths.plan_path` con ruta absoluta
El prompt pasa rutas como `.tier-s/CURRENT_PLAN.md` (relativas al repo si el cwd del planner es el repo), pero `paths.plan_path` es un `Path` absoluto si `repo_path` es absoluto. Esto le dice al planner que escriba en `/home/.../jardal-trading/.tier-s/CURRENT_PLAN.md`. Funciona, pero el prompt se ve crudo. Considera convertir a `paths.plan_path.relative_to(repo_path)` antes de meterlo al prompt.

### M2. `consensus.py:570-590` — `_code_reviewer_prompt` no lista los archivos que debe revisar
El prompt dice "Open and review the actual code changes with absolute rigor", pero el reviewer no sabe qué archivos abrir excepto vía el `BUILD_SUMMARY_{p}.md`. Esto depende de que el builder liste bien "Files modified" en el summary. Considera añadir al prompt: "Use the 'Files modified' section of BUILD_SUMMARY as the file list to review."

### M3. `projects/jardal-trading/project.yaml:24` — modelo `gpt-5.5`
El default actual de `CODEX_DEFAULT_ARGS` en `settings.py:34` es `gpt-5.4`. El nuevo project.yaml usa `gpt-5.5`. Confirmar que el id de modelo `gpt-5.5` existe en la versión actual de codex CLI; si no, el `codexf` arranca y muere instantáneamente.

### M4. `consensus.py:539-541` — borrado de stale review files
Solo borra los del round/sub-plan actual; los de rondas/sub-planes previos quedan en `.tier-s/`. Aceptable (es historial útil), pero considera mencionarlo en el README o agregar `.tier-s/` al `.gitignore` del repo target para no committearlos por accidente.

### M5. `_run_review_round` paralelo — sesiones distintas, OK ✓
Confirmé que `clauded` usa la sesión `planner`, `codexf` usa `plan-reviewer`, `gemini` usa `gemini-reviewer`. No hay race condition tmux. ✓ Sin acción necesaria.

### M6. Regresión vs. legacy: `prime_reviewers` ya no se usa
El legacy enviaba un primer prompt a cada reviewer ("READY_FOR_REVIEW") antes de la primera revisión, para que el modelo asumiera el rol de reviewer. Esto se eliminó. Probablemente OK porque los prompts de review son auto-contenidos, pero si en pruebas notas que los reviewers piden contexto extra en la primera ronda, considera reintroducir un prime prompt.

---

## ✅ LO QUE QUEDÓ BIEN

- Estructura de 4 sesiones tmux con reutilización de planner/plan-reviewer como reviewers de código ✓
- Override de comando por rol en `_resolve_command` (role-first, fallback a provider) ✓ — esto permite `clauded` vs `claudep` ambos como `provider: claude` sin colisión
- Renombre `consensus::` → `tier_s::` en session keys y `_CONSENSUS_TMUX_SESSION_RE` actualizado consistentemente ✓
- Eliminación completa del path OpenCode-team (6 reviewers + compiler) y todos sus imports ✓
- Validación de `n_plans` entre 1 y `max_plans` después de re-leer el plan (por si codexf cambió `TOTAL_PLANS:`) ✓
- `config_loader.py` con nuevo shape role-based y merge legacy eliminado ✓
- `state.py`, `session.py`, `main.py`, `rate_monitor.py` limpiados de referencias a `CRITICAL_CONSENSUS` / `"critical_consensus"` ✓
- `projects/jardal-trading/project.yaml` con los 4 wrappers correctos: `clauded`, `codexf`, `claudep`, `gemini -y` ✓
- `_send_participant_instruction` simplificado (eliminada la rama OpenCode) ✓

---

## Acción sugerida

1. Arregla **B1** y **B2** ya (rompen en runtime real).
2. Resuelve **I1–I4** para que el orchestrator nunca devuelva una excepción cruda y siempre informe al usuario con `progress()`/`summary`.
3. Limpia **Z1–Z5** en un commit aparte si quieres mantener el PR atómico.
4. **M1–M6** son mejoras de calidad, no bloqueantes.

Para verificar después del fix: seguir el plan de verificación en `/home/develop/.claude/plans/eager-soaring-lake.md` (smoke E2E con un ticket trivial en `jardal-openceo`).
