"""Piloto real de aislamiento del worker (TASK-2026-00062).

Que se prueba y por que asi
===========================
No basta con comprobar que el plan de aislamiento "tiene los flags": hay que
**ejecutar** un proceso dentro de la frontera y comprobar el limite efectivo. El probe:

1. Intenta leer un fichero canario que vive **fuera** del workspace -> debe fallar.
2. Intenta alcanzar un destino de red no autorizado -> hoy **si** lo alcanza, y el test
   lo afirma en vez de fingir una frontera que no existe (`egress_restricted=False`).
3. Comprueba que el entorno no hereda credenciales del host.

El plan se construye con el **mismo** camino que usa el runtime
(`detect_isolation_backend()` + `backend.plan(...)`), no con un bwrap escrito a mano.
Si no hay backend, el test se salta: un entorno sin frontera no puede acreditar la
frontera, y decir lo contrario seria el fallo que este ticket previene.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from research.runtimes.isolation import (  # noqa: E402
    IsolationPlan,
    detect_isolation_backend,
    ensure_worker_paths,
    sanitized_worker_env,
)


class _Listener:
    """Servidor TCP local: el 'destino prohibido' del piloto, sin salir a la red."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.reached = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        try:
            self._sock.settimeout(10)
            connection, _ = self._sock.accept()
            self.reached = True
            connection.close()
        except Exception:  # noqa: BLE001 - el timeout tambien es un resultado
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class IsolationPilotTests(unittest.TestCase):
    """Ejecuta el probe dentro de la frontera real cuando el host la concede."""

    def setUp(self) -> None:
        self.backend = detect_isolation_backend()
        if self.backend is None:
            self.skipTest("Sin backend de aislamiento en este host: no se puede ejecutar")

    def _plan(self, tmp: str, *, allow_network: bool = True) -> tuple[IsolationPlan, Path, Path]:
        base = Path(tmp)
        paths = ensure_worker_paths(base / "state", "res_pilot")
        canary_dir = base / "canary"
        canary_dir.mkdir(parents=True, exist_ok=True)
        canary = canary_dir / "secreto.txt"
        canary.write_text("CANARIO-LEIDO", encoding="utf-8")

        env = sanitized_worker_env(
            {"PATH": "/usr/bin:/bin", "DEEPSEEK_API_KEY": "no-debe-heredarse", "HOME": "/home/develop"},
            worker_home=paths["home"],
            worker_config=paths["config"],
        )
        script = (
            f"cat {canary} 2>/dev/null || echo CANARIO-INVISIBLE; "
            "id -u"
        )
        plan = self.backend.plan(
            argv=["/bin/sh", "-c", script],
            workspace=paths["workspace"],
            worker_home=paths["home"],
            worker_config=paths["config"],
            env=env,
            allow_network=allow_network,
        )
        return plan, canary, paths["workspace"]

    def test_the_canary_outside_the_workspace_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, canary, workspace = self._plan(tmp)
            self.assertTrue(plan.filesystem_isolated)
            result = subprocess.run(
                plan.argv, cwd=plan.cwd, env=plan.env, capture_output=True, text=True, timeout=60
            )
            # El canario existe y es legible por este proceso, pero no por el worker.
            self.assertEqual(canary.read_text(encoding="utf-8"), "CANARIO-LEIDO")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CANARIO-INVISIBLE", result.stdout)
        self.assertNotIn("CANARIO-LEIDO", result.stdout)

    def test_the_worker_env_does_not_inherit_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, _, _ = self._plan(tmp)
            worker_home = str(Path(tmp) / "state" / "worker" / "res_pilot")
        self.assertNotIn("DEEPSEEK_API_KEY", plan.env)
        self.assertNotEqual(plan.env.get("HOME"), "/home/develop")
        # El home y la config del worker son propios del job, no del host.
        self.assertEqual(plan.env.get("HOME"), f"{worker_home}/home")
        self.assertEqual(plan.env.get("XDG_CONFIG_HOME"), f"{worker_home}/config")

    def test_the_worker_can_resolve_inside_the_boundary(self) -> None:
        """Regresion del defecto real: sin resolutor, el worker no llega al modelo.

        `/etc/resolv.conf` es un enlace a `/run/...` en este host. Con solo
        `--ro-bind /etc /etc`, el fichero no existe dentro de la frontera y el worker
        muere con `provider.transport getaddrinfo ETIMEOUT`. El plan debe montar el
        resolutor.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_worker_paths(base / "state", "res_pilot")
            env = sanitized_worker_env(
                {"PATH": "/usr/bin:/bin"},
                worker_home=paths["home"],
                worker_config=paths["config"],
            )
            plan = self.backend.plan(
                argv=["/bin/sh", "-c", "cat /etc/resolv.conf"],
                workspace=paths["workspace"],
                worker_home=paths["home"],
                worker_config=paths["config"],
                env=env,
                allow_network=True,
            )
            result = subprocess.run(
                plan.argv, cwd=plan.cwd, env=plan.env, capture_output=True, text=True, timeout=60
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nameserver", result.stdout + result.stderr)
            # El resolutor es una copia propia del worker, no el /run del host.
            self.assertTrue((paths["home"] / "resolv.conf").exists())

    def test_the_network_boundary_is_not_claimed_when_it_is_not_enforced(self) -> None:
        """El egress no esta restringido y el plan lo dice.

        Un plan que afirmara `network="broker"` mientras pasa `--share-net` haria creer
        que el worker solo alcanza el broker. Este test fija la verdad medida: se
        alcanza un destino arbitrario, y `egress_restricted` es False.
        """
        listener = _Listener()
        self.addCleanup(listener.close)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_worker_paths(base / "state", "res_pilot")
            env = sanitized_worker_env(
                {"PATH": "/usr/bin:/bin"},
                worker_home=paths["home"],
                worker_config=paths["config"],
            )
            probe = (
                "python3 -c \"import socket;"
                f"s=socket.create_connection(('127.0.0.1',{listener.port}),3);"
                "s.close();print('DESTINO-ALCANZADO')\""
            )
            plan = self.backend.plan(
                argv=["/bin/sh", "-c", probe],
                workspace=paths["workspace"],
                worker_home=paths["home"],
                worker_config=paths["config"],
                env=env,
                allow_network=True,
            )
            result = subprocess.run(
                plan.argv, cwd=plan.cwd, env=plan.env, capture_output=True, text=True, timeout=60
            )
        self.assertFalse(plan.egress_restricted)
        self.assertEqual(plan.network, "unrestricted")
        if result.returncode == 0:
            # El backend concedio red sin filtrar destino: se declara, no se esconde.
            self.assertIn("DESTINO-ALCANZADO", result.stdout)
            self.assertTrue(listener.reached)
        else:
            # Si el host no concede red, el limite es mas fuerte y tambien se declara.
            self.assertFalse(listener.reached)


class NoFilesystemIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Un backend sin aislamiento de ficheros no puede ejecutar el worker."""

    class _NoMounts:
        name = "sin-monturas"

        @property
        def available(self) -> bool:
            return True

        def plan(self, *, argv, workspace, worker_home, worker_config, env, allow_network, extra_read_only=(), egress=None):
            return IsolationPlan(
                backend=self.name,
                argv=list(argv),
                env=dict(env),
                cwd=str(workspace),
                network="unrestricted",
                filesystem_isolated=False,
                egress_restricted=False,
                notes=["sin bind mounts"],
            )

    async def test_the_runtime_refuses_a_backend_that_does_not_isolate_files(self) -> None:
        from research.runtimes.base import IsolationUnavailable
        from research.runtimes.opencode import OpenCodeRuntime

        class _Settings:
            RESEARCH_OPENCODE_CLI_PATH = "/bin/true"
            RESEARCH_OPENCODE_VERSION_PIN = "2.0.20"
            RESEARCH_STATE_DIR = "/tmp/research-pilot"
            RESEARCH_OPENCODE_TEST_MODEL = "modelo/explicito"

        runtime = OpenCodeRuntime(
            settings=_Settings(),
            isolation_backend=self._NoMounts(),
            # Sin broker el runtime falla antes, por configuracion: aqui se quiere
            # llegar justamente al plan de aislamiento.
            broker_url="http://127.0.0.1:9/internal/mcp-research-broker/",
        )
        with tempfile.TemporaryDirectory() as tmp:
            from research.runtimes.base import RuntimeRequest

            request = RuntimeRequest(
                research_id="res_x",
                project_key="jardal-openceo",
                owner_id="usr_owner",
                prompt="p",
                workspace=Path(tmp),
                model="modelo/explicito",
                timeout_seconds=5,
            )
            with self.assertRaises(IsolationUnavailable):
                await runtime.run(request)


