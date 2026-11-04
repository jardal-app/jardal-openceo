"""Run-environment contract for dev builders (TASK-2026-00478).

Builders run inside the agent sandbox by default, which has no Docker
(daemon and socket are hidden by design), while the orchestrator -- not the
builder -- runs the project gates. A builder that does not know this reads a
check it cannot run as an external blocker and closes ``bloqueado``. A
project with ``dev.isolation: host`` (TASK-2026-00481) runs its builder with
the host toolchain instead, so the text states that fact rather than the
sandbox one.
These texts state the facts, and a builder that still closes ``bloqueado``
with work in the tree gets one more turn to confirm or retract.

Dependency-free (stdlib only): the simple tiers and the Tier S cycle both
import it, and the Tier S package imports its own cycle on load.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "blocked_continuation_due",
    "blocked_continuation_prompt",
    "builder_environment_prompt",
    "kept_work_note",
]

_SANDBOX_FACTS = (
    "Este sandbox no tiene Docker (ni daemon ni socket, por diseño) ni las "
    "credenciales del host: las suites que levantan contenedores "
    "(Testcontainers, PostgreSQL) o necesitan esos secretos no pueden "
    "ejecutarse aquí."
)

_HOST_FACTS = (
    "Corres en el host con el usuario del orquestador: tienes el toolchain "
    "del proyecto, Docker y los stores/cachés del host. El orquestador "
    "ejecuta los gates al final; no repitas la suite global si el ticket "
    "no lo pide."
)

_MOTIVO_CHARS = 600
_NECESIDAD_CHARS = 400


def _gate_lines(gates: Sequence[Any], oracle_cmd: str | None) -> list[str]:
    lines: list[str] = []
    for gate in gates or ():
        flag = "" if getattr(gate, "required", True) else " (opcional)"
        lines.append(f"  - {gate.name}{flag}: `{gate.cmd}`")
    oracle = str(oracle_cmd or "").strip()
    if oracle:
        lines.append(f"  - oráculo congelado del ticket, si declara uno: `{oracle}`")
    return lines


def _gates_intro(gates: Sequence[Any], oracle_cmd: str | None) -> list[str]:
    lines = _gate_lines(gates, oracle_cmd)
    if not lines:
        return ["- Al terminar, el orquestador ejecuta en el host los gates "
                "del proyecto y decide el cierre."]
    return ["- Al terminar, el orquestador ejecuta en el host estos gates y "
            "decide el cierre:", *lines]


def builder_environment_prompt(
    gates: Sequence[Any], oracle_cmd: str | None = None,
    *, sandboxed: bool = True,
) -> str:
    """What the builder can run, what the orchestrator runs for it."""
    facts = _SANDBOX_FACTS if sandboxed else _HOST_FACTS
    limit = ("- Una verificación que este sandbox no puede ejecutar no es un "
             "impedimento externo: cierra con resultado=hecho y anota en "
             "evidencia qué ejecutaste, qué no pudiste ejecutar y por qué. "
             "Reserva bloqueado para lo que te impide entregar lo pedido."
             if sandboxed else
             "- Una verificación que el host no pueda ejecutar no es un "
             "impedimento: cierra con resultado=hecho y anota en evidencia "
             "qué ejecutaste y qué quedó pendiente. Reserva bloqueado para "
             "lo que te impide entregar lo pedido.")
    lines = [
        "ENTORNO Y VERIFICACIÓN (el orquestador corre los gates, no tú)",
        f"- {facts}" + (" No las intentes." if sandboxed else ""),
        *_gates_intro(gates, oracle_cmd),
        "- No repliques la suite global: valida lo que cambiaste con las "
        "comprobaciones y los tests dirigidos que sí corren aquí.",
        limit,
    ]
    return "\n".join(lines)


def blocked_continuation_due(
    verdict: Mapping[str, Any] | None, has_candidate: bool,
) -> bool:
    """A bloqueado with a deliverable in the tree earns one more turn.

    A scheduled wait (``resume_at``) keeps its own checkpoint path, and a
    ``no_aplica`` says the work is not for this role: neither is a
    verification the builder can retract.
    """
    if not has_candidate or not isinstance(verdict, Mapping):
        return False
    return (str(verdict.get("resultado") or "") == "bloqueado"
            and not verdict.get("resume_at"))


def blocked_continuation_prompt(
    verdict: Mapping[str, Any],
    gates: Sequence[Any],
    oracle_cmd: str | None = None,
    *, sandboxed: bool = True,
) -> str:
    """Prefix for the one continuation turn after a bloqueado with work.

    Informs, never overrides: the builder either retracts with ``hecho``
    or restates a real impediment; the gates still decide the closure.
    """
    motivo = str(verdict.get("motivo") or "").strip()[:_MOTIVO_CHARS]
    necesidad = str(verdict.get("necesidad") or "").strip()[:_NECESIDAD_CHARS]
    lines = [
        "CONTINUACIÓN TRAS TU VEREDICTO bloqueado",
        f"Tu última respuesta cerró bloqueado: {motivo}",
    ]
    if necesidad:
        lines.append(f"Necesidad declarada: {necesidad}")
    lines += [
        "Tu trabajo sigue en el árbol de trabajo (git status y git diff) y "
        ".openceo/HANDOFF.md lo resume: no lo rehagas ni lo descartes.",
        "Hechos del entorno, confirmados por el orquestador:",
        f"- {_SANDBOX_FACTS if sandboxed else _HOST_FACTS} "
        "Eso no es un impedimento.",
        *_gates_intro(gates, oracle_cmd),
        "Decide con eso:",
        "- Si ese era tu único impedimento, cierra con resultado=hecho y en "
        "evidencia indica qué ejecutaste y qué no pudiste ejecutar y por qué.",
        "- Si otro impedimento externo te impide entregar lo pedido, repítelo "
        "con precisión en resultado=bloqueado.",
        "Verifica solo lo que cambies ahora y no repitas efectos ya "
        "confirmados. La petición original sigue como referencia:",
        "",
        "",
    ]
    return "\n".join(lines)


def kept_work_note(kept: Mapping[str, Any]) -> str:
    """One-line pointer to a candidate kept on a branch."""
    base = str(kept.get("base_sha") or "")[:12]
    branch = str(kept.get("branch") or "")
    return (f"candidate kept on branch {branch} "
            f"({kept.get('files', 0)} file(s); git diff {base}..{branch})")
