#!/usr/bin/env python3
"""One explicit prepaid provider probe; no model, production flags or purchases.

Usage: ./.venv/bin/python scripts/research_provider_probe.py --provider serper --use-credit
Firecrawl reads only https://example.org using the real wired reader and egress guard.
Shared ledgers stay in Mongo; deleting them would reset the measured quota.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))


async def main(args):
    if not args.use_credit:
        raise SystemExit("Explicit --use-credit is required")
    from settings import get_settings
    from db.mongo import MongoRepository
    from research.wiring import build_research_service
    settings = get_settings()
    repo = MongoRepository(settings)
    await repo.connect()
    service, resources = build_research_service(settings, repo)
    research_id = "res_provider_probe_" + uuid4().hex
    now = datetime.now(timezone.utc)
    doc = {"research_id": research_id, "project_key": "jardal-openceo", "owner_id": "provider-probe",
        "purpose": "provider-probe", "status": "partial", "classification": "public",
        "request_fingerprint": research_id, "adapters": {args.provider: True},
        "read_provider": "firecrawl" if args.provider == "firecrawl" else "http",
        "created_at": now, "updated_at": now, "expires_at": now + timedelta(days=1)}
    try:
        await repo.create_research_job(doc)
        async with asyncio.timeout(30):
            if args.provider == "serper":
                result = await resources.discover(question="site:example.org Example Domain", research_id=research_id,
                                                   project_key="jardal-openceo", count=1)
                report = {"provider": "serper", "items": len(result.items),
                    "gaps": [d.to_payload() for d in result.degradations], "model_calls": 0}
                ok = bool(result.items) and not result.degradations
            else:
                page = await service._deps.read(url="https://example.org", research_id=research_id, project_key="jardal-openceo")
                report = {"provider": "firecrawl", "characters": len(page["text"]), "url": page["url"],
                    "remote_egress_unattested": page.get("remote_egress_unattested"), "model_calls": 0}
                ok = bool(page["text"])
        print(json.dumps({"ok": ok, "research_id": research_id, **report}, ensure_ascii=False))
        return 0 if ok else 2
    finally:
        await resources.aclose()
        await repo.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", required=True, choices=["serper", "firecrawl"])
    p.add_argument("--use-credit", action="store_true")
    raise SystemExit(asyncio.run(main(p.parse_args())))