class ContainerEgressTests(unittest.TestCase):
    """El contenedor es el unico backend que puede filtrar el egress por destino."""

    def _plan(self, *, egress=None, run_user=""):
        from research.runtimes.isolation import ContainerBackend

        backend = ContainerBackend("docker", network="red-test", run_user=run_user)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_worker_paths(base / "state", "res_egress")
            env = sanitized_worker_env(
                {"PATH": "/usr/bin:/bin"},
                worker_home=paths["home"],
                worker_config=paths["config"],
            )
            plan = backend.plan(
                argv=["opencode", "run", "hola"],
                workspace=paths["workspace"],
                worker_home=paths["home"],
                worker_config=paths["config"],
                env=env,
                allow_network=True,
                egress=egress,
            )
            return plan

    def test_without_a_gateway_the_backend_does_not_claim_the_boundary(self) -> None:
        from research.runtimes.isolation import EgressPolicy

        plan = self._plan(egress=EgressPolicy(provider_hosts=("opencode.ai",), gateway=""))
        self.assertFalse(plan.egress_restricted)
        self.assertEqual(plan.network, "unrestricted")
        self.assertTrue(any("no se filtra" in note for note in plan.notes))

    def test_with_a_gateway_the_plan_denies_by_default(self) -> None:
        from research.runtimes.isolation import EgressPolicy

        plan = self._plan(
            egress=EgressPolicy(
                provider_hosts=("opencode.ai",),
                broker_host="172.31.240.1",
                broker_port=18765,
                gateway="172.31.240.1",
            )
        )
        self.assertTrue(plan.egress_restricted)
        self.assertEqual(plan.network, "restricted")
        argv = " ".join(plan.argv)
        # Deny por defecto y despues solo lo listado: loopback, DNS, broker y modelo.
        self.assertIn("iptables -P OUTPUT DROP", argv)
        self.assertIn("-o lo -j ACCEPT", argv)
        self.assertIn("-d 172.31.240.1 --dport 18765 -j ACCEPT", argv)
        self.assertIn("for host in opencode.ai; do", argv)
        self.assertIn("--cap-add=NET_ADMIN", argv)
        self.assertIn("--network red-test", argv)
        # Nada de la red del host.
        self.assertNotIn("--network host", argv)

    def test_the_container_is_the_only_backend_that_reports_egress_restricted(self) -> None:
        from research.runtimes.isolation import BubblewrapBackend, EgressPolicy

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_worker_paths(base / "state", "res_egress")
            env = sanitized_worker_env(
                {"PATH": "/usr/bin:/bin"}, worker_home=paths["home"], worker_config=paths["config"]
            )
            bwrap = BubblewrapBackend().plan(
                argv=["/bin/true"],
                workspace=paths["workspace"],
                worker_home=paths["home"],
                worker_config=paths["config"],
                env=env,
                allow_network=True,
                egress=EgressPolicy(provider_hosts=("opencode.ai",), gateway="172.31.240.1"),
            )
        # bwrap acredita ficheros, no egress: declararlo seria la mentira que el ticket evita.
        self.assertTrue(bwrap.filesystem_isolated)
        self.assertFalse(bwrap.egress_restricted)


