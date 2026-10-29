"""Deterministic diagnosis bundle, closed output schema and policy.

Design v2 sections 8.2, 8.4 and 15.3 (epic TASK-2026-00365, ticket
TASK-2026-00381). Pure functions only: no I/O, no clock, no model calls.
Code excerpts and protocol extracts arrive through injected readers and
allowlist maps, so tests use fakes and the runner user never grants the
diagnosis process any tool.

A bundle carries the Problem summary, up to five incidents (first, last
and up to three diverse from another release or project), per-kind
material, project context only for project scope, and a manifest with
source, hash, truncation and omission reason per piece. Free text travels
exclusively inside delimited data blocks: it is evidence and never an
instruction. When the essential evidence does not fit the budget, the
bundle reports insufficient evidence and the model is never called.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

SCHEMA_VERSION = 1
BUNDLE_VERSION = 1

MAX_BUNDLE_BYTES = 65536
MAX_INPUT_TOKENS_ESTIMATED = 12000
MAX_INCIDENTS = 5
MAX_EXCEPTION_FRAMES = 5
CODE_CONTEXT_LINES = 15
SEALED_PIECE_RESERVE = 512

CLASSIFICATIONS = frozenset({
    "engine_bug", "protocol_gap", "retry_policy_gap",
    "timer_or_admission_gap", "project_defect", "environment",
    "task_quality", "insufficient_evidence",
})

OUTPUT_ATTRIBUTIONS = frozenset({
    "engine", "project", "environment", "task", "unknown",
})

CONFIDENCES = frozenset({"confirmed", "supported", "weak"})

ALTERNATIVE_STATUSES = frozenset({"not_supported", "open"})

TICKET_TYPES = frozenset({"fix", "investigate", "none"})

RECOVERIES = frozenset({
    "resume_after_fix", "reconcile_business_state", "needs_owner", "none",
})

INSUFFICIENT_EVIDENCE = "insufficient_evidence"
NEEDS_SPLIT = "needs_split"
LINK = "link"
INVESTIGATE = "investigate"
FIX = "fix"
NO_ACTION = "no_action"

DATA_BEGIN = "<<<EVIDENCE-DATA"
DATA_END = "EVIDENCE-DATA>>>"
FENCE_REPLACEMENT = "[FENCE]"

EVIDENCE_ROLE = "data"

_PROJECT_MENTION = re.compile(r"project:([A-Za-z0-9][A-Za-z0-9_-]*)")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return len(text.encode("utf-8")) // 4


def wrap_data(text: str) -> str:
    return f"{DATA_BEGIN}\n{str(text).replace(DATA_END, FENCE_REPLACEMENT)}\n{DATA_END}"


def contains_cross_project_reference(text: str, scope: str) -> bool:
    own = scope.split(":", 1)[1] if scope.startswith("project:") else None
    return any(key != own for key in _PROJECT_MENTION.findall(text or ""))


def _created(value: Any) -> str:
    return str(getattr(value, "isoformat", lambda: str(value))())


def select_incidents(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(incidents, key=lambda doc: (
        _created(doc.get("created_at")), str(doc.get("_id") or "")))
    if len(ordered) <= MAX_INCIDENTS:
        return ordered
    first, last = ordered[0], ordered[-1]
    seen_keys = {
        (str((first.get("release") or {}).get("sha")),
         str(((first.get("engine") or {}).get("project_key")
              or first.get("project_key") or ""))),
        (str((last.get("release") or {}).get("sha")),
         str(((last.get("engine") or {}).get("project_key")
              or last.get("project_key") or ""))),
    }
    seen_ids = {first.get("_id"), last.get("_id")}
    diverse: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for doc in ordered[1:-1]:
        if doc.get("_id") in seen_ids:
            continue
        seen_ids.add(doc.get("_id"))
        key = (str((doc.get("release") or {}).get("sha")),
               str(((doc.get("engine") or {}).get("project_key")
                    or doc.get("project_key") or "")))
        if key not in seen_keys:
            seen_keys.add(key)
            diverse.append(doc)
        else:
            fallback.append(doc)
    picked = (diverse + fallback)[:MAX_INCIDENTS - 2]
    return [first, *picked, last]


def _manifest(ref: str, source: str, content: str, *,
              truncated: bool = False,
              omission: str | None = None,
              note: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ref": ref, "source": source, "sha256": _sha256(content),
        "truncated": bool(truncated), "omission": omission,
    }
    if note is not None:
        entry["note"] = note
    return entry


def _problem_summary(problem: dict[str, Any]) -> dict[str, Any]:
    counts = problem.get("counts") or {}
    return {
        "key": str(problem.get("_id") or problem.get("problem_key") or ""),
        "scope": str(problem.get("scope") or ""),
        "kind": str(problem.get("kind") or ""),
        "component": str(problem.get("component") or ""),
        "attribution": str(problem.get("attribution") or ""),
        "attribution_rule": problem.get("attribution_rule"),
        "counts": {
            "occurrences": int(counts.get("occurrences", 0)),
            "distinct_tickets": int(counts.get("distinct_tickets", 0)),
            "distinct_projects": int(counts.get("distinct_projects", 0)),
        },
        "releases_seen": list(problem.get("releases_seen") or []),
        "severity_max": str(problem.get("severity_max") or ""),
    }


def _incident_core(doc: dict[str, Any]) -> dict[str, Any]:
    engine = doc.get("engine") or {}
    return {
        "ref": f"incident:{doc.get('_id')}",
        "kind": str(doc.get("kind") or ""),
        "component": str(doc.get("component") or ""),
        "severity": str(doc.get("severity") or ""),
        "created_at": _created(doc.get("created_at")),
        "release": dict(doc.get("release") or {}),
        "task_id": engine.get("task_id"),
        "project_key": engine.get("project_key") or doc.get("project_key"),
    }


def _normalized_stack(frames: Any) -> list[str]:
    stack: list[str] = []
    for frame in frames or []:
        if isinstance(frame, str):
            stack.append(frame.split(":")[0] if ":" not in frame
                        else ":".join(frame.split(":")[:2]))
        elif isinstance(frame, dict):
            stack.append(
                f"{frame.get('module') or '?'}:{frame.get('function') or '?'}")
    return stack


def _raw_in_app_frames(doc: dict[str, Any]) -> list[dict[str, str]]:
    from .sanitize import is_in_app_frame

    raw = doc.get("frames_raw")
    frames = raw if isinstance(raw, list) else []
    return [frame for frame in frames
            if isinstance(frame, dict)
            and is_in_app_frame(str(frame.get("file") or ""))]


def _code_excerpt(source: str | None, path: str, line: Any) -> str | None:
    if source is None:
        return None
    try:
        number = int(str(line))
    except (TypeError, ValueError):
        return None
    lines = source.splitlines()
    if not 1 <= number <= len(lines):
        return None
    low = max(0, number - 1 - CODE_CONTEXT_LINES)
    high = min(len(lines), number + CODE_CONTEXT_LINES)
    return "\n".join(f"{index + 1}:{lines[index]}"
                     for index in range(low, high))


def manifest_refs(bundle: dict[str, Any]) -> set[str]:
    return {str(entry.get("ref"))
            for entry in bundle.get("manifest", [])
            if isinstance(entry, dict) and entry.get("ref")}


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_bundle(
    problem: dict[str, Any],
    incidents: list[dict[str, Any]],
    *,
    code_reader: Callable[[str, str], str | None] | None = None,
    protocol_map: dict[str, str] | None = None,
    brake_values: dict[str, Any] | None = None,
    project_context: str | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    max_bytes = int((limits or {}).get("max_bytes", MAX_BUNDLE_BYTES))
    max_tokens = int((limits or {}).get("max_tokens",
                                       MAX_INPUT_TOKENS_ESTIMATED))
    scope = str(problem.get("scope") or "")
    summary = _problem_summary(problem)
    chosen = select_incidents([doc for doc in incidents
                               if isinstance(doc, dict)])
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "problem": summary,
        "incidents": [],
        "manifest": [_manifest("problem:summary", "problem",
                               canonical_json(summary))],
        "byte_size": 0,
        "tokens_estimated": 0,
        "sufficient": True,
    }

    def sealed_size(extra: str = "") -> int:
        text = canonical_json(
            {"problem": bundle["problem"],
             "incidents": bundle["incidents"],
             "manifest": bundle["manifest"]})
        return len((text + extra).encode("utf-8"))

    def fits(text: str) -> bool:
        grown = sealed_size(text) + SEALED_PIECE_RESERVE
        return grown <= max_bytes and grown // 4 <= max_tokens

    def omit(ref: str, source: str, reason: str,
             note: str | None = None) -> None:
        bundle["manifest"].append(_manifest(ref, source, "", omission=reason,
                                            note=note))

    if not chosen:
        bundle["sufficient"] = False
        bundle["decision_hint"] = INSUFFICIENT_EVIDENCE
        return _seal(bundle)
    essential = [_incident_core(chosen[0])]
    if len(chosen) > 1:
        essential.append(_incident_core(chosen[-1]))
    essential_text = canonical_json(
        {"problem": summary, "incidents": essential,
         "manifest": bundle["manifest"]})
    if len(essential_text.encode("utf-8")) > max_bytes \
            or len(essential_text.encode("utf-8")) // 4 > max_tokens:
        bundle["sufficient"] = False
        bundle["decision_hint"] = INSUFFICIENT_EVIDENCE
        return _seal(bundle)
    for doc in chosen:
        core = _incident_core(doc)
        core_text = canonical_json(core)
        if not fits(core_text):
            omit(core["ref"], "incident", "over_budget")
            continue
        bundle["incidents"].append(core)
        bundle["manifest"].append(_manifest(core["ref"], "incident",
                                            core_text))
        _add_material(bundle, doc, core["ref"], scope,
                      code_reader=code_reader,
                      protocol_map=protocol_map,
                      brake_values=brake_values, fits=fits, omit=omit)
    _add_project_context(bundle, scope, project_context, fits, omit)
    present = {str(item.get("ref")) for item in bundle["incidents"]}
    for doc in chosen:
        ref = f"incident:{doc.get('_id')}"
        if ref not in present:
            omit(ref, "incident", "over_budget")
    return _seal(bundle)


def _seal(bundle: dict[str, Any]) -> dict[str, Any]:
    text = canonical_json(
        {"incidents": bundle["incidents"], "manifest": bundle["manifest"],
         "problem": bundle["problem"]})
    bundle["byte_size"] = len(text.encode("utf-8"))
    bundle["tokens_estimated"] = bundle["byte_size"] // 4
    return bundle


def _note_for(doc: dict[str, Any], scope: str) -> str | None:
    message = str((doc.get("context") or {}).get("message") or "")
    if message and contains_cross_project_reference(message, scope):
        return "cross_project_reference_kept_as_data"
    return None


def _add_material(bundle: dict[str, Any], doc: dict[str, Any], ref: str,
                  scope: str, *,
                  code_reader: Callable[[str, str], str | None] | None,
                  protocol_map: dict[str, str] | None,
                  brake_values: dict[str, Any] | None,
                  fits: Callable[[str], bool],
                  omit: Callable[..., None]) -> None:
    kind = str(doc.get("kind") or "")
    engine = doc.get("engine") or {}
    note = _note_for(doc, scope)
    message = str((doc.get("context") or {}).get("message") or "")
    if message:
        blocked = wrap_data(message)
        if fits(blocked):
            bundle["incidents"][-1]["message"] = blocked
            bundle["manifest"].append(_manifest(
                f"{ref}:message", "incident", blocked, note=note))
        else:
            omit(f"{ref}:message", "incident", "over_budget", note)
    if kind == "exception":
        _add_exception_material(bundle, doc, ref, code_reader,
                                fits, omit, note)
    elif kind == "stall":
        _add_stall_material(bundle, doc, ref, protocol_map, fits, omit, note)
    elif kind in ("brake_exhausted", "terminal_failure"):
        _add_brake_material(bundle, doc, ref, brake_values, fits, omit)


def _add_exception_material(bundle: dict[str, Any], doc: dict[str, Any],
                            ref: str,
                            code_reader: Callable[[str, str], str | None]
                            | None,
                            fits: Callable[[str], bool],
                            omit: Callable[..., None],
                            note: str | None) -> None:
    engine = doc.get("engine") or {}
    stack = _normalized_stack(engine.get("frames"))
    stack_text = canonical_json({"stack": stack})
    if fits(stack_text):
        bundle["incidents"][-1]["stack"] = stack
        bundle["manifest"].append(_manifest(f"{ref}:stack", "incident",
                                            stack_text, note=note))
    else:
        omit(f"{ref}:stack", "incident", "over_budget", note)
        return
    frames = _raw_in_app_frames(doc)[:MAX_EXCEPTION_FRAMES]
    if not frames:
        omit(f"{ref}:code", "git", "paths_unavailable_sanitized", note)
        return
    release_sha = str((doc.get("release") or {}).get("sha") or "unknown")
    for frame in frames:
        path = str(frame.get("file") or "")
        source = code_reader(release_sha, path) if code_reader else None
        excerpt = _code_excerpt(source, path, frame.get("line"))
        piece_ref = f"{ref}:code:{path}"
        if excerpt is None:
            omit(piece_ref, "git",
                 "code_unavailable" if code_reader else "no_code_reader",
                 note)
            continue
        blocked = wrap_data(excerpt)
        if fits(blocked):
            bundle["incidents"][-1].setdefault("code", []).append(
                {"path": path, "excerpt": blocked})
            bundle["manifest"].append(_manifest(
                piece_ref, f"git:{release_sha}:{path}", blocked, note=note))
        else:
            omit(piece_ref, f"git:{release_sha}:{path}", "over_budget", note)


def _add_stall_material(bundle: dict[str, Any], doc: dict[str, Any],
                       ref: str, protocol_map: dict[str, str] | None,
                       fits: Callable[[str], bool],
                       omit: Callable[..., None],
                       note: str | None) -> None:
    engine = doc.get("engine") or {}
    window = {
        "phase": doc.get("phase"),
        "guard_rule": engine.get("guard_rule"),
        "brake": engine.get("brake"),
        "markers": engine.get("result_signature"),
    }
    window_text = canonical_json({"brake_window": window})
    if fits(window_text):
        bundle["incidents"][-1]["brake_window"] = window
        bundle["manifest"].append(_manifest(f"{ref}:brake_window",
                                            "incident", window_text,
                                            note=note))
    else:
        omit(f"{ref}:brake_window", "incident", "over_budget", note)
    component = str(doc.get("component") or "")
    extract = (protocol_map or {}).get(component)
    piece_ref = f"{ref}:protocol:{component}"
    if extract is None:
        omit(piece_ref, "protocol", "no_allowlist_entry", note)
        return
    blocked = wrap_data(extract)
    if fits(blocked):
        bundle["incidents"][-1]["protocol_extract"] = blocked
        bundle["manifest"].append(_manifest(piece_ref, f"protocol:{component}",
                                            blocked, note=note))
    else:
        omit(piece_ref, f"protocol:{component}", "over_budget", note)


def _add_brake_material(bundle: dict[str, Any], doc: dict[str, Any],
                       ref: str, brake_values: dict[str, Any] | None,
                       fits: Callable[[str], bool],
                       omit: Callable[..., None]) -> None:
    engine = doc.get("engine") or {}
    codes = {
        "reason_code": engine.get("reason_code"),
        "brake": engine.get("brake"),
        "guard_rule": engine.get("guard_rule"),
        "effective_values": brake_values,
    }
    codes_text = canonical_json({"brake_codes": codes})
    if brake_values is None:
        omit(f"{ref}:brake_values", "config", "brake_values_not_provided")
    if fits(codes_text):
        bundle["incidents"][-1]["brake_codes"] = codes
        bundle["manifest"].append(_manifest(f"{ref}:brake_codes", "incident",
                                            codes_text))
    else:
        omit(f"{ref}:brake_codes", "incident", "over_budget")


def _add_project_context(bundle: dict[str, Any], scope: str,
                         project_context: str | None,
                         fits: Callable[[str], bool],
                         omit: Callable[..., None]) -> None:
    if project_context is None:
        if scope.startswith("project:"):
            omit("project:context", "project", "no_project_context")
        return
    if not scope.startswith("project:"):
        omit("project:context", "project", "scope_not_project")
        return
    blocked = wrap_data(project_context)
    if fits(blocked):
        bundle["project_context"] = blocked
        bundle["manifest"].append(_manifest("project:context", "project",
                                            blocked))
    else:
        omit("project:context", "project", "over_budget")


def parse_output(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "not_a_mapping"
    return payload, None


def validate_output(payload: dict[str, Any],
                    manifest: set[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return False, ["not_a_mapping"]
    allowed = {"schema_version", "classification", "attribution",
               "cluster_consistent", "duplicate_of", "root_cause",
               "confidence", "alternatives", "evidence_refs",
               "proposed_change", "target_components", "regression_test",
               "recovery", "ticket_type", "unknowns"}
    for key in payload:
        if key not in allowed:
            errors.append(f"unknown_key:{key}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("bad_schema_version")
    if payload.get("classification") not in CLASSIFICATIONS:
        errors.append("bad_classification")
    if payload.get("attribution") not in OUTPUT_ATTRIBUTIONS:
        errors.append("bad_attribution")
    if not isinstance(payload.get("cluster_consistent"), bool):
        errors.append("bad_cluster_consistent")
    duplicate = payload.get("duplicate_of")
    if duplicate is not None and not (isinstance(duplicate, str)
                                      and duplicate.strip()):
        errors.append("bad_duplicate_of")
    if not (isinstance(payload.get("root_cause"), str)
            and payload["root_cause"].strip()):
        errors.append("bad_root_cause")
    if payload.get("confidence") not in CONFIDENCES:
        errors.append("bad_confidence")
    alternatives = payload.get("alternatives")
    if not isinstance(alternatives, list):
        errors.append("bad_alternatives")
    else:
        for item in alternatives:
            if not isinstance(item, dict) \
                    or not (isinstance(item.get("cause"), str)
                            and item["cause"].strip()) \
                    or item.get("status") not in ALTERNATIVE_STATUSES:
                errors.append("bad_alternative")
                break
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list) \
            or any(not isinstance(ref, str) for ref in refs):
        errors.append("bad_evidence_refs")
    else:
        for ref in refs:
            if ref not in manifest:
                errors.append(f"unknown_evidence_ref:{ref}")
                break
    if not isinstance(payload.get("proposed_change"), str):
        errors.append("bad_proposed_change")
    targets = payload.get("target_components")
    if not isinstance(targets, list) \
            or any(not isinstance(item, str) for item in targets):
        errors.append("bad_target_components")
    if not isinstance(payload.get("regression_test"), str):
        errors.append("bad_regression_test")
    if payload.get("recovery") not in RECOVERIES:
        errors.append("bad_recovery")
    if payload.get("ticket_type") not in TICKET_TYPES:
        errors.append("bad_ticket_type")
    if not isinstance(payload.get("unknowns"), list):
        errors.append("bad_unknowns")
    return (len(errors) == 0), errors


def decide(valid: bool, payload: dict[str, Any]) -> str:
    if not valid or not isinstance(payload, dict):
        return INSUFFICIENT_EVIDENCE
    if payload.get("cluster_consistent") is False:
        return NEEDS_SPLIT
    duplicate = payload.get("duplicate_of")
    if isinstance(duplicate, str) and duplicate.strip():
        return LINK
    if payload.get("confidence") == "weak" \
            or payload.get("classification") == INSUFFICIENT_EVIDENCE:
        return INVESTIGATE
    if payload.get("attribution") in ("environment", "task"):
        return NO_ACTION
    if payload.get("attribution") in ("engine", "project") \
            and payload.get("confidence") in ("confirmed", "supported"):
        return FIX
    return INVESTIGATE


def investigate_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, str]] = []
    for item in bundle.get("incidents", []):
        if not isinstance(item, dict):
            continue
        for key in ("message", "protocol_extract"):
            value = item.get(key)
            if isinstance(value, str) and value:
                blocks.append({"ref": str(item.get("ref")),
                               "kind": key, "data": value})
        for piece in item.get("code", []) or []:
            if isinstance(piece, dict) and piece.get("excerpt"):
                blocks.append({"ref": str(item.get("ref")),
                               "kind": "code", "data": piece["excerpt"]})
    context = bundle.get("project_context")
    if isinstance(context, str) and context:
        blocks.append({"ref": "project:context", "kind": "context",
                       "data": context})
    return {
        "kind": INVESTIGATE,
        "role": EVIDENCE_ROLE,
        "problem": bundle.get("problem"),
        "evidence_blocks": blocks,
        "manifest": list(bundle.get("manifest", [])),
        "note": "diagnosis_disabled_deterministic_evidence",
    }
