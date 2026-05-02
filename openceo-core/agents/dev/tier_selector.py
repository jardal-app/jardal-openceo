from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from settings import Settings
from state import CodingTier

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# System prompt — benchmark-backed + operational-reality routing
# ────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are an expert AI routing system. Given a software engineering task \
(title + description), decide which execution tier will produce the \
highest-quality result.

## The four tiers

### tier1 — Best individual EXECUTOR / OPERATOR
Engine: Codex CLI · GPT-5.4 xhigh reasoning.
Benchmarks: Terminal-Bench 2.0 75.1% (leader), OSWorld 75.0%, \
BrowseComp 82.7%, MCP Atlas 67.2%, Toolathlon 54.6%, \
SWE-bench Pro 57.7% (leader).
Choose when the BOTTLENECK IS EXECUTION:
- Implementing a feature with a clear spec (build-heavy).
- Terminal / DevOps / CI-CD / Docker / shell / MCP / infrastructure.
- Complex frontend / UI work (GPT-5.4 excels at frontend).
- Bug operativo with clear repro steps.
- Relatively linear, single-threaded tickets where one coherent model \
  is better than coordinating several.
- Browser or desktop automation (native computer-use).
- Quick prototyping, scripts, tooling tasks.

### tier2 — Best individual THINKER / REVIEWER
Engine: Claude Code · Opus 4.6 extended thinking.
Benchmarks: SWE-bench Verified 80.8% (leader), Terminal-Bench 65.4%, \
OSWorld 72.7%, OpenRCA 34.9%, ARC-AGI-2 68.8% (leader, novel reasoning).
Choose when the BOTTLENECK IS THINKING CORRECTLY:
- Delicate refactoring across many files / modules (1M context, best \
  codebase comprehension).
- Migration, security, billing, auth, concurrency — high-stakes code \
  where getting it wrong is costly.
- Subtle bug with uncertain root cause (forensics / investigation).
- Architecture design, API design, type system work.
- Deep code review of complex PRs.
- Ambiguous or nuanced task description that requires interpretation.
- Any ticket where the main risk is making the wrong technical decision.

### tier3 — Best COORDINATOR for decomposable mini-projects
Engine: OpenCode multi-model team (one-shot, not live session).
Team roles: Lead M2.7, Architect GLM-5, Builders M2.5, \
Visual/Kimi K2.5, Runtime-review M2.7.
Component benchmarks: M2.5 80.2% SWE-bench Verified, \
GLM-5 77.8% SWE-bench, Kimi K2.5 76.8% SWE-bench + 78.4% BrowseComp \
Agent Swarm, M2.7 56.22% SWE-bench Pro + 57.0% Terminal-Bench 2.
Choose ONLY when the ticket is DECOMPOSABLE into parallel workstreams:
- Architecture + implementation + tests/docs + runtime validation.
- Backend + UI + Docker/MCP in the same ticket.
- Work that benefits from screenshots/PDF/video analysis (visual-analyst).
- Mini-project with at least 2 real independent lanes of work AND \
  preferably material for runtime-review or visual-analyst roles.

### tier4 — Best ITERATIVE REVIEWER / VISUAL CRAFTSMAN
Engine: Kimi CLI · Kimi-k2.6 with thinking mode.
Benchmarks: Strong on UI/UX polish, visual analysis, iterative refinement,
long-context comprehension (262k tokens), and interactive review loops.
Choose when the BOTTLENECK IS ITERATIVE REFINEMENT OR VISUAL QUALITY:
- UI/UX pixel-perfect adjustments, CSS fine-tuning, responsive design.
- Tasks requiring visual analysis (screenshots, PDFs, video review).
- QA-heavy workflows or approval chains needing human-in-the-loop review.
- Tickets that benefit from session persistence and resume (multi-turn).
- Any ticket explicitly tagged or requested for Kimi.

## CRITICAL GUARDRAILS

1. tier3 is NEVER a fallback or default. Only choose tier3 if you can \
   identify at least 2 real parallel workstreams in the ticket.
2. tier4 is NEVER a fallback or default. Only choose tier4 if the ticket \
   clearly benefits from Kimi's iterative review, visual analysis, or \
   long-context strengths.
3. If the ticket is difficult but NOT decomposable → tier2 wins.
4. If the ticket is clear but execution-heavy → tier1 wins.
5. When in doubt between tier1 and tier2:
   - Choose tier2 if the bottleneck is investigating / deciding.
   - Choose tier1 if the bottleneck is building / executing.
6. When in doubt about tier3 or tier4: do NOT choose them. Fall to tier1 or tier2.

