# Contrato de integración — OpenCEO Finance ↔ ERPNext

**Fecha:** 2026-09-21  
**Estado:** FINAL para implementación — **con una sección superada** (véase §14)  
**Repositorio dueño:** `jardal-openceo`  
**Decisión financiera canónica:** `jardal-erpnext/decision-2026-09-21-arquitectura-financiera-final.md`

> Este archivo no redefine contabilidad. Define **cómo OpenCEO consume ERPNext sin duplicarlo y cómo debe implementarse el adapter del agente Finance**.
>
> **Nota del 2026-09-24:** la frontera de *proyectos y tickets* cambió. `OPENCEO_DIRECTION.md`
> v1.1 asigna el trabajo empresarial a ERPNext, así que **§14 queda superada en esa frontera**
> (el detalle está en la propia sección). Todo lo demás —contabilidad, idempotencia,
> `OpenCEO Scope`, pagos— sigue vigente sin cambios.

## 0. Instrucción para el agente CLI que implemente este contrato

Al trabajar desde `/home/develop/development/Jardal/jardal-openceo/`:

1. Leer el README/arquitectura principal de OpenCEO y este contrato.
2. Respetar la decisión financiera canónica del repo ERPNext.
3. Implementar la integración dentro del agente Finance; no crear un microservicio financiero separado.
4. No crear un ledger paralelo en MongoDB.
5. No usar n8n para llamadas REST normales a ERPNext.
6. Mantener `execute_money_movements=false`.
7. Implementar primero lectura y registro seguro; ejecución monetaria real queda fuera de esta fase.
8. Añadir campos custom en ERPNext únicamente cuando un workflow real demuestre que son necesarios.

---

## 1. Frontera

OpenCEO Finance:

- interpreta lenguaje natural;
- aplica políticas propias;
- clasifica hacia Company / OpenCEO Scope / Account / Party;
- prepara operaciones;
- gestiona aprobaciones según política;
- consulta y escribe ERPNext mediante tools semánticos;
- invoca proveedores externos cuando una fase futura autorice movimientos reales;
- explica resultados al usuario.

ERPNext:

- guarda documentos financieros;
- valida contabilidad;
- mantiene libro mayor y saldos;
- controla presupuestos;
- mantiene cuentas por cobrar/pagar;
- registra pagos/cobros;
- concilia.

Proveedores externos:

- confirman el movimiento real de dinero.

MongoDB puede guardar estado de workflow, referencias y resúmenes fechados; **no replica el ledger**.

---

## 2. Ubicación del adapter

No crear microservicio nuevo ni workflow n8n para REST directo.

Estructura recomendada:

```text
openceo-core/
└── agents/
    └── finance/
        ├── prompt.md
        ├── tools.py
        ├── erpnext_client.py
        ├── requirements.txt
        └── .env.example
```

### `erpnext_client.py`

Cliente HTTP determinista de bajo nivel:

- autenticación;
- timeouts;
- retries seguros;
- operaciones REST solo sobre DocTypes permitidos;
- submit/cancel cuando el tool semántico lo autorice;
- propagación de identidades externas;
- normalización de errores;
- cero lógica LLM.

### `tools.py`

Expone operaciones semánticas cerradas.

Nunca exponer al LLM:

```text
create_document(doctype, arbitrary_payload)
run_method(arbitrary_python_path, arbitrary_args)
```

---

## 3. Regla de implementación incremental

**No se decide por adelantado qué DocTypes necesitan `openceo_ref`.**

Cada tool se diseña primero en términos de negocio. Luego se determina qué documentos ERPNext crea realmente. Solo esos DocTypes reciben el mecanismo de idempotencia que necesiten.

Ejemplo:

```text
finance.register_expense
  ↓
contrato técnico definido por implementación
  ↓
Purchase Invoice (+ quizá Payment Entry)
  ↓
solo esos documentos necesitan identidad idempotente de esa operación
```

No añadir campos custom “por si acaso”.

---

## 4. Superficie inicial de tools

