"""Runtime OpenCode V2 de research (TASK-2026-00062).

Lo que se comprueba: la forma exacta del argv (sin `--dir`, sin `--command`), el
parseo tolerante del JSONL, la frontera de aislamiento **obligatoria**, el deadline
que mata el arbol de procesos y la reanudacion de sesion solo dentro del mismo scope.

La fixture JSONL es sintetica y esta documentada como tal: este entorno no tiene el
CLI, asi que no se pudo capturar una ejecucion real de la version fijada.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from research.runtimes.base import (  # noqa: E402
    IsolationUnavailable,
    RuntimeCancelled,
    RuntimeExecutionError,
    RuntimeMalformedOutput,
    RuntimeOutputTooLarge,
    RuntimeRequest,
    RuntimeTimeout,
    RuntimeUnavailable,
    RuntimeVersionMismatch,
)
from research.runtimes.isolation import (  # noqa: E402
    IsolationPlan,
    detect_isolation_backend,
    ensure_worker_paths,
    sanitized_worker_env,
)
from research.runtimes.opencode import (  # noqa: E402
    OpenCodeRuntime,
    SessionRegistry,
    build_run_command,
    parse_jsonl_events,
)
from settings import Settings  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_run_2_0_20.jsonl"


class _FakeBackend:
    name = "fake-namespace"

    @property
    def available(self) -> bool:
        return True

    def plan(self, *, argv, workspace, worker_home, worker_config, env, allow_network):
        return IsolationPlan(
            backend=self.name,
            argv=[self.name, "--", *argv],
            env=dict(env),
            cwd=str(workspace),
            network="broker" if allow_network else "none",
            notes=["backend de prueba"],
        )


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        chunk = self._data[self._offset : self._offset + (size if size > 0 else len(self._data))]
        self._offset += len(chunk)
        return chunk


class _FakeProcess:
    """Proceso falso. `pid=None` evita que el kill de grupo alcance al propio test."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False) -> None:
        self.pid = None
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = None if hang else returncode
        self._hang = hang
        self.killed = False
        self._release = asyncio.Event()
        if not hang:
            self._release.set()

    async def wait(self) -> int:
        await self._release.wait()
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._release.set()


class _RecordingSpawner:
    def __init__(self, process: _FakeProcess) -> None:
        self.process = process
        self.calls: list[dict] = []

    async def __call__(self, argv, *, cwd, env):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        return self.process


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "RESEARCH_OPENCODE_CLI_PATH": str(tmp_path / "opencode"),
        "RESEARCH_STATE_DIR": str(tmp_path / "state"),
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)


def _request(tmp_path: Path, **overrides) -> RuntimeRequest:
    payload = {
        "research_id": "res_abc",
        "project_key": "jardal-openceo",
        "owner_id": "usr_owner",
        "prompt": "Investiga X",
        "workspace": tmp_path / "ws",
        "model": "opencode/muse-spark-1.3-contributor-free",
        "timeout_seconds": 5,
    }
    payload.update(overrides)
    return RuntimeRequest(**payload)


def _touch_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "opencode"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    return cli


class BuildCommandTests(unittest.TestCase):
    def test_the_argv_matches_the_pinned_v2_shape(self) -> None:
        argv = build_run_command(
            cli_path="/opt/opencode",
            model="opencode/muse-spark-1.3-contributor-free",
            prompt="Investiga X",
        )
        self.assertEqual(argv[0], "/opt/opencode")
        self.assertEqual(argv[1], "run")
        self.assertIn("--standalone", argv)
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "investigator")
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--format") + 1], "json")
        self.assertIn("--model", argv)
        self.assertEqual(argv[-1], "Investiga X")

    def test_the_dev_runner_flags_are_absent(self) -> None:
        argv = build_run_command(cli_path="/opt/opencode", model="m", prompt="p")
        self.assertNotIn("--dir", argv)
        self.assertNotIn("--command", argv)
        self.assertNotIn("--continue", argv)

    def test_a_model_must_be_explicit(self) -> None:
        with self.assertRaises(ValueError):
            build_run_command(cli_path="/opt/opencode", model="", prompt="p")

    def test_a_prompt_is_required(self) -> None:
        with self.assertRaises(ValueError):
            build_run_command(cli_path="/opt/opencode", model="m", prompt="   ")

    def test_session_resume_adds_the_session_flag_only_when_asked(self) -> None:
        without = build_run_command(cli_path="/o", model="m", prompt="p")
        with_session = build_run_command(cli_path="/o", model="m", prompt="p", session_id="ses_1")
        self.assertNotIn("--session", without)
        self.assertIn("--session", with_session)
        self.assertEqual(with_session[with_session.index("--session") + 1], "ses_1")


class JsonlParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = FIXTURE.read_text(encoding="utf-8")

    def test_the_fixture_yields_the_composed_text_in_order(self) -> None:
        events, text, session_id, malformed = parse_jsonl_events(self.raw)
        self.assertEqual(
            text, "Primer fragmento de sintesis. Segundo fragmento con cita [e1]."
        )
        self.assertEqual(session_id, "ses_fixture_01")
        self.assertEqual(malformed, 0)
        self.assertEqual(len(events), 6)

    def test_an_unknown_event_type_is_preserved_not_fatal(self) -> None:
        events, _text, _session, malformed = parse_jsonl_events(self.raw)
        types = [e.get("type") for e in events]
        self.assertIn("evento_desconocido_de_version_futura", types)
        self.assertEqual(malformed, 0)

    def test_malformed_lines_are_counted_and_do_not_discard_valid_ones(self) -> None:
        raw = '{"type":"text","text":"ok"}\n{esto no es json}\n\n'
        events, text, _session, malformed = parse_jsonl_events(raw)
        self.assertEqual(text, "ok")
        self.assertEqual(malformed, 1)
        self.assertEqual(len(events), 1)

    def test_an_empty_output_has_no_text(self) -> None:
        _events, text, session, malformed = parse_jsonl_events("")
        self.assertEqual(text, "")
        self.assertIsNone(session)
        self.assertEqual(malformed, 0)


class WorkerEnvTests(unittest.TestCase):
    def test_credentials_and_host_home_are_not_inherited(self) -> None:
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/develop",
            "BRAVE_API_KEY": "secreto",
            "MONGODB_URL": "mongodb://localhost:27017",
            "MCP_SERVER_API_KEY": "secreto",
            "CLOUDFLARE_API_TOKEN": "secreto",
            "NOTION_API_TOKEN": "secreto",
            "OPENCODE_GO_API_KEY": "secreto",
            "UNRELATED_THING": "x",
        }
        env = sanitized_worker_env(
            base,
            worker_home=Path("/worker/home"),
            worker_config=Path("/worker/config"),
        )
        self.assertNotIn("BRAVE_API_KEY", env)
        self.assertNotIn("MONGODB_URL", env)
        self.assertNotIn("MCP_SERVER_API_KEY", env)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", env)
        self.assertNotIn("NOTION_API_TOKEN", env)
        self.assertNotIn("OPENCODE_GO_API_KEY", env)
        self.assertNotIn("UNRELATED_THING", env)
        self.assertEqual(env["HOME"], "/worker/home")
        self.assertEqual(env["XDG_CONFIG_HOME"], "/worker/config")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_the_vault_path_is_never_exported(self) -> None:
        env = sanitized_worker_env(
            {"PATH": "/usr/bin", "HOME": "/home/develop"},
            worker_home=Path("/worker/home"),
            worker_config=Path("/worker/config"),
        )
        self.assertFalse(any("jardal-knowledge" in value for value in env.values()))

    def test_only_the_broker_and_a_short_credential_are_added(self) -> None:
        env = sanitized_worker_env(
            {"PATH": "/usr/bin"},
            worker_home=Path("/w/h"),
            worker_config=Path("/w/c"),
            broker_url="http://127.0.0.1:9/mcp",
            tool_credential="cap-abc",
        )
        self.assertEqual(env["OPENCEO_RESEARCH_BROKER_URL"], "http://127.0.0.1:9/mcp")
        self.assertEqual(env["OPENCEO_RESEARCH_TOOL_CREDENTIAL"], "cap-abc")


