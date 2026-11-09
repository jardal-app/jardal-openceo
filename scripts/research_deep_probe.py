#!/usr/bin/env python3
"""Ten public-source reports on the free deep route by default.

No production activation or recurring jobs. Jobs/evidence live in a dedicated probe
database/directory; opt-in paid cases share the production account ledger.
Review every material claim in the saved results against its persisted excerpt;
schema checks and a model verifier do not establish real quality on their own.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

CASES = [
    ("brief-deepseek", "brief", "DeepSeek", "ingeniero de integración LLM", "https://api-docs.deepseek.com/"),
    ("brief-firecrawl", "brief", "Firecrawl", "ingeniero de extracción web", "https://docs.firecrawl.dev/introduction"),
    ("brief-github", "brief", "GitHub", "ingeniero de automatización CI", "https://github.com/features/actions"),
    ("brief-cloudflare", "brief", "Cloudflare", "ingeniero de aplicaciones edge", "https://developers.cloudflare.com/workers/"),
    ("brief-microsoft", "brief", "Microsoft", "ingeniero de funciones cloud", "https://learn.microsoft.com/en-us/azure/azure-functions/functions-overview"),
    ("event-python", "event", "Python 3.14.0", "", "https://www.python.org/downloads/release/python-3140/"),
    ("event-deepseek", "event", "anuncio DeepSeek del 10 de septiembre de 2026", "", "https://api-docs.deepseek.com/news/news260910/"),
    ("event-firecrawl", "event", "lanzamiento Firecrawl v2", "", "https://www.firecrawl.dev/blog/firecrawl-v2-series-a-announcement"),
    ("question-asyncio", "question", "¿Qué limitaciones de cancelación documenta asyncio y qué no garantiza?", "", "https://docs.python.org/3/library/asyncio-task.html"),
    ("question-pricing", "question", "¿Qué incertidumbres existen al estimar una factura con precios de tokens y caché?", "", "https://api-docs.deepseek.com/quick_start/pricing"),
]


def question(kind, subject, role):
    if kind == "brief":
        return (f"Brief sintético empresa/rol: {subject}, {role}. Resume hasta tres afirmaciones materiales sobre el producto "
                "documentado y tareas técnicas plausibles. Distingue hechos e inferencias. No afirmes que exista una vacante "
                "ni evalúes candidatos. No hay CV ni datos de persona. Cita extractos exactos.")
    if kind == "event":
        return f"Resume hasta tres afirmaciones sobre {subject}, con fecha declarada o desconocida. Atribuye declaraciones del anunciante; cita extractos exactos. No presentes un anuncio antiguo como noticia de hoy."
    return subject + " Responde con hasta tres afirmaciones, inferencias distinguidas y extractos exactos; declara los límites de estas fuentes."


def _probe_limits(paid_deepseek):
    from research.deep import FREE_DEEP_SECONDS
    return {"search_requests": 0, "pages": 1, "steps": 10,
            "seconds": 120 if paid_deepseek else FREE_DEEP_SECONDS}


async def main(args):
    if args.paid_deepseek and (not args.use_approved_budget or not args.case):
        raise SystemExit("Paid DeepSeek requires --use-approved-budget and explicit --case selections")
    from settings import get_settings
    from db.mongo import MongoRepository
    from research.budget import ResearchBudgetLedger, ResearchPolicyLedger
    from research.contracts import ResearchJobRequest
    from research.deep_model import BudgetedDeepModel
    from research.deep import FREE_DEEP_PHASE_SECONDS
    from research.policies import Principal
    from research.wiring import build_research_service
    from research.broker_mount import broker_url_for
    from research.money import parse_optional_micro
    root = Path(args.output).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = get_settings()
    if args.paid_deepseek and (not settings.RESEARCH_DEEP_API_KEY or settings.RESEARCH_DEEP_ACCOUNT_CEILING_USD in {"", "0", "0.00"}):
        raise SystemExit("Deep key and account ceiling must be configured in the official .env")
    account_repo = None
    isolated = settings.model_copy(update={"MONGODB_DATABASE": "openceo_test_research_deep_pilot",
        "RESEARCH_ENABLED": True, "RESEARCH_SCHEDULER_ENABLED": False,
        "RESEARCH_EVIDENCE_DIR": str(root / "evidence"), "RESEARCH_BROKER_ENABLED": False,
        "RESEARCH_BROKER_URL": settings.RESEARCH_BROKER_URL or broker_url_for(settings, settings.RESEARCH_BROKER_PATH),
        "RESEARCH_STATE_DIR": str(root / "runtime"), "RESEARCH_WORKSPACE_DIR": str(root / "workspaces"),
        "RESEARCH_MCP_ENABLED": False})
    repo = MongoRepository(isolated)
    await repo.connect()
    service, resources = build_research_service(isolated, repo)
    if args.paid_deepseek:
        account_repo = MongoRepository(settings)
        await account_repo.connect()
        ledger = ResearchPolicyLedger(ResearchBudgetLedger(accounts=account_repo.research_budget_accounts,
            usage=account_repo.research_usage), repo, parse_optional_micro(settings.RESEARCH_PAID_SPEND_USD) or 0,
            parse_optional_micro(settings.RESEARCH_DEEP_ACCOUNT_CEILING_USD) or 0)
        service._deps.ledger = ledger
        service._deps.deep_model = BudgetedDeepModel(isolated, ledger)
    await service._deps.events.initialize()
    principal = Principal(user_id="research-acceptance", permissions=frozenset({"run_research", "read_research"}),
                          allowed_projects=frozenset({"jardal-openceo"}))
    limits = _probe_limits(args.paid_deepseek)
    cfg = {"research": {"enabled": True, "allowed_modes": ["deep"], "classification_ceiling": "public",
        "paid_budget_usd": settings.RESEARCH_PAID_SPEND_USD, "adapters": {},
        "limits": {"deep": limits}}}
    manifest = {"model": settings.RESEARCH_DEEP_MODEL if args.paid_deepseek else None,
        "profile": None if args.paid_deepseek else "free-research",
        "route": "deepseek-http" if args.paid_deepseek else "free-router+opencode",
        "version": None, "version_pin": None if args.paid_deepseek else settings.RESEARCH_OPENCODE_VERSION_PIN,
        "rubric": "All material claims reviewed against persisted exact excerpts; no invented/foreign citations; contradictions retained; partial when insufficient; dates and attribution explicit.",
        "limits": limits, "phase_caps_seconds": None if args.paid_deepseek else FREE_DEEP_PHASE_SECONDS,
        "reconciled_micro_usd": None, "cases": []}
    try:
        for case_id, kind, subject, role, url in CASES:
            if args.case and case_id not in args.case:
                continue
            request = ResearchJobRequest(project_key="jardal-openceo", purpose="acceptance-d", mode="deep",
                deep_paid_opt_in=args.paid_deepseek,
                question=question(kind, subject, role), seed_urls=[url],
                limits=limits)
            job = await service.submit(request, principal=principal, project_cfg=cfg)
            # Probe database only: this never claims jobs belonging to production.
            began = time.monotonic()
            result = await service.run_once()
            (root / (case_id + ".json")).write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            row = {"case": case_id, "kind": kind, "research_id": job["research_id"],
                "status": result["status"], "seconds": time.monotonic() - began,
                "claims": len((result.get("result") or {}).get("claims", [])), "agent_reviewed": False}
            manifest["cases"].append(row)
            (root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
            print(json.dumps(row), flush=True)
        return 0
    finally:
        await resources.aclose()
        await repo.close()
        if account_repo is not None:
            await account_repo.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--use-approved-budget", action="store_true")
    p.add_argument("--paid-deepseek", action="store_true", help="Owner-named cases only; never an automatic fallback")
    p.add_argument("--output", required=True)
    p.add_argument("--case", choices=[c[0] for c in CASES], action="append", help="Repeat only a case affected by a measured defect")
    raise SystemExit(asyncio.run(main(p.parse_args())))
