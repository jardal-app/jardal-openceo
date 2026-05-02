from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from settings import Settings


class ConfigLoader:
    """Loads per-project YAML configuration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._repo_root = Path(__file__).resolve().parent.parent

    def available_projects(self) -> list[str]:
        projects_root = self._resolve_projects_root()
        if not projects_root.exists():
            return []

        names: list[str] = []
        for candidate in projects_root.iterdir():
            if not candidate.is_dir():
                continue
            if (candidate / "project.yaml").exists():
                names.append(candidate.name)
        return sorted(names)

    def has_project(self, project: str | None) -> bool:
        if not project:
            return False
        return (self._resolve_projects_root() / project / "project.yaml").exists()

    def load_project(self, project: str | None) -> dict[str, Any]:
        if not project:
            return self._merge_with_defaults("", {})
        cfg_path = self._resolve_projects_root() / project / "project.yaml"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return self._merge_with_defaults(project, data)
        return self._merge_with_defaults(project, {})

    def context_path(self, project: str | None) -> Path:
        base = self._resolve_project_context_root() / (project or "_global")
        base.mkdir(parents=True, exist_ok=True)
        return base / "CONTEXT.md"

    def project_root(self, project: str | None) -> Path:
        base = self._resolve_projects_root() / (project or "_global")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _resolve_projects_root(self) -> Path:
        configured = Path(self._settings.PROJECTS_DIR)

        candidates = [configured] if configured.is_absolute() else [
            configured,
            Path(__file__).resolve().parent / self._settings.PROJECTS_DIR,
            self._repo_root / self._settings.PROJECTS_DIR,
        ]
        for candidate in candidates:
            if self._has_project_configs(candidate):
                return candidate
        for candidate in candidates:
            if candidate.exists():
                return candidate

        # Fallback path even if it does not exist yet.
        return self._repo_root / self._settings.PROJECTS_DIR

    def _resolve_project_context_root(self) -> Path:
        configured = Path(self._settings.PROJECT_CONTEXTS_DIR)
        if configured.is_absolute():
            return configured
        return (Path(__file__).resolve().parent / configured).resolve()

    @staticmethod
    def _has_project_configs(path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        return any(
            candidate.is_dir() and (candidate / "project.yaml").exists()
            for candidate in path.iterdir()
        )

    @staticmethod
    def _merge_with_defaults(project: str, cfg: dict[str, Any]) -> dict[str, Any]:
        defaults = {
            "project": {
                "name": project,
                "repos": [],
            },
            "dev": {
                "coding_tool": "claude",
                "coding_tier": "auto",
                "default_provider": "auto",
                "branch_pattern": "{session_id}-{slug}",
                "auto_push": False,
                "create_pr": True,
                "pr_body": "empty",
                "tier_s": {
                    "enabled": False,
                    "iterations": 2,
                    "timeout_minutes": 15,
                    "lead": "claude",
                    "reviewers": ["codex", "gemini", "opencode"],
                    "keep_sessions_alive": True,
                    "max_steps": 12,
                },
                "critical_consensus": {
                    "enabled": False,
                    "iterations": 2,
                    "timeout_minutes": 15,
                    "lead": "claude",
                    "reviewers": ["codex", "gemini", "opencode"],
                    "keep_sessions_alive": True,
                    "max_steps": 12,
                },
                "providers": {
                    "codex": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "claude": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "gemini": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "opencode": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "glm": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "kimi": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                },
                "qa": {
                    "playwright": "optional",
                    "confidence_threshold": 0.9,
                    "max_retries": 2,
                },
            },
            "voice": {
                "peer_programming": {
                    "enabled": True,
                    "require_project_selection": True,
                }
            },
            "pm": {
                "default_tool": "langraph",
            },
            "routing": {
                "llm_fallback": True,
                "default_agent": "dev",
                "agents": {
                    "pm": {
                        "triggers": [
                            "entrevista",
                            "requerimiento",
                            "requerimientos",
                            "requirements",
                            "historias de usuario",
                            "user stories",
                            "crear tickets",
                            "genera tickets",
                            "extrae tickets",
                        ],
                        "patterns": [
                            r"\b(?:crear|generar|extraer)\s+tickets?\b",
                            r"\bhistorias?\s+de\s+usuario\b",
                        ],
                    },
                    "finance": {
                        "triggers": [
                            "invoice",
                            "bank",
                            "reconcile",
                            "transaction",
                            "payment",
                            "pago",
                            "pagar",
                            "factura",
                            "finanzas",
                            "elfec",
                            "luz",
                        ],
                        "patterns": [],
                    },
                    "secretary": {
                        "triggers": [
                            "email",
                            "correo",
                            "calendar",
                            "calendario",
                            "meeting",
                            "reunion",
                            "reunión",
                            "follow-up",
                            "seguimiento",
                            "outreach",
                        ],
                        "patterns": [],
                    },
                    "sales": {
                        "triggers": [
                            "lead",
                            "crm",
                            "quotation",
                            "cotizacion",
                            "cotización",
                            "whatsapp",
                            "prospect",
                        ],
                        "patterns": [],
                    },
                    "marketing": {
                        "triggers": [
                            "campaign",
                            "ad",
                            "social",
                            "seo",
                            "content calendar",
                        ],
                        "patterns": [],
                    },
                    "media": {
                        "triggers": [
                            "video",
                            "image",
                            "music",
                            "publish",
                            "creative",
                        ],
                        "patterns": [],
                    },
                },
            },
            "finance": {
                "workflows": [],
            },
            "notifications": [],
        }
        merged = _deep_merge(defaults, cfg)
        dev_cfg = merged.get("dev")
        raw_dev_cfg = cfg.get("dev") if isinstance(cfg.get("dev"), dict) else {}
        if isinstance(dev_cfg, dict):
            tier_s_cfg = dev_cfg.get("tier_s")
            legacy_cfg = dev_cfg.get("critical_consensus")
            raw_has_tier_s = "tier_s" in raw_dev_cfg
            raw_has_legacy = "critical_consensus" in raw_dev_cfg
            if isinstance(tier_s_cfg, dict) and isinstance(legacy_cfg, dict):
                if raw_has_tier_s and not raw_has_legacy:
                    dev_cfg["critical_consensus"] = _deep_merge(legacy_cfg, tier_s_cfg)
                elif raw_has_legacy and not raw_has_tier_s:
                    dev_cfg["tier_s"] = _deep_merge(tier_s_cfg, legacy_cfg)
                else:
                    dev_cfg["tier_s"] = _deep_merge(legacy_cfg, tier_s_cfg)
                    dev_cfg["critical_consensus"] = _deep_merge(tier_s_cfg, legacy_cfg)
        return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
