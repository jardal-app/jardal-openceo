# Especificación técnica — Router de modelos gratuitos para OpenCode

**Proyecto:** Free Model Gateway + Ranking + Orquestación de OpenCode  
**Versión del documento:** 1.1 — correcciones técnicas y de seguridad  
**Fecha de verificación:** 22 de octubre de 2026  
**Estado:** diseño revisado; NO desplegado ni probado en el equipo del usuario; gateway en HOLD de seguridad  
**Ámbito inicial:** **solo OpenCode**. Preparar una interfaz reutilizable para futuros clientes (Hermes, OpenClaw, otros) sin instalarlos ahora.  
**Referencia del sistema existente:** script Python de automatización adjuntado en la conversación, ~762 líneas, con fases `work`, `reviewers`, `correction`, `next`, `done`.

> **Advertencia crítica de seguridad, 22/10/2026:** el proyecto LiteLLM divulgó **ayer, 21/10/2026**, una nueva intrusión en su cadena de distribución. La versión `1.107.0` de PyPI y distintas etiquetas de contenedor estuvieron comprometidas. **NO usar `latest`, `main-latest`, `main-stable`, `1.107.0` ni instalación sin versión fijada.** El fabricante recomienda provisionalmente `1.106.2` o alguna versión explícita anterior, pero también informó de una vulnerabilidad pendiente de divulgación. Por ello **la implantación de LiteLLM queda sujeta a una aprobación de seguridad expresa** y a validar el artefacto concreto; la arquitectura debe poder cambiar a otro gateway sin modificar el coordinador. Fuente primaria: https://docs.litellm.ai/blog/security-incident-oct-21-2026. Esta advertencia tiene prioridad sobre los ejemplos de despliegue del resto del documento.

> **Addendum v1.2 (24/10/2026):** `docs/FREE_MODEL_ROUTING_SPEC_ADDENDUM_v1.2.md` corrige esta especificación donde su §9 lo indica (OpenCode Free solo por su cliente nativo; `native_first`; sin alias HTTP único). Cada sección reemplazada lleva una nota `v1.2`; lo no citado sigue vigente.

---

## 1. Resumen ejecutivo

> **v1.2:** el gateway **nunca** incluye los modelos Free oficiales de OpenCode mediante endpoint externo; prioridad nativa desde OpenCode (addendum §1–§3, mapa §9). La frase de «Arquitectura evolutiva» sobre activar OpenCode Console vía gateway queda anulada.

Se quiere un sistema único para acceder a **tantos tiers gratuitos de inferencia como sea razonablemente posible** desde OpenCode, que:

1. Descubra y clasifique periódicamente modelos gratuitos reales de OpenCode Console/Free (mediante su API de inferencia oficial, tras verificación por modelo), OpenRouter `:free`, NVIDIA NIM, Groq, Google AI Studio/Gemini, Cerebras y otros proveedores verificables.
2. Ofrezca perfiles de uso **`free-research`**, **`free-review`** y **`free-general`**, con diferentes filtros, puntuaciones y mínimos de calidad.
3. Use automáticamente el **modelo más competente elegible** según el perfil, y degrade hacia el siguiente al detectar falta de disponibilidad o cuota, **sin invocar modelos de pago**.
4. Permita que DeepResearch/investigador élite, agentes MCP y usos cotidianos sigan funcionando desde OpenCode sin seleccionar manualmente modelos cada vez.
5. Conserve y amplíe el coordinador Python ya existente para dos sesiones OpenCode reviewer en paralelo más `agy2`, con **modelos base distintos** y quórum verificable.
6. Permita, cuando corresponda, **dos rondas de revisión** separadas por corrección, escogiendo entre reutilizar modelos superiores o añadir perspectivas nuevas sin empeorar innecesariamente la calidad.
7. Use los **crons actuales** para refrescar catálogos y benchmarks, sin navegador automatizado para la operación habitual.
8. Sea auditable, reversible, seguro, sin pérdida silenciosa de informes y sin prometer recuperación imposible ante interrupciones parciales de streaming o herramientas.

**Arquitectura evolutiva recomendada:** **Fase inicial SIN gateway**: OpenCode nativo + catálogo/benchmarks + coordinador Python para reviews. **Fase posterior con gateway sustituible**: OpenCode → endpoint local OpenAI-compatible aprobado → proveedores gratuitos (incluidos los modelos Chat Completions gratuitos de OpenCode Console tras pruebas). El gateway NO es requisito para que el coordinador y el ranking funcionen. Un módulo Python conserva políticas, rankings, crons y reglas de sesiones. No instalar un router comunitario pequeño como componente crítico.

**Distinción esencial:** el gateway gestiona fallos de **solicitudes al modelo**. El coordinador resuelve las reglas de **sesiones, diversidad, rondas, persistencia, quórum y reintentos de trabajos completos**. No atribuirle al gateway características que no tiene.

---

## 2. Alcance, no objetivos y prioridades

### 2.1 Incluido

- Uso interactivo y programático desde OpenCode.
- DeepResearch largo, investigación con MCP y reanudación basada en checkpoints.
- Selección de modelos por rankings externos + mediciones internas propias.
- Listas blancas dinámicas de proveedores y modelos comprobados gratuitos.
- Fallback por fallo técnico, 429, sobrecarga y cuota conocida.
- Control de concurrencia, límites por proveedor y ventanas de enfriamiento.
- Dos reviewers OpenCode paralelos y `agy2` como tercer reviewer, conservando el protocolo actual.
- Segunda ronda de verificación cuando sea necesaria o esté configurada como obligatoria.
- Inspección de resultados: modelo efectivo, proveedor, intentos, coste estimado, razones de fallback, evidencias de revisión.
- Integración con los crons existentes, sin reemplazarlos obligatoriamente.
- Ruta de rollback inmediato al comportamiento previo.

### 2.2 Fuera del alcance inicial

- Migrar ahora a Hermes, OpenClaw o Claude Code.
- Crear cuentas adicionales para eludir cuotas.
- Usar modelos de pago como fallback silencioso.
- Ejecutar un modelo arbitrario por el simple hecho de estar marcado como “free”.
- Garantizar que una investigación interrumpida a mitad de una operación se reanude exactamente en el mismo token.
- Garantizar acceso gratuito ilimitado, 1.000 consultas por cada proveedor o que todos los proveedores acepten el mismo formato de herramientas.
- Actualizaciones automáticas del gateway o de sus dependencias críticas sin aprobación.

### 2.3 Orden de prioridades

1. Seguridad, **política de datos** y **coste cero efectivo**.
2. Corrección y preservación de resultados.
3. Calidad del modelo adecuada a la tarea.
4. Independencia real de reviewers.
5. Continuidad del trabajo ante errores recuperables.
6. Aprovechamiento de cuotas y rendimiento.
7. Facilidad de administración.

**Regla:** es preferible detener y reportar un bloqueo antes que continuar con un modelo insuficiente, uno de pago o un reviewer que rompe la independencia.

---

## 3. Estado real del coordinador entregado

El código de referencia aporta una base útil, que **no debe reescribirse** sin necesidad:

| Elemento actual | Comportamiento observado | Tratamiento previsto |
|---|---|---|
| `OPENCODE_MODELS` | Lista ordenada y fija de diez identificadores `opencode/...` | Migrar gradualmente a un catálogo JSON versionado; mantener como respaldo |
| `OPENCODE_SLOTS` | `opencode-1`, `opencode-2` | Mantener |
| `AGY2_SLOT` | `agy2` | Mantener |
| `probe_model()` | Ejecuta un prompt mínimo con modelo explícito | Mantener como sondeo opcional, con presupuesto, caché y controles de concurrencia |
| `select_opencode_models()` | Escoge los primeros dos modelos que pasan el sondeo | Sustituir por un selector multi-proveedor y multi-familia |
| `reviewer()` | Ejecuta el modelo y va bajando por `OPENCODE_MODELS` si detecta cuota | Integrar clasificación robusta de errores y reservas de modelos |
| `run()` | Ejecuta `work → reviewers → correction → next` | Añadir `verify_corrections` y corrección adicional si se activa ronda 2 |
| Quórum | Mínimo **2 de 3** reviews completadas | Conservar; reforzar criterio de independencia y calidad |
| Informes | Archivos `.md`, hashes SHA-256 y recibos `.result.json` | Mantener y ampliar la trazabilidad con proveedor/modelo efectivos |
| Aislamiento | `bubblewrap`, permisos de lectura y carpeta de informes | Mantener y auditar (ver sección de seguridad) |
| Estado | JSON de fase/ciclo/sesión, bloqueos y reanudación | Mantener compatibilidad o introducir migración explícita de versión |

**Detalles que importan para no romper el sistema:**

- El builder es **`musej`**. Genera el incremento y posteriormente evalúa/corrige los informes. No sustituirlo ni alterar sus marcadores (`CICLO COMPLETADO LISTO PARA REVIEW`, `SESION FINALIZADA CON REVIEW`, etc.).
- Los reviewers reciben un prompt **solo lectura**, pueden generar únicamente su `.md` en `reviewers_temp`, y no deben acceder a revisiones de otros reviewers de la misma ronda.
- Actualmente el tercer reviewer es `agy2` y los tres trabajan concurrentemente. La primera implementación debe conservar exactamente esto.
- `reviewer()` baja por la lista de modelos de manera **independiente en cada tarea**: dos reviewers pueden terminar en el mismo modelo. Es el principal problema de diversidad a corregir.
- `is_quota_error()` reconoce solo cadenas específicas (`provider.quota`, `FreeUsageLimitError`, HTTP 429). Debe ampliarse sin confundir contexto, autenticación, red y agotamiento diario.
- La automatización sigue indefinidamente cuando `max_cycles=0` hasta recibir el marcador de finalización; no crear bucles sin límites en las rondas de corrección.
- El script adjuntado **no muestra la implementación de los crons externos**: su formato, ubicación y horario deberán identificarse en el equipo antes de integrarlos.
- La inclusión de `--agent build` y `--auto` en reviews debe validarse frente a la política efectiva de herramientas y el sandbox: el texto del prompt no es una garantía de aislamiento.

