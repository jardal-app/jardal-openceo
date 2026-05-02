# Discovery & Crystallization Architecture — DRAFT

> Decisiones arquitectónicas definidas el 2026-03-21. Listo para mergear al README.

## Concepto central

OpenCEO tiene dos modos de ejecución para cualquier tarea:

1. **Determinista** — el flujo ya existe como YAML cristalizado → Python executor lo corre sin LLM
2. **Discovery** — el flujo es desconocido → `create_react_agent` lo descubre → humano aprueba → se cristaliza

---

## Dos niveles de discovery

### Nivel 1: CEO
- `create_react_agent` con tools: `[call_pm, call_finance, call_secretary, call_dev]`
- Pregunta que responde: **¿qué agentes necesito y en qué orden?**

### Nivel 2: Agente interno
- `create_react_agent` con tools primitivos del dominio (`binance_api`, `bank_transfer`, etc.)
- Se activa solo cuando el agente recibe una subtarea que no tiene cristalizada
- Pregunta que responde: **¿cómo combino mis primitivos para lograr esto?**

---

## Cómo pasa el estado entre tools (ReAct)

El LLM es el "pegamento" durante el discovery:
- Lee el output del tool anterior desde el historial de mensajes
- Extrae los datos relevantes por inferencia
- Los pasa como parámetros al siguiente tool

Los tools deben retornar outputs **flat y explícitos** para minimizar errores de extracción:
```python
# Correcto
return {"usd_amount": 95000, "status": "completed"}

# Evitar
return {"data": {"financial": {"amount": 95000}}}
```

---

## YAML cristalizado

Captura: secuencia de steps + mapeo de datos entre steps.

```yaml
workflow: crypto_reembolso
steps:
  - id: step1
    tool: finance.binance_api
    params:
      action: "sell"
      coin: "{{task.coin}}"
      amount: "{{task.amount}}"

  - id: step2
    tool: finance.bank_transfer
    params:
      amount: "{{step1.usd_received}}"   # mapeo explícito desde step anterior
      to: "{{task.client_email}}"

  - id: step3
    tool: secretary.send_email           # tool de otro agente, válido
    params:
      to: "{{task.client_email}}"
      body: "Transfer {{step2.transfer_id}} procesada"
```

### Sin LLM en ejecución cristalizada
El Python executor resuelve variables `{{stepN.field}}` con outputs reales.
Cero tokens. Cero LLM. Determinista.

---

## Tool registry

Diccionario que mapea `agent.tool_name` → función Python.

```python
TOOL_REGISTRY = {
    "finance.binance_api":    binance_api_fn,
    "finance.bank_transfer":  bank_transfer_fn,
    "secretary.send_email":   send_email_fn,
    "pm.create_ticket":       create_ticket_fn,
}
```

### Naming convention
- Formato: `{agente}.{tool_name}` (e.g., `secretary.send_email`, `finance.pago_elfec`)
- Tools reutilizados por varios agentes se registran bajo el agente que los "posee"
  (no se crea namespace `global` — el registry los resuelve igual)

---

## Flujo completo — tiers de resolución

El antiguo "LLM fallback" se estructura en tiers escalonados.
Cada tier es más costoso que el anterior, y solo se activa si el anterior no resolvió.

```
tarea llega al CEO
      ↓
Tier 0: Keywords → match directo a workflow conocido? → executor determinista (0 tokens)
      ↓ no match
Tier 1: check_registry → CEO consulta catálogo compacto → match a 1 workflow? → ejecutar (1 + N llamadas LLM)
      ↓ no match único, pero varios workflows juntos cubren
Tier 1.5: Unión de workflows → CEO conecta workflows existentes vía referencias → YAML combinado
      ↓ no match ni parcial
Tier 2: Discovery + claim → create_react_agent descubre el flujo desde primitivos
      ↓
Humano revisa y aprueba
      ↓
Se guarda como YAML
      ↓
Próxima vez: executor determinista, sin LLM
```

### Costo por tier

| Tier | Mecanismo | Tokens |
|---|---|---|
| 0 | Keywords existentes | 0 |
| 1 | check_registry compacto + detalle de N candidatos | 1 + N llamadas CEO |
| 1.5 | Unión de workflows con mapeos | discovery ligero del CEO |
| 2 | Discovery completo + claim cross-agente | N llamadas por agente |

