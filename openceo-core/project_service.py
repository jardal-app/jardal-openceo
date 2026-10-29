from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config_loader import ConfigLoader
from db.mongo import MongoRepository
from settings import Settings


class ProjectService:
    """Project discovery and per-user project selection."""

    def __init__(self, settings: Settings, repo: MongoRepository, config_loader: ConfigLoader) -> None:
        self._settings = settings
        self._repo = repo
        self._config_loader = config_loader

    def list_projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in self._config_loader.available_projects():
            cfg = self._config_loader.load_project(name)
            rows.append(
                {
                    "name": name,
                    "title": cfg.get("project", {}).get("name", name),
                    "repo_count": len(cfg.get("project", {}).get("repos", [])),
                    "default_provider": cfg.get("dev", {}).get("default_provider", "auto"),
                }
            )
        return rows

    def get_project_by_index(self, index: int) -> str | None:
        projects = self._config_loader.available_projects()
        if 1 <= index <= len(projects):
            return projects[index - 1]
        return None

    def get_project_index(self, project: str) -> int | None:
        projects = self._config_loader.available_projects()
        for idx, name in enumerate(projects, start=1):
            if name.lower() == project.lower():
                return idx
        return None

    async def select_project(self, user_id: str, project: str) -> dict[str, Any]:
        if not self._config_loader.has_project(project):
            raise ValueError(f"Project not found: {project}")
        payload = await self._repo.set_selected_project(user_id, project)
        payload["updated_at"] = payload.get("updated_at") or datetime.now(timezone.utc)
        return payload

    async def resolve_project(self, user_id: str, explicit_project: str | None = None) -> str:
        if explicit_project:
            if not self._config_loader.has_project(explicit_project):
                raise ValueError(f"Project not found: {explicit_project}")
            return explicit_project

        selected = await self._repo.get_selected_project(user_id)
        if selected and self._config_loader.has_project(selected):
            return selected

        if self._config_loader.has_project(self._settings.DEFAULT_PROJECT):
            return self._settings.DEFAULT_PROJECT

        available = self._config_loader.available_projects()
        if available:
            return available[0]

        raise ValueError("No projects configured")

    async def get_project_presets(self, project: str) -> dict[str, dict[str, Any]]:
        """Return merged dictionary of project prompt presets from YAML and MongoDB.
        Presets are indexed by both lowercase name and numeric ID/order string.
        """
        presets: dict[str, dict[str, Any]] = {}
        if self._config_loader.has_project(project):
            cfg = self._config_loader.load_project(project)
            yaml_presets = cfg.get("presets", {})
            if isinstance(yaml_presets, dict):
                idx = 1
                for k, v in yaml_presets.items():
                    key_str = str(k).strip()
                    if isinstance(v, dict):
                        p_name = str(v.get("name") or key_str).strip().lower()
                        p_num = v.get("number") or v.get("id") or v.get("order")
                        try:
                            preset_num = int(p_num) if p_num is not None else idx
                        except (ValueError, TypeError):
                            preset_num = idx

                        prompt_val = str(v.get("prompt") or v.get("cmd") or v.get("command") or "")
                        entry = {
                            "name": p_name,
                            "number": preset_num,
                            "prompt": prompt_val,
                            "cmd": prompt_val,
                            "description": str(v.get("description") or ""),
                            "tmux": v.get("tmux", True),
                            "source": "yaml",
                        }
                        presets[p_name] = entry
                        presets[str(preset_num)] = entry
                        idx += 1
                    elif isinstance(v, str):
                        p_name = key_str.lower()
                        entry = {
                            "name": p_name,
                            "number": idx,
                            "prompt": v,
                            "cmd": v,
                            "description": "",
                            "tmux": True,
                            "source": "yaml",
                        }
                        presets[p_name] = entry
                        presets[str(idx)] = entry
                        idx += 1
        # Merge MongoDB presets (overrides/additions)
        try:
            db_presets = await self._repo.get_project_presets(project)
            for p in db_presets:
                k = p["name"].lower()
                prompt_val = str(p.get("prompt") or p.get("cmd") or "")
                p_num = p.get("number") or p.get("id") or p.get("order")
                entry = {
                    "name": k,
                    "number": int(p_num) if p_num is not None else len(presets) + 1,
                    "prompt": prompt_val,
                    "cmd": prompt_val,
                    "description": str(p.get("description") or ""),
                    "tmux": p.get("tmux", True),
                    "source": "mongo",
                }
                presets[k] = entry
                if p_num is not None:
                    presets[str(p_num)] = entry
        except Exception:  # noqa: BLE001, S110
            pass
        return presets
