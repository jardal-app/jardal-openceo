"""free-router command line (TASK-2026-00285, TASK-2026-00287, TASK-2026-00291).

``status``, ``validate-config``, ``errors``, ``models``, ``explain``,
``simulate-fallback``, ``sources`` and ``update``; further subcommands
arrive with their tickets. Usage from
the repo root::

    ./.venv/bin/python openceo-core/model_routing/cli.py status [--json]

Config output never includes secret values.
"""

from __future__ import annotations

import argparse
import dataclasses
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
from model_routing.models import RouterPolicy, collect_status  # noqa: E402
from model_routing.paths import (  # noqa: E402
    CATALOG_FILENAME,
    POLICY_FILENAME,
    RANKINGS_FILENAME,
    config_dir,
    state_dir,
)
from model_routing.publish import PublishError, load_catalog_doc  # noqa: E402
from model_routing.ranking import (  # noqa: E402
    RANK_PROFILES,
    explain_model,
    fallback_chain,
    load_rankings,
    resolve_scoring,
)
from model_routing.refresh import run_update, source_staleness  # noqa: E402

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """CLI parser; one subcommand per router operation."""
    parser = argparse.ArgumentParser(prog="free-router")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Show router paths and published artifacts")
    status.add_argument("--json", action="store_true", help="Machine-readable output")
    validate = sub.add_parser("validate-config", help="Check policy, providers and secrets")
    validate.add_argument("--json", action="store_true", help="Machine-readable output")
    errors = sub.add_parser("errors", help="Query the persistent error registry")
    errors.add_argument("--since", required=True,
                        help="ISO datetime or relative window (e.g. 24h, 7d, 30m)")
    errors.add_argument("--json", action="store_true", help="Machine-readable output")
    models = sub.add_parser("models", help="Show one profile ranking")
    models.add_argument("--profile", required=True, choices=sorted(RANK_PROFILES))
    models.add_argument("--top", type=int, default=10)
    models.add_argument("--json", action="store_true", help="Machine-readable output")
    models.add_argument("--config-dir", default=None)
    explain = sub.add_parser("explain", help="Explain one model verdict per profile")
    explain.add_argument("--model", required=True, help="Canonical model or deployment id")
    explain.add_argument("--profile", default=None, choices=sorted(RANK_PROFILES),
                         help="Single profile (default: all)")
    explain.add_argument("--json", action="store_true", help="Machine-readable output")
    explain.add_argument("--config-dir", default=None)
    simulate = sub.add_parser("simulate-fallback", help="Walk the fallback chain")
    simulate.add_argument("--profile", required=True, choices=sorted(RANK_PROFILES))
    simulate.add_argument("--down", nargs="*", default=[],
                          help="Canonical or deployment ids treated as failed")
    simulate.add_argument("--json", action="store_true", help="Machine-readable output")
    simulate.add_argument("--config-dir", default=None)
    sources = sub.add_parser("sources", help="List provider sources and freshness")
    sources.add_argument("--show-staleness", action="store_true",
                         help="Include observed times, TTLs and fresh/stale flags")
    sources.add_argument("--json", action="store_true", help="Machine-readable output")
    sources.add_argument("--config-dir", default=None)
    update = sub.add_parser("update", help="Refresh catalogs and rankings")
    kind = update.add_mutually_exclusive_group(required=True)
    kind.add_argument("--providers", action="store_true", help="Cron B: provider catalogs (6 h)")
    kind.add_argument("--benchmarks", action="store_true", help="Cron A: benchmarks (24 h)")
    update.add_argument("--config-dir", default=None)
    update.add_argument("--state-dir", default=None)
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


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config_dir) if args.config_dir else config_dir()


def _policy_or_default(directory: Path) -> RouterPolicy:
    try:
        return load_policy(directory / POLICY_FILENAME)
    except ConfigError:
        return RouterPolicy()


def _run_models(args: argparse.Namespace) -> int:
    if args.top < 1:
        print("models: --top must be positive")
        return 2
    try:
        rankings = load_rankings(_config_path(args) / RANKINGS_FILENAME)
    except PublishError as exc:
        print(f"models: {exc}")
        return 1
    ranking = rankings.profiles.get(args.profile)
    if ranking is None:
        print(f"models: profile {args.profile} has no published ranking")
        return 1
    if args.json:
        print(ranking.model_dump_json(indent=2))
        return 0
    print(f"{args.profile} (snapshot {ranking.provenance.catalog_snapshot or 'none'})")
    for item in ranking.order[:args.top]:
        print(f"#{item.rank} {item.canonical_model_id} [{item.family_id}] "
              f"q={item.quality:.3f} via {item.primary.provider}/"
              f"{item.primary.deployment_id} ({len(item.backups)} backups)")
    print(f"experimental: {len(ranking.experimental)}")
    if ranking.pending_top3 is not None:
        pending = ranking.pending_top3
        print(f"pending top-3 (seen {pending.first_seen}): "
              f"{', '.join(pending.proposed)}")
    return 0


