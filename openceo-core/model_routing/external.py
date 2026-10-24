"""External interfaces: gateway config, OpenCode config, consumer snapshot (TASK-2026-00295).

SPEC §4.2, §9.1, §13 and §17 as code. The gateway stays in security
HOLD (SPEC §14.1): nothing here installs or runs LiteLLM. The
generator emits a logical contract in the SPEC §13.1 grammar for
review only; field names must be re-validated against the selected
LiteLLM version before any deployment. While the gateway is on HOLD,
OpenCode routes stay native (SPEC §13.3) and external coordinators
(``automatico.py`` of other repos, interactive OpenCode) consume a
versioned JSON snapshot plus the ``reserve``/``release``/``cooldowns``/
``reservations`` CLI over the shared store (TASK-2026-00291), without
importing OpenCEO code.

Fail-closed throughout: a ranked deployment missing from the catalog,
no longer usable, or on an unmapped provider refuses generation (never
an alias able to route to an unapproved model); ``opencode`` provider
routes are native-only by design and reported, never silently lost.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from model_routing import alerts as router_alerts
from model_routing.models import (
    CatalogDocument,
    CatalogEntry,
    Deployment,
    ProfileRanking,
    RankingsDocument,
    RouterPolicy,
    is_usable,
)
from model_routing.paths import (
    CATALOG_FILENAME,
    GATEWAY_FROZEN_FILENAME,
    RANKINGS_FILENAME,
    config_dir,
)
from model_routing.publish import PublishError, atomic_write_json, load_catalog_doc
from model_routing.ranking import RANK_PROFILES, load_rankings

__all__ = [
    "EXTERNAL_SNAPSHOT_SCHEMA_VERSION",
    "FREE_GATEWAY_KEY_ENV",
    "GATEWAY_BASE_URL",
    "GATEWAY_FROZEN_FILENAME",
    "GATEWAY_PROVIDER_NAME",
    "OPENCODE_SCHEMAS",
    "ConsumerGateway",
    "ConsumerProfile",
    "ConsumerRoute",
    "ConsumerSnapshot",
    "ExternalError",
    "FreezeRecord",
    "GatewayFrozenError",
    "GatewayPlan",
    "NativeRoute",
    "ProfileRoutes",
    "SecurityRollback",
    "SkippedDeployment",
    "apply_security_rollback",
    "build_external_providers_config",
    "build_gateway_plan",
    "build_opencode_config",
    "credential_env_for",
    "detect_opencode_schema",
    "eligible_set_hash",
    "export_consumer_snapshot",
    "extract_eligible_hash",
    "freeze_gateway",
    "gateway_freeze",
    "gateway_reload_needed",
    "litellm_model",
    "load_consumer_snapshot",
    "merge_gateway_provider",
    "plan_consumer_routes",
    "read_opencode_config",
    "render_gateway_yaml",
    "strip_gateway_provider",
    "unfreeze_gateway",
]

#: External snapshot contract version; consumers pin this, not the code.
EXTERNAL_SNAPSHOT_SCHEMA_VERSION = 2

#: Gateway endpoint OpenCode dials (SPEC §13.2); stubbed in tests, HOLD in prod.
GATEWAY_BASE_URL = "http://127.0.0.1:4000/v1"

#: OpenCode provider key holding the generated gateway entry.
GATEWAY_PROVIDER_NAME = "free-gateway"

#: Local gateway credential, referenced by name only, never read here.
FREE_GATEWAY_KEY_ENV = "FREE_GATEWAY_KEY"

#: OpenCode config grammars (SPEC §13.2); never mixed in one document.
OPENCODE_SCHEMAS = ("classic", "v2")

_PROFILE_DISPLAY = {
    "free-research": "Free Research",
    "free-general": "Free General",
    "free-review": "Free Review",
}

#: LiteLLM model prefix and credential env per provider. ``opencode``
#: is native-only by design (SPEC §13.3) and stays out of model_list.
_LITELLM_PROVIDERS: dict[str, tuple[str, str]] = {
    "openrouter": ("openrouter", "FREE_ROUTER_OPENROUTER_API_KEY"),
    "nvidia": ("nvidia_nim", "FREE_ROUTER_NVIDIA_NIM_API_KEY"),
    "groq": ("groq", "FREE_ROUTER_GROQ_API_KEY"),
    "gemini": ("gemini", "FREE_ROUTER_GEMINI_API_KEY"),
}
_NATIVE_ONLY_PROVIDERS = ("opencode",)

#: OpenAI-compatible chat-completions base URLs for group B providers
#: configured directly in OpenCode (addendum v1.2 §3.4). Credentials
#: stay in the ``FREE_ROUTER_*`` variables of :data:`_LITELLM_PROVIDERS`.
_EXTERNAL_PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}
_EXTERNAL_PROVIDER_DISPLAY = {
    "openrouter": "OpenRouter",
    "nvidia": "NVIDIA",
    "groq": "Groq",
    "gemini": "Gemini",
}


class ExternalError(RuntimeError):
    """External artifact misconfigured or unreadable; the message says why."""


class GatewayFrozenError(ExternalError):
    """Security freeze in force: no new gateway deployments (T20)."""


class FreezeRecord(BaseModel):
    """Security freeze marker; while present, generation refuses (T20)."""

    model_config = ConfigDict(extra="forbid")

    frozen_at: str
    reason: str


@dataclass(frozen=True)
class SkippedDeployment:
    """A ranked deployment left out of model_list, with its reason."""

    deployment_id: str
    provider: str
    reason: str


@dataclass(frozen=True)
class NativeRoute:
    """One OpenCode CLI route: ``--model`` plus its provenance.

    Group A runs natively; group B runs through the provider
    configured in OpenCode (addendum v1.2 §3.4), never the gateway.
    """

    deployment_id: str
    canonical_model_id: str
    family_id: str
    rank: int
    command: tuple[str, ...]
    route_type: str = "opencode_native_free"
    allowed_client: str = "opencode_only"


@dataclass(frozen=True)
class GatewayPlan:
    """Logical gateway contract plus everything generation set aside."""

    backend: str
    model_list: tuple[dict[str, Any], ...]
    review_aliases: tuple[str, ...]
    review_routes: tuple[tuple[str, str], ...]
    skipped: tuple[SkippedDeployment, ...]
    native_routes: tuple[NativeRoute, ...]
    eligible_sha256: str


@dataclass(frozen=True)
class ProfileRoutes:
    """How one profile serves: gateway aliases or native routes (T14)."""

    profile: str
    mode: str
    aliases: tuple[str, ...] = ()
    native: tuple[NativeRoute, ...] = ()
    notice: str = ""


@dataclass(frozen=True)
class SecurityRollback:
    """T20 outcome: native-only config plus the recorded freeze."""

    native_config: dict[str, Any]
    freeze: FreezeRecord


class ConsumerRoute(BaseModel):
    """One servable route inside the external snapshot."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    canonical_model_id: str
    family_id: str
    deployment_id: str
    provider: str
    route_type: str
    allowed_client: str
    provider_id: str
    model_id: str
    gateway_alias: str | None = None
    native_cli_model: str | None = None


