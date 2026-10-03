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

        discovered: list[str] = []
        for candidate in projects_root.iterdir():
            if not candidate.is_dir():
                continue
            if (candidate / "project.yaml").exists():
                discovered.append(candidate.name)

        if not discovered:
            return []

        # Sort projects by 'number' (or 'order' / 'id') defined directly in each project.yaml
        def _get_project_sort_key(name: str) -> tuple[int, str]:
            cfg_path = projects_root / name / "project.yaml"
            if cfg_path.exists():
                try:
                    with cfg_path.open("r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh) or {}
                    p_info = data.get("project") if isinstance(data.get("project"), dict) else {}
                    num = data.get("number")
                    if num is None:
                        num = p_info.get("number")
                    if num is None:
                        num = data.get("order") or p_info.get("order")
                    if num is None:
                        num = data.get("id") or p_info.get("id")

                    if num is not None:
                        try:
                            return (int(num), name.lower())
                        except (ValueError, TypeError):
                            pass
                except Exception:
                    pass
            return (9999, name.lower())

        return sorted(discovered, key=_get_project_sort_key)

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

    def validate_project(self, project: str | None) -> list[str]:
        """Check raw project.yaml for obsolete or misplaced keys.

        Returns human-readable errors; an empty list means the project may
        run. Tier S design §15: Tier S config is global (the defaults above),
        so any ``dev.tier_s`` content in a project.yaml is invalid, and
        ``dev.coding_tool`` is obsolete (§3: routing is per-task). Callers
        park the project's tickets in ``waiting(config)`` when this is
        non-empty; other projects are unaffected.
        """
        if not project:
            return []
        cfg_path = self._resolve_projects_root() / project / "project.yaml"
        if not cfg_path.exists():
            return []
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:
            return [f"{project}/project.yaml: unreadable ({exc})"]
        if not isinstance(data, dict):
            return [f"{project}/project.yaml: top level must be a mapping"]
        dev = data.get("dev")
        if dev is None:
            return []
        if not isinstance(dev, dict):
            return [f"{project}/project.yaml: 'dev' must be a mapping"]
        errors: list[str] = []
        if "coding_tool" in dev:
            errors.append(
                "dev.coding_tool is obsolete: routing is per-task "
                "(TaskDocument.coding_tool, set at creation). Remove it from "
                f"{project}/project.yaml."
            )
        tier_s = dev.get("tier_s")
        if tier_s is not None and tier_s != {}:
            if isinstance(tier_s, dict):
                found = ", ".join(sorted(str(key) for key in tier_s))
                errors.append(
                    "dev.tier_s must not be set in project.yaml: Tier S roles "
                    f"and pool are global. Remove it from {project}/project.yaml "
                    f"(found: {found})."
                )
            else:
                errors.append(
                    f"dev.tier_s in {project}/project.yaml must be a mapping or absent."
                )
        return errors

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
                # No dev.coding_tool default: routing is per-task
                # (TaskDocument.coding_tool). A project default would silently
                # route untagged tickets (Tier S design §3).
                "coding_tier": "auto",
                "default_provider": "auto",
                "branch_pattern": "{session_id}-{slug}",
                "auto_push": False,
                "create_pr": True,
                "pr_body": "empty",
                # Global Tier S config: the single source of roles and pool.
                # Projects must not set dev.tier_s (see validate_project).
                "tier_s": {
                    "timeout_minutes": 15,
                    "roles": {
                        "planner": {"provider": "claude", "label": "claudej", "model": "opus", "active": True, "command": ["claudej"]},
                        "plan-reviewer": {"provider": "codex", "label": "codexj", "model": "gpt-6.1-sol", "active": True, "command": ["codexj"]},
                        "builder": {"provider": "muse", "label": "muse", "model": "muse-spark-1.3-contributor", "active": True, "command": ["muse", "--model", "muse-spark-1.3-contributor", "--reasoning-effort", "max", "--yolo"]},
                    },
                    "reviewers": [
                        {"label": "qwen", "provider": "qwen", "model": "qwen3.8-max", "family": "qwen", "active": True, "command": ["qwen", "-m", "qwen3.8-max", "--approval-mode", "plan"]},
                        {"label": "claudeg", "provider": "claude", "model": "glm-5.3", "family": "glm", "active": True, "command": ["claudeg", "--model", "opus", "--tools", "Read,Glob,Grep"]},
                        {"label": "claudep", "provider": "claude", "model": "deepseek-flash", "family": "deepseek", "active": True, "command": ["claudep", "--model", "opus", "--tools", "Read,Glob,Grep"]},
                        {"label": "agyd", "provider": "gemini", "model": "gemini", "family": "gemini", "active": False, "command": ["agyd"]},
                        {"label": "mimo", "provider": "opencode", "model": "opencode-go/mimo-v2.6-pro", "family": "mimo", "active": False, "command": ["opencode"]},
                    ],
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
                    "muse": {
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
                            "gasto",
                            "gastos",
                            "registra",
                            "registrar",
                            "anota",
                            "anotar",
                            "bs",
                            "bolivianos",
                            "boliviano",
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
            "research": {
                # Politica de research POR PROYECTO. Nace apagada de forma deliberada:
                # habilitar research es una decision explicita del proyecto, no un
                # efecto colateral de tener claves de proveedor en el entorno. Los
                # adaptadores pagados nacen apagados aunque exista clave.
                "enabled": False,
                "default_mode": "quick",
                "allowed_modes": ["quick"],
                # Techo de clasificacion que este proyecto puede pedir. El servidor
                # recorta cualquier peticion por encima; el payload no eleva derechos.
                "classification_ceiling": "public",
                "adapters": {
                    "brave": True,
                    "serper": False,
                    "firecrawl": False,
                    "grounding": False,
                },
                # Presupuesto pagado aprobado para este proyecto (USD, cadena decimal).
                # "0.00" significa pago cero: no se llaman proveedores pagados.
                "paid_budget_usd": "0.00",
                "limits": {
                    "quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120},
                    "deep": {"search_requests": 15, "pages": 25, "steps": 30, "seconds": 900},
                    # ``watch`` define lote/periodo por suscripcion (Entrega B).
                    "watch": {"search_requests": 0, "pages": 10, "steps": 0, "seconds": 60},
                },
                # Referencias de cuenta autorizadas. Vacio = ninguna cuenta social.
                "allowed_account_refs": [],
            },
        }
        return _deep_merge(defaults, cfg)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
