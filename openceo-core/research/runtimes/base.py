"""Contratos del runtime de research (TASK-2026-00062).

El runtime es sustituible: el servicio habla con `ResearchRuntime`, no con OpenCode.
Un cambio de motor no debe tocar clientes, evidencias ni schedules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ResearchRuntimeError(RuntimeError):
    """Fallo del runtime del investigador."""


class RuntimeUnavailable(ResearchRuntimeError):
    """El motor no esta disponible (binario ausente, version distinta, sin ruta)."""


class RuntimeVersionMismatch(RuntimeUnavailable):
    """La version detectada no es la version probada y fijada."""


class IsolationUnavailable(RuntimeUnavailable):
    """No hay frontera de aislamiento real: se niega a ejecutar.

    Es deliberado: `--standalone`, un `cwd` distinto y un entorno filtrado **no** son
    un sandbox. El proceso conserva la autoridad del usuario del host, asi que
    ejecutarlo sin frontera real seria fingir un aislamiento que no existe.
    """


class RuntimeTimeout(ResearchRuntimeError):
    """El deadline vencio y el arbol de procesos fue cancelado."""


class RuntimeCancelled(ResearchRuntimeError):
    """La ejecucion fue cancelada explicitamente."""


class RuntimeConfigurationError(ResearchRuntimeError):
    """La configuracion del worker no es ejecutable o no esta cerrada.

    Es un fallo de construccion, no del modelo: falta el broker, o el perfil concede
    algo que el worker no debe tener. Se falla **antes** de lanzar el proceso.
    """


class RuntimeExecutionError(ResearchRuntimeError):
    """El proceso termino con codigo distinto de cero."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        stderr: str = "",
        runtime_error: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        # Motivo que el CLI reporto por stdout (evento `type: error`): con v2.0.20 el
        # stderr va vacio, asi que sin este campo el fallo quedaria sin explicacion.
        self.runtime_error = runtime_error


class RuntimeMalformedOutput(ResearchRuntimeError):
    """Salida no interpretable.

    Un `exit 0` **no** equivale a un informe valido: el proceso puede terminar bien y
    producir una salida inservible.
    """

    def __init__(self, message: str, *, malformed_lines: int = 0, raw: str = "") -> None:
        super().__init__(message)
        self.malformed_lines = malformed_lines
        self.raw = raw


class RuntimeOutputTooLarge(RuntimeMalformedOutput):
    """La salida supero el techo configurado y se corto."""


@dataclass(frozen=True)
class RuntimeRequest:
    research_id: str
    project_key: str
    owner_id: str
    prompt: str
    workspace: Path
    model: str
    agent: str = "investigator"
    timeout_seconds: int = 120
    max_output_bytes: int = 2_000_000
    session_id: str | None = None
    allow_session_resume: bool = True
    # Credencial corta que el worker presenta al broker en cada llamada de
    # herramienta. No es una clave de proveedor: es una capability ligada a este job
    # (proyecto, owner y research_id), con operaciones explicitas y vencimiento.
    tool_credential: str = ""

    @property
    def session_scope(self) -> tuple[str, str, str]:
        return (self.project_key, self.owner_id, self.research_id)


@dataclass(frozen=True)
class RuntimeOutcome:
    text: str
    session_id: str | None
    events: list[dict[str, Any]] = field(default_factory=list)
    malformed_lines: int = 0
    exit_code: int = 0
    stderr: str = ""
    timed_out: bool = False
    version_verified: bool = False
    isolation: str = ""
    # Si el backend restringe destinos de red. Hoy ninguno de los disponibles lo hace:
    # se declara para que nadie lea "aislado" como "sin egress".
    egress_restricted: bool = False

    def to_doc(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "session_id": self.session_id,
            "malformed_lines": self.malformed_lines,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "version_verified": self.version_verified,
            "isolation": self.isolation,
            "egress_restricted": self.egress_restricted,
            "event_types": [e.get("type") for e in self.events],
        }


class ResearchRuntime(Protocol):
    name: str

    async def run(self, request: RuntimeRequest) -> RuntimeOutcome:  # pragma: no cover
        ...

    async def cancel(self, research_id: str) -> bool:  # pragma: no cover
        ...