class WorkerPathTests(unittest.TestCase):
    def test_a_traversing_research_id_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for bad in ("../evil", "a/b", "..", ""):
                with self.assertRaises(ValueError):
                    ensure_worker_paths(base, bad)

    def test_worker_paths_are_created_private(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_worker_paths(Path(tmp), "res_ok")
            for path in paths.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)


class SessionRegistryTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, **overrides) -> RuntimeRequest:
        payload = {
            "research_id": "res_1",
            "project_key": "proj",
            "owner_id": "owner-a",
            "prompt": "p",
            "workspace": Path("/tmp/ws"),
            "model": "m",
        }
        payload.update(overrides)
        return RuntimeRequest(**payload)

    async def test_resume_requires_the_same_scope(self) -> None:
        class _Collection:
            def __init__(self) -> None:
                self.rows: dict[str, dict] = {}

            async def find_one(self, query):
                return self.rows.get(query["research_id"])

            async def update_one(self, query, update, upsert=False):
                self.rows[query["research_id"]] = dict(update["$set"])
                return None

        registry = SessionRegistry(_Collection())
        await registry.record(
            research_id="res_1", project_key="proj", owner_id="owner-a", session_id="ses_1"
        )
        self.assertEqual(await registry.resolve(self._request()), "ses_1")
        # Otro owner no hereda la sesion, aunque el research_id coincida.
        self.assertIsNone(await registry.resolve(self._request(owner_id="owner-b")))

    async def test_resume_is_opt_out(self) -> None:
        registry = SessionRegistry()
        self.assertIsNone(await registry.resolve(self._request(allow_session_resume=False)))


class RuntimeRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _touch_cli(self.tmp)
        self.settings = _settings(self.tmp)
        self.raw = FIXTURE.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runtime(self, process: _FakeProcess, **overrides):
        spawner = _RecordingSpawner(process)
        runtime = OpenCodeRuntime(
            settings=self.settings,
            isolation_backend=_FakeBackend(),
            spawn=spawner,
            state_dir=self.tmp / "state",
            base_env={"PATH": "/usr/bin", "BRAVE_API_KEY": "secreto", "HOME": "/home/develop"},
            **overrides,
        )
        return runtime, spawner

    async def test_a_successful_run_returns_text_and_records_the_session(self) -> None:
        runtime, spawner = self._runtime(_FakeProcess(stdout=self.raw.encode()))
        outcome = await runtime.run(_request(self.tmp))
        self.assertIn("Primer fragmento", outcome.text)
        self.assertEqual(outcome.session_id, "ses_fixture_01")
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.isolation, "fake-namespace")
        # El aislamiento envuelve el argv real.
        self.assertEqual(spawner.calls[0]["argv"][0], "fake-namespace")
        self.assertIn("--standalone", spawner.calls[0]["argv"])
        self.assertNotIn("BRAVE_API_KEY", spawner.calls[0]["env"])

    async def test_exit_zero_without_text_is_malformed_not_success(self) -> None:
        runtime, _ = self._runtime(_FakeProcess(stdout=b'{"type":"step_finish"}\n'))
        with self.assertRaises(RuntimeMalformedOutput):
            await runtime.run(_request(self.tmp))

    async def test_a_non_zero_exit_is_a_typed_error(self) -> None:
        runtime, _ = self._runtime(
            _FakeProcess(stdout=b"", stderr=b"boom", returncode=3)
        )
        with self.assertRaises(RuntimeExecutionError) as ctx:
            await runtime.run(_request(self.tmp))
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertIn("boom", ctx.exception.stderr)

    async def test_output_over_the_cap_is_refused(self) -> None:
        runtime, _ = self._runtime(_FakeProcess(stdout=b"x" * 5000))
        with self.assertRaises(RuntimeOutputTooLarge):
            await runtime.run(_request(self.tmp, max_output_bytes=100))

    async def test_the_deadline_cancels_the_process(self) -> None:
        process = _FakeProcess(hang=True)
        runtime, _ = self._runtime(process)
        with self.assertRaises(RuntimeTimeout):
            await runtime.run(_request(self.tmp, timeout_seconds=0.05))
        self.assertTrue(process.killed)

    async def test_cancel_kills_the_running_process(self) -> None:
        process = _FakeProcess(hang=True)
        runtime, _ = self._runtime(process)
        request = _request(self.tmp, timeout_seconds=5)
        task = asyncio.create_task(runtime.run(request))
        for _ in range(50):
            if runtime.is_running(request.research_id):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(await runtime.cancel(request.research_id))
        with self.assertRaises(RuntimeCancelled):
            await task
        self.assertTrue(process.killed)

    async def test_cancelling_an_unknown_job_reports_false(self) -> None:
        runtime, _ = self._runtime(_FakeProcess())
        self.assertFalse(await runtime.cancel("res_nope"))

    async def test_a_missing_cli_refuses_to_run(self) -> None:
        settings = _settings(self.tmp, RESEARCH_OPENCODE_CLI_PATH=str(self.tmp / "no-existe"))
        runtime = OpenCodeRuntime(
            settings=settings,
            isolation_backend=_FakeBackend(),
            spawn=_RecordingSpawner(_FakeProcess()),
            state_dir=self.tmp / "state",
        )
        with self.assertRaises(RuntimeUnavailable):
            await runtime.run(_request(self.tmp))

    async def test_without_isolation_the_runtime_refuses(self) -> None:
        runtime = OpenCodeRuntime(
            settings=self.settings,
            isolation_backend=_FakeBackend(),
            spawn=_RecordingSpawner(_FakeProcess()),
            state_dir=self.tmp / "state",
        )
        with mock.patch(
            "research.runtimes.opencode.detect_isolation_backend", return_value=None
        ):
            runtime._isolation = None
            runtime._isolation_checked = False
            with self.assertRaises(IsolationUnavailable):
                await runtime.run(_request(self.tmp))

    async def test_a_version_mismatch_refuses_to_run(self) -> None:
        async def probe(_path: str) -> str:
            return "2.0.99"

        runtime, _ = self._runtime(_FakeProcess(stdout=self.raw.encode()), version_probe=probe)
        with self.assertRaises(RuntimeVersionMismatch):
            await runtime.run(_request(self.tmp))

    async def test_the_version_is_not_presumed_without_a_probe(self) -> None:
        runtime, _ = self._runtime(_FakeProcess(stdout=self.raw.encode()))
        status = await runtime.pinned_version_status()
        self.assertFalse(status.verified)
        self.assertIsNone(status.detected)


class IsolationDetectionTests(unittest.TestCase):
    def test_detection_reports_reality_instead_of_assuming(self) -> None:
        backend = detect_isolation_backend()
        if backend is None:
            # Este contenedor no permite user namespaces: la respuesta honesta es
            # "no hay frontera", y el runtime se niega a ejecutar.
            self.assertIsNone(backend)
        else:
            self.assertTrue(backend.available)

    def test_worker_paths_use_an_internal_id(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_worker_paths(Path(tmp), "res_abcdef")
            self.assertEqual(paths["workspace"].name, "res_abcdef")


class ProcessTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_real_process_group_is_killed(self) -> None:
        """Sin CLI no se puede ejecutar OpenCode, pero si probar el kill del arbol."""
        from research.runtimes.opencode import _kill_process_tree

        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            "sleep 30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await _kill_process_tree(process)
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(process.pid, 0)


if __name__ == "__main__":
    unittest.main()
