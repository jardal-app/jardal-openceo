# Memoria y aprendizaje de los agentes — Dirección definitiva

**Fecha:** 2026-10-27
**Estado:** Aprobado por el Owner. La implementación empieza por la auditoría del paso 1.1.
**Alcance:** OpenCEO (Dev, CEO, planner y agentes de rol), el vault de `jardal-knowledge`, las sesiones manuales (Claude Code, Codex, Antigravity, Muse Code) y la evolución futura hacia Jardal ERP.
**Ubicación sugerida:** repositorio de OpenCEO, junto a `OPENCEO_DIRECTION.md`. En el vault, una nota de decisión corta del proyecto `jardal-openceo` que enlace a este documento, sin copiarlo.

## 0. Cómo usar este documento

- Es la referencia para todo cambio en la memoria y el aprendizaje de los agentes. Complementa `docs/ARCHITECTURE_REFERENCE_2026.md`, la sección «Vault y mantenimiento del conocimiento» de `jardal-knowledge/README.md`, el skill `obsidian-vault-links` (`jardal-knowledge/.agents/skills/obsidian-vault-links/SKILL.md`) y —fuera de estas fases— el documento del ERP (D14, etapa 5). No modifica los ADR aprobados del ERP.
- Lo que este documento describe como existente procede de esa documentación y **no está auditado en código**. Antes de crear tickets de implementación se verifica contra el código y los tests (paso 1.1).
- No es un backlog. Los tickets se registran en ERPNext y citan la decisión que implementan (D1–D14).
- Una decisión solo cambia con una decisión nueva del Owner, basada en un problema observado o en una medición.

## 1. Objetivo

Que los agentes aprendan del trabajo diario, compartan conocimiento dentro del proyecto correcto, mejoren sus decisiones y reduzcan la supervisión del Owner, con seguridad y trazabilidad.

No se busca que recuerden todo. Se busca que recuerden lo que cambia cómo deben trabajar la próxima vez, y que sepan cuándo una regla dejó de valer.

## 2. Principios

1. **Una fuente de verdad por tipo de conocimiento.** Nada se duplica entre sistemas.
2. **La memoria pertenece al proyecto, no al agente.** Varios agentes contribuyen a la misma memoria según sus permisos.
3. **Aprender es escribir texto gobernado que se recupera después.** Los pesos del modelo no cambian. Cargar más memoria no es mejor: el contexto es finito y su exceso degrada el rendimiento.
4. **Ningún agente autónomo escribe directamente en la memoria duradera.** Propone; el código valida y publica.
5. **Una lección aprendida no es una autorización.** El aprendizaje nunca amplía permisos, límites ni gasto. Solo propone, y el Owner decide.
6. **Lo que debe cumplirse siempre se aplica en código** (locks, hooks, permisos), no en instrucciones de texto.
7. **Se aprende de resultados verificados por OpenCEO**, no de la autoevaluación del agente ni del volumen de tickets cerrados. Cuentan los aciertos, los fracasos y las abstenciones.
8. **Recuperación acotada:** índice siempre, detalle bajo demanda. Nunca se carga el vault entero.
9. **Nada de infraestructura nueva sin necesidad demostrada** y sin una mejora medible.

## 3. Mapa de memoria

