"""Mongo endpoint for the Mongo-backed tests (TASK-2026-00985).

The shared server enforces auth. Tests connect with ``OPENCEO_TEST_MONGODB_URL``
(environment first, else the untracked repo-root ``.env.test``): a user limited
to the ``openceo_test_*`` databases. Without it they skip; a ``ping`` probe
alone succeeds unauthenticated and would turn the skip into failures.
"""

from __future__ import annotations

import os
from pathlib import Path

URL_VAR = "OPENCEO_TEST_MONGODB_URL"
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env.test"
_DEFAULT_URL = "mongodb://localhost:27017"


def _configured_url() -> str:
    url = os.environ.get(URL_VAR, "").strip()
    if url:
        return url
    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _DEFAULT_URL
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == URL_VAR and value.strip():
            return value.strip().strip("'\"")
    return _DEFAULT_URL


MONGO_URL = _configured_url()


def mongo_available(db_name: str, url: str = MONGO_URL) -> bool:
    """True when ``url`` may use ``db_name`` (a new test database needs a grant)."""
    try:
        from pymongo import MongoClient

        client = MongoClient(url, serverSelectionTimeoutMS=400)
        try:
            client[db_name].list_collection_names()
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - any failure means skip, never fail
        return False
    return True
