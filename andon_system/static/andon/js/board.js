const boardGrid = document.getElementById("boardGrid");
const boardViewToggle = document.querySelector(".board-view-toggle");
const boardViewPanel = document.getElementById("boardViewPanel");
const boardMachineGroup = document.getElementById("boardMachineGroup");
const boardDepartment = document.getElementById("boardDepartment");
const boardLockView = document.getElementById("boardLockView");
const boardClearView = document.getElementById("boardClearView");
const boardLockStatus = document.getElementById("boardLockStatus");
const boardAlertModal = new bootstrap.Modal(document.getElementById("boardAlertModal"));
const boardAlertModalTitle = document.getElementById("boardAlertModalTitle");
const boardAlertIssueSummary = document.getElementById("boardAlertIssueSummary");
const boardAlertModalId = document.getElementById("boardAlertModalId");
const boardAlertAssigneeSummaryWrap = document.getElementById("boardAlertAssigneeSummaryWrap");
const boardAlertAssigneeSummary = document.getElementById("boardAlertAssigneeSummary");
const boardAlertNoteSummaryWrap = document.getElementById("boardAlertNoteSummaryWrap");
const boardAlertNoteSummary = document.getElementById("boardAlertNoteSummary");
const boardAlertUserButtonsWrap = document.getElementById("boardAlertUserButtonsWrap");
const boardAlertUserButtons = document.getElementById("boardAlertUserButtons");
const boardAlertNote = document.getElementById("boardAlertNote");
const boardActionBtn = document.getElementById("boardActionBtn");

const storageKey = "andon-board-view";

const state = {
  alerts: [],
  boardState: { machines: [], departments: [] },
  users: [],
  filters: {
    machineGroup: "",
    department: "",
  },
  locked: false,
  selectedAlert: null,
  selectedResponderUserId: null,
};

