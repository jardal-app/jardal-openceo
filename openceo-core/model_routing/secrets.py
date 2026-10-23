"""Router secrets from ``openceo-core/.env.free-router.local`` (TASK-2026-00287).

Loaded with the same mechanism as ``.env.finance.local`` (pydantic-settings
``env_file``), declaring only ``FREE_ROUTER_*`` fields so the unprefixed
names in ``openceo-core/.env`` (where ``GROQ_API_KEY`` is the voice key and
may be paid) are never read. Refuses files with permissions wider than
0600 and never renders values in ``repr`` or errors.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "SECRETS_FILENAME",
    "RouterSecrets",
    "SecretsError",
    "default_secrets_path",
    "load_router_secrets",
]

SECRETS_FILENAME = ".env.free-router.local"
_CORE_DIR = Path(__file__).resolve().parents[1]


class SecretsError(RuntimeError):
    """Secrets file missing, unreadable or too permissive (no values)."""


class RouterSecrets(BaseSettings):
    """Free-router credentials; only ``FREE_ROUTER_*`` names are read."""

    model_config = SettingsConfigDict(
        env_file=(_CORE_DIR / SECRETS_FILENAME,),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    FREE_ROUTER_OPENROUTER_API_KEY: str = ""
    FREE_ROUTER_NVIDIA_NIM_API_KEY: str = ""
    FREE_ROUTER_GROQ_API_KEY: str = ""
    FREE_ROUTER_GEMINI_API_KEY: str = ""
    FREE_ROUTER_AA_API_KEY: str = ""
    FREE_ROUTER_HF_TOKEN: str = ""
    FREE_ROUTER_GATEWAY_KEY: str = ""
    FREE_ROUTER_LITELLM_MASTER_KEY: str = ""

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{name}={'<set>' if getattr(self, name) else '<empty>'}"
            for name in type(self).model_fields
        )
        return f"{type(self).__name__}({shown})"

    __str__ = __repr__

    def present(self) -> dict[str, bool]:
        """Per-key presence without exposing values."""
        return {name: bool(getattr(self, name)) for name in type(self).model_fields}


def default_secrets_path() -> Path:
    """Repo ``openceo-core/.env.free-router.local`` (a symlink in worktrees)."""
    return _CORE_DIR / SECRETS_FILENAME


def load_router_secrets(env_file: Path | None = None) -> RouterSecrets:
    """Load secrets after enforcing 0600-or-tighter permissions."""
    path = env_file or default_secrets_path()
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError as exc:
        raise SecretsError(f"secrets file missing or unreadable: {path}") from exc
    if mode & 0o077:
        raise SecretsError(f"secrets file {path} has permissions {mode:03o}; need 0600 or tighter")
    return RouterSecrets(_env_file=str(path))
