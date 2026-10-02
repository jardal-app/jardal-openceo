#!/usr/bin/env python3
"""Read-only probe of a public feed or job board through the production reader and adapters.

Usage:
  ./.venv/bin/python scripts/research_watch_probe.py --url https://example.org/feed.xml
  ./.venv/bin/python scripts/research_watch_probe.py --type greenhouse --board BOARD_TOKEN
  ./.venv/bin/python scripts/research_watch_probe.py --type lever --board BOARD_TOKEN

Uses the same egress-guarded reader and the same adapters the watch engine runs, so a
source that passes here is one a subscription can read. Reads it twice, the second time
with the validators the first returned, to show whether the source answers a
conditional request (Lever never sends validators: a 304 on page one would hide later
pages). No Mongo, no model, no flags, no paid API and nothing is stored: it needs
neither RESEARCH_ENABLED nor the scheduler, and enables neither.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))


async def main(args) -> int:
    from research.acquisition.http_reader import HttpReader
    from research.policies import EgressGuard, GuardedFetcher
    from research.watch import build_watch_adapters, normalize_source

    spec = {"type": args.type, "url": args.url} if args.type == "rss" else {"type": args.type, "board": args.board}
    source = normalize_source(spec)  # same canonical form a subscription stores
    adapter = build_watch_adapters(HttpReader(fetcher=GuardedFetcher(guard=EgressGuard())),
                                   max_items=args.max_items)[source["type"]]
    scope = {"research_id": "res_watch_probe", "project_key": "jardal-openceo", "owner_id": "watch-probe"}

    async def read(conditional):
        async with asyncio.timeout(args.timeout):
            return await adapter.fetch_page(source, page_token=None, conditional=conditional,
                                            scope=scope, deadline=None)

    first = await read(None)
    report = {"source": source, "items": len(first.items), "truncated": first.truncated,
              "next_page": first.next_page, "etag": first.etag, "last_modified": first.last_modified,
              "gaps": [d.to_payload() for d in first.degradations],
              "sample": [{"title": (i.title or "")[:80], "url": i.url,
                          "published_at": i.published_at.isoformat() if i.published_at else None}
                         for i in first.items[:3]]}
    ok = bool(first.items) and not first.degradations
    if ok:
        second = await read({"etag": first.etag, "last_modified": first.last_modified,
                             "content_hash": first.content_hash})
        report["second_read"] = {"not_modified": second.not_modified, "items": len(second.items),
                                 "gaps": [d.to_payload() for d in second.degradations]}
        ok = not second.degradations
    print(json.dumps({"ok": ok, **report}, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--type", choices=["rss", "greenhouse", "lever"], default="rss")
    p.add_argument("--url", default="", help="Public http(s) RSS/Atom URL (--type rss)")
    p.add_argument("--board", default="", help="Public board token (--type greenhouse|lever)")
    p.add_argument("--max-items", type=int, default=50)
    p.add_argument("--timeout", type=float, default=30)
    args = p.parse_args()
    if (args.type == "rss") != bool(args.url) or (args.type != "rss" and not args.board):
        p.error("rss needs --url; greenhouse and lever need --board")
    raise SystemExit(asyncio.run(main(args)))
