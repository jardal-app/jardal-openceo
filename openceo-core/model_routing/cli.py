"""free-router command line (TASK-2026-00285).

F0 entry point: ``status`` only; further subcommands arrive with their
tickets. Usage from the repo root::

    ./.venv/bin/python openceo-core/model_routing/cli.py status [--json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    _CORE = str(Path(__file__).resolve().parents[1])
    if _CORE not in sys.path:
        sys.path.insert(0, _CORE)

from model_routing.models import collect_status  # noqa: E402

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """CLI parser; one subcommand per router operation."""
    parser = argparse.ArgumentParser(prog="free-router")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Show router paths and published artifacts")
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; 0 on success (missing F0 artifacts are expected)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "status":
        parser.error(f"unknown command: {args.command}")
    report = collect_status()
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0
    print(f"free-router status (phase {report.phase}, schema {report.schema_version})")
    print(f"config: {report.config_dir} ({'present' if report.config_present else 'missing'})")
    print(f"state: {report.state_dir} ({'present' if report.state_present else 'missing'})")
    print(f"catalog: {'present' if report.catalog_present else 'missing'}")
    print(f"rankings: {'present' if report.rankings_present else 'missing'}")
    print(f"snapshot: {report.snapshot or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
