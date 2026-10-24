# ADDENDUM v1.2 — Corrección de integración de OpenCode Free y fallback multiproveedor

**Tipo:** corrección incremental prioritaria de arquitectura y contrato de implementación.  
**Fecha de revisión:** 2026-10-23.  
**Documento base:** `ESPECIFICACION_ROUTER_FREE_OPENCODE_v1.1_CORREGIDA.md`.  
**Destinatario:** la sesión de Codex CLI que **ya está implementando** el documento base.  
**Estado:** especificación corregida, **no** evidencia de que la integración esté instalada, probada o funcionando en el entorno.

> **INSTRUCCIÓN PRIORITARIA PARA CODEX — LEER ANTES DE CONTINUAR**
>
> Este documento es un **parche de requisitos**, NO una orden para reiniciar el desarrollo, borrar commits, reescribir módulos terminados ni sustituir la especificación v1.1 por completo. Primero inspecciona el árbol Git y el trabajo existente. Conserva las implementaciones válidas (catálogos, rankings, crons, controles de costes, privacidad, selección de familias, reviewers, pruebas). Corrige solo lo que presuponga que **LiteLLM o cualquier otro cliente externo puede consumir directamente el tier Free de OpenCode**. Informa qué quedó implementado, qué cambiaste y qué sigue pendiente. No marques como automático un fallback que todavía requiere intervención manual.

## 1. Hallazgo confirmado y corrección normativa

### 1.1 Lo que dice OpenCode V2 hoy

La documentación oficial de **OpenCode Console / Inference API** indica:

- La API de inferencia publica endpoints compatibles con OpenAI, Anthropic y Gemini.
- **Los modelos gratuitos de chat de Console solo están disponibles desde el cliente OpenCode.** Un cliente externo recibe **HTTP 403**, incluso si presenta una API key válida.
- Los modelos de pago y las modalidades permitidas para otros clientes tienen reglas diferentes: **NO** extrapolar la excepción Free a todos los modelos de Console.

**Fuente primaria:** <https://opencode.ai/v2/docs/console/api/inference/> (sección *Authentication*) y <https://opencode.ai/v2/docs/console/models/>.

**Conclusión obligatoria:** tener un endpoint `.../chat/completions` compatible con OpenAI **NO** implica que se autorice a LiteLLM a invocar modelos `*-free`. **No intentar falsificar el origen del cliente ni eludir las restricciones de OpenCode.**

### 1.2 Corrección de la premisa en v1.1

La v1.1 afirma erróneamente en varias secciones que los modelos gratuitos oficiales se pueden añadir, después de pruebas, como deployments normales al gateway. **Esa premisa queda anulada.**

| Afirmación anterior | Estado | Regla reemplazante |
|---|---|---|
| OpenCode Free como deployment HTTP directo de LiteLLM | **Anulada** | OpenCode Free solo por su integración nativa dentro de OpenCode |
| OpenCode Free + proveedores externos bajo un único alias HTTP de LiteLLM | **No compatible con el límite documentado** | **Dos rutas** controladas en OpenCode: modelo nativo o proveedor externo/gateway |
| Cambiar a otro proveedor equivale a cambiar un deployment en el gateway | **Incompleta** | Para atravesar la frontera `opencode` → externo se debe **cambiar el modelo de la sesión de OpenCode** |
| El cambio se hará automáticamente en cualquier TUI sin más instalación | **No demostrado** | Requiere que un componente activo controle las sesiones y errores; no basta el YAML de LiteLLM |

## 2. Arquitectura objetivo corregida

**ÚNICA autoridad de selección:** el componente `model_routing` dentro de OpenCEO, reutilizando el coordinador y capacidades existentes sin duplicarlas. **Motor de ejecución:** OpenCode para sus agentes, sesiones y herramientas MCP. **LiteLLM:** gateway **opcional** y condicionado a revisión de seguridad, **solo para proveedores externos compatibles**.