La lista es el **mapa objetivo inicial**, no una obligación de implementar tools que todavía no tengan un caso real.

### Lectura

```text
finance.get_cash_position
finance.get_scope_summary
finance.get_budget_status
finance.list_pending_obligations
finance.get_contribution_10_status
```

### Escritura financiera

```text
finance.register_income
finance.register_expense
finance.register_internal_transfer
```

### Pagos / conciliación

```text
finance.prepare_payment
finance.confirm_external_payment
finance.sync_bank_transactions
finance.reconcile_transaction
```

### Maestros, solo donde el workflow lo requiera

```text
finance.ensure_company
finance.ensure_scope
finance.ensure_customer
finance.ensure_supplier
```

Cada tool valida un esquema cerrado de entrada. El agente nunca elige DocTypes arbitrarios.

Si un tool no es necesario para el primer flujo end-to-end, puede quedar pendiente; documentar la reducción en vez de implementar por anticipado.

---

## 5. Modelo de clasificación

Antes de registrar una transacción, Finance resuelve:

```text
owner       -> Company
scope       -> OpenCEO Scope
nature      -> Account
counterpart -> Customer/Supplier si aplica
```

Reglas:

- Docencia y trabajo remoto son Scope, no Company.
- Contrato/proyecto es Scope.
- Entidad propietaria de los libros es Company.
- Naturaleza económica se mantiene en Account.
- Cliente/proveedor se mantiene como Party.

El usuario puede expresar estos datos en lenguaje natural; la traducción a valores canónicos debe terminar en IDs/valores verificables antes de escribir.

---

## 6. Identidad e idempotencia

### 6.1 `openceo_ref` para operaciones

`openceo_ref` significa:

> ID idempotente y estable de una operación financiera originada por OpenCEO.

Reglas:

- mismo intento/retry → mismo valor;
- timeout no genera una nueva referencia;
- nullable para documentos no originados por OpenCEO;
- unicidad protegida en base de datos donde el campo exista;
- no se usa como identidad de maestros.

El campo existente en `Journal Entry` se conserva, pero **su mera existencia no obliga a usar Journal Entry en los workflows**.

Durante la implementación de cada tool:

1. identificar el/los DocTypes que realmente crea;
2. decidir cuál representa la operación idempotente;
3. añadir `openceo_ref` únicamente allí si hace falta;
4. probar retry, timeout y colisión.

Para eventos importados desde banco/Binance/BTCPay, usar preferentemente el ID nativo del proveedor + origen cuando eso dé identidad suficiente.

### 6.2 `openceo_id` para maestros

`openceo_id` es la identidad permanente de un maestro propiedad de OpenCEO.

`OpenCEO Scope` lo usa desde el inicio.

Para `Customer`, `Supplier`, `Company` u otros maestros:

- no añadir el campo antes de que exista un workflow real que los cree;
- si OpenCEO pasa a ser dueño de su creación, añadir identidad estable y operación `ensure_*` idempotente.

Patrón:

```text
buscar por openceo_id
  ├─ existe -> devolver existente
  └─ no existe -> crear -> devolver creado
```

No mezclar `openceo_id` con `openceo_ref`.

---

## 7. Estados de pago

OpenCEO debe distinguir al menos:

```text
planned
prepared
awaiting_approval
approved
execution_requested
externally_confirmed
recorded
reconciled
failed
```

Una respuesta HTTP exitosa de OpenCEO/adapter **no equivale** a dinero movido.

Solo evidencia verificable del proveedor permite pasar a `externally_confirmed`.

ERPNext registra el movimiento y la conciliación en los pasos posteriores apropiados.

---

## 8. Política de side effects

Mientras `execute_money_movements=false`:

- se puede consultar;
- se pueden clasificar movimientos;
- se pueden registrar movimientos ya ocurridos;
- se pueden preparar obligaciones/pagos;
- no se ejecutan transferencias externas reales.

Activar ejecución monetaria real requiere una decisión posterior y pruebas específicas de:

