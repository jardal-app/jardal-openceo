"""policy.yaml / providers.yaml loading with fail-closed validation (TASK-2026-00287).

Deny by default: missing or invalid documents are errors, never silent
defaults. ``providers.yaml`` must not contain keys: any secret-looking
mapping key is rejected naming only the key, since Pydantic ``extra``
errors would otherwise echo the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from model_routing.models import ProvidersDocument, RouterPolicy
from model_routing.paths import policy_path, providers_path
from model_routing.secrets import SecretsError, default_secrets_path, load_router_secrets

__all__ = [
    "ConfigError",
    "ConfigIssue",
    "load_policy",
    "load_providers",
    "validate_config",
]

_SECRET_KEY_HINTS = ("secret", "token", "password", "api_key", "apikey", "private_key")


class ConfigError(RuntimeError):
    """Config file missing or invalid; messages carry no secret values."""


@dataclass(frozen=True)
class ConfigIssue:
    """One ``validate-config`` finding; ``ok`` when the file checks out."""

    name: str
    ok: bool
    detail: str


def _secret_keys(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
                found.append(key)
            _secret_keys(value, found)
    elif isinstance(node, list):
        for value in node:
            _secret_keys(value, found)


def _read_yaml(path: Path, what: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{what} missing or unreadable: {path}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{what} {path} is not valid YAML") from exc


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(bit) for bit in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _validate(path: Path, what: str, model: type[BaseModel]) -> BaseModel:
    data = _read_yaml(path, what)
    if data is None:
        raise ConfigError(f"{what} {path} is empty")
    found: list[str] = []
    _secret_keys(data, found)
    if found:
        keys = ", ".join(sorted(set(found)))
        raise ConfigError(f"{what} {path} must not contain secrets (keys: {keys})")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{what} {path} invalid: {_format_errors(exc)}") from exc


def load_policy(path: Path | None = None) -> RouterPolicy:
    """Load and validate ``policy.yaml``."""
    result = _validate(path or policy_path(), "policy", RouterPolicy)
    assert isinstance(result, RouterPolicy)
    return result


def load_providers(path: Path | None = None) -> ProvidersDocument:
    """Load and validate ``providers.yaml``."""
    result = _validate(path or providers_path(), "providers", ProvidersDocument)
    assert isinstance(result, ProvidersDocument)
    return result


def validate_config() -> list[ConfigIssue]:
    """Check policy, providers and secrets; read-only, values never shown."""
    issues: list[ConfigIssue] = []
    for name, loader in (("policy", load_policy), ("providers", load_providers)):
        try:
            loader()
        except ConfigError as exc:
            issues.append(ConfigIssue(name, False, str(exc)))
        else:
            issues.append(ConfigIssue(name, True, "valid"))
    try:
        secrets = load_router_secrets()
    except SecretsError as exc:
        issues.append(ConfigIssue("secrets", False, str(exc)))
    else:
        missing = [key for key, is_set in secrets.present().items() if not is_set]
        detail = "all keys set" if not missing else f"unset: {', '.join(missing)}"
        issues.append(ConfigIssue("secrets", True, f"{default_secrets_path()}: {detail}"))
    return issues