---

## Comunicación entre agentes: Peer-to-Peer

OpenCEO adopta comunicación **peer-to-peer** entre agentes (no lead-céntrica).

### Por qué no lead-céntrico

En arquitectura lead-céntrica (como Claude Code Agent Teams), todo pasa por el CEO:
- Finance termina → devuelve al CEO → CEO decide llamar a Secretary → Secretary ejecuta
- Cada relay consume un ciclo completo de LLM = tokens + latencia innecesarios

### Peer-to-peer en OpenCEO

Los agentes se comunican directamente cuando la tarea lo requiere:

```
CEO detecta tarea compleja
      ↓
CEO inicia: Finance + Secretary en paralelo o en secuencia
      ↓
Finance termina → pasa resultado directo a Secretary (sin volver al CEO)
      ↓
Secretary genera reporte con los datos de Finance
      ↓
CEO recibe resultado final
```

### Casos donde peer-to-peer aplica

1. **Discovery en cadena**: Finance descubre que el siguiente paso es un reporte → se lo pasa directo a Secretary sin CEO como relay
2. **Desbloqueo entre agentes**: Finance está bloqueado esperando un dato → pregunta directo a Secretary si puede proveerlo, en vez de devolver al CEO e iniciar otro ciclo
3. **Error/fallback**: Agent A falla → le pregunta directo a Agent B si puede cubrir el dato faltante
4. **Sincronización en tasks largas**: agentes sincronizan estado entre ellos sin gastar ciclos CEO

### Casos donde NO aplica

- **Workflows cristalizados (YAML)**: el executor llama funciones Python directamente — no hay comunicación entre agentes, solo ejecución secuencial de tools
- **Inicio de tarea**: el CEO decide qué agentes participan y les da el objetivo — eso es top-down. Una vez activos, la comunicación entre ellos es peer-to-peer sin límite de iteraciones

### Implementación de referencia

Inspirado en OpenCode team functions:
- `team_message`: mensaje directo agente → agente
- `team_claim`: toma atómica de subtarea del pool compartido (previene double-claim)
- Mensajes en JSONL append-only (O(1) write) + session injection (sin polling)

---

## Workflows con loops (extensión del modelo de cristalización)

Los workflows lineales (`step1 → step2 → step3`) no cubren todos los casos reales.
Procesos de negocio complejos tienen **loops con condición dinámica de terminación**.

### Extensión del schema YAML

```yaml
workflow: propuesta_licitacion
steps:
  - id: step1
    tool: sales.extract_checklist
    params:
      document: "{{task.document}}"

  - id: step2
    type: loop                              # nuevo construct
    max_iterations: 15                      # Python lo controla, no el LLM
    condition:
      type: llm_eval                        # LLM solo responde True/False
      prompt: "¿Este draft cumple todos los ítems del checklist?"
      input: "{{step2.draft}} + {{step1.checklist}}"
    state: current_draft                    # acumula entre iteraciones
    steps:
      - tool: sales.draft_section
      - tool: sales.self_review
      - tool: legal.review_clauses
        when: "{{step2b.legal_pending}} > 0"  # condición Python pura, sin LLM

  - id: step3
    tool: sales.final_output
    params:
      draft: "{{step2.final_draft}}"
```

### Dos tipos de condición

| tipo | evaluado por | ejemplo |
|---|---|---|
| `python_eval` | Python puro | `"{{count}} > 0"` |
| `llm_eval` | LLM (True/False) | `"¿está completa la propuesta?"` |

### LLM como evaluador, no como orquestador

Distinción crítica:
- **LLM orquestando** → decide qué pasos ejecutar → impredecible, puede cambiar el flujo
- **LLM evaluando** → responde True/False dentro de una condición del YAML → el flujo no cambia

El LLM **no puede** agregar pasos, cambiar el orden ni saltarse steps.
El YAML es la receta. El LLM solo responde preguntas dentro de ella.

### Casos predecibles en cualquier empresa