**Referencia documental local:** archivo aportado `Texto pegado(4).txt`, 762 líneas. Es la especificación del comportamiento existente, no una prueba de que versiones o endpoints externos actualmente funcionen.

---

## 4. Arquitectura objetivo

> **v1.2:** no hay «gateway único para todos»: el cruce nativo→externo es un **cambio de modelo de la sesión** OpenCode (grupo A nativo, grupo B externo), con gateway externo opcional (addendum §2, mapa §9).

```text
                           ┌──────────────────────────┐
                           │ Cron: descubrimiento     │
                           │ + benchmarks + políticas │
                           └─────────────┬────────────┘
                                         │ JSON versionado
                           ┌─────────────▼────────────┐
                           │ Catálogo + Ranking       │
                           │ por perfil y familia     │
                           └─────────────┬────────────┘
                                         │
         ┌───────────────────────────────┴──────────────────────────────┐
         │                                                              │
┌────────▼──────────┐                                       ┌───────────▼──────────┐
│ OpenCode general  │                                       │ Coordinador Python   │
│ DeepResearch/MCP  │                                       │ 2 OpenCode + agy2    │
│ free-research     │                                       │ rondas y reservas    │
│ free-general      │                                       └───────────┬──────────┘
└────────┬──────────┘                                                   │
         └──────────────────────┬──────────────────────────────────────┘
                                │
                  ┌─────────────▼─────────────────────────┐
                  │ API gateway local, sustituible       │
                  │ alias + routing + fallback + métricas│
                  │ (LiteLLM: condicionado a seguridad)   │
                  └─────────────┬─────────────────────────┘
                                │
             ┌──────────────────┼────────────────────────────┐
             │                  │                            │
        OpenRouter :free    NVIDIA / Groq             Gemini / Cerebras
               + otros proveedores probados y aprobados

     Excepción: OpenCode Zen/Free puede permanecer como conexión nativa
     si su tier gratuito NO dispone de integración API/gateway validada.
```

### 4.1 Responsabilidades por componente

**OpenCode:** sesiones, herramientas MCP, permisos locales, historial y ejecución.  
**Gateway:** API compatible, selección entre *deployments* permitidos, timeout, retries y fallbacks dentro de la solicitud.  
**Catálogo/Ranker:** descubrir modelos, verificar gratuidad, unir benchmarks, deduplicar modelos base, priorizar.  
**Coordinador Python:** reservar modelos, ejecutar sesiones distintas, recuperar errores, gestionar rondas y persistir informes.  
**Cron:** refrescar fuentes; **NO** decidir modelos a mitad de una ronda en marcha.  
**Políticas:** prohibición de pago, mínimos de calidad/contexto y listas de proveedores aprobados.

### 4.2 Abstracción para no depender de LiteLLM

La configuración debe apuntar a un **endpoint OpenAI-compatible** y usar un inventario independiente del proveedor del gateway. Exponer a Python estas operaciones lógicas:

```text
list_eligible_models(profile, snapshot_id)
reserve_model(run_id, slot, family_id, deployment_id)
release_reservation(run_id, slot)
record_provider_error(deployment_id, error_category, retry_after)
get_next_candidate(profile, exclusions, minimum_quality)
write_audit_event(event)
```

No son métodos de LiteLLM: **son contratos internos propuestos**. La primera implementación puede ser un módulo Python, SQLite local y archivos JSON, sin microservicios.

---

## 5. Proveedores y condiciones de inclusión

> **v1.2:** la fila OpenCode Console/Free queda sustituida por `allowed_client=opencode_only`: sus modelos gratuitos solo responden al cliente OpenCode; un cliente externo recibe HTTP 403 (addendum §1, mapa §9).

| Proveedor | Inclusión objetivo | Requisitos previos / cautelas |
|---|---|---|
| **OpenCode Console/Free** | Sí; sus modelos gratis de chat están publicados en API oficial compatible con OpenAI Chat Completions | Confirmado endpoint `https://opencode.ai/inference/openai/v1/chat/completions`; los modelos gratis de chat pueden llamarse sin autenticación, según documentación. Verificar ID real, soporte de tools, contexto, términos y funcionamiento mediante el gateway antes de incluir cada deployment. La variante nativa OpenCode queda disponible como rollback. |
| **OpenRouter `:free`** | Sí, base muy útil | Solo modelos con precio de entrada **y** salida cero y disponibilidad verificada; límites globales de cuenta. |
| **NVIDIA NIM/Build** | Sí | Verificar tier, términos de prueba, tool calling y modelo vigente; no presumir cuota ilimitada ni permiso de uso de producción. |
| **Groq** | Sí | Verificar modelos, RPM/RPD/TPM y políticas actuales de la cuenta. |
| **Google Gemini API / AI Studio** | Sí | Verificar proyecto, región, modelo y cuotas gratis; no confundir Gemini API con Vertex de pago. |
| **Cerebras** | Sí | Confirmar el plan gratuito, cuotas y compatibilidad de cada modelo. |
| **Mistral y otros** | Condicional | Solo si consta un tier gratuito API válido, coste final cero y soporte probado. |

**OpenRouter:** la fuente técnica primaria para límites es https://openrouter.ai/docs/api_reference/limits, no solo su página de precios. La política del tier `:free` incluye 20 RPM y el salto de 50 a 1.000 solicitudes/día al alcanzar el umbral acumulado de compra de créditos que indique la documentación vigente (habitualmente USD 10). Es cuota **compartida por cuenta**. Leer `GET https://openrouter.ai/api/v1/key`, especialmente `free_model_daily_requests.{used,limit,remaining}`; la contabilidad local solo sirve como estimación y auditoría. La plataforma puede responder 402/403/429 por causas distintas.

**Tres conceptos separados:**

1. `free_offered`: el proveedor anuncia una modalidad gratuita.
2. `free_verified`: la cuenta y el endpoint concreto están configurados para costo efectivo cero.
3. `healthy`: el endpoint puede servir una solicitud compatible ahora.

Un deployment solo se habilita si `free_verified && healthy && feature_compatible && privacy_approved && capacity_approved && policy_approved`. Para proyectos con código sensible, privacidad y condiciones legales son **filtros duros** antes de ordenar por benchmark. No asumir que `no_train` significa `zero_retention`.

**No extrapolar un mismo modelo a todas las rutas.** El modelo base puede tener dos deployments (NVIDIA y OpenRouter) con cuotas independientes; sirven de respaldo **del mismo modelo** pero **no cuentan como dos reviewers intelectualmente independientes**.

---

## 6. Catálogo único y modelo de datos

### 6.1 Archivos previstos

```text
~/.config/free-router/
├── policy.yaml                  # reglas persistentes aprobadas
├── providers.yaml               # proveedores y tipos de cuentas, SIN claves
├── catalog.json                 # inventario enriquecido
├── rankings.json                # orden por perfil + procedencia
├── gateway.generated.yaml       # configuración generada del gateway
├── secrets.env                  # SOLO claves, permisos 0600; preferir secret manager
└── history/
    ├── rankings-YYYYMMDDTHHMMSSZ.json
    └── sources-YYYYMMDDTHHMMSSZ.json

~/.local/state/free-router/
├── availability.sqlite          # salud, cooldowns, uso, bloqueos, reservas
├── updater.log
├── gateway.log
└── reviews/                     # trazas adicionales fuera del proyecto
```

Los paths son **propuestos**, no los del script actual. El despliegue real deberá respetar `XDG_CONFIG_HOME` y `XDG_STATE_HOME` y crear sus directorios con permisos adecuados.

### 6.2 Esquema de una entrada de catálogo (ejemplo ficticio)

```json
{
  "canonical_model_id": "fabricante/familia/version",
  "family_id": "familia-base",
  "display_name": "Modelo Ejemplo",
  "benchmarks": {
    "coding": {"score": 72.4, "source": "artificial_analysis", "observed_at": "2026-10-22T08:00:00Z"},
    "agentic": {"score": 68.0, "source": "artificial_analysis", "observed_at": "2026-10-22T08:00:00Z"}
  },
  "capabilities": {
    "tools": true,
    "streaming": true,
    "context_tokens": 131072,
    "verified_in_opencode": true
  },
  "deployments": [
    {
      "deployment_id": "openrouter/fabricante/modelo:free",
      "provider": "openrouter",
      "gateway_model_id": "openrouter/fabricante/modelo:free",
      "free_verified": true,
      "input_price_usd": 0,
      "output_price_usd": 0,
      "rate_limit_policy": "account_shared",
      "data_policy": "unknown",
      "retention_policy": "unknown",
      "approved_confidentiality": ["public"],
      "limits": {"rpm": null, "rpd": null, "tpm": null, "tpd": null},
      "last_success_at": null,
      "state": "unknown"
    }
  ]
}
```

**Todos los nombres, puntuaciones y límites del ejemplo son ficticios.** Nunca generar `free_verified: true` por defecto.

Campos adicionales recomendados: `estimated_tokens_per_request`, `min_viable_tpm`, `policy_last_verified_at`, `intelligence_index_version`, `benchmark_model_vs_deployment_confidence`, `created_at`, `updated_at`, `schema_version`, `license_or_terms`, `confidence`, `benchmark_coverage`, `benchmarks_source_urls`, `endpoint_type`, `region`, `privacy_policy`, `requires_payment_method`, `expires_at`, `known_issues`, `tool_call_test_version`, `gateway_compatibility`, `staleness`.

### 6.3 Desduplicación de identidad

Conservar tres niveles diferentes:

