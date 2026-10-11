const form = document.querySelector("#scan-form");
const authSelect = document.querySelector("#authentication");
const windowsAuthSelect = document.querySelector("#windows-authentication");
const sqlCredentialFields = document.querySelectorAll(".sql-credential-field");
const windowsCredentialFields = document.querySelectorAll(".windows-credential-field");
const advancedToggle = document.querySelector("#advanced-toggle");
const advancedFields = document.querySelector("#advanced-fields");
const emptyState = document.querySelector("#empty-state");
const loadingState = document.querySelector("#loading-state");
const errorState = document.querySelector("#error-state");
const results = document.querySelector("#results");
const scanButton = document.querySelector("#scan-button");
const snapshotInput = document.querySelector("#snapshot-file");
const snapshotLabel = document.querySelector('label[for="snapshot-file"]');
const loadingMessage = document.querySelector("#loading-message");
const toast = document.querySelector("#toast");

authSelect.addEventListener("change", () => {
  const sqlAuth = authSelect.value === "sql";
  sqlCredentialFields.forEach((field) => field.classList.toggle("is-hidden", !sqlAuth));
  document.querySelector("#username").required = sqlAuth;
  document.querySelector("#password").required = sqlAuth;
});

windowsAuthSelect.addEventListener("change", () => {
  const explicitCredential = windowsAuthSelect.value === "credentials";
  windowsCredentialFields.forEach((field) => field.classList.toggle("is-hidden", !explicitCredential));
  document.querySelector("#windows-username").required = explicitCredential;
  document.querySelector("#windows-password").required = explicitCredential;
});

advancedToggle.addEventListener("click", () => {
  const expanded = advancedToggle.getAttribute("aria-expanded") === "true";
  advancedToggle.setAttribute("aria-expanded", String(!expanded));
  advancedFields.hidden = expanded;
  advancedToggle.textContent = expanded ? "Ajustar reservas" : "Ocultar reservas";
});

document.querySelector("#new-scan").addEventListener("click", () => {
  results.hidden = true;
  emptyState.hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  document.querySelector("#server").focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  loadingMessage.textContent = "Consultando hardware e serviços do Windows…";
  setState("loading");
  const cpuValue = document.querySelector("#reserve-cpus").value;
  const payload = {
    server: document.querySelector("#server").value,
    windows_authentication: windowsAuthSelect.value,
    windows_username: windowsAuthSelect.value === "credentials" ? document.querySelector("#windows-username").value : null,
    windows_password: windowsAuthSelect.value === "credentials" ? document.querySelector("#windows-password").value : null,
    winrm_use_ssl: document.querySelector("#winrm-transport").value === "https",
    authentication: authSelect.value,
    username: authSelect.value === "sql" ? document.querySelector("#username").value : null,
    password: authSelect.value === "sql" ? document.querySelector("#password").value : null,
    reserve_memory_percent: Number(document.querySelector("#reserve-memory-percent").value),
    reserve_memory_gb: Number(document.querySelector("#reserve-memory-gb").value),
    reserve_logical_cpus: cpuValue ? Number(cpuValue) : null,
    encrypt_connection: document.querySelector("#encrypt-connection").checked,
    trust_server_certificate: document.querySelector("#trust-certificate").checked,
  };

  try {
    const response = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("; ")
        : body.detail;
      throw new Error(detail || "Falha inesperada ao analisar o servidor.");
    }
    clearPasswords();
    renderResults(body);
    setState("results");
  } catch (error) {
    clearPasswords();
    document.querySelector("#error-message").textContent = error.message;
    setState("error");
  }
});

snapshotInput.addEventListener("change", async () => {
  const file = snapshotInput.files?.[0];
  if (!file) return;

  loadingMessage.textContent = "Importando o diagnóstico coletado dentro do servidor…";
  setState("loading");
  try {
    if (file.size > 2 * 1024 * 1024) {
      throw new Error("O arquivo excede o limite de 2 MB e não parece ser um diagnóstico válido.");
    }
    const text = (await file.text()).replace(/^\uFEFF/, "");
    const snapshot = JSON.parse(text);
    const cpuValue = document.querySelector("#reserve-cpus").value;
    const response = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        snapshot,
        reserve_memory_percent: Number(document.querySelector("#reserve-memory-percent").value),
        reserve_memory_gb: Number(document.querySelector("#reserve-memory-gb").value),
        reserve_logical_cpus: cpuValue ? Number(cpuValue) : null,
      }),
    });
    const body = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("; ")
        : body.detail;
      throw new Error(detail || "O arquivo de diagnóstico não pôde ser importado.");
    }
    renderResults(body);
    setState("results");
    showToast("Diagnóstico importado com sucesso.");
  } catch (error) {
    const message = error instanceof SyntaxError
      ? "O arquivo selecionado não contém um JSON válido."
      : error.message;
    document.querySelector("#error-message").textContent = message;
    setState("error");
  } finally {
    snapshotInput.value = "";
  }
});

function clearPasswords() {
  document.querySelector("#password").value = "";
  document.querySelector("#windows-password").value = "";
}

function setState(state) {
  emptyState.hidden = state !== "empty";
  loadingState.hidden = state !== "loading";
  errorState.hidden = state !== "error";
  results.hidden = state !== "results";
  const loading = state === "loading";
  scanButton.disabled = loading;
  snapshotInput.disabled = loading;
  snapshotLabel.setAttribute("aria-disabled", String(loading));
  scanButton.classList.toggle("is-loading", loading);
  scanButton.querySelector(".button-label").textContent = loading ? "Analisando…" : "Analisar servidor";
}

