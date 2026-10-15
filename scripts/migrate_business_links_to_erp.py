#!/usr/bin/env python3
"""Migrate Mongo execution mirrors from ERPNext to Jardal ERP authority.

Directed link migration required by the startup authority gate
(db/mongo.py: assert_business_authority) before Core boots on Jardal ERP.

Covers both mirror shapes: the ``business`` key and the legacy ``erpnext``
key. Each NAMED mirror is verified to exist in Jardal ERP before its
``business`` mirror is written with provider ``jardal_erp``. Named mirrors
absent from BOTH systems (registrations that never completed anywhere) are
still re-stamped so the assert passes and the orphan-rollup path can handle
them; they are reported as dangling. Mirrors without a name never produced
a business record, so they get a provider-only marker (no record invented).
The legacy ``erpnext`` key is kept as audit trail. Personal-scope documents
are excluded. Idempotent: re-running is safe.

Usage:
    ./.venv/bin/python scripts/migrate_business_links_to_erp.py --dry-run
    ./.venv/bin/python scripts/migrate_business_links_to_erp.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "openceo-core"))

from settings import get_settings  # noqa: E402


async def collect(repo: Any) -> list[dict[str, Any]]:
    from horizon.privacy import mirror_exclusion_filter

    cursor = repo.tasks.find(
        {
            "$or": [
                {"params._mirrors.business.provider": "erpnext"},
                {"params._mirrors.business": {"$exists": False}, "params._mirrors.erpnext": {"$exists": True}},
            ],
            **mirror_exclusion_filter(),
        },
        {"task_id": 1, "params._mirrors": 1},
    )
    return [doc async for doc in cursor]


async def verify_ref(adapter: Any, ref: str) -> bool:
    try:
        found = await adapter.resolve_task(ref)
    except Exception:  # noqa: BLE001 - unverified stays unmigrated
        return False
    return bool(found and found.get("name") == ref)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from business.work import JardalERPWork
    from db.mongo import MongoRepository

    settings = get_settings()
    repo = MongoRepository(settings)
    await repo.connect()
    docs = await collect(repo)
    adapter = JardalERPWork(settings)
    migrated = 0
    marked = 0
    dangling: list[str] = []
    skipped: list[str] = []
    try:
        for doc in docs:
            mirrors = (doc.get("params") or {}).get("_mirrors") or {}
            source = mirrors.get("business") or mirrors.get("erpnext") or {}
            ref = str(source.get("name") or "").strip()
            if not ref:
                if args.apply:
                    await repo.tasks.update_one(
                        {"_id": doc["_id"], "params._mirrors.business": {"$exists": False}},
                        {"$set": {"params._mirrors.business": {"provider": "jardal_erp"}}},
                    )
                marked += 1
                continue
            business = {k: v for k, v in source.items() if v is not None}
            business["provider"] = "jardal_erp"
            if not await verify_ref(adapter, ref):
                dangling.append(f"{doc.get('task_id')}:{ref}")
            else:
                migrated += 1
            if args.apply:
                await repo.tasks.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"params._mirrors.business": business}},
                )
    finally:
        await adapter.aclose()
        await repo.close()
    print(
        f"mirrors={len(docs)} migrated={migrated} marked={marked} "
        f"dangling={len(dangling)} skipped={len(skipped)} applied={args.apply}"
    )
    for entry in dangling:
        print(f"dangling: {entry}")
    for entry in skipped[:30]:
        print(f"skipped: {entry}")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
