const boardGrid = document.getElementById("boardGrid");
const boardMachineGroup = document.getElementById("boardMachineGroup");
const boardDepartment = document.getElementById("boardDepartment");
const boardLockView = document.getElementById("boardLockView");
const boardClearView = document.getElementById("boardClearView");
const boardLockStatus = document.getElementById("boardLockStatus");
const csrfHeaders = (headers = {}) => window.AndonSecurity?.withCsrfHeaders(headers) || headers;

const storageKey = "andon-board-view";
const lockedMachineGroup = "Press";

const state = {
  alerts: [],
  boardState: { machines: [], departments: [] },
  users: [],
  filters: {
    machineGroup: "",
    department: "",
  },
  locked: false,
  inlineDrafts: {},
};

let boardTimerIntervalId = null;
let boardRefreshTimeoutId = null;
let boardFallbackPollIntervalId = null;

function startBoardTimerLoop() {
  if (boardTimerIntervalId || document.hidden) return;
  boardTimerIntervalId = setInterval(updateBoardTimers, 1000);
}

function stopBoardTimerLoop() {
  if (!boardTimerIntervalId) return;
  clearInterval(boardTimerIntervalId);
  boardTimerIntervalId = null;
}

function handleBoardVisibilityChange() {
  if (document.hidden) {
    stopBoardTimerLoop();
    return;
  }
  startBoardTimerLoop();
}

function statusClass(status) {
  return `status-${String(status || "").toLowerCase()}`;
}

function statusLabel(status) {
  if (status === "ACKNOWLEDGED") return "Working";
  return String(status || "");
}

