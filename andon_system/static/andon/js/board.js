const alertsUrl = "/api/andon/alerts?status=active";
const operatorMetadataUrl = "/api/andon/operator-metadata";

const openAlertsList = document.getElementById("openAlertsList");
const workingAlertsList = document.getElementById("workingAlertsList");
const boardStatusDock = document.getElementById("boardStatusDock");
const csrfHeaders = (headers = {}) => window.AndonSecurity?.withCsrfHeaders(headers) || headers;

const state = {
  alerts: [],
  users: [],
  loadedAt: Date.now(),
};

let timerIntervalId = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatElapsedDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function safeDateToMs(value) {
  const time = new Date(value || 0).getTime();
  return Number.isFinite(time) ? time : 0;
}

function elapsedSince(value) {
  const start = safeDateToMs(value);
  if (!start) return 0;
  return Math.max(0, Math.floor((Date.now() - start) / 1000));
}

function getSection(alert) {
  const status = String(alert?.status || "").toUpperCase();
  if (status === "OPEN") return "open";
  if (status === "ACKNOWLEDGED" || status === "ARRIVED") return "working";
  return "closed";
}

function userOptions(selectedUserId) {
  const options = ['<option value="">Select responder</option>'];
  for (const user of state.users) {
    const selected = Number(selectedUserId) === Number(user.id) ? " selected" : "";
    options.push(`<option value="${user.id}"${selected}>${escapeHtml(user.display_name || user.username || `User ${user.id}`)}</option>`);
  }
  return options.join("");
}

function renderOpenCard(alert) {
  const machineName = alert.machine?.name || "Machine";
  const issueName = alert.issue_problem?.name || alert.issue_category?.name || "Unassigned";
  const departmentName = alert.department?.name || "Unassigned";
  const elapsed = elapsedSince(alert.created_at);
  return `
    <article class="management-machine-card" data-alert-id="${alert.id}">
      <div class="management-machine-card__hero management-machine-card__hero--status-open">
        <div class="management-machine-card__title-row">
          <div class="management-machine-card__title">${escapeHtml(machineName)}</div>
        </div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-text">${escapeHtml(departmentName)}</span>
        </div>
      </div>
      <div class="management-machine-card__section management-machine-card__section--status">
        <div class="management-machine-card__body">
          <div class="management-machine-card__state-row management-machine-card__state-row--two">
            <div class="management-machine-card__state management-machine-card__state--issue-open">
              <div class="management-machine-card__state-label">Issue</div>
              <div class="management-machine-card__state-value">${escapeHtml(issueName)}</div>
            </div>
            <div class="management-machine-card__state management-machine-card__state--timer-open">
              <div class="management-machine-card__state-label">Elapsed</div>
              <div class="management-machine-card__state-value" data-live-seconds="${elapsed}" data-live-type="open">${formatElapsedDuration(elapsed)}</div>
            </div>
          </div>
          <div class="mt-2">
            <select class="form-select form-select-sm" data-field="responder_user_id">
              ${userOptions(alert.responder_user_id)}
            </select>
          </div>
          <div class="mt-2">
            <input class="form-control form-control-sm" data-field="note" maxlength="255" placeholder="Optional note" value="${escapeHtml(alert.note || "")}">
          </div>
          <div class="mt-2 d-flex gap-2">
            <button class="btn btn-warning btn-sm flex-grow-1" type="button" data-action="acknowledge">Acknowledge</button>
          </div>
        </div>
      </div>
    </article>`;
}

function renderWorkingCard(alert) {
  const machineName = alert.machine?.name || "Machine";
  const issueName = alert.issue_problem?.name || alert.issue_category?.name || "Unassigned";
  const responder = alert.responder_name_text || "No responder assigned";
  const startedAt = alert.acknowledged_at || alert.created_at;
  const elapsed = elapsedSince(startedAt);
  return `
    <article class="management-machine-card" data-alert-id="${alert.id}">
      <div class="management-machine-card__hero management-machine-card__hero--status-acknowledged">
        <div class="management-machine-card__title-row">
          <div class="management-machine-card__title">${escapeHtml(machineName)}</div>
        </div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-text">Working</span>
        </div>
      </div>
      <div class="management-machine-card__section management-machine-card__section--status">
        <div class="management-machine-card__body">
          <div class="management-machine-card__state-row management-machine-card__state-row--two">
            <div class="management-machine-card__state management-machine-card__state--issue-working">
              <div class="management-machine-card__state-label">Issue</div>
              <div class="management-machine-card__state-value">${escapeHtml(issueName)}</div>
            </div>
            <div class="management-machine-card__state management-machine-card__state--responder">
              <div class="management-machine-card__state-label">Responder</div>
              <div class="management-machine-card__state-value">${escapeHtml(responder)}</div>
            </div>
          </div>
          <div class="management-machine-card__state management-machine-card__state--timer-working management-machine-card__state--elapsed-full">
            <div class="management-machine-card__state-label">Working Time</div>
            <div class="management-machine-card__state-value management-machine-card__state-value--elapsed" data-live-seconds="${elapsed}" data-live-type="working">${formatElapsedDuration(elapsed)}</div>
          </div>
          <div class="mt-2">
            <input class="form-control form-control-sm" data-field="note" maxlength="255" placeholder="Resolution note" value="${escapeHtml(alert.note || "")}">
          </div>
          <div class="mt-2 d-flex gap-2">
            <button class="btn btn-success btn-sm flex-grow-1" type="button" data-action="resolve">Close Alert</button>
          </div>
        </div>
      </div>
    </article>`;
}

