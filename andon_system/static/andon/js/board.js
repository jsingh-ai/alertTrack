const alertsUrl = "/api/andon/alerts?status=active";
const operatorMetadataUrl = "/api/andon/operator-metadata";
const operatorMetadataCacheScope = [
  String(window.AndonRealtimeConfig?.companyId ?? "none"),
  String((document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "").slice(0, 16) || "anon"),
].join(":");
const operatorMetadataCacheKey = `andon-operator-metadata-cache-v1:${operatorMetadataCacheScope}`;
const operatorMetadataCacheTtlMs = 5 * 60 * 1000;

const openAlertsList = document.getElementById("openAlertsList");
const workingAlertsList = document.getElementById("workingAlertsList");
const boardStatusDock = document.getElementById("boardStatusDock");
const csrfHeaders = (headers = {}) => window.AndonSecurity?.withCsrfHeaders(headers) || headers;

const state = {
  alerts: [],
  users: [],
  timerSnapshotByAlert: {},
  selectedResponderByAlert: {},
  appendNoteByAlert: {},
  metadataLoading: false,
};

let timerIntervalId = null;
let operatorMetadataLoadPromise = null;
let boardRefreshInFlight = false;
let boardRefreshQueued = false;
let boardRefreshTimeoutId = null;
let localMutationRefreshLockUntil = 0;

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

function getTimerOrigin(alert, section) {
  if (section === "working") {
    return String(alert?.acknowledged_at || alert?.created_at || "");
  }
  return String(alert?.created_at || "");
}

function getTimerSeed(alert, section) {
  const alertId = Number(alert?.id || 0);
  const origin = getTimerOrigin(alert, section);
  const elapsedFromOrigin = elapsedSince(origin);
  const elapsedFromApi = Math.max(0, Math.floor(Number(alert?.elapsed_seconds) || 0));
  let seed = Math.max(elapsedFromOrigin, elapsedFromApi);

  const snapshot = state.timerSnapshotByAlert[alertId];
  if (snapshot && snapshot.section === section && snapshot.origin === origin) {
    seed = Math.max(seed, Math.floor(Number(snapshot.seconds) || 0));
  }
  return seed;
}

function syncTimerSnapshotFromDom() {
  document.querySelectorAll(".board-alert-card__timer[data-live-alert-id]").forEach((node) => {
    const alertId = Number(node.getAttribute("data-live-alert-id") || 0);
    if (!alertId) return;
    const section = String(node.getAttribute("data-live-section") || "");
    const origin = String(node.getAttribute("data-live-origin") || "");
    const base = Math.max(0, Math.floor(Number(node.getAttribute("data-live-seconds") || 0)));
    const anchorMs = Number(node.getAttribute("data-live-anchor-ms") || 0);
    const elapsedSinceAnchor = anchorMs ? Math.max(0, Math.floor((Date.now() - anchorMs) / 1000)) : 0;
    const rendered = base + elapsedSinceAnchor;
    state.timerSnapshotByAlert[alertId] = {
      seconds: rendered,
      section,
      origin,
    };
  });
}

function getScopedUsers(alert) {
  const machineGroupName = String(alert?.machine?.machine_type || "");
  const departmentId = Number(alert?.department?.id || alert?.department_id || 0);
  return (state.users || []).filter((user) => {
    const userDepartmentId = Number(user.department_id || 0);
    const userMachineGroup = String(user.machine_group_name || "");
    const matchesDepartment = departmentId ? userDepartmentId === departmentId : true;
    // Department-scoped responders may not carry a machine-group assignment.
    const matchesMachineGroup = machineGroupName ? (!userMachineGroup || userMachineGroup === machineGroupName) : true;
    return matchesDepartment && matchesMachineGroup;
  });
}