- **`family_id`**: parentesco de pesos/arquitectura; para **diversidad intelectual**.
- **`canonical_model_id`**: versión concreta del modelo base; para medición de calidad.
- **`deployment_id`**: instancia proveedor+modelo+endpoint+cuenta; para cuotas, enfriamiento y fallback.

Si dos endpoints ofrecen el mismo modelo base, no tratarlos como dos fuentes de opinión distintas en una ronda. Si se desconoce la familia, no dar por hecho que son diferentes: usar política conservadora (`unknown-family` requiere revisión/validación).

---

## 7. Obtención de benchmarks sin navegador

### 7.1 Fuente principal: Artificial Analysis

Documentación: https://artificialanalysis.ai/data-api/docs y https://artificialanalysis.ai/data-api/docs/reference.

Endpoint gratuito **confirmado**:

```bash
curl --fail --silent --show-error \
  'https://artificialanalysis.ai/api/v2/language/models/free' \
  -H "x-api-key: ${AA_API_KEY}"
```

Permite recuperar índices públicos de **Intelligence, Coding y Agentic**, métricas medianas de rendimiento y precios publicados. Según la documentación del 22/10/2026 el plan API Free concede **100 solicitudes por ventana de 24 horas**. Los detalles individuales de evaluación y campos como ventana de contexto **no están** disponibles en la respuesta Free; completar desde el proveedor/Models.dev y mediciones locales.

**Importante:** `/language/models/free` significa **endpoint gratuito de datos**, **no** un listado compuesto únicamente por modelos con inferencia gratis. Guardar la **versión del índice de inteligencia** y no comparar numéricamente versiones mayores distintas; respetar atribución/licencia de Artificial Analysis.

### 7.2 Fuente complementaria: LMArena

Dataset oficial: https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset.

Usar datasets `agent`, `webdev` y, si están disponibles y pertinentes, `agent_tool_hallucination`, `search` y `text_style_control`; verificar dinámicamente `config`/`split` oficiales antes de descargar. Descargar por API oficial de Hugging Face o librería `datasets`/Parquet, no por scraping de páginas. LMArena usa metodologías/escala diferentes a Artificial Analysis: **normalizar antes de combinar**. Distinguir los datos de *Agent Arena* (IPS/intervalos) de los rankings de texto (ratings).

### 7.3 Catálogos de proveedores

> **v1.2:** la API/documentación de OpenCode vale como **metadatos**; el tier Free no se invoca desde un backend externo (addendum §1, mapa §9). La mención a modelos «invocables fuera del cliente nativo» queda anulada.

- OpenRouter: https://openrouter.ai/docs/api/api-reference/models/get-models y https://openrouter.ai/collections/free-models.
- NVIDIA: catálogo/API de NIM del modelo con la clave del usuario.
- Groq, Gemini, Cerebras: listados de modelos y condiciones de cuota de sus APIs oficiales.
- OpenCode Console: https://opencode.ai/v2/docs/console/api/inference/ y https://opencode.ai/v2/docs/console/models/; API de inferencia publicada y modelos de chat gratuitos invocables fuera del cliente nativo. No todos los modelos comparten familia de API: usar la tabla de endpoints y verificar por ID; confirmar expresamente la traducción `tool_calls` y los límites reales antes de declararlo compatible con LiteLLM.

No usar la **popularidad en OpenRouter** como benchmark de calidad: su colección gratuita ordena por volumen de uso, no por exactitud técnica.

### 7.4 Detección de capacidades

El cron debe combinar **metadatos oficiales**, compatibilidad de formato y, cuando sea razonable, pruebas pequeñas y presupuestadas:

- Chat no-streaming.
- Chat streaming y detección de corte parcial.
- `tool_calls` con argumentos estructurados.
- Segundo turno con resultados de herramientas, respetando `tool_call_id`.
- Contexto mínimo y salida máxima.
- Compatibilidad con el formato que OpenCode realmente usa (Chat Completions frente a Responses).
- Degradación cuando falta soporte de reasoning/tool calls o se excede contexto.

Los probes **también pueden consumir la cuota de inferencia**. No probar todos los modelos cada seis horas indiscriminadamente; usar TTL y probing selectivo.

---

## 8. Rankings por perfil

> **v1.2:** los perfiles son **lógicos en OpenCEO**; `free-research` no tiene por qué ser un ID único servido por LiteLLM (addendum §2, mapa §9).

### 8.1 Nombres expuestos al usuario

| Alias | Selección deseada | Consumo |
|---|---|---|
| `free-research` | razonamiento, herramientas, fidelidad de fuentes, contexto largo | DeepResearch / investigador élite |
| `free-review` | calidad de código, hallazgos reproducibles, pocas alucinaciones | review interactivo individual |
| `free-general` | combinación de calidad, rapidez y disponibilidad | uso general |
| `free-review-r1-a`, `free-review-r1-b` | asignaciones **reservadas** a modelos concretos y distintos | primera ronda del coordinador |
| `free-review-r2-a`, `free-review-r2-b` | asignaciones **reservadas** por política | segunda ronda |

**Importante:** `free-review-r1-a` etc. son **identificadores lógicos de ejecución**. No es obligatorio publicarlos como alias permanentes en LiteLLM: es más seguro que el coordinador invoque un **ID concreto** del catálogo y registre qué deployment sirvió la solicitud.

### 8.2 Reglas mínimas

**Research:** tool calling y streaming probados, **TPM/RPM/TPD suficientes para el historial real**, política de datos compatible y contexto objetivo ≥128K donde exista suficiente oferta; permitir umbral menor explícito, por ejemplo 64K, cuando no queden opciones y el checkpoint haya reducido el historial. Fuentes, citas y trazabilidad indispensables.  
**Review:** tool calling/lectura fiables, contexto suficiente para diff + archivos, TPM viable para prompts grandes, alta puntuación de coding, capacidad de generar informe con líneas verificables y diversidad verificada de familia.  
**General:** tolerancias menores de contexto cuando el usuario no requiera MCP ni investigación larga.

### 8.3 Puntuaciones: hipótesis inicial, no ley científica

Propuesta de **pesos iniciales** (modificables tras recopilar resultados reales):

| Dimensión normalizada | Research | Review | General |
|---|---:|---:|---:|
| Coding | 20% | 60% | 25% |
| Agentic | 45% | 30% | 25% |
| Intelligence | 35% | 10% | 50% |

**Método de normalización:** percentiles o transformación robusta **por fuente y fecha**, no suma directa de índices con escalas distintas. Registrar cobertura de benchmarks. Cuando falte una métrica, aplicar política explícita de **confianza degradada** y nunca puntuarla por encima de una métrica ausente sin pruebas.

Ejemplo conceptual:

```python
quality = sum(profile_weights[k] * percentile_scores[k] for k in available_dimensions)
quality *= coverage_confidence
quality += bounded_local_eval_adjustment
# Aplicar restricciones duras ANTES de ordenar.
# Medir latencia/cuotas como desempates; no confundir rapidez con calidad.
```

**Promoción de ranking:** el cron puede proponer un orden nuevo, pero cambios al **top-3** deben pasar evaluaciones locales reproducibles o aprobación explícita antes de publicarse. El benchmark mide un **modelo canónico**, no asegura que un deployment gratuito concreto conserve contexto/calidad. No imponer precisión falsa a percentiles con pocos candidatos elegibles.

A futuro, un conjunto propio de revisión de código (fallos reales, severidad, falsos positivos, evidencia archivo/línea, aceptación del builder) debe influir más que benchmarks generales. No dejar que un LLM califique sin supervisión el trabajo de otro LLM como única evidencia de calidad.

### 8.4 Degradación mínima permitida

Política sugerida:

- Elegir siempre el mejor **modelo elegible** del perfil.
- Si falla, escoger el siguiente elegible dentro del `quality_floor` y `max_quality_drop` del perfil.
- No permitir salto automático a un modelo sin herramientas si la sesión usa MCP.
- Si ningún candidato cumple, `BLOCKED_NO_ELIGIBLE_MODEL` y explicación verificable.
- Recalcular ranking periódicamente, **congelar snapshot durante cada ronda** y evitar cambios arbitrarios por turno.

---

## 9. Fallback y escalado progresivo

> **v1.2:** se añade el nivel `switchModel`/relanzamiento de trabajo en OpenCode para cruzar la frontera nativo→externo; LiteLLM solo cubre external-free, con un único presupuesto global de intentos (addendum §3–§4, §7, mapa §9).

### 9.1 Dos niveles distintos y autoridad única por ruta

**Modo inicial SIN gateway:** OpenCode accede a proveedores de forma nativa, y el coordinador decide reintentos y cambio de modelo de un reviewer **completo**. No delegar la selección de dos reviewers a un alias opaco.

**Modo interactivo con gateway aprobado:** el gateway gestiona fallos pre-respuesta de **una llamada**; limitar los intentos para que no se multipliquen con los reintentos de OpenCode. No habilitar a la vez múltiples políticas autónomas de retry sin presupuesto global.

**Nivel A: gateway, dentro de una solicitud de inferencia.** Prioridades configuradas y fallbacks entre deployments elegibles (ejemplo: OpenRouter `:free` → NVIDIA NIM → Groq). LiteLLM documenta `order` para niveles de prioridad; valor menor = mayor prioridad. Al fallar un nivel, intenta el siguiente según sus reglas. Fuente: https://docs.litellm.ai/docs/routing.

**Nivel B: coordinador, entre ejecuciones completas de agentes.** Si una revisión termina sin informe válido, reiniciar **solo ese reviewer** en otra sesión/modelo, manteniendo reservas y sin borrar informes verificados de los demás. En DeepResearch, reanudar desde checkpoint verificable, no prometer reanudación token a token.

### 9.2 Clasificación de errores