class ConsumerProfile(BaseModel):
    """One profile order as external coordinators consume it."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    routes: list[ConsumerRoute] = Field(default_factory=list)
    native_notice: str = ""


class ConsumerGateway(BaseModel):
    """Gateway side of the contract; frozen means native-only (T20)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    backend: str
    eligible_sha256: str
    frozen: bool = False


class ConsumerSnapshot(BaseModel):
    """Versioned external contract; validated on every read."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = EXTERNAL_SNAPSHOT_SCHEMA_VERSION
    generated_at: str
    generator: str = ""
    rankings_sha256: str = ""
    catalog_snapshot: str = ""
    gateway: ConsumerGateway
    profiles: dict[str, ConsumerProfile] = Field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _routes_word(count: int) -> str:
    return "route" if count == 1 else "routes"


def litellm_model(provider: str, gateway_model_id: str | None,
                  deployment_id: str) -> str:
    """LiteLLM ``model`` string for one deployment (SPEC §13.1 grammar)."""
    if provider not in _LITELLM_PROVIDERS:
        raise ExternalError(
            f"deployment {deployment_id} has no gateway mapping for provider"
            f" {provider!r} (mapped: {', '.join(sorted(_LITELLM_PROVIDERS))})")
    if not gateway_model_id:
        raise ExternalError(
            f"deployment {deployment_id} carries no provider model id")
    prefix, _env = _LITELLM_PROVIDERS[provider]
    return f"{prefix}/{gateway_model_id}"


def credential_env_for(provider: str) -> str:
    """``FREE_ROUTER_*`` credential name of one group B provider.

    Names only, never values. Native ``opencode`` needs no
    credential and is refused, like any unmapped provider.
    """
    try:
        _prefix, env_var = _LITELLM_PROVIDERS[provider]
    except KeyError:
        raise ExternalError(
            f"provider {provider!r} has no group B credential mapping"
            f" (mapped: {', '.join(sorted(_LITELLM_PROVIDERS))})") from None
    return env_var


def _deployment_index(catalog: CatalogDocument) -> dict[str, tuple[CatalogEntry, Deployment]]:
    return {
        deployment.deployment_id: (entry, deployment)
        for entry in catalog.entries
        for deployment in entry.deployments
    }


def _check_frozen(directory: Path | None) -> None:
    if directory is None:
        return
    marker = directory / GATEWAY_FROZEN_FILENAME
    if not marker.is_file():
        return
    try:
        record = FreezeRecord.model_validate(json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        record = None
    reason = record.reason if record else "unreadable freeze marker"
    raise GatewayFrozenError(f"gateway frozen: {reason}")


def build_gateway_plan(
    catalog: CatalogDocument,
    rankings: RankingsDocument,
    *,
    backend: str = "litellm",
    freeze_dir: Path | None = None,
    policy: RouterPolicy | None = None,
) -> GatewayPlan:
    """Logical gateway contract from ranking order; refuses while frozen.

    Every ranked deployment must resolve in the catalog and stay
    usable; anything else fails closed. ``opencode`` routes are
    native-only (SPEC §13.3): reported under ``native_routes``, never
    in ``model_list``.
    """
    if backend != "litellm":
        raise ExternalError(f"unknown gateway backend: {backend!r} (known: litellm)")
    _check_frozen(freeze_dir)
    active = policy or RouterPolicy()
    index = _deployment_index(catalog)
    model_list: list[dict[str, Any]] = []
    review_aliases: list[str] = []
    skipped: list[SkippedDeployment] = []
    native: list[NativeRoute] = []
    seen_native: set[str] = set()

    def _note_native(canonical: str, family: str, rank: int,
                     deployment_id: str) -> None:
        if deployment_id in seen_native:
            return
        seen_native.add(deployment_id)
        native.append(NativeRoute(
            deployment_id=deployment_id, canonical_model_id=canonical,
            family_id=family, rank=rank,
            command=("opencode", "run", "-m", deployment_id)))

    def _resolve(profile: str, rank: int, canonical: str, family: str,
                 deployment_id: str, provider: str) -> tuple[CatalogEntry, Deployment] | None:
        found = index.get(deployment_id)
        if found is None:
            raise ExternalError(
                f"profile {profile} ranks {deployment_id}, missing from the catalog")
        entry, deployment = found
        if deployment.provider != provider:
            raise ExternalError(
                f"profile {profile} ranks {deployment_id} on {provider!r},"
                f" catalog says {deployment.provider!r}")
        if not is_usable(deployment, active):
            raise ExternalError(
                f"profile {profile} ranks {deployment_id}, no longer usable"
                " (re-verify before routing)")
        if provider in _NATIVE_ONLY_PROVIDERS:
            skipped.append(SkippedDeployment(
                deployment_id, provider, "native-only provider (SPEC 13.3)"))
            _note_native(canonical, family, rank, deployment_id)
            return None
        if provider not in _LITELLM_PROVIDERS:
            raise ExternalError(
                f"profile {profile} ranks {deployment_id} on unmapped provider"
                f" {provider!r}")
        if not deployment.healthy:
            skipped.append(SkippedDeployment(
                deployment_id, provider, "unhealthy at generation time"))
            return None
        return entry, deployment

    for profile in RANK_PROFILES:
        ranking: ProfileRanking | None = rankings.profiles.get(profile)
        if ranking is None:
            continue
        order = 0
        for item in ranking.order:
            routes = [item.primary, *item.backups]
            for route in routes:
                resolved = _resolve(profile, item.rank, item.canonical_model_id,
                                    item.family_id, route.deployment_id, route.provider)
                if resolved is None:
                    continue
                _entry, deployment = resolved
                order += 1
                _prefix, key_env = _LITELLM_PROVIDERS[route.provider]
                model_list.append({
                    "model_name": profile,
                    "litellm_params": {
                        "model": litellm_model(
                            route.provider, deployment.gateway_model_id,
                            route.deployment_id),
                        "api_key": f"os.environ/{key_env}",
                        "order": order,
                    },
                })
    review = rankings.profiles.get("free-review")
    review_routes: list[tuple[str, str]] = []
    if review is not None:
        seq = 0
        for item in review.order:
            for route in [item.primary, *item.backups]:
                resolved = _resolve("free-review", item.rank, item.canonical_model_id,
                                    item.family_id, route.deployment_id, route.provider)
                if resolved is None:
                    continue
                _entry, deployment = resolved
                seq += 1
                alias = f"review-candidate-{seq:03d}"
                review_aliases.append(alias)
                review_routes.append((alias, route.deployment_id))
                _prefix, key_env = _LITELLM_PROVIDERS[route.provider]
                model_list.append({
                    "model_name": alias,
                    "litellm_params": {
                        "model": litellm_model(
                            route.provider, deployment.gateway_model_id,
                            route.deployment_id),
                        "api_key": f"os.environ/{key_env}",
                        "num_retries": 0,
                    },
                })
    digest = eligible_set_hash(model_list)
    return GatewayPlan(
        backend=backend,
        model_list=tuple(model_list),
        review_aliases=tuple(review_aliases),
        review_routes=tuple(review_routes),
        skipped=tuple(dict.fromkeys(skipped)),
        native_routes=tuple(native),
        eligible_sha256=digest,
    )


def eligible_set_hash(model_list: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    """Stable hash of the semantic routing set; cosmetics do not move it."""
    semantic = sorted(
        (entry["model_name"],
         entry["litellm_params"].get("order"),
         entry["litellm_params"]["model"],
         entry["litellm_params"]["api_key"])
        for entry in model_list
    )
    payload = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _yaml_block(entry: dict[str, Any]) -> list[str]:
    params = entry["litellm_params"]
    lines = [f"  - model_name: {entry['model_name']}", "    litellm_params:"]
    lines.append(f"      model: {params['model']}")
    lines.append(f"      api_key: {params['api_key']}")
    if "order" in params:
        lines.append(f"      order: {params['order']}")
    if "num_retries" in params:
        lines.append(f"      num_retries: {params['num_retries']}")
    return lines


def render_gateway_yaml(plan: GatewayPlan) -> str:
    """Deterministic LiteLLM YAML; header carries the eligible hash."""
    lines = [
        "# gateway.generated.yaml - generated, do not edit by hand.",
        "# Logical contract in the SPEC 13.1 grammar; HOLD: never deploy",
        "# without the SPEC 14.1 security approval.",
        f"# eligible-sha256: {plan.eligible_sha256}",
        "model_list:",
    ]
    for entry in plan.model_list:
        lines.extend(_yaml_block(entry))
    lines.extend([
        "router_settings:",
        "  allowed_fails: 3",
        "  cooldown_time: 30",
        "litellm_settings:",
        "  drop_params: false",
        "  turn_off_message_logging: true",
        "general_settings:",
        "  master_key: os.environ/FREE_ROUTER_LITELLM_MASTER_KEY",
        "",
    ])
    return "\n".join(lines)


def extract_eligible_hash(text: str) -> str | None:
    """Eligible hash from a generated file header, if present."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# eligible-sha256:"):
            digest = stripped.split(":", 1)[1].strip()
            return digest or None
    return None


