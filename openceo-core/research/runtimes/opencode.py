"""Adaptador headless de OpenCode V2 para research (TASK-2026-00062).

Por que un adaptador nuevo y no el runner de `agents/dev`
========================================================
El runner de Dev construye `--dir` y `--command`, que **no aparecen** en el help de
la version instalada (v2.0.20), y su centinela `OPENCEO_OPENCODE_DONE` pertenece al
wrapper shell de Dev, no al JSONL nativo. Reutilizarlo habria sido una compatibilidad
supuesta, no demostrada. Aqui la invocacion es:

    opencode run --standalone --agent investigator --format json --model <explicito> <prompt>

sin `--dir` (el directorio se fija con `cwd`), sin `--command` y sin `--continue`
global. Reanudar una sesion exige que pertenezca al mismo
`(project_key, owner_id, research_id)`.

Limites y honestidad
====================
`exit 0` no equivale a un informe valido: la salida se parsea y, si no hay texto
util, el resultado es un error tipado. La salida esta acotada en bytes. El deadline
cancela el **grupo** de procesos, no solo el hijo. Y si no hay una frontera de
aislamiento real disponible, el runtime se niega a ejecutar.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse, urlunparse

from research.runtimes.base import (
    IsolationUnavailable,
    ResearchRuntimeError,
    RuntimeCancelled,
    RuntimeConfigurationError,
    RuntimeExecutionError,
    RuntimeFirstResponseTimeout,
    RuntimeMalformedOutput,
    RuntimeOutcome,
    RuntimeOutputTooLarge,
    RuntimeRequest,
    RuntimeTimeout,
    RuntimeUnavailable,
    RuntimeVersionMismatch,
)
from research.runtimes.isolation import (
    IsolationBackend,
    IsolationPlan,
    detect_isolation_backend,
    ensure_worker_paths,
    sanitized_worker_env,
)

__all__ = [
    "JSONL_ERROR_EVENT_TYPE",
    "probe_cli_version",
    "JSONL_TEXT_EVENT_TYPES",
    "OpenCodeRuntime",
    "PinnedVersion",
    "SessionRegistry",
    "build_run_command",
    "extract_jsonl_error",
    "parse_jsonl_events",
    "write_worker_config",
]

# Version probada. Una version distinta no se presume compatible: el parser del JSONL
# y las banderas cambian entre versiones.
PINNED_VERSION = "2.0.20"

# Tipos de evento cuyo contenido textual compone la respuesta final. Un tipo
# desconocido se conserva como evento crudo en lugar de romper el parseo.
JSONL_TEXT_EVENT_TYPES = frozenset({"text", "message", "assistant", "result"})

# Tipo de evento con el que el CLI informa de un fallo. Importa porque el CLI lo
# escribe en **stdout** y deja **stderr vacio** (verificado con v2.0.20): leer solo
# stderr perderia el motivo real del fallo.
JSONL_ERROR_EVENT_TYPE = "error"

# Ruta relativa de la configuracion del worker dentro de su XDG_CONFIG_HOME.
WORKER_CONFIG_RELPATH = Path("opencode") / "opencode.json"



async def probe_cli_version(cli_path: str, *, timeout: float = 20.0) -> str | None:
    """Version que reporta el CLI, o `None` si no puede leerse.

    Sin sonda, `pinned_version_status()` devuelve `verified=False` y el runtime ejecuta
    igual: la version fijada seria una declaracion sin comprobacion. Con sonda, una
    version distinta de la fijada **aborta** (`RuntimeVersionMismatch`) en vez de
    presumir compatibilidad del parser de JSONL.
    """
    if not cli_path:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            cli_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except Exception:  # noqa: BLE001 - sin sonda la version no se presume
        return None
    text = (stdout or b"").decode("utf-8", "replace") or (stderr or b"").decode("utf-8", "replace")
    match = re.search(r"(\d+\.\d+\.\d+)", text)
    return match.group(1) if match else None


@dataclass(frozen=True)
class PinnedVersion:
    pinned: str
    detected: str | None
    verified: bool
    note: str = ""


class _Process(Protocol):
    pid: int
    returncode: int | None
    stdout: Any
    stderr: Any

    async def wait(self) -> int:  # pragma: no cover - interfaz
        ...

    def kill(self) -> None:  # pragma: no cover - interfaz
        ...


Spawner = Callable[..., Awaitable[Any]]


async def _default_spawn(argv: Sequence[str], *, cwd: str, env: Mapping[str, str]) -> Any:
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def build_run_command(
    *,
    cli_path: str,
    model: str,
    prompt: str,
    agent: str = "investigator",
    standalone: bool = True,
    session_id: str | None = None,
) -> list[str]:
    """argv del runtime. Sin `--dir`, sin `--command`, sin `--continue`."""
    if not cli_path:
        raise ValueError("Falta la ruta del CLI de OpenCode")
    if not (prompt or "").strip():
        raise ValueError("El runtime necesita un prompt")
    if not model:
        # Un modelo implicito podria caer en un proveedor pagado o en un tier que
        # entrena con los datos. El modelo se fija de forma explicita.
        raise ValueError("El runtime necesita un modelo explicito")
    command = [cli_path, "run"]
    if standalone:
        command.append("--standalone")
    command.extend(["--agent", agent, "--format", "json", "--model", model])
    if session_id:
        command.extend(["--session", session_id])
    command.append(prompt)
    return command


def parse_jsonl_events(raw: str) -> tuple[list[dict[str, Any]], str, str | None, int]:
    """Parsea el JSONL del runtime.

    Devuelve ``(eventos, texto, session_id, lineas_malformadas)``. Tolerante a
    eventos desconocidos (se conservan) y estricto con las lineas corruptas: se
    cuentan para que el servicio pueda decidir si la salida es utilizable.
    """
    events: list[dict[str, Any]] = []
    parts: list[str] = []
    session_id: str | None = None
    malformed = 0

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("data:"):  # tolera prefijos SSE-like
            stripped = stripped[5:].strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        if str(event.get("payload_type", "")).startswith("run.terminal."):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                malformed += 1
                continue
            event = ({"type": "text", "part": {"text": payload.get("text", "")}}
                     if payload.get("terminal") == "completed" else
                     {"type": "error", "error": str(payload.get("error") or payload.get("terminal") or "musej failed")})
        events.append(event)
        if session_id is None:
            for key in ("sessionID", "session_id", "sessionId"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    session_id = value
                    break
        if event.get("type") in JSONL_TEXT_EVENT_TYPES:
            text = _extract_text(event)
            if text:
                parts.append(text)
    return events, "".join(parts).strip(), session_id, malformed


def _extract_text(event: Mapping[str, Any]) -> str:
    part = event.get("part")
    if isinstance(part, Mapping):
        for key in ("text", "content"):
            value = part.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("text", "content", "result"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            inner = value.get("text")
            if isinstance(inner, str) and inner:
                return inner
    return ""


def extract_jsonl_error(events: Sequence[Mapping[str, Any]]) -> str:
    """Mensaje de error que el CLI escribe en stdout, o cadena vacia.

    El CLI de v2.0.20 informa de los fallos con un evento ``type: error`` en stdout,
    deja **stderr vacio** y sale con codigo distinto de cero. Sin esto, el motivo real
    del fallo se perderia y quedaria un ``RuntimeExecutionError`` sin explicacion.
    """
    messages: list[str] = []
    for event in events:
        if event.get("type") != JSONL_ERROR_EVENT_TYPE:
            continue
        error = event.get("error")
        if isinstance(error, Mapping):
            parts = [
                str(error.get(key))
                for key in ("type", "message")
                if isinstance(error.get(key), str) and error.get(key)
            ]
            status = error.get("status")
            if isinstance(status, int):
                parts.append(f"status={status}")
            if parts:
                messages.append(": ".join(parts[:1]) + (" " + " ".join(parts[1:]) if len(parts) > 1 else ""))
                continue
            messages.append(json.dumps(dict(error), ensure_ascii=False)[:400])
        elif isinstance(error, str) and error:
            messages.append(error[:400])
        else:
            messages.append("error sin detalle")
    return " | ".join(messages)


def write_worker_config(
    *,
    config_dir: Path,
    model: str,
    deadline_seconds: float,
    broker_url: str,
    broker_bearer: str = "",
    agent: str = "investigator",
    provider_config: dict[str, Any] | None = None,
) -> Path:
    """Escribe la configuracion **cerrada** del worker dentro de su XDG_CONFIG_HOME.

    Sin este paso el CLI arranca con su configuracion global (o con ninguna) y
    ``--agent investigator`` falla con ``Agent not found``. Ademas se **valida** antes
    de escribir: si el perfil no esta cerrado, el worker no arranca.
    """
    from research.worker.profiles import (  # import local: evita acoplar la carga
        BrokerEndpoint,
        assert_worker_config_is_closed,
        build_worker_config,
    )

    if not broker_url:
        raise RuntimeConfigurationError(
            "El worker necesita el broker de research: sin el no hay adquisicion "
            "posible y la configuracion no podria estar cerrada"
        )
    endpoint = BrokerEndpoint(url=broker_url, bearer=broker_bearer)
    config = build_worker_config(
        model=model,
        endpoint=endpoint,
        deadline_seconds=deadline_seconds,
        agents=[agent],
    )
    if provider_config:
        config["providers"] = provider_config
    problems = assert_worker_config_is_closed(config)
    if problems:
        raise RuntimeConfigurationError(
            "La configuracion del worker no esta cerrada: " + "; ".join(problems)
        )
    target = Path(config_dir) / WORKER_CONFIG_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    os.chmod(target, 0o600)
    return target


class SessionRegistry:
    """Relacion validada `(project_key, owner_id, research_id) -> session_id`.

    Se niega a reanudar una sesion de otro scope. Es lo contrario de `--continue`
    global, que arrastraria contexto de un proyecto a otro.
    """

    def __init__(self, collection: Any | None = None) -> None:
        self._collection = collection

    async def get(self, research_id: str) -> dict[str, Any] | None:
        if self._collection is None:
            return None
        row = await self._collection.find_one({"research_id": research_id})
        return dict(row) if row else None

    async def record(
        self,
        *,
        research_id: str,
        project_key: str,
        owner_id: str,
        session_id: str | None,
    ) -> None:
        if self._collection is None or not session_id:
            return
        await self._collection.update_one(
            {"research_id": research_id},
            {
                "$set": {
                    "research_id": research_id,
                    "project_key": project_key,
                    "owner_id": owner_id,
                    "session_id": session_id,
                }
            },
            upsert=True,
        )

    async def resolve(self, request: RuntimeRequest) -> str | None:
        if not request.allow_session_resume:
            return None
        row = await self.get(request.research_id)
        if not row:
            return None
        scope = (row.get("project_key"), row.get("owner_id"), row.get("research_id"))
        if scope != request.session_scope:
            return None
        session_id = row.get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None


class OpenCodeRuntime:
    """Runtime OpenCode V2 con aislamiento obligatorio, deadline y cancelacion."""

    name = "opencode"

    def __init__(
        self,
        *,
        settings: Any,
        isolation_backend: IsolationBackend | None = None,
        spawn: Spawner | None = None,
        sessions: SessionRegistry | None = None,
        version_probe: Callable[[str], Awaitable[str | None]] | None = None,
        state_dir: Path | None = None,
        base_env: Mapping[str, str] | None = None,
        broker_url: str | None = None,
        broker_bearer: str = "",
        preferred_backend: str = "",
        container_backend: Any | None = None,
    ) -> None:
        self._settings = settings
        self._preferred_backend = preferred_backend
        self._container_backend = container_backend
        self._isolation = isolation_backend
        self._isolation_checked = isolation_backend is not None
        self._spawn = spawn or _default_spawn
        self._sessions = sessions or SessionRegistry()
        self._version_probe = version_probe
        self._state_dir = Path(state_dir or getattr(settings, "RESEARCH_STATE_DIR", "/tmp/research")).resolve()
        self._base_env = dict(base_env if base_env is not None else os.environ)
        self._broker_url = broker_url
        self._broker_bearer = broker_bearer
        self._running: dict[str, Any] = {}
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------------ helpers

    @property
    def cli_path(self) -> str:
        return str(getattr(self._settings, "RESEARCH_OPENCODE_CLI_PATH", ""))

    @property
    def pinned_version(self) -> str:
        return str(getattr(self._settings, "RESEARCH_OPENCODE_VERSION_PIN", PINNED_VERSION))

    def cli_available(self) -> bool:
        path = self.cli_path
        if not path:
            return False
        if os.path.sep in path:
            return Path(path).exists()
        return shutil.which(path) is not None

    def _engine_paths(self, cli_path: str | None = None) -> list[Path]:
        """Rutas que hay que montar en solo lectura para que el motor arranque.

        El binario de OpenCode no vive en `/usr/bin`: en este host esta en
        `~/.opencode/bin/opencode`. Se resuelve el symlink a su ruta real porque bwrap
        monta el destino, no el enlace.
        """
        path = cli_path or self.cli_path
        if not path:
            return []
        resolved = shutil.which(path) if os.path.sep not in path else path
        if not resolved:
            return []
        try:
            return [Path(resolved).resolve()]
        except OSError:  # pragma: no cover - ruta irresoluble
            return []

    def _backend(self) -> IsolationBackend | None:
        if not self._isolation_checked:
            self._isolation = detect_isolation_backend(
                self._preferred_backend or None,
                container_backend=self._container_backend,
            )
            self._isolation_checked = True
        return self._isolation

    def _effective_broker_url(self, backend: IsolationBackend | None) -> str:
        """URL del broker **tal como la ve el worker**.

        Un worker en contenedor no alcanza el `127.0.0.1` del host: su red dedicada llega
        al servicio por el gateway. Cambiar solo la URL del contenedor evita publicar el
        broker en interfaces de mas.
        """
        url = self._broker_url or ""
        gateway = str(getattr(self._settings, "RESEARCH_WORKER_NETWORK_GATEWAY", "") or "")
        if not url or not gateway or backend is None or backend.name != "container":
            return url
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return url
        netloc = f"{gateway}:{parsed.port}" if parsed.port else gateway
        return urlunparse(parsed._replace(netloc=netloc))

    def _egress_policy(self, backend: IsolationBackend | None, broker_url: str) -> Any | None:
        """Lista blanca de salida, o `None` si el backend no puede aplicarla."""
        gateway = str(getattr(self._settings, "RESEARCH_WORKER_NETWORK_GATEWAY", "") or "")
        if backend is None or backend.name != "container" or not gateway:
            return None
        from research.runtimes.isolation import EgressPolicy

        hosts = tuple(
            host.strip()
            for host in str(getattr(self._settings, "RESEARCH_WORKER_EGRESS_HOSTS", "") or "").split(",")
            if host.strip()
        )
        parsed = urlparse(broker_url)
        return EgressPolicy(
            provider_hosts=hosts,
            broker_host=gateway,
            broker_port=int(parsed.port or 0),
            gateway=gateway,
        )

    async def pinned_version_status(self) -> PinnedVersion:
        if self._version_probe is None:
            return PinnedVersion(
                pinned=self.pinned_version,
                detected=None,
                verified=False,
                note="Sin sonda de version en este despliegue; la version no se presume.",
            )
        detected = await self._version_probe(self.cli_path)
        verified = detected == self.pinned_version
        note = "" if verified else "Version distinta de la fijada; no se presume compatible."
        return PinnedVersion(pinned=self.pinned_version, detected=detected, verified=verified, note=note)

    # ---------------------------------------------------------------------- run

    def _musej_worker(self, request, paths, broker_url, env):
        from research.worker.profiles import BrokerEndpoint

        cli = str(getattr(self._settings, "MUSE_CLI_PATH", ""))
        engines = self._engine_paths(cli)
        if not engines:
            raise RuntimeUnavailable("musej executable unavailable")
        directory = engines[0].parent
        for name in ("muse", ".muse-version", ".muse-release-info.json"):
            path = directory / name
            if path.is_file():
                engines.append(path.resolve())
        version_file = directory / ".muse-version"
        if version_file.is_file():
            version = version_file.read_text().strip()
            if not re.fullmatch(r"[0-9.]+-R[0-9.]+", version):
                raise RuntimeConfigurationError("Invalid musej installed version")
            engines.append(directory / f"muse-bin-{version}")
        home = Path(self._base_env.get("HOME", str(Path.home())))
        config = Path(self._base_env.get("XDG_CONFIG_HOME", str(home / ".config")))
        auth = Path(self._base_env.get("MUSE_AUTH_PATH", str(config / "muse" / "auth.json"))).resolve()
        if not auth.is_file():
            raise RuntimeConfigurationError("musej subscription auth unavailable")
        engines.append(auth)
        env.update(MUSE_NO_AUTO_UPDATE="1", MUSE_AUTH_PATH=str(auth))
        endpoint = BrokerEndpoint(url=broker_url, bearer=self._broker_bearer)
        settings = {"run": {"workflow_trigger_mode": "off", "subagent_delegation_mode": "off", "toolset": []},
                    "mcpServers": {"research-broker": {"url": broker_url, "headers": endpoint.headers()}}}
        target = paths["config"] / "muse" / "settings.json"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth_target = target.with_name("auth.json")
        if not auth_target.exists():
            auth_target.unlink(missing_ok=True)
            auth_target.symlink_to(auth)
        target.write_text(json.dumps(settings), encoding="utf-8")
        target.chmod(0o600)
        argv = [cli, "exec", "--model", request.model, "--json", "--disable-shell", "--disable-write",
                "--disable-web-tools", "--disable-reminders", "--disable-request-user-input",
                "--no-foreign-personal-context", "--no-session-log", request.prompt]
        return argv, engines

    async def run(self, request: RuntimeRequest) -> RuntimeOutcome:
        musej = request.provider == "musej"
        if not musej and not self.cli_available():
            raise RuntimeUnavailable(
                f"CLI de OpenCode no disponible en {self.cli_path!r}"
            )
        status = PinnedVersion("", None, False) if musej else await self.pinned_version_status()
        if status.detected is not None and not status.verified:
            raise RuntimeVersionMismatch(
                f"OpenCode {status.detected} != {status.pinned}: {status.note}"
            )
        backend = self._backend()
        if backend is None:
            raise IsolationUnavailable(
                "No hay backend de aislamiento (bwrap/unshare/podman/docker). "
                "--standalone y un cwd propio no son un sandbox: se niega a ejecutar."
            )
        if musej and backend.name != "bwrap":
            raise IsolationUnavailable("musej research fallback requires bwrap")

        broker_url = self._effective_broker_url(backend)
        provider_env = {}
        if request.provider not in {"opencode", "musej"}:
            from model_routing.external import ExternalError, credential_env_for
            from model_routing.secrets import SecretsError, load_router_secrets

            try:
                name = credential_env_for(request.provider)
                value = getattr(load_router_secrets(), name)
                if not value or set(request.provider_config) != {request.provider}:
                    raise RuntimeConfigurationError("Free provider credential/config unavailable")
                provider_env[name] = value
            except (ExternalError, SecretsError, AttributeError) as exc:
                raise RuntimeConfigurationError("Free provider credential unavailable") from exc
        paths = ensure_worker_paths(self._state_dir, request.research_id)
        workspace = Path(request.workspace).resolve() if request.workspace else paths["workspace"]
        workspace.mkdir(parents=True, exist_ok=True)

        # La configuracion del worker se materializa **antes** de lanzar el CLI: sin
        # ella el agente `investigator` no existe para v2.0.20 ("Agent not found") y el
        # proceso heredaria la configuracion global. Se escribe solo si esta cerrada.
        write_worker_config(
            config_dir=paths["config"],
            model=request.model,
            deadline_seconds=request.timeout_seconds,
            broker_url=broker_url,
            broker_bearer=self._broker_bearer,
            agent=request.agent,
            provider_config=request.provider_config,
        )

        env = sanitized_worker_env(
            self._base_env,
            worker_home=paths["home"],
            worker_config=paths["config"],
            broker_url=broker_url,
            tool_credential=request.tool_credential or None,
        )
        env.update(provider_env)
        engine_paths = self._engine_paths()
        if musej:
            session_id = None
            argv, engine_paths = self._musej_worker(request, paths, broker_url, env)
        else:
            session_id = request.session_id or await self._sessions.resolve(request)
            argv = build_run_command(cli_path=self.cli_path, model=request.model,
                                     agent=request.agent, prompt=request.prompt, session_id=session_id)
        egress = self._egress_policy(backend, broker_url)
        plan = backend.plan(
            argv=argv,
            workspace=workspace,
            worker_home=paths["home"],
            worker_config=paths["config"],
            env=env,
            allow_network=True,
            egress=egress,
            # El CLI vive fuera de las rutas del sistema (p. ej.
            # `~/.opencode/bin/opencode`). Sin montarlo en solo lectura el worker no
            # arranca; montarlo concede el **ejecutable**, no el home que lo contiene.
            extra_read_only=engine_paths,
        )
        if not plan.filesystem_isolated:
            # Un backend que no monta nada (p. ej. `unshare` sin bind mounts) deja el
            # home, los repos y el vault del host visibles. Ejecutar ahi no seria
            # aislamiento: seria el fallo silencioso que este runtime existe para evitar.
            raise IsolationUnavailable(
                f"El backend {plan.backend!r} no acredita aislamiento de ficheros "
                f"({'; '.join(plan.notes) or 'sin detalle'})"
            )

        self._cancelled.discard(request.research_id)
        process = await self._spawn(plan.argv, cwd=plan.cwd, env=plan.env)
        self._running[request.research_id] = process
        try:
            stdout, stderr, truncated = await self._drain(
                process, request.timeout_seconds, request.max_output_bytes,
                first_response_timeout_seconds=request.first_response_timeout_seconds,
            )
        except (asyncio.TimeoutError, RuntimeTimeout) as exc:
            await self._terminate(process)
            self._running.pop(request.research_id, None)
            if isinstance(exc, RuntimeTimeout):
                raise
            raise RuntimeTimeout(
                f"El job {request.research_id} supero {request.timeout_seconds}s"
            ) from None
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            self._running.pop(request.research_id, None)

        if request.research_id in self._cancelled:
            self._cancelled.discard(request.research_id)
            raise RuntimeCancelled(f"Job {request.research_id} cancelado")

        exit_code = process.returncode if process.returncode is not None else await process.wait()
        stderr_text = stderr.decode("utf-8", "replace")
        events, text, parsed_session, malformed = parse_jsonl_events(
            stdout.decode("utf-8", "replace")
        )
        # El CLI reporta los fallos por stdout (evento `type: error`) y deja stderr
        # vacio: se extrae aqui para que el error no quede sin explicacion.
        reported_error = extract_jsonl_error(events)
        outcome = RuntimeOutcome(text=text, session_id=parsed_session or session_id,
                                 events=events, exit_code=int(exit_code), stderr=stderr_text[:4000])
        if truncated:
            await self._terminate(process)
            raise RuntimeOutputTooLarge(
                "La salida del runtime supero el techo configurado",
                raw=stdout[:4000].decode("utf-8", "replace"), outcome=outcome,
            )
        if exit_code != 0:
            detail = f": {reported_error}" if reported_error else ""
            raise RuntimeExecutionError(
                f"{'musej' if musej else 'OpenCode'} termino con codigo {exit_code}{detail}",
                exit_code=int(exit_code),
                stderr=stderr_text[:4000],
                runtime_error=reported_error,
                outcome=outcome,
            )

        if not text:
            reason = (
                f"OpenCode reporto un error sin texto util: {reported_error}"
                if reported_error
                else "Exit 0 sin texto util: un codigo de salida correcto no acredita un informe"
            )
            raise RuntimeMalformedOutput(
                reason,
                malformed_lines=malformed,
                raw=stdout[:2000].decode("utf-8", "replace"),
                outcome=outcome,
            )

        session_id = parsed_session or session_id
        await self._sessions.record(
            research_id=request.research_id,
            project_key=request.project_key,
            owner_id=request.owner_id,
            session_id=session_id,
        )
        return RuntimeOutcome(
            text=text,
            session_id=session_id,
            events=events,
            malformed_lines=malformed,
            exit_code=int(exit_code),
            stderr=stderr_text[:4000],
            version_verified=status.verified,
            isolation=plan.backend,
            egress_restricted=plan.egress_restricted,
        )

    async def _drain(
        self, process: Any, timeout_seconds: int, max_output_bytes: int,
        *, first_response_timeout_seconds: float | None = None,
    ) -> tuple[bytes, bytes, bool]:
        stdout_buffer, stderr_buffer = bytearray(), bytearray()

        def partial_outcome() -> RuntimeOutcome:
            events, text, session_id, malformed = parse_jsonl_events(stdout_buffer.decode("utf-8", "replace"))
            return RuntimeOutcome(text=text, session_id=session_id, events=events,
                                  malformed_lines=malformed, timed_out=True)

        async def _read(stream: Any, buffer: bytearray, *, observe: bool = False) -> tuple[bytes, bool]:
            truncated = False
            pending = bytearray()
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                if len(buffer) + len(chunk) > max_output_bytes:
                    buffer.extend(chunk[: max(0, max_output_bytes - len(buffer))])
                    truncated = True
                    break
                buffer.extend(chunk)
                if observe:
                    pending.extend(chunk)
                    lines = pending.split(b"\n")
                    pending = bytearray(lines.pop())
                    events, _, _, _ = parse_jsonl_events(b"\n".join(lines).decode("utf-8", "replace"))
                    tail_events, _, _, _ = parse_jsonl_events(pending.decode("utf-8", "replace"))
                    if tail_events:
                        events.extend(tail_events)
                        pending.clear()
                    if any((event.get("type") in JSONL_TEXT_EVENT_TYPES | {"reasoning"} and _extract_text(event).strip())
                           or (event.get("type") == "tool_use" and isinstance(event.get("part"), dict)
                               and event["part"].get("id") and event["part"].get("tool")) for event in events):
                        first_response.reschedule(None)
                    if extract_jsonl_error(events):
                        await self._terminate(process)
                        break
            return bytes(buffer), truncated

        async def _collect() -> tuple[bytes, bool, bytes]:
            stdout_result, stderr_result = await asyncio.gather(
                _read(process.stdout, stdout_buffer, observe=True), _read(process.stderr, stderr_buffer)
            )
            # La espera del proceso va **dentro** del deadline. Si quedara fuera, un
            # proceso que cierra su stdout pero sigue vivo (p. ej. el servidor
            # standalone que no termina) bloquearia el job para siempre.
            await process.wait()
            return stdout_result[0], stdout_result[1], stderr_result[0]

        try:
            async with asyncio.timeout(timeout_seconds):
                try:
                    async with asyncio.timeout(first_response_timeout_seconds) as first_response:
                        stdout, truncated, stderr = await _collect()
                except TimeoutError:
                    raise RuntimeFirstResponseTimeout(
                        "OpenCode first response deadline exhausted", outcome=partial_outcome()) from None
        except asyncio.TimeoutError:
            raise RuntimeTimeout("OpenCode deadline exhausted", outcome=partial_outcome()) from None
        return stdout, stderr, truncated

    async def _terminate(self, process: Any) -> None:
        await _kill_process_tree(process)

    async def cancel(self, research_id: str) -> bool:
        """Cancela el arbol de procesos del job. Devuelve si habia algo que cancelar."""
        process = self._running.get(research_id)
        if process is None:
            return False
        self._cancelled.add(research_id)
        await _kill_process_tree(process)
        return True

    def is_running(self, research_id: str) -> bool:
        return research_id in self._running


async def _kill_process_tree(process: Any) -> None:
    """Mata el grupo de procesos: hijos y servidor standalone incluidos."""
    if getattr(process, "returncode", None) is not None:
        return
    pid = getattr(process, "pid", None)
    killed = False
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            killed = False
    if not killed:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


def runtime_isolation_plan(
    *,
    backend: IsolationBackend | None,
    argv: Sequence[str],
    workspace: Path,
    worker_home: Path,
    worker_config: Path,
    env: Mapping[str, str],
    allow_network: bool = True,
) -> IsolationPlan:
    """Construye el plan de aislamiento o falla con un motivo explicito."""
    if backend is None:
        raise IsolationUnavailable("No hay backend de aislamiento disponible")
    return backend.plan(
        argv=argv,
        workspace=workspace,
        worker_home=worker_home,
        worker_config=worker_config,
        env=env,
        allow_network=allow_network,
    )