setInterval(updateBoardTimers, 1000);

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
    state.filters.machineGroup = saved.machineGroup || "";
    state.filters.department = saved.department || "";
    state.locked = Boolean(saved.locked);
  } catch {
    state.filters.machineGroup = "";
    state.filters.department = "";
    state.locked = false;
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

function renderUserButtons(users, selectedUserId) {
  if (!boardAlertUserButtons) return;
  const validSelected = users.some((user) => Number(user.id) === Number(selectedUserId)) ? selectedUserId : null;
  state.selectedResponderUserId = validSelected;
  if (!users.length) {
    boardAlertUserButtons.innerHTML = '<div class="problem-empty">No users found for this machine and department.</div>';
    return;
  }
  boardAlertUserButtons.innerHTML = users
    .map(
      (user) => `
        <button type="button" class="user-chip ${Number(selectedUserId) === Number(user.id) ? "is-selected" : ""}" data-board-user-id="${user.id}">
          <span class="user-chip__name">${escapeHtml(user.display_name)}</span>
          <span class="user-chip__meta">${escapeHtml(user.work_id || "")}</span>
        </button>`,
    )
    .join("");
}

function renderAlertNoteThread(container, alert) {
  if (!container) return false;
  const createdNote = String(alert?.created_note || "").trim();
  const currentNote = String(alert?.note || "").trim();
  const responderName = String(alert?.responder_name_text || "").trim();
  const bubbles = [];
  if (createdNote) {
    bubbles.push({
      side: "left",
      label: "Request",
      text: createdNote,
    });
  }
  if (currentNote && currentNote !== createdNote) {
    bubbles.push({
      side: "right",
      label: responderName || "Response",
      text: currentNote,
    });
  }
  if (!bubbles.length && currentNote) {
    bubbles.push({
      side: "left",
      label: "Note",
      text: currentNote,
    });
  }
  if (!bubbles.length) {
    container.innerHTML = "";
    return false;
  }
  container.innerHTML = bubbles
    .map(
      (bubble) => `
        <div class="alert-note-bubble ${bubble.side === "right" ? "is-right" : "is-left"}">
          <div class="alert-note-bubble__label">${escapeHtml(bubble.label)}</div>
          <div class="alert-note-bubble__text">${escapeHtml(bubble.text)}</div>
        </div>`,
    )
    .join("");
  return true;
}

function renderIssueSummary(categoryName, problemName) {
  const category = String(categoryName || "").trim();
  const problem = String(problemName || "").trim();
  const summary = [category, problem].filter(Boolean).join(" - ");
  if (!summary) return "";
  return `
    <div class="alert-issue-summary__label">Issue Summary</div>
    <div class="alert-issue-summary__value">${escapeHtml(summary)}</div>`;
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

function cardTemplate(alert) {
  const issue = [alert.department?.name || "", alert.issue_problem?.name || ""].filter(Boolean).join(" - ");
  const elapsedSeconds = Math.max(0, Math.floor(alert.elapsed_seconds || 0));
  return `
    <button type="button" class="board-card h-100 board-alert-card ${statusClass(alert.status)}" data-alert-id="${alert.id}">
      <div class="board-alert-card__top">
        <h3 class="board-alert-card__title">${escapeHtml(alert.machine?.name || "")}</h3>
        <span class="status-pill ${statusClass(alert.status)}">${statusLabel(alert.status)}</span>
      </div>
      <div class="board-alert-card__issue">${escapeHtml(issue)}</div>
      ${alert.responder_name_text ? `<div class="board-alert-card__user">${escapeHtml(alert.responder_name_text)}</div>` : ""}
      ${alert.note ? `<div class="board-alert-card__note">${escapeHtml(alert.note)}</div>` : ""}
      <div class="alert-timer board-alert-card__timer" data-elapsed-seconds="${elapsedSeconds}">${formatElapsedSeconds(elapsedSeconds)}</div>
    </button>`;
}

function renderDepartmentGroup(departmentName, alerts) {
  return `
    <section class="board-card board-department-panel">
      <div class="board-department-panel__header">
        <h3 class="board-department-panel__title">${escapeHtml(departmentName)}</h3>
        <div class="board-department-panel__count">${alerts.length} alerts</div>
      </div>
      <div class="board-department-panel__grid">
        ${alerts.map(cardTemplate).join("")}
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
        <h2 class="board-group__title">${escapeHtml(machineGroupName)}</h2>
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
  const currentGroup = state.filters.machineGroup;
  const currentDepartment = state.filters.department;

  boardMachineGroup.innerHTML = '<option value="">All Groups</option>' + groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("");
  boardDepartment.innerHTML = '<option value="">All Departments</option>' + departments.map((department) => `<option value="${escapeHtml(department)}">${escapeHtml(department)}</option>`).join("");
  boardMachineGroup.value = currentGroup;
  boardDepartment.value = currentDepartment;
  boardMachineGroup.disabled = state.locked;
  boardDepartment.disabled = state.locked;
  boardLockView.textContent = state.locked ? "Unlock View" : "Lock View";
  boardLockStatus.textContent = state.locked ? "Locked" : "Unlocked";
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
    if (statusText === "Working on it") return;
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

function syncViewFromControls() {
  state.filters.machineGroup = boardMachineGroup.value || "";
  state.filters.department = boardDepartment.value || "";
  saveView();
  renderBoard();
}

function clearBoardView() {
  state.filters.machineGroup = "";
  state.filters.department = "";
  state.locked = false;
  saveView();
  populateFilterOptions();
  renderBoard();
}

function openAlertModal(alertId) {
  const alert = state.alerts.find((item) => Number(item.id) === Number(alertId));
  if (!alert) return;
  state.selectedAlert = alert;
  state.selectedResponderUserId = alert.responder_user_id || null;
  boardAlertModalTitle.textContent = alert.machine?.name || "Alert";
  boardAlertIssueSummary.innerHTML = renderIssueSummary(alert.department?.name || "", alert.issue_problem?.name || "");
  boardAlertModalId.value = String(alert.id);
  boardAlertNote.value = "";
  boardActionBtn.textContent = alert.status === "OPEN" ? "Acknowledge" : "Clear";
  const isOpen = alert.status === "OPEN";
  const hasAssignee = Boolean(!isOpen && alert.responder_name_text);
  const hasConversation = renderAlertNoteThread(boardAlertNoteSummary, alert);
  if (boardAlertAssigneeSummaryWrap && boardAlertAssigneeSummary) {
    boardAlertAssigneeSummaryWrap.classList.toggle("d-none", !hasAssignee);
    boardAlertAssigneeSummary.textContent = alert.responder_name_text || "";
  }
  if (boardAlertNoteSummaryWrap && boardAlertNoteSummary) {
    boardAlertNoteSummaryWrap.classList.toggle("d-none", !hasConversation);
  }
  if (boardAlertUserButtonsWrap) {
    boardAlertUserButtonsWrap.classList.toggle("d-none", !isOpen);
  }
  renderUserButtons(getRelevantUsers(alert.machine, { id: alert.department?.id, name: alert.department?.name }), state.selectedResponderUserId);
  boardAlertModal.show();
}

boardGrid.addEventListener("click", (event) => {
  const tile = event.target.closest("[data-alert-id]");
  if (!tile) return;
  openAlertModal(tile.dataset.alertId);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-board-user-id]");
  if (!button) return;
  state.selectedResponderUserId = Number(button.dataset.boardUserId);
  renderUserButtons(getRelevantUsers(state.selectedAlert?.machine, { id: state.selectedAlert?.department?.id, name: state.selectedAlert?.department?.name }), state.selectedResponderUserId);
});

boardMachineGroup.addEventListener("change", syncViewFromControls);
boardDepartment.addEventListener("change", syncViewFromControls);
boardLockView.addEventListener("click", () => {
  state.locked = !state.locked;
  saveView();
  populateFilterOptions();
});
boardClearView?.addEventListener("click", clearBoardView);

document.addEventListener("click", onDocumentClick);

boardActionBtn.addEventListener("click", async () => {
  const alertId = boardAlertModalId.value;
  const activeAlert = state.selectedAlert;
  if (!alertId) return;
  const responderUserId = activeAlert?.status === "OPEN"
    ? state.selectedResponderUserId
    : activeAlert?.responder_user_id || state.selectedResponderUserId;
  const payload = new FormData();
  if (responderUserId) {
    payload.append("responder_user_id", String(responderUserId));
  }
  payload.append("responder_name_text", "");
  if (boardAlertNote.value.trim()) {
    payload.append("note", boardAlertNote.value.trim());
  }
  let endpoint = `/api/andon/alerts/${alertId}/acknowledge`;
  if (activeAlert?.status !== "OPEN") {
    endpoint = `/api/andon/alerts/${alertId}/cancel`;
    if (boardAlertNote.value) payload.append("reason", boardAlertNote.value);
  }
  const response = await fetch(endpoint, { method: "POST", body: payload });
  const data = await response.json();
  if (!data.success) {
    alert(data.error.message);
    return;
  }
  boardAlertModal.hide();
  state.selectedAlert = null;
  window.AndonRefreshBus?.notify();
});

document.getElementById("boardAlertModal").addEventListener("hidden.bs.modal", () => {
  state.selectedAlert = null;
  state.selectedResponderUserId = null;
  boardActionBtn.textContent = "Acknowledge";
  if (boardAlertIssueSummary) {
    boardAlertIssueSummary.innerHTML = "";
  }
  boardAlertNote.value = "";
  if (boardAlertAssigneeSummaryWrap) {
    boardAlertAssigneeSummaryWrap.classList.add("d-none");
  }
  if (boardAlertAssigneeSummary) {
    boardAlertAssigneeSummary.textContent = "";
  }
  if (boardAlertNoteSummaryWrap) {
    boardAlertNoteSummaryWrap.classList.add("d-none");
  }
  if (boardAlertNoteSummary) {
    boardAlertNoteSummary.textContent = "";
  }
  if (boardAlertUserButtonsWrap) {
    boardAlertUserButtonsWrap.classList.remove("d-none");
  }
  if (boardAlertUserButtons) {
    boardAlertUserButtons.innerHTML = "";
    boardAlertUserButtons.classList.remove("d-none");
  }
});

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
window.AndonRefreshBus?.onRefresh(refreshBoard);

function onDocumentClick(event) {
  if (!boardViewPanel || !boardViewToggle) return;
  if (boardViewPanel.classList.contains("show")) {
    const clickedInsidePanel = boardViewPanel.contains(event.target);
    const clickedToggle = boardViewToggle.contains(event.target);
    if (!clickedInsidePanel && !clickedToggle) {
      bootstrap.Collapse.getOrCreateInstance(boardViewPanel, { toggle: false }).hide();
    }
  }
}