```text
                      OpenCEO / model_routing
                    perfil, catálogo, políticas,
                reservas, ranking, cooldown y retries
                                │
                 controla la selección y cambio
                 de modelo en OpenCode mediante
                  su API/SDK oficial o launcher
                                │
                                ▼
                OpenCode (sesión y contexto reales)
                  │                       │
                  ├── Ruta A: NATIVA        └── Ruta B: EXTERNA
                  │    provider: opencode       provider: openrouter,
                  │    modelos Free oficiales    nvidia, gemini, ...
                  │                             o proveedor local
                  │                             compatible con LiteLLM
                  ▼                       ▼
            OpenCode Free          LiteLLM opcional
            (primero)             (solo APIs externas)
                                          │
                               OpenRouter / NVIDIA /
                               Gemini / Groq / otros
```

**No existe un único alias HTTP de LiteLLM que comprenda legítimamente ambas rutas.** Sí puede existir **un único perfil lógico para el usuario**, por ejemplo `free-research`, cuya implementación selecciona el **modelo real de OpenCode** en cada fase. Un perfil lógico NO es necesariamente un `model_name` de LiteLLM ni un ID `provider/model` directamente seleccionable con `--model`.

## 3. Orden de selección y reglas configurables

**La política solicitada es priorizar primero los modelos Free oficiales de OpenCode**, y solo después los proveedores gratuitos independientes. Esto es una preferencia por grupo, no una afirmación de que sus modelos siempre son superiores a los externos.

1. Filtrar por **coste comprobado cero**, permisos y condiciones de datos, compatibilidad MCP/tool calling, contexto suficiente y calidad mínima.
2. En el grupo **A: OpenCode Free nativo**, ordenar candidatos elegibles según perfil `research | review | general`, calidad y pruebas locales. Usar el mejor disponible.
3. Tras fallo recuperable, intentar otro candidato nativo del grupo A, sin repetir innecesariamente cuotas agotadas.
4. Si el grupo A no tiene modelos elegibles, cambiar la sesión hacia el **grupo B: proveedores externos** (directamente configurados en OpenCode o por LiteLLM cuando el gateway esté aprobado).
5. Dentro del grupo B, continuar con fallback ordenado por calidad y disponibilidad; **LiteLLM solo administra los deployments que sí puede servir**.
6. Si el contexto ya no cabe, la privacidad no permite el nuevo proveedor o no quedan cuotas, **bloquear y registrar**, no degradar silenciosamente.
7. Si se requiere la política alternativa `best_global`, podrá habilitarse explícitamente en el futuro; el valor por defecto solicitado es `native_first`.

Ejemplo conceptual de política, **no configuración ejecutable sin adaptar al esquema real**:

```yaml
free_routing:
  policy: native_first
  require_zero_cost: true
  prohibit_paid_fallback: true
  prefer_verified_family_diversity_for_reviews: true
  profiles:
    research:
      min_context_tokens: 131072
      require_tools: true
      allow_external: true
      resume_from_checkpoint_on_partial_failure: true
    review:
      require_tools: true
      select_explicit_model_per_reviewer: true
      require_distinct_model_families: true
    general:
      allow_external: true
  provider_groups:
    - name: opencode_native_free
      transport: opencode_session_native
      priority: 1
    - name: external_free
      transport: opencode_configured_provider_or_approved_gateway
      priority: 2
```

`131072` es el objetivo inicial de contexto de investigación, no una garantía de que todos los modelos Free lo admitan: se aplica por elegibilidad real y se reduce solo por política explícita.

## 4. Cambio oficial de modelo en OpenCode: mecanismo permitido

OpenCode V2 publica el método oficial **`session.switchModel`**, que cambia el modelo usado por los **siguientes turnos** de una sesión.

**API V2 documentada:**

```text
POST /api/session/{sessionID}/model
Content-Type: application/json

{"model": {"providerID": "PROVIDER_ID_REAL", "id": "MODEL_ID_REAL"}}
```

**SDK / plugin V2 documentado:**

```ts
await ctx.session.switchModel({
  sessionID,
  model: { providerID: "PROVIDER_ID_REAL", id: "MODEL_ID_REAL" },
});
```

**Fuentes:** <https://opencode.ai/v2/docs/api> (`session.switchModel`) y <https://opencode.ai/v2/docs/build/plugins> (*Sessions*).

**Reglas de uso:**

