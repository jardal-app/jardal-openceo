"""free-router command line (TASK-2026-00285, TASK-2026-00287, TASK-2026-00291).

``status``, ``validate-config``, ``errors``, ``models``, ``explain``,
``simulate-fallback``, ``sources``, ``update``, ``reserve``,
``release``, ``cooldowns``, ``reservations``, ``cost-audit``,
``sessions``, ``tui-launch`` and ``tui-check``; further subcommands
arrive with their tickets. Usage from the repo root::

    ./.venv/bin/python openceo-core/model_routing/cli.py status [--json]

Config output never includes secret values.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    _CORE = str(Path(__file__).resolve().parents[1])
    if _CORE not in sys.path:
        sys.path.insert(0, _CORE)

from model_routing.acceptance import active_sessions, cost_audit  # noqa: E402
from model_routing.availability import AvailabilityStore, ReservationConflict  # noqa: E402
from model_routing.config import ConfigError, load_policy, validate_config  # noqa: E402
from model_routing.error_findings import queued_previews  # noqa: E402
from model_routing import tui as tui_launcher  # noqa: E402
from model_routing.research_wiring import CHECKPOINTS_DIRNAME  # noqa: E402
from model_routing.research_wiring import (  # noqa: E402
    ResearchRoutingError,
    SessionClient,
    SessionError,
    load_session_snapshot,
    native_research_targets,
)
from model_routing.models import RouterPolicy, collect_status  # noqa: E402
from model_routing.paths import (  # noqa: E402
    AVAILABILITY_DB_FILENAME,
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
    errors.add_argument("--since", required=False, default=None,
                        help="ISO datetime or relative window (e.g. 24h, 7d, 30m)")
    errors.add_argument("--summary", action="store_true",
                        help="Resume the window grouped by class and provider")
    errors.add_argument("--sync-findings", action="store_true",
                        help="Report findings queued for the inbox (report only, ignores --since)")
    errors.add_argument("--state-dir", default=None)
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
    reserve = sub.add_parser("reserve", help="Hold one run/round/slot (external coordinators)")
    reserve.add_argument("--run-id", required=True)
    reserve.add_argument("--round", required=True)
    reserve.add_argument("--slot", required=True)
    reserve.add_argument("--canonical", required=True, help="Canonical model id")
    reserve.add_argument("--family", required=True, help="Family id")
    reserve.add_argument("--deployment", required=True, help="Deployment id")
    reserve.add_argument("--ttl-s", type=float, default=3600.0)
    reserve.add_argument("--state-dir", default=None)
    reserve.add_argument("--json", action="store_true", help="Machine-readable output")
    release = sub.add_parser("release", help="Free one held slot")
    release.add_argument("--run-id", required=True)
    release.add_argument("--round", required=True)
    release.add_argument("--slot", required=True)
    release.add_argument("--state-dir", default=None)
    release.add_argument("--json", action="store_true", help="Machine-readable output")
    cooldowns = sub.add_parser("cooldowns", help="List active deployment cooldowns")
    cooldowns.add_argument("--state-dir", default=None)
    cooldowns.add_argument("--json", action="store_true", help="Machine-readable output")
    reservations = sub.add_parser("reservations", help="List active reservations of one round")
    reservations.add_argument("--run-id", required=True)
    reservations.add_argument("--round", required=True)
    reservations.add_argument("--state-dir", default=None)
    reservations.add_argument("--json", action="store_true", help="Machine-readable output")
    cost = sub.add_parser("cost-audit", help="Audit zero-cost over one window")
    cost.add_argument("--since", required=True,
                      help="ISO datetime or relative window (e.g. 24h, 7d, 30m)")
    cost.add_argument("--receipts-dir", action="append", default=[],
                      help="Accepted-report receipts to check (repeatable)")
    cost.add_argument("--state-dir", default=None)
    cost.add_argument("--json", action="store_true", help="Machine-readable output")
    sessions = sub.add_parser("sessions", help="List research sessions by freshness")
    sessions.add_argument("--active", action="store_true",
                          help="Only sessions updated within the window")
    sessions.add_argument("--max-age-h", type=float, default=24.0)
    sessions.add_argument("--state-dir", default=None)
    sessions.add_argument("--json", action="store_true", help="Machine-readable output")
    launch = sub.add_parser(
        "tui-launch", help="Prepare a managed session and attach the TUI",
        description=f"opencode-free-research (TASK-2026-00308): {tui_launcher.TUI_LIMITATION}.")
    launch.add_argument("--project-dir", required=True)
    launch.add_argument("--title", default="",
                        help="Session title (default: launcher name + profile)")
    launch.add_argument("--run-id", default="-")
    launch.add_argument("--print-only", action="store_true",
                        help="Print the attach command instead of attaching")
    launch.add_argument("--config-dir", default=None)
    launch.add_argument("--state-dir", default=None)
    launch.add_argument("--json", action="store_true",
                        help="Print the plan as JSON instead of attaching")
    check = sub.add_parser(
        "tui-check", help="Observe a launcher session; route its next turn",
        description=f"TASK-2026-00308: {tui_launcher.TUI_LIMITATION}.")
    check.add_argument("--session", required=True)
    check.add_argument("--project-dir", required=True)
    check.add_argument("--after", default=None,
                       help="Message id watermark; only newer messages count")
    check.add_argument("--run-id", default="-")
    check.add_argument("--config-dir", default=None)
    check.add_argument("--state-dir", default=None)
    check.add_argument("--json", action="store_true", help="Machine-readable output")
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
    if args.sync_findings:
        return _run_errors_sync_findings(args)
    if not args.since:
        print("errors: --since is required (ISO datetime or relative window)")
        return 2
    try:
        since_ts = _parse_since(args.since)
    except ValueError as exc:
        print(f"errors: {exc}")
        return 2
    store = AvailabilityStore(_state_path(args))
    try:
        records = store.errors_since(since_ts)
        if args.summary:
            return _print_errors_summary(records, args.json)
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


def _print_errors_summary(records: list, as_json: bool) -> int:
    by_class: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for record in records:
        by_class[record.error_class] = by_class.get(record.error_class, 0) + 1
        by_provider[record.provider] = by_provider.get(record.provider, 0) + 1
    if as_json:
        print(json.dumps({"total": len(records), "by_class": by_class,
                          "by_provider": by_provider}, indent=2))
        return 0
    print(f"{len(records)} errors")
    for name in sorted(by_class):
        print(f"  {name}: {by_class[name]}")
    for name in sorted(by_provider):
        print(f"  {name}: {by_provider[name]}")
    return 0


def _run_errors_sync_findings(args: argparse.Namespace) -> int:
    store = AvailabilityStore(_state_path(args))
    try:
        queued = queued_previews(store)
    finally:
        store.close()
    if args.json:
        print(json.dumps([{"fingerprint": fingerprint, "payload": payload}
                          for fingerprint, payload in queued], indent=2))
        return 0
    if not queued:
        print("no findings queued")
        return 0
    for fingerprint, payload in queued:
        title = (payload or {}).get("title", "<unreadable>") if payload else "<unreadable>"
        print(f"{fingerprint} autorun=false :: {title}")
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


def _state_path(args: argparse.Namespace) -> Path:
    base = Path(args.state_dir) if args.state_dir else state_dir()
    return base / AVAILABILITY_DB_FILENAME


def _run_reserve(args: argparse.Namespace) -> int:
    if args.ttl_s <= 0:
        print("reserve: --ttl-s must be above zero")
        return 2
    store = AvailabilityStore(_state_path(args))
    try:
        held = store.reserve(
            args.run_id, args.round, args.slot, args.canonical,
            args.family, args.deployment, ttl_s=args.ttl_s)
    except ReservationConflict as exc:
        print(f"reserve: {exc}")
        return 1
    finally:
        store.close()
    if args.json:
        print(json.dumps({
            "run_id": held.run_id, "round": held.round, "slot": held.slot,
            "canonical_model_id": held.canonical_model_id,
            "family_id": held.family_id, "deployment_id": held.deployment_id,
            "expires_at": held.expires_at,
        }, indent=2))
        return 0
    print(f"reserved {held.slot} -> {held.deployment_id}"
          f" (family {held.family_id}) for {held.run_id}/{held.round}")
    return 0


def _run_release(args: argparse.Namespace) -> int:
    store = AvailabilityStore(_state_path(args))
    try:
        freed = store.release(args.run_id, args.round, args.slot)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"released": freed}, indent=2))
        return 0
    if freed:
        print(f"released {args.slot} for {args.run_id}/{args.round}")
    else:
        print(f"no reservation held for {args.slot} in {args.run_id}/{args.round}")
    return 0


def _run_cooldowns(args: argparse.Namespace) -> int:
    store = AvailabilityStore(_state_path(args))
    try:
        rows = store.active_cooldowns()
    finally:
        store.close()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no active cooldowns")
        return 0
    for row in rows:
        moment = datetime.fromtimestamp(row["until_ts"], timezone.utc).isoformat()
        print(f"{row['deployment_id']} until {moment}"
              f" ({row['reason']}, failures {row['failures']})")
    return 0


def _run_reservations(args: argparse.Namespace) -> int:
    store = AvailabilityStore(_state_path(args))
    try:
        held = store.reservations(args.run_id, args.round)
    finally:
        store.close()
    if args.json:
        print(json.dumps([{
            "slot": item.slot, "canonical_model_id": item.canonical_model_id,
            "family_id": item.family_id, "deployment_id": item.deployment_id,
            "expires_at": item.expires_at, "exception": item.exception,
        } for item in held], indent=2))
        return 0
    if not held:
        print(f"no reservations for {args.run_id}/{args.round}")
        return 0
    for item in held:
        line = (f"{item.slot} -> {item.deployment_id}"
                f" (family {item.family_id}, canonical {item.canonical_model_id})")
        if item.exception:
            line += f" [exception: {item.exception}]"
        print(line)
    return 0


def _run_cost_audit(args: argparse.Namespace) -> int:
    try:
        since_ts = _parse_since(args.since)
    except ValueError as exc:
        print(f"cost-audit: {exc}")
        return 2
    store = AvailabilityStore(_state_path(args))
    try:
        audit = cost_audit(store, since_ts,
                           [Path(entry) for entry in args.receipts_dir])
    finally:
        store.close()
    if args.json:
        print(json.dumps({
            "since_ts": audit.since_ts, "errors_total": audit.errors_total,
            "by_class": audit.by_class, "by_provider": audit.by_provider,
            "billing_rows": audit.billing_rows,
            "receipts_checked": audit.receipts_checked,
            "receipts_missing_model": audit.receipts_missing_model,
            "receipts_invalid": audit.receipts_invalid,
            "clean": audit.clean,
        }, indent=2))
        return 0 if audit.clean else 1
    print(f"{audit.errors_total} errors since window;"
          f" billing rows: {audit.billing_rows}")
    print(f"receipts: {audit.receipts_checked} checked,"
          f" {audit.receipts_missing_model} missing model,"
          f" {audit.receipts_invalid} invalid")
    print("cost-audit: clean" if audit.clean else "cost-audit: VIOLATIONS")
    return 0 if audit.clean else 1


def _run_sessions(args: argparse.Namespace) -> int:
    if args.max_age_h <= 0:
        print("sessions: --max-age-h must be above zero")
        return 2
    base = Path(args.state_dir) if args.state_dir else state_dir()
    rows = active_sessions(base / CHECKPOINTS_DIRNAME, max_age_hours=args.max_age_h)
    if args.active:
        rows = [row for row in rows if row.status == "active"]
    if args.json:
        print(json.dumps([{
            "research_id": row.research_id, "session_id": row.session_id,
            "turn": row.turn, "effective_deployment": row.effective_deployment,
            "updated_at": row.updated_at, "status": row.status,
        } for row in rows], indent=2))
        return 0
    if not rows:
        print("no sessions" if not args.active else "no active sessions")
        return 0
    for row in rows:
        print(f"{row.research_id} turn {row.turn} {row.effective_deployment or '-'}"
              f" [{row.status}] updated {row.updated_at or 'never'}")
    return 0


def _tui_env_credentials() -> dict[str, str]:
    """Group B credential presence from the launcher environment.

    The TUI child inherits this same environment, so a group B
    target is only eligible when its ``FREE_ROUTER_*`` variable is
    already exported here. Names gate eligibility; values are never
    printed.
    """
    return {name: value for name, value in os.environ.items()
            if name.startswith("FREE_ROUTER_") and value}


def _run_tui_launch(args: argparse.Namespace) -> int:
    project = Path(args.project_dir)
    if not project.is_dir():
        print(f"tui-launch: project dir is not a directory: {project}")
        return 2
    config = _config_path(args)
    try:
        snapshot = load_session_snapshot(config / RANKINGS_FILENAME)
    except ResearchRoutingError as exc:
        print(f"tui-launch: {exc}")
        return 1
    if snapshot is None:
        print("tui-launch: no published ranking; run update first")
        return 1
    try:
        catalog = load_catalog_doc(config / CATALOG_FILENAME)
    except PublishError as exc:
        print(f"tui-launch: {exc}")
        return 1
    store = AvailabilityStore(_state_path(args))
    try:
        try:
            targets = native_research_targets(
                snapshot, catalog, store,
                credentials=_tui_env_credentials(), run_id=args.run_id)
        except ResearchRoutingError as exc:
            print(f"tui-launch: {exc}")
            return 1
        try:
            plan = tui_launcher.prepare_tui_session(
                client=SessionClient(), project_dir=project,
                targets=targets, title=args.title, store=store,
                run_id=args.run_id)
        except (ResearchRoutingError, SessionError) as exc:
            print(f"tui-launch: {exc}")
            return 1
    finally:
        store.close()
    if args.json:
        print(json.dumps({
            "session_id": plan.session_id, "profile": plan.profile,
            "target": plan.target.model_arg(),
            "project_dir": str(plan.project_dir),
            "argv": list(plan.argv), "audit": plan.audit,
            "limitation": tui_launcher.TUI_LIMITATION,
        }, indent=2))
        return 0
    print(f"session: {plan.session_id} ({plan.profile}, "
          f"{plan.target.model_arg()})")
    print(f"attach: {shlex.join(plan.argv)}")
    print(f"limitation: {tui_launcher.TUI_LIMITATION}")
    if args.print_only:
        return 0
    os.execvp(plan.argv[0], list(plan.argv))
    return 0


def _run_tui_check(args: argparse.Namespace) -> int:
    project = Path(args.project_dir)
    if not project.is_dir():
        print(f"tui-check: project dir is not a directory: {project}")
        return 2
    client = SessionClient()
    try:
        seen = tui_launcher.observe_tui_session(
            client=client, project_dir=project,
            session_id=args.session, after=args.after)
    except SessionError as exc:
        print(f"tui-check: {exc}")
        return 1
    store = AvailabilityStore(_state_path(args))
    try:
        targets = None
        targets_error = None
        config = _config_path(args)
        try:
            snapshot = load_session_snapshot(config / RANKINGS_FILENAME)
            if snapshot is None:
                raise ResearchRoutingError("no published ranking")
            catalog = load_catalog_doc(config / CATALOG_FILENAME)
            targets = native_research_targets(
                snapshot, catalog, store,
                credentials=_tui_env_credentials(), run_id=args.run_id)
        except (ResearchRoutingError, PublishError) as exc:
            targets_error = str(exc)
        moved = None
        if targets is not None:
            try:
                moved = tui_launcher.apply_next_turn_fallback(
                    client=client, project_dir=project, observation=seen,
                    targets=targets, store=store, run_id=args.run_id)
            except (ResearchRoutingError, SessionError) as exc:
                print(f"tui-check: {exc}")
                return 1
    finally:
        store.close()
    failure_id = seen.failure.message_id if seen.failure else None
    if args.json:
        print(json.dumps({
            "session_id": seen.session_id, "model": seen.model,
            "cost": seen.cost, "failure": failure_id,
            "drift": [{"message_id": item[0], "model": item[1]}
                      for item in seen.drift],
            "fallback": None if moved is None else {
                "applied": moved.applied, "new_model": moved.new_model,
                "reason": moved.reason},
            "targets_error": targets_error,
            "watermark": seen.watermark,
            "limitation": tui_launcher.TUI_LIMITATION,
        }, indent=2))
        return 0
    print(f"session: {seen.session_id} serves {seen.model or 'unset'} "
          f"(cost {seen.cost})")
    print(f"failure: {failure_id or 'none'}")
    for message_id, model in seen.drift:
        print(f"drift: {message_id} switched to {model}")
    if moved is None:
        print(f"fallback: observation only ({targets_error})")
    elif moved.applied:
        print(f"fallback: next turn serves {moved.new_model}")
    else:
        print(f"fallback: stood down ({moved.reason})")
    print(f"watermark: {seen.watermark or 'none'}")
    print(f"limitation: {tui_launcher.TUI_LIMITATION}")
    return 0


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
    if args.command == "reserve":
        return _run_reserve(args)
    if args.command == "release":
        return _run_release(args)
    if args.command == "cooldowns":
        return _run_cooldowns(args)
    if args.command == "reservations":
        return _run_reservations(args)
    if args.command == "cost-audit":
        return _run_cost_audit(args)
    if args.command == "sessions":
        return _run_sessions(args)
    if args.command == "tui-launch":
        return _run_tui_launch(args)
    if args.command == "tui-check":
        return _run_tui_check(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
