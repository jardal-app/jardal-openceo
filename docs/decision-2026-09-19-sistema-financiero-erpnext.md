# Decisión: sistema financiero de OpenCEO → ERPNext

**Fecha:** 2026-09-19
**Estado:** Decidido — **no desplegado**
**Alcance:** registro financiero personal, por proyecto y futuro por empresa
**Registrado en:** `jardal-horizon/README.md` §4.7 y §9.9 · trabajo en TASK-2026-00025 (ERPNext)

---

## 1. Decisión

**ERPNext** (GPL-3.0, self-hosted) es el sistema de registro financiero de OpenCEO.

La separación de responsabilidades que ya definía Horizon §4.7 se mantiene intacta:

| Componente | Responsabilidad | Fuente de verdad |
| --- | --- | --- |
| CEO con Horizon | Priorizar objetivos, decidir qué análisis hace falta | Objetivos, decisiones y tareas en OpenCEO |
| Agente Finance | Interpretar solicitudes, preparar operaciones, explicar resultados | Referencias al sistema financiero |
| **Sistema financiero (ERPNext)** | Cuentas, movimientos, conciliación, presupuestos, deudas | Registros financieros y cálculos verificables |
| Banco / BTCPay / proveedor | Procesar operaciones y comunicar su estado real | Confirmaciones externas |

OpenCEO **no** reconstruye un segundo libro contable. Guarda referencias y resúmenes con fecha de corte.

---

## 2. Requisito que motivó la decisión

Manejar todo el dinero — personal, por proyecto, y a futuro por empresa:

- Registro de ingresos, gastos y pagos
- Separación **por proyecto** hoy, **por entidad legal** en el futuro
- Objetivos con su "cuánto falta"
- Multi-moneda (BOB base, USD de referencia)
- Conectado al agente Finance por API, con identidad en cada escritura (Horizon §4.7: *"Cada escritura tiene identidad para evitar duplicados"*)
- Consultable aunque OpenCEO o el modelo de IA estén fuera de servicio (Horizon §4.7)
- Presupuesto de herramientas nuevas: **USD 0** (Horizon §4.6)

---

## 3. Por qué ERPNext