def _run_explain(args: argparse.Namespace) -> int:
    directory = _config_path(args)
    try:
        catalog = load_catalog_doc(directory / CATALOG_FILENAME)
        rankings = load_rankings(directory / RANKINGS_FILENAME)
    except PublishError as exc:
        print(f"explain: {exc}")
        return 1
    profiles = [args.profile] if args.profile else sorted(RANK_PROFILES)
    policy = _policy_or_default(directory)
    reports = [explain_model(args.model, profile, catalog, rankings, policy)
               for profile in profiles]
    if args.json:
        print(json.dumps([dataclasses.asdict(report) for report in reports], indent=2))
        return 0
    for report in reports:
        line = f"{report.profile}: {report.subject} -> {report.verdict}"
        if report.quality is not None:
            line += f" (q={report.quality:.3f})"
        print(line)
        for reason in report.reasons:
            print(f"  - {reason}")
    return 0


def _run_simulate_fallback(args: argparse.Namespace) -> int:
    directory = _config_path(args)
    try:
        rankings = load_rankings(directory / RANKINGS_FILENAME)
    except PublishError as exc:
        print(f"simulate-fallback: {exc}")
        return 1
    ranking = rankings.profiles.get(args.profile)
    if ranking is None:
        print(f"simulate-fallback: profile {args.profile} has no published ranking")
        return 1
    policy = _policy_or_default(directory)
    scoring = resolve_scoring(args.profile, policy)
    chain = fallback_chain(ranking, policy, down=set(args.down))
    if args.json:
        print(json.dumps({
            "profile": args.profile,
            "down": list(args.down),
            "floor": scoring.quality_floor,
            "max_drop": scoring.max_quality_drop,
            "chain": [item.model_dump() for item in chain],
        }, indent=2, default=str))
        return 0
    if args.down:
        print(f"down: {', '.join(args.down)}")
    if not chain:
        print("no servable candidate (BLOCKED_NO_ELIGIBLE_MODEL)")
        return 0
    print(f"serves: #{chain[0].rank} {chain[0].canonical_model_id} "
          f"via {chain[0].primary.deployment_id}")
    for item in chain[1:]:
        print(f"  then #{item.rank} {item.canonical_model_id} "
              f"via {item.primary.deployment_id}")
    return 0


def _run_sources(args: argparse.Namespace) -> int:
    try:
        catalog = load_catalog_doc(_config_path(args) / CATALOG_FILENAME)
    except PublishError as exc:
        print(f"sources: {exc}")
        return 1
    report = source_staleness(catalog)
    if args.json:
        print(json.dumps([dataclasses.asdict(row) for row in report], indent=2))
        return 0
    for row in report:
        line = f"{row.provider}: {row.entries} entries, {row.verified} verified"
        if args.show_staleness:
            observed = row.newest_observed_at or "never"
            ttl = f"{row.ttl_hours:g}h" if row.ttl_hours is not None else "none"
            line += f", newest {observed}, ttl {ttl}, {'fresh' if row.fresh else 'stale'}"
        print(line)
    if catalog.stale:
        print(f"catalog is stale: {catalog.stale_reason or 'no reason recorded'}")
    return 0


def _run_update(args: argparse.Namespace) -> int:
    kind = "providers" if args.providers else "benchmarks"
    config = _config_path(args)
    state = Path(args.state_dir) if args.state_dir else state_dir()
    code, detail = run_update(kind, config, state)
    print(f"update --{kind}: {detail}")
    return code


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; 0 on success (missing F0 artifacts are expected)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return _run_status(args)
    if args.command == "validate-config":
        return _run_validate_config(args)
    if args.command == "errors":
        return _run_errors(args)
    if args.command == "models":
        return _run_models(args)
    if args.command == "explain":
        return _run_explain(args)
    if args.command == "simulate-fallback":
        return _run_simulate_fallback(args)
    if args.command == "sources":
        return _run_sources(args)
    if args.command == "update":
        return _run_update(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