| Proceso | Condición de terminación |
|---|---|
| Licitación / propuesta | checklist completo |
| Aprobación de crédito | todos los revisores aprobaron |
| Negociación | acuerdo alcanzado |
| Control de calidad | estándares cumplidos |
| Investigación | suficiente información recopilada |

### Diseño del schema: una vez, no constantemente

```
schema YAML (se diseña una vez)
    ├── type: sequence    → flujos lineales
    ├── type: loop        → iteraciones con condición
    │     ├── condition: python_eval
    │     └── condition: llm_eval
    └── when: "{{expr}}"  → steps condicionales dentro de cualquier tipo
```

Cada nuevo workflow es un archivo YAML que usa el schema. El schema rara vez cambia.

---

## Usos del LLM con tokens ilimitados

Con un plan de tokens ilimitado, el LLM puede usarse más ampliamente sin preocupación por costo.
El criterio ya no es "¿puedo evitar el LLM?" sino "¿agrega valor real aquí?".

### Donde sí agrega valor

1. **Evaluador de condiciones cualitativas** — `llm_eval` en loops: "¿está completo?", "¿es suficiente calidad?"
2. **Extracción de estructura desde documentos** — parsear licitaciones, contratos, PDFs y convertirlos en datos estructurados como input del workflow
3. **Detección de anomalías en inputs** — antes de ejecutar un workflow cristalizado, detectar si los inputs son atípicos y alertar al humano
4. **Normalización entre tools** — cuando el output de un tool no casa exactamente con el input del siguiente, el LLM adapta sin romper el flujo
5. **Validación del YAML antes de cristalizar** — después del discovery, el LLM verifica coherencia del workflow generado antes de que el humano lo apruebe

6. **Donde el criterio técnico lo justifique** — en cualquier punto del flujo donde el LLM aporte inteligencia genuina que el determinismo no puede cubrir, garantizando que el resultado enriquece la ejecución sin comprometer la estabilidad ni la consistencia del sistema.

### Donde NO agrega valor

- Decidir el orden de steps en un workflow ya cristalizado → el YAML lo define
- Elegir qué tool usar cuando ya está determinado → el executor lo sabe
- Routing de intención → ya lo maneja el CEO (tier 0 keywords + tiers 1-2 si escala)
- Procesos repetitivos idénticos → workflow cristalizado da más consistencia que el LLM

**Regla**: no es ilimitado de por vida, tiene límites altos pero reales. Usarlo donde aporta inteligencia genuina, no como sustituto de lógica determinista.

---

## Tool faltante durante discovery — flujo completo

### Estado del discovery se guarda

Cuando el discovery detecta que le falta un tool, no falla ni reinicia desde cero.
Guarda su estado actual en MongoDB:

```json
{
  "discovery_id": "abc123",
  "task": "conectar mikrotik y extraer métricas",
  "status": "BLOCKED_TOOL",
  "blocked_at": "step2",
  "reason": "tool network.mikrotik_api no existe en registry",
  "context": {
    "steps_completed": ["step1"],
    "current_output": "...todo lo producido hasta aquí...",
    "checklist_pending": ["exportar métricas", "generar reporte"]
  }
}
```

Cuando el tool esté disponible, el discovery continúa desde `blocked_at` con el contexto completo. No reescribe lo que ya hizo.

### Human in the loop — aprobación de tool nuevo

```
Discovery bloqueado por tool faltante
      ↓
Sistema notifica al admin: especificación del tool necesario
Admin aprueba → se crea ticket al Dev agent
      ↓
Dev implementa y registra en el registry
      ↓
Discovery continúa desde blocked_at
```

Sin aprobación humana no se crea ningún ticket ni tool.

Si el admin **rechaza** la creación del tool, el discovery y el run cierran definitivamente:

```json
{
  "status": "CANCELED_ADMIN",
  "reason": "admin rechazó creación de tool: network.mikrotik_api",
  "blocked_at": "step2",
  "final": true
}
```

El task original se marca como no resuelto con el motivo del rechazo.
El sistema no retoma el run automáticamente en el futuro — si el admin cambia de opinión, el task debe re-enviarse manualmente.

### QA del tool nuevo

No hay paso de QA separado. El QA es **re-continuar el discovery desde blocked_at**. Si completa con éxito, el tool está validado. El flow fallido original es el test case automático.

