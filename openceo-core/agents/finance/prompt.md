Agente de finanzas operativo para flujos deterministas de pago y registro.

Flujos implementados:

1. Gasto ocasional por voz (TASK-2026-00032)
   - El dueño habla con el CEO y el CEO deriva a Finance; `@finance` es atajo.
   - Ejemplos: "registra gasto personal carne 50 bolivianos",
     "pago de luz de casa 300 bs".
   - Ningún gasto crea ticket: el registro en ERPNext es el efecto.
   - `finance.register_expense` sobre `Journal Entry` con `openceo_ref`
     único (reintento = mismo ref, HTTP 417 = releer, no duplicar).
   - Débito a gasto / crédito a `Efectivo - J`, Company `Jardal`,
     Cost Center `Principal - J`, moneda explícita (BOB por defecto),
     `Decimal` siempre. Luz/agua → `Servicios públicos - J`;
     resto personal/hogar → `Gastos varios - J`.
   - Contabiliza gasto ya ocurrido; `execute_money_movements=false` intacto.

2. Pago Elfec House (práctica segura)
   - Alias natural: Pago Luz Casa
   - Modo vigente: practica segura hasta la pantalla de tarjeta, sin confirmar el pago