function renderStatusDock() {
  const open = state.alerts.filter((alert) => getSection(alert) === "open").length;
  const working = state.alerts.filter((alert) => getSection(alert) === "working").length;
  const total = open + working;
  boardStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${open === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__title">Department Queue</div>
        <div class="operator-status-dock__subcopy">${total} active alerts</div>
      </div>
      <div class="operator-status-dock__stats">
        <div class="operator-status-dock__stat"><i class="bi bi-exclamation-triangle-fill"></i><div><div class="operator-status-dock__stat-label">Open</div><div class="operator-status-dock__stat-value">${open}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-tools"></i><div><div class="operator-status-dock__stat-label">Working</div><div class="operator-status-dock__stat-value">${working}</div></div></div>
      </div>
    </div>`;
}

function render() {
  const openAlerts = state.alerts.filter((alert) => getSection(alert) === "open");
  const workingAlerts = state.alerts.filter((alert) => getSection(alert) === "working");

  openAlertsList.innerHTML = openAlerts.length
    ? openAlerts.map((alert) => renderOpenCard(alert)).join("")
    : '<div class="board-builder-empty"><div class="h5 mb-1">No open alerts.</div><div class="small text-secondary">New operator calls will appear here.</div></div>';

  workingAlertsList.innerHTML = workingAlerts.length
    ? workingAlerts.map((alert) => renderWorkingCard(alert)).join("")
    : '<div class="board-builder-empty"><div class="h5 mb-1">No active work.</div><div class="small text-secondary">Acknowledged alerts move here.</div></div>';

  renderStatusDock();
  state.loadedAt = Date.now();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    throw new Error(data.error?.message || data.message || "Request failed");
  }
  return data.data;
}

async function loadState() {
  const [alertsData, metadata] = await Promise.all([
    fetchJson(alertsUrl),
    fetchJson(operatorMetadataUrl),
  ]);
  state.alerts = Array.isArray(alertsData) ? alertsData : [];
  state.users = metadata.users || [];
}

function payloadFromCard(card) {
  const responderUserId = card.querySelector('[data-field="responder_user_id"]')?.value || "";
  const note = card.querySelector('[data-field="note"]')?.value || "";
  const payload = {};
  if (responderUserId) payload.responder_user_id = Number(responderUserId);
  if (note.trim()) payload.note = note.trim();
  return payload;
}

async function acknowledgeAlert(alertId, card) {
  const payload = payloadFromCard(card);
  await fetchJson(`/api/andon/alerts/${alertId}/acknowledge`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
}

async function resolveAlert(alertId, card) {
  const payload = payloadFromCard(card);
  await fetchJson(`/api/andon/alerts/${alertId}/resolve`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
}

async function onListClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = event.target.closest("[data-alert-id]");
  if (!card) return;
  const alertId = Number(card.dataset.alertId);
  if (!alertId) return;

  button.disabled = true;
  try {
    if (button.dataset.action === "acknowledge") {
      await acknowledgeAlert(alertId, card);
    } else if (button.dataset.action === "resolve") {
      await resolveAlert(alertId, card);
    }
    await refresh();
  } catch (error) {
    window.alert(error.message || "Action failed");
  } finally {
    button.disabled = false;
  }
}

function tickTimers() {
  document.querySelectorAll("[data-live-seconds]").forEach((node) => {
    const base = Number(node.getAttribute("data-live-seconds") || 0);
    const rendered = Math.max(0, base + Math.floor((Date.now() - state.loadedAt) / 1000));
    node.textContent = formatElapsedDuration(rendered);
  });
}

function startTimer() {
  if (timerIntervalId) return;
  timerIntervalId = window.setInterval(tickTimers, 1000);
}

async function refresh() {
  await loadState();
  render();
}

async function boot() {
  openAlertsList?.addEventListener("click", (event) => { void onListClick(event); });
  workingAlertsList?.addEventListener("click", (event) => { void onListClick(event); });
  window.AndonRefreshBus?.onRefresh(() => {
    void refresh();
  });
  window.AndonRealtime?.onEvent((event) => {
    if (["alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "board_refresh"].includes(event.type)) {
      void refresh();
    }
  });
  await refresh();
  startTimer();
}

boot().catch((error) => {
  openAlertsList.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load alerts")}</div>`;
  workingAlertsList.innerHTML = "";
});