---

## Aprobación humana

La aprobación humana no es un step YAML. Las aprobaciones humanas se modelan en el ticket con `status=waiting` y una `phase` de negocio visible por dashboard, por ejemplo `qa_review`, `pm_approval` o `client_signoff`.

Los YAML cristalizados quedan como recetas técnicas deterministas. Si una operación necesita aprobación antes de ejecutar un side effect, el workflow de fases del ticket debe entrar a una fase humana antes de lanzar el YAML correspondiente.

---

## Workflow runs — almacenamiento

| Qué | Dónde | Por qué |
|---|---|---|
| Definición del workflow (receta) | Archivos YAML | git versionado, editable por Dev agent |
| Run del workflow (ejecución) | MongoDB | runtime, mutable, queryable, sobrevive reinicios |

Los runs son datos de runtime genuinamente distintos a las definiciones YAML.

---

## Consultas sobre workflow runs

Tool genérico de consulta — el LLM formula el query según la solicitud.
No hay queries hardcodeados por keyword.

```python
@tool
def query_workflow_runs(filters: dict) -> list:
    """Consulta runs de workflows en MongoDB.

    Schema disponible para filtrar:
    - status: "PENDING" | "RUNNING" | "BLOCKED_TOOL" | "COMPLETED" | "FAILED" | "CANCELED"
    - workflow: nombre del workflow (ej. "propuesta_licitacion")
    - agent: agente responsable (ej. "finance", "sales")
    - created_at: fecha de creación (ISO format)

    Ejemplo: {"status": "BLOCKED_TOOL", "agent": "sales"}
    """
    return list(db.workflow_runs.find(filters))
```

El schema dentro de la descripción del tool garantiza que el LLM formule queries consistentes
sin necesidad de keywords hardcodeados ni queries predefinidos.
Las consultas son lectura pura — sin efecto sobre la ejecución de workflows.

---

## CEO Discovery — fragmentación y matching contra el registry

### El CEO analiza dependencias primero

El LLM del CEO detecta si los fragmentos son paralelos o secuenciales analizando dependencias de datos en el lenguaje natural de la tarea:

- "actualiza contabilidad **y** notifica clientes" → paralelo (independientes)
- "actualiza contabilidad **luego** prepara reportes con esos datos" → secuencial (dependencia)

### check_registry — dos niveles de consulta

```python
@tool
def check_registry(agent: str | None = None) -> dict:
    """Devuelve workflows cristalizados disponibles.

    Sin agente: devuelve resumen compacto de todos los agentes y sus workflows.
    Con agente: devuelve detalle completo solo de ese agente.

    Ejemplo sin filtro:
    {
        "finance":   ["cierre_contable", "reporte_mensual", "pago_elfec"],
        "secretary": ["notificacion_clientes", "send_email"],
        "pm":        ["create_tickets", "sprint_planning"],
    }
    """
```

### Flujo del CEO para tarea grande

```
1. check_registry()            → resumen compacto de todos los agentes
2. CEO razona qué workflows aplican a cada fragmento
3. check_registry("finance")   → detalle completo del agente relevante
4. check_registry("secretary") → detalle completo del agente relevante
```

Primero resumen global (evita perder matches por asumir mal el agente), luego detalle solo donde aplica. El LLM hace el matching semántico contra subconjuntos pequeños.

### Los 4 casos de fragmentación

| Fragmentación | Cobertura | Resultado |
|---|---|---|
| Paralelo | todos con match | tickets independientes, cero LLM en ejecución |
| Paralelo | alguno sin match | tickets para los que tienen match + discovery solo para el huérfano |
| Secuencial | todos con match | YAML dinámico conectando workflows existentes con mapeos |
| Secuencial | alguno sin match | discovery para el step huérfano, bloquea hasta resolverse |

### Tarea grande paralela — CEO distribuye subtasks

Cuando el CEO fragmenta una tarea grande y los fragmentos son independientes (sin dependencia de datos),
el CEO asigna subtasks directamente a los agentes. No pasan por el PM.