| Señal | Acción por defecto |
|---|---|
| HTTP 429 con `Retry-After` breve | cooldown del **deployment**; probar otro si existe; respetar `Retry-After` |
| Cuota diaria de proveedor confirmada | marcar provider/account/deployment inhabilitado hasta ventana de restablecimiento conocida |
| HTTP 502/503/504, timeout antes de respuesta | retries acotados con jitter y siguiente deployment |
| HTTP 401 | deshabilitar credencial, alerta; no insistir con la misma clave |
| HTTP 403 | inspeccionar permiso, región o términos; **no asumir** cuota |
| HTTP 402 / facturación / modelo dejó de ser gratis | bloquear de inmediato esa ruta; **prohibido pagar automáticamente** |
| HTTP 400: herramientas no compatibles | excluir modelo de perfiles con tools; registrar fallo de compatibilidad |
| Contexto excedido | pasar a otro modelo con más contexto o compactar mediante checkpoint aprobado |
| Streaming iniciado y cortado | **NO** repetir automáticamente la misma llamada como si nada; OpenCode debe decidir reconstrucción desde el último estado consistente |
| Herramienta MCP con efecto no idempotente | no repetir ciegamente la operación; inspeccionar si se ejecutó antes |
| Respuesta vacía, JSON inconsistente, `tool_call_id` corrupto | modelo no apto para este flujo; fallar seguro y registrar |

**Parámetros iniciales de seguridad, sujetos a carga real:** para reviews automatizadas preferir **un deployment explícito por alias y `num_retries: 0` en el gateway** (tras verificar campo en la versión seleccionada); el coordinador controla reintentos de trabajo entero. En investigación interactiva permitir fallback pre-respuesta con presupuesto total conocido. Nunca asumir que OpenCode, gateway y coordinador comparten un contador de intentos. Para ese modo, un máximo lógico orientativo es `max_total_hops=5`, backoff exponencial con jitter, cooldown progresivo y un límite global de peticiones en curso por cuenta. No multiplicar `3 intentos en Python × 5 fallbacks × 3 reintentos HTTP` sin un presupuesto coordinado: podría agotar rápidamente el tier gratuito.

### 9.3 Estado compartido y reservas

Para ejecución local en un único host, SQLite con transacciones es suficiente para:

- Reservas `run_id + round + slot → canonical_model_id/family_id/deployment_id`.
- Cooldowns por `deployment_id` y razón.
- Contador aproximado de peticiones/RPM por proveedor; estado de cuota diaria si se conoce.
- Historial de errores y latencias.
- Locks para evitar **dos reviewers en la misma familia**.

Si dos procesos compiten por el último modelo de una familia, solo uno debe conseguir la reserva. Las reservas se liberan al completar/cancelar, con expiración y recuperación tras crash.

### 9.4 Auditoría del modelo real

**El alias solicitado no basta.** Guardar, cuando el gateway lo revele, deployment efectivo (`model_id`/cabeceras/traza) y correlación por solicitud. Si no se puede determinar exactamente qué modelo respondió, **no usar ese alias opaco para dos reviews que exigen diversidad verificable**.

---

## 10. DeepResearch / investigador élite

> **v1.2:** el gateway no gestiona nativo+externo por sí solo; el investigador usa una sesión OpenCode bajo control del router con `switchModel` y checkpoints (addendum §4–§5, mapa §9).

### 10.1 Inicio

El usuario elige `free-research` dentro de OpenCode. El gateway utiliza el primer deployment elegible de la lista ordenada, con fallback seguro ante errores recuperables. **Esto no requiere un coordinador de reviewers para cada consulta.**

### 10.2 Sesión larga y MCP

- Mantener el mismo modelo durante la sesión cuando sea viable; evitar rotación arbitraria por mensaje.
- Si el gateway soporta afinidad por sesión, configurar `session_affinity`; para LiteLLM la documentación requiere enviar un identificador consistente mediante `x-litellm-session-id` o `metadata.session_id`. **Probar que la versión concreta de OpenCode transmite ese valor antes de considerar la afinidad operativa**. Fuente: https://docs.litellm.ai/docs/routing.
- Las herramientas MCP se ejecutan mediante OpenCode y sus servidores; el router de LLM no redirige automáticamente las propias llamadas MCP.
- Guardar evidencias y checkpoints del proyecto: objetivo, plan, consultas/fuentes, resultados validados, pendientes, resumen de decisiones y referencias a artefactos.
- Mantener identificadores de tool calls y orden de mensajes coherentes al reintentar.
- Los fallbacks entre modelos con representaciones incompatibles de razonamiento (`reasoning`, contenido cifrado, Responses/Chat Completions) deben pasar pruebas de replay; no garantizar compatibilidad universal.
- No introducir fuentes ficticias ni declarar finalizada una investigación incompleta.

### 10.3 Recuperación

1. Si el fallo ocurre **antes de una respuesta**: el gateway puede probar otro deployment.
2. Si el fallo ocurre **después de texto parcial**: se interrumpe el turno, se conserva lo válido y OpenCode reconstruye a partir del historial/checkpoint.
3. Si el fallo ocurre tras ejecutar una herramienta: comprobar si la acción fue aplicada; no duplicar operaciones.
4. Si no hay modelo elegible: guardar estado y reportar bloqueo reanudable.

### 10.4 Condición de aceptación para Research

En una prueba controlada de ≥50 turnos con MCP (y fallos 429/503 inyectados), la investigación debe conservar fuentes/checkpoints y continuar cuando exista modelo compatible. Ninguna respuesta parcial ni comando MCP debe duplicarse silenciosamente.

---

## 11. Reviewers: primera y segunda ronda

> **v1.2:** el fallback es al siguiente **modelo real** de la lista `native_first` (grupo A, luego reserva externa B), con modelo explícito por revisor y reinicio íntegro; no a dos alias dinámicos sin diversidad garantizada (addendum §5.1, §10.5, mapa §9).

### 11.1 Política general

**Primera ronda:** reviewer A y B son **familias verificadas diferentes**, de la mayor calidad disponible; `agy2` permanece como tercer reviewer si está disponible. Cada uno elabora informe independiente, sin leer los informes de la ronda actual. Se aplica el quórum operativo 2/3 **solo con dos familias verificadas distintas**, incluyendo la identidad real de `agy2` cuando participa.

**Después:** `musej` lee los informes verificados, decide cuáles hallazgos son reales, corrige y produce commit local sin push, preservando la lógica actual.

**Segunda ronda (configurable):** inspecciona **el estado corregido**, no repite sin más la primera revisión del mismo diff. Objetivos: confirmar correcciones, detectar regresiones nuevas y documentar elementos no verificados. Nunca confundir “dos rondas” con “dos revisores” (una ronda puede contener 2 OpenCode + `agy2`).

### 11.2 Selección de familias

Para quórum estricto, cada reviewer completado debe usar un **`family_id` verificable** y el par que satisface quórum debe ser distinto. Incluir también al reviewer externo **`agy2`** si participa: su familia/deployment reales se declaran en la política y se validan mediante configuración/evidencia, nunca por el nombre `agy2` solamente. Si su familia no se conoce, marcarla `unknown` y **no contarla como segunda opinión independiente** sin una excepción explícita aprobada.

La reserva debe ser **atómica y por toda la ronda**, cubriendo los tres slots. Proteger la asignación frente a carreras y fallbacks simultáneos. Dos proveedores que sirven los mismos pesos/modelo canónico no cumplen diversidad aunque cambie la API. Quórum 2/3 requiere **dos informes verificados y dos familias verificadas distintas**. Si no hay diversidad suficiente, bloquear e informar, no declararla por inferencia.

La familia de `agy2` puede cambiar por configuración/proveedor; renovar la verificación cuando se modifique. Mantener quórum operativo existente solo cuando su independencia pueda comprobarse.

### 11.3 Segunda ronda: política que se recomienda

**`reuse_best_plus_novel`** (predeterminada para dos rondas):

- Reviewer R2-A: repetir uno de los mejores modelos, especialmente útil para comprobar correcciones específicas.
- Reviewer R2-B: escoger un modelo de otra familia bien puntuado, preferiblemente que no haya trabajado en R1.
- Si el candidato novedoso está muy por debajo del mínimo de calidad, reutilizar el segundo mejor modelo de R1 antes de sacrificar rigor.
- No exigir siempre cuatro modelos distintos. La diversidad es un medio, no un objetivo en sí mismo.
- Conservar `agy2` según disponibilidad, cuota y política del usuario; no asumir que es siempre un tier gratuito del gateway.

**`four_distinct`**, modo opcional: exige cuatro familias diferentes en dos rondas; si no hay cuatro aptas, registrar bloqueo o permitir degradación **solo si el usuario la habilitó**.

**`reuse_top_two`**, modo alternativo: repetir dos modelos superiores con cambios en el prompt centrados en correcciones y regresiones.

### 11.4 Cuándo activar segunda ronda

`--review-rounds 1|2|auto` determina la política; `auto` debe ser **determinista y auditable**, no una intuición variable del LLM.

- `auto`: segunda ronda obligatoria cuando el diff toca globs versionados de `security/**`, `auth/**`, `payments/**`, `migrations/**`, `infra/**` o equivalentes reales del proyecto; también cuando R1 reportó hallazgos críticos/altos **confirmados**, cambios significativos de corrección o verificación insuficiente según criterios codificados.
- Revisiones triviales con R1 válida y sin cambios de riesgo: una ronda basta.
- Presupuesto: comprobar cuotas y tokens restantes antes de abrir R2. Si no cabe, conservar estado, registrar `BLOCKED_CAPACITY` o aplicar omisión **expresamente permitida por política**, sin fingir verificación completa.
- `2` fuerza segunda ronda solo cuando existan recursos y modelos elegibles; ante falta de cuota no usar pagos.
- Mantener máximo fijo de correcciones, con revisión/verificación final si después de R2 se modifica código.

### 11.5 Nuevas fases de estado

