"""Project gates run by the orchestrator (Tier S design v2 §11, F1).

Gates replace ``dev.qa`` and apply to every tier. The orchestrator runs them
after the build, after each fix and at close — always before any reviewer.
The agent is never the source of "tests pass": the ``Gates`` section of the
handoff is written here, from these results.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from agents.dev.tier_s.models import GateResult, GateSpec

#: Lines of trailing output kept per failed gate (design §11).
OUTPUT_TAIL_LINES = 200


def load_gates(dev_cfg: Mapping[str, Any] | None) -> list[GateSpec]:
    """Parse ``dev.gates`` (design §17.2). Empty when unconfigured.

    A project without gates runs with a telemetry note instead of failing:
    failing closed here would park every project that has not migrated yet,
    while the old engine (which reads ``dev.qa``) is still dispatching.
    """
    raw = (dev_cfg or {}).get("gates", [])
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("dev.gates must be a list")
    return [GateSpec.from_mapping(entry) for entry in raw]


def validate_gates_config(dev_cfg: Mapping[str, Any] | None) -> list[str]:
    """Human-readable errors for a ``dev.gates`` block, [] when valid."""
    raw = (dev_cfg or {}).get("gates")
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ["dev.gates must be a list"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"dev.gates[{index}]"
        try:
            spec = GateSpec.from_mapping(entry)
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
            continue
        if spec.name in seen:
            errors.append(f"{where}: duplicate gate name {spec.name!r}")
        seen.add(spec.name)
        if spec.baseline_red not in {"fail", "ignore"}:
            errors.append(
                f"{where}: baseline_red must be 'fail' or 'ignore'")
    return errors


def _tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    rows = text.splitlines()
    if len(rows) <= lines:
        return text
    return "\n".join(rows[-lines:])


def run_gate(repo: Path, spec: GateSpec) -> GateResult:
    """Run one gate in the executor's environment (cwd = repo worktree)."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            spec.cmd,
            cwd=repo,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=spec.timeout_minutes * 60,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
        return GateResult(
            name=spec.name, status="timeout", required=spec.required,
            elapsed_s=time.monotonic() - started,
            output_tail=_tail(out),
            detail=f"exceeded {spec.timeout_minutes} min",
        )
    except OSError as exc:
        return GateResult(
            name=spec.name, status="error", required=spec.required,
            elapsed_s=time.monotonic() - started, detail=str(exc),
        )
    elapsed = time.monotonic() - started
    if completed.returncode == 0:
        return GateResult(
            name=spec.name, status="pass", required=spec.required,
            elapsed_s=elapsed)
    return GateResult(
        name=spec.name, status="fail", required=spec.required,
        elapsed_s=elapsed, output_tail=_tail(completed.stdout or ""),
        detail=f"exit {completed.returncode}",
    )


def run_gates(repo: Path, gates: Sequence[GateSpec]) -> list[GateResult]:
    """Run every gate in order. No short-circuit: all results are telemetry."""
    return [run_gate(repo, spec) for spec in gates]


def gates_failed(results: Sequence[GateResult]) -> list[GateResult]:
    """Required gates that did not pass (fail/timeout/error)."""
    return [item for item in results
            if item.required and item.status != "pass"]


def baseline_blocked(
    results: Sequence[GateResult],
    specs: Sequence[GateSpec],
) -> list[GateResult]:
    """Required gates red on the base branch, unless ``baseline_red: ignore``.

    A red baseline parks the ticket in ``waiting(needs_owner, baseline_red)``
    instead of building on broken ground (design §6.1).
    """
    by_name = {spec.name: spec for spec in specs}
    blocked: list[GateResult] = []
    for item in results:
        if item.status == "pass" or not item.required:
            continue
        if by_name.get(item.name, GateSpec(item.name, "x")).baseline_red == "ignore":
            continue
        blocked.append(item)
    return blocked


def handoff_gates_section(results: Sequence[GateResult]) -> str:
    """The ``Gates:`` handoff lines, written by the orchestrator only."""
    if not results:
        return "Gates: none configured"
    lines = ["Gates:"]
    for item in results:
        flag = "" if item.required else " (optional)"
        lines.append(
            f"- {item.name}{flag}: {item.status} "
            f"({item.elapsed_s:.1f}s)" + (f" — {item.detail}" if item.detail else "")
        )
    return "\n".join(lines)
