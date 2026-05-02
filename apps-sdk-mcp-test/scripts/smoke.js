import { createHash, randomBytes } from "node:crypto";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const origin = process.env.SMOKE_ORIGIN ?? "http://127.0.0.1:8787";
const mcpUrl = new URL(process.env.SMOKE_MCP_URL ?? `${origin}/mcp`);
const mode = (process.env.SMOKE_AUTH_MODE ?? "none").trim().toLowerCase();

if (!["none", "embedded"].includes(mode)) {
  throw new Error(`SMOKE_AUTH_MODE no soportado: ${mode}`);
}

const result = mode === "none" ? await runNoAuthSmoke() : await runEmbeddedAuthSmoke();
console.log(JSON.stringify(result, null, 2));

async function runNoAuthSmoke() {
  const client = new Client({ name: "smoke-client", version: "0.1.0" });
  const transport = new StreamableHTTPClientTransport(mcpUrl);
  await client.connect(transport);

  const tools = await client.listTools();

  await client.close();
  return {
    mode,
    tools: tools.tools.map((tool) => tool.name),
    tool_count: tools.tools.length,
  };
}

async function runEmbeddedAuthSmoke() {
  const protectedResource = await fetchJson(`${origin}/.well-known/oauth-protected-resource`);
  const metadata = await fetchJson(`${origin}/oauth/.well-known/openid-configuration`);

  const redirectUri = "https://chatgpt.com/connector/oauth/test";
  const registration = await fetchJson(`${origin}/oauth/register`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      client_name: "Smoke ChatGPT Connector",
      redirect_uris: [redirectUri],
    }),
  });

  const scope = Array.isArray(metadata.scopes_supported) && metadata.scopes_supported.length
    ? metadata.scopes_supported.join(" ")
    : "connector.access";
  const codeVerifier = randomBytes(32).toString("base64url");
  const codeChallenge = createHash("sha256").update(codeVerifier).digest("base64url");

  const authorizeUrl = new URL(`${origin}/oauth/authorize`);
  authorizeUrl.searchParams.set("response_type", "code");
  authorizeUrl.searchParams.set("client_id", registration.client_id);
  authorizeUrl.searchParams.set("redirect_uri", redirectUri);
  authorizeUrl.searchParams.set("scope", scope);
  authorizeUrl.searchParams.set("state", "smoke-state");
  authorizeUrl.searchParams.set("resource", protectedResource.resource);
  authorizeUrl.searchParams.set("code_challenge", codeChallenge);
  authorizeUrl.searchParams.set("code_challenge_method", "S256");

  const authorizeResponse = await fetch(authorizeUrl, { redirect: "manual" });
  const redirected = new URL(authorizeResponse.headers.get("location"));
  const code = redirected.searchParams.get("code");
  if (!code) {
    throw new Error("No llego authorization code en el redirect.");
  }

  const token = await fetchJson(`${origin}/oauth/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: registration.client_id,
      code,
      redirect_uri: redirectUri,
      code_verifier: codeVerifier,
      resource: protectedResource.resource,
    }),
  });

  const client = new Client({ name: "smoke-client", version: "0.1.0" });
  const transport = new StreamableHTTPClientTransport(mcpUrl, {
    requestInit: {
      headers: {
        Authorization: `Bearer ${token.access_token}`,
      },
    },
  });
  await client.connect(transport);

  const tools = await client.listTools();

  await client.close();
  return {
    mode,
    resource: protectedResource.resource,
    issuer: metadata.issuer,
    registration: {
      client_id: registration.client_id,
      redirect_uris: registration.redirect_uris,
      scope: registration.scope,
    },
    token: {
      token_type: token.token_type,
      scope: token.scope,
      access_token_len: token.access_token?.length ?? 0,
    },
    tools: tools.tools.map((tool) => tool.name),
    tool_count: tools.tools.length,
  };
}

async function fetchJson(url, init) {
  const response = await fetch(url, init);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`${url} -> HTTP ${response.status}: ${text}`);
  }
  return text ? JSON.parse(text) : {};
}
