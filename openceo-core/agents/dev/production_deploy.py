"""Post-merge production step (TASK-2026-00415).

After an auto-merge to the base branch, the engine publishes the base
to origin (push only when ahead) and applies the project's
``dev.deploy`` mode: ``self`` (origin/<base> self-deploys, nothing to
run), ``command`` (run the configured cmd with a timeout, same runner
shape as the Tier S gates) or ``none`` (no production target;
delivery to origin/<base> plus a receipt).

The step never raises and never fails the ticket close: every failure
lands in the receipt as ``production_pending`` with its cause, and the
push retries on the next close (idempotent). Call sites fire it only
on merges to the base branch, never into ``objective/*`` branches.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

DEPLOY_MODES = ("none", "self", "command")

DEFAULT_DEPLOY: dict[str, Any] = {
    "mode": "none",
    "cmd": "",
    "timeout_minutes": 10,
}

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class ProductionReceipt:
    """One-line outcome of the production step (goes to close summaries)."""

    ok: bool
    pushed: bool
    sha: str
    mode: str
    detail: str

    @property
    def summary(self) -> str:
        return self.detail[:200]


def normalize_deploy(raw: Any) -> dict[str, Any]:
    """Fail-closed ``dev.deploy`` plan: unknown shapes become ``none``."""
    plan = dict(DEFAULT_DEPLOY)
    if not isinstance(raw, Mapping):
        return plan
    mode = raw.get("mode", "none")
    plan["mode"] = mode if mode in DEPLOY_MODES else "none"
    cmd = raw.get("cmd", "")
    plan["cmd"] = cmd if isinstance(cmd, str) else ""
    timeout = raw.get("timeout_minutes", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = 10
    plan["timeout_minutes"] = timeout
    return plan


def validate_deploy_config(dev_cfg: Mapping[str, Any] | None) -> list[str]:
    """Human-readable errors for ``dev.deploy``; ``[]`` when valid/absent."""
    raw = (dev_cfg or {}).get("deploy")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["dev.deploy must be a mapping {mode, cmd, timeout_minutes}"]
    errors: list[str] = []
    mode = raw.get("mode", "none")
    if mode not in DEPLOY_MODES:
        errors.append("dev.deploy.mode must be one of none, self, command")
    cmd = raw.get("cmd", "")
    if cmd is not None and not isinstance(cmd, str):
        errors.append("dev.deploy.cmd must be a string")
    elif mode == "command" and not str(cmd or "").strip():
        errors.append("dev.deploy.cmd is required when mode is command")
    elif isinstance(cmd, str) and len(cmd) > 512:
        errors.append("dev.deploy.cmd runs at most 512 characters")
    timeout = raw.get("timeout_minutes")
    if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int)
            or timeout <= 0):
        errors.append("dev.deploy.timeout_minutes must be a positive integer")
    return errors


def _default_run(cmd: Any, *, shell: bool, cwd: Path, timeout: int,
                 ) -> subprocess.CompletedProcess[str]:
    if shell:
        return subprocess.run(
            cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, text=True,
            errors="replace")
    return subprocess.run(
        cmd, shell=False, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, text=True,
        errors="replace")


def _tail(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat[-limit:] if len(flat) > limit else flat


def run_production_step(
    repo: Path | str,
    base_branch: str,
    deploy: Mapping[str, Any] | None,
    run: Runner | None = None,
) -> ProductionReceipt:
    """Publish ``base_branch`` and apply the deploy mode. Never raises."""
    do_run = run or _default_run
    try:
        return _run_production_step(Path(repo), base_branch, deploy, do_run)
    except Exception as exc:  # noqa: BLE001 - the close must survive this step
        return ProductionReceipt(
            ok=False, pushed=False, sha="unknown", mode="none",
            detail=f"production_pending: production step crashed "
                   f"({_tail(f'{type(exc).__name__}: {exc}')}); the merge is "
                   f"safe locally")


def _run_production_step(
    repo: Path,
    base_branch: str,
    deploy: Mapping[str, Any] | None,
    do_run: Runner,
) -> ProductionReceipt:
    plan = normalize_deploy(deploy)
    mode = str(plan["mode"])
    head = do_run(["git", "rev-parse", "HEAD"], shell=False, cwd=repo,
                  timeout=60)
    sha = head.stdout.strip() if head.returncode == 0 else "unknown"
    short = sha[:12] if sha != "unknown" else "unknown"
    origin = do_run(["git", "remote", "get-url", "origin"], shell=False,
                    cwd=repo, timeout=60)
    if origin.returncode != 0:
        return ProductionReceipt(
            ok=True, pushed=False, sha=sha, mode=mode,
            detail=f"merged {base_branch}@{short} locally; no origin remote")
    ahead_out = do_run(
        ["git", "rev-list", "--count", f"origin/{base_branch}..{base_branch}"],
        shell=False, cwd=repo, timeout=60)
    try:
        ahead = int(ahead_out.stdout.strip())
    except ValueError:
        ahead = -1
    pushed = False
    if ahead != 0:
        push = do_run(["git", "push", "origin", base_branch], shell=False,
                      cwd=repo, timeout=300)
        if push.returncode != 0:
            pull = do_run(["git", "pull", "--ff-only", "origin", base_branch],
                          shell=False, cwd=repo, timeout=300)
            if pull.returncode == 0:
                push = do_run(["git", "push", "origin", base_branch],
                              shell=False, cwd=repo, timeout=300)
        if push.returncode != 0:
            return ProductionReceipt(
                ok=False, pushed=False, sha=sha, mode=mode,
                detail=f"production_pending: push to origin/{base_branch} "
                       f"failed ({_tail(push.stderr)}); the merge is safe "
                       f"locally and the next close retries the push")
        pushed = True
    where = (f"pushed {base_branch}@{short}" if pushed
             else f"{base_branch}@{short} already published")
    if mode == "self":
        return ProductionReceipt(
            ok=True, pushed=pushed, sha=sha, mode=mode,
            detail=f"{where}; origin/{base_branch} self-deploys")
    if mode == "command":
        cmd = str(plan["cmd"] or "").strip()
        if not cmd:
            return ProductionReceipt(
                ok=False, pushed=pushed, sha=sha, mode=mode,
                detail=f"production_pending: deploy misconfigured (command "
                       f"mode without dev.deploy.cmd); {where}")
        try:
            done = do_run(cmd, shell=True, cwd=repo,
                          timeout=int(plan["timeout_minutes"]) * 60)
        except subprocess.TimeoutExpired:
            return ProductionReceipt(
                ok=False, pushed=pushed, sha=sha, mode=mode,
                detail=f"production_pending: deploy cmd timed out after "
                       f"{plan['timeout_minutes']} min; {where}")
        if done.returncode != 0:
            return ProductionReceipt(
                ok=False, pushed=pushed, sha=sha, mode=mode,
                detail=f"production_pending: deploy cmd failed "
                       f"(rc={done.returncode}: {_tail(done.stdout)}); {where}")
        return ProductionReceipt(
            ok=True, pushed=pushed, sha=sha, mode=mode,
            detail=f"{where}; deploy cmd ok")
    return ProductionReceipt(
        ok=True, pushed=pushed, sha=sha, mode=mode,
        detail=f"{where}; no production target configured")
