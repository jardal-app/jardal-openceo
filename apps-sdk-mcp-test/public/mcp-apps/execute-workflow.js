import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "../vendor/mcp-apps-app-with-deps.js";

const runtime = window.__OPENCEO_EXECUTE_WORKFLOW_UI__ || {};
const root = document.getElementById("app");

const state = {
  hostContext: null,
  payload: null,
  rawToolResult: null,
  toolInput: null,
  selectedProject: "",
  submitPending: false,
  submitDone: false,
  submitResult: null,
  status: "Conectando UI MCP Apps...",
  error: "",
};

const app = new App(
  { name: "openceo-execute-workflow-ui", version: "0.1.0" },
  { availableDisplayModes: ["inline", "fullscreen"] }
);

app.ontoolinput = ({ arguments: input } = {}) => {
  state.toolInput = input || {};
  render();
};

app.ontoolresult = (result = {}) => {
  const payload = result.structuredContent && typeof result.structuredContent === "object"
    ? result.structuredContent
    : {};
  state.rawToolResult = result;
  state.payload = payload;
  state.submitResult = isPmUiPayload(payload) ? state.submitResult : null;
  const options = getProjectOptions(payload);
  const preferredProject = readText(payload?.project?.selected);
  if (preferredProject) {
    state.selectedProject = preferredProject;
  } else if (!state.selectedProject && options.length) {
    state.selectedProject = options[0].value;
  }
  state.status = "";
  state.error = result.isError ? extractSummaryText(result, payload, "El host marco el resultado como error.") : "";
  render();
};

app.onhostcontextchanged = () => {
  syncHostStyles();
  render();
};

void bootstrap();

async function bootstrap() {
  try {
    await app.connect();
    syncHostStyles();
    state.status = "";
  } catch (error) {
    state.error = toMessage(error, "No pude inicializar la app MCP.");
  }
  render();
}

function syncHostStyles() {
  const hostContext = app.getHostContext ? app.getHostContext() : null;
  state.hostContext = hostContext;
  if (!hostContext || typeof hostContext !== "object") {
    return;
  }
  if (hostContext.theme) {
    applyDocumentTheme(hostContext.theme);
  }
  if (hostContext.styles && hostContext.styles.variables) {
    applyHostStyleVariables(hostContext.styles.variables);
  }
  if (hostContext.styles && hostContext.styles.css && hostContext.styles.css.fonts) {
    applyHostFonts(hostContext.styles.css.fonts);
  }
}

function readText(value) {
  return typeof value === "string" ? value.trim() : "";
}

function toMessage(error, fallback) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error.trim();
  }
  return fallback;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isPmUiPayload(payload) {
  return payload && payload.kind === "execute_workflow_pm_create_tickets_ui";
}

function getProjectOptions(payload) {
  return Array.isArray(payload?.project?.options)
    ? payload.project.options
      .map((option) => ({
        value: readText(option?.value),
        label: readText(option?.label || option?.value),
      }))
      .filter((option) => option.value)
    : [];
}

function supportsFullscreen() {
  const available = Array.isArray(state.hostContext?.availableDisplayModes)
    ? state.hostContext.availableDisplayModes
    : [];
  return available.includes("fullscreen");
}

function extractSummaryText(result, payload, fallback) {
  const contentText = Array.isArray(result?.content) && typeof result.content[0]?.text === "string"
    ? result.content[0].text.trim()
    : "";
  return readText(payload?.message) || contentText || fallback;
}

function renderTicketPreview(items) {
  return items.map((item) => {
    const meta = [];
    if (readText(item.priority)) {
      meta.push(item.priority);
    }
    if (readText(item.ticket_type)) {
      meta.push(item.ticket_type);
    }
    return ""
      + "<li class=\"ticket-item\">"
      + `  <div class="ticket-title">${escapeHtml(item.title || "Ticket")}</div>`
      + `  <div class="ticket-meta">${escapeHtml(meta.join(" · ") || "sin metadatos extra")}</div>`
      + "</li>";
  }).join("");
}