```
tarea grande: "cierra el mes"
      ↓
CEO fragmenta → 3 subtasks paralelas independientes
      ↓
assign_task("finance", "cierre contable")     → Finance ejecuta con su workflow cristalizado
assign_task("finance", "reporte mensual")     → Finance ejecuta con su workflow cristalizado
assign_task("secretary", "notificar clientes") → Secretary ejecuta con su workflow cristalizado
      ↓
Cada agente trabaja en paralelo, sin esperar a los demás
CEO recibe resultados cuando todos terminan
```

#### Dos niveles de gestión de tareas

| Quién asigna | Para quién | Qué tipo de tarea |
|---|---|---|
| PM | Dev agent | tickets de desarrollo (features, bugs, sprints) |
| CEO | cualquier agente | subtasks operativas de la empresa |

#### Conexión con tiers

- **Tier 0/1**: CEO sabe quién lo hace → `assign_task` directo al agente
- **Tier 2**: CEO no sabe quién puede → pone en la mesa compartida → modelo claim

#### Paralelo vs secuencial — mecanismo distinto

**Paralelo** (fragmentos independientes):
- CEO usa `assign_task` por cada fragmento
- Cada agente trabaja independiente, sin esperar a nadie
- CEO recibe resultados cuando todos terminan
- No hay YAML compartido — son subtasks sueltas

**Secuencial** (fragmentos con dependencia de datos):
- CEO construye un workflow YAML con steps y mapeos
- Tier 1.5 si puede unir workflows existentes vía referencias
- Tier 2 si necesita discovery
- El executor corre en orden — step 2 espera el output de step 1
- No hay `assign_task` — es un workflow único

No se mezclan: o son subtasks paralelas o son steps secuenciales.

---

### Tarea grande = unión de workflows existentes (Tier 1.5)

El CEO construye un YAML de nivel superior que **referencia** workflows ya cristalizados.
Formato con referencias — no se aplana/copia el contenido de cada sub-workflow:

```yaml
workflow: cierre_mes_completo
steps:
  - id: step1
    ref: finance.cierre_contable          # referencia a workflow existente
    params:
      periodo: "{{task.periodo}}"
    output: datos_contables

  - id: step2
    ref: finance.reporte_mensual          # referencia a workflow existente
    params:
      input: "{{steps.datos_contables}}"
    output: reporte

  - id: step3
    ref: secretary.notificacion           # referencia a workflow existente
    params:
      reporte: "{{steps.reporte}}"
```

**¿Por qué referencias y no aplanado?**
Si Finance mejora su workflow `cierre_contable` (agrega validación, corrige mapeo), el workflow combinado se beneficia automáticamente. Con aplanado, el YAML copiado queda desactualizado y no te enterarías hasta que falle.

El YAML combinado resultante también es cristalizable — la próxima vez el executor lo corre directo sin LLM.

### Workflows cross-agente — dónde se guardan

Workflows que usan tools/workflows de múltiples agentes se guardan bajo `ceo/`:

```
workflows/
  finance/
    cierre_contable.yaml
    reporte_mensual.yaml
  secretary/
    notificacion_clientes.yaml
  ceo/                              # workflows cross-agente
    cierre_mes_completo.yaml
    crypto_reembolso.yaml
```

---

## Modelo claim — discovery de nivel 2 (peer-to-peer)

Para discovery de nivel 2 y comunicación cross-agente, el modelo claim reemplaza
la necesidad de un catálogo centralizado de primitivos.

### Cómo funciona

Cuando un agente (o el CEO) necesita capacidades que no tiene:

```
Finance no puede completar su tarea solo
      ↓
Finance pone subtask en la mesa compartida:
"genera un reporte con estos datos financieros"
      ↓
Cada agente evalúa contra SUS PROPIOS tools:
  Finance:   "¿puedo? mis tools son binance_api, bank_transfer... no"
  Secretary: "¿puedo? mis tools son send_email, generate_doc... sí" → CLAIM
      ↓
Secretary ejecuta con sus primitivos
```

### Por qué es mejor que un catálogo centralizado

- Cada agente ya conoce sus propios tools (están en su `create_react_agent`)
- No se mantiene un PRIMITIVE_CATALOG ni check_primitives separado
- Agregar un agente nuevo no requiere actualizar ningún catálogo central
- Los agentes se auto-seleccionan — nadie necesita conocer las capacidades de otro