- Obtener `providerID` y `id` **reales** del catálogo que ve esa instancia de OpenCode; no inventarlos a partir del nombre comercial.
- Cambiar modelo **cuando la sesión esté en un límite seguro de turno/solicitud**. `switchModel` afecta a turnos posteriores; NO promete conmutación dentro de una respuesta que ya emitió tokens.
- Antes de reanudar verificar compatibilidad de tools, límites de contexto, formato de mensajes y política de datos del siguiente modelo.
- Registrar: `sessionID`, `requested_profile`, modelo anterior, nuevo `providerID/modelID`, `reason`, intento y resultado.
- Probar en la versión **real** de OpenCode instalada. La documentación de V2 no prueba compatibilidad con un binario V1 o scripts CLI personalizados.
- Nunca incluir la API interna de OpenCode como endpoint público de acceso sin autenticación/control de red.

## 5. Cómo garantizar el cambio automático en cada modalidad

### 5.1 Reviewer automatizado / comando headless (prioridad primera)

El coordinador Python existente ya ejecuta `opencode run ... --model MODELO_REAL` por reviewer y controla errores y reintentos. **Mantener esa estrategia.** Aquí no hace falta que cada reviewer use un único alias HTTP.

- El selector entrega al coordinador una **lista ordenada de targets `providerID/modelID` reales**.
- Lanzar el reviewer con modelo explícito.
- Si no finaliza con informe válido por error recuperable, **reiniciar solo ese reviewer** desde cero contra el mismo snapshot de código y el siguiente modelo elegible.
- No conservar como válido un informe parcial; preservar informes terminados de otros reviewers.
- Reservar familias entre reviewers concurrentes, incluyendo `agy2` cuando cuente para diversidad.
- Una segunda ronda sigue la regla de la v1.1: verificar correcciones; no sacrificar la calidad por exigir cuatro modelos distintos a cualquier coste.
- El respaldo externo puede ser un proveedor ya configurado **directamente en OpenCode**. No bloquear el avance de reviewers por el estado HOLD de LiteLLM.

**Importante:** una ejecución CLI `--standalone` con `--model` explícito NO queda mágicamente bajo control de `session.switchModel` del servidor: se aplica la lógica explícita del **coordinador Python**, y solo se usa la API de sesión si esa ejecución está realmente administrada por un host OpenCode compatible.

### 5.2 Investigador élite / DeepResearch controlado por OpenCEO

- El usuario elige un **perfil lógico** `free-research` dentro de la integración de OpenCEO.
- OpenCEO inicia o adjunta una sesión de OpenCode bajo control efectivo del componente de routing.
- La sesión empieza con un `opencode/*-free` real del grupo A.
- OpenCEO procesa errores observables y cuotas/cooldowns. Ante un fallo elegible, cambia el modelo para el próximo turno con la API oficial y reanuda **desde el último checkpoint confirmado** si la respuesta quedó incompleta.
- OpenCEO puede cambiar después a un proveedor externo configurado en OpenCode si se agotó el grupo A.
- El historial guardado por OpenCode puede reutilizarse al cambiar el modelo cuando sea compatible, pero **no** se recupera necesariamente el razonamiento oculto ni una generación cortada a mitad.
- Si el agente usa un backend/puente distinto para ejecutar la inferencia, integrar el control en el **punto que realmente origina las llamadas al LLM**, no solo en el servicio que envía tareas.

### 5.3 OpenCode interactivo abierto manualmente en TUI

**Advertencia de implementación:** OpenCEO, LiteLLM o un cron **no interceptan automáticamente** una TUI que el usuario abre fuera de su control. **No declararlo terminado por el mero hecho de instalar el módulo `model_routing`.**

Elegir y probar UNA estrategia de integración, según el código disponible:

- **Preferida:** launcher/comando de uso habitual que inicie o adjunte OpenCode bajo el controlador de sesiones basado en el SDK/API oficial y aplique el perfil lógico al elegir modelos/reanudar; ejemplo de nombre lógico `opencode-free-research` (**no afirmar que ya existe**).
- **Alternativa, solo si es necesaria:** extensión propia mínima integrada y versionada junto con OpenCEO, construida sobre el contrato **oficial** de plugins V2; evitar plugins comunitarios pequeños. Evaluar hooks reales de error/retry antes de prometer intervención de cambio de modelo.
- Si ninguna estrategia logra recuperar de forma controlada un turno interrumpido, ofrecer fallback en el **siguiente turno** o relanzamiento supervisado y mostrar claramente esa limitación; no fingir continuidad sin cortes.

