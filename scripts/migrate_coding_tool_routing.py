#!/usr/bin/env python3
"""One-shot migration of legacy task routing to top-level ``coding_tool``.

Tier S design §3.3 (TASK-2026-00086, F0): open tasks whose routing still lives
in ``params.coding_tool`` or ``coding_tier`` get it promoted to the top-level
field, with a deprecation note per task. Terminal tasks are left untouched.
Tasks with no routing value anywhere are REPORTED, not guessed: the tier
authority must assign their ``coding_tool`` explicitly.

Idempotent: re-running promotes nothing (migrated tasks already carry a valid
top-level value). Dry-run by default; pass --apply to write.

Usage:
    ./.venv/bin/python scripts/migrate_coding_tool_routing.py
    ./.venv/bin/python scripts/migrate_coding_tool_routing.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "openceo-core"))

from agents.dev.routing import plan_routing_migration  # noqa: E402
from db.mongo import MongoRepository  # noqa: E402
from settings import get_settings  # noqa: E402
from state import TaskStatus  # noqa: E402

logger = logging.getLogger("migrate_coding_tool_routing")

OPEN_STATUSES = (
    TaskStatus.QUEUED.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.WAITING.value,
)


async def collect_open_tasks(repo: MongoRepository) -> list[dict]:
    rows = repo.tasks.find({"status": {"$in": list(OPEN_STATUSES)}})
    return [MongoRepository._clean(row) async for row in rows]


async def main_async(*, apply: bool) -> int:
    repo = MongoRepository(get_settings())
    await repo.connect()
    try:
        tasks = await collect_open_tasks(repo)
    except Exception:
        await repo.close()
        raise
    plan = plan_routing_migration(tasks)
    by_id = {str(task.get("task_id") or ""): task for task in tasks}
    print(f"open tasks scanned: {len(tasks)}")
    print(f"already ok: {plan.already_ok}")
    print(f"to promote: {len(plan.promoted)}")
    print(f"needs assignment: {len(plan.needs_assignment)}")
    for entry in plan.promoted:
        for warning in entry["warnings"]:
            print(f"  promote task_id={entry['task_id']} patch={entry['patch']} ({warning})")
    for task_id in plan.needs_assignment:
        task = by_id.get(task_id) or {}
        agent = task.get("agent_hint") or task.get("agent") or "?"
        title = str(task.get("title") or "")[:70]
        print(f"  needs assignment task_id={task_id} agent={agent} title={title!r}")
    if not apply:
        print("dry-run: no writes (pass --apply to write)")
        await repo.close()
        return 0
    try:
        for entry in plan.promoted:
            for warning in entry["warnings"]:
                logger.warning("deprecated routing task_id=%s (%s)", entry["task_id"], warning)
            await repo.patch_task(entry["task_id"], entry["patch"])
        print(f"applied {len(plan.promoted)} patches")
    finally:
        await repo.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write patches (default is dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(main_async(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
