const boardUrl = "/api/andon/board-state";
const operatorViewStorageKey = "andon-operator-view";

const machineBoard = document.getElementById("machineBoard");
const operatorViewToggle = document.querySelector(".operator-page-header__badge");
const operatorViewPanel = document.getElementById("operatorViewPanel");
const operatorMachineGroupSelect = document.getElementById("operatorMachineGroup");
const operatorMachineSelect = document.getElementById("operatorMachine");
const operatorLockView = document.getElementById("operatorLockView");
const operatorClearView = document.getElementById("operatorClearView");
const operatorViewSummary = document.getElementById("operatorViewSummary");
const machineModal = new bootstrap.Modal(document.getElementById("machineModal"));
const machineModalTitle = document.getElementById("machineModalTitle");
const modalMachineId = document.getElementById("modalMachineId");
const departmentButtons = document.getElementById("departmentButtons");
const problemSection = document.getElementById("problemSection");
const problemList = document.getElementById("problemList");
const operatorNote = document.getElementById("operatorNote");
const sendMessageBtn = document.getElementById("sendMessageBtn");
const operatorAlertModal = new bootstrap.Modal(document.getElementById("operatorAlertModal"));
const operatorAlertModalTitle = document.getElementById("operatorAlertModalTitle");
const operatorAlertIssueSummary = document.getElementById("operatorAlertIssueSummary");
const operatorAlertModalId = document.getElementById("operatorAlertModalId");
const operatorAlertAssigneeSummaryWrap = document.getElementById("operatorAlertAssigneeSummaryWrap");
const operatorAlertAssigneeSummary = document.getElementById("operatorAlertAssigneeSummary");
const operatorAlertNoteSummaryWrap = document.getElementById("operatorAlertNoteSummaryWrap");
const operatorAlertNoteSummary = document.getElementById("operatorAlertNoteSummary");
const operatorAlertUserButtonsWrap = document.getElementById("operatorAlertUserButtonsWrap");
const operatorAlertUserButtons = document.getElementById("operatorAlertUserButtons");
const operatorAlertNote = document.getElementById("operatorAlertNote");
const operatorAlertActionBtn = document.getElementById("operatorAlertActionBtn");

const departmentLabelMap = {
  Maintenance: "Maintenance Needed",
  Materials: "Materials Alert",
  Quality: "Quality Check",
  Supervisor: "Assistance Needed",
  Safety: "Safety",
  Production: "Production",
};

const state = {
  board: { machines: [], filters: { machine_types: [], areas: [], departments: [] } },
  departments: [],
  issueGroups: [],
  users: [],
  selectedMachine: null,
  selectedDepartment: null,
  selectedProblem: null,
  selectedAlertUserId: null,
  selectedAlert: null,
  refreshedAt: null,
  view: {
    machineGroup: "",
    machineId: "",
    locked: false,
  },
};

let elapsedTimerIntervalId = null;
let operatorRefreshTimeoutId = null;
let operatorFallbackPollIntervalId = null;

function normalizeActiveAlert(alert, machine) {
  return {
    id: alert.id,
    department_id: alert.department_id ?? machine?.department_id ?? null,
    department_name: alert.department_name ?? alert.department?.name ?? machine?.department_name ?? null,
    responder_user_id: alert.responder_user_id ?? null,
    responder_name_text: alert.responder_name_text ?? null,
    note: alert.note ?? null,
    created_note: alert.created_note ?? null,
    category_name: alert.category_name ?? alert.issue_category?.name ?? "",
    problem_name: alert.problem_name ?? alert.issue_problem?.name ?? "",
    status: alert.status,
    priority: alert.priority,
    created_at: alert.created_at ?? null,
    elapsed_seconds: alert.elapsed_seconds ?? 0,
    acknowledged_seconds: alert.acknowledged_seconds ?? null,
    ack_to_clear_seconds: alert.ack_to_clear_seconds ?? null,
    color: alert.color ?? alert.issue_category?.color ?? "#ef476f",
  };
}