### Claim atómico

Previene double-claim cuando dos agentes quieren la misma subtask:
- Implementado con operación atómica: temp file → fsync → rename → fsync(dir)
- El primero gana, el segundo recibe "ya tomada"

### Casos edge

| Caso | Qué pasa |
|---|---|
| Nadie hace claim | timeout → escala al humano |
| Dos agentes hacen claim | claim atómico, el primero gana |
| El agente equivocado hace claim | intenta, falla, libera la task → otro la toma |

### Dónde aplica y dónde no

- **Tier 0 y 1**: NO claim — el CEO resuelve directo con keywords o check_registry
- **Tier 1.5**: NO claim — el CEO une workflows conocidos
- **Tier 2**: SÍ claim — discovery con primitivos, coordinación peer-to-peer cross-agente

---

## create_ticket como tool global

El PM usa `create_ticket` orientado al Dev agent. El CEO necesita crear tickets para cualquier agente.
Se extiende el tool existente — no se crea uno nuevo:

```python
@tool
def create_ticket(
    title: str,
    description: str,
    assigned_to: str,          # "finance", "secretary", "dev", "sales", etc.
    parent_task: str | None,   # referencia al task padre (fragmentación)
    priority: str | None,
) -> dict:
    """Crea un ticket operativo asignable a cualquier agente."""
```

- PM siempre usa `assigned_to="dev"` (su scope no cambia)
- CEO usa `assigned_to` con cualquier agente

Si el tool no existe como global actualmente, debe crearse o adaptarse.

---

## Task padre — tracking de tareas grandes

Toda tarea grande que se fragmenta (paralela o secuencial) genera un **task padre** a nivel sistema
en MongoDB. Los subtasks, workflow_runs o discovery_runs son hijos referenciando al padre.

```
Task padre: "cierre del mes"  (MongoDB, nivel sistema)
  │
  ├── Caso paralelo:
  │   ├── ticket: "cierre contable"      → assigned_to: finance
  │   ├── ticket: "reporte mensual"      → assigned_to: finance
  │   └── ticket: "notificar clientes"   → assigned_to: secretary
  │
  ├── Caso secuencial:
  │   └── workflow_run: cierre_mes_completo → referencia al YAML
  │
  └── Caso discovery:
      └── discovery_run: estado del discovery (BLOCKED_TOOL, etc.)
```

El task padre siempre existe — da visibilidad del progreso total.
Los hijos pueden ser de distinto tipo según cómo el CEO resolvió la tarea.

### Sincronización con Notion

En MongoDB (source of truth):
```json
{"title": "cierre contable", "parent_task_id": "abc123", "assigned_to": "finance"}
```

En Notion (mirror): se usa una **Relation property** que apunta a la misma base de datos.

```
Base de datos: Tasks
  ├── title: "cierre del mes"       | parent_task: (vacío)          | ← padre
  ├── title: "cierre contable"      | parent_task: "cierre del mes" | ← hijo via Relation
  ├── title: "reporte mensual"      | parent_task: "cierre del mes" | ← hijo via Relation
  └── title: "notificar clientes"   | parent_task: "cierre del mes" | ← hijo via Relation
```

Es flat en la base de datos pero vinculado vía Relation.
Al sincronizar MongoDB → Notion, el sync crea la Relation automáticamente cuando `parent_task_id` tiene valor.
Notion permite filtrar por `parent_task` para ver todos los hijos de una tarea.

---

## Monolito modular — no microservicios

Cada agente es un paquete Python dentro del mismo proyecto. No se separa en microservicios
salvo que haya una razón técnica real (proceso 24/7, lenguaje diferente, cómputo pesado).

### Estructura de carpetas por agente