| Memoria | Qué guarda | Dónde vive | Quién escribe | Cómo se usa |
|---|---|---|---|---|
| De ejecución | El contexto de una corrida | Prompt de la ejecución | — | Se descarta al terminar |
| Instrucciones del proyecto | Reglas que valen en cada sesión e índice de prácticas | `CLAUDE.md` o `AGENTS.md` (equipo) y `CLAUDE.local.md` (personal), con menos de 200 líneas; reglas por ruta en `.claude/rules/` | El Owner, o el agente con su confirmación | Se carga siempre; las reglas por ruta, al tocar esos archivos |
| Conocimiento del proyecto | Arquitectura, decisiones, componentes, incidentes, investigación, glosario y **prácticas de trabajo** | `vault/projects/<org>/<proyecto>/` | Las sesiones manuales, directamente; los agentes autónomos, mediante el publicador (D7) | Hub e índice; las notas, bajo demanda |
| Procedimientos | Cómo ejecutar tareas repetibles | Skills por rol, en git | Cristalización con compuerta por resultado (D9) | Catálogo; se cargan al usarse |
| Recetas entre agentes | Secuencias de tickets | Playbooks, en git | Aprobación del Owner | El planner las consulta |
| Precedentes | Propuesta del agente, respuesta del Owner y resultado | OpenCEO (Mongo): referencias y resúmenes fechados | Automático (D10) | Hasta 3 casos parecidos al proponer |
| Reglas y límites | Mandato, allowlists, locks, delegaciones | `projects/company_mandate.yaml`, configuración por proyecto y herramientas | El Owner | Los aplica el código |
| Trabajo y estados | Tickets y su estado oficial | ERPNext (después, Jardal ERP); Jira y Autotask en los trabajos remotos | Cada sistema | No se duplican |
| Personal | Objetivos y planificación del Owner | Horizon | El Owner | Privado |
| Memoria propia de los motores | Notas que el motor escribe por su cuenta | No se usa (D5) | — | — |

## 4. Estado actual y brechas

La columna «Origen» indica la sección o el ticket de la documentación de referencia. Todo está pendiente de auditar en código (paso 1.1).

| Capacidad | Hoy (según la documentación) | Falta | Origen |
|---|---|---|---|
| Vault por proyecto, skill y checker con `--project` | Existe | Tipo `practice` (D3) | `jardal-knowledge/README.md` (§ Vault y mantenimiento del conocimiento), skill `obsidian-vault-links` |
| Lectura del vault por agentes autónomos | No: el sandbox no monta el vault | Montaje de solo lectura del proyecto activo e índice de prácticas (D6) | Agent isolation (00192, 00214) |
| Escritura del vault por agentes autónomos | No | Propuestas y publicador (D7, D8) | — |
| Respaldo nocturno | Existe: systemd, bloqueo, trabaja en `main`, no hace pull ni force-push | Exclusión mutua con el publicador (D8) | `jardal-knowledge/README.md` (§ Vault y mantenimiento del conocimiento), `jardal-knowledge/scripts/nightly-git-backup.sh` |
| Cristalización de skills | Existe: corre tras cierres completados o fallidos y promueve por reutilización en otro ticket | Promoción por resultado verificado, métricas por versión, reversión y retiro (D9) | 00210 |
| Canal del Owner y locks | Existe: preguntas al Owner y locks de correo, DNS y Elfec; lo aprobado se ejecuta una sola vez | Enlazar la respuesta con su resultado (D10) y locks para Teams, Jira y Autotask (D12) | 00206, 00208 |
| Envío a Teams, Jira y Autotask | No existe; solo hay correo | Herramientas sobre los locks existentes (D12) | 00199 |
| Revisión del CEO | Existe: cada revisión es una ejecución nueva que no recupera decisiones anteriores con su resultado | Precedentes evaluados (D11) | 00119, 00213 |
| Telemetría del planner | Existe solo como información (`tier_mix`, `executor_link`, coste) | Precedentes de tickets cerrados (D10) | 00148, 00178, 00212 |
| Handoff entre tickets | Solo desde los predecesores directos | Cubierto por los precedentes | 00211 |
| Memoria automática de Claude Code | Activada por defecto en sesiones locales; estado real desconocido | Comprobar, migrar y desactivar (D5) | Documentación de Claude Code |
| Sección «Engineering Knowledge and Code Context» | Alineada con el skill (paso 1.3, TASK-2026-00324) | — | `docs/ARCHITECTURE_REFERENCE_2026.md` |
| Insignias de agente | Se atan al nombre del ticket en ERPNext | Aceptar referencias externas si se ejecutan tickets de Jira o Autotask en autónomo (D13) | 00214 |

## 5. Decisiones

**D1. Arquitectura conservada.** ERPNext, y después Jardal ERP, es la autoridad de negocio. OpenCEO es el plano de ejecución, el vault es la memoria de conocimiento por proyecto, y git guarda el código, las skills y los playbooks. No hay cerebro central, memoria libre por agente, grafo de conocimiento, base vectorial ni otro runtime (Hermes Agent, OpenClaw).

