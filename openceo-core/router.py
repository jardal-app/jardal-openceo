from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from settings import Settings
from state import AgentName


_PREFIX_REGEX = re.compile(r"^@(?P<agent>[a-zA-Z_]+)\b")
_PM_REQUIREMENT_PATTERNS = (
    r"\b(?:el\s+)?cliente\s+(?:quiere|necesita|pide|pidio|pidió|requiere)\b",
    r"\b(?:el\s+)?sistema\s+(?:debe|deberia|debería|necesita|requiere|incluye|tendra|tendrá)\b",
    r"\b(?:requerimientos?|requisitos?|alcance|casos?\s+de\s+uso)\b",
)
_PM_FEATURE_HINTS = (
    "login",
    "dashboard",
    "roles",
    "admin",
    "auditoria",
    "auditoría",
    "usuarios",
    "usuario",
    "api",
    "integracion",
    "integración",
    "modulo",
    "módulo",
    "pantalla",
    "pantallas",
    "reporte",
    "reportes",
    "permisos",
    "notificaciones",
    "facturacion",
    "facturación",
    "inventario",
    "catalogo",
    "catálogo",
)
_PM_LIST_ITEM_REGEX = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S")


class CEORouter:
    """Agent selector. Uses direct hints, project-config rules, then optional LLM fallback."""

    AGENT_ALIASES = {
        "dev": AgentName.DEV,
        "developer": AgentName.DEV,
        "devops": AgentName.DEV,
        "pm": AgentName.PM,
        "marketing": AgentName.MARKETING,
        "finance": AgentName.FINANCE,
        "secretary": AgentName.SECRETARY,
        "sales": AgentName.SALES,
        "media": AgentName.MEDIA,
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: ChatOpenAI | None = None

        if settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
            )

    async def select_agent(
        self,
        title: str,
        description: str,
        agent_hint: str | None = None,
        project_config: dict[str, Any] | None = None,
    ) -> AgentName:
        if agent_hint:
            hinted = self.AGENT_ALIASES.get(agent_hint.lower())
            if hinted:
                return hinted

        text = f"{title}\n{description}".strip()

        prefixed = self._extract_prefixed_agent(text)
        if prefixed:
            return prefixed

        from_config = self._classify_by_config(text, project_config)
        if from_config is not None:
            return from_config

        if self._looks_like_pm_payload(text):
            return AgentName.PM

        if self._llm is not None and self._llm_fallback_enabled(project_config):
            by_llm = await self._classify_with_llm(text)
            if by_llm:
                return by_llm

        return self._default_agent(project_config)

    def _extract_prefixed_agent(self, text: str) -> AgentName | None:
        match = _PREFIX_REGEX.match(text)
        if not match:
            return None
        return self.AGENT_ALIASES.get(match.group("agent").lower())

    async def _classify_with_llm(self, text: str) -> AgentName | None:
        if self._llm is None:
            return None

        instruction = (
            "Classify the task into one agent: dev, pm, marketing, finance, secretary, sales, media. "
            "pm = project management: extracting tickets from interviews or requirements. "
            "Return strict JSON with key 'agent'. "
            "The user speaks Spanish. Understand Spanish input."
        )

        try:
            response = await self._llm.ainvoke(
                [
                    SystemMessage(content=instruction),
                    HumanMessage(content=text),
                ]
            )
            payload = json.loads(response.content)
            raw = str(payload.get("agent", "")).strip().lower()
            return self.AGENT_ALIASES.get(raw)
        except Exception:
            return None

    def _classify_by_config(self, text: str, project_config: dict[str, Any] | None) -> AgentName | None:
        blob = text.lower()
        routing = self._routing_config(project_config)
        agents_cfg = routing.get("agents")
        if not isinstance(agents_cfg, dict):
            return None

        for raw_agent, raw_cfg in agents_cfg.items():
            agent = self.AGENT_ALIASES.get(str(raw_agent).lower())
            if agent is None or not isinstance(raw_cfg, dict):
                continue

            triggers = raw_cfg.get("triggers")
            if isinstance(triggers, list):
                for raw_trigger in triggers:
                    trigger = str(raw_trigger or "").strip().lower()
                    if trigger and self._matches_trigger(blob, trigger):
                        return agent

            patterns = raw_cfg.get("patterns")
            if isinstance(patterns, list):
                for raw_pattern in patterns:
                    pattern = str(raw_pattern or "").strip()
                    if not pattern:
                        continue
                    try:
                        if re.search(pattern, text, flags=re.IGNORECASE):
                            return agent
                    except re.error:
                        continue

        return None

    @staticmethod
    def _routing_config(project_config: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(project_config, dict):
            return {}
        routing = project_config.get("routing")
        return routing if isinstance(routing, dict) else {}

    @staticmethod
    def _matches_trigger(blob: str, trigger: str) -> bool:
        if " " in trigger or "-" in trigger:
            return trigger in blob
        return re.search(rf"\b{re.escape(trigger)}\b", blob, flags=re.IGNORECASE) is not None

    @staticmethod
    def _looks_like_pm_payload(text: str) -> bool:
        raw = str(text or "").strip()
        if not raw:
            return False
        if raw.isdigit():
            return False

        normalized = " ".join(raw.lower().split())
        if len(normalized) < 40:
            return False

        feature_hits = sum(
            1
            for hint in _PM_FEATURE_HINTS
            if re.search(rf"\b{re.escape(hint)}\b", normalized, flags=re.IGNORECASE)
        )
        structured = (
            _PM_LIST_ITEM_REGEX.search(raw) is not None
            or any(token in raw for token in ("\n", ";", ":"))
        )

        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in _PM_REQUIREMENT_PATTERNS):
            return structured or feature_hits >= 1 or len(normalized) >= 80

        if structured and feature_hits >= 2:
            return True
        return feature_hits >= 3 and len(normalized) >= 60

    def _llm_fallback_enabled(self, project_config: dict[str, Any] | None) -> bool:
        routing = self._routing_config(project_config)
        value = routing.get("llm_fallback", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _default_agent(self, project_config: dict[str, Any] | None) -> AgentName:
        routing = self._routing_config(project_config)
        raw = str(routing.get("default_agent") or "dev").strip().lower()
        return self.AGENT_ALIASES.get(raw, AgentName.DEV)
