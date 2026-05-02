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