## Scoring dimensions (evaluate mentally, do not output scores)
- Clarity of specification (clear → tier1, ambiguous → tier2)
- Architectural uncertainty (high → tier2)
- Terminal/tools/runtime intensity (high → tier1)
- Visual/frontend need (high → tier1)
- Number of parallelizable workstreams (≥2 real lanes → tier3 candidate)
- Breadth of surfaces: UI/API/DB/tests/runtime (broad + decomposable → tier3)
- Consistency risk if multiple hands touch the same code (high → NOT tier3)
- Correctness risk if the model reasons wrong (high → tier2)

## Output
Return ONLY valid JSON: {"tier": "tier1"} or {"tier": "tier2"} or {"tier": "tier3"} or {"tier": "tier4"}
Optionally include a one-line rationale: {"tier": "tier2", "reason": "..."}

The user speaks Spanish. Understand Spanish task titles and descriptions.\
"""


class TierSelector:
    """LLM-backed intelligent tier selection with keyword fallback."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._llm: ChatOpenAI | None = None
        if settings and settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
            )

    # ── public API ───────────────────────────────────────────────

    async def select(
        self,
        config_tier: str,
        title: str,
        description: str,
    ) -> CodingTier:
        """Pick the best coding tier for a task.

        If *config_tier* is an explicit tier (tier1/tier2/tier3/tier4) return it
        directly.  Otherwise use an LLM to analyse the task, falling back
        to keyword heuristics if the LLM is unavailable or fails.
        """
        normalized = (config_tier or "auto").strip().lower()
        if normalized in ("tier1", "tier2", "tier3", "tier4"):
            return CodingTier(normalized)

        # ── LLM-based selection ──────────────────────────────────
        if self._llm is not None:
            tier = await self._select_with_llm(title, description)
            if tier is not None:
                return tier

        # ── keyword fallback (legacy) ────────────────────────────
        return self._select_by_keywords(title, description)

    # ── LLM selector ────────────────────────────────────────────

    async def _select_with_llm(
        self,
        title: str,
        description: str,
    ) -> CodingTier | None:
        task_text = f"Title: {title}\nDescription: {description}".strip()
        try:
            response = await self._llm.ainvoke(  # type: ignore[union-attr]
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=task_text),
                ]
            )
            raw = str(response.content).strip()
            tier = self._parse_llm_response(raw)
            if tier is not None:
                logger.info(
                    "LLM tier selection: %s (response: %s)",
                    tier.value,
                    raw[:200],
                )
            return tier
        except Exception:
            logger.warning(
                "LLM tier selection failed, falling back to keywords",
                exc_info=True,
            )
            return None

    @staticmethod
    def _parse_llm_response(raw: str) -> CodingTier | None:
        cleaned = raw.strip()
        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()
        try:
            data = json.loads(cleaned)
            tier_val = str(data.get("tier", "")).strip().lower()
            if tier_val in ("tier1", "tier2", "tier3", "tier4"):
                return CodingTier(tier_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Fallback: scan for tier mention in raw text
        lowered = raw.lower()
        for candidate in ("tier1", "tier2", "tier3", "tier4"):
            if candidate in lowered:
                return CodingTier(candidate)
        return None

    # ── keyword fallback ─────────────────────────────────────────

    @staticmethod
    def _select_by_keywords(title: str, description: str) -> CodingTier:
        """Fallback when LLM is unavailable. Defaults to tier1, never tier3/tier4."""
        blob = f"{title}\n{description}".lower()

        # tier4 signals: kimi-specific / iterative review / QA-heavy
        tier4_signals = [
            "kimi", "iterativo", "revisión visual", "ui pixel-perfect",
            "qa manual", "flujo de aprobación", "ajuste visual",
        ]
        # tier2 signals: thinking-heavy / high-stakes / multi-file
        tier2_signals = [
            "refactor", "migration", "security", "auth", "billing",
            "concurrency", "race condition", "architecture", "redesign",
            "multi-file", "cross-module", "api design", "type system",
            "root cause", "forensic", "investigate", "review",
        ]
        # tier1 signals: execution-heavy / tooling / frontend / clear spec
        tier1_signals = [
            "feature", "bug", "fix", "implement", "add", "create",
            "devops", "ci/cd", "pipeline", "deploy", "infrastructure",
            "terminal", "shell", "bash", "nginx", "docker", "systemd",
            "frontend", "ui", "css", "react", "vue", "component",
            "script", "automation", "mcp", "tool",
        ]

        if any(token in blob for token in tier4_signals):
            return CodingTier.TIER4
        if any(token in blob for token in tier2_signals):
            return CodingTier.TIER2
        if any(token in blob for token in tier1_signals):
            return CodingTier.TIER1
        # Default to tier1 (executor), NEVER tier3 or tier4 as fallback
        return CodingTier.TIER1
