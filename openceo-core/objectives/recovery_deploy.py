"""Host side of a recovery deploy: is the checkout deployable, restart it.

The recovery timer decides *when* (``recovery_policy``); this module only
answers whether the live checkout is safe to deploy and launches the
canonical restart where it survives its launcher (TASK-2026-00483).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["REPO_ROOT", "live_checkout_ready", "spawn_service_restart"]

#: The checkout the service runs from (and restarts from).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-optional-locks", *args], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", timeout=30)


def live_checkout_ready(root: Path = REPO_ROOT, base: str = "main") -> bool:
    """True when a restart would deploy ``base`` from a clean tree.

    A ticket branch or an Owner session's uncommitted edits in the live
    checkout must never become the running release.
    """
    try:
        head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        status = _git(root, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        logger.warning("recovery deploy: git unreadable in %s", root,
                       exc_info=True)
        return False
    if head.returncode != 0 or status.returncode != 0:
        return False
    return head.stdout.strip() == base and not status.stdout.strip()


def spawn_service_restart(root: Path = REPO_ROOT) -> bool:
    """Launch ``openceo-core-host.sh restart`` outside the service cgroup.

    The restart stops this very process, so it runs as a transient user
    unit. ``KillMode=process``: when the script exits, systemd must not
    reap the core it just started on the script path (control-group mode
    killed it, TASK-2026-00483); under the core unit the script only asks
    systemctl to restart it.
    """
    script = root / "scripts" / "openceo-core-host.sh"
    if not script.is_file():
        logger.warning("recovery deploy: %s not found", script)
        return False
    runner = shutil.which("systemd-run")
    if runner is None:
        logger.warning("recovery deploy: systemd-run missing; restart the "
                       "service to apply the merged fix")
        return False
    cmd = [
        runner, "--user", "--collect", "-p", "KillMode=process",
        "--on-active=2s",
        "--unit", f"openceo-recovery-deploy-{uuid.uuid4().hex[:8]}",
        str(script), "restart",
    ]
    try:
        subprocess.run(cmd, cwd=str(root), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("recovery deploy: could not schedule the restart: %s",
                       exc)
        return False
    return True
