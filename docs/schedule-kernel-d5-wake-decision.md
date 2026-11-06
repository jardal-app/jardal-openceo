# D5: vía de wake para escritores fuera de proceso (ticket #574)

> TEMPORAL. El ticket ARCH lo transcribe a `docs/ARCHITECTURE_REFERENCE_2026.md` y lo borra.
> Cierra D5 (§8 de `docs/TASK-2026-00441-schedule-kernel-discovery.md`). Hechos leídos el 2026-11-06.

## Decisión

**(a), acotada**: ruta autenticada del propio core que llama a `kernel.wake("ceo")`.
(b) se descarta: sin el tope de 3600 s la latencia deja de estar acotada (hecho 4).
(c) sigue descartada (§8): `db/mongo.py:49` abre un `AsyncIOMotorClient` plano, sin replica set ni `watch()`.

Contrato para el seguimiento de cableado del CEO:
1. El escritor persiste primero (`save_ceo_trigger`) y avisa después: Mongo es la verdad, el wake es una pista.
2. El wake es idempotente y mejor esfuerzo: `kernel.wake` es síncrono, coalescido y no lanza (`kernel.py:144-155`).
3. Con el core caído no se pierde nada: `start()` marca sucia toda agenda y relee `next_due` (`kernel.py:165-173`).
4. Con el core vivo, un wake fallido deja el cambio a la espera del siguiente wake en proceso: el escritor
   informa `woken: false` en su reporte en vez de callar.

## Hechos verificados

1. **Escritores de `ceo_review_trigger`** (grep sobre todo el repo; no hay acceso crudo a la colección fuera de `db/mongo.py`):
   - Primitiva: `MongoRepository.save_ceo_trigger` (`db/mongo.py:865`, `$set` con upsert sobre `_id="trigger"`).
   - En proceso (`CEOLoop`): `ceo/loop.py:212` (ensure), `:319` (debounce), `:382` (scheduled), `:397` (reactive).
   - Fuera de proceso: **uno**, `ops/resume_pedido.py:96` (`next_scheduled_at = now`, `:84`).
   - Dobles de test: `tests/test_ceo_loop.py:44,295`, `tests/test_resume_pedido_349.py:80`. Ninguno en skills, scripts, workflows ni yaml/md.
2. **`resume_pedido` contra el servicio vivo**: sin llamador de producción (su único importador es su test) y
   `.claude/skills/` solo trae `notion-api-local`. El docstring (`resume_pedido.py:13-15`) lo deja como paso de
   sesión de cierre de TASK-2026-00349 (merge `ca41e37`): una sesión con su propio `MongoRepository` sobre
   `MONGODB_URL`, no una ruta del core. Si se ejecutó en vivo y con qué procedimiento no consta en código
   versionado; hay rastro en `.openceo-automatico/`, sin abrir por el presupuesto de ficheros.
3. **Latencia hoy**: ≤3600 s, porque cada ciclo relee el trigger (`loop.py:53,262-272,274-288`).
4. **Latencia sin tope**: el kernel solo relee `next_due` con la agenda sucia (wake en proceso, fin de carril,
   arranque: `kernel.py:153,244,364,166`) y se construye sin `safety_net_seconds` (`main.py:623-626`; nada más
   lo usa en el core). Un avance externo esperaría al vencimiento previo: 1 d a 28 d, 7 d por defecto
   (`loop.py:32-42`). Sin wake no se cumple el caso de Fase 4 «`resume_pedido` surte efecto sin esperar 1 h».

## Superficie autenticada para (a)

Hoy **no existe** ruta ni herramienta que llame a `kernel.wake` desde fuera: el único asa es
`app.state.schedule_kernel` (`main.py:641`) y todos los `wake` viven en el arranque y en los dominios.

- **Recomendada**: ruta nueva en `openceo-core/main.py` con `Depends(require_admin())`
  (`auth/middleware.py:145`; cookie o Bearer, `:42-126`). Cuerpo: nombre de agenda; `kernel.wake` ya
  registra y descarta los desconocidos.
- Descartadas: MCP interno `mcp_server.py` (`_InternalOnlyBearerWrapper`, `:1193-1259`: solo loopback, sin
  credencial pasa a la caja por defecto `:1229-1233`, Bearer legado opcional `:1248`; herramientas = lista
  cerrada de frontera de ticket, `:123`); `verify_api_key` (`main.py:869`: solo `/v1/*` y abierto con
  `OPENCEO_API_KEY` vacío); secreto de webhook ERP (`require_erp_webhook`: solo ERP→core).
- **Precondición, no comprobada** (es config desplegada): con identidad ERP, la vía de cookie exige
  `auth_provider == "jardal_erp"` (`middleware.py:81-83`) y `require_admin` exige `"local"` (`:150`); ningún
  usuario cumple ambas. La vía Bearer ERP (`_revalidate_federated_user`) no se leyó. Si el despliegue es ERP,
  el seguimiento necesita otra credencial y esa elección es del Owner.

## Fuera de alcance

Ruta, agenda CEO y cliente de `resume_pedido`: seguimiento de cableado del CEO. No se corrió la suite ni modelos.
