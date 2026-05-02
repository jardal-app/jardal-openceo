# Apps SDK MCP Test

Base MCP para conectar ChatGPT con tools reales de OpenCEO y una primera UI `MCP Apps` estandar para PM.

## Estado actual

- Servidor MCP `streamable-http` en `server.js`
- Endpoint publico esperado en `/mcp`
- OAuth de prueba embebido disponible cuando `MCP_TEST_AUTH_MODE` no es `none`
- `tools/list` publica `get_company_context`, `get_user_context`, `execute_workflow` y `get_task_status`
- `get_company_context` se describe como discovery al inicio del chat y solo se debe refrescar si el catalogo esta viejo, ausente o dudoso; no debe depender de listas estaticas de workflows en la descripcion
- si `get_company_context` descubre un workflow compatible para una accion real, el path ChatGPT no debe detenerse en un borrador local; debe continuar hacia faltantes, aprobacion o ejecucion
- si el workflow ya esta identificado y solo faltan selecciones o confirmaciones que el connector puede resolver, el path ChatGPT debe entrar a `execute_workflow` y dejar que ese contrato devuelva `needs_input` o MCP Apps, en vez de cerrar ese loop manualmente en puro chat
- `get_user_context` queda on-demand: no se debe llamar al inicio por defecto; solo cuando la peticion depende de proyecto activo, sesion dev, jobs en curso, pendientes o status
- `active_project` desde `get_user_context` es contexto advisory; no se usa como default silencioso para workflows de escritura sensibles por proyecto, especialmente PM batch
- `execute_workflow` es el contrato canonico de negocio y ahora tambien es el punto donde cuelga la primera UI `MCP Apps` estandar para `pm.create_tickets`
- si existe un workflow compatible con una accion real, el path ChatGPT debe seguir una regla `mcp-first`: priorizar el workflow conectado por encima de resolver la peticion solo con comportamiento local del modelo; para side effects con aprobacion, eso significa pasar a un resumen corto de aprobacion en vez de pegar el artefacto final completo
- una vez identificado el workflow, `execute_workflow` tambien es el punto correcto para dejar al connector resolver los ultimos faltantes confirmables o seleccionables
- `pm.create_tickets` es el nombre publico correcto para este flujo: registra un task canonico en Mongo por ticket con `autorun=false`, lo deja en cola manual de dev y deja la sincronizacion a Notion como implementacion secundaria del mirror humano
- ley absoluta: no subir workflows de negocio a tools MCP de primer nivel solo para resolver UI
- `execute_workflow` ya no inyecta `widget_html` inline; cuando falta input devuelve solo texto + `structuredContent`

## UI / Theme

Regla obligatoria para este app desde ahora:

- Toda UI nueva debe usar `MCP Apps` estandar, no APIs ChatGPT-only
- La UI debe heredar primero `theme`, estilos y fonts del host mediante `@modelcontextprotocol/ext-apps`
- El fallback visual debe ser dark neutral: `#212121`, `#2a2a2a`, `#303030`, `#3f3f46`, `#ececec`, `#a1a1aa`
- Fondos blancos, cards blancas, gradientes vistosos y layouts decorativos estan prohibidos
- El acento debe ser minimo y reservarse a la accion primaria
- El contraste debe priorizar comodidad visual y evitar fatiga ocular

Esto aplica tambien a pantallas auxiliares del flujo OAuth. Si mas adelante se agrega un widget, debe respetar esta regla desde la primera version.

Nota:

- `openceo-core/agents/ceo/chatgpt_system_prompt.md` funciona como referencia de comportamiento deseado para este path.
- La palanca runtime que realmente ve ChatGPT en el connector externo son los titulos, descripciones y metadatos de tools expuestos por `server.js`.

## Autenticacion

Modos disponibles:

- `none`: sin autenticacion
- `optional`: discovery OAuth publicado, listo para tools mixtos
- `required`: discovery OAuth publicado, pensado para tools protegidos

OAuth embebido incluido:

