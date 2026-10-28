"""Structured agent result: hecho / bloqueado / no_aplica (TASK-2026-00202).

Every agent ends with a verdict plus its reason. A scheduled technical
block waits on its persisted ``resume_at``; other blocked or declined
work returns once to the same planner. The motivo is never checked
against the catalog: no extra verifiers.

Dependency-free (stdlib + pydantic): sessions, the graph and the funnel
import this without cycles.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "AGENT_RESULT_FENCE",
    "AGENT_RESULT_JSON_SCHEMA",
    "AGENT_RESULT_PROMPT",
    "SCHEDULED_WAIT_COUNT_KEY",
    "SCHEDULED_WAIT_MAX_DECLINES",
    "SCHEMA_PATH",
    "AgentOutcome",
    "AgentResult",
    "legacy_blocked_result",
    "parse_agent_result",
    "scheduled_resume",
    "scheduled_wait_declines",
]

#: Params key carrying the consecutive scheduled_wait decline count
#: (TASK-2026-00350): every re-parked decline increments it, any other
#: outcome resets it, so a builder that keeps declining cannot loop forever.
SCHEDULED_WAIT_COUNT_KEY = "scheduled_wait_count"

#: Consecutive scheduled_wait declines before the ticket parks for the
#: owner instead of re-parking (mirrors the review no-quorum cap of 3).
SCHEDULED_WAIT_MAX_DECLINES = 3


class AgentOutcome(str, Enum):
    HECHO = "hecho"
    BLOQUEADO = "bloqueado"
    NO_APLICA = "no_aplica"


class AgentResult(BaseModel):
    """What the agent declares at the end of its run."""

    resultado: AgentOutcome
    motivo: str = Field(min_length=1)
    necesidad: str = ""
    entregables: list[str] = Field(default_factory=list)
    evidencia: str = ""
    resume_at: datetime | None = None

    @field_validator("resume_at", mode="before")
    @classmethod
    def _resume_is_timestamp(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("resume_at must be an ISO-8601 timestamp")
        return value

    @model_validator(mode="after")
    def _scheduled_wait(self) -> AgentResult:
        if self.resume_at is not None:
            if self.resultado is not AgentOutcome.BLOQUEADO:
                raise ValueError("resume_at requires resultado=bloqueado")
            if self.resume_at.tzinfo is None:
                raise ValueError("resume_at requires an explicit timezone")
            self.resume_at = self.resume_at.astimezone(timezone.utc)
        return self

    @field_validator("motivo")
    @classmethod
    def _motivo_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("entregables")
    @classmethod
    def _deliverables_nonblank(cls, value: list[str]) -> list[str]:
        for item in value:
            if not str(item).strip():
                raise ValueError("must not contain blank items")
        return value


#: Marker of the verdict block the agent prints (prompt + parser agree).
AGENT_RESULT_FENCE = "agent-result"

#: Flat JSON Schema (no $defs/$ref) for ``muse exec --output-schema``.
AGENT_RESULT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AgentResult",
    "type": "object",
    "required": ["resultado", "motivo", "necesidad", "entregables",
                 "evidencia", "resume_at"],
    "additionalProperties": False,
    "properties": {
        "resultado": {
            "type": "string",
            "enum": ["hecho", "bloqueado", "no_aplica"],
        },
        "motivo": {"type": "string", "minLength": 1},
        "necesidad": {"type": "string"},
        "entregables": {"type": "array", "items": {"type": "string"}},
        "evidencia": {"type": "string"},
        "resume_at": {"type": ["string", "null"], "format": "date-time"},
    },
}

#: Checked-in copy of the schema above, for the CLI flag.
SCHEMA_PATH = Path(__file__).with_name("agent_result.schema.json")

#: Verdict instructions appended to agent prompts (Spanish: dev voice).
AGENT_RESULT_PROMPT = """RESULTADO DEL AGENTE (obligatorio)
Cierra tu respuesta con exactamente un bloque agent-result con tu veredicto en JSON:
```agent-result
{"resultado": "hecho", "motivo": "qué quedó terminado", "necesidad": "", "entregables": ["..."], "evidencia": "...", "resume_at": null}
```
Reglas:
- resultado es hecho (terminaste lo pedido), bloqueado (un impedimento externo te frena) o no_aplica (este trabajo no es para tu rol).
- motivo siempre dice por qué, en una frase.
- necesidad describe en palabras lo que te falta (nunca nombres de tools, que no ves).
- entregables lista lo que dejaste; evidencia, dónde verificarlo (rutas, comandos).
- Una espera técnica con fecha conocida (observación de 24 h, propagación o cuota) usa resultado=bloqueado y resume_at en ISO-8601 con zona UTC. Conserva en evidencia el inicio, plazo y qué se debe comprobar al vencer; no duermas dentro del CLI ni pidas una aprobación humana por el tiempo de espera.
- Al reanudarte, comprueba el estado y la evidencia antes de actuar. No repitas un despliegue o un efecto confirmado. La llegada de resume_at solo permite volver a verificar: no demuestra que la prueba pasó.
- Decisiones técnicas rutinarias se resuelven en el alcance. Solo pide al Owner decisiones reservadas, sus datos o credenciales que únicamente él pueda aportar; no uses resume_at para saltarte esa espera humana.
- Si dudas entre hecho y bloqueado, elige bloqueado: los gates deciden el cierre, no tu veredicto."""


def scheduled_resume(verdict: AgentResult, now: datetime | None = None) -> datetime | None:
    if verdict.resume_at is None:
        return None
    if verdict.resume_at <= (now or datetime.now(timezone.utc)):
        raise ValueError("resume_at must be in the future; verify the condition now")
    return verdict.resume_at


def scheduled_wait_declines(params: Any) -> int:
    try:
        return max(0, int((params or {}).get(SCHEDULED_WAIT_COUNT_KEY) or 0))
    except (TypeError, ValueError):
        return 0

_FENCE_RE = re.compile(r"```agent-result\s*\n(.*?)\n```", re.DOTALL)

#: Raw-JSON fallback scans at most this many trailing brace opens.
_MAX_RAW_OPENS = 32


def parse_agent_result(text: str | None) -> AgentResult | None:
    """Best-effort verdict parse: last fenced block, else trailing JSON.

    Never raises: a missing or malformed verdict means legacy behavior
    (dev gates still decide completion), never a failure.
    """
    if not text or not str(text).strip():
        return None
    raw = str(text)
    blocks = _FENCE_RE.findall(raw)
    if blocks:
        return _validate_json(blocks[-1])
    candidate = _validate_json(raw.strip())
    if candidate is not None:
        return candidate
    opens = [index for index, char in enumerate(raw) if char == "{"]
    for index in reversed(opens[-_MAX_RAW_OPENS:]):
        candidate = _validate_json(raw[index:].strip())
        if candidate is not None:
            return candidate
    return None


def _validate_json(payload: str) -> AgentResult | None:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return AgentResult.model_validate(data)
    except ValueError:
        return None


def legacy_blocked_result(motivo: str) -> dict[str, Any]:
    """Verdict for a deterministic session failure (graph mapping).

    Legacy sessions (finance/secretary/pm) return a bare status: their
    FAILED means the agent could not do it, i.e. ``bloqueado`` with the
    session detail as motive. Always validates: the funnel stores it blind.
    """
    text = str(motivo or "").strip() or "legacy FAILED sin detalle"
    return AgentResult(
        resultado=AgentOutcome.BLOQUEADO, motivo=text).model_dump(mode="json", exclude_none=True)
