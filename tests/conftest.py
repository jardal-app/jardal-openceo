"""Configuracion de la suite de tests.

Importa **temprano** las dependencias que otro modulo de test sustituye por stubs.

Motivo: `tests/test_dev_runtime_cleanup.py` reemplaza `motor.motor_asyncio` y
`fastmcp` por stubs si los modulos reales todavia no estan en `sys.modules`. El stub de
motor no tiene `drop_database` ni soporta `db["coleccion"]`, y el de fastmcp no expone
`Client`, asi que al correr la suite completa fallaban tests que usan las librerias de
verdad (research) solo por el orden de importacion, mientras pasaban en aislamiento.
Forzar la carga temprana elimina esa contaminacion global sin cambiar el
comportamiento de los tests que ya existian (los stubs solo existen para entornos donde
la dependencia no esta instalada).
"""

from __future__ import annotations

import fastmcp  # noqa: F401  (se importa por su efecto en sys.modules)
import motor.motor_asyncio  # noqa: F401  (se importa por su efecto en sys.modules)

import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_CORE = _REPO / "openceo-core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

# Recrea los symlinks de exposicion de skills (estado local, .codex/ esta
# ignorado): sin esto, un clon limpio falla en load_catalog antes de
# empezar (review 00007). Solo crea los que faltan, nunca reemplaza, asi
# que no enmascara derivas; en un checkout de solo lectura falla el test
# que necesite el link, como antes.
try:
    from catalog import (  # noqa: E402
        discover_role_skills,
        sync_skill_links,
    )
    sync_skill_links(discover_role_skills())
except OSError:
    pass


def pytest_configure(config):
    """Register the free-router ``live`` marker (TASK-2026-00287)."""
    config.addinivalue_line(
        "markers", "live: real-provider calls, only with FREE_ROUTER_LIVE=1"
    )


def pytest_collection_modifyitems(items):
    """Skip live tests unless the harness is explicitly enabled."""
    if os.environ.get("FREE_ROUTER_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live tests need FREE_ROUTER_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