```text
work
  → review_round_1
  → correction_1
  → [decide_round_2]
      → review_round_2
      → [correction_2_if_needed]
      → [verify_corrections_if_changed]
  → next
  → ... → done
```

Puntos críticos:

- Solo cerrar tras comprobación del estado final; **si `correction_2` modifica código, debe existir una comprobación posterior** de regresión, aunque sea acotada, antes del cierre.
- `max_correction_loops` fijo (p. ej., 2); bloquear cuando se agote y conservar evidencias.
- No ejecutar una nueva tarea `SIGUE` del builder mientras se revisa/corrige el incremento vigente.
- Eliminar `.md` temporales **solo después** de persistir recibos y, si corresponde, evidencia histórica fuera del proyecto. Preservar el comportamiento de limpieza al finalizar.
- Los prompts de R2 no deben permitir modificar archivos ni consultar informes independientes de la misma ronda antes de terminar.

### 11.6 Fallback de un reviewer

Un reviewer debe ser **una unidad de trabajo reproducible**: prompt, commit/diff objetivo, snapshot de ranking y política de herramientas. Si su llamada falla y no hay informe íntegro:

1. Registrar fallo y marcar el deployment en cooldown.
2. Liberar su reserva.
3. Reservar siguiente familia/endpoint permitido que no esté usado por otro reviewer de esa ronda.
4. **Reiniciar solo ese reviewer** en una sesión limpia sobre el mismo código y prompt, sin mezclar un informe parcial anterior.
5. Publicar `.md` y recibo SHA-256 únicamente si se verificó finalización y contenido.
6. Si no hay sustituto válido, conservar informes de los otros y aplicar el quórum y política de bloqueo existentes.

**El gateway no puede garantizar por sí solo esos pasos.** Para reviewers, preferir IDs de modelo/deployment explícitos sobre el alias dinámico opaco `free-review`.

### 11.7 Modelo de recibo ampliado (propuesto)

```json
{
  "run_id": "...",
  "cycle": 8,
  "round": 2,
  "slot": "opencode-1",
  "requested_model": "gateway/model-x",
  "effective_model": "fabricante/model-x",
  "family_id": "fabricante/familia-x",
  "provider": "nvidia_nim",
  "deployment_id": "nvidia/model-x",
  "rankings_snapshot": "sha256:...",
  "reviewed_commit": "<SHA-del-commit>",
  "report": "reviewers_temp/cycle-...-r2-opencode-1.md",
  "sha256": "<SHA256-del-informe>",
  "result": "validated",
  "fallback_count": 1,
  "started_at": "2026-10-22T12:00:00Z",
  "finished_at": "2026-10-22T12:11:00Z"
}
```

Nombres y datos ficticios. No incluir tokens, claves ni código sensible innecesario en logs.

---

## 12. Crons: descubrimiento, rankings y publicación

> **v1.2:** `gateway.generated.yaml` nunca publica deployments nativos OpenCode Free (addendum §10.8, §12, mapa §9); el pipeline y los snapshots congelados no cambian.

### 12.1 Dos tareas programadas

**Cron A — benchmarks (cada 24 h):** pedir Artificial Analysis + snapshot LMArena y guardar datos, fecha y procedencia.  
**Cron B — catálogo de modelos gratuitos (cada 6 h):** revisar OpenRouter, NVIDIA, Groq, Gemini, Cerebras, OpenCode y posibles nuevos proveedores, con TTL y sondas selectivas.  
**Tiempo de ejecución:** cooldowns y fallbacks se actualizan en el momento de los errores; **no esperar al próximo cron**.

Ejemplo ilustrativo de entradas de cron del usuario, adaptar rutas y locks:

```cron
# Zona horaria del host: revisar explícitamente con timedatectl; usar flock.
15 2 * * * flock -n /tmp/free-rankings.lock /usr/bin/python3 /opt/free-router/update_rankings.py >> /var/log/free-rankings.log 2>&1
20 */6 * * * flock -n /tmp/free-catalog.lock /usr/bin/python3 /opt/free-router/update_catalog.py >> /var/log/free-catalog.log 2>&1
```

Preferible reutilizar los **crons ya existentes** cuando tengan esa responsabilidad. No instalar tareas duplicadas. Si se elige systemd timers, conservar una sola fuente de planificación.

### 12.2 Pipeline de publicación

```text
Fetch → validate → normalize → canonicalize → score → policy filter
   → consistency checks → write temp → fsync → atomic rename
   → snapshot versionado → reload controlado del gateway
```

- Validar schema, URLs, fechas y versiones.
- No borrar todos los modelos ante caída de una fuente: conservar último snapshot bueno, con `stale` y fecha.
- Fallar cerrado frente a **precio desconocido**, cambio de precio o desaparece el `:free`.
- Nunca crear un alias al gateway que pueda enrutar a un modelo no aprobado.
- Si LiteLLM usa YAML estático, regenerarlo y aplicar una **recarga/reinicio controlado, con health check y rollback**. No asumir que modificar el archivo cambia dinámicamente el proceso.
- Las sesiones de review en curso usan el **snapshot congelado**; futuras rondas usan el nuevo.

### 12.3 Mapa de modelos faltantes

- Publicar el nuevo ranking por archivo temporal + `fsync` + `rename` atómico. **El cron no reiniciará un gateway con sesiones activas**. Cambios a `gateway.generated.yaml` se validan y solo se aplican en una ventana sin sesiones; si se habilita configuración dinámica con DB, verificar su contrato y seguridad primero. No reiniciar solo por variaciones cosméticas: comparar hash del conjunto elegible.
- Usar alias explícitos fabricante→modelo para unir `LMArena ↔ Artificial Analysis ↔ proveedor`; no resolver por coincidencia difusa de nombres sin reglas.
- Mantener una lista manual de excepciones/aliases versionada.
- Un modelo nuevo con benchmark insuficiente entra en `experimental` y recibe probes, no el puesto #1 automáticamente.

---

## 13. Configuración ejemplar del gateway (no desplegar sin aprobar seguridad)

### 13.1 LiteLLM: contrato lógico de ejemplo

El ejemplo de abajo **ilustra la gramática de `model_name`, `litellm_params.model`, `order` y claves por entorno**. **NO es un fichero listo para ejecutar**: reemplazar IDs falsos por modelos **actuales, verificados gratuitos** y validar los campos contra la versión específica. No inferir que NVIDIA o OpenRouter ofrecen esos nombres ficticios. **v1.2:** solo deployments externos; ningún deployment `opencode` nativo entra en esta lista (addendum §12).

```yaml
# gateway.generated.yaml — EJEMPLO, NO desplegable
model_list:
  # Perfil research: varios deployments bajo un mismo alias virtual
  - model_name: free-research
    litellm_params:
      model: openrouter/PROVEEDOR/MODELO_FREE:free
      api_key: os.environ/OPENROUTER_API_KEY
      order: 1
  - model_name: free-research
    litellm_params:
      model: nvidia_nim/PROVEEDOR/MODELO_NIM
      api_key: os.environ/NVIDIA_NIM_API_KEY
      order: 2
  # Los deployments de Groq/Cerebras entran a Research SOLO si las cuotas
  # medidas y las políticas de datos/contexto pasan filtros. No incluir
  # un tercero por obligación: falla cerrado si no hay otro elegible.

  # Perfil general: ranking independiente, aunque coincida un deployment.
  - model_name: free-general
    litellm_params:
      model: groq/MODELO_GENERAL_GRATUITO_VERIFICADO
      api_key: os.environ/GROQ_API_KEY
      order: 1
  - model_name: free-general
    litellm_params:
      model: openrouter/PROVEEDOR/MODELO_GENERAL:free
      api_key: os.environ/OPENROUTER_API_KEY
      order: 2

  # Review interactiva individual: alias dinámico; NO usar a ciegas para
  # dos reviewers simultáneos con requisito estricto de modelos distintos.
  - model_name: free-review
    litellm_params:
      model: openrouter/PROVEEDOR/MODELO_REVIEW:free
      api_key: os.environ/OPENROUTER_API_KEY
      order: 1
  - model_name: free-review
    litellm_params:
      model: nvidia_nim/PROVEEDOR/MODELO_REVIEW_NIM
      api_key: os.environ/NVIDIA_NIM_API_KEY
      order: 2

  # Review: endpoints EXACTOS, un alias por modelo/deployment.
  # Política operacional: NO habilitar reintentos autónomos adicionales para
  # estos alias mientras el coordinador sea autoridad del trabajo completo.
  # El coordinador elige uno y controla la reserva y fallback del reviewer.
  - model_name: review-candidate-001
    litellm_params:
      model: openrouter/OTRO_PROVEEDOR/OTRO_MODELO:free
      api_key: os.environ/OPENROUTER_API_KEY

router_settings:
  allowed_fails: 3
  cooldown_time: 30
  # Validar compatibilidad antes de habilitar:
  # optional_pre_call_checks: [session_affinity]

litellm_settings:
  drop_params: false

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

**No colocar todos los deployments con igual orden** si se pretende escalado por calidad: el enrutador puede balancear o seleccionar aleatoriamente entre un mismo nivel. `order=1,2,3…` debe derivarse del ranking y conservarse en cada actualización. Fuente: https://docs.litellm.ai/docs/routing y https://docs.litellm.ai/docs/proxy/configs.

**No mezclar automáticamente dos modelos distintos detrás de `review-candidate-001`**: impediría saber si una revisión usó un modelo reservado. Si un mismo modelo se sirve mediante dos proveedores, el coordinador puede permitir fallback entre *deployments del mismo modelo base*, pero debe registrar cuál respondió.

### 13.2 Conectar OpenCode al gateway

Las opciones de configuración de OpenCode **difieren entre versiones**. Comprobar `opencode --version`, `opencode run --help` y el esquema de configuración instalado. Documentación:

- https://opencode.ai/docs/providers/ (configuración clásica: `provider`, `npm`, `options.baseURL`).
- https://opencode.ai/v2/docs/providers (configuración V2: `providers`, `package`, `settings.baseURL`).

**Ejemplo V2, solo si el ejecutable instalado coincide con esa documentación:**

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "free-gateway": {
      "name": "Free Gateway local",
      "package": "@opencode/ai/providers/openai-compatible",
      "env": ["FREE_GATEWAY_KEY"],
      "settings": { "baseURL": "http://127.0.0.1:4000/v1" },
      "models": {
        "free-research": { "name": "Free Research" },
        "free-general": { "name": "Free General" },
        "free-review": { "name": "Free Review" }
      }
    }
  }
}
```

