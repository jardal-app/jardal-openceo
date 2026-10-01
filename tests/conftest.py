"""Configuracion de la suite de tests.

Importa `motor.motor_asyncio` **antes** de que se importe ningun modulo de test.

Motivo: `tests/test_dev_runtime_cleanup.py` sustituye `motor.motor_asyncio` por un
stub si el modulo real todavia no esta en `sys.modules`. Ese stub no tiene
`drop_database` ni soporta `db["coleccion"]`, asi que al correr la suite completa los
tests que usan Mongo de verdad (research) fallaban solo por el orden de importacion,
mientras pasaban en aislamiento. Forzar la carga temprana elimina esa contaminacion
global sin cambiar el comportamiento de los tests que ya existian.
"""

from __future__ import annotations

import motor.motor_asyncio  # noqa: F401  (se importa por su efecto en sys.modules)