def gateway_reload_needed(previous_text: str | None, plan: GatewayPlan) -> bool:
    """True when the eligible set moved; missing hash reloads (fail-closed)."""
    if not previous_text:
        return True
    return extract_eligible_hash(previous_text) != plan.eligible_sha256


def detect_opencode_schema(data: Mapping[str, Any]) -> str:
    """``classic`` vs ``v2`` from config keys; mixed documents refuse."""
    has_classic = "provider" in data
    has_v2 = "providers" in data
    if has_classic and has_v2:
        raise ExternalError("opencode config mixes classic and v2 schemas")
    if has_v2:
        return "v2"
    return "classic"


def read_opencode_config(path: Path) -> dict[str, Any]:
    """Read one OpenCode config file as strict JSON."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ExternalError(f"opencode config unreadable: {path}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ExternalError(
            f"opencode config {path} is not plain JSON;"
            " pass schema explicitly for JSONC") from exc
    if not isinstance(data, dict):
        raise ExternalError(f"opencode config {path} has no top-level object")
    return data


def build_opencode_config(
    schema: str,
    *,
    base_url: str = GATEWAY_BASE_URL,
    profiles: tuple[str, ...] = RANK_PROFILES,
) -> dict[str, Any]:
    """Standalone config routing profiles through the gateway (SPEC §13.2)."""
    if schema not in OPENCODE_SCHEMAS:
        raise ExternalError(
            f"unknown opencode schema: {schema!r} (known: {', '.join(OPENCODE_SCHEMAS)})")
    unknown = [name for name in profiles if name not in RANK_PROFILES]
    if unknown:
        raise ExternalError(f"unknown profiles: {', '.join(unknown)}")
    models = {name: {"name": _PROFILE_DISPLAY[name]} for name in profiles}
    if schema == "v2":
        provider = {
            "name": "Free Gateway local",
            "package": "@opencode/ai/providers/openai-compatible",
            "env": [FREE_GATEWAY_KEY_ENV],
            "settings": {"baseURL": base_url},
            "models": models,
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "providers": {GATEWAY_PROVIDER_NAME: provider},
        }
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Free Gateway local",
        "options": {
            "baseURL": base_url,
            "apiKey": f"{{env:{FREE_GATEWAY_KEY_ENV}}}",
        },
        "models": models,
    }
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {GATEWAY_PROVIDER_NAME: provider},
    }


def build_external_providers_config(
    schema: str,
    catalog: CatalogDocument,
    *,
    policy: RouterPolicy | None = None,
) -> dict[str, Any]:
    """Group B provider entries for OpenCode (addendum v1.2 §3.4, §5.1).

    Only usable, healthy, ``free_verified`` external deployments land
    in the document, under their plain provider id so the catalog
    ``provider_id`` matches what the instance sees; ``{env:...}``
    references carry the ``FREE_ROUTER_*`` credential names, never
    values. Native ``opencode`` deployments need no entry and are
    skipped. Unmapped external providers, route inconsistencies, an
    empty eligible set, or an unknown schema refuse generation.
    """
    if schema not in OPENCODE_SCHEMAS:
        raise ExternalError(
            f"unknown opencode schema: {schema!r} (known: {', '.join(OPENCODE_SCHEMAS)})")
    active = policy or RouterPolicy()
    per_provider: dict[str, dict[str, str]] = {}
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if deployment.provider in _NATIVE_ONLY_PROVIDERS:
                if deployment.route_type != "opencode_native_free":
                    raise ExternalError(
                        f"deployment {deployment.deployment_id} mixes the native"
                        " provider with a non-native route")
                continue
            if deployment.route_type != "external_free":
                raise ExternalError(
                    f"deployment {deployment.deployment_id} mixes provider"
                    f" {deployment.provider} with a native route")
            if deployment.provider not in _EXTERNAL_PROVIDER_BASE_URLS:
                raise ExternalError(
                    f"deployment {deployment.deployment_id} has no OpenCode"
                    f" mapping for provider {deployment.provider!r} (mapped:"
                    f" {', '.join(sorted(_EXTERNAL_PROVIDER_BASE_URLS))})")
            if not is_usable(deployment, active) or not deployment.healthy:
                continue
            models = per_provider.setdefault(deployment.provider, {})
            models.setdefault(deployment.model_id,
                              entry.display_name or deployment.model_id)
    if not per_provider:
        raise ExternalError("no eligible external deployment for group B config")
    providers: dict[str, Any] = {}
    for provider in sorted(per_provider):
        base_url = _EXTERNAL_PROVIDER_BASE_URLS[provider]
        env_var = _LITELLM_PROVIDERS[provider][1]
        name = f"Free {_EXTERNAL_PROVIDER_DISPLAY[provider]} (model_routing)"
        models = {model_id: {"name": per_provider[provider][model_id]}
                  for model_id in sorted(per_provider[provider])}
        if schema == "v2":
            providers[provider] = {
                "name": name,
                "package": "@opencode/ai/providers/openai-compatible",
                "env": [env_var],
                "settings": {"baseURL": base_url, "apiKey": f"{{env:{env_var}}}"},
                "models": models,
            }
        else:
            providers[provider] = {
                "npm": "@ai-sdk/openai-compatible",
                "name": name,
                "options": {"baseURL": base_url, "apiKey": f"{{env:{env_var}}}"},
                "models": models,
            }
    key = "providers" if schema == "v2" else "provider"
    return {
        "$schema": "https://opencode.ai/config.json",
        key: providers,
    }


def strip_gateway_provider(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of the config without the gateway provider: native-only."""
    cleaned = copy.deepcopy(dict(data))
    for key in ("provider", "providers"):
        providers = cleaned.get(key)
        if isinstance(providers, dict) and GATEWAY_PROVIDER_NAME in providers:
            rest = {name: entry for name, entry in providers.items()
                    if name != GATEWAY_PROVIDER_NAME}
            if rest:
                cleaned[key] = rest
            else:
                del cleaned[key]
    return cleaned