**Ejemplo sintáctico clásico, no mezclar con V2:**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "free-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Free Gateway local",
      "options": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "{env:FREE_GATEWAY_KEY}"
      },
      "models": {
        "free-research": { "name": "Free Research" },
        "free-general": { "name": "Free General" },
        "free-review": { "name": "Free Review" }
      }
    }
  }
}
```

**Requisito:** verificar que OpenCode efectivamente conecta y que `/models` presenta los modelos virtuales, que `tools` funcionan y que la API usa el endpoint correcto. Ejemplos de uso propuestos:

```bash
opencode run --model free-gateway/free-research "Investiga el problema..."
opencode run --model free-gateway/free-general "Explica este cambio..."
# Para reviews automáticos: no asignar siempre free-review opaco a los dos slots.
```

La autenticación del gateway debe validarse contra `/v1/models` y `/v1/chat/completions` usando una clave local con permisos mínimos. **No incluir claves en los archivos de proyecto.**

### 13.3 La excepción OpenCode Free

> **v1.2 — sección reemplazada íntegramente por el addendum:** los modelos Free solo están disponibles desde el cliente OpenCode; un cliente externo (LiteLLM, Python u otro) recibe HTTP 403 aunque presente una clave válida. Ninguna inferencia a `opencode.ai/inference` fuera de OpenCode; su endpoint de modelos queda solo como metadatos (addendum §1, mapa §9). Lo que sigue se conserva solo como registro histórico.

**Corrección v1.1: OpenCode Console ya no es una excepción por ausencia de API.** La documentación oficial publica:

- API base: `https://opencode.ai/inference/openai/v1`.
- Chat Completions: `POST https://opencode.ai/inference/openai/v1/chat/completions`.
- Descubrimiento: `GET https://opencode.ai/inference/v1/models`.
- Los modelos de chat **gratuitos** pueden llamarse sin cabecera de autenticación. No extrapolarlo a modelos de pago o a otras familias de endpoint.
- Fuentes: https://opencode.ai/v2/docs/console/api/inference/ y https://opencode.ai/v2/docs/console/models/.

**Integración:** introducir cada modelo gratuito de chat como deployment OpenAI-compatible mediante un adaptador/configuración del gateway aprobado **solo después de probar formato de herramientas, streaming, contexto real, límites y coste cero**. Un endpoint compatible no demuestra por sí solo que LiteLLM haya superado la prueba end-to-end.

Mientras el gateway esté en HOLD, mantener las rutas **nativas** OpenCode y hacer que el coordinador pueda cambiar de modelo entre ejecuciones completas. Después, activar vía gateway la ruta OpenCode Console junto a NVIDIA/OpenRouter y los demás elegibles. No instalar `opencode-free-router`; se trata exclusivamente de **modelos gratuitos oficiales**.

Para DeepResearch, no prometer reconstrucción exacta si un stream falla a mitad; usar checkpoints incluso con el gateway operativo.

## 14. Seguridad, aislamiento y costes

### 14.1 Seguridad de LiteLLM y criterio de aprobación

**Estado al 22/10/2026: HOLD.** El equipo de LiteLLM confirmó una publicación maliciosa de PyPI y contenedores `v1.107.0` el **21/10/2026**. Confirmó que su pipeline comprometido firmó/publicó artefactos maliciosos y que un despliegue expuesto permitió extraer credenciales aparecidas en logs. Ya había ocurrido otro incidente en marzo de 2026. Fuentes primarias:
- https://docs.litellm.ai/blog/security-incident-oct-21-2026
- https://docs.litellm.ai/blog/security-update-march-2026

**Matiz temporal importante:** el comunicado del 21/10 recomienda temporalmente fijar `1.106.2` o una versión anterior y menciona **una corrección privada** cuya vulnerabilidad tiene divulgación **prevista para el 27/10/2026**. **El 27 de octubre todavía es futuro** en la fecha de esta revisión: no afirmar que ya hay parche OSS ni que ninguna versión futura vaya a ser segura. La recomendación de no desplegar todavía es una **decisión conservadora del proyecto**, no una prohibición impuesta por el fabricante.

**Gates de seguridad para un eventual despliegue:**

- [ ] Aprobación expresa de un producto/versión oficial verificable y su historial de vulnerabilidades. Descartar artefactos comprometidos, tags móviles y autoupdate.
- [ ] Digest fijado y contrastado con un digest confiable independiente; firma y procedencia son controles **complementarios, no suficientes**: el atacante consiguió firmar artefactos.
- [ ] Contenedor no-root, rootfs readonly, secretos mínimos, sin socket Docker ni montajes del host, red de salida por allowlist verificable; interfaz solo local, sin exposición pública.
- [ ] **Logging de contenido de prompts, respuestas y payloads desactivado**, no depender de simples regex de redacción para tokens; retener solo métricas mínimas y trazas sin contenido.
- [ ] Ninguna credencial entra en contenedores/ambientes de reviewers. Controlar egress del servicio que tenga las claves. Revisar permisos/seguridad del host también.
- [ ] Si se usa Docker: enlazar puertos a `127.0.0.1`, excluir de Traefik público y de actualizadores automáticos. `internal: true` sin una vía de salida permitida no funcionará; la arquitectura de egress/proxy necesita prueba real.
- [ ] Validar rollback a OpenCode nativo y proceso de rotación de claves ante sospecha de compromiso.

**Decisión:** no hacer depender del gateway ninguna parte necesaria de la Fase 0–2. Cuando exista release pública evaluada y el riesgo sea aceptable, decidir si LiteLLM aporta valor frente a otro gateway consolidado; no imponerlo solo porque ya aparezca en el diagrama.

### 14.2 Costo cero real, no solo “alias free”

- **Denegar por defecto** todo modelo que no figure explícitamente aprobado como gratis.
- Precio de entrada **y** salida verificados en cero al momento de publicar catálogo.
- Bloquear endpoints que facturen por cache, razonamiento, herramientas, búsquedas externas, grounding o modalidades adicionales aunque la inferencia de texto sea gratuita.
- Desactivar recargas automáticas y configurar topes en el proveedor cuando sea posible.
- Contabilidad local de requests/tokens; los presupuestos precisos y bloqueos por gasto de LiteLLM requieren validar configuración y dependencias (p. ej. base de datos).
- No consultar modelos de pago como simple **test de salud**.
- **OpenRouter USD 10:** comprar créditos puede elevar el límite de solicitudes `:free`, pero no habilitar modelos de pago por ese hecho. Comprobar política de comisión, caducidad y cuenta antes de comprar.

### 14.3 Protección del proyecto durante reviews

**Problema verificado en el coordinador adjunto:** el revisor invoca OpenCode con `--agent build` y `--auto`, y `bubblewrap` monta `/` antes de montar **solo el proyecto** readonly, dejando rutas como HOME potencialmente accesibles y red sin aislamiento explícito. El texto del prompt no impide extracción de secretos. Esto es una **superficie de riesgo**, no demostración de que se hayan filtrado claves.

**Corrección obligatoria:**

1. Agente `review` específico con allowlist mínima de lectura (`read`, `glob`, `grep`), denegar `bash`, `webfetch`, edición y herramientas MCP no requeridas; verificar la **política efectiva** en la versión instalada de OpenCode. Probar que las exclusiones prevalecen sobre `--auto` y sobre configuración heredada.
2. Ejecutar en HOME temporal vacío y con filesystem restringido; denegar `.ssh`, `auth.json`, `.env`, tokens, socket Docker, secretos de contenedores y metadata del host. Revisar el montaje completo, no solo el directorio del proyecto.
3. No pasar claves externas por entorno del proceso reviewer. Proveer, cuando esté implementado y verificado, un endpoint local con autenticación inyectada **fuera del sandbox** y con límites de destinos/salida.
4. Limitar egress. `bwrap --unshare-net` puede servir de base, pero también bloquea el acceso al gateway por TCP; requiere diseño comprobado con socket/relay o network namespace apropiado, no asumir que basta añadir la opción.
5. Probar con archivos que contengan instrucciones de prompt injection y accesos simulados a secretos; exigir pruebas negativas para lectura/exfiltración y escrituras no autorizadas.

No enviar secretos reales a pruebas. Conservar informes SHA, quórum y estado actual durante la migración.

### 14.4 Gobierno de cambios

- Políticas, benchmark versionado, catálogo, fuentes, coste y términos sujetos a control de versiones.
- **`data_policy` por deployment**: `no_train`, `zero_retention`, `trains`, `unknown`; guardar por separado retención efectiva y jurisdicción. `zero_retention` es más estricto que `no_train`, no son sinónimos.
- **`confidentiality` por proyecto**: `public`, `internal`, `confidential`, y reglas explícitas. Por defecto, datos no públicos NO salen a deployments `trains`/`unknown`; para código privado sensible exigir política contractual aceptable y soporte de retención, no solo marketing.
- Google Gemini Free declara uso de contenido para mejorar productos; NVIDIA trial prohíbe enviar información confidencial; OpenRouter varía según proveedor seleccionado. Política exacta verificada antes de habilitar cada deployment.
- Auditoría **sin contenido de prompts ni respuestas**, con métricas/cuotas, deployment, motivo de fallback y SHA de ranking.
- Cambios al top-3 requieren evaluación local/aprobación, con rollback automático **de configuración**, no de binarios.
- Revisar explícitamente si el usuario realmente despliega en Hetzner, Docker y Traefik: **esos componentes aparecen en la crítica externa pero NO constan en el script entregado**; aplicar hardening específico solo si corresponde.