```
openceo-core/agents/
  finance/
    __init__.py
    tools/
      trading.py          # binance_api, sell_crypto, buy_crypto
      budgets.py          # calculate_budget, split_income
      payments.py         # bank_transfer, pago_elfec
      reporting.py        # generate_report, monthly_close
    workflows/
      cierre_contable.yaml
      crypto_reembolso.yaml
  hr/
    __init__.py
    tools/
      evaluation.py       # evaluate_employee, performance_review
      recruitment.py      # post_job, screen_candidate
      social.py           # publish_facebook, coordinate_marketing
    workflows/
      hiring_flow.yaml
  secretary/
    __init__.py
    tools/
      email.py            # send_email, list_accounts
      documents.py        # generate_doc, export_pdf
    workflows/
      notificacion_clientes.yaml
  ceo/
    workflows/            # workflows cross-agente
      cierre_mes_completo.yaml
```

Cada archivo de tools tiene máximo 200-300 líneas. Un agente con 200 tools
se distribuye en 10-15 archivos organizados por dominio — no hay espagueti.

### Cuándo SÍ se saca un microservicio

| Razón técnica real | Ejemplo | Decisión |
|---|---|---|
| Proceso 24/7 independiente | Trading bot que monitorea precios | Microservicio |
| Lenguaje diferente | Modelo ML en Rust | Microservicio |
| Cómputo pesado que bloquea | Procesamiento de video/imágenes | Microservicio |
| Solo más funciones Python | 200 tools de finanzas | Monolito modular |
| Muchos workflows | 50 YAMLs de HR | Monolito modular |

### TOOL_REGISTRY absorbe ambos casos

```python
TOOL_REGISTRY = {
    # Local — función Python directa
    "finance.calculate_budget": calculate_budget_fn,

    # Externo — wrapper que llama al microservicio
    "finance.binance_api": binance_api_fn,  # internamente hace HTTP al trading bot
}
```

El executor, el discovery y el claim no saben ni les importa si el tool es local o llama
a un servicio externo. La abstracción es a nivel función, no a nivel servicio.

---

## Sistema proactivo — CEO autónomo con objetivos

Todo lo anterior es **reactivo**: llega una tarea, el sistema la procesa.
Esta capa convierte a OpenCEO en un sistema **proactivo**: el CEO genera trabajo sin que nadie le pida.

### Jerarquía

```
Objetivos (nuevo, MongoDB)
      ↓ CEO genera tareas automáticamente
Tasks (ya existe)
      ↓ tiers 0-2
Workflows / Discovery (ya existe)
      ↓
Ejecución
```

Los objetivos NO son tareas. Los objetivos generan tareas.

### Colección de objetivos en MongoDB

```json
{
  "objective_id": "obj_001",
  "description": "Reducir costos operativos en 15% este trimestre",
  "priority": "high",
  "deadline": "2026-06-30",
  "progress": 0.35,
  "status": "active",
  "max_active_tickets": 3,
  "kpis": [
    {"metric": "costo_mensual", "target": 5000, "current": 5800}
  ]
}
```

### CEO loop autónomo — dos triggers

**Periódico** (ej. cada lunes, cada día a las 8am):

```
CEO revisa objetivos activos
      ↓
¿Hay alguno con deadline cerca y progreso bajo?
      ↓ sí
CEO crea tickets específicos para avanzar ese objetivo
```

**Reactivo** (cuando un workflow completa):

```
Workflow "reporte_mensual" completó
      ↓
CEO evalúa: ¿esto avanzó algún objetivo?
      ↓
Actualiza progreso del objetivo
      ↓
¿Falta algo más para cumplirlo? → crea siguiente ticket
¿Objetivo cumplido? → marca como completado
```

### Anti-bloat — evitar tickets infinitos e inútiles

Sin control, el CEO podría generar tickets vagos o redundantes que no aportan valor.
Reglas del sistema:

1. **Máximo N tickets activos por objetivo** — no crea más si ya hay tickets sin completar para ese objetivo
2. **Acción concreta obligatoria** — no "mejorar finanzas" sino "generar reporte de gastos Q1 y comparar con Q4"
3. **Revisar tickets existentes antes de crear** — si ya hay uno que cubre la necesidad, no duplica
4. **KPIs medibles** — el CEO evalúa si un ticket realmente mueve el indicador. Si no lo mueve, no se crea
5. **Scope check** — el ticket debe estar alineado con el objetivo principal. Si se desvía, se descarta

### Quién genera los tickets proactivos

El CEO, usando `create_ticket` global con `assigned_to` a cualquier agente.
El PM sigue enfocado en tickets de desarrollo. El CEO genera tickets operativos
de empresa para cumplir objetivos.

