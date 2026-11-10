"""Techo quick del servidor para jobs admitidos por tick (ticket #694).

Sin red viva ni modelos: la admision por tick no fija limites explicitos
(``request.limits`` None resuelve al techo quick del servidor), de modo que
``effective_limits.seconds`` alcanza ``RESEARCH_QUICK_MAX_SECONDS`` cuando el
proyecto lo permite (Demo B: quick.seconds 300). Una peticion API/MCP sin
limites explicitos sigue resolviendo a la escala del contrato bajo la
politica de proyecto a escala de contrato.

No prueba: cooldowns/rotacion, parser, flota ni cierre del epico.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from research.contracts import ResearchJobRequest, ResearchLimits  # noqa: E402
from research.execution import ResearchExecutionContext  # noqa: E402
from research.policies import RESEARCH_RUN, Principal, derive_effective_policy  # noqa: E402
from research.watch import WatchEngine  # noqa: E402
from settings import Settings  # noqa: E402

PROJECT = "jardal-openceo"
OWNER = "usr_tick_owner"
QUESTION = "Que releases nuevos publico jardal-openceo?"

# Proyecto Demo B: sin limites propios, el servidor decide el techo.
CFG_NO_PROJECT_LIMITS = {
    "research": {
        "enabled": True, "allowed_modes": ["quick"], "classification_ceiling": "public",
        "paid_budget_usd": "0.00",
        "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False},
    }
}
# Proyecto a escala de contrato: recorta al suelo 3/5/8/120.
CFG_CONTRACT_SCALE = {
    "research": {
        **CFG_NO_PROJECT_LIMITS["research"],
        "limits": {"quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120}},
    }
}


class _FakeRepo:
    def __init__(self) -> None:
        self.marked: list[tuple] = []

    async def list_pending_research_watch_events(self, sid: str, cap: int):
        pending = [
            {"event_key": "e1", "url": "https://example.org/jobs/v1"},
            {"event_key": "e2", "url": "https://example.org/jobs/v2"},
        ]
        return pending[:cap]

    async def mark_research_watch_events(self, sid: str, keys, status: str, **kw):
        self.marked.append((sid, tuple(keys), status, kw))
        return len(keys)


class _CapturingService:
    """Doble del servicio: deriva la politica real y devuelve un doc encolado."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.requests: list[ResearchJobRequest] = []

    async def submit(self, request: ResearchJobRequest, *, principal, project_cfg):
        self.requests.append(request)
        policy = derive_effective_policy(
            principal=principal, request=request,
            project_cfg=project_cfg, settings=self.settings,
        )
        return {
            "research_id": f"res_tick_{len(self.requests)}", "status": "queued",
            "_created": True, "effective_limits": policy.limits.model_dump(),
        }


def _principal() -> Principal:
    return Principal(
        user_id=OWNER, permissions=frozenset({RESEARCH_RUN}),
        allowed_projects=frozenset({PROJECT}),
    )


def _execution(project_cfg: dict) -> ResearchExecutionContext:
    return ResearchExecutionContext(
        project_key=PROJECT, owner_id=OWNER, purpose="demo-b",
        principal=_principal(), project_cfg=project_cfg, execution_key="tick-694",
        schedule={
            "schedule_id": "sub-demo-b", "workflow": "research.watch_tick",
            "params": {"question": QUESTION},
        },
    )


class TickCeilingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_admission_resolves_to_the_server_quick_ceiling(self):
        settings = Settings(_env_file=None, RESEARCH_QUICK_MAX_SECONDS=300)
        service = _CapturingService(settings)
        engine = WatchEngine(repo=_FakeRepo(), settings=settings, service=service)
        admitted = await engine._admit_pending(
            _execution(CFG_NO_PROJECT_LIMITS),
            {"schedule_id": "sub-demo-b", "params": {}}, QUESTION, prefix="watch",
        )
        self.assertIsNotNone(admitted)
        (request,) = service.requests
        # El tick no fija limites: el contrato efectivo resuelve al techo.
        self.assertIsNone(request.limits)
        policy = derive_effective_policy(
            principal=_principal(), request=request,
            project_cfg=CFG_NO_PROJECT_LIMITS, settings=settings,
        )
        self.assertEqual(
            policy.limits.model_dump(),
            {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 300},
        )

    async def test_api_request_without_limits_stays_at_contract_scale(self):
        settings = Settings(_env_file=None, RESEARCH_QUICK_MAX_SECONDS=300)
        request = ResearchJobRequest(
            project_key=PROJECT, purpose="brief", question="Pregunta", mode="quick",
        )
        self.assertIsNone(request.limits)
        policy = derive_effective_policy(
            principal=_principal(), request=request,
            project_cfg=CFG_CONTRACT_SCALE, settings=settings,
        )
        self.assertEqual(policy.limits, ResearchLimits())


if __name__ == "__main__":
    unittest.main()