function renderResults(data) {
  const host = data.host;
  document.querySelector("#host-title").textContent = host.computer_name;
  document.querySelector("#host-meta").textContent = `${host.os_caption} · versão ${host.os_version}`;
  const metrics = [
    ["Memória física", formatMb(host.total_memory_mb), `${formatMb(host.free_memory_mb)} livres agora`],
    ["Reserva Windows", formatMb(host.reserved_memory_mb), `${formatMb(host.observed_non_sql_memory_mb)} não-SQL em uso`],
    ["Orçamento SQL", formatMb(host.sql_memory_budget_mb), "todas as instâncias"],
    ["CPUs lógicas", String(host.logical_cpus), "detectadas no host"],
    ["Reserva de CPU", String(host.reserved_logical_cpus), "fora do dimensionamento"],
    ["Parcela SQL", String(host.sql_cpu_budget), "base para MAXDOP"],
  ];
  document.querySelector("#host-metrics").innerHTML = metrics.map(([label, value, note]) => `
    <div class="metric"><div class="metric-label">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(value)}</div><small>${escapeHtml(note)}</small></div>
  `).join("");

  const warnings = document.querySelector("#warnings");
  warnings.hidden = !data.warnings.length;
  warnings.innerHTML = data.warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("");
  const recommendationCount = data.instances.reduce((total, instance) => total + instance.recommendations.length, 0);
  const actionCount = data.instances.reduce(
    (total, instance) => total + instance.recommendations.filter((item) => item.change_required).length,
    0,
  );
  document.querySelector("#instance-count").textContent = `${data.instances.length} instância${data.instances.length === 1 ? "" : "s"} · ${recommendationCount} verificaç${recommendationCount === 1 ? "ão" : "ões"} · ${actionCount} aç${actionCount === 1 ? "ão" : "ões"}`;
  document.querySelector("#instances").innerHTML = data.instances.length
    ? data.instances.map(renderInstance).join("")
    : `<div class="empty-state"><h2>Nenhuma instância encontrada</h2><p>Confira se os serviços e as chaves de Registro do SQL Server estão presentes no servidor informado.</p></div>`;
  document.querySelector("#methodology-list").innerHTML = data.methodology.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderInstance(instance, index) {
  const running = instance.service_state.toLowerCase() === "running";
  const version = [instance.edition, instance.version, instance.product_level].filter(Boolean).join(" · ");
  const rows = instance.recommendations.map((item) => {
    const reference = typeof item.reference === "string" && item.reference.startsWith("https://learn.microsoft.com/")
      ? `<a class="recommendation-reference" href="${escapeHtml(item.reference)}" target="_blank" rel="noopener noreferrer">Microsoft Learn</a>`
      : "";
    return `
    <tr class="${item.change_required ? "requires-action" : "is-compliant"}">
      <td data-label="Configuração"><div class="recommendation-name"><i class="priority ${item.priority}"></i><span><small>${escapeHtml(item.category || "Instância")}</small>${escapeHtml(item.label)}${reference}</span></div></td>
      <td data-label="Atual" class="current-value">${escapeHtml(item.current)}</td>
      <td data-label="Recomendado" class="recommended-value">${escapeHtml(item.recommended)}</td>
      <td data-label="Por quê" class="rationale">${escapeHtml(item.rationale)}</td>
    </tr>`;
  }).join("");
  const table = rows ? `
    <table class="recommendation-table">
      <thead><tr><th>Configuração</th><th>Atual</th><th>Recomendado</th><th>Por quê</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>` : "";
  const error = instance.connection_error ? `<p class="connection-error">${escapeHtml(instance.connection_error)}</p>` : "";
  const notes = instance.notes.length ? `<ul class="instance-notes">${instance.notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>` : "<span></span>";
  const button = instance.script ? `<button type="button" class="script-button" data-script-index="${index}">Copiar script T-SQL</button>` : "";
  window.__advisorScripts = window.__advisorScripts || {};
  if (instance.script) window.__advisorScripts[index] = instance.script;
  return `
    <article class="instance-card">
      <header class="instance-header">
        <div class="instance-title"><span class="instance-icon">DB</span><div><h3>${escapeHtml(instance.instance_name)}</h3><p>${escapeHtml(instance.endpoint)}${version ? ` · ${escapeHtml(version)}` : ""}</p></div></div>
        <span class="service-badge ${running ? "" : "offline"}">${escapeHtml(instance.service_state)}</span>
      </header>
      ${error}${table}
      <div class="instance-footer">${notes}${button}</div>
    </article>`;
}

document.querySelector("#instances").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-script-index]");
  if (!button) return;
  const script = window.__advisorScripts?.[button.dataset.scriptIndex];
  if (!script) return;
  try {
    await navigator.clipboard.writeText(script);
    showToast("Script copiado. Revise e teste antes de aplicar.");
  } catch {
    showToast("O navegador bloqueou a cópia. Tente novamente.");
  }
});

function formatMb(value) {
  if (value >= 1024) return `${(value / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} GB`;
  return `${Number(value).toLocaleString("pt-BR")} MB`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.setTimeout(() => toast.classList.remove("is-visible"), 2800);
}
