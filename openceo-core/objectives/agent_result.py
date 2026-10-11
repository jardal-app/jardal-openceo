"""Structured agent result: hecho / bloqueado / no_aplica (TASK-2026-00202).

Every agent ends with a verdict plus its reason. A ``bloqueado`` or
``no_aplica`` objective child returns once to the same planner; loose
tickets record the verdict without returning (TASK-2026-00204). The
motivo is never checked against the catalog: no extra verifiers.

Dependency-free (stdlib + pydantic): sessions, the graph and the funnel
import this without cycles.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "AGENT_RESULT_FENCE",
    "AGENT_RESULT_JSON_SCHEMA",
    "AGENT_RESULT_PROMPT",
    "SCHEMA_PATH",
    "AgentOutcome",
    "AgentResult",
    "legacy_blocked_result",
    "parse_agent_result",
]


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
    "required": ["resultado", "motivo"],
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
    },
}

#: Checked-in copy of the schema above, for the CLI flag.
SCHEMA_PATH = Path(__file__).with_name("agent_result.schema.json")

#: Verdict instructions appended to agent prompts (Spanish: dev voice).
AGENT_RESULT_PROMPT = """RESULTADO DEL AGENTE (obligatorio)
Cierra tu respuesta con exactamente un bloque agent-result con tu veredicto en JSON:
```agent-result
{"resultado": "hecho", "motivo": "qué quedó terminado", "necesidad": "", "entregables": ["..."], "evidencia": "..."}
```
Reglas:
- resultado es hecho (terminaste lo pedido), bloqueado (un impedimento externo te frena) o no_aplica (este trabajo no es para tu rol).
- motivo siempre dice por qué, en una frase.
- necesidad describe en palabras lo que te falta (nunca nombres de tools, que no ves).
- entregables lista lo que dejaste; evidencia, dónde verificarlo (rutas, comandos).
- Si dudas entre hecho y bloqueado, elige bloqueado: los gates deciden el cierre, no tu veredicto."""

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
        resultado=AgentOutcome.BLOQUEADO, motivo=text).model_dump(mode="json")
