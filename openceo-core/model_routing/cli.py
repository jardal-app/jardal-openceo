"""free-router command line (TASK-2026-00285, TASK-2026-00287, TASK-2026-00291).

``status``, ``validate-config``, ``live-budget`` and ``errors``; further
subcommands arrive with their tickets. Usage from the repo root::

    ./.venv/bin/python openceo-core/model_routing/cli.py status [--json]

Config and budget output never includes secret values.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    _CORE = str(Path(__file__).resolve().parents[1])
    if _CORE not in sys.path:
        sys.path.insert(0, _CORE)

from model_routing.availability import AvailabilityStore  # noqa: E402
from model_routing.config import ConfigError, load_policy, validate_config  # noqa: E402
from model_routing.live import LiveError, budget_state, live_enabled  # noqa: E402
from model_routing.models import collect_status  # noqa: E402

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """CLI parser; one subcommand per router operation."""
    parser = argparse.ArgumentParser(prog="free-router")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Show router paths and published artifacts")
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    validate = sub.add_parser("validate-config", help="Check policy, providers and secrets")
    validate.add_argument("--json", action="store_true", help="Machine-readable output")
    budget = sub.add_parser("live-budget", help="Show today's live-test budgets")
    budget.add_argument("--json", action="store_true", help="Machine-readable output")
    errors = sub.add_parser("errors", help="Query the persistent error registry")
    errors.add_argument("--since", required=True,
                        help="ISO datetime or relative window (e.g. 24h, 7d, 30m)")
    errors.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def _run_status(args: argparse.Namespace) -> int:
    report = collect_status()
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    print(f"free-router status (phase {report.phase}, schema {report.schema_version})")
    print(f"config: {report.config_dir} ({'present' if report.config_present else 'missing'})")
    print(f"state: {report.state_dir} ({'present' if report.state_present else 'missing'})")
    print(f"policy: {'present' if report.policy_present else 'missing'}")
    print(f"providers: {'present' if report.providers_present else 'missing'}")
    print(f"catalog: {'present' if report.catalog_present else 'missing'}")
    print(f"rankings: {'present' if report.rankings_present else 'missing'}")
    print(f"snapshot: {report.snapshot or 'none'}")
    return 0


def _run_validate_config(args: argparse.Namespace) -> int:
    issues = validate_config()
    if args.json:
        print(json.dumps([issue.__dict__ for issue in issues], indent=2))
    else:
        for issue in issues:
            print(f"{issue.name}: {'ok' if issue.ok else 'FAIL'} ({issue.detail})")
    return 0 if all(issue.ok for issue in issues) else 1


def _run_live_budget(args: argparse.Namespace) -> int:
    try:
        policy = load_policy()
    except ConfigError:
        policy = None
    try:
        state = budget_state(policy)
    except LiveError as exc:
        print(f"live budgets unavailable: {exc}")
        return 1
    if args.json:
        print(json.dumps({
            "date": state.date,
            "live_enabled": live_enabled(),
            "budgets": {
                name: {"cap": state.caps[name], "used": state.used[name]}
                for name in state.caps
            },
        }, indent=2))
        return 0
    print(f"live budgets for {state.date} (live {'enabled' if live_enabled() else 'disabled'})")
    for name in sorted(state.caps):
        print(f"{name}: used {state.used[name]}/{state.caps[name]}")
    return 0


def _parse_since(value: str) -> float:
    """ISO datetime or relative window (``Nh``/``Nd``/``Nm``) to epoch seconds."""
    text = value.strip()
    if text and text[-1] in "mhd" and text[:-1].strip().isdigit():
        amount = int(text[:-1].strip())
        span = {"m": 60, "h": 3600, "d": 86400}[text[-1]]
        return time.time() - amount * span
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid --since value: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _run_errors(args: argparse.Namespace) -> int:
    try:
        since_ts = _parse_since(args.since)
    except ValueError as exc:
        print(f"errors: {exc}")
        return 2
    store = AvailabilityStore()
    try:
        records = store.errors_since(since_ts)
    finally:
        store.close()
    if args.json:
        print(json.dumps([{
            "id": record.id, "ts": record.ts, "provider": record.provider,
            "deployment": record.deployment_id, "model": record.model,
            "class": record.error_class, "retry_after_s": record.retry_after_s,
            "action": record.action, "run_id": record.run_id,
            "excerpt": record.excerpt,
        } for record in records], indent=2))
        return 0
    if not records:
        print("no errors recorded")
        return 0
    for record in records:
        moment = datetime.fromtimestamp(record.ts, timezone.utc).isoformat()
        print(f"{moment} {record.provider} {record.deployment_id}"
              f" {record.error_class} {record.action} run={record.run_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; 0 on success (missing F0 artifacts are expected)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return _run_status(args)
    if args.command == "validate-config":
        return _run_validate_config(args)
    if args.command == "live-budget":
        return _run_live_budget(args)
    if args.command == "errors":
        return _run_errors(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
