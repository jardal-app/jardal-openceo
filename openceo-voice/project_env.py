from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"


def load_project_env() -> None:
    """Load only the local env file for this project."""
    load_dotenv(ENV_PATH, override=False)