**D2. Un vault, organizado por proyecto y compartido.** Las sesiones manuales y las autónomas leen y amplían el mismo vault. No hay carpetas por agente. Ningún proyecto comparte prácticas, precedentes ni notas con otro de forma automática; rige la regla de aislamiento del skill.

**D3. Categoría `practices/`.** Guarda cómo se trabaja en el proyecto: entregas, pull requests, validaciones, comunicación, criterios de revisión, correcciones confirmadas y acuerdos vigentes.

- Una nota por área.
- Cada regla lleva fuente (enlace o ID), autoridad y fecha.
- Una regla nueva reemplaza a la anterior en su sitio; git conserva el historial.
- Antes de crear notas, se añade `type: practice` al skill y al checker.

Estructura resultante:

```text
vault/projects/<org>/<proyecto>/
├── <proyecto>.md
├── architecture/
├── decisions/
├── components/
├── incidents/
├── research/
├── glossary/
└── practices/        ← nueva
```

**D4. `CLAUDE.md` corto.** Menos de 200 líneas: solo lo que vale en cada sesión y un índice de las prácticas.

- Los procedimientos van a skills.
- Lo que afecta a una zona del código va a reglas por ruta.
- Lo personal va a `CLAUDE.local.md` o a un archivo importado.
- Lo que debe cumplirse siempre (no enviar sin confirmación, no tocar producción) se aplica con locks, hooks o permisos. `CLAUDE.md` es contexto: el cliente no lo hace cumplir.

**D5. Memoria propia de los motores.**

1. Comprobar si la memoria automática de Claude Code está activa en cada perfil (`~/.claude/`, `~/.claude-j/`, `~/.claude-deepseek/`) y en las ejecuciones de Tier A.
2. Inventariar su contenido y migrar lo útil a `practices/` o a `CLAUDE.md`.
3. Desactivarla donde corresponda, con `autoMemoryEnabled: false` o `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

Los motores no conservan memoria propia: todo el aprendizaje va a los almacenes de la sección 3.

**D6. Lectura preventiva.**

- Dev, en todos sus modos de ejecución, lee en solo lectura la carpeta del proyecto activo.
- Al iniciar cada ticket, Dev recibe el índice de prácticas y del hub, y lee las notas pertinentes antes de planificar.
- Los demás roles reciben lectura cuando su función lo requiere por diseño (por ejemplo, Research sobre un proyecto) o cuando uno termina `bloqueado` o `no_aplica` por falta de conocimiento.
- Ningún rol lee otro proyecto.

**D7. Escritura mediante propuestas.** Los agentes autónomos no escriben en el vault. Dejan propuestas en la carpeta de su ticket, y un publicador de OpenCEO las valida y publica fuera del sandbox.

Validaciones obligatorias:

- La nota de destino está dentro de la carpeta del proyecto del ticket.
- La evidencia existe y se puede resolver: ticket, commit o archivo.
- Si ya existe una nota del mismo tema, la propuesta la actualiza; el agente busca antes de proponer, como exige el skill.
- No contiene secretos.
- Pasa `vault_check.py --project`.
- La versión de la nota no cambió desde que se generó la propuesta.
- Nunca modifica `CLAUDE.md`, perfiles, skills ni archivos fuera del vault del proyecto.

Tratamiento según el contenido:

| Contenido propuesto | Tratamiento |
|---|---|
| Hecho técnico, causa de incidente o decisión técnica, comprobados por las compuertas de OpenCEO (tests, oráculo, verificación de entrega) | Publicación automática |
| Práctica, regla de trabajo o instrucción para futuros agentes, venga de donde venga | Confirmación del Owner, agrupada en el resumen |
| Cambio de permisos, obligaciones, políticas o mandato | Aprobación expresa del Owner |
| Opinión no confirmada | Queda provisional y no se publica |
| Avances, pasos y arreglos rutinarios | Se quedan en el ticket |

«Comprobado» significa verificado por OpenCEO, no declarado por el agente. Una publicación fallida conserva la propuesta para reintentarla y no bloquea el cierre del ticket.

Formato mínimo de una propuesta:

```yaml
target: projects/<org>/<proyecto>/incidents/2026-10-27-<tema>.md
action: create          # create | update
class: descriptive      # descriptive | normative
base_version: <hash git de la nota leída, o null si es nueva>
evidence:
  - ticket: TASK-2026-00NNN
  - commit: <sha>
  - path: <ruta en el repositorio>