| Criterio | ERPNext | Verificación |
| --- | --- | --- |
| **"Por proyecto"** | **Cost Center y Project son dimensiones contables nativas**, por línea y por documento, filtrables en General Ledger, P&L y Balance | [Accounting Dimensions](https://docs.frappe.io/erpnext/accounting-dimensions) |
| **"Por empresa"** | Multi-company nativo + **Inter Company Journal Entry**, sin gating | [Inter Company Journal Entry](https://docs.frappe.io/erpnext/inter-company-journal-entry) |
| Partida doble | Completa, sin edición pagada | GPL-3.0, edición única |
| API | REST autogenerada por DocType, OAuth2, API Request Log | [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest) |
| Costo | USD 0 | — |
| Independencia | Sistema separado; sobrevive sin IA ni OpenCEO | — |

**Caveat registrado:** Cost Center y Project están asociados a una company ([frappe/erpnext#45625](https://github.com/frappe/erpnext/issues/45625)). Un proyecto que cruce dos empresas requiere una dimensión custom sin campo company. **Decidir al inicio, no después.**

---

## 4. Alternativas descartadas

### Odoo — descartado

El modelo de datos de partida doble existe en Community (`account_move`, `account_move_line`), pero **la contabilidad está mutilada en la edición gratis**:

- `account_accountant` devuelve **404** en `odoo/odoo` — es Enterprise
- La tabla oficial de ediciones marca **"Accounting"** y **"Comprehensive Accounting"** (General Ledger, Bank Statement Reconciliation, **Analytic Accounting**, Budgets, Reports) como **NO en Community**
- Conciliación bancaria: *"In Odoo Community, bank reconciliation is not available"*
- **La contabilidad analítica — que es justo lo que da "por proyecto" — es Enterprise.** Odoo Community no cumple el requisito principal
- Sin servicio oficial de upgrade para Community (`upgrade.odoo.com` exige suscripción Enterprise)
- Odoo Enterprise (plan Custom, único con on-premise + API externa): **€37.40/usuario/mes**

**Módulos de Odoo que chocarían con OpenCEO** (todos Community y gratis, y por eso son la tentación): Project, Kanban, CRM, Helpdesk, Calendar, Discuss, To-Do. Duplican el Kanban Mission Control de `openceo-react`, las notificaciones Telegram, la app Android LiveKit y las colecciones `tasks`/`objectives`/`schedules`. Adoptarlos crearía una **tercera superficie de tareas** (Mongo + Notion + Odoo), violando la ley del README: *"MongoDB is the canonical runtime store… External systems are projections or mirrors, not the authoritative source of truth."*

Fuentes: [Editions](https://www.odoo.com/page/editions) · [Pricing](https://www.odoo.com/pricing)

### Notion como libro — descartado

Sigue siendo correcto como **espejo de tickets para ojos humanos**. No puede ser el libro de dinero:

| Requisito | Notion |
| --- | --- |
| Precisión decimal verificable | **No documentada.** `number` se describe como *"A number representing some value."* Sin tipo. `format` es solo presentación |
| Control de concurrencia | **Inexistente y no documentado** |
| Idempotencia | El código `idempotency_key_reused` existe, pero **no hay forma documentada de enviar la clave**. No usable |
| Escritura atómica multi-objeto | **No existe en la API REST** |
| Auditoría | `last_edited_by` **no atribuye los bots internos de Notion** |
| Volumen | **Tope de 10,000 resultados por query**; workaround por `created_time`, que Notion guarda al minuto |
| Rate limit | **180 req/min (3/s)** en planes no-Business, más límite por workspace |
| Export | Sin Enterprise, **sin endpoint**. Solo UI |
| Self-hosted | **No existe** |

Evidencia de primera mano en este repo: `openceo-core/task_service.py:89-102` ya serializa las escrituras a Notion con `await asyncio.sleep(1)` — *"~1 req/s to avoid 429"*. Viable para espejar tickets; inviable para importar extractos o conciliar.

Fuentes: [Request limits](https://developers.notion.com/reference/request-limits) · [Pagination](https://developers.notion.com/reference/pagination)

### Firefly III — descartado

Partida doble real, API REST completa con Personal Access Tokens, multi-moneda, ligero. **Pero no soporta entidades legales** — separa por tags, no por company. No llega al requisito "a futuro por empresa", y migrar un libro contable después es lo más caro de rehacer (historial, saldos de apertura, conciliaciones).

### Actual Budget — descartado de raíz

**No expone API HTTP.** Su documentación: *"Actual does not expose HTTP endpoints that can be called."* Solo una librería Node que descarga una copia local del presupuesto. **Incompatible con el agente Finance.**

### Construir un motor propio — descartado como primer paso

Horizon §4.7 es explícito: *"Antes de construir un motor contable propio, evaluar software existente contra casos reales, API, exportación, permisos, monedas, mantenimiento"*, y *"no intentar construir un ERP completo antes de resolver el registro actual"*. Si el software existente falla la evaluación, el desarrollo propio tendría repositorio separado con el adaptador en OpenCEO.

---

## 5. Fronteras de responsabilidad

| Capa | Dueño | Razón |
| --- | --- | --- |
| Asientos, cuentas, saldos, conciliación | **ERPNext** | Partida doble, integridad, independiente de la IA |
| Captura y consulta (voz, chat, Telegram) | **Agente Finance** | La UI de un ERP es inadecuada para gasto diario; aquí está el valor de OpenCEO |
| Objetivos y metas | **OpenCEO `objectives`** | Son objetivos de vida, no cuentas contables |
| Crypto (BTC / Lightning) | **BTCPay Server** | Ya es fuente de verdad; se sincroniza **hacia** ERPNext, no se duplica |
| Tickets | MongoDB → espejo Notion | Sin cambios. **Notion nunca toca dinero** |

### Regla de adopción

> **Adoptar el complemento, nunca el duplicado.**

De ERPNext **no** se usan los módulos Project, CRM, Helpdesk, Calendar ni Discuss. Solo el dominio financiero: Accounting, Invoicing, Purchase, y a futuro Payroll e Inventory.

---

## 6. Riesgos conocidos y capacidad medida

### Host medido el 2026-09-19

| Recurso | Realidad | Veredicto |
| --- | --- | --- |
| RAM | **30 GB** totales, **18 GB disponibles** | Sobra |
| CPU | **16 cores** Xeon Skylake | Sobra |
| Disco | 301 GB, **55 GB libres — 82% usado** | ⚠️ **Restricción real** |
| Swap | 8 GB, **4.9 GB en uso** | ⚠️ Revisar presión previa |
| Contenedores | 65 corriendo | Ya opera a escala |

Consumo actual mayor: `bitcoind` 3.7 GB, `nbxplorer` 1.58 GB, `postgres` 507 MB, `jardal-mongo` 373 MB.

**Estimación para 1 usuario (topology mínimo, sin medir aún):** ~1–1.5 GB en reposo, 2.5–4 GB bajo carga. **Las cifras citadas de 8–16 GB corresponden a 15–20 usuarios activos**, no a este caso. La fuente que las origina indica 4–8 GB para 1–10 usuarios.

**La estimación no sustituye la medición.** El despliegue debe reportar el consumo real.

### Otros riesgos

- El disco al 82% es una condición preexistente, independiente de esta decisión
- Cost Center / Project asociados a company (ver §3)
- Sin el adapter del agente, la captura diaria no existe y el sistema no se adopta
- `NOTION_MIRROR_ENABLED: true` (`openceo-core/settings.py:87`) espeja **toda** tarea a Notion — cerrar para datos financieros antes de importar nada (Horizon §9.2)

---

## 7. Consecuencias

- El campo `financial_system_selection` de Horizon §9.9 pasa de `pending_evaluation` a `erpnext`
- La implementación se desglosa en TASK-2026-00025 (ERPNext)
- ERPNext no reemplaza ningún componente de OpenCEO; añade el dominio financiero que hoy no existe
- El agente Finance hoy tiene **un solo tool** (`finance.pago_elfec_house`, `workflow_service.py:122`). La integración se registra en `WorkflowToolRegistry` (`workflow_service.py:49`)
- `execute_money_movements` permanece en `false` (Horizon §9.9) hasta que la conciliación esté probada

---

## 8. Fuentes

- [ERPNext — Accounting Dimensions](https://docs.frappe.io/erpnext/accounting-dimensions)
- [ERPNext — Inter Company Journal Entry](https://docs.frappe.io/erpnext/inter-company-journal-entry)
- [frappe/erpnext#45625 — dimension company validation](https://github.com/frappe/erpnext/issues/45625)
- [Frappe Framework — REST API](https://docs.frappe.io/framework/user/en/api/rest)
- [Odoo — Editions comparison](https://www.odoo.com/page/editions)
- [Odoo — Pricing](https://www.odoo.com/pricing)
- [Notion — Request limits](https://developers.notion.com/reference/request-limits)
- [Notion — Pagination](https://developers.notion.com/reference/pagination)
- [Notion — Page property values](https://developers.notion.com/reference/page-property-values)
- [Firefly III](https://github.com/firefly-iii/firefly-iii)
- [Actual Budget — API](https://actualbudget.org/docs/api/)
- Horizon §4.1–4.7, §9.2, §9.3, §9.9 (`jardal-horizon/README.md`)
- OpenCEO `README.md` — Data Modeling Strategy, Tool Decision Framework