def merge_gateway_provider(data: Mapping[str, Any], generated: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of the config with the generated gateway provider merged in."""
    first = detect_opencode_schema(data) if ("provider" in data or "providers" in data) else None
    second = detect_opencode_schema(generated)
    if first is not None and first != second:
        raise ExternalError(
            f"cannot merge {second} gateway config into {first} document")
    key = "providers" if second == "v2" else "provider"
    existing = data.get(key)
    if existing is not None and not isinstance(existing, dict):
        raise ExternalError(f"opencode config {key!r} section is not an object")
    generated_providers = generated.get(key)
    if not isinstance(generated_providers, dict) or GATEWAY_PROVIDER_NAME not in generated_providers:
        raise ExternalError("generated config carries no gateway provider")
    merged = copy.deepcopy(dict(data))
    providers = copy.deepcopy(dict(existing or {}))
    providers[GATEWAY_PROVIDER_NAME] = copy.deepcopy(
        dict(generated_providers[GATEWAY_PROVIDER_NAME]))
    merged[key] = providers
    if "$schema" not in merged and "$schema" in generated:
        merged["$schema"] = generated["$schema"]
    return merged


def freeze_gateway(directory: Path, reason: str) -> FreezeRecord:
    """Freeze new gateway deployments; generation refuses until unfreeze."""
    record = FreezeRecord(frozen_at=_utc_now_iso(), reason=reason)
    atomic_write_json(directory / GATEWAY_FROZEN_FILENAME,
                      record.model_dump_json(indent=2))
    return record


def gateway_freeze(directory: Path) -> FreezeRecord | None:
    """Active freeze marker, if any; corrupt markers read as frozen."""
    marker = directory / GATEWAY_FROZEN_FILENAME
    if not marker.is_file():
        return None
    try:
        return FreezeRecord.model_validate(
            json.loads(marker.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return FreezeRecord(frozen_at="", reason="unreadable freeze marker")


def unfreeze_gateway(directory: Path) -> bool:
    """Lift the freeze; True when a marker was actually present."""
    marker = directory / GATEWAY_FROZEN_FILENAME
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def apply_security_rollback(
    opencode_config: Mapping[str, Any],
    directory: Path,
    reason: str,
    *,
    alerts_dir: Path | None = None,
) -> SecurityRollback:
    """T20: freeze new deployments and roll the config back to native.

    The security alert lands next to the freeze marker unless the
    caller points ``alerts_dir`` elsewhere (the state dir in
    production); without a writable directory the freeze still
    holds and no alert is emitted.
    """
    freeze = freeze_gateway(directory, reason)
    router_alerts.emit_alert(alerts_dir or directory, "gateway_security",
                             "gateway", reason)
    return SecurityRollback(native_config=strip_gateway_provider(opencode_config),
                            freeze=freeze)


def plan_consumer_routes(
    catalog: CatalogDocument,
    rankings: RankingsDocument,
    *,
    gateway_available: bool,
    policy: RouterPolicy | None = None,
) -> dict[str, ProfileRoutes]:
    """Per-profile serving plan; absent gateway falls back to OpenCode (T14).

    Native mode serves ``opencode`` routes plus external routes via
    the provider configured in OpenCode (addendum v1.2 §3.4): group
    B needs the generated provider config installed, and a paid
    route must fail instead of billing. The notice says so in
    plain words.
    """
    active = policy or RouterPolicy()
    index = _deployment_index(catalog)
    plans: dict[str, ProfileRoutes] = {}
    for profile in RANK_PROFILES:
        ranking = rankings.profiles.get(profile)
        if ranking is None:
            plans[profile] = ProfileRoutes(
                profile=profile, mode="blocked",
                notice=f"no {profile} ranking published")
            continue
        if gateway_available:
            if not ranking.order:
                plans[profile] = ProfileRoutes(
                    profile=profile, mode="blocked",
                    notice=f"{profile} ranking order is empty; nothing to serve")
                continue
            plans[profile] = ProfileRoutes(
                profile=profile, mode="gateway", aliases=(profile,),
                notice=f"{profile} serves through the gateway alias")
            continue
        native: list[NativeRoute] = []
        seen: set[str] = set()
        for item in ranking.order:
            for route in [item.primary, *item.backups]:
                found = index.get(route.deployment_id)
                if found is None:
                    continue
                _entry, deployment = found
                if (route.provider != deployment.provider
                        or not is_usable(deployment, active)):
                    continue
                if route.deployment_id in seen:
                    continue
                seen.add(route.deployment_id)
                native.append(NativeRoute(
                    deployment_id=route.deployment_id,
                    canonical_model_id=item.canonical_model_id,
                    family_id=item.family_id, rank=item.rank,
                    command=("opencode", "run", "-m",
                             f"{deployment.provider_id}/{deployment.model_id}"),
                    route_type=deployment.route_type,
                    allowed_client=deployment.allowed_client))
        if native:
            group_b = sum(1 for route in native
                          if route.route_type != "opencode_native_free")
            group_a = len(native) - group_b
            if group_b:
                notice = (f"gateway unavailable; {profile} serves {group_a}"
                          f" native OpenCode {_routes_word(group_a)} and {group_b}"
                          f" configured-provider {_routes_word(group_b)};"
                          " group B needs the generated provider config")
            else:
                notice = (f"gateway unavailable; {profile} serves {group_a}"
                          f" native OpenCode {_routes_word(group_a)}")
            plans[profile] = ProfileRoutes(
                profile=profile, mode="native", native=tuple(native), notice=notice)
        else:
            plans[profile] = ProfileRoutes(
                profile=profile, mode="blocked",
                notice=(f"gateway unavailable and {profile} has no OpenCode"
                        " route, native or configured; blocked,"
                        " nothing paid touched"))
    return plans


def export_consumer_snapshot(
    catalog: CatalogDocument,
    rankings: RankingsDocument,
    path: Path,
    *,
    generator: str = "",
    gateway_base_url: str = GATEWAY_BASE_URL,
    gateway_available: bool = False,
    freeze_dir: Path | None = None,
    policy: RouterPolicy | None = None,
) -> ConsumerSnapshot:
    """Publish the versioned external contract (SPEC §17 consumers).

    Fail-closed like generation: unusable or missing ranked routes
    refuse the export. The freeze marker only flips the ``frozen``
    flag; describing a frozen gateway stays allowed, deploying new
    routes does not.
    """
    active = policy or RouterPolicy()
    index = _deployment_index(catalog)
    plan = build_gateway_plan(catalog, rankings, policy=active)
    frozen = freeze_dir is not None and gateway_freeze(freeze_dir) is not None
    rank_payload = rankings.model_dump_json()
    rankings_sha = hashlib.sha256(rank_payload.encode("utf-8")).hexdigest()
    review_names = {deployment: alias for alias, deployment in plan.review_routes}
    profiles: dict[str, ConsumerProfile] = {}
    for profile in RANK_PROFILES:
        ranking = rankings.profiles.get(profile)
        if ranking is None:
            continue
        routes: list[ConsumerRoute] = []
        for item in ranking.order:
            for route in [item.primary, *item.backups]:
                found = index.get(route.deployment_id)
                if found is None:
                    continue
                _entry, deployment = found
                if not is_usable(deployment, active):
                    continue
                if not deployment.healthy:
                    continue
                native_only = route.provider in _NATIVE_ONLY_PROVIDERS
                alias: str | None = None
                if not native_only:
                    if profile == "free-review":
                        alias = review_names.get(route.deployment_id)
                    else:
                        alias = profile
                routes.append(ConsumerRoute(
                    rank=item.rank,
                    canonical_model_id=item.canonical_model_id,
                    family_id=item.family_id,
                    deployment_id=route.deployment_id,
                    provider=route.provider,
                    route_type=deployment.route_type,
                    allowed_client=deployment.allowed_client,
                    provider_id=deployment.provider_id,
                    model_id=deployment.model_id,
                    gateway_alias=alias,
                    native_cli_model=(route.deployment_id
                                      if route.provider == "opencode" else None),
                ))
        notice = ""
        if not gateway_available:
            native_count = sum(1 for route in routes if route.native_cli_model)
            external_count = len(routes) - native_count
            notice = (f"gateway unavailable; {native_count}"
                      f" native {_routes_word(native_count)}"
                      + (f" and {external_count} configured-provider"
                         f" {_routes_word(external_count)};"
                         " group B needs the generated provider config"
                         if external_count else ""))
        profiles[profile] = ConsumerProfile(profile=profile, routes=routes,
                                            native_notice=notice)
    snapshot = ConsumerSnapshot(
        generated_at=_utc_now_iso(),
        generator=generator,
        rankings_sha256=f"sha256:{rankings_sha}",
        catalog_snapshot=rankings.catalog_snapshot,
        gateway=ConsumerGateway(
            base_url=gateway_base_url,
            backend="litellm",
            eligible_sha256=plan.eligible_sha256,
            frozen=frozen,
        ),
        profiles=profiles,
    )
    atomic_write_json(Path(path), snapshot.model_dump_json(indent=2))
    return snapshot


def _migrate_snapshot_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Fill the v2 route fields on a v1 snapshot; malformed input refuses."""
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise ExternalError("v1 snapshot has no profiles object")
    migrated_profiles: dict[str, Any] = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ExternalError(f"v1 snapshot profile {name!r} is not an object")
        routes = profile.get("routes")
        if not isinstance(routes, list):
            raise ExternalError(f"v1 snapshot profile {name!r} has no routes list")
        migrated_routes: list[dict[str, Any]] = []
        for route in routes:
            if not isinstance(route, dict):
                raise ExternalError(f"v1 snapshot route in {name!r} is not an object")
            provider = route.get("provider")
            deployment_id = route.get("deployment_id")
            if not isinstance(provider, str) or not provider:
                raise ExternalError(f"v1 snapshot route in {name!r} misses its provider")
            if not isinstance(deployment_id, str) or "/" not in deployment_id:
                raise ExternalError(
                    f"v1 snapshot route id is not provider/model:"
                    f" {deployment_id!r}")
            migrated = dict(route)
            if provider in _NATIVE_ONLY_PROVIDERS:
                migrated.setdefault("route_type", "opencode_native_free")
                migrated.setdefault("allowed_client", "opencode_only")
            else:
                migrated.setdefault("route_type", "external_free")
                migrated.setdefault("allowed_client", "configured_opencode_or_gateway")
            migrated.setdefault("provider_id", provider)
            migrated.setdefault("model_id", deployment_id.split("/", 1)[1])
            migrated_routes.append(migrated)
        migrated_profile = dict(profile)
        migrated_profile["routes"] = migrated_routes
        migrated_profiles[name] = migrated_profile
    migrated_doc = dict(data)
    migrated_doc["schema_version"] = EXTERNAL_SNAPSHOT_SCHEMA_VERSION
    migrated_doc["profiles"] = migrated_profiles
    return migrated_doc


def load_consumer_snapshot(path: Path) -> ConsumerSnapshot:
    """Read and validate an external snapshot; invalid reads refuse.

    v1 snapshots migrate to v2 on read; anything else than v1 or v2
    refuses.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExternalError(f"snapshot missing or unreadable: {path}") from exc
    except ValueError as exc:
        raise ExternalError(f"snapshot {path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ExternalError(f"snapshot {path} has no top-level object")
    if data.get("schema_version") == 1:
        data = _migrate_snapshot_v1_to_v2(data)
    try:
        snapshot = ConsumerSnapshot.model_validate(data)
    except ValidationError as exc:
        raise ExternalError(f"snapshot {path} failed schema validation") from exc
    if snapshot.schema_version != EXTERNAL_SNAPSHOT_SCHEMA_VERSION:
        raise ExternalError(
            f"unsupported snapshot schema_version: {snapshot.schema_version}")
    return snapshot


def load_consumer_inputs(directory: Path | None = None) -> tuple[CatalogDocument, RankingsDocument]:
    """Catalog plus rankings from one config dir; partial reads refuse."""
    target = Path(directory) if directory is not None else config_dir()
    try:
        catalog = load_catalog_doc(target / CATALOG_FILENAME)
    except PublishError as exc:
        raise ExternalError(f"consumer inputs: {exc}") from exc
    try:
        rankings = load_rankings(target / RANKINGS_FILENAME)
    except PublishError as exc:
        raise ExternalError(f"consumer inputs: {exc}") from exc
    return catalog, rankings