**No crear una capa compleja o un nuevo proyecto solo para simular un único `--model` de red.** El perfil estable es la abstracción de OpenCEO; los modelos efectivos pueden cambiar en OpenCode.

## 6. Contratos de datos mínimos para el router

Propuesta para el catálogo, respetando el esquema real ya implementado:

```json
{
  "profile": "research",
  "target_id": "native:opencode/glm-5.3-flash-free",
  "provider_id": "opencode",
  "model_id": "glm-5.3-flash-free",
  "model_family_id": "glm-5.3-flash",
  "route_type": "opencode_native_free",
  "allowed_client": "opencode_only",
  "zero_cost_verified": true,
  "rank_source": "snapshot_versionado",
  "status": "candidate_pending_validation"
}
```

El JSON anterior es **ilustrativo**. La identidad y disponibilidad concretas deben comprobarse en el catálogo local de OpenCode; la presencia en la documentación no prueba que la cuenta tenga cuota actualmente.

Para rutas externas, guardar algo equivalente a:

```json
{
  "target_id": "external:openrouter/fabricante/modelo:free",
  "route_type": "external_free",
  "allowed_client": "configured_opencode_or_gateway",
  "backend": "direct_or_approved_gateway",
  "provider_id": "ID_VISIBLE_EN_OPENCODE",
  "model_id": "ID_REAL_VISIBLE_EN_OPENCODE"
}
```

**No mezclar `model_family_id` con `deployment_id` ni `provider_id`:** dos proveedores pueden servir la misma familia y no contar como dos revisores independientes.

## 7. Clasificación de errores y límite de reintentos

| Error | Interpretación inicial | Acción |
|---|---|---|
| 429 / cuota por minuto | Saturación temporal | Cooldown y siguiente elegible; evitar reintentos multiplicados |
| Límite diario del proveedor | Sin capacidad actual | Marcar cuota agotada hasta fecha/ventana verificada |
| 401 / credencial inválida | Error de configuración | Desactivar deployment y alertar; no ciclar sin fin |
| **403 de OpenCode Free llamado desde LiteLLM** | **Ruta no autorizada** | **Eliminar esa ruta del gateway; usar modelo nativo OpenCode** |
| 402 / saldo insuficiente | Facturación | Bloquear ese proveedor aunque el modelo solicitado sea gratis |
| Contexto excedido | Incompatibilidad | Otro candidato que soporte los tokens/contexto necesarios o compactación segura |
| Timeout antes de respuesta | Error recuperable posible | Reintentar bajo presupuesto global, con idempotencia |
| Streaming cortado / herramienta externa ejecutada | Estado incierto | Guardar checkpoint, verificar efectos; no repetir a ciegas |

Un único componente debe controlar el **presupuesto total de intentos** para no multiplicar el gasto con reintentos en OpenCode × gateway × coordinador. El failover entre providers no elude cuotas compartidas de OpenRouter.

## 8. Qué se preserva de v1.1 (NO reimplementar)

**Queda vigente y debe conservarse**, salvo error de código real detectado:

- `model_routing` como capacidad modular **dentro de OpenCEO**, sin proyecto duplicado.
- Catálogos, rankings y snapshots versionados; **cron de benchmarking, descubrimiento y elegibilidad** sin navegador por defecto.
- Fuentes de calidad LMArena y Artificial Analysis, y pruebas propias; promoción controlada de cambios al top-3.
- Clasificación por perfil: `free-research`, `free-review`, `free-general`.
- Política de datos, prohibición de costes no aprobados, presupuesto por proveedor, controles de contexto/herramientas.
- Coordinador de reviewers existente: trabajo `musej`, 2 OpenCode + `agy2`, aislamiento, hashes de informes, quórum y marcadores.
- La segunda ronda **condicional** de verificaciones, si se implementa, sin forzar diversidad a costa de una regresión de calidad.
- Checkpoints del investigador, sin promesa de recuperar streaming parcial.
- Cualquier implementación de LiteLLM **solo para proveedores externos**, aún condicionada a controles de seguridad y validación de artefacto; no promoverla a dependencia del núcleo.
- Ausencia de plugins comunitarios experimentales como dependencias críticas.

