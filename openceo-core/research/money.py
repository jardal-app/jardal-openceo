"""Dinero de research: enteros escalados en micro-USD, nunca floats.

Motivo (TASK-2026-00057): el grounding de Meta cuesta **$0.0025 por busqueda**, y
los tokens se facturan aparte. Con centavos enteros esa tarifa se redondearia a 0 y
el ledger diria que una operacion pagada fue gratis. Se usa un entero escalado
(1 USD = 1_000_000 micro-USD) con precision suficiente para tarifas subcentavo y
para sumas exactas; la frontera JSON/servicio serializa como cadena decimal.

Regla del epico: **desconocido es `None`, nunca 0**. Un importe no medido no es un
importe cero.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

MICRO_PER_USD = 1_000_000

__all__ = [
    "MICRO_PER_USD",
    "MoneyFormatError",
    "format_micro",
    "micro_from_decimal",
    "micro_from_str",
    "micro_from_value",
    "parse_optional_micro",
]


class MoneyFormatError(ValueError):
    """El texto recibido no es un importe decimal valido."""


def micro_from_decimal(value: Decimal) -> int:
    """Convierte USD a micro-USD redondeando al micro mas cercano.

    Se redondea solo cuando el valor ya es un decimal explicito (configuracion o
    tarifa publicada). Nunca se redondea "a centavos por llamada".
    """
    if not isinstance(value, Decimal):
        raise MoneyFormatError(f"Se esperaba Decimal, llego {type(value).__name__}")
    return int((value * MICRO_PER_USD).to_integral_value())


def micro_from_str(value: str | None) -> int | None:
    """Convierte una cadena decimal USD a micro-USD.

    Cadena vacia o `None` significan **desconocido** y devuelven `None`, que no es
    lo mismo que 0. Un valor mal formado lanza en vez de degradar a 0.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return micro_from_decimal(Decimal(text))
    except (InvalidOperation, ValueError) as exc:  # noqa: PERF203 - frontera unica
        raise MoneyFormatError(f"Importe USD invalido: {value!r}") from exc


def parse_optional_micro(value: str | int | None) -> int | None:
    """Acepta cadena decimal o entero micro-USD ya escalado."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool es int en Python: se rechaza explicitamente
        raise MoneyFormatError("Un booleano no es un importe")
    if isinstance(value, int):
        return value
    return micro_from_str(value)


def micro_from_value(value: str | int | Decimal | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MoneyFormatError("Un booleano no es un importe")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return micro_from_decimal(value)
    return micro_from_str(value)


def format_micro(micro: int | None) -> str | None:
    """Serializa micro-USD como cadena decimal de 6 posiciones.

    `None` se conserva como `None` (desconocido). Se usa cuantizacion explicita para
    que `2500` sea `"0.002500"` y no `"0.0025"` redondeado a algo menor.
    """
    if micro is None:
        return None
    quantized = (Decimal(int(micro)) / Decimal(MICRO_PER_USD)).quantize(Decimal("0.000001"))
    return format(quantized, "f")