function renderNotes(items) {
  return items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function renderResult(result) {
  if (!result) {
    return "";
  }
  const kind = result.ok === false ? "warning" : "success";
  return ""
    + `<section class="panel result-panel" data-kind="${kind}">`
    + "  <div class=\"section-title\">Resultado</div>"
    + `  <div class="result-copy">${escapeHtml(readText(result.message) || "Operacion completada.")}</div>`
    + "  <div class=\"stats-row\">"
    + `    <div class="stat"><span>creados</span><strong>${escapeHtml(result.created ?? 0)}</strong></div>`
    + `    <div class="stat"><span>fallidos</span><strong>${escapeHtml(result.failed ?? 0)}</strong></div>`
    + `    <div class="stat"><span>source_id</span><strong>${escapeHtml(readText(result.source_id) || "n/a")}</strong></div>`
    + "  </div>"
    + "</section>";
}

async function requestFullscreen() {
  if (!supportsFullscreen()) {
    return;
  }
  try {
    await app.requestDisplayMode({ mode: "fullscreen" });
  } catch (error) {
    state.error = toMessage(error, "No pude pedir fullscreen.");
    render();
  }
}

async function submitBatch() {
  if (state.submitDone || state.submitPending) {
    return;
  }
  const payload = state.payload || {};
  const pending = payload.pending_workflow || {};
  const params = pending.params || {};
  const project = readText(state.selectedProject);
  if (!project) {
    state.error = "Selecciona un proyecto antes de crear tickets.";
    render();
    return;
  }
  if (!runtime.executeToolName) {
    state.error = "Falta executeToolName en la configuracion del recurso.";
    render();
    return;
  }

  state.submitPending = true;
  state.error = "";
  render();

  try {
    const response = await app.callServerTool({
      name: runtime.executeToolName,
      arguments: {
        agent: readText(pending.agent) || "pm",
        workflow: readText(pending.workflow) || "create_tickets",
        user_id: readText(pending.user_id),
        params: {
          ...params,
          project,
          _ui_commit: true,
        },
      },
    });
    const result = response && typeof response.structuredContent === "object"
      ? response.structuredContent
      : {};
    state.rawToolResult = response;
    state.submitResult = result;
    if (response?.isError || result?.ok === false) {
      state.error = extractSummaryText(response, result, "No pude crear el batch de tickets.");
    } else {
      state.status = extractSummaryText(response, result, "Batch creado.");
      state.submitDone = true;
      await postSuccessUpdates(result);
    }
  } catch (error) {
    state.error = toMessage(error, "No pude crear el batch de tickets.");
  } finally {
    state.submitPending = false;
    render();
  }
}

async function postSuccessUpdates(result) {
  const hostCapabilities = app.getHostCapabilities ? app.getHostCapabilities() : {};
  const message = readText(result?.message);

  if (hostCapabilities?.updateModelContext) {
    try {
      await app.updateModelContext({
        structuredContent: {
          execute_workflow_result: result,
        },
      });
    } catch {
      // Best-effort only.
    }
  }

  if (message && hostCapabilities?.message) {
    try {
      await app.sendMessage({
        content: [
          {
            type: "text",
            text: message,
          },
        ],
      });
    } catch {
      // Best-effort only.
    }
  }
}

function bind() {
  const projectField = root.querySelector("#project-select");
  if (projectField) {
    projectField.addEventListener("change", (event) => {
      state.selectedProject = readText(event.currentTarget.value);
    });
  }

  const fullscreenButton = root.querySelector("#fullscreen-btn");
  if (fullscreenButton) {
    fullscreenButton.addEventListener("click", () => {
      void requestFullscreen();
    });
  }

  const submitButton = root.querySelector("#submit-btn");
  if (submitButton) {
    submitButton.addEventListener("click", () => {
      void submitBatch();
    });
  }
}

function renderPmUi(payload) {
  const projectOptions = getProjectOptions(payload);
  const summary = payload.ticket_summary || {};
  const preview = Array.isArray(summary.preview) ? summary.preview : [];
  const notes = Array.isArray(payload.notes) ? payload.notes : [];
  const activeProject = readText(payload?.user_context?.active_project);
  const selectedProject = readText(state.selectedProject);
  const fallbackText = readText(payload.fallback_text);

  return ""
    + "<div class=\"shell\">"
    + "  <section class=\"panel hero\">"
    + `    <h1 class="title">${escapeHtml(readText(payload.title) || "PM Batch Tickets")}</h1>`
    + `    <div class="subtitle">${escapeHtml(readText(payload.subtitle) || "UI de confirmacion para tickets PM.")}</div>`
    + "    <div class=\"meta-row\">"
    + `      <div class="badge">${escapeHtml(summary.count ?? 0)} tickets</div>`
    + `      <div class="badge">${escapeHtml(activeProject || "sin proyecto activo")}</div>`
    + "      <div class=\"badge\">workflow-first</div>"
    + "    </div>"
    + (state.status ? `    <div class="status">${escapeHtml(state.status)}</div>` : "")
    + (state.error ? `    <div class="error">${escapeHtml(state.error)}</div>` : "")
    + "  </section>"
    + "  <section class=\"panel\">"
    + "    <div class=\"section-title\">Proyecto</div>"
    + "    <div class=\"field\">"
    + "      <label for=\"project-select\">Selecciona el proyecto destino</label>"
    + "      <select id=\"project-select\">"
    + projectOptions.map((option) => {
      const selected = option.value === selectedProject ? " selected" : "";
      return `<option value="${escapeHtml(option.value)}"${selected}>${escapeHtml(option.label)}</option>`;
    }).join("")
    + "      </select>"
    + "    </div>"
    + `    <div class="section-copy">Proyecto activo del usuario: ${escapeHtml(activeProject || "ninguno")}</div>`
    + (fallbackText ? `    <div class="section-copy">${escapeHtml(fallbackText)}</div>` : "")
    + "  </section>"
    + "  <section class=\"panel\">"
    + "    <div class=\"section-title\">Batch a crear</div>"
    + `    <div class="section-copy">source_id: ${escapeHtml(readText(payload?.pending_workflow?.params?.source_id) || "n/a")}</div>`
    + "    <ul class=\"ticket-list\">"
    + renderTicketPreview(preview)
    + "    </ul>"
    + (summary.remaining_count ? `    <div class="section-copy">+${escapeHtml(summary.remaining_count)} tickets adicionales no mostrados.</div>` : "")
    + (notes.length ? `    <ul class="list">${renderNotes(notes)}</ul>` : "")
    + "  </section>"
    + "  <section class=\"panel\">"
    + "    <div class=\"button-row\">"
    + `      <button id="fullscreen-btn" class="secondary"${supportsFullscreen() ? "" : " disabled"}>Pantalla completa</button>`
    + `      <button id="submit-btn" class="primary"${state.submitPending || state.submitDone ? " disabled" : ""}>${state.submitDone ? "Tickets creados" : state.submitPending ? "Creando..." : "Crear tickets"}</button>`
    + "    </div>"
    + "  </section>"
    + renderResult(state.submitResult)
    + "</div>";
}

function renderGenericFallback(payload, result) {
  const agent = readText(state.toolInput?.agent);
  const workflow = readText(state.toolInput?.workflow);
  const summary = extractSummaryText(result, payload, "Este workflow no expone una UI interactiva.");
  const status = readText(payload?.status);
  const missingParam = readText(payload?.missing_param);
  const title = agent && workflow ? `${agent}.${workflow}` : "execute_workflow";

  return ""
    + "<div class=\"shell\">"
    + "  <section class=\"panel hero\">"
    + `    <h1 class="title">${escapeHtml(title)}</h1>`
    + "    <div class=\"subtitle\">La capa de presentacion MCP Apps solo se activa para algunos casos puntuales del workflow. Este resultado sigue viniendo del contrato canonico `execute_workflow`.</div>"
    + "    <div class=\"meta-row\">"
    + `      <div class="badge">${escapeHtml(status || "sin estado")}</div>`
    + "      <div class=\"badge\">fallback</div>"
    + "    </div>"
    + (state.error ? `    <div class="error">${escapeHtml(state.error)}</div>` : "")
    + "  </section>"
    + "  <section class=\"panel\">"
    + "    <div class=\"section-title\">Resumen</div>"
    + `    <div class="section-copy">${escapeHtml(summary)}</div>`
    + (missingParam ? `    <div class="section-copy">Falta: ${escapeHtml(missingParam)}</div>` : "")
    + "  </section>"
    + "  <section class=\"panel\">"
    + "    <div class=\"button-row\">"
    + `      <button id="fullscreen-btn" class="secondary"${supportsFullscreen() ? "" : " disabled"}>Pantalla completa</button>`
    + "    </div>"
    + "  </section>"
    + "</div>";
}

function render() {
  const payload = state.payload || {};
  const result = state.rawToolResult || {};

  root.innerHTML = ""
    + "<style>"
    + "  :root {"
    + "    color-scheme: dark;"
    + "    --oc-bg: var(--color-background-primary, #212121);"
    + "    --oc-panel: var(--color-background-secondary, #2a2a2a);"
    + "    --oc-panel-strong: var(--color-background-tertiary, #303030);"
    + "    --oc-border: var(--color-border-primary, #3f3f46);"
    + "    --oc-text: var(--color-text-primary, #ececec);"
    + "    --oc-text-muted: var(--color-text-secondary, #a1a1aa);"
    + "    --oc-primary: var(--color-background-info, #10a37f);"
    + "    --oc-primary-text: var(--color-text-inverse, #0f172a);"
    + "    --oc-danger: var(--color-text-danger, #f87171);"
    + "    --oc-success: var(--color-text-success, #4ade80);"
    + "    --oc-radius: var(--border-radius-md, 12px);"
    + "    --oc-shadow: var(--shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.35));"
    + "  }"
    + "  * { box-sizing: border-box; }"
    + "  body { background: var(--oc-bg); color: var(--oc-text); }"
    + "  .shell { max-width: 760px; margin: 0 auto; padding: 16px; display: grid; gap: 12px; }"
    + "  .panel { background: var(--oc-panel); border: 1px solid var(--oc-border); border-radius: var(--oc-radius); padding: 16px; box-shadow: var(--oc-shadow); }"
    + "  .panel.hero { background: var(--oc-panel-strong); }"
    + "  .title { margin: 0; font-size: 24px; line-height: 1.15; letter-spacing: -0.02em; }"
    + "  .subtitle, .muted, .ticket-meta, .section-copy, .result-copy { color: var(--oc-text-muted); }"
    + "  .subtitle { margin-top: 6px; line-height: 1.5; }"
    + "  .meta-row, .button-row, .stats-row { display: flex; flex-wrap: wrap; gap: 8px; }"
    + "  .badge { display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--oc-border); background: rgba(255, 255, 255, 0.03); font-size: 12px; }"
    + "  .section-title { font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--oc-text-muted); margin-bottom: 10px; }"
    + "  .list { margin: 0; padding-left: 18px; display: grid; gap: 6px; }"
    + "  .ticket-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }"
    + "  .ticket-item { padding: 10px 12px; border-radius: 10px; border: 1px solid var(--oc-border); background: rgba(255, 255, 255, 0.02); }"
    + "  .ticket-title { font-weight: 600; margin-bottom: 4px; }"
    + "  .field { display: grid; gap: 8px; }"
    + "  label { font-size: 13px; color: var(--oc-text-muted); }"
    + "  select { width: 100%; padding: 11px 12px; border-radius: 10px; border: 1px solid var(--oc-border); background: #171717; color: var(--oc-text); font: inherit; }"
    + "  button { font: inherit; border-radius: 10px; padding: 11px 14px; border: 1px solid var(--oc-border); cursor: pointer; }"
    + "  button.primary { background: var(--oc-primary); color: var(--oc-primary-text); border-color: transparent; font-weight: 600; }"
    + "  button.secondary { background: transparent; color: var(--oc-text); }"
    + "  button:disabled { opacity: 0.55; cursor: not-allowed; }"
    + "  .stats-row { gap: 10px; }"
    + "  .stat { min-width: 120px; padding: 12px; border-radius: 10px; border: 1px solid var(--oc-border); background: rgba(255, 255, 255, 0.02); }"
    + "  .stat span { display: block; font-size: 12px; color: var(--oc-text-muted); margin-bottom: 8px; }"
    + "  .stat strong { font-size: 18px; }"
    + "  .result-panel[data-kind='success'] { border-color: rgba(16, 163, 127, 0.28); }"
    + "  .result-panel[data-kind='warning'] { border-color: rgba(245, 158, 11, 0.28); }"
    + "  .status { color: var(--oc-success); }"
    + "  .error { color: var(--oc-danger); }"
    + "  @media (max-width: 720px) { .shell { padding: 12px; } }"
    + "</style>"
    + (isPmUiPayload(payload) ? renderPmUi(payload) : renderGenericFallback(payload, result));

  bind();
}