## 9. Mapa de sustituciones del documento anterior

Leer esto como **override normativo**, no como sustitución general de todo el contenido.

| v1.1 | Corrección v1.2 |
|---|---|
| §1 resumen y arquitectura evolutiva | El gateway **no** incluye modelos Free oficiales de OpenCode mediante endpoint externo; prioridad nativa desde OpenCode |
| §4 arquitectura / diagrama | Reemplazar “gateway único para todos” por **cambio de modelo de sesión** entre OpenCode nativo y gateway externo opcional |
| §5 tabla OpenCode Console/Free | Sustituir la declaración de acceso externo sin auth por `allowed_client=opencode_only` |
| §7 fuentes / OpenCode API | Usar API/documentación como **metadata**; no invocar el tier Free vía backend externo |
| §8 perfiles | Los perfiles son **lógicos en OpenCEO**; `free-research` no tiene por qué ser un ID único servido por LiteLLM |
| §9 fallback | Agregar nivel `switchModel` / relanzamiento de trabajo en OpenCode; LiteLLM solo external-free |
| §10 investigador | No asegurar que el gateway puede gestionar nativo+externo por sí solo; requiere controlador de sesión activo |
| §11 reviewers | Fallback al siguiente **modelo real**, no a dos alias dinámicos sin diversidad garantizada |
| §12 cron | No publicar deployments nativos OpenCode Free en `gateway.generated.yaml` |
| §13.3 excepción Free | **Reemplazar íntegramente** por este addendum: limitación 403 para clientes externos |
| §15 plan de implementación | Primero consolidar native-first en OpenCode; gateway externo después, si aprobado |
| §18 riesgos/decisiones | Marcar como **restricción confirmada**, no como “compatibilidad pendiente de prueba” |
| §21 fuentes | Priorizar la documentación oficial V2 de Inference API, Models y `session.switchModel` |

## 10. Instrucción para Codex que ya comenzó la implementación

**No descartar commits ni revertir cambios correctos sin justificación.** Ejecutar en este orden:

1. Revisar `git status`, commits recientes y lista de archivos modificados sin modificar nada todavía.
2. Identificar si existen rutas HTTP creadas hacia `https://opencode.ai/inference/...` para modelos Free desde LiteLLM, Python puro u otro cliente **externo**. Si existen, deshabilitarlas del gateway y reemplazarlas por targets nativos OpenCode; no tocar integraciones de modelos **de pago** que estén explícitamente autorizadas para otros clientes, aunque por defecto el proyecto debe ser cero gasto.
3. Mantener el catálogo y crons creados; ajustar el campo de capacidad del deployment a `route_type` y `allowed_client` (o equivalente existente), con migración de esquema si ya hay datos.
4. Hacer que el selector `native_first` genere targets aptos para la sesión OpenCode y una lista externa de reserva.
5. Integrar primero con **los reviewers existentes** usando sus `opencode run --model` explícitos y sus fallbacks sobre trabajo completo; **no** cambiar el protocolo de aislamiento ni los marcadores.
6. Integrar DeepResearch en el punto real de invocación y probar `switchModel` en un host OpenCode V2 controlado, con checkpoints y continuación segura.
7. Añadir la integración interactiva **solo si el mecanismo de control de sesión queda realmente demostrado**; si no, reportar la limitación como pendiente, no construir una promesa ficticia.
8. Actualizar pruebas, ejemplos YAML, documentos y las decisiones técnicas afectadas, **sin reescribir la arquitectura de OpenCEO**.
9. Reportar un resumen: `hecho`, `pendiente`, `bloqueos`, `pruebas`, `cambios de esquema`, `riesgos`, `rollback`.

### Condición de progreso mientras el gateway está en HOLD

**NO** esperar a LiteLLM para terminar el core del selector, rankings, cuotas, pruebas y fallback entre modelos ya configurados directamente en OpenCode. La parte de gateway se mantiene desacoplada y deshabilitada hasta verificación de seguridad y compatibilidad.

