"""Aislamiento del worker del investigador (TASK-2026-00062).

Por que existe
==============
`--standalone`, un `cwd` propio y un entorno filtrado **no** son un sandbox: el
proceso conserva la autoridad del usuario del host y puede leer el home, los
repositorios, el vault, el socket de Docker o tmux y cualquier credencial heredada.
Aislar de verdad exige una frontera del sistema operativo (namespaces, contenedor o
usuario dedicado).

Politica
========
Si no hay backend disponible, el runtime **se niega a ejecutar** y devuelve
`IsolationUnavailable`. No existe un modo "degradado" que ejecute igual: degradar en
silencio seria exactamente el fallo que este ticket previene.

Backends soportados:
* `bubblewrap` (`bwrap`) — preferido: monta solo lo autorizado y deshabilita red.
* `unshare` — namespaces de usuario/montaje/red (requiere que el kernel lo permita).
* `podman`/`docker` — contenedor desechable con solo el workspace montado.

La construccion de argv y entorno es determinista y verificable; que el kernel
conceda la frontera se comprueba en `available`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "BubblewrapBackend",
    "IsolationBackend",
    "IsolationPlan",
    "ContainerBackend",
    "UnshareBackend",
    "detect_isolation_backend",
    "sanitized_worker_env",
]

# Variables que jamas se heredan: credenciales de infraestructura, del proyecto,
# del vault o de cualquier proveedor. El worker recibe solo lo que se le concede.
_DENIED_ENV_PREFIXES = (
    "AWS_",
    "CLOUDFLARE_",
    "DEEPSEEK_",
    "ERPNEXT_",
    "GROQ_",
    "JWT_",
    "MCP_",
    "MONGODB_",
    "NOTION_",
    "OPENCEO_",
    "OPENCODE_",
    "R2_",
    "SMTP_",
)
_DENIED_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_CREDENTIAL")
_ALLOWED_ENV_EXACT = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR"})


def sanitized_worker_env(
    base_env: Mapping[str, str],
    *,
    worker_home: Path,
    worker_config: Path,
    broker_url: str | None = None,
    tool_credential: str | None = None,
) -> dict[str, str]:
    """Entorno minimo del worker, sin homes personales ni credenciales heredadas.

    Solo se conserva una allowlist (PATH/idioma/zona/tmp) y se reescriben HOME y los
    XDG_* hacia el directorio del worker. El acceso a herramientas se concede con una
    credencial corta y explicita, no heredando claves del host.
    """
    env: dict[str, str] = {}
    for key, value in base_env.items():
        upper = key.upper()
        if upper in _ALLOWED_ENV_EXACT:
            env[key] = value
            continue
        if upper.startswith(_DENIED_ENV_PREFIXES):
            continue
        if upper.endswith(_DENIED_ENV_SUFFIXES):
            continue
        if upper.startswith("XDG_") or upper == "HOME":
            continue
        # El resto no se propaga: la allowlist es mas segura que una denylist.
    home = str(worker_home)
    config = str(worker_config)
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = config
    env["XDG_STATE_HOME"] = str(worker_home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(worker_home / ".cache")
    env["XDG_DATA_HOME"] = str(worker_home / ".local" / "share")
    env.setdefault("PATH", "/usr/bin:/bin")
    if broker_url:
        # Unico endpoint de salida autorizado para el worker.
        env["OPENCEO_RESEARCH_BROKER_URL"] = broker_url
    if tool_credential:
        env["OPENCEO_RESEARCH_TOOL_CREDENTIAL"] = tool_credential
    return env


@dataclass(frozen=True)
class IsolationPlan:
    """argv/entorno/cwd efectivos que se ejecutan, y que las pruebas pueden afirmar."""

    backend: str
    argv: list[str]
    env: dict[str, str]
    cwd: str
    read_only_paths: tuple[str, ...] = ()
    network: str = "none"
    notes: list[str] = field(default_factory=list)

    def to_doc(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "argv": self.argv,
            "cwd": self.cwd,
            "read_only_paths": list(self.read_only_paths),
            "network": self.network,
            "notes": list(self.notes),
        }


class IsolationBackend(Protocol):
    name: str

    @property
    def available(self) -> bool:  # pragma: no cover - propiedad
        ...

    def plan(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        worker_home: Path,
        worker_config: Path,
        env: Mapping[str, str],
        allow_network: bool,
    ) -> IsolationPlan:  # pragma: no cover - interfaz
        ...


class BubblewrapBackend:
    name = "bwrap"

    @property
    def available(self) -> bool:
        return shutil.which("bwrap") is not None

    def plan(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        worker_home: Path,
        worker_config: Path,
        env: Mapping[str, str],
        allow_network: bool,
    ) -> IsolationPlan:
        command = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            # Solo el workspace se monta escribible; el sistema, en solo lectura.
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--ro-bind",
            "/etc",
            "/etc",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--bind",
            str(workspace),
            str(workspace),
            "--chdir",
            str(workspace),
            "--setenv",
            "HOME",
            str(worker_home),
            "--setenv",
            "XDG_CONFIG_HOME",
            str(worker_config),
            "--unshare-all",
        ]
        if allow_network:
            command.append("--share-net")
        command.append("--")
        command.extend(argv)
        return IsolationPlan(
            backend=self.name,
            argv=command,
            env=dict(env),
            cwd=str(workspace),
            read_only_paths=("/usr", "/bin", "/lib", "/lib64", "/etc"),
            network="broker" if allow_network else "none",
            notes=["El home del host, los repos y el vault no se montan."],
        )


class UnshareBackend:
    """Namespaces de usuario/montaje/red via util-linux.

    Requiere que el kernel permita namespaces de usuario sin privilegios; si no,
    `available` es False y el runtime se niega a ejecutar en vez de seguir sin
    frontera.
    """

    name = "unshare"

    def __init__(self, probe_runner=None) -> None:
        self._probe_runner = probe_runner

    @property
    def available(self) -> bool:
        if shutil.which("unshare") is None:
            return False
        runner = self._probe_runner or _probe_unshare
        return bool(runner())

    def plan(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        worker_home: Path,
        worker_config: Path,
        env: Mapping[str, str],
        allow_network: bool,
    ) -> IsolationPlan:
        command = [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "--pid",
            "--fork",
            "--ipc",
            "--uts",
        ]
        if not allow_network:
            command.append("--net")
        command.append("--")
        command.extend(argv)
        return IsolationPlan(
            backend=self.name,
            argv=command,
            env=dict(env),
            cwd=str(workspace),
            network="broker" if allow_network else "none",
            notes=["Sin bind mounts: la separacion de ficheros depende del cwd y del entorno."],
        )


class ContainerBackend:
    name = "container"

    def __init__(self, binary: str = "podman", image: str = "openceo-research-worker:latest") -> None:
        self._binary = binary
        self._image = image

    @property
    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    def plan(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        worker_home: Path,
        worker_config: Path,
        env: Mapping[str, str],
        allow_network: bool,
    ) -> IsolationPlan:
        command = [
            self._binary,
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none" if not allow_network else "bridge",
            "--volume",
            f"{workspace}:{workspace}",
            "--workdir",
            str(workspace),
            "--env",
            "HOME=/worker",
            self._image,
            *argv,
        ]
        return IsolationPlan(
            backend=self.name,
            argv=command,
            env=dict(env),
            cwd=str(workspace),
            network="broker" if allow_network else "none",
            notes=["Contenedor desechable; sin socket de Docker ni home del host."],
        )


def _probe_unshare() -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def detect_isolation_backend(preferred: str | None = None) -> IsolationBackend | None:
    """Devuelve el primer backend realmente disponible, o `None`.

    `None` no es un modo degradado: el runtime lo trata como motivo para no ejecutar.
    """
    candidates: list[IsolationBackend] = [
        BubblewrapBackend(),
        UnshareBackend(),
        ContainerBackend("podman"),
        ContainerBackend("docker"),
    ]
    if preferred:
        ordered = sorted(candidates, key=lambda b: 0 if b.name == preferred else 1)
    else:
        ordered = candidates
    for backend in ordered:
        if backend.available:
            return backend
    return None


def isolation_report(backend: IsolationBackend | None) -> dict[str, Any]:
    return {
        "isolation_available": backend is not None,
        "backend": backend.name if backend else None,
        "checked": ["bwrap", "unshare", "podman", "docker"],
    }


def default_worker_paths(state_dir: Path) -> tuple[Path, Path]:
    """(home, config) del worker, deliberadamente fuera del home del host."""
    home = state_dir / "home"
    config = state_dir / "config"
    return home, config


def ensure_worker_paths(state_dir: Path, research_id: str) -> dict[str, Path]:
    """Crea workspace y estado del worker para un job concreto.

    El id se **valida**, no se sanea: recortar caracteres convertiria `../../etc` en
    `etc` y escondería un intento de traversal en lugar de rechazarlo.
    """
    if not research_id or not all(ch.isalnum() or ch in "_-" for ch in research_id):
        raise ValueError(f"research_id invalido para construir rutas: {research_id!r}")
    workspace = state_dir / "workspaces" / research_id
    home = state_dir / "worker" / research_id / "home"
    config = state_dir / "worker" / research_id / "config"
    for path in (workspace, home, config):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return {"workspace": workspace, "home": home, "config": config}