### Flujo completo

```
Objetivos activos en MongoDB
      ↓
CEO loop (periódico o reactivo)
      ↓
Revisa progreso vs KPIs vs deadline
      ↓
¿Hay tickets activos suficientes? → no crea más
¿Falta avance concreto? → crea ticket con acción específica
      ↓
Ticket entra al flujo normal (tiers 0-2)
      ↓
Workflow ejecuta
      ↓
Resultado actualiza progreso del objetivo
      ↓
Loop continúa hasta objetivo cumplido o deadline
```

---

## Scheduler interno — tareas recurrentes y recordatorios

OpenCEO tiene su propio scheduler sin depender de n8n.
El usuario crea schedules por lenguaje natural (chat o voz).

### Colección MongoDB: schedules

```json
{
  "schedule_id": "sch_001",
  "cron": "0 8 * * 4",
  "workflow": "secretary.send_telegram",
  "params": {"user": "jorge", "message": "comprar carne"},
  "project": null,
  "status": "active"
}
```

```json
{
  "schedule_id": "sch_002",
  "cron": "0 0 1 * *",
  "workflow": "finance.pago_sueldos",
  "params": {},
  "project": "jardal-openceo",
  "status": "active"
}
```

```json
{
  "schedule_id": "sch_003",
  "cron": "0 0 * * *",
  "workflow": "finance.sync_ingresos",
  "params": {"negocios": ["negocio1", "negocio2", "negocio3"]},
  "project": null,
  "status": "active"
}
```

### Un solo tipo: recurring_workflow

No hay tipos separados para recordatorios vs workflows recurrentes.
Un recordatorio es un workflow de un solo step (`secretary.send_telegram`).
Todo pasa por el mismo mecanismo.

### Creación por lenguaje natural

```
"recuérdame todos los jueves a las 8am que debo comprar carne"
→ CEO parsea: cron=0 8 * * 4, workflow=secretary.send_telegram

"programa pago de sueldos el primero de cada mes"
→ CEO parsea: cron=0 0 1 * *, workflow=finance.pago_sueldos

"sincroniza ingresos de mis negocios todas las noches a medianoche"
→ CEO parsea: cron=0 0 * * *, workflow=finance.sync_ingresos
```

Si el CEO no encuentra un workflow cristalizado para la tarea, entra a los tiers normalmente.
Si se descubre y cristaliza un workflow nuevo, el schedule apunta a ese workflow.

### Cron runner interno

Proceso dentro del servicio FastAPI — revisa cada minuto:

```python
async def cron_runner():
    while True:
        now = datetime.now()
        schedules = db.schedules.find({"status": "active"})
        for s in schedules:
            if cron_matches(s["cron"], now):
                execute_workflow(s["workflow"], s.get("params"), s.get("project"))
        await asyncio.sleep(60)
```

### Personal vs empresarial

La diferencia es solo `project`:
- `project: null` → personal (recordatorios, tareas de trabajos externos)
- `project: "jardal-openceo"` → empresarial

No hay dos sistemas — es el mismo scheduler con o sin proyecto.

---

## MCP — tres tipos

| Tipo | Scope | Quién lo consume | Ejemplo |
|---|---|---|---|
| **MCP Internal** | Dentro de OpenCEO | Los agentes internos entre sí | Agentes acceden a tools de otros agentes |
| **MCP Global** | Dentro de la infraestructura | Servicios internos de la empresa | Servicios propios que necesitan acceso a OpenCEO |
| **MCP External** | Fuera de OpenCEO | Clientes externos como ChatGPT, otros LLMs | ChatGPT llama a `secretary.send_email` vía MCP |

El MCP External expone tools del TOOL_REGISTRY (primitivos y workflows) hacia consumidores externos.
El nombre `execute_workflow` es incorrecto porque no siempre ejecuta workflows — también ejecuta primitivos.
Debe renombrarse a algo genérico como `execute_tool` o usar directamente el naming `agent.tool_name`.

---

## Estado: MERGEADO

Draft mergeado al README el 2026-03-21. Tickets de implementación creados en PENDINGS.md (TICKET-005 a TICKET-016).