## 15. Plan de implementación recomendado

> **v1.2:** primero consolidar `native_first` en OpenCode (selector, revisores, DeepResearch gestionado); el gateway externo después, solo si se aprueba (addendum §10, mapa §9).

### Fase 0 — Inventario y baseline (sin romper producción)

- Capturar versiones de OpenCode, Python, bwrap y servicios de review.
- Documentar ubicación real de crons, scripts y arrays actuales.
- Ejecutar tests de los ciclos con las entradas de `OPENCODE_MODELS` actuales y capturar métricas base: **solicitudes, tokens de entrada/salida, tamaños máximos de prompt, 429, duración, éxito por reviewer/DeepResearch**; usar proyectos de prueba sin secretos.
- Hacer backup de configuración y estado. Crear fixtures sintéticos.

**Entrega:** `baseline.md`, inventario de versiones/flags y test reproducible.

### Fase 1 — Catálogo y ranking (sin gateway)

- Programar fetchers de Artificial Analysis, Hugging Face/LMArena y catálogos de proveedores.
- Implementar normalización, resolución de familias, validación de gratuidad, `data_policy`, confidencialidad y **capacidad TPM/TPD/RPM/RPD**. Ningún modelo entra en perfiles intensivos solo por precio cero.
- Publicar JSON atomizado, histórico y reporte legible.
- Integrar con cron existente sin duplicar programación.
- No sondear masivamente cuotas gratuitas.

**Entrega:** `catalog.json`, `rankings.json`, reporte `free-router status` y pruebas de fuentes caídas.

### Fase 2 — Política de reviewers y reservas

- Crear SQLite local de reservas/cooldowns.
- Cambiar `select_opencode_models()` a `select_reviewers(profile, round_id, snapshot_id)`, incluyendo la familia declarada y comprobada de `agy2`.
- Corregir colisiones de familia, usar IDs explícitos, registrar modelo real y fallbacks.
- Mantener `agy2`, `musej`, prompts, firmas SHA y quórum.
- Probar que el funcionamiento previo sigue válido con `--gateway=off`, sin exponer claves en el sandbox de reviewers, con reintentos y cuotas controlados únicamente por el coordinador.

**Entrega:** coordinador compatible hacia atrás y tests de concurrencia.

### Fase 3 — Gateway controlado (GATE DE SEGURIDAD)

- Decidir gateway aprobado después de los incidentes de LiteLLM.
- Ejecutarlo en localhost, versión/digest fijados y claves mínimas.
- Conectar **un** proveedor con inferencia gratuita real; ejecutar pruebas de tools y fallbacks.
- Ampliar paulatinamente a OpenRouter, NVIDIA, Groq, Gemini y Cerebras validados.
- Exponer `free-research` y `free-general` a OpenCode.
- Incluir la API oficial OpenCode Console **solo tras pruebas de compatibilidad real**; el soporte de endpoint ya está documentado.

**Entrega:** configuración de gateway fijada, `security-approval.md`, pruebas end-to-end.

### Fase 4 — DeepResearch resistente

- Crear checkpointing y resumen de fuentes, estado y pendientes.
- Verificar tool calls y recuperación ante 429/5xx/stream parcial.
- Asegurar que la continuidad no duplica herramientas ni pierde evidencia.
- Medir latencia, solicitudes por investigación y degradación de calidad.

**Entrega:** sesión de investigación larga reproducible y resultados verificados.

### Fase 5 — Dos rondas (sin afectar el quórum previo)

- Incorporar nuevas fases con migración de estado y rollback.
- Configurar modos `auto`, `1`, `2` para rondas.
- Política `reuse_best_plus_novel`, `four_distinct` opcional y `reuse_top_two`.
- Verificar estado final después de la última corrección.
- Evitar nuevas tareas `SIGUE` mientras haya reviews/correcciones pendientes.

**Entrega:** pipeline R1 → corrección → R2 → eventual nueva corrección/verificación → siguiente ciclo.

### Fase 6 — Estabilización

- Ensayo desatendido en proyecto de prueba.
- Métricas de efectividad de reviews, costes, errores y tiempos.
- Alertas por ausencia de modelos gratuitos, fallos de renovación y nuevas vulnerabilidades del gateway.
- Documentación operativa: inicio, parada, backup, recuperación, rotación de claves y rollback.

---

## 16. Pruebas de aceptación y escenarios obligatorios

| ID | Escenario | Resultado esperado |
|---|---|---|
| T01 | Proveedor gratuito devuelve 429 antes de responder | Salta al siguiente deployment elegible sin consumir modelo de pago |
| T02 | OpenRouter alcanza cupo global | Se bloquean temporalmente sus deployments; continúan otros proveedores independientes |
| T03 | NVIDIA falla mientras Groq funciona | Continúa vía Groq **solo si** supera TPM/TPD/contexto/privacidad del perfil; en caso contrario bloquea |
| T04 | Dos reviewers compiten por una familia | Nunca terminan ambos con la misma familia salvo excepción explícita registrada |
| T05 | Dos API providers ofrecen el mismo modelo base | Se consideran backups del modelo, **no** dos opiniones independientes |
| T06 | Reviewer A falla cuando B ya terminó | Solo se reintenta A; B conserva informe y SHA |
| T07 | No hay dos familias aptas | Se registra bloqueo o se aplica quórum con regla explícita; nunca fingir independencia |
| T08 | R1 encuentra error crítico y `musej` corrige | R2 revisa el estado corregido y busca regresiones |
| T09 | `correction_2` cambia código | Hay verificación final antes de pasar a `next` |
| T10 | Modelo #1 cae; #2 y #3 sanos | Fallback sigue prioridades vigentes, no selección aleatoria inesperada |
| T11 | Un modelo pierde condición gratuita o precio desconocido | Sale del catálogo utilizable y no recibe solicitudes |
| T12 | Cron falla mientras un ciclo está activo | Conserva snapshot previo; no altera modelos en la ronda actual |
| T13 | MCP retorna resultado válido; streaming se corta | No duplica la llamada a herramienta ni declara éxito falso |
| T14 | Gateway no está disponible | Sesiones reportan error claro; mecanismos legacy/nativos siguen utilizables según política |
| T15 | Se inyecta respuesta con token sensible | Logging de contenido desactivado; nunca persiste secretos en trazas |
| T16 | Modelo con tool calling defectuoso responde | Excluirlo de perfiles MCP/review y elegir candidato compatible |
| T17 | `agy2` falla pero dos reviews OpenCode válidas completan | Continúa solo si las dos familias efectivas son verificadamente distintas; registra fallo |
| T18 | Al reiniciar coordinador, hay informe ya verificado | No reescribir ni perder el informe; recuperar desde recibo válido |
| T19 | Ranking nuevo contradice uno antiguo | Registro de fuentes/fecha, snapshot y motivo de nueva prioridad |
| T20 | Gateway seleccionado presenta alerta de seguridad | Congelar despliegues nuevos y activar rollback al modo nativo |

**Criterios medibles mínimos propuestos:**

- 0 invocaciones a modelos de pago durante pruebas de cero coste.
- 0 informes aceptados sin SHA/recibo y modelo efectivo identificado.
- 0 colisiones de familia dentro de una ronda estricta.
- 100% de cambios de ranking publican snapshot inmutable y esquema válido.
- 100% de errores de cuota se clasifican y registran; no garantizar que todos tengan fallback posible.
- ≥95% de recuperación de fallos **pre-respuesta** en batería sintética, **cuando exista otro proveedor sano y compatible**.
- 0 operaciones de herramientas no idempotentes repetidas sin comprobación.

---

## 17. CLI de administración propuesto

Estos **no son comandos existentes**. Son la interfaz que se podría implementar en un pequeño programa Python (`free_router.py`) sin inventar un servicio adicional:

```text
free-router status
free-router models --profile research --top 20
free-router models --profile review --top 20
free-router sources --show-staleness
free-router update --benchmarks
free-router update --providers
free-router validate-config
free-router test-tools --provider openrouter --max-requests 2
free-router simulate-fallback --profile research
free-router sessions --active
free-router reservations --round <id>
free-router cooldowns
free-router cost-audit --since 24h
free-router explain --model <canonical-id>
free-router rollback --snapshot <id>
```

Funcionalidad prioritaria: `status`, `models`, `validate-config`, `simulate-fallback`, `explain`. El resto después.

---

## 18. Riesgos y decisiones abiertas antes de tocar el equipo

> **v1.2:** la restricción de OpenCode Free a su cliente nativo (403 a externos) es una **restricción confirmada**, no una «compatibilidad pendiente de prueba»: la fila «OpenCode Console/Free fuera de gateway» queda cerrada (addendum §1, mapa §9).

| Riesgo/decisión | Política recomendada |
|---|---|
| Incidente de seguridad LiteLLM del 21/10/2026 | **Bloqueo de despliegue** hasta aprobación; no usar tags móviles |
| Incompatibilidades OpenCode clásico y V2 | Detectar versión y usar **un** esquema correcto; no mezclar |
| OpenCode Console/Free fuera de gateway | API externa oficialmente documentada; integrar por adaptador solo después de pruebas reales de tools/contexto/streaming y coste cero |
| Cuotas API con términos cambiantes | Refresh de catálogos y consulta de políticas; no asumir ilimitado |
| Cuatro reviewers distintos de peor calidad | Preferir segunda ronda con modelo superior + otra familia competente |
| Herramientas no idempotentes | Checkpoints y comprobación del estado antes de repetir |
| Filtración de secretos por logs o prompts | Desactivar logging de contenido y aislar HOME, claves y red de reviewers; no depender de regex |
| Falsos positives de reviewers | Evaluaciones propias por hallazgos confirmados y evidencia verificable |
| Cambio de ranking durante una ronda | Snapshot congelado por `run_id` y `round_id` |
| Saturación por probes | TTL y presupuesto de peticiones para probes |
| Crons duplicados | Reutilizar los ya existentes o sustituirlos, nunca ambos |
| El gateway reporta alias, no modelo real | No usarlo en independencia estricta sin observabilidad adicional |

