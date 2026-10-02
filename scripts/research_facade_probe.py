#!/usr/bin/env python3
"""Circuito de la Entrega A por la **API** y por la **fachada MCP**, con consumidores de prueba.

Que demuestra
=============
No es el runtime aislado en solitario (eso lo mide `research_circuit_probe.py`): aqui corre
el camino completo del servicio, con identidad y transporte reales.

    consumidor de prueba (JWT + `require_permissions`, o credencial de consumidor MCP)
      -> submit                             (router /api/research/jobs | tool MCP)
      -> servicio research                  (politica, presupuesto, slots, persistencia)
      -> despachador                        (claim con lease sobre Mongo real)
      -> worker aislado                     (contenedor con egress filtrado por destino)
      -> research_read del broker           (lectura HTTP real, evidencia persistida)
      -> sintesis citada                    (el informe cita el evidence_id)
      -> get                                (mismo consumidor, mismo job)

Sin buscador configurado (no hay clave de Brave/Serper) las fuentes se aportan como
`seed_urls`; el grounding pagado esta apagado y responde `missing_config`.

Uso:
    ./.venv/bin/python scripts/research_facade_probe.py --path both \\
        --backend container --bind 0.0.0.0 --gateway 172.31.240.1 --run-as 1002:1002
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
TESTS = REPO / "tests"
for path in (str(CORE), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("HOME", "/home/develop")

PROJECT_KEY = "jardal-openceo"
OWNER_ID = "usr_facade_probe"
DB_NAME = "openceo_test_research_facade"


def _settings(db_name: str, args: argparse.Namespace, state_dir: Path, broker_url: str):
    import research_support as support

    base = support.test_settings(db_name)
    overrides = {
        "RESEARCH_ENABLED": True,
        "RESEARCH_OPENCODE_CLI_PATH": "/home/develop/.opencode/bin/opencode",
        "RESEARCH_OPENCODE_TEST_MODEL": args.model,
        "RESEARCH_STATE_DIR": str(state_dir),
        "RESEARCH_EVIDENCE_DIR": str(state_dir / "evidence"),
        "RESEARCH_BROKER_ENABLED": True,
        "RESEARCH_BROKER_BEARER": "facade-probe-broker-token",
        "RESEARCH_BROKER_URL": broker_url,
        "RESEARCH_BROKER_ALLOWED_IPS": "127.0.0.1/32,::1/128,172.31.240.0/24",
        "RESEARCH_ISOLATION_BACKEND": args.backend,
        "RESEARCH_WORKER_IMAGE": "openceo-research-worker:latest",
        "RESEARCH_WORKER_NETWORK": args.network,
        "RESEARCH_WORKER_NETWORK_GATEWAY": args.gateway,
        "RESEARCH_WORKER_EGRESS_HOSTS": args.egress_hosts,
        "RESEARCH_WORKER_RUN_AS": args.run_as,
        # Sin buscador: las fuentes vienen en `seed_urls` y no se gasta en busquedas.
        "RESEARCH_QUICK_MAX_SEARCHES": 0,
        "RESEARCH_QUICK_MAX_PAGES": 3,
    }
    if hasattr(base, "model_copy"):
        return base.model_copy(update=overrides)
    for key, value in overrides.items():  # pragma: no cover - pydantic v1
        setattr(base, key, value)
    return base


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opencode/muse-spark-1.3-contributor-free")
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--port", type=int, default=18899)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--backend", default="", choices=["", "bwrap", "unshare", "container"])
    parser.add_argument("--network", default="openceo-research-egress")
    parser.add_argument("--gateway", default="")
    parser.add_argument("--egress-hosts", default="opencode.ai")
    parser.add_argument("--run-as", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--path", default="both", choices=["both", "facade", "api"])
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    import httpx
    import research_support as support
    import uvicorn
    from fastapi import FastAPI
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from pymongo import MongoClient

    from auth.jwt import create_access_token
    from auth.service import create_default_user_if_needed
    from research.consumers import ConsumerRegistry
    from research.mcp_facade import build_research_mcp_facade
    from research.router import build_research_router
    from research.wiring import build_research_service, close_research_service

    report: dict[str, object] = {"ok": False, "steps": []}
    if not support.mongo_available():
        report["error"] = "Mongo no disponible"
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
        return 2

    state_dir = Path(tempfile.mkdtemp(prefix="research-facade-"))
    broker_url = f"http://127.0.0.1:{args.port}/internal/mcp-research-broker/"
    settings = _settings(DB_NAME, args, state_dir, broker_url)

    repo = support.build_repo(DB_NAME)
    await repo.connect()
    service, resources = build_research_service(settings, repo)
    available, reason = service.available()
    report["service_available"] = [available, reason]
    if not available:
        report["error"] = f"servicio no disponible: {reason}"
        print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
        return 2

    # Politica del proyecto: en un despliegue vive en `project.yaml`. Aqui se entrega
    # como configuracion de prueba para no cambiar el proyecto real desde un probe.
    project_cfg = {
        "research": {
            "enabled": True,
            "classification_ceiling": "public",
            "allowed_purposes": ["facade-probe", "brief", "monitor"],
        }
    }

    registry = ConsumerRegistry(ttl_seconds=1800)
    consumer_token = registry.register(
        consumer_id="jobhunter", owner_id=OWNER_ID, projects=[PROJECT_KEY]
    )
    facade_server = build_research_mcp_facade(
        consumers=registry,
        get_service=lambda: service,
        project_cfg_for=lambda _project_key: project_cfg,
    )
    facade_app = facade_server.http_app(path="/")

    # Identidad real para la via API: un usuario en Mongo y un JWT firmado con el mismo
    # secreto del proyecto, verificado por la misma dependencia `require_permissions`.
    auth_client = MongoClient(settings.MONGODB_URL)
    auth_db = auth_client[DB_NAME]
    api_user_id = "usr_facade_probe_api"
    if args.path in {"both", "api"}:
        auth_db.users.delete_many({"user_id": api_user_id})
        auth_db.users.insert_one(
            {
                "user_id": api_user_id,
                "username": "facade-probe",
                "email": "facade-probe@jardal.local",
                "password_hash": "x",
                "is_active": True,
                "is_admin": False,
                "group_ids": [],
                "all_permissions": ["run_research", "read_research"],
                # Sin proyecto autorizado la politica rechaza la peticion aunque el
                # permiso exista: la autoridad es (permiso, proyecto), no solo permiso.
                "allowed_projects": [PROJECT_KEY],
            }
        )
    create_default_user_if_needed(auth_db)

    @asynccontextmanager
    async def lifespan(_app):
        # Cada montura tiene su propio ciclo de vida: sin entrar el de la fachada, su
        # gestor de sesiones MCP no arranca y `initialize` responde 500.
        if service._deps.events is not None:
            await service._deps.events.initialize()
        async with resources.broker.mount.lifespan():
            async with facade_app.router.lifespan_context(_app):
                yield

    app = FastAPI(lifespan=lifespan)
    app.state.research_service = service
    app.state.auth_db = auth_db
    app.mount(resources.broker.path, resources.broker.mount.app)
    if args.path in {"both", "facade"}:
        app.mount("/facade", facade_app)
    if args.path in {"both", "api"}:
        app.include_router(
            build_research_router(
                get_service=lambda request: getattr(request.app.state, "research_service", None),
                project_cfg_for=lambda _request, _project_key: project_cfg,
            )
        )

    uv = uvicorn.Server(
        uvicorn.Config(app, host=args.bind, port=args.port, log_level="warning")
    )
    uv_task = asyncio.ensure_future(uv.serve())
    for _ in range(100):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    if not uv.started:
        raise RuntimeError("la app no arranco")

    base = f"http://127.0.0.1:{args.port}"
    report["broker_url"] = resources.broker.url
    report["facade_url"] = f"{base}/facade/"
    report["api_base"] = f"{base}/api/research"

    request_body = {
        "project_key": PROJECT_KEY,
        "purpose": "facade-probe",
        "question": f"Describe el dominio de {args.url}",
        "mode": "quick",
        "seed_urls": [args.url],
    }

    async def dispatch(research_id: str) -> float:
        """Despacho: el mismo `run_once` que usa el despachador del servicio."""
        started_at = time.time()
        while time.time() - started_at < args.timeout:
            document = await service.run_once()
            if document is not None:
                return time.time() - started_at
            await asyncio.sleep(0.5)
        return -1.0

    try:
        if args.path in {"both", "facade"}:
            transport = StreamableHttpTransport(
                url=report["facade_url"],
                headers={"Authorization": f"Bearer {consumer_token}"},
                httpx_client_factory=lambda **kw: httpx.AsyncClient(**kw),
            )
            async with Client(transport) as client:
                submitted = await client.call_tool("openceo_research_submit", dict(request_body))
            report["submit"] = submitted.data
            facade_id = str(submitted.data.get("research_id") or "")
            if not submitted.data.get("ok"):
                report["error"] = f"admision por fachada rechazada: {submitted.data}"
            else:
                report["steps"].append("facade_submitted")
                report["facade_dispatch_s"] = round(await dispatch(facade_id), 1)
                async with Client(transport) as client:
                    fetched = await client.call_tool(
                        "openceo_research_get", {"research_id": facade_id}
                    )
                report["get"] = fetched.data
                report["facade_job"] = await service.status(facade_id)

        if args.path in {"both", "api"}:
            jwt = create_access_token(api_user_id, "facade-probe")
            async with httpx.AsyncClient(
                timeout=30, cookies={"openceo_session": jwt}
            ) as api:
                submitted_api = await api.post(f"{report['api_base']}/jobs", json=request_body)
                report["api_submit_status"] = submitted_api.status_code
                report["api_submit"] = (
                    submitted_api.json() if submitted_api.content else {}
                )
                api_id = str(report["api_submit"].get("research_id") or "")
                if api_id:
                    report["steps"].append("api_submitted")
                    report["api_dispatch_s"] = round(await dispatch(api_id), 1)
                    fetched_api = await api.get(f"{report['api_base']}/jobs/{api_id}")
                    report["api_get_status"] = fetched_api.status_code
                    report["api_job"] = fetched_api.json() if fetched_api.content else {}
    finally:
        uv.should_exit = True
        await uv_task
        await close_research_service(resources)
        if hasattr(repo, "disconnect"):
            await repo.disconnect()
        auth_client.close()

    facade_job = report.get("facade_job") or {}
    api_job = report.get("api_job") or {}

    def _summary(job: dict) -> dict:
        result = job.get("result") or {}
        evidence = result.get("evidence") or []
        claims = result.get("claims") or []
        cited = {
            evidence_id
            for claim in claims
            for evidence_id in (claim.get("evidence_ids") or [])
        }
        known = {row.get("id") for row in evidence}
        return {
            "status": job.get("status"),
            "claims": len(claims),
            "evidence": [
                {
                    "id": row.get("id"),
                    "url": row.get("url"),
                    "content_hash": row.get("content_hash"),
                    "observed_at": row.get("observed_at"),
                    "access_method": row.get("access_method"),
                }
                for row in evidence
            ],
            "cited_evidence_ids": sorted(cited),
            "citations_resolve_to_job_evidence": bool(cited) and cited <= known,
            "coverage_gaps": [gap.get("reason") for gap in (result.get("coverage_gaps") or [])],
        }

    if facade_job:
        report["facade"] = _summary(facade_job)
    if api_job:
        report["api"] = _summary(api_job)

    wanted: list[bool] = []
    if args.path in {"both", "facade"}:
        summary = report.get("facade") or {}
        facade_ok = bool(
            summary.get("status") == "completed"
            and summary.get("evidence")
            and summary.get("citations_resolve_to_job_evidence")
            and summary.get("claims")
        )
        report["facade_ok"] = facade_ok
        wanted.append(facade_ok)
    if args.path in {"both", "api"}:
        summary = report.get("api") or {}
        api_ok = bool(
            report.get("api_submit_status") == 202
            and summary.get("status") == "completed"
            and summary.get("citations_resolve_to_job_evidence")
            and summary.get("claims")
        )
        report["api_ok"] = api_ok
        wanted.append(api_ok)
    report["ok"] = all(wanted) if wanted else False
    report["model"] = args.model
    report["isolation_backend"] = args.backend or "auto"

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str)[:6000], flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