- discovery
- dynamic client registration
- PKCE `S256`
- `token_endpoint_auth_method=none`
- JWKS local
- clave de firma persistida en `MCP_TEST_DATA_DIR` para que un restart/rebuild no invalide los access tokens ya emitidos
- TTL de access token configurable con `MCP_TEST_OAUTH_ACCESS_TOKEN_TTL_SECONDS`
- auto-approve opcional para pruebas

## Requisitos reales de ChatGPT

Para conectar el conector en ChatGPT:

- La URL debe ser publica y `https://.../mcp`
- ChatGPT soporta `noauth` y `oauth2` en `securitySchemes`
- Si se usa login real, el issuer debe soportar OAuth 2.1 con PKCE `S256`, dynamic client registration y discovery metadata

No usar API key custom, bearer estatico ni `client_credentials` como login del connector.

## Uso local

```bash
cd apps-sdk-mcp-test
npm install
cp .env.example .env
node server.js
```

Servidor local esperado:

```text
http://127.0.0.1:8787/mcp
```

Inspector:

```bash
npx @modelcontextprotocol/inspector@latest --server-url http://127.0.0.1:8787/mcp --transport http
```

Smoke reproducible:

```bash
npm run smoke
SMOKE_AUTH_MODE=embedded npm run smoke
```

## Variables de entorno

### Sin autenticacion

```env
MCP_TEST_AUTH_MODE=none
```

### OAuth opcional con issuer embebido

```env
MCP_TEST_AUTH_MODE=optional
APP_PUBLIC_BASE_URL=https://tu-subdominio.ngrok.app
MCP_TEST_OAUTH_PROVIDER=embedded
MCP_TEST_OAUTH_SCOPES=connector.access
MCP_TEST_OAUTH_AUTO_APPROVE=true
```

### OAuth requerido con issuer embebido

```env
MCP_TEST_AUTH_MODE=required
APP_PUBLIC_BASE_URL=https://tu-subdominio.ngrok.app
MCP_TEST_OAUTH_PROVIDER=embedded
MCP_TEST_OAUTH_SCOPES=connector.access
MCP_TEST_OAUTH_AUTO_APPROVE=true
```

Opcionales:

- `MCP_TEST_OAUTH_PROVIDER=external`: usa un IdP externo
- `MCP_TEST_OAUTH_RESOURCE`: override del resource identifier del MCP
- `MCP_TEST_OAUTH_AUDIENCE`: audience esperado en el JWT
- `MCP_TEST_OAUTH_JWKS_URI`: override del JWKS si tu discovery no lo expone donde corresponde
- `MCP_TEST_DEMO_USER_*`: cambia el usuario del issuer embebido

## Conectar a ChatGPT

1. Expone el servidor por HTTPS.
2. Usa la URL publica con sufijo `/mcp`.
3. En ChatGPT activa `Settings -> Apps & Connectors -> Advanced settings -> Developer mode`.
4. Crea un connector nuevo en `Settings -> Connectors -> Create`.
5. Pega la URL publica del MCP.
6. Verifica que el connector conecte y que `tools/list` publique solo las tools reales del contrato canonico.

## Notas

- `/.well-known/oauth-protected-resource` se publica solo cuando el modo no es `none`.
- Con `MCP_TEST_OAUTH_PROVIDER=embedded`, el mismo servidor publica `/oauth/.well-known/*`, `/oauth/register`, `/oauth/authorize`, `/oauth/token` y `/oauth/jwks`.
- El servidor valida JWTs por `issuer` + `audience`.
- Si usas un IdP externo y discovery no publica `registration_endpoint` o `code_challenge_methods_supported: ["S256"]`, ChatGPT no va a completar el login aunque el MCP responda bien.
- La capa UI se basa en `@modelcontextprotocol/ext-apps` y queda pensada para cualquier cliente MCP que soporte `MCP Apps`, no solo ChatGPT.
- El primer caso interactivo vive dentro de `execute_workflow` para `agent=pm`, `workflow=create_tickets`; no existe una tool PM extra de primer nivel para eso. La confirmacion publica es registro canonico en Mongo + sincronizacion automatica del espejo; Notion queda como implementacion de fondo.