function startElapsedTimers() {
  if (elapsedTimerIntervalId || document.hidden) return;
  elapsedTimerIntervalId = setInterval(updateElapsedTimers, 1000);
}

function stopElapsedTimers() {
  if (!elapsedTimerIntervalId) return;
  clearInterval(elapsedTimerIntervalId);
  elapsedTimerIntervalId = null;
}

function handleVisibilityChange() {
  if (document.hidden) {
    stopElapsedTimers();
    return;
  }
  startElapsedTimers();
}

async function boot() {
  restoreViewState();
  await loadBoardState();
  normalizeViewState();
  wireEvents();
  renderViewControls();
  renderBoard();
  startElapsedTimers();
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.AndonRefreshBus?.onRefresh(scheduleOperatorRefresh);
  window.AndonRealtime?.onEvent((event) => {
    if (["board_refresh", "alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
      scheduleOperatorRefresh();
    }
  });
  window.AndonRealtime?.onStatus((status) => setOperatorFallbackPolling(!status.connected));
  setOperatorFallbackPolling(!window.AndonRealtime?.connected);
}

async function loadBoardState() {
  const response = await fetch(boardUrl);
  const data = await response.json();
  state.board = data.data || state.board;
  state.departments = state.board.departments || [];
  state.issueGroups = state.board.issue_groups || [];
  state.users = state.board.users || [];
  state.refreshedAt = Date.now();
}

function wireEvents() {
  machineBoard.addEventListener("click", onBoardClick);
  departmentButtons.addEventListener("click", onDepartmentButtonClick);
  problemList.addEventListener("click", onProblemClick);
  sendMessageBtn.addEventListener("click", createAlertFromModal);
  operatorAlertActionBtn.addEventListener("click", actOnActiveAlert);
  operatorMachineGroupSelect.addEventListener("change", onMachineGroupChange);
  operatorMachineSelect.addEventListener("change", onMachineChange);
  operatorLockView.addEventListener("click", toggleOperatorViewLock);
  operatorClearView.addEventListener("click", clearOperatorView);
  document.addEventListener("click", onDocumentClick);
  document.getElementById("machineModal").addEventListener("hidden.bs.modal", resetModal);
  document.getElementById("operatorAlertModal").addEventListener("hidden.bs.modal", resetAlertModal);
}

function onBoardClick(event) {
  const tile = event.target.closest("[data-machine-id]");
  if (!tile) return;
  const machineId = Number(tile.dataset.machineId);
  void openMachineModal(machineId);
}

async function openMachineModal(machineId) {
  let machine = state.board.machines.find((row) => row.id === machineId);
  if (!machine && !state.board.machines.length) {
    await loadBoardState();
    machine = state.board.machines.find((row) => row.id === machineId);
  }
  if (!machine) return;
  if (machine.active_alert) {
    openActiveAlertModal(machine.active_alert, machine);
    return;
  }
  state.selectedMachine = machine;
  state.selectedDepartment = null;
  state.selectedProblem = null;
  modalMachineId.value = String(machine.id);
  machineModalTitle.textContent = machine.name;
  operatorNote.value = "";
  problemSection.hidden = true;
  renderDepartmentButtons();
  renderProblemOptions([]);
  machineModal.show();
}

function openActiveAlertModal(alert, machine) {
  state.selectedAlert = alert;
  state.selectedMachine = machine;
  state.selectedAlertUserId = alert.responder_user_id || null;
  operatorAlertModalTitle.textContent = machine.name;
  operatorAlertIssueSummary.innerHTML = renderIssueSummary(alert.category_name || "", alert.problem_name || "");
  operatorAlertModalId.value = String(alert.id);
  operatorAlertNote.value = "";
  operatorAlertActionBtn.textContent = alert.status === "OPEN" ? "Acknowledge" : "Clear";
  const isOpen = alert.status === "OPEN";
  const hasAssignee = Boolean(!isOpen && alert.responder_name_text);
  const hasConversation = renderAlertNoteThread(operatorAlertNoteSummary, alert);
  if (operatorAlertAssigneeSummaryWrap && operatorAlertAssigneeSummary) {
    operatorAlertAssigneeSummaryWrap.classList.toggle("d-none", !hasAssignee);
    operatorAlertAssigneeSummary.textContent = alert.responder_name_text || "";
  }
  if (operatorAlertNoteSummaryWrap && operatorAlertNoteSummary) {
    operatorAlertNoteSummaryWrap.classList.toggle("d-none", !hasConversation);
  }
  if (operatorAlertUserButtonsWrap) {
    operatorAlertUserButtonsWrap.classList.toggle("d-none", !isOpen);
  }
  renderUserButtons(operatorAlertUserButtons, getRelevantUsersForSelection(machine, { id: alert.department_id, name: alert.department_name }), state.selectedAlertUserId, "alert");
  operatorAlertModal.show();
}

function renderDepartmentButtons() {
  const preferredOrder = Object.keys(departmentLabelMap);
  const departments = [...state.departments].sort((left, right) => {
    const leftIndex = preferredOrder.indexOf(left.name);
    const rightIndex = preferredOrder.indexOf(right.name);
    if (leftIndex === -1 && rightIndex === -1) return left.name.localeCompare(right.name);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
  departmentButtons.innerHTML = departments
    .map((department) => {
      const label = departmentLabelMap[department.name] || department.name;
      return `
        <button type="button" class="btn btn-lg board-category-btn" data-department-id="${department.id}" data-department-name="${escapeHtml(department.name)}">
          <span class="d-block">${escapeHtml(label)}</span>
        </button>`;
    })
    .join("");
  syncDepartmentButtonState();
  syncDepartmentButtonVisibility();
}

function onDepartmentButtonClick(event) {
  const button = event.target.closest("[data-department-name]");
  if (!button) return;
  const departmentName = button.dataset.departmentName;
  const department = state.departments.find((row) => row.name === departmentName);
  if (!department) return;

  const isSelected = state.selectedDepartment && Number(state.selectedDepartment.id) === Number(department.id);
  if (isSelected) {
    state.selectedDepartment = null;
    state.selectedProblem = null;
    problemSection.hidden = true;
    problemList.innerHTML = "";
    syncDepartmentButtonState();
    syncDepartmentButtonVisibility();
    syncProblemButtonState();
    return;
  }

  state.selectedDepartment = department;
  state.selectedProblem = null;
  const group = state.issueGroups.find((entry) => Number(entry.department_id) === Number(department.id));
  const problems = group ? group.problems : [];
  problemSection.hidden = false;
  renderProblemOptions(problems);
  syncDepartmentButtonState();
  syncDepartmentButtonVisibility();
  syncProblemButtonState();
  problemSection.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function renderProblemOptions(problems) {
  problemList.innerHTML = problems.length
    ? problems
        .map(
          (problem) => `
            <button type="button" class="problem-btn" data-problem-id="${problem.id}">
              <span class="problem-btn__name">${escapeHtml(problem.name)}</span>
            </button>`,
        )
        .join("")
    : '<div class="problem-empty">No issues found for this department.</div>';
  syncProblemButtonState();
}

function onProblemClick(event) {
  const button = event.target.closest("[data-problem-id]");
  if (!button) return;
  const problemId = Number(button.dataset.problemId);
  const department = state.selectedDepartment;
  if (!department) return;
  const group = state.issueGroups.find((entry) => Number(entry.department_id) === Number(department.id));
  const problem = group?.problems?.find((entry) => Number(entry.id) === problemId);
  if (!problem) return;
  state.selectedProblem = problem;
  syncProblemButtonState();
}

function getRelevantUsersForSelection(machine, department) {
  const machineGroupName = machine?.machine_type || "";
  const departmentId = department?.id ? Number(department.id) : null;
  const departmentName = department?.name || "";
  return (state.users || []).filter((user) => {
    const matchesDepartment = departmentId
      ? Number(user.department_id || 0) === departmentId
      : departmentName
        ? String(user.department_name || "") === departmentName
        : true;
    const matchesMachineGroup = machineGroupName ? String(user.machine_group_name || "") === machineGroupName : true;
    return matchesDepartment && matchesMachineGroup;
  });
}

function renderUserButtons(container, users, selectedUserId, kind) {
  if (!container) return;
  const validSelected = users.some((user) => Number(user.id) === Number(selectedUserId)) ? selectedUserId : null;
  if (kind === "alert") {
    state.selectedAlertUserId = validSelected;
  }
  if (!users.length) {
    container.innerHTML = '<div class="problem-empty">No users found for this machine and department.</div>';
    return;
  }
  container.innerHTML = users
    .map(
      (user) => `
        <button type="button" class="user-chip ${Number(selectedUserId) === Number(user.id) ? "is-selected" : ""}" data-user-choice="${kind}" data-user-id="${user.id}">
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

async function createAlertFromModal() {
  if (!state.selectedMachine || !state.selectedDepartment || !state.selectedProblem) {
    alert("Select a department and problem.");
    return;
  }
  const payload = {
    machine_id: state.selectedMachine.id,
    department_id: state.selectedDepartment.id,
    issue_problem_id: Number(state.selectedProblem.id),
    operator_name_text: null,
    note: operatorNote.value || null,
  };
  const response = await fetch("/api/andon/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (response.status === 409 && data?.error?.existing_alert) {
    const targetMachine = state.board.machines.find((row) => Number(row.id) === Number(state.selectedMachine.id)) || state.selectedMachine;
    const activeAlert = normalizeActiveAlert(data.error.existing_alert, targetMachine);
    if (targetMachine) {
      targetMachine.active_alert = activeAlert;
    }
    machineModal.hide();
    renderBoard();
    openActiveAlertModal(activeAlert, targetMachine);
    window.AndonRefreshBus?.notify();
    return;
  }
  if (!data.success) {
    alert(data.error.message);
    return;
  }
  const createdAlert = normalizeActiveAlert(data.data, state.selectedMachine);
  const machine = state.board.machines.find((row) => Number(row.id) === Number(state.selectedMachine.id));
  if (machine) {
    machine.active_alert = createdAlert;
  }
  machineModal.hide();
  renderBoard();
  window.AndonRefreshBus?.notify();
}

async function actOnActiveAlert() {
  const alertId = operatorAlertModalId.value;
  const activeAlert = state.selectedAlert;
  if (!alertId || !activeAlert) return;
  const responderUserId = activeAlert.status === "OPEN"
    ? state.selectedAlertUserId
    : activeAlert.responder_user_id || state.selectedAlertUserId;
  const payload = new FormData();
  if (responderUserId) {
    payload.append("responder_user_id", String(responderUserId));
  }
  payload.append("responder_name_text", "");
  if (operatorAlertNote.value.trim()) {
    payload.append("note", operatorAlertNote.value.trim());
  }
  let endpoint = `/api/andon/alerts/${alertId}/acknowledge`;
  if (activeAlert.status !== "OPEN") {
    endpoint = `/api/andon/alerts/${alertId}/cancel`;
    if (operatorAlertNote.value) payload.append("reason", operatorAlertNote.value);
  }
  const response = await fetch(endpoint, { method: "POST", body: payload });
  const data = await response.json();
  if (!data.success) {
    alert(data.error.message);
    return;
  }
  operatorAlertModal.hide();
  state.selectedAlert = null;
  window.AndonRefreshBus?.notify();
}

function renderBoard() {
  const now = Date.now();
  const visibleMachines = getVisibleMachines();
  machineBoard.innerHTML = visibleMachines.length
    ? renderGroupedBoard(visibleMachines, now)
    : renderEmptyBoard();
}

function renderGroupedBoard(rows, now) {
  const groups = new Map();
  rows.forEach((machine) => {
    const key = machine.machine_type || "Unassigned";
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(machine);
  });
  return [...groups.entries()]
    .map(([groupName, machines]) => renderGroupSection(groupName, machines, now))
    .join("");
}

function renderGroupSection(groupName, machines, now) {
  return `
    <section class="machine-group operator-machine-group">
      <div class="operator-machine-group__header">
        <h2 class="h4 mb-0 operator-machine-group__title">${escapeHtml(groupName)}</h2>
        <div class="operator-machine-group__count">${machines.length} machine${machines.length === 1 ? "" : "s"}</div>
      </div>
      <div class="operator-machine-group__grid">
        ${machines.map((machine) => renderMachineTile(machine, now)).join("")}
      </div>
    </section>`;
}

function renderMachineTile(machine, now) {
  const active = Boolean(machine.active_alert);
  const alert = machine.active_alert;
  const elapsedSeconds = active ? Math.max(0, Math.floor(alert.elapsed_seconds || 0)) : 0;
  const elapsed = active ? formatElapsedSeconds(elapsedSeconds) : "";
  const alertColor = alert?.color || "#ef476f";
  const tileStyle = active ? `style="--alert-accent:${alertColor}"` : "";
  return `
    <button class="machine-tile operator-machine-tile ${active ? "machine-tile--alert" : "machine-tile--idle"} ${machine.is_active ? "" : "machine-tile--off"} ${active && alert.status !== "OPEN" ? "machine-tile--working" : ""}" data-machine-id="${machine.id}" ${tileStyle}>
      <div class="machine-tile__name">${escapeHtml(machine.name)}</div>
      ${active ? `
        <div class="machine-tile__alert">${escapeHtml(alert.category_name || "")}${alert.category_name && alert.problem_name ? " - " : ""}${escapeHtml(alert.problem_name || "")}</div>
        ${alert.responder_name_text ? `<div class="machine-tile__user">${escapeHtml(alert.responder_name_text)}</div>` : ""}
        ${alert.note ? `<div class="machine-tile__note">${escapeHtml(alert.note)}</div>` : ""}
        <div class="machine-tile__timer" data-elapsed-seconds="${elapsedSeconds}">${elapsed}</div>
      ` : `
        <div class="machine-tile__idle-text">Healthy</div>
      `}
    </button>`;
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

async function refreshBoardState() {
  await loadBoardState();
  normalizeViewState();
  renderViewControls();
  renderBoard();
  if (state.selectedMachine) {
    renderDepartmentButtons();
    if (state.selectedDepartment) {
      const group = state.issueGroups.find((entry) => Number(entry.department_id) === Number(state.selectedDepartment.id));
      renderProblemOptions(group ? group.problems : []);
    }
  }
}

function scheduleOperatorRefresh() {
  if (operatorRefreshTimeoutId) {
    clearTimeout(operatorRefreshTimeoutId);
  }
  operatorRefreshTimeoutId = setTimeout(() => {
    operatorRefreshTimeoutId = null;
    refreshBoardState();
  }, 150);
}

function setOperatorFallbackPolling(enabled) {
  if (!enabled && operatorFallbackPollIntervalId) {
    clearInterval(operatorFallbackPollIntervalId);
    operatorFallbackPollIntervalId = null;
    return;
  }
  if (enabled && !operatorFallbackPollIntervalId) {
    operatorFallbackPollIntervalId = setInterval(scheduleOperatorRefresh, 15000);
  }
}

function updateElapsedTimers() {
  document.querySelectorAll(".machine-tile__timer[data-elapsed-seconds]").forEach((timer) => {
    const currentSeconds = Number(timer.dataset.elapsedSeconds || "0") + 1;
    timer.dataset.elapsedSeconds = String(currentSeconds);
    timer.textContent = formatElapsedSeconds(currentSeconds);
  });
}

function getVisibleMachines() {
  let machines = [...(state.board.machines || [])];
  if (state.view.machineGroup) {
    machines = machines.filter((machine) => (machine.machine_type || "") === state.view.machineGroup);
  }
  if (state.view.machineId) {
    machines = machines.filter((machine) => Number(machine.id) === Number(state.view.machineId));
  }
  return machines;
}

function renderEmptyBoard() {
  return `
    <div class="board-empty text-center p-4 p-md-5">
      <div class="h4 mb-2">No machines match this view.</div>
      <div class="small text-secondary">Open Screen View or unlock it to show more machines.</div>
    </div>`;
}

function renderViewControls() {
  const machines = [...(state.board.machines || [])].sort((left, right) => left.name.localeCompare(right.name));
  const groups = [...new Set(machines.map((machine) => machine.machine_type).filter(Boolean))].sort((left, right) =>
    left.localeCompare(right),
  );
  const groupExists = !state.view.machineGroup || groups.includes(state.view.machineGroup);
  const selectedGroup = groupExists ? state.view.machineGroup : "";
  const machinePool = selectedGroup
    ? machines.filter((machine) => (machine.machine_type || "") === selectedGroup)
    : machines;

  operatorMachineGroupSelect.innerHTML = `
    <option value="">All Groups</option>
    ${groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("")}
  `;
  operatorMachineGroupSelect.value = selectedGroup;

  operatorMachineSelect.innerHTML = `
    <option value="">All Machines</option>
    ${machinePool
      .map((machine) => `<option value="${machine.id}">${escapeHtml(machine.name)}</option>`)
      .join("")}
  `;
  operatorMachineSelect.value = state.view.machineId && machinePool.some((machine) => Number(machine.id) === Number(state.view.machineId))
    ? state.view.machineId
    : "";

  operatorMachineGroupSelect.disabled = state.view.locked;
  operatorMachineSelect.disabled = state.view.locked;
  operatorLockView.textContent = state.view.locked ? "Unlock View" : "Lock View";
  if (operatorViewSummary) {
    operatorViewSummary.textContent = buildViewSummary();
  }
}

function buildViewSummary() {
  const machine = state.board.machines.find((entry) => Number(entry.id) === Number(state.view.machineId));
  if (state.view.machineId && machine) {
    return state.view.locked ? `Locked to machine: ${machine.name}` : `Selected machine: ${machine.name}`;
  }
  if (state.view.machineGroup) {
    return state.view.locked ? `Locked to group: ${state.view.machineGroup}` : `Selected group: ${state.view.machineGroup}`;
  }
  return state.view.locked ? "Locked to all machines." : "Showing all machines.";
}

function onMachineGroupChange() {
  if (state.view.locked) return;
  state.view.machineGroup = operatorMachineGroupSelect.value;
  state.view.machineId = "";
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function onMachineChange() {
  if (state.view.locked) return;
  const machineId = operatorMachineSelect.value;
  state.view.machineId = machineId;
  if (!machineId) {
    persistOperatorViewState();
    renderViewControls();
    renderBoard();
    return;
  }
  const machine = state.board.machines.find((entry) => Number(entry.id) === Number(machineId));
  state.view.machineGroup = machine?.machine_type || "";
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function toggleOperatorViewLock() {
  state.view.locked = !state.view.locked;
  normalizeViewState();
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function clearOperatorView() {
  state.view.machineGroup = "";
  state.view.machineId = "";
  state.view.locked = false;
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function onDocumentClick(event) {
  if (!operatorViewPanel || !operatorViewToggle) return;
  if (operatorViewPanel.classList.contains("show")) {
    const clickedInsidePanel = operatorViewPanel.contains(event.target);
    const clickedToggle = operatorViewToggle.contains(event.target);
    if (!clickedInsidePanel && !clickedToggle) {
      bootstrap.Collapse.getOrCreateInstance(operatorViewPanel, { toggle: false }).hide();
    }
  }
}

function restoreViewState() {
  try {
    const raw = window.localStorage.getItem(operatorViewStorageKey);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    state.view.machineGroup = typeof parsed.machineGroup === "string" ? parsed.machineGroup : "";
    state.view.machineId = typeof parsed.machineId === "string" || typeof parsed.machineId === "number" ? String(parsed.machineId) : "";
    state.view.locked = Boolean(parsed.locked);
  } catch (_error) {
    state.view.machineGroup = "";
    state.view.machineId = "";
    state.view.locked = false;
  }
}

function persistOperatorViewState() {
  try {
    window.localStorage.setItem(operatorViewStorageKey, JSON.stringify(state.view));
  } catch (_error) {
    // localStorage can be unavailable in locked-down kiosk browsers.
  }
}

function normalizeViewState() {
  const machines = state.board.machines || [];
  const groups = new Set(machines.map((machine) => machine.machine_type).filter(Boolean));
  const selectedMachine = state.view.machineId
    ? machines.find((machine) => Number(machine.id) === Number(state.view.machineId))
    : null;

  if (state.view.machineGroup && !groups.has(state.view.machineGroup)) {
    state.view.machineGroup = "";
  }

  if (state.view.machineId && !selectedMachine) {
    state.view.machineId = "";
  }

  if (selectedMachine) {
    state.view.machineGroup = selectedMachine.machine_type || state.view.machineGroup;
  }

  if (state.view.machineGroup && state.view.machineId && selectedMachine && selectedMachine.machine_type !== state.view.machineGroup) {
    state.view.machineId = "";
  }

  persistOperatorViewState();
}

function resetModal() {
  state.selectedMachine = null;
  state.selectedDepartment = null;
  state.selectedProblem = null;
  modalMachineId.value = "";
  problemList.innerHTML = "";
  problemSection.hidden = true;
  syncDepartmentButtonState();
  syncProblemButtonState();
}

function resetAlertModal() {
  state.selectedAlert = null;
  state.selectedAlertUserId = null;
  operatorAlertModalTitle.textContent = "Alert";
  if (operatorAlertIssueSummary) {
    operatorAlertIssueSummary.innerHTML = "";
  }
  operatorAlertModalId.value = "";
  operatorAlertNote.value = "";
  operatorAlertActionBtn.textContent = "Acknowledge";
  if (operatorAlertAssigneeSummaryWrap) {
    operatorAlertAssigneeSummaryWrap.classList.add("d-none");
  }
  if (operatorAlertAssigneeSummary) {
    operatorAlertAssigneeSummary.textContent = "";
  }
  if (operatorAlertNoteSummaryWrap) {
    operatorAlertNoteSummaryWrap.classList.add("d-none");
  }
  if (operatorAlertNoteSummary) {
    operatorAlertNoteSummary.textContent = "";
  }
  if (operatorAlertUserButtonsWrap) {
    operatorAlertUserButtonsWrap.classList.remove("d-none");
  }
  if (operatorAlertUserButtons) {
    operatorAlertUserButtons.innerHTML = "";
    operatorAlertUserButtons.classList.remove("d-none");
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function syncDepartmentButtonState() {
  departmentButtons.querySelectorAll(".board-category-btn").forEach((button) => {
    const active = state.selectedDepartment && Number(button.dataset.departmentId) === Number(state.selectedDepartment.id);
    button.classList.toggle("is-active", Boolean(active));
  });
}

function syncDepartmentButtonVisibility() {
  departmentButtons.querySelectorAll(".board-category-btn").forEach((button) => {
    const visible = !state.selectedDepartment || Number(button.dataset.departmentId) === Number(state.selectedDepartment.id);
    button.hidden = !visible;
  });
}

function syncProblemButtonState() {
  problemList.querySelectorAll(".problem-btn").forEach((button) => {
    const active = state.selectedProblem && Number(button.dataset.problemId) === Number(state.selectedProblem.id);
    button.classList.toggle("is-active", Boolean(active));
  });
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-user-choice]");
  if (!button) return;
  const userId = Number(button.dataset.userId);
  if (button.dataset.userChoice === "alert") {
    state.selectedAlertUserId = Number.isFinite(userId) ? userId : null;
    renderUserButtons(operatorAlertUserButtons, getRelevantUsersForSelection(state.selectedMachine, { id: state.selectedAlert?.department_id, name: state.selectedAlert?.department_name }), state.selectedAlertUserId, "alert");
  }
});


boot();