## 11. Pruebas obligatorias del ajuste

1. **Ruta prohibida:** el catálogo **no** ofrece modelos OpenCode Free a través de una configuración directa de LiteLLM; si se intenta y devuelve 403, no lo reintenta como cuota.
2. **Preferencia nativa:** al abrir el perfil `free-research` bajo el controlador, el modelo real seleccionado es el mejor candidato **Free de OpenCode** que cumpla política.
3. **Fallback nativo:** ante agotamiento del primer nativo, pasa al segundo nativo real sin petición manual del usuario.
4. **Cruce nativo → externo:** sin candidatos nativos, OpenCode cambia de proveedor/modelo a un destino externo de coste cero configurado y validado, sin inventar un alias HTTP global.
5. **Compatibilidad de contexto y tools:** no se cambia a un modelo incompatible con el historial, llamadas a herramientas o la política de privacidad.
6. **Reviewer A/B:** modelos base diferentes dentro de una ronda, también después de fallo simultáneo y selección concurrente.
7. **Reviewer fallido:** se rehace íntegramente ese informe desde el mismo snapshot, sin presentar un informe parcial como válido.
8. **Investigación larga:** el historial y checkpoints persisten; ante corte de streaming no se duplica una herramienta con efectos externos.
9. **TUI abierta manualmente:** demostrar que el controlador está realmente conectado y observa fallos. Si no lo está, la prueba **no pasa** y la capacidad se etiqueta “solo sesiones gestionadas”.
10. **Sin costes:** confirmar que no se usa ningún deployment de pago ni se activa fallback a modelos no aprobados.
11. **Rollback:** deshabilitar el router y volver a elegir un modelo gratuito nativo en OpenCode, preservando proyectos, prompts y sesiones verificadas.
12. **Auditoría:** conservar modelo, familia, proveedor, motivo del cambio y resultado real en logs redactados sin secretos ni prompts sensibles.

## 12. Criterios de aceptación

**MVP seguro:** catálogo/cron + selector `native_first` + selección de dos reviewers con familias distintas + cambio automático entre ejecuciones de reviewers, **sin requerir LiteLLM**. No debe romper el investigador ni el pipeline existente.

**DeepResearch automático:** sesión OpenCode gestionada y comprobada con switch de modelo tras error seguro, manteniendo checkpoint; recuperación de fallos parciales definida y probada.

**TUI general automático:** solo “terminado” si existe launcher/cliente/extensión **operativa**, conectada a sesiones arbitrarias que se inicien por esa ruta, capaz de detectar y gestionar fallo. Un YAML de proveedores no cuenta como implementación de esta función.

**Gateway externo:** opcional; únicamente para APIs de terceros, con revisión de seguridad separada. La arquitectura no depende de su aprobación ni despliegue.

## 13. Fuentes primarias para esta corrección

1. OpenCode Console Inference API — restricción Free desde clientes externos: <https://opencode.ai/v2/docs/console/api/inference/>
2. OpenCode Console Models — catálogo Free y restricción de clientes: <https://opencode.ai/v2/docs/console/models/>
3. OpenCode V2 API — `POST /api/session/{sessionID}/model`: <https://opencode.ai/v2/docs/api>
4. OpenCode V2 Plugins — `ctx.session.switchModel`: <https://opencode.ai/v2/docs/build/plugins>
5. OpenCode V2 Models — comportamiento de `--model`, defaults y modelos disponibles: <https://opencode.ai/v2/docs/models/>
6. Especificación anterior: `ESPECIFICACION_ROUTER_FREE_OPENCODE_v1.1_CORREGIDA.md` (las secciones no citadas como reemplazadas siguen vigentes).

---

**Decisión final v1.2:** mantener **OpenCEO como cerebro del routing** y **OpenCode como cliente autorizado de sus modelos Free**. Pasar a OpenRouter/NVIDIA/Gemini/etc. mediante **selección de modelo en OpenCode**, no tratando de incorporar ilegítimamente los modelos Free de OpenCode al backend de LiteLLM. El fallback automático debe implementarse realmente para las sesiones que controla el sistema; no debe afirmarse para una TUI ajena sin una integración activa y probada.
