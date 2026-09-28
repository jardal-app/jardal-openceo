"""Despacho determinista de la intención financiera. Cero logica LLM.

Un solo lugar decide si una frase es finanzas y de qué tipo. Existía como tres
imports sueltos de detectores en `first_layer.py`, `main.py` y `session.py`,
cada uno con su propio orden implícito: añadir un flujo nuevo obligaba a
acertar la precedencia en tres sitios, y un error ahí invierte el signo de un
asiento o contesta el saldo cuando se pedía un ingreso.

El orden es parte del contrato, no un detalle:

1. **ingreso** antes que gasto — los verbos se solapan ("registra ingreso
   500 bs" cumple el detector de gasto);
2. **aporte (regla del 10%)** antes que saldo y antes que gasto — "cuánto" lo
   reclamaría el saldo, y "registra mi diezmo 500 bs" no puede terminar en
   `Gastos varios - J`;
3. **saldo** antes que gasto — preguntar no registra;
4. **gasto** al final, que es la escritura por defecto.

Las escrituras (ingreso, gasto) descartan preguntas; las lecturas no, porque
una pregunta es exactamente lo que atienden. Corre antes del LLM: no cuesta
tokens y sigue vivo con el presupuesto de IA agotado (Horizon §9.7).
"""

from __future__ import annotations

from agents.finance.contribution import looks_like_contribution_query
from agents.finance.scope_summary import looks_like_balance_query
from agents.finance.voice_expense import looks_like_expense
from agents.finance.voice_income import looks_like_income

INCOME = "income"
CONTRIBUTION = "contribution"
BALANCE = "balance"
EXPENSE = "expense"


def classify(text: str) -> str | None:
    """Devuelve `income`, `contribution`, `balance`, `expense` o None."""
    blob = str(text or "").strip()
    if not blob:
        return None
    if looks_like_income(blob):
        return INCOME
    if looks_like_contribution_query(blob):
        return CONTRIBUTION
    if looks_like_balance_query(blob):
        return BALANCE
    if looks_like_expense(blob):
        return EXPENSE
    return None


def is_finance_text(text: str) -> bool:
    return classify(text) is not None
