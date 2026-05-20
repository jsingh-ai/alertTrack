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
  selectedResponderByAlert: {},
  appendNoteByAlert: {},
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

function getScopedUsers(alert) {
  const machineGroupName = String(alert?.machine?.machine_type || "");
  const departmentId = Number(alert?.department?.id || alert?.department_id || 0);
  return (state.users || []).filter((user) => {
    const userDepartmentId = Number(user.department_id || 0);
    const userMachineGroup = String(user.machine_group_name || "");
    const matchesDepartment = departmentId ? userDepartmentId === departmentId : true;
    const matchesMachineGroup = machineGroupName ? userMachineGroup === machineGroupName : true;
    return matchesDepartment && matchesMachineGroup;
  });
}

function renderUserChips(alert, kind) {
  const users = getScopedUsers(alert);
  const activeResponderId = state.selectedResponderByAlert[alert.id] || alert.responder_user_id || null;
  if (!users.length) {
    return '<div class="problem-empty">No users found for this machine group and department.</div>';
  }
  return `
    <div class="board-alert-card__chip-grid">
      ${users
        .map((user) => `
          <button type="button" class="user-chip board-alert-card__user-chip ${Number(activeResponderId) === Number(user.id) ? "is-selected" : ""}" data-action="pick-user" data-alert-id="${alert.id}" data-user-id="${user.id}" data-kind="${kind}">
            <span class="user-chip__name">${escapeHtml(user.display_name || user.username || `User ${user.id}`)}</span>
            <span class="user-chip__meta">${escapeHtml(user.work_id || "")}</span>
          </button>
        `)
        .join("")}
    </div>`;
}

function renderOpenCard(alert) {
  const machineName = alert.machine?.name || "Machine";
  const issueName = alert.issue_problem?.name || alert.issue_category?.name || "Unassigned";
  const elapsed = elapsedSince(alert.created_at);
  const existingNote = String(alert.note || "").trim();
  const appendValue = state.appendNoteByAlert[alert.id] || "";

  return `
    <article class="board-alert-card board-alert-card--inline-open status-open" data-alert-id="${alert.id}">
      <div class="board-alert-card__top">
        <div class="board-alert-card__title-row">
          <h3 class="board-alert-card__title">${escapeHtml(machineName)}</h3>
        </div>
      </div>

      <div class="board-alert-card__issue">
        <div class="board-alert-card__issue-value">${escapeHtml(issueName)}</div>
      </div>

      ${existingNote ? `
        <div class="board-alert-card__note">
          <div class="board-alert-card__note-label">Operator Note</div>
          <div class="board-alert-card__note-value">${escapeHtml(existingNote)}</div>
        </div>
      ` : ""}

      <div class="board-alert-card__timer-box">
        <div class="board-alert-card__timer-label">Elapsed</div>
        <div class="board-alert-card__timer" data-live-seconds="${elapsed}">${formatElapsedDuration(elapsed)}</div>
      </div>

      <div class="board-alert-card__actions">
        <div class="board-alert-card__action-title">Select Responder</div>
        ${renderUserChips(alert, "open")}
        <input class="form-control board-alert-card__note-input" data-action="append-note" data-alert-id="${alert.id}" maxlength="255" placeholder="Append response to operator text" value="${escapeHtml(appendValue)}">
        <button class="btn board-alert-card__ack-btn" type="button" data-action="acknowledge" data-alert-id="${alert.id}">Acknowledge</button>
      </div>
    </article>`;
}

