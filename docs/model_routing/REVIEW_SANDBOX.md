# Aislamiento de revisores (TASK-2026-00288)

Auditoría de `orchestration/agent_sandbox.py` y de la invocación de
OpenCode/AGY frente a SPEC §14.3, más el endurecimiento entregado.
Verificado el 2026-10-23 con opencode v2.0.22, agy 1.4.6 y bwrap 0.9.0.
(BASELINE.md registró agy 1.4.4 antes el mismo día; el binario pasó a
1.4.6 durante la jornada.)

## Hallazgos de la auditoría

1. Los revisores invocaban `opencode run --agent build` (p. ej.
   `agents/dev/opencode_session_manager.py`): sin agente dedicado ni
   política de herramientas. AGY no tiene flags de denegación (`agy2`
   fuerza `--dangerously-skip-permissions`); su confinamiento es el
   fence bwrap más `--sandbox` y un MCP vacío por run.
2. El fence monta por allowlist (nunca `/` completo): lo no listado no
   existe dentro. Los `.env*` bajo raíces montadas se enmascaran, pero
   la config global `~/.config/opencode/opencode.json` (montada RO)
   embebe una clave API real, y `service.json` guarda el password del
   servicio local. Un revisor podía leerlos con `read`.
3. `OPENCODE_CONFIG_CONTENT` se honra (documento virtual último en
   `Config.load`, claves escalares ganan por `findLast`) y las
   `experimental.policies` con `action: "permission"` casan
   `"<tool>:<recurso>"` con `*` cruzando `/`: última coincidencia por
   recurso, y cualquier `deny` fuerza `deny` antes de `ask`/`allow`.
   Un `deny` explícito prevalece sobre `--auto` ("auto-approve
   permissions that are not explicitly denied") y sobre allows
   heredados. El agente `review` definido vía CONTENT resuelve
   (`--agent nope` → `Agent not found`; `--agent review` → error de
   proveedor, sin gastar tokens). La cita literal del error queda para
   la prueba viva de 00293.
4. `--agent build` + políticas es lo que ya usan los reviewers del
   bucle en producción; el endurecimiento lo convierte en módulo
   (`model_routing/reviewer_isolation.py`) y añade el agente `review`.

## Endurecimiento (`plan_reviewer`)

- Perfiles de home por CLI (`home_profile`): opencode conserva solo
  `.opencode` + `.config/opencode`; agy conserva solo su binario y su
  `~/.gemini*` propio. Sin `opencode_state_rw`: el estado es tmpfs
  efímero. El home completo (`None`) queda intacto para el resto.
- La config global y `service.json` se sustituyen por run (overlay de
  fichero + `OPENCODE_CONFIG_CONTENT` con el mismo contenido):
  `snapshots: false`, `formatter: false`, `share: disabled`,
  `websearch: false`, skills/plugins desactivados, `mcp: {}`, agente
  `review` (solo allows: `read`/`glob`/`grep`/`edit`/`write`) y
  políticas deny-all con allow de lectura, deny de secretos replicado
  en `read`/`grep`/`glob` (`*.env`, `*.ssh/*`, `auth.json`,
  `service.json`, `oauth-token`, `opencode.jsonc`, `/etc/*`) y
  `edit`/`write` solo al informe (absoluto + relativo; los relativos
  se resuelven contra el proyecto).
- La frontera vive solo en `experimental.policies`: cualquier `deny`
  en la capa del agente hace que la capa gratuita responda
  `provider.auth 403` (medido en `research/worker/profiles.py`), así
  que la entrada del agente no lleva denies.
- AGY hereda MCP vacío: overlay por run de
  `<cuenta>/config/mcp_config.json` con `{"mcpServers": {}}` (omitido
  si la cuenta no trae fichero MCP). `settings.json` no se toca: su
  esquema puede llevar auth y los approvals son irrelevantes con
  `--dangerously-skip-permissions`; 00293 confirma que no declara
  servidores.
- `~/.config/free-router` y `~/.local/state/free-router` van en tmpfs,
  incluidas las rutas XDG personalizadas cuando difieren del defecto;
  los `.env*` se resuelven (enlaces incluidos) y se enmascaran cuando
  el destino está bajo una raíz montada (fuera de ella ya cuelga).
- El overlay de entorno rechaza claves (todo `FREE_ROUTER_*`,
  `*_API_KEY`, `MCP_*`, `DSH_HOME`…); el allowlist base ya las excluye.
- El workspace es la única raíz escribible: `plan_reviewer` rechaza
  `extra_write_roots`; quien cablea monta el código por
  `extra_read_roots`. `plan_reviewer` no inyecta flags argv para que
  las sondas `--help`/`--version` sigan funcionando; el cableado real
  añade `--agent review` / `--sandbox` y conserva `--standalone`.

## Desviaciones razonadas

- HOME temporal (§14.3.2): no se adopta literal; AGY/OAuth y el estado
  efímero de opencode exigen su home. Equivalente funcional: dentro
  del fence el home solo contiene las entradas del perfil + tmpfs; el
  resto no existe. El código va RO (`extra_read_roots`) y el workspace
  es el directorio del informe: solo el informe es escribible.
- Edición acotada al informe (§14.3.1 pedía denegar edición): el
  revisor entrega su informe en fichero (`edit`/`write` solo a sus dos
  recursos), así que la denegación total impediría el entregable. 00293
  confirma el canal (fichero frente a stdout) antes de cablear.
- `*.env.example` se permite tras el deny (el fence lo deja legible
  como doc; el agente `explore` hace lo mismo).
- Sin `mcp` en reviewers: las herramientas MCP casan el deny `*`;
  OpenCode lleva `mcp: {}` y AGY el overlay vacío.
- `/etc` sigue montado RO para operar (DNS, passwd); la política
  deniega su lectura vía herramientas.

## Egress (diseño, no activado)

La red sigue compartida: los revisores deben alcanzar las APIs de
modelos. `--unshare-net` también cortaría localhost, así que no basta
añadirlo. Diseño cuando el gateway salga de HOLD: gateway en
127.0.0.1 + relay documentado (proxy de reenvío con allowlist de
destinos sobre socket unix montado en el sandbox), con prueba de que
el revisor solo alcanza el gateway. Sin gateway auditable no se
activa (SPEC §14.3.4). El endpoint local con autenticación inyectada
(§14.3.3) y el relay (§14.3.4) pertenecen a la decisión del gateway,
no a 00293.

## Verificación

- `tests/test_model_routing_review_sandbox.py`: 25 tests (políticas,
  agente, máscaras, composición, negativos con bwrap real y binario
  revisor falso, arranque fenced de `opencode run --help`,
  `opencode --version` y `agy2 --help`). Cero llamadas a modelos.
- Leído con precisión: fence verificado y política comprobada
  estructuralmente; la prueba efectiva con revisor real es de 00293.
- Riesgo residual: un revisor AGY conserva shell; sin egress
  restringido podría alcanzar endpoints locales sin badge. Lo cubre el
  diseño de egress + el cableado de TASK-2026-00293 (un slot, un
  snapshot, sin MCP).

## Relevo a TASK-2026-00293

Cablear cada slot gratuito a `plan_reviewer` (workspace = directorio
vacío del informe pre-creado, código RO por `extra_read_roots`,
`--agent review` / `--sandbox`, `--standalone` conservado) y probar
con un revisor real sobre diff sintético, con recibo de modelo
efectivo y cita literal de errores:

- Positiva: el informe se crea y se escribe con la config efectiva.
- Negativas: intento de `bash`, lectura de secreto y escritura fuera
  del informe, todo denegado.
- Si aparece el 403 de la capa gratuita, la frontera ya está solo en
  policies: revisar la entrada del agente antes de reintroducir denies.
- Confirmar el canal de informe (fichero frente a stdout), que
  `settings.json` de AGY no declara servidores, y valorar un validador
  de `reviewer_config` al estilo `assert_worker_config_is_closed`.

## Reserva externa (TASK-2026-00306, 2026-10-24)

Con el grupo A agotado, los slots gratuitos se alimentan de la lista
`native_first` del perfil `free-review` (`review_wiring.py`): cada
revisor arranca con `opencode run --model` explícito y, ante un fallo
recuperable, solo ese revisor se reinicia desde cero sobre el mismo
snapshot y prompt con el siguiente modelo elegible, cruzando al grupo
B cuando el A no tiene elegibles. La credencial del grupo B viaja por
un handoff auditado (`plan_reviewer(providers=...,
provider_secrets=...)`): la config por run lleva solo referencias
`{env:FREE_ROUTER_*}`, el valor llega únicamente al entorno del
proceso y las herramientas del revisor no pueden leerlo (sin `exec`,
`/proc/*` denegado en `read`/`grep`/`glob` junto a `*.env*`,
`*auth.json` y `service.json`, `.env.free-router.local` enmascarado
en el fence). El overlay de entorno sigue rechazando todo
`FREE_ROUTER_*`; el recibo §11.7 suma `route_type`,
`provider_id`/`model_id` efectivos y el motivo del cambio.

## Cableado (TASK-2026-00293, 2026-10-23)

- Canal confirmado: stdout (`report=None`). El canal fichero cambiaría
  el contrato de parseo de ambos motores sin que el ticket lo pida.
- Los slots OpenCode gratuitos de A/B/C/S van a `plan_reviewer` con
  `--agent review` (`review_hardened`, `orchestration/live.py`);
  `--standalone` y `--format json` se conservan. dsh/claudep/gemini/
  muse-review no cambian por orden del ticket, así que el cableado AGY
  (`--sandbox`, `settings.json`, MCP vacío en producción) queda futuro.
- Prueba viva: 1 llamada real (`opencode/nemotron-3-ultra-free`, diff
  sintético diminuto) bajo el fence, informe válido y recibo §11.7
  (`tests/test_model_routing_review_live.py`). Sin 403 de capa
  gratuita; si aparece, la rama endurecida no rota modelos ante 403
  (`ROTATE_CLASSES` en `review_wiring.py`) para no quemar modelos ante
  un problema de config.
- Negativas bajo el cableado real con binario falso: marca ausente →
  fence anterior intacto; `FREE_ROUTER_*` en el entorno → `sandbox
  refused` (`tests/test_model_routing_review_wiring.py`,
  `HardenedExecutorTests`).
- Validador de `reviewer_config` al estilo worker: pendiente, no lo
  exige la aceptación de 00293.