function renderUserChips(alert, kind) {
  const users = getScopedUsers(alert);
  const activeResponderId = state.selectedResponderByAlert[alert.id] || alert.responder_user_id || null;
  if (!users.length) {
    if (state.metadataLoading && !state.users.length) {
      return '<div class="problem-empty">Loading responders...</div>';
    }
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
  const section = "open";
  const origin = getTimerOrigin(alert, section);
  const elapsed = getTimerSeed(alert, section);
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
        <div
          class="board-alert-card__timer"
          data-live-seconds="${elapsed}"
          data-live-alert-id="${alert.id}"
          data-live-section="${section}"
          data-live-origin="${escapeHtml(origin)}"
          data-live-anchor-ms="${Date.now()}"
        >${formatElapsedDuration(elapsed)}</div>
      </div>

      <div class="board-alert-card__actions">
        <div class="board-alert-card__action-title">Select Responder</div>
        ${renderUserChips(alert, "open")}
        <div class="board-alert-card__action-title board-alert-card__action-title--notes">Notes</div>
        <input class="form-control board-alert-card__note-input" data-action="append-note" data-alert-id="${alert.id}" maxlength="255" placeholder="Append response to operator text" value="${escapeHtml(appendValue)}">
        <button class="btn board-alert-card__ack-btn" type="button" data-action="acknowledge" data-alert-id="${alert.id}">Acknowledge</button>
      </div>
    </article>`;
}

function renderWorkingCard(alert) {
  const machineName = alert.machine?.name || "Machine";
  const issueName = alert.issue_problem?.name || alert.issue_category?.name || "Unassigned";
  const responder = alert.responder_name_text || "No responder assigned";
  const section = "working";
  const origin = getTimerOrigin(alert, section);
  const elapsed = getTimerSeed(alert, section);
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
        <div
          class="board-alert-card__timer"
          data-live-seconds="${elapsed}"
          data-live-alert-id="${alert.id}"
          data-live-section="${section}"
          data-live-origin="${escapeHtml(origin)}"
          data-live-anchor-ms="${Date.now()}"
        >${formatElapsedDuration(elapsed)}</div>
      </div>

      <div class="board-alert-card__working-actions">
        <div class="board-alert-card__action-title board-alert-card__action-title--notes">Notes</div>
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
  syncTimerSnapshotFromDom();
  const openAlerts = state.alerts.filter((alert) => getSection(alert) === "open");
  const workingAlerts = state.alerts.filter((alert) => getSection(alert) === "working");

  openAlertsList.innerHTML = openAlerts.length
    ? openAlerts.map((alert) => renderOpenCard(alert)).join("")
    : '<div class="board-builder-empty"><div class="h5 mb-1">No open alerts.</div><div class="small text-secondary">New operator calls will appear here.</div></div>';

  workingAlertsList.innerHTML = workingAlerts.length
    ? workingAlerts.map((alert) => renderWorkingCard(alert)).join("")
    : '<div class="board-builder-empty"><div class="h5 mb-1">No active work.</div><div class="small text-secondary">Acknowledged alerts move here.</div></div>';

  renderStatusDock();

  const activeAlertIds = new Set([...openAlerts, ...workingAlerts].map((alert) => Number(alert.id)).filter(Boolean));
  Object.keys(state.timerSnapshotByAlert).forEach((id) => {
    if (!activeAlertIds.has(Number(id))) {
      delete state.timerSnapshotByAlert[id];
    }
  });

  tickTimers();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    throw new Error(data.error?.message || data.message || "Request failed");
  }
  return data.data;
}

async function loadAlerts() {
  const alertsData = await fetchJson(alertsUrl);
  state.alerts = Array.isArray(alertsData) ? alertsData : [];
}

function getCachedOperatorMetadata() {
  try {
    const raw = window.sessionStorage.getItem(operatorMetadataCacheKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const cachedAt = Number(parsed.cachedAt || 0);
    if (!cachedAt || (Date.now() - cachedAt) > operatorMetadataCacheTtlMs) return null;
    return parsed.data || null;
  } catch (_error) {
    return null;
  }
}

function setCachedOperatorMetadata(metadata) {
  try {
    window.sessionStorage.setItem(
      operatorMetadataCacheKey,
      JSON.stringify({ cachedAt: Date.now(), data: metadata || {} }),
    );
  } catch (_error) {
    // Ignore storage failures in locked-down kiosk environments.
  }
}

function applyOperatorMetadata(metadata) {
  state.users = metadata?.users || [];
}

function hydrateOperatorMetadataFromCache() {
  const cached = getCachedOperatorMetadata();
  if (!cached) return false;
  applyOperatorMetadata(cached);
  return true;
}

async function loadOperatorMetadata(options = {}) {
  const preferCache = options.preferCache !== false;
  if (preferCache && hydrateOperatorMetadataFromCache()) return;
  const metadata = await fetchJson(operatorMetadataUrl);
  applyOperatorMetadata(metadata);
  setCachedOperatorMetadata(metadata);
}

async function ensureOperatorMetadataLoaded(options = {}) {
  const force = Boolean(options.force);
  if (!force && state.users.length) {
    return;
  }
  if (!operatorMetadataLoadPromise) {
    state.metadataLoading = true;
    operatorMetadataLoadPromise = loadOperatorMetadata({ preferCache: !force })
      .finally(() => {
        state.metadataLoading = false;
        operatorMetadataLoadPromise = null;
      });
  }
  await operatorMetadataLoadPromise;
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

  const updatedAlert = await fetchJson(`/api/andon/alerts/${alertId}/acknowledge`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });

  state.appendNoteByAlert[alertId] = "";
  return updatedAlert;
}

async function resolveAlert(alert) {
  const alertId = Number(alert.id);
  const responderUserId = state.selectedResponderByAlert[alertId] || alert.responder_user_id || null;
  const payload = {};
  if (responderUserId) payload.responder_user_id = Number(responderUserId);
  const combinedNote = buildCombinedNote(alert);
  if (combinedNote) payload.note = combinedNote;

  const updatedAlert = await fetchJson(`/api/andon/alerts/${alertId}/resolve`, {
    method: "POST",
    headers: csrfHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });

  state.appendNoteByAlert[alertId] = "";
  return updatedAlert;
}

function mergeAlertPayload(updatedAlert, fallbackAlert) {
  if (!updatedAlert) return fallbackAlert;
  return {
    ...(fallbackAlert || {}),
    ...updatedAlert,
    machine: updatedAlert.machine ?? fallbackAlert?.machine ?? null,
    department: updatedAlert.department ?? fallbackAlert?.department ?? null,
    issue_category: updatedAlert.issue_category ?? fallbackAlert?.issue_category ?? null,
    issue_problem: updatedAlert.issue_problem ?? fallbackAlert?.issue_problem ?? null,
  };
}

function applyAlertUpdate(updatedAlert, fallbackAlert) {
  if (!updatedAlert && !fallbackAlert) return;
  const merged = mergeAlertPayload(updatedAlert, fallbackAlert);
  const alertId = Number(merged?.id || fallbackAlert?.id || 0);
  if (!alertId) return;

  state.alerts = state.alerts.filter((item) => Number(item.id) !== alertId);
  if (getSection(merged) !== "closed") {
    state.alerts.unshift(merged);
  }
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
    if (button.dataset.kind !== "open") return;
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
      const updated = await acknowledgeAlert(alert);
      applyAlertUpdate(updated, alert);
      localMutationRefreshLockUntil = Date.now() + 700;
      render();
      scheduleRefresh(60);
    } else if (action === "resolve") {
      const updated = await resolveAlert(alert);
      applyAlertUpdate(updated, alert);
      localMutationRefreshLockUntil = Date.now() + 700;
      render();
      scheduleRefresh(60);
    }
  } catch (error) {
    window.alert(error.message || "Action failed");
  } finally {
    button.disabled = false;
  }
}

function tickTimers() {
  document.querySelectorAll(".board-alert-card__timer[data-live-alert-id]").forEach((node) => {
    const alertId = Number(node.getAttribute("data-live-alert-id") || 0);
    if (!alertId) return;
    const section = String(node.getAttribute("data-live-section") || "");
    const origin = String(node.getAttribute("data-live-origin") || "");
    const base = Number(node.getAttribute("data-live-seconds") || 0);
    const anchorMs = Number(node.getAttribute("data-live-anchor-ms") || 0);
    const elapsedSinceAnchor = anchorMs ? Math.floor((Date.now() - anchorMs) / 1000) : 0;
    const rendered = Math.max(0, base + Math.max(0, elapsedSinceAnchor));
    node.textContent = formatElapsedDuration(rendered);
    state.timerSnapshotByAlert[alertId] = {
      seconds: rendered,
      section,
      origin,
    };
  });
}

function startTimer() {
  if (timerIntervalId) return;
  timerIntervalId = window.setInterval(tickTimers, 1000);
}

async function refresh() {
  if (boardRefreshInFlight) {
    boardRefreshQueued = true;
    return;
  }
  boardRefreshInFlight = true;
  try {
    await loadAlerts();
    render();
  } finally {
    boardRefreshInFlight = false;
    if (boardRefreshQueued) {
      boardRefreshQueued = false;
      scheduleRefresh(80);
    }
  }
}

function scheduleRefresh(delayMs = 120) {
  if (boardRefreshTimeoutId) {
    clearTimeout(boardRefreshTimeoutId);
  }
  boardRefreshTimeoutId = window.setTimeout(() => {
    boardRefreshTimeoutId = null;
    void refresh();
  }, Math.max(0, Number(delayMs) || 0));
}

async function refreshImmediately() {
  await loadAlerts();
  render();
}

async function boot() {
  hydrateOperatorMetadataFromCache();
  openAlertsList?.addEventListener("click", (event) => { void onListClick(event); });
  workingAlertsList?.addEventListener("click", (event) => { void onListClick(event); });
  openAlertsList?.addEventListener("input", onListInput);
  workingAlertsList?.addEventListener("input", onListInput);

  window.AndonRefreshBus?.onRefresh(() => {
    scheduleRefresh(100);
  });
  window.AndonRealtime?.onEvent((event) => {
    if (["alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
      if (Date.now() < localMutationRefreshLockUntil) return;
      scheduleRefresh(100);
      if (event.type === "admin_metadata_updated") {
        void ensureOperatorMetadataLoaded({ force: true }).then(render).catch(() => {});
      }
    }
  });

  await refreshImmediately();
  startTimer();
  void ensureOperatorMetadataLoaded().then(render).catch(() => {});
}

boot().catch((error) => {
  openAlertsList.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load alerts")}</div>`;
  workingAlertsList.innerHTML = "";
});