function renderWorkingCard(alert) {
  const machineName = alert.machine?.name || "Machine";
  const issueName = alert.issue_problem?.name || alert.issue_category?.name || "Unassigned";
  const responder = alert.responder_name_text || "No responder assigned";
  const startedAt = alert.acknowledged_at || alert.created_at;
  const elapsed = elapsedSince(startedAt);
  const currentNote = String(alert.note || "").trim();
  const appendValue = state.appendNoteByAlert[alert.id] || "";

  return `
    <article class="board-alert-card status-acknowledged" data-alert-id="${alert.id}">
      <div class="board-alert-card__top">
        <div class="board-alert-card__title-row">
          <h3 class="board-alert-card__title">${escapeHtml(machineName)}</h3>
        </div>
      </div>

      <div class="board-alert-card__issue">
        <div class="board-alert-card__issue-value">${escapeHtml(issueName)}</div>
      </div>

      <div class="board-alert-card__responder">
        <div class="board-alert-card__note-label">Responder</div>
        <div class="board-alert-card__responder-value">${escapeHtml(responder)}</div>
      </div>

      ${currentNote ? `
        <div class="board-alert-card__note board-alert-card__note--responding">
          <div class="board-alert-card__note-label">Current Note</div>
          <div class="board-alert-card__note-value">${escapeHtml(currentNote)}</div>
        </div>
      ` : ""}

      <div class="board-alert-card__timer-box">
        <div class="board-alert-card__timer-label">Working Time</div>
        <div class="board-alert-card__timer" data-live-seconds="${elapsed}">${formatElapsedDuration(elapsed)}</div>
      </div>

      <div class="board-alert-card__working-actions">
        <div class="board-alert-card__action-title">Update Responder (Optional)</div>
        ${renderUserChips(alert, "working")}
        <input class="form-control board-alert-card__note-input" data-action="append-note" data-alert-id="${alert.id}" maxlength="255" placeholder="Append additional response text" value="${escapeHtml(appendValue)}">
        <button class="btn board-alert-card__close-btn" type="button" data-action="resolve" data-alert-id="${alert.id}">Close Alert</button>
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

function buildCombinedNote(alert) {
  const existing = String(alert.note || "").trim();
  const append = String(state.appendNoteByAlert[alert.id] || "").trim();
  if (!append) return existing;
  if (!existing) return append;
  return `${existing}\n${append}`;
}

async function acknowledgeAlert(alert) {
  const alertId = Number(alert.id);
  const responderUserId = state.selectedResponderByAlert[alertId] || alert.responder_user_id || null;
  const payload = {};
  if (responderUserId) payload.responder_user_id = Number(responderUserId);
  const combinedNote = buildCombinedNote(alert);
  if (combinedNote) payload.note = combinedNote;

  await fetchJson(`/api/andon/alerts/${alertId}/acknowledge`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });

  state.appendNoteByAlert[alertId] = "";
}

async function resolveAlert(alert) {
  const alertId = Number(alert.id);
  const responderUserId = state.selectedResponderByAlert[alertId] || alert.responder_user_id || null;
  const payload = {};
  if (responderUserId) payload.responder_user_id = Number(responderUserId);
  const combinedNote = buildCombinedNote(alert);
  if (combinedNote) payload.note = combinedNote;

  await fetchJson(`/api/andon/alerts/${alertId}/resolve`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });

  state.appendNoteByAlert[alertId] = "";
}

function onListInput(event) {
  const input = event.target.closest('[data-action="append-note"]');
  if (!input) return;
  const alertId = Number(input.dataset.alertId);
  if (!alertId) return;
  state.appendNoteByAlert[alertId] = input.value || "";
}

async function onListClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const alertId = Number(button.dataset.alertId);
  if (!alertId) return;

  if (action === "pick-user") {
    const userId = Number(button.dataset.userId);
    state.selectedResponderByAlert[alertId] = Number.isFinite(userId) ? userId : null;
    render();
    return;
  }

  const alert = state.alerts.find((item) => Number(item.id) === alertId);
  if (!alert) return;

  button.disabled = true;
  try {
    if (action === "acknowledge") {
      await acknowledgeAlert(alert);
    } else if (action === "resolve") {
      await resolveAlert(alert);
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
  openAlertsList?.addEventListener("input", onListInput);
  workingAlertsList?.addEventListener("input", onListInput);

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
