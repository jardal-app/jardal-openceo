"""systemd del core: reinicios acotados, last-exit y alerta (TASK-2026-00370, §16.4).

Sin modelos vivos ni core real: unidad sintética equivalente (proceso
`sleep`), scripts reales del repo y user manager de systemd. Las pruebas
vivas se omiten donde no haya user manager gestionable.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SYSTEMD_DIR = _REPO / "deploy" / "systemd"
HOST_UNIT = SYSTEMD_DIR / "openceo-core-host.service"
ALERT_UNIT = SYSTEMD_DIR / "openceo-alert.service"
RECORD_EXIT = _REPO / "scripts" / "record-exit.sh"
ALERT_SH = _REPO / "scripts" / "openceo-alert.sh"
HOST_SH = _REPO / "scripts" / "openceo-core-host.sh"
RUNNER_SH = _REPO / "scripts" / "openceo-core-host-runner.sh"
MIGRATE_SH = _REPO / "scripts" / "openceo-systemd-migrate.sh"


def _read_unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=True)
    parser.optionxform = str  # noqa: A003 - systemd keys are case-sensitive
    parser.read(path, encoding="utf-8")
    return parser


def _run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ, OPENCEO_ALLOW_DIRTY="1")
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged)


# --- Unidad del core: directivas exigidas por el alcance ---


def test_host_unit_restart_policy():
    unit = _read_unit(HOST_UNIT)
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "10"
    assert "record-exit.sh" in service["ExecStopPost"]
    assert service["ExecStart"].rstrip().endswith("openceo-core-host.sh foreground")
    # El tope de reinicios vive en [Unit]: en [Service] systemd lo ignoraría.
    assert unit["Unit"]["StartLimitIntervalSec"] == "600"
    assert unit["Unit"]["StartLimitBurst"] == "5"
    assert unit["Unit"]["OnFailure"] == "openceo-alert.service"


def test_host_unit_no_retry_on_lock_held():
    service = _read_unit(HOST_UNIT)["Service"]
    prevented = service.get("RestartPreventExitStatus", "")
    assert "3" in prevented.split()


def test_alert_unit_oneshot_without_core():
    unit = _read_unit(ALERT_UNIT)
    assert unit["Service"]["Type"] == "oneshot"
    assert "openceo-alert.sh" in unit["Service"]["ExecStart"]
    assert "main.py" not in unit["Service"]["ExecStart"]


def test_scripts_compile():
    for script in (HOST_SH, RUNNER_SH, MIGRATE_SH, RECORD_EXIT, ALERT_SH):
        proc = subprocess.run(["bash", "-n", str(script)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_host_script_has_foreground_mode():
    text = HOST_SH.read_text(encoding="utf-8")
    assert "foreground)" in text
    assert "unit_managed" in text


# --- record-exit.sh ---


def test_record_exit_registers_signal(tmp_path: Path):
    runtime = tmp_path / "rt"
    proc = _run(["bash", str(RECORD_EXIT)], env={
        "OPENCEO_RUNTIME_DIR": str(runtime),
        "SERVICE_RESULT": "signal",
        "EXIT_CODE": "killed",
        "EXIT_STATUS": "SIGKILL",
    })
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((runtime / "last-exit.json").read_text())
    assert doc["service_result"] == "signal"
    assert doc["exit_code"] == "killed"
    assert doc["exit_status"] == "SIGKILL"
    assert doc["at"]
    mode = stat.S_IMODE((runtime / "last-exit.json").stat().st_mode)
    assert mode == 0o600


def test_record_exit_defaults_and_never_fails(tmp_path: Path):
    runtime = tmp_path / "rt"
    env = {k: v for k, v in os.environ.items()
           if k not in ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS")}
    env.update({"OPENCEO_RUNTIME_DIR": str(runtime),
                "OPENCEO_ALLOW_DIRTY": "1"})
    proc = subprocess.run(["bash", str(RECORD_EXIT)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    doc = json.loads((runtime / "last-exit.json").read_text())
    assert doc["service_result"] == "unknown"


# --- openceo-alert.sh: canal externo sin depender del core ---


def test_alert_spools_and_never_fails(tmp_path: Path):
    runtime = tmp_path / "rt"
    runtime.mkdir()
    (runtime / "last-exit.json").write_text('{"exit_status": "SIGKILL"}')
    proc = _run(["bash", str(ALERT_SH)], env={
        "OPENCEO_RUNTIME_DIR": str(runtime),
        # Webhook inalcanzable: debe seguir saliendo 0.
        "OPENCEO_ALERT_WEBHOOK": "http://127.0.0.1:9/unreachable",
    })
    assert proc.returncode == 0, proc.stderr
    spool = (runtime / "alerts.log").read_text()
    assert "SIGKILL" in spool


# --- Pruebas vivas con unidad sintética (nunca el core real) ---

TEST_CRASH = "openceo-test-370-crash.service"
TEST_ALERT = "openceo-test-370-alert.service"


def _user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=timeout)


def _user_manager_ok() -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        return _systemctl("show", "-p", "Version").returncode == 0 \
            and _systemctl("list-units").returncode == 0
    except Exception:
        return False


needs_user_manager = pytest.mark.skipif(
    not _user_manager_ok(), reason="sin user manager de systemd gestionable")


def _write_test_units(runtime: Path) -> None:
    unit_dir = _user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / TEST_CRASH).write_text(
        "[Unit]\nDescription=synthetic crash process (TASK-2026-00370 test)\n"
        f"OnFailure={TEST_ALERT}\n"
        "StartLimitIntervalSec=60\nStartLimitBurst=3\n"
        "\n[Service]\nType=simple\n"
        "ExecStart=/bin/bash -c 'sleep 300'\n"
        f"ExecStopPost={RECORD_EXIT}\n"
        "Restart=on-failure\nRestartSec=1\n"
        f"Environment=OPENCEO_RUNTIME_DIR={runtime}\n")
    (unit_dir / TEST_ALERT).write_text(
        "[Unit]\nDescription=synthetic alert (TASK-2026-00370 test)\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart={ALERT_SH}\n"
        f"Environment=OPENCEO_RUNTIME_DIR={runtime}\n")
    assert _systemctl("daemon-reload").returncode == 0


def _remove_test_units() -> None:
    _systemctl("stop", TEST_CRASH)
    _systemctl("stop", TEST_ALERT)
    _systemctl("reset-failed")
    unit_dir = _user_unit_dir()
    for name in (TEST_CRASH, TEST_ALERT):
        try:
            (unit_dir / name).unlink()
        except FileNotFoundError:
            pass
    _systemctl("daemon-reload")


def _main_pid(unit: str) -> int | None:
    proc = _systemctl("show", unit, "-p", "MainPID", "--value")
    try:
        pid = int(proc.stdout.strip())
    except ValueError:
        return None
    return pid or None


def _wait_for(pred, timeout: float, tick: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(tick)
    return pred()


@pytest.fixture()
def synthetic_units(tmp_path: Path):
    runtime = tmp_path / "rt"
    runtime.mkdir()
    _write_test_units(runtime)
    _systemctl("start", TEST_CRASH)
    assert _wait_for(
        lambda: _systemctl("is-active", TEST_CRASH).returncode == 0, 15), \
        _systemctl("status", TEST_CRASH).stdout
    yield runtime
    _remove_test_units()


@needs_user_manager
def test_kill9_registers_signal_and_restarts(synthetic_units: Path):
    runtime = synthetic_units
    first = _main_pid(TEST_CRASH)
    assert first
    os.kill(first, 9)
    assert _wait_for(
        lambda: (runtime / "last-exit.json").exists(), 15)
    doc = json.loads((runtime / "last-exit.json").read_text())
    assert doc["service_result"] == "signal"
    assert "KILL" in doc["exit_status"]  # "KILL" o "SIGKILL" según systemd
    assert _wait_for(
        lambda: (_main_pid(TEST_CRASH) not in (None, first)), 15)
    restarts = _systemctl("show", TEST_CRASH, "-p", "NRestarts", "--value")
    assert int(restarts.stdout.strip()) >= 1


@needs_user_manager
def test_restart_exhaustion_fires_alert(synthetic_units: Path):
    runtime = synthetic_units
    for _ in range(6):
        pid = _main_pid(TEST_CRASH)
        if pid is None:
            break
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        time.sleep(2)
    assert _wait_for(
        lambda: _systemctl("is-failed", TEST_CRASH).returncode == 0, 60), \
        _systemctl("status", TEST_CRASH).stdout
    assert _wait_for(
        lambda: (runtime / "alerts.log").exists(), 30)
    assert "reinicios" in (runtime / "alerts.log").read_text()
