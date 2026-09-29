Agente de finanzas operativo para flujos deterministas de pago y registro.

Flujos implementados:

1. Gasto ocasional por voz (TASK-2026-00032)
   - El dueño habla con el CEO y el CEO deriva a Finance; `@finance` es atajo.
   - Ejemplos: "registra gasto personal carne 50 bolivianos",
     "pago de luz de casa 300 bs".
   - Ningún gasto crea ticket: el registro en ERPNext es el efecto.
   - `finance.register_expense` sobre `Journal Entry` con `openceo_ref`
     único (reintento = mismo ref, HTTP 417 = releer, no duplicar). En el
     camino del workflow, el ref se deriva de la tarea (`voice-expense:<task_id>`),
     no del azar: un re-despacho no puede duplicar el asiento.
   - Débito a gasto / crédito a `Efectivo - J`, Company `Jardal`,
     Cost Center `Principal - J`, moneda explícita (BOB por defecto),
     `Decimal` siempre. Luz/agua → `Servicios públicos - J`;
     transporte → `CARGOS DE TRANSITO Y TRANSPORTE - J`; teléfono →
     `Cuenta telefonica - J`; sueldos → `Salario. - J`;
     resto personal/hogar → `Gastos varios - J`.
   - Contabiliza gasto ya ocurrido; `execute_money_movements=false` intacto.
   - **Moneda distinta a la de la Company**: mismo criterio que el ingreso —
     sin tasa real no se escribe nada; con tasa se convierte a BOB y la tasa
     queda en el `user_remark` (contrato §11).

2. Ingreso ocasional por voz (TASK-2026-00053)
   - Espejo del gasto con el asiento invertido: débito `Efectivo - J` /
     crédito a la cuenta de ingreso.
   - Ejemplos: "registra ingreso sueldo 6200 bolivianos",
     "me pagaron 6.200 bolivianos del contrato",
     "anota ingreso docencia 3800 bs".
   - `finance.register_income`, mismo `Journal Entry` idempotente por
     `openceo_ref` (prefijo `voice-income:`, derivado de la tarea en el
     camino del workflow).
   - Cuentas de ingreso reales de Jardal, sin inventar: trabajo remoto,
     docencia y honorarios → `Servicios - J`; venta de bienes → `Ventas - J`;
     intereses → `Interest Income - J`; plazo fijo →
     `Interest on Fixed Deposits - J`.
   - **Moneda distinta a la de la Company**: si el dictado viene en USD/USDT
     hace falta la tasa real de la operación. Sin tasa el flujo **no escribe
     nada** y la pide; con tasa convierte el asiento a BOB y deja la tasa en el
     `user_remark`. Nunca se contabiliza "500 dólares" como Bs 500 (contrato §11).
   - Contabiliza dinero ya registrado como ingreso. No emite facturas ni mueve dinero.

3. Frontera ingreso/gasto (lo que decide el signo del asiento)
   - Un solo despachador: `agents/finance/intent.py`. Orden ingreso →
     aporte → saldo → gasto; el mismo clasificador lo usa Horizon para no
     quedarse con frases de dinero.
   - Un ingreso exige señal de cobro real: "ingreso", un cobro en primera
     persona ("me pagaron", "cobré", "recibí") o un sustantivo de ingreso
     etiquetando un monto ("sueldo 6200 bolivianos"). "pagaron" desnudo y los
     sustantivos con complemento ("honorarios del contador", "sueldo del
     empleado", "depósito de garantía") **no** son ingreso: son egresos.
   - "pago de sueldo 3000 bs" es **gasto** (sustantivo de ingreso con orden
     de pago); "me pagaron el sueldo 6200 bs" es **ingreso**.
   - Una pregunta no registra nada: "¿cuándo me pagaron los 500 dólares?"
     no contabiliza. Solo cuenta como pregunta si el interrogativo abre la
     frase, porque la voz no trae tildes ("registra como gasto 50 bs" sí se
     registra). Las lecturas sí atienden preguntas.

4. Lectura de saldo y gasto por Scope (TASK-2026-00044)
   - Ejemplos: "cuánto gastamos este mes", "dame el saldo de la caja".
   - `finance.get_scope_summary`: **solo lectura** sobre `GL Entry` vía el
     lector dedicado `list_gl_entries` (`GL Entry` no entra a la allowlist
     de escritura). Sin ledger paralelo en Mongo.
   - El período se deriva del dictado ("este mes", "el mes pasado", "esta
     semana", "hoy", "ayer", "este año"); sin palabras de período la lectura es
     histórica y **lo dice**.
   - Toda respuesta lleva fecha de corte, Company, moneda, Cost Center,
     período medido y filtros aplicados (contrato §15), y avisa si quedaron
     asientos fuera por moneda distinta o si se alcanzó el límite de filas
     (en ese caso el total es parcial y la voz lo advierte).

5. Regla del 10% (TASK-2026-00054)
   - Ejemplos: "cómo vamos con el diezmo", "cuánto debo del 10 %".
   - `finance.get_contribution_10_status`: **solo lectura**. Ingreso
     elegible = créditos a cuentas de ingreso del período; objetivo = 10%;
     aritmética determinista en `Decimal`.
   - La medición **no** verifica la contrapartida de caja: un devengado cuenta
     igual que un cobro, así que la voz dice "el libro registra ... en cuentas
     de ingreso" y no "cobramos".
   - El detector de la consulta reclama siempre el vocabulario inequívoco
     ("diezmo", "regla del 10") y, para "aporte"/"donación"/"contribución",
     exige pregunta: "registra gasto del aporte patronal 1200 bs" es un gasto,
     no una consulta del 10%.
   - Movimientos entre cuentas propias y deuda nueva **no** son ingreso
     elegible: no se diezma dos veces el mismo dinero.
   - El plan de cuentas de Jardal **no tiene cuenta de aportes/donaciones**,
     así que lo aportado se devuelve como `None` con `state="needs_input"`
     y la razón, **nunca como 0**: desconocido no es cero. Definir esa
     cuenta es decisión del dueño; cuando exista, se lista en
     `CONTRIBUTION_ACCOUNTS` y el estado pasa a `ok` sin más cambios
     (los tests parchean la constante para no quedar clavados al plan vivo).

6. Pago Elfec House (práctica segura)
   - Alias natural: Pago Luz Casa
   - Modo vigente: practica segura hasta la pantalla de tarjeta, sin confirmar el pago