content: |
  <nota completa con frontmatter>
```

**D8. Git y concurrencia.**

- El publicador escribe en el mismo directorio local del vault, con exclusión mutua compatible con el respaldo nocturno, y hace commit con el número del ticket.
- No hace push propio: el respaldo no hace pull, y un push paralelo lo dejaría atascado.
- Antes de escribir comprueba que la versión base de la nota no cambió. Si cambió, o si la nota tiene cambios sin commitear, reintenta después.
- Nunca sobrescribe cambios de otra sesión.

**D9. Skills evaluadas por resultado.**

- Siguen creándose solas, y un fallo puede dejar un borrador.
- Un borrador solo pasa a activo cuando un ticket distinto lo usa y cierra con resultado verificado.
- Cada versión registra usos, éxitos verificados, devoluciones `bloqueado` o `no_aplica` y retrabajo. Una regresión abre la propuesta de revertir.
- Las skills sin uso o con bajo éxito se retiran.
- Se editan con cambios pequeños y evidencia, nunca con reescrituras completas.
- Un borrador que incorpore contenido externo pasa, antes de promoverse, por una revisión independiente proporcional al riesgo.

**D10. Precedentes.** OpenCEO enlaza la propuesta del agente, la respuesta del Owner (aprobó, editó con un cambio concreto o rechazó) y el resultado observado.

- Reutiliza lo que ya registra: preguntas al Owner, revisiones del CEO y resultados de tickets.
- Mongo guarda referencias y resúmenes fechados; la autoridad sigue en ERPNext, Jira o Autotask.
- «Verificado» se refiere al resultado, no al acierto: se recuperan aciertos, fracasos y abstenciones correctas.
- Un resultado inconcluso queda en `null` y no cuenta como evidencia.
- Al proponer se recuperan hasta 3 casos parecidos del mismo proyecto (Dev) o del mismo ámbito (CEO), como información y no como umbral.

**D11. Aprendizaje del CEO.**

- Aprende de sus decisiones, de las respuestas del Owner y de los resultados, incluidas las veces en que no hacer nada fue lo correcto.
- No infiere causalidad solo porque un KPI cambió.
- Nunca edita su mandato ni amplía límites. Propone cambios (umbrales, allowlists, mandato) como preguntas al Owner, agrupadas en el resumen.
- La v1 no incluye un archivo de lecciones.

**D12. Autonomía y mensajes.**

- **Mensajes en nombre del Owner** (Teams, Jira, Autotask, correo): siempre con confirmación, mostrando el destinatario y el texto exacto. Lo aprobado se envía exactamente una vez. Cambiar esta regla es decisión del Owner.
- **Otras acciones externas:** empiezan con confirmación. El Owner puede delegarlas por categoría y proyecto, con evidencia (por ejemplo, N propuestas aceptadas sin cambios y ningún incidente). La delegación queda como política ejecutable en la herramienta, no como conclusión del modelo, y se puede revocar.
- **Siempre con confirmación:** producción, merges a ramas protegidas, acciones irreversibles y todo lo que quede fuera de una delegación.
- El envío a Teams, Jira y Autotask reutiliza `tool_locks.py` y el canal del Owner. No se crea un segundo sistema de aprobaciones.

**D13. Sistemas externos.**

- Jira, Autotask y Teams son la fuente original de sus tickets y conversaciones. No se duplican en el ERP; OpenCEO guarda referencias (`source`, `source_id`).
- Los conectores de terceros se usan en solo lectura; las escrituras pasan por las herramientas de D12.
- Solo se aprende de eventos concretos: cambios solicitados en un PR, un ticket reabierto o rechazado, un mensaje directo de una persona con autoridad declarada en el proyecto.
- Se guarda la regla derivada con su enlace, nunca la conversación.
- Si se ejecutan tickets externos en autónomo, el emisor de insignias debe aceptar referencias externas.

**D14. Futuro en Jardal ERP.** En la etapa 5, un solo módulo de conocimiento sirve a personas (UI) y a empleados IA (API).

- Por tenant, empresa y rol, con RLS, versiones, dueño y aprobador por página, y búsqueda de texto en español.
- Embeddings solo si se miden fallos de recuperación y con un ADR.
- Ese ADR, nuevo y compatible con los aprobados, fijará tres reglas:
  1. Lo aprendido en un tenant nunca se aplica a otro.
  2. El administrador del tenant aprueba lo que cambia el comportamiento.
  3. Todo lo aprendido conserva su procedencia para auditarlo y borrarlo.
- Jardal migra ahí su conocimiento de empresa como tenant 1. El vault técnico se queda en git.

## 6. Fuera de alcance

- Un servicio de «cerebro» o de memoria central.
- Grafo de conocimiento, GraphRAG, base vectorial o modelos de embeddings locales en el host compartido.
- Memoria libre por agente o carpetas por agente en el vault.
- Adoptar el runtime de Hermes Agent u OpenClaw.
- Fine-tuning.
- Que el CEO edite su mandato o que el aprendizaje amplíe permisos.
- Guardar conversaciones, transcripciones o avances de tickets en el vault.
- Duplicar tickets de Jira o Autotask en el ERP.
- Backlogs en Markdown.

## 7. Plan de implementación

Las fases no bloquean el desarrollo comercial de Jardal ERP. Cada paso se registra como ticket en ERPNext con sus dependencias, criterios de aceptación y pruebas.

### Fase 1 — Alinear lo que existe

| Paso | Qué | Depende de | Aceptación y pruebas |
|---|---|---|---|
| 1.1 | Auditar el código y los tests de: montaje del sandbox, script y bloqueo del respaldo, `tool_locks.py` y `owner_channel.py`, condición de promoción de `crystallization.py`, persistencia de `CEODecision` y emisor de insignias | — | Informe por punto con archivo, línea o test, y las diferencias con la documentación, adjunto al ticket |
| 1.2 | Memoria automática de Claude Code (D5) | — | Inventario por perfil y proyecto; lo útil, migrado; desactivada donde se decidió; ejecuciones de Tier A sin memoria automática |
| 1.3 | Corregir la sección del vault en el documento de OpenCEO | — | Sin búsqueda global con qmd, sin checker sin `--project` y sin `qmd update` desde proyectos; remite al skill |
| 1.4 | `type: practice` en el skill y el checker (D3) | — | Tests con fixtures sintéticos: nota válida, campos faltantes, ruta fuera del proyecto |
| 1.5 | Limpiar cada `CLAUDE.md` (D4) con `/doctor prompt-audit` | 1.4 | Cada archivo con menos de 200 líneas; lista de reglas trasladadas, sin pérdidas; `/context` muestra lo esperado |

### Fase 2 — OpenCEO consulta y mejora el vault

| Paso | Qué | Depende de | Aceptación y pruebas |
|---|---|---|---|
| 2.1 | Montaje de solo lectura del proyecto activo para Dev en todos sus modos (D6) | 1.1 | Dev lee el hub y las prácticas; leer otro proyecto falla; escribir falla |
| 2.2 | Índice de prácticas y del hub al iniciar cada ticket (D6) | 2.1 | El prompt incluye un índice acotado, nunca el vault; probado con un proyecto de prueba |
| 2.3 | Formato de propuestas e instrucción en el perfil de rol (D7) | 1.4 | Las propuestas inválidas se rechazan con el motivo registrado en el ticket |
| 2.4 | Publicador (D7, D8) | 1.1, 2.3 | Tests con repositorio temporal: publicación válida; versión cambiada provoca reintento; nota con cambios sin commitear se salta; respaldo y publicador no se solapan; evidencia inexistente o proyecto ajeno provocan rechazo; el contenido normativo va a confirmación |
| 2.5 | Confirmación en bloque de las propuestas normativas, sobre el canal del Owner | 2.4 | Una sola pregunta agrupada; aprobar publica exactamente lo mostrado; rechazar descarta con motivo |

### Fase 3 — Aprendizaje verificable

| Paso | Qué | Depende de | Aceptación y pruebas |
|---|---|---|---|
| 3.1 | Promoción de skills por resultado (D9) | 1.1 | Un borrador de un cierre fallido no se promueve; reutilizarlo con cierre verificado lo promueve; reutilizarlo con fallo no lo promueve |
| 3.2 | Métricas por versión, reversión y retiro de skills (D9) | 3.1 | Métricas visibles por versión; una regresión abre la propuesta de revertir; las skills sin uso se retiran |
| 3.3 | Registro de precedentes (D10) | 1.1 | Cada respuesta del Owner genera un precedente con referencias; el resultado se completa por eventos; lo inconcluso queda en `null` |
| 3.4 | Recuperación de precedentes en las propuestas de Dev y las revisiones del CEO (D10, D11) | 3.3 | Hasta 3 casos del mismo proyecto o ámbito, incluidos fracasos y abstenciones; reproducción histórica usando solo precedentes anteriores a cada caso |

### Fase 4 — Autonomía sobre sistemas externos

| Paso | Qué | Depende de | Aceptación y pruebas |
|---|---|---|---|
| 4.1 | Envío a Teams, Jira y Autotask sobre los locks existentes (D12) | 1.1, 3.3 | Sin aprobación no se envía; cambiar el texto tras la aprobación bloquea el envío; dos aprobaciones producen un solo envío; cada respuesta queda como precedente |
| 4.2 | Conectores de terceros en solo lectura (D13) | 4.1 | Ninguna escritura externa fuera de las herramientas del paso 4.1 |
| 4.3 | Candidatos de aprendizaje desde Jira, Autotask y Teams (D13) | 2.5 | Solo eventos concretos de personas con autoridad declarada; cada candidato lleva su enlace y va a confirmación |
| 4.4 | Delegación por categoría y proyecto (D12) | 3.4, 4.1 | Lo delegado se ejecuta sin preguntar; lo no delegado pregunta; la delegación se puede revocar |
| 4.5 | Insignias con referencia externa, si se ejecutan tickets externos en autónomo (D13) | 1.1 | Un ticket de Jira o Autotask se ejecuta sin duplicarse en el ERP |

### Pospuesto, con su condición de activación

| Capacidad | Se activa cuando |
|---|---|
| Lectura del vault para otros roles | Un rol lo necesita por diseño o termina `bloqueado`/`no_aplica` por falta de conocimiento |
| Conocimiento compartido de empresa | Un agente necesita conocimiento que no pertenece a ningún proyecto |
| Módulo de conocimiento del ERP | Etapa 5, o cuando entre personal que no use el vault |
| Búsqueda semántica | Se miden fallos de recuperación con la búsqueda actual |

## 8. Métricas

Se revisan en el resumen del Owner. Mientras haya pocos casos, la revisión es cualitativa.

- Porcentaje de acciones que necesitaron intervención del Owner.
- Propuestas aceptadas sin cambios.
- Preguntas repetidas sobre algo que ya tenía precedente.
- Retrabajo y tickets reabiertos.
- Errores en acciones ejecutadas sin consulta.
- Veces en que el agente preguntó cuando debía preguntar.
- Abstenciones que el Owner corrigió después.
- Correcciones del Owner que se repiten después de haberse guardado como práctica.

## 9. Referencias

- `jardal-knowledge/README.md` (§ Vault y mantenimiento del conocimiento) y skill `obsidian-vault-links` (`jardal-knowledge/.agents/skills/obsidian-vault-links/SKILL.md`, `scripts/vault_check.py`): estructura, aislamiento y checker del vault.
- `docs/ARCHITECTURE_REFERENCE_2026.md`: sandbox (00192, 00214), canal del Owner (00206), locks (00208), cristalización (00210), handoff (00211) y CEO (00119, 00213).
- `ARCHITECTURE_REFERENCE_2026-ERP.md`: principios, etapa 5 y ADR aprobados.
- How Claude remembers your project — Claude Code Docs: https://code.claude.com/docs/en/memory
- OWASP Top 10 for Agentic Applications 2026, ASI06 (Memory & Context Poisoning).
- OpenClaw — Memory architecture: https://docs.openclaw.ai/concepts/memory-architecture
- Agentic Context Engineering (ACE), arXiv 2510.04618.
