from __future__ import annotations

from typing import Any

from agents.finance.elfec import run_elfec_house_payment_practice
from settings import get_settings


def pago_elfec_house() -> dict[str, Any]:
    return run_elfec_house_payment_practice(get_settings())