- idempotencia;
- timeout después de side effect;
- retry;
- doble callback/webhook;
- conciliación;
- reverso/fallo;
- auditoría;
- aprobación humana donde corresponda.

---

## 9. Workflows deterministas

Una vez conocido el flujo, debe cristalizarse en YAML/Python determinista siguiendo la arquitectura de OpenCEO.

Ejemplo conceptual:

```yaml
workflow: finance_register_expense
steps:
  - id: resolve
    tool: finance.resolve_accounting_context
  - id: ensure_scope
    tool: finance.ensure_scope
  - id: register
    tool: finance.register_expense
```

El LLM puede ayudar en discovery o clasificación semántica, pero no altera libremente los pasos de un workflow cristalizado.

`resolve_accounting_context` puede ser un helper/tool si la implementación demuestra que mejora el flujo; no es obligación introducirlo como nueva API pública si basta lógica interna determinista.

---

## 10. Regla del 10%

Implementar como función determinista de Finance.

Entrada:

- período;
- política explícita de ingresos elegibles;
- datos confirmados desde ERPNext.

Salida conceptual:

```json
{
  "eligible_income": "Decimal",
  "target_contribution": "Decimal",
  "confirmed_contributions": "Decimal",
  "pending_contribution": "Decimal",
  "as_of": "ISO-8601"
}
```

No usar `float` para dinero.

El LLM puede explicar el resultado; no hace la aritmética autoritativa.

---

## 11. Dinero y tipos numéricos

- usar `Decimal` en Python;
- nunca `float` para cantidades monetarias;
- conservar moneda explícita;
- conservar la tasa realmente usada por la transacción;
- no asumir que la tasa diaria P2P reemplaza la tasa real de una operación;
- respetar USDT como moneda distinta de USD.

---

## 12. Credenciales y permisos

Variables sugeridas en `.env.finance.local`:

```text
ERPNEXT_BASE_URL=
ERPNEXT_API_KEY=
ERPNEXT_API_SECRET=
ERPNEXT_TIMEOUT_SECONDS=
```

El usuario API debe:

- ser exclusivo de OpenCEO;
- tener permisos mínimos;
- no ser `Administrator`;
- quedar auditado como identidad propia.

Los permisos se amplían solo cuando un nuevo tool necesita un DocType adicional.

---

## 13. No usar n8n para el adapter base

Las llamadas REST a ERPNext son integración directa usada por Finance y pertenecen al Local Tool.

n8n solo entra si aparece un webhook externo o una orquestación compleja que realmente lo justifique.

---

## 14. No duplicar dominios de OpenCEO

> **⚠️ SUPERADO EN LA FRONTERA DE PROYECTOS Y TICKETS (2026-09-24).**
> La asignación de *proyectos, tareas y tickets* a OpenCEO que hace esta sección queda
> **superada** por [`OPENCEO_DIRECTION.md`](OPENCEO_DIRECTION.md) v1.1 (2026-09-23), que
> asigna el trabajo empresarial a ERPNext, y por
> [`ARCHITECTURE_REFERENCE_2026.md`](ARCHITECTURE_REFERENCE_2026.md) §System Boundaries.
> Desde el 2026-09-24 los tickets viven en ERPNext (`Task`) y la identidad de proyecto en
> `Project`; MongoDB conserva el estado de **ejecución**. Decisión operativa y plan en
> `PENDINGS.md` → ERPNext.
>
> **Lo que sigue vigente de esta sección:** OpenCEO conserva objetivos, conversación y
> workflows empresariales generales; `OpenCEO Scope` sigue siendo la dimensión
> **financiera** y no un segundo gestor de proyectos. Ejes distintos que conviven:
> `Project` es identidad de *trabajo*, `OpenCEO Scope` es el eje *contable*.
>
> Se conserva el texto original abajo como registro de la decisión anterior.

OpenCEO sigue siendo dueño de:

- proyectos/tareas;
- objetivos;
- tickets;
- conversación;
- workflows empresariales generales.

