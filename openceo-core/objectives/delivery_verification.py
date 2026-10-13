"""Generic non-dev delivery verification (TASK-2026-00216).

A completed non-dev child verifies when its structured result carries
the outputs its skill declares plus the evidence the skill requires.
Citations match normalized (lowercase, alphanumerics only), so
``journal_entry`` finds ``register_expense.journal_entry: JE-1``; the
agent sees the exact declared names in its prompt, so the strict check
is fair. A skill may ship a stricter ``verify.py``: it runs only after
the generic check passed, and a broken one is a gap, never a silent
fallback. Skills ship in git like any merged code, so a ``verify.py``
runs with the same trust; exceptions stay contained as gaps.

Missing verifiers still park as capability_gap and missing evidence as
data_gaps (TASK-2026-00175): this module only fills the registered map
``service.py`` builds, one generic entry per enabled AI agent.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "SkillSpec",
    "catalog_skill_resolver",
    "generic_agent_verifiers",
    "normalize_citation",
    "verify_agent_delivery",
]

#: Stricter per-skill check, loaded from the skill dir when present.
VERIFY_FILENAME = "verify.py"

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_citation(text: Any) -> str:
    """Citation form: lowercase alphanumerics, separators dropped."""
    return _NON_ALNUM_RE.sub("", str(text or "").lower())


@dataclass(frozen=True)
class SkillSpec:
    """What the generic check needs from one skill declaration."""

    outputs: list[str]
    evidence: list[str]
    source_dir: str | None = None


#: (agent, skill) -> declaration, None when unknown.
SkillResolver = Callable[[str, str], SkillSpec | None]


def catalog_skill_resolver(
    repo_root: str | Path | None = None,
) -> SkillResolver:
    """Resolver over the shipped role skills, cached per resolver."""
    from catalog import load_role_skills

    cache: dict[tuple[str, str], SkillSpec | None] = {}

    def resolve(agent: str, skill: str) -> SkillSpec | None:
        key = (agent, skill)
        if key not in cache:
            cache[key] = _load_spec(agent, skill, repo_root)
        return cache[key]

    def _load_spec(agent: str, skill: str,
                   root: str | Path | None) -> SkillSpec | None:
        try:
            found = load_role_skills(agent, f"skills/{agent}", root)
        except Exception as exc:  # noqa: BLE001 - unknown skill, gap later
            logger.warning("skill lookup %s/%s failed: %s", agent, skill,
                           exc)
            return None
        for item in found:
            if item.name == skill:
                return SkillSpec(outputs=list(item.outputs),
                                 evidence=list(item.evidence),
                                 source_dir=str(item.source))
        return None

    return resolve


def _report(ok: bool, missing: list[str],
            unmet: list[str]) -> dict[str, Any]:
    return {"ok": ok, "missing_artifacts": missing,
            "unmet_criteria": unmet}


def _pool_texts(verdict: Mapping[str, Any],
                record: Mapping[str, Any]) -> list[str]:
    """Every citation surface: verdict entregables + evidence + record."""
    pool: list[str] = []
    entregables = verdict.get("entregables")
    if isinstance(entregables, list):
        pool.extend(str(item) for item in entregables)
    pool.append(str(verdict.get("evidencia") or ""))
    texts = record.get("texts")
    if isinstance(texts, list):
        pool.extend(str(item) for item in texts)
    files = record.get("files")
    if isinstance(files, list):
        pool.extend(str(item.get("name") or "")
                    for item in files if isinstance(item, dict))
    pool.append(str(record.get("evidence") or ""))
    return [text for text in pool if text.strip()]


def _pool_evidence(verdict: Mapping[str, Any],
                   record: Mapping[str, Any]) -> list[str]:
    pool = [str(verdict.get("evidencia") or ""),
            str(record.get("evidence") or "")]
    return [text for text in pool if text.strip()]


async def _run_skill_verify(source_dir: str, child: Mapping[str, Any],
                            verdict: Mapping[str, Any],
                            record: Mapping[str, Any]) -> dict[str, Any]:
    """Stricter per-skill check, fail-closed to a gap."""
    target = Path(source_dir) / VERIFY_FILENAME
    if not target.is_file():
        return _report(True, [], [])
    name = (f"skill_verify_{re.sub(r'[^a-z0-9]+', '_', source_dir.lower())}")
    try:
        spec = importlib.util.spec_from_file_location(name, target)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {target}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - strict: broken is a gap
        return _report(False, [],
                       [f"skill verifier error: {type(exc).__name__}"])
    check = getattr(module, "verify", None)
    if not callable(check):
        return _report(False, [],
                       ["skill verifier error: no verify()"])
    try:
        answer = check(dict(child), {"verdict": dict(verdict),
                                     "record": dict(record)})
        if inspect.isawaitable(answer):
            answer = await answer
    except Exception as exc:  # noqa: BLE001 - strict: broken is a gap
        return _report(False, [],
                       [f"skill verifier error: {type(exc).__name__}"])
    if not isinstance(answer, Mapping) or "ok" not in answer:
        return _report(False, [],
                       ["skill verifier error: bad report"])
    missing = answer.get("missing_artifacts") or []
    unmet = answer.get("unmet_criteria") or []
    return _report(
        bool(answer.get("ok")) and not missing and not unmet,
        [str(item) for item in missing],
        [str(item) for item in unmet])


async def verify_agent_delivery(
    child: Mapping[str, Any], *,
    resolve_skill: SkillResolver,
) -> dict[str, Any]:
    """Generic delivery check for one completed non-dev child.

    A ``DeliveryVerifier`` once the skill resolver is bound: hecho
    verdict, every declared output cited in the result, every required
    evidence cited, then the skill's own ``verify.py`` when it ships
    one. Report shape follows TASK-2026-00175.
    """
    params = child.get("params")
    params = params if isinstance(params, Mapping) else {}
    agent = str(params.get("target_agent") or "").strip() \
        or str(child.get("agent_hint") or "").strip()
    skill = str(params.get("skill") or "").strip()
    verdict = params.get("agent_result")
    if not isinstance(verdict, dict) \
            or verdict.get("resultado") != "hecho":
        return _report(False, [], ["hecho verdict"])
    record = params.get("ticket_outputs")
    record = record if isinstance(record, Mapping) else {}
    spec = resolve_skill(agent, skill) if skill else None
    if spec is None:
        return _report(False, [], ["known skill"])
    if not spec.outputs:
        return _report(False, [], ["declared outputs"])
    pool = [normalize_citation(text)
            for text in _pool_texts(verdict, record)]
    missing = [name for name in spec.outputs
               if normalize_citation(name)
               and not any(normalize_citation(name) in text
                           for text in pool)]
    if missing:
        return _report(False, missing, [])
    evidence_pool = [normalize_citation(text)
                     for text in _pool_evidence(verdict, record)]
    unmet = [f"evidence: {name}" for name in spec.evidence
             if normalize_citation(name)
             and not any(normalize_citation(name) in text
                         for text in evidence_pool)]
    if unmet:
        return _report(False, [], unmet)
    if spec.source_dir:
        extra = await _run_skill_verify(spec.source_dir, child, verdict,
                                        record)
        if not extra["ok"]:
            return extra
    return _report(True, [], [])


def generic_agent_verifiers(
    catalog: Any | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Callable[[Mapping[str, Any]],
                        Awaitable[Mapping[str, Any]]]]:
    """One generic verifier per enabled AI agent (service map)."""
    from catalog import AgentType, load_catalog

    loaded = catalog if catalog is not None else load_catalog()
    resolve = catalog_skill_resolver(repo_root)
    return {name: partial(verify_agent_delivery, resolve_skill=resolve)
            for name, card in loaded.agents.items()
            if card.type is AgentType.AI and card.enabled}