### Decisiones para fijar en `policy.yaml` (valores sugeridos)

```yaml
policy_version: 2
strict_zero_cost: true
allow_paid_fallback: false
allow_unknown_price: false
use_only_verified_endpoints: true
confidentiality_default: internal
privacy:
  require_no_train_for_nonpublic: true
  reject_unknown_data_policy: true
  min_retention_standard_for_confidential: zero_retention
capacity:
  require_known_tpm_for_research: true
  require_known_tpm_for_review: true
  reserve_daily_request_budget_for_review: 100  # valor de ejemplo: calibrar con mediciones
  use_provider_counters_when_available: true

profiles:
  research:
    require_tools: true
    preferred_context_tokens: 131072
    minimum_context_tokens: 65536
    min_quality_percentile: 60
  review:
    require_tools: true
    minimum_context_tokens: 65536
    min_quality_percentile: 65
    distinct_family_per_round: true
  general:
    require_tools: false
    minimum_context_tokens: 16384
    min_quality_percentile: 45

review:
  rounds: auto               # auto | 1 | 2
  second_round_strategy: reuse_best_plus_novel
  max_correction_loops: 2
  keep_agy2: true
  agy2_family_id: null       # verificar antes de contar agy2 en quórum estricto
  quorum_distinct_families: true
  rounds_auto_critical_globs:
    - "security/**"
    - "auth/**"
    - "payments/**"
    - "migrations/**"
    - "infra/**"
  quorum: 2                  # conservar 2/3 con criterios de validez
  reserve_distinct_families: true

routing:
  max_total_hops: 5
  max_attempts_per_deployment: 1  # para reviews: el coordinador decide el retry completo
  prefer_session_stickiness: true
  require_effective_model_audit_for_reviews: true

cron:
  benchmark_refresh_hours: 24
  model_catalog_refresh_hours: 6
  keep_last_good_snapshot: true
  require_local_evals_for_top3_changes: true
  defer_gateway_reload_until_idle: true
```

**Importante:** esta configuración es **diseño**, no un formato de configuración ya soportado por LiteLLM. Un generador propio debe traducirla a las opciones concretas que acepte el gateway seleccionado.

---

## 19. Entregables y definición de terminado

La implementación solo se considera completada cuando existan:

- [ ] Código de fetchers y ranker, con tests y fuentes documentadas.
- [ ] `catalog.json` y `rankings.json` válidos y versionados.
- [ ] `policy.yaml` y generador de config del gateway.
- [ ] Informe de seguridad sobre la versión exacta de gateway elegida.
- [ ] Gateway local con 2+ proveedores independientes realmente gratuitos, ampliable después.
- [ ] OpenCode conectado a `free-research` y `free-general`; `free-review` usable para review interactiva.
- [ ] Reviewers con modelos reservados y de familias distintas + `agy2` y quórum conservados.
- [ ] Segunda ronda opcional/automática con evaluación de correcciones y cierre verificable.
- [ ] Fallback probado sin caer en modelos de pago ni repetir operaciones MCP peligrosas.
- [ ] Métricas: proveedor/modelo efectivos, calidad, cuotas, fallback, errores y resultados.
- [ ] Crons existentes integrados, sin duplicados, con logs y recuperación.
- [ ] Manual de rollback al flujo OpenCode original.
- [ ] Pruebas T01–T20 + batería §11 del addendum (A01–A12), con evidencia y revisión de criterios de aceptación.

---

## 20. Orden de ejecución prioritario

**No empezar instalando varios routers.** Orden de trabajo:

1. **Asegurar y medir lo existente**: snapshot del coordinador y crons.
2. Implementar **catálogo/ranking gratuito** de forma independiente de cualquier gateway.
3. Corregir reservas y fallback de reviewers en el coordinador actual, incluyendo `agy2`, entorno seguro y una única autoridad de reintentos.
4. Endurecer aislamiento de reviewers, privacidad por deployment y medición de cuotas **antes de introducir más claves**; resolver formalmente el riesgo del gateway si se decide incorporarlo.
5. Solo tras el gate de seguridad, evaluar e integrar **un gateway aprobado** y habilitar `free-research` / `free-general`; mientras tanto mantener el flujo nativo.
6. Ejecutar investigación larga con MCP y fallos simulados; añadir checkpoints.
7. Añadir segunda ronda de revisión con política configurable.
8. Medir; optimizar cuotas y calidad con datos propios.

**Recomendación de ingeniería:** es una arquitectura de gateway universal **en su interfaz**, aunque el único cliente inicial sea OpenCode. Esto facilita sumar Hermes/OpenClaw más adelante sin instalar ahora servicios que no se usan. La complejidad extra se limita a una API local compatible, un inventario JSON y un coordinador Python ya existente.

---

## 21. Fuentes primarias verificadas (consulta: 22/10/2026)

> **v1.2:** priorizar la documentación oficial V2 de Inference API, Models y `session.switchModel` (addendum §13, mapa §9).

1. **LiteLLM — incidente del 21 de octubre de 2026**: https://docs.litellm.ai/blog/security-incident-oct-21-2026
2. **LiteLLM — incidente de marzo de 2026**: https://docs.litellm.ai/blog/security-update-march-2026
3. **LiteLLM — Routing, prioridad, cooldowns, fallback y session affinity**: https://docs.litellm.ai/docs/routing
4. **LiteLLM — Config YAML**: https://docs.litellm.ai/docs/proxy/configs
5. **LiteLLM — Docker quickstart, para lectura y validación, NO ejecutar sin pin**: https://docs.litellm.ai/docs/proxy/docker_quick_start
6. **LiteLLM — NVIDIA NIM**: https://docs.litellm.ai/docs/providers/nvidia_nim
7. **OpenCode — Providers (clásico)**: https://opencode.ai/docs/providers/
8. **OpenCode — Providers (V2)**: https://opencode.ai/v2/docs/providers
9. **OpenCode — CLI run**: https://opencode.ai/v2/docs/cli/commands
10. **OpenRouter — límites de modelos gratuitos**: https://openrouter.ai/pricing
11. **OpenRouter — catálogo y modelos gratuitos**: https://openrouter.ai/collections/free-models
12. **OpenRouter — Models API**: https://openrouter.ai/docs/api/api-reference/models/get-models
13. **Artificial Analysis — Data API**: https://artificialanalysis.ai/data-api/docs
14. **Artificial Analysis — endpoint Free y campos**: https://artificialanalysis.ai/data-api/docs/reference
15. **LMArena — dataset oficial**: https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset

### Notas sobre lo todavía no verificado

- No se ha accedido a tus cuentas ni a sus cuotas reales; no hay garantía de que un modelo específico esté disponible o gratuito hoy **para tu cuenta**.
- No se han inspeccionado los crons externos, porque su código no venía en el archivo adjunto.
- No se ha probado la versión OpenCode de tu equipo ni el formato de los eventos JSON que genera contra los ejemplos propuestos.
- No se ha instalado ni ejecutado gateway alguno; los snippets son contratos/plantillas con placeholders.
- No hay una API única oficial que ordene **todos** los modelos gratuitos de **todos** los proveedores con garantía de herramientas y de cuota vigente. El catálogo unificado debe construirse.
- El incidente reciente exige revisar la versión y procedencia exactas del gateway **al momento del despliegue**, incluso si esta especificación queda archivada.

---

**Resultado buscado:** desde OpenCode, perfiles de inferencia gratuitos capaces de pasar de un deployment a otro sin intervención cuando el problema sea recuperable; en review automatizado, dos sesiones de modelos de familias distintas con quórum, fallback coordinado, evidencia verificable y segunda ronda configurable; todo alimentado por benchmarks oficiales y crons existentes, sin modelos de pago ni dependencia de routers comunitarios pequeños.

---

## 21.1. Fuentes añadidas y límites de lo afirmado (v1.1)

- OpenCode Console Inference API oficial: https://opencode.ai/v2/docs/console/api/inference/
- OpenCode Console lista de modelos gratuitos, precios y retención: https://opencode.ai/v2/docs/console/models/
- OpenRouter límites y cuota visible por API: https://openrouter.ai/docs/api_reference/limits
- Google Gemini API Free y uso de contenido: https://ai.google.dev/gemini-api/docs/pricing
- Groq límites por modelo y cuenta: https://console.groq.com/docs/rate-limits
- NVIDIA API Trial Terms (privacidad y exclusión de información confidencial): https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf
- LiteLLM incidente de 21/10/2026, incluido anuncio de parche privado previo a divulgación planeada 27/10: https://docs.litellm.ai/blog/security-incident-oct-21-2026
- LiteLLM gestión de modelos y DB: https://docs.litellm.ai/docs/proxy/model_management
- Artificial Analysis plan gratis y rate limits: https://artificialanalysis.ai/data-api/docs

**No verificado sin acceder al entorno del usuario:** límites efectivos de sus cuentas; disponibilidad real de la API de OpenCode sin token en su caso; versión y mecanismos de retry de OpenCode instalado; identidad real de `agy2`; ubicaciones/configuraciones de sus crons; despliegue específico en Hetzner, Docker o Traefik; modelo de amenaza y política contractual de cada repo; estado de una futura corrección pública de LiteLLM tras el 22/10/2026.

---