class ContainerEgressSmokeTest(unittest.TestCase):
    """Smoke real del filtro: opt-in, porque exige imagen, red y regla de cortafuegos."""

    def setUp(self) -> None:
        if os.environ.get("RESEARCH_EGRESS_SMOKE") != "1":
            self.skipTest("smoke de egress desactivado (RESEARCH_EGRESS_SMOKE=1 para activarlo)")
        self.gateway = os.environ.get("RESEARCH_WORKER_NETWORK_GATEWAY", "172.31.240.1")
        self.network = os.environ.get("RESEARCH_WORKER_NETWORK", "openceo-research-egress")

    def test_a_forbidden_destination_is_blocked_and_the_provider_is_allowed(self) -> None:
        from research.runtimes.isolation import ContainerBackend, EgressPolicy

        backend = ContainerBackend("docker", network=self.network)
        self.assertTrue(backend.available, "docker no disponible")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = ensure_worker_paths(base / "state", "res_egress")
            env = sanitized_worker_env(
                {"PATH": "/usr/bin:/bin"}, worker_home=paths["home"], worker_config=paths["config"]
            )
            script = (
                'if wget -q -O /dev/null --timeout=6 https://example.com/ 2>/dev/null; '
                "then echo FORBIDDEN_OK; else echo FORBIDDEN_BLOCKED; fi; "
                'if wget -q -O /dev/null --timeout=8 https://opencode.ai 2>/dev/null; '
                "then echo PROVIDER_OK; else echo PROVIDER_FAIL; fi"
            )
            plan = backend.plan(
                argv=["/bin/sh", "-c", script],
                workspace=paths["workspace"],
                worker_home=paths["home"],
                worker_config=paths["config"],
                env=env,
                allow_network=True,
                egress=EgressPolicy(
                    provider_hosts=("opencode.ai",),
                    broker_host=self.gateway,
                    broker_port=18765,
                    gateway=self.gateway,
                ),
            )
            self.assertTrue(plan.egress_restricted)
            result = subprocess.run(
                plan.argv, cwd=plan.cwd, env=plan.env, capture_output=True, text=True, timeout=180
            )
        self.assertIn("FORBIDDEN_BLOCKED", result.stdout, result.stderr[-400:])
        self.assertIn("PROVIDER_OK", result.stdout, result.stderr[-400:])