No adoptar ERPNext Project, CRM, Helpdesk o equivalentes como segunda fuente canónica.

`OpenCEO Scope` es una dimensión financiera, no un segundo gestor de proyectos.

---

## 15. Contrato de lectura

Toda respuesta financiera que dependa de ERPNext debe poder asociarse internamente con:

- fecha/hora de corte;
- Company;
- moneda;
- Scope/filtros aplicados;
- IDs de documentos relevantes cuando aplique.

No guardar en MongoDB una copia viva de saldos como nueva fuente de verdad.

Cachés/resúmenes deben tener fecha de corte y poder reconstruirse desde ERPNext.

---

## 16. Orden recomendado de implementación

### Fase A — conexión y lectura

1. `ERPNextClient` + auth/timeouts/errores.
2. health/read mínimo.
3. `get_cash_position` / `get_scope_summary` sobre datos de prueba.
4. verificar Decimal, moneda y fechas.

### Fase B — primer write end-to-end

Elegir **un solo flujo real**, preferentemente `register_expense` o `register_income`.

1. definir exactamente qué DocTypes ERPNext crea;
2. añadir idempotencia solo donde ese flujo la necesite;
3. probar create/retry/timeout/colisión;
4. comprobar GL/P&L/Scope resultante.

### Fase C — maestros necesarios

Implementar `ensure_scope` y después solo los `ensure_*` realmente necesarios para el flujo.

### Fase D — presupuestos y multimoneda

- budget por Scope;
- tasa explícita de transacción;
- USDT/BOB sin alterar el mecanismo diario existente.

### Fase E — pagos sin ejecución externa

- preparar pago;
- registrar confirmación simulada/controlada;
- Payment Entry / Bank Transaction según diseño real;
- conciliación.

### Fase F — ejecución real

Fuera de este contrato inicial. Requiere nueva decisión y `execute_money_movements=true` explícito.

---

## 17. Pruebas obligatorias antes de adopción

### Identidad

- crear Scope con `openceo_id`;
- repetir con mismo ID y obtener el mismo maestro;
- intentar colisión controlada.

### Primer flujo transaccional

- crear operación con `openceo_ref` donde corresponda;
- repetir exactamente la misma operación y demostrar que no duplica;
- simular timeout después de creación y repetir;
- demostrar que un `openceo_ref` diferente sí crea una operación distinta.

### Contabilidad

Según los tools implementados:

- ingreso;
- gasto;
- transferencia interna;
- invoice + cobro/pago cuando forme parte del diseño;
- multimoneda con tasa explícita;
- presupuesto por Scope;
- consulta filtrada por Scope.

### Pagos

Antes de permitir ejecución real:

- preparar sin ejecutar;
- callback duplicado;
- confirmación externa;
- registro contable;
- conciliación;
- recuperación de fallos.

### Regresión ERPNext

- scheduler activo;
- timezone `America/La_Paz`;
- USDT intacto;
- tasa diaria P2P intacta;
- `execute_money_movements=false`.

---

## 18. Criterio de aceptación de la integración inicial

La primera integración se considera utilizable cuando:

1. existe un cliente ERPNext determinista y probado;
2. no hay tool genérico de escritura arbitraria;
3. `OpenCEO Scope` funciona end-to-end;
4. existe al menos un flujo real de ingreso o gasto idempotente;
5. el retry de ese flujo no duplica documentos;
6. lectura de saldos/gasto por Scope coincide con ERPNext;
7. multimoneda conserva moneda y tasa de transacción;
8. budget por Scope está probado si forma parte del primer release;
9. los tests pasan con movimientos monetarios externos deshabilitados.

No es requisito de la primera integración implementar todos los tools del mapa objetivo.

---

## 19. Regla final del adapter

> **Finance interpreta intención y política; tools deterministas convierten esa intención en operaciones permitidas; ERPNext valida y registra; el proveedor externo confirma el dinero real. La integración crece por flujos demostrados, no por campos y endpoints creados preventivamente.**