function formatElapsedSeconds(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(seconds / 60);
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hrs > 0) {
    const remSeconds = seconds % 60;
    return `${String(hrs).padStart(2, "0")}:${String(remMins).padStart(2, "0")}:${String(remSeconds).padStart(2, "0")}`;
  }
  return `${String(mins).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function loadSavedView() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    state.filters.machineGroup = saved.machineGroup || lockedMachineGroup;
    state.filters.department = saved.department || "";
    state.locked = typeof saved.locked === "boolean" ? saved.locked : true;
  } catch {
    state.filters.machineGroup = lockedMachineGroup;
    state.filters.department = "";
    state.locked = true;
  }
}

function saveView() {
  localStorage.setItem(storageKey, JSON.stringify({
    machineGroup: state.filters.machineGroup,
    department: state.filters.department,
    locked: state.locked,
  }));
}

function groupAlerts(alerts) {
  const groups = new Map();
  alerts.forEach((alert) => {
    const machineGroup = alert.machine?.machine_type || "Unassigned";
    const departmentName = alert.department?.name || "Unassigned";
    if (!groups.has(machineGroup)) {
      groups.set(machineGroup, new Map());
    }
    const departmentGroups = groups.get(machineGroup);
    if (!departmentGroups.has(departmentName)) {
      departmentGroups.set(departmentName, []);
    }
    departmentGroups.get(departmentName).push(alert);
  });
  return groups;
}

function buildAlertsFromMachines(machines) {
  return machines
    .filter((machine) => machine.active_alert)
    .map((machine) => ({
      ...machine.active_alert,
      machine: {
        id: machine.id,
        name: machine.name,
        machine_type: machine.machine_type,
        department_id: machine.department_id,
      },
      department: {
        id: machine.active_alert.department_id || machine.department_id,
        name: machine.active_alert.department_name || machine.active_alert.category_name || machine.department_name || "Unassigned",
      },
      issue_problem: {
        name: machine.active_alert.problem_name || "",
      },
    }));
}

function getRelevantUsers(machine, department) {
  const machineGroupName = machine?.machine_type || "";
  const departmentId = department?.id ? Number(department.id) : null;
  return (state.users || []).filter((user) => {
    const matchesDepartment = departmentId ? Number(user.department_id || 0) === departmentId : true;
    const matchesMachineGroup = machineGroupName ? String(user.machine_group_name || "") === machineGroupName : true;
    return matchesDepartment && matchesMachineGroup;
  });
}

function getInlineDraft(alertId) {
  const key = String(alertId);
  if (!state.inlineDrafts[key]) {
    state.inlineDrafts[key] = {
      responderUserId: null,
      note: "",
    };
  }
  return state.inlineDrafts[key];
}

function setInlineDraft(alertId, patch) {
  const draft = getInlineDraft(alertId);
  state.inlineDrafts[String(alertId)] = {
    ...draft,
    ...patch,
  };
}

function findAlertById(alertId) {
  return state.alerts.find((item) => Number(item.id) === Number(alertId));
}

function getInlineResponderUserId(alertId) {
  const draft = getInlineDraft(alertId);
  if (Number.isFinite(Number(draft.responderUserId))) {
    return Number(draft.responderUserId);
  }
  const alert = findAlertById(alertId);
  return Number(alert?.responder_user_id || 0) || null;
}

function syncInlineCardState(alertId) {
  const card = boardGrid.querySelector(`[data-alert-id="${alertId}"]`);
  if (!card) return;
  const selectedUserId = getInlineResponderUserId(alertId);
  card.querySelectorAll("[data-board-inline-user-id]").forEach((button) => {
    button.classList.toggle("is-selected", Number(button.dataset.boardInlineUserId) === Number(selectedUserId));
  });
}

function renderInlineUserButtons(users, selectedUserId, alertId) {
  const validSelected = users.some((user) => Number(user.id) === Number(selectedUserId)) ? selectedUserId : null;
  if (!users.length) {
    return '<div class="problem-empty">No users found for this machine and department.</div>';
  }
  return users
    .map(
      (user) => `
        <button type="button" class="user-chip board-alert-card__user-chip ${Number(validSelected) === Number(user.id) ? "is-selected" : ""}" data-board-inline-user-id="${user.id}" data-board-alert-id="${alertId}">
          <span class="user-chip__name">${escapeHtml(user.display_name)}</span>
          <span class="user-chip__meta">${escapeHtml(user.work_id || "")}</span>
        </button>`,
    )
    .join("");
}

function matchesFilter(alert) {
  if (state.filters.machineGroup && (alert.machine?.machine_type || "") !== state.filters.machineGroup) {
    return false;
  }
  if (state.filters.department && (alert.department?.name || "") !== state.filters.department) {
    return false;
  }
  return true;
}

function radiusValue(value) {
  return escapeHtml(value || "N/A");
}

function renderRadiusEventBadge(machine) {
  const value = String(machine?.radius?.event_type || "").trim();
  if (!value) return "";
  return `<span class="radius-event-badge" aria-label="Radius event type">${escapeHtml(value)}</span>`;
}

function renderRadiusPanel(machine) {
  const radius = machine?.radius || null;
  return `
    <div class="radius-panel radius-panel--board">
      <div class="radius-panel__header">
        <div class="radius-panel__title">Radius</div>
        <div class="radius-panel__machine">Machine ${radiusValue(radius?.machine_id || machine?.radius_machine_id)}</div>
      </div>
      <div class="radius-panel__grid radius-panel__grid--pair">
        <div class="radius-panel__item">
          <div class="radius-panel__label">Operator Code</div>
          <div class="radius-panel__value">${radiusValue(radius?.operation_code)}</div>
        </div>
        <div class="radius-panel__item">
          <div class="radius-panel__label">Job Code</div>
          <div class="radius-panel__value">${radiusValue(radius?.job_code)}</div>
        </div>
      </div>
      <div class="radius-panel__grid radius-panel__grid--stack">
        <div class="radius-panel__item radius-panel__item--wide">
          <div class="radius-panel__label">Status</div>
          <div class="radius-panel__value">${radiusValue(radius?.status_label)}</div>
        </div>
      </div>
    </div>`;
}

function cardTemplate(alert) {
  const issue = String(alert.issue_problem?.name || "").trim();
  const operatorMessage = String(alert.created_note || alert.note || "").trim();
  const respondingMessage = alert.status === "ACKNOWLEDGED" ? String(alert.note || "").trim() : "";
  const elapsedSeconds = Math.max(0, Math.floor(alert.elapsed_seconds || 0));
  const isOpen = alert.status === "OPEN";
  const isAcknowledged = alert.status === "ACKNOWLEDGED";
  const draft = getInlineDraft(alert.id);
  const selectedResponderUserId = getInlineResponderUserId(alert.id);
  const users = isOpen ? getRelevantUsers(alert.machine, { id: alert.department?.id, name: alert.department?.name }) : [];
  const canClose = Boolean(alert.responder_user_id || alert.responder_name_text);
  return `
    <article class="board-card h-100 board-alert-card ${statusClass(alert.status)} ${isOpen ? "board-alert-card--inline-open" : ""}" data-alert-id="${alert.id}">
      <div class="board-alert-card__top">
        <div class="board-alert-card__title-row">
          <h3 class="board-alert-card__title">${escapeHtml(alert.machine?.name || "")}</h3>
          ${renderRadiusEventBadge(alert.machine)}
        </div>
        <span class="status-pill ${statusClass(alert.status)}">${statusLabel(alert.status)}</span>
      </div>
      ${renderRadiusPanel(alert.machine)}
      <div class="board-alert-card__issue ${isAcknowledged ? "board-alert-card__issue--acknowledged" : ""}">
        <div class="board-alert-card__issue-value">${escapeHtml(issue || "Unassigned")}</div>
      </div>
      ${isAcknowledged && alert.responder_name_text ? `
        <div class="board-alert-card__responder">
          <div class="board-alert-card__note-label">Who is responding</div>
          <div class="board-alert-card__responder-value">${escapeHtml(alert.responder_name_text)}</div>
        </div>` : ""}
      ${isAcknowledged ? `
        <div class="board-alert-card__timer-box">
          <div class="board-alert-card__timer-label">Elapsed timer</div>
          <div class="alert-timer board-alert-card__timer" data-elapsed-seconds="${elapsedSeconds}">${formatElapsedSeconds(elapsedSeconds)}</div>
        </div>
      ` : ""}
      ${!isAcknowledged ? `
        <div class="board-alert-card__timer-box">
          <div class="board-alert-card__timer-label">Elapsed timer</div>
          <div class="alert-timer board-alert-card__timer" data-elapsed-seconds="${elapsedSeconds}">${formatElapsedSeconds(elapsedSeconds)}</div>
        </div>
      ` : ""}
      ${isOpen && operatorMessage ? `
        <div class="board-alert-card__note">
          <div class="board-alert-card__note-label">Operator Message</div>
          <div class="board-alert-card__note-value">${escapeHtml(operatorMessage)}</div>
        </div>` : ""}
      ${isAcknowledged ? `
        <div class="board-alert-card__working-actions">
          <div class="board-alert-card__action-title">Discussion Chat</div>
          ${operatorMessage ? `
            <div class="board-alert-card__note">
              <div class="board-alert-card__note-label">Operator Message</div>
              <div class="board-alert-card__note-value">${escapeHtml(operatorMessage)}</div>
            </div>` : ""}
          ${respondingMessage && respondingMessage !== operatorMessage ? `
            <div class="board-alert-card__note board-alert-card__note--responding">
              <div class="board-alert-card__note-label">Responding Message</div>
              <div class="board-alert-card__note-value">${escapeHtml(respondingMessage)}</div>
            </div>` : ""}
          <div class="board-alert-card__action-title">Note</div>
          <textarea class="form-control board-alert-card__note-input" rows="2" placeholder="Add note" data-board-alert-note="true" data-board-alert-id="${alert.id}">${escapeHtml(draft.note)}</textarea>
          <button type="button" class="btn btn-primary board-alert-card__close-btn" data-board-close="true" data-alert-id="${alert.id}" ${canClose ? "" : "disabled"}>Close</button>
        </div>
      ` : ""}
      ${isOpen ? `
        <div class="board-alert-card__actions">
          <div class="board-alert-card__action-title">WHO IS ACCEPTING</div>
          <div class="user-chip-grid board-alert-card__chip-grid">
            ${renderInlineUserButtons(users, selectedResponderUserId, alert.id)}
          </div>
          <div class="board-alert-card__action-title">Note</div>
          <textarea class="form-control board-alert-card__note-input" rows="2" placeholder="Add note" data-board-alert-note="true" data-board-alert-id="${alert.id}">${escapeHtml(draft.note)}</textarea>
          <button type="button" class="btn btn-primary board-alert-card__ack-btn" data-board-acknowledge="true" data-alert-id="${alert.id}">Acknowledge</button>
        </div>
      ` : ""}
    </article>`;
}

function renderAlertCardList(alerts) {
  return alerts.map(cardTemplate).join("");
}

function renderDepartmentStatusGroup(title, alerts, variant) {
  if (!alerts.length) return "";
  return `
    <section class="board-department-subgroup board-department-subgroup--${variant}">
      <div class="board-department-subgroup__header">
        <div class="board-department-subgroup__header-spacer" aria-hidden="true"></div>
        <h4 class="board-department-subgroup__title">${escapeHtml(title)}</h4>
        <div class="board-department-subgroup__header-spacer" aria-hidden="true"></div>
      </div>
      <div class="board-department-subgroup__grid">
        ${renderAlertCardList(alerts)}
      </div>
    </section>`;
}

function renderDepartmentGroup(departmentName, alerts) {
  const isMaintenance = String(departmentName || "").toLowerCase() === "maintenance";
  if (isMaintenance) {
    const waitingAlerts = alerts.filter((alert) => alert.status === "OPEN");
    const workingAlerts = alerts.filter((alert) => alert.status !== "OPEN");
    return `
      <section class="board-card board-department-panel board-department-panel--split">
        <div class="board-department-panel__header">
          <h3 class="board-department-panel__title">${escapeHtml(departmentName)}</h3>
          <div class="board-department-panel__count">${alerts.length} alerts</div>
        </div>
        <div class="board-department-panel__grid board-department-panel__grid--split">
          ${renderDepartmentStatusGroup("Waiting for Acknowledge", waitingAlerts, "waiting")}
          ${renderDepartmentStatusGroup("Working On It", workingAlerts, "working")}
        </div>
      </section>`;
  }
  return `
    <section class="board-card board-department-panel">
      <div class="board-department-panel__header">
        <h3 class="board-department-panel__title">${escapeHtml(departmentName)}</h3>
        <div class="board-department-panel__count">${alerts.length} alerts</div>
      </div>
      <div class="board-department-panel__grid">
        ${renderAlertCardList(alerts)}
      </div>
    </section>`;
}

function renderMachineGroup(machineGroupName, departmentGroups) {
  const totalAlerts = [...departmentGroups.values()].reduce((sum, alerts) => sum + alerts.length, 0);
  const inner = [...departmentGroups.entries()]
    .map(([departmentName, alerts]) => renderDepartmentGroup(departmentName, alerts))
    .join("");
  return `
    <section class="board-group board-group-panel">
      <div class="board-group__header">
        <div class="board-group__header-spacer" aria-hidden="true"></div>
        <h2 class="board-group__title">${escapeHtml(machineGroupName)} Department</h2>
        <div class="board-group__count">${totalAlerts} alert${totalAlerts === 1 ? "" : "s"}</div>
      </div>
      <div class="board-group__body">
        ${inner}
      </div>
    </section>`;
}

function populateFilterOptions() {
  const groups = [...new Set(state.boardState.machines.map((machine) => machine.machine_type || "Unassigned"))].sort();
  const departments = [...new Set(state.boardState.departments.map((department) => department.name || "Unassigned"))].sort();
  const currentGroup = state.filters.machineGroup || lockedMachineGroup;
  const currentDepartment = state.filters.department;

  boardMachineGroup.innerHTML = '<option value="">All Groups</option>' + groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("");
  boardDepartment.innerHTML = '<option value="">All Departments</option>' + departments.map((department) => `<option value="${escapeHtml(department)}">${escapeHtml(department)}</option>`).join("");
  boardMachineGroup.value = currentGroup;
  boardDepartment.value = currentDepartment;
  boardMachineGroup.disabled = state.locked;
  boardDepartment.disabled = state.locked;
  boardLockView.textContent = state.locked ? "Unlock View" : "Lock View";
  boardLockView.disabled = false;
  boardLockStatus.textContent = state.locked ? `${lockedMachineGroup} Locked` : "Unlocked";
}

function renderBoard() {
  const filtered = state.alerts.filter(matchesFilter);
  const grouped = groupAlerts(filtered);
  boardGrid.innerHTML = filtered.length
    ? [...grouped.entries()].map(([machineGroupName, departmentGroups]) => renderMachineGroup(machineGroupName, departmentGroups)).join("")
    : '<div class="board-empty text-center">No active alerts for the selected view.</div>';
}

function updateBoardTimers() {
  document.querySelectorAll(".alert-timer[data-elapsed-seconds]").forEach((timer) => {
    const card = timer.closest(".board-alert-card");
    const statusText = card?.querySelector(".status-pill")?.textContent?.trim();
    if (statusText === "RESOLVED" || statusText === "CANCELLED") return;
    const nextSeconds = Number(timer.dataset.elapsedSeconds || "0") + 1;
    timer.dataset.elapsedSeconds = String(nextSeconds);
    timer.textContent = formatElapsedSeconds(nextSeconds);
  });
}

async function refreshBoard() {
  const boardStateResponse = await fetch("/api/andon/board-state");
  const boardStateData = await boardStateResponse.json();
  state.boardState = boardStateData.data || state.boardState;
  state.users = state.boardState.users || [];
  state.alerts = buildAlertsFromMachines(state.boardState.machines || []);
  populateFilterOptions();
  renderBoard();
}

function scheduleBoardRefresh() {
  if (boardRefreshTimeoutId) {
    clearTimeout(boardRefreshTimeoutId);
  }
  boardRefreshTimeoutId = setTimeout(() => {
    boardRefreshTimeoutId = null;
    refreshBoard();
  }, 150);
}

function setBoardFallbackPolling(enabled) {
  if (!enabled && boardFallbackPollIntervalId) {
    clearInterval(boardFallbackPollIntervalId);
    boardFallbackPollIntervalId = null;
    return;
  }
  if (enabled && !boardFallbackPollIntervalId) {
    boardFallbackPollIntervalId = setInterval(scheduleBoardRefresh, 15000);
  }
}

function syncViewFromControls() {
  state.filters.machineGroup = state.locked ? lockedMachineGroup : (boardMachineGroup.value || "");
  state.filters.department = boardDepartment.value || "";
  saveView();
  renderBoard();
}

function clearBoardView() {
  state.filters.machineGroup = lockedMachineGroup;
  state.filters.department = "";
  state.locked = true;
  saveView();
  populateFilterOptions();
  renderBoard();
}

boardGrid.addEventListener("click", async (event) => {
  const userButton = event.target.closest("[data-board-inline-user-id]");
  if (userButton) {
    const alertId = Number(userButton.dataset.boardAlertId);
    const userId = Number(userButton.dataset.boardInlineUserId);
    setInlineDraft(alertId, { responderUserId: Number.isFinite(userId) ? userId : null });
    syncInlineCardState(alertId);
    return;
  }

  const ackButton = event.target.closest("[data-board-acknowledge]");
  if (ackButton) {
    const alertId = Number(ackButton.dataset.alertId);
    const alertData = findAlertById(alertId);
    if (!alertData) return;
    const draft = getInlineDraft(alertId);
    const responderUserId = getInlineResponderUserId(alertId);
    const payload = new FormData();
    if (responderUserId) {
      payload.append("responder_user_id", String(responderUserId));
    }
    payload.append("responder_name_text", "");
    if (draft.note.trim()) {
      payload.append("note", draft.note.trim());
    }
    ackButton.disabled = true;
    const response = await fetch(`/api/andon/alerts/${alertId}/acknowledge`, {
      method: "POST",
      body: payload,
      headers: csrfHeaders(),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!data.success) {
      ackButton.disabled = false;
      window.alert(data.error.message);
      return;
    }
    delete state.inlineDrafts[String(alertId)];
    window.AndonRefreshBus?.notify();
    return;
  }

  const closeButton = event.target.closest("[data-board-close]");
  if (closeButton) {
    const alertId = Number(closeButton.dataset.alertId);
    const alertData = findAlertById(alertId);
    if (!alertData) return;
    const draft = getInlineDraft(alertId);
    const payload = new FormData();
    if (alertData.responder_user_id) {
      payload.append("responder_user_id", String(alertData.responder_user_id));
    }
    payload.append("responder_name_text", alertData.responder_name_text || "");
    if (draft.note.trim()) {
      payload.append("note", draft.note.trim());
    }
    closeButton.disabled = true;
    const response = await fetch(`/api/andon/alerts/${alertId}/resolve`, {
      method: "POST",
      body: payload,
      headers: csrfHeaders(),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!data.success) {
      closeButton.disabled = false;
      window.alert(data.error.message);
      return;
    }
    delete state.inlineDrafts[String(alertId)];
    window.AndonRefreshBus?.notify();
  }
});

boardGrid.addEventListener("input", (event) => {
  const noteField = event.target.closest("[data-board-alert-note]");
  if (!noteField) return;
  const alertId = Number(noteField.dataset.boardAlertId);
  setInlineDraft(alertId, { note: noteField.value });
});

boardMachineGroup.addEventListener("change", syncViewFromControls);
boardDepartment.addEventListener("change", syncViewFromControls);
boardLockView.addEventListener("click", () => {
  state.locked = !state.locked;
  if (state.locked) {
    state.filters.machineGroup = lockedMachineGroup;
  } else if (!state.filters.machineGroup) {
    state.filters.machineGroup = lockedMachineGroup;
  }
  saveView();
  populateFilterOptions();
  renderBoard();
});
boardClearView?.addEventListener("click", clearBoardView);

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadSavedView();
refreshBoard();
startBoardTimerLoop();
document.addEventListener("visibilitychange", handleBoardVisibilityChange);
window.AndonRefreshBus?.onRefresh(scheduleBoardRefresh);
window.AndonRealtime?.onEvent((event) => {
  if (["board_refresh", "alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
    scheduleBoardRefresh();
  }
});
window.AndonRealtime?.onStatus((status) => setBoardFallbackPolling(!status.connected));
setBoardFallbackPolling(!window.AndonRealtime?.connected);
