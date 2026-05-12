const boardUrl = "/api/andon/operator-snapshot";
const operatorMetadataUrl = "/api/andon/operator-metadata";
const operatorViewStorageKey = "andon-operator-view";

const machineBoard = document.getElementById("machineBoard");
const operatorViewToggle = document.querySelector(".operator-page-header__badge");
const operatorViewPanel = document.getElementById("operatorViewPanel");
const operatorMachineGroupSelect = document.getElementById("operatorMachineGroup");
const operatorMachineSelect = document.getElementById("operatorMachine");
const operatorLockView = document.getElementById("operatorLockView");
const operatorClearView = document.getElementById("operatorClearView");
const operatorViewSummary = document.getElementById("operatorViewSummary");
const operatorStatusDock = document.getElementById("operatorStatusDock");

const departmentLabelMap = {
  Maintenance: "Maintenance",
  Materials: "Materials Alert",
  Quality: "Quality Check",
  Supervisor: "Supervisor",
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
  createNoteDraft: "",
  alertNoteDraft: "",
  refreshedAt: null,
  metadataLoaded: false,
  view: {
    machineGroup: "",
    machineId: "",
    locked: false,
  },
};

let elapsedTimerIntervalId = null;
let operatorRefreshTimeoutId = null;
let operatorFallbackPollIntervalId = null;
let operatorCreateTransitionFrameId = null;
let operatorRefreshInFlight = false;
let operatorRefreshQueued = false;
let liveTimerNodes = [];
let operatorMetadataLoadPromise = null;

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
  try {
    await loadOperatorMetadata();
  } catch (_error) {
    console.warn("Failed to preload operator metadata.");
  }
  normalizeViewState();
  wireEvents();
  renderViewControls();
  renderBoard();
  startElapsedTimers();
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.AndonRefreshBus?.onRefresh(scheduleOperatorRefresh);
  window.AndonRealtime?.onEvent((event) => {
    if (["board_refresh", "alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
      if (event.type === "admin_metadata_updated") {
        state.metadataLoaded = false;
        operatorMetadataLoadPromise = null;
        if (state.selectedMachine || state.selectedAlert) {
          void loadOperatorMetadata().then(renderBoard).catch(() => {});
        }
      }
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
  state.refreshedAt = Date.now();
}

async function loadOperatorMetadata() {
  if (state.metadataLoaded) return;
  if (!operatorMetadataLoadPromise) {
    operatorMetadataLoadPromise = (async () => {
      const response = await fetch(operatorMetadataUrl);
      const data = await response.json();
      const metadata = data.data || {};
      state.departments = metadata.departments || [];
      state.issueGroups = metadata.issue_groups || [];
      state.users = metadata.users || [];
      state.metadataLoaded = true;
    })().finally(() => {
      operatorMetadataLoadPromise = null;
    });
  }
  await operatorMetadataLoadPromise;
}

async function ensureOperatorMetadataLoaded() {
  if (state.metadataLoaded) return;
  await loadOperatorMetadata();
}

function wireEvents() {
  machineBoard.addEventListener("click", onBoardClick);
  machineBoard.addEventListener("input", onBoardInput);
  operatorMachineGroupSelect.addEventListener("change", onMachineGroupChange);
  operatorMachineSelect.addEventListener("change", onMachineChange);
  operatorLockView.addEventListener("click", toggleOperatorViewLock);
  operatorClearView.addEventListener("click", clearOperatorView);
  document.addEventListener("click", onDocumentClick);
}

function onBoardClick(event) {
  const toggle = event.target.closest("[data-machine-toggle]");
  if (toggle) {
    void toggleMachinePanel(Number(toggle.dataset.machineId));
    return;
  }

  const departmentButton = event.target.closest("[data-department-name]");
  if (departmentButton) {
    onDepartmentButtonClick(departmentButton);
    return;
  }

  const problemButton = event.target.closest("[data-problem-id]");
  if (problemButton) {
    onProblemClick(problemButton);
    return;
  }

  const userButton = event.target.closest("[data-user-choice]");
  if (userButton) {
    onUserChoiceClick(userButton);
    return;
  }

  const actionButton = event.target.closest("[data-inline-action]");
  if (!actionButton) return;
  const action = actionButton.dataset.inlineAction;
  if (action === "send-message") {
    void createAlertFromModal();
  } else if (action === "act-on-alert") {
    void actOnActiveAlert();
  } else if (action === "close-panel") {
    closeMachinePanel();
  }
}

function onBoardInput(event) {
  const target = event.target;
  if (!(target instanceof HTMLTextAreaElement)) return;
  if (target.dataset.noteKind === "create") {
    state.createNoteDraft = target.value;
    return;
  }
  if (target.dataset.noteKind === "alert") {
    state.alertNoteDraft = target.value;
  }
}

async function toggleMachinePanel(machineId) {
  let machine = state.board.machines.find((row) => row.id === machineId);
  if (!machine && !state.board.machines.length) {
    await loadBoardState();
    machine = state.board.machines.find((row) => row.id === machineId);
  }
  if (!machine) return;
  const isOpenMachine = state.selectedMachine && Number(state.selectedMachine.id) === Number(machine.id);
  if (isOpenMachine) {
    closeMachinePanel();
    return;
  }
  if (machine.active_alert) {
    await openActiveAlertModal(machine.active_alert, machine);
    return;
  }
  await openCreatePanel(machine);
}

async function openActiveAlertModal(activeAlert, machine) {
  try {
    await ensureOperatorMetadataLoaded();
  } catch (_error) {
    window.alert("Unable to load operator metadata.");
    return;
  }
  state.selectedAlert = activeAlert;
  state.selectedMachine = machine;
  state.selectedAlertUserId = activeAlert.responder_user_id || null;
  state.selectedDepartment = null;
  state.selectedProblem = null;
  state.createNoteDraft = "";
  state.alertNoteDraft = "";
  renderBoard();
}

async function openCreatePanel(machine) {
  try {
    await ensureOperatorMetadataLoaded();
  } catch (_error) {
    window.alert("Unable to load operator metadata.");
    return;
  }
  state.selectedMachine = machine;
  state.selectedAlert = null;
  state.selectedAlertUserId = null;
  state.selectedDepartment = null;
  state.selectedProblem = null;
  state.createNoteDraft = "";
  state.alertNoteDraft = "";
  renderBoard();
}

function closeMachinePanel() {
  state.selectedMachine = null;
  state.selectedAlert = null;
  state.selectedAlertUserId = null;
  state.selectedDepartment = null;
  state.selectedProblem = null;
  state.createNoteDraft = "";
  state.alertNoteDraft = "";
  renderBoard();
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

function onDepartmentButtonClick(button) {
  if (!button) return;
  const tile = button.closest(".operator-machine-tile");
  const machineId = Number(tile?.dataset.machineId);
  const machine = state.board.machines.find((row) => Number(row.id) === machineId) || null;
  const departmentName = button.dataset.departmentName;
  const department = state.departments.find((row) => row.name === departmentName);
  if (!department) return;
  state.selectedMachine = machine || state.selectedMachine;

  const isSelected = state.selectedDepartment && Number(state.selectedDepartment.id) === Number(department.id);
  if (isSelected) {
    state.selectedDepartment = null;
    state.selectedProblem = null;
    renderBoard();
    return;
  }

  state.selectedDepartment = department;
  state.selectedProblem = null;
  renderBoard();
}

function onProblemClick(button) {
  if (!button) return;
  const tile = button.closest(".operator-machine-tile");
  const machineId = Number(tile?.dataset.machineId);
  const machine = state.board.machines.find((row) => Number(row.id) === machineId) || null;
  const problemId = Number(button.dataset.problemId);
  const department = state.selectedDepartment;
  if (!department) return;
  const group = state.issueGroups.find((entry) => Number(entry.department_id) === Number(department.id));
  const problem = group?.problems?.find((entry) => Number(entry.id) === problemId);
  if (!problem) return;
  state.selectedMachine = machine || state.selectedMachine;
  state.selectedProblem = problem;
  syncProblemButtonState();
  syncCreateSubmitState();
}

function onUserChoiceClick(button) {
  if (!button) return;
  const tile = button.closest(".operator-machine-tile");
  const machineId = Number(tile?.dataset.machineId);
  const machine = state.board.machines.find((row) => Number(row.id) === machineId) || null;
  const userId = Number(button.dataset.userId);
  if (button.dataset.userChoice === "alert") {
    state.selectedMachine = machine || state.selectedMachine;
    state.selectedAlert = machine?.active_alert || state.selectedAlert;
    state.selectedAlertUserId = Number.isFinite(userId) ? userId : null;
    renderBoard();
  }
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
    note: state.createNoteDraft.trim() || null,
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
    await openActiveAlertModal(activeAlert, targetMachine);
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
  closeMachinePanel();
  window.AndonRefreshBus?.notify();
}

async function actOnActiveAlert() {
  const activeAlert = state.selectedAlert || state.board.machines.find((machine) => Number(machine.id) === Number(state.selectedMachine?.id))?.active_alert;
  const alertId = activeAlert?.id;
  if (!alertId || !activeAlert) return;
  const responderUserId = activeAlert.status === "OPEN"
    ? state.selectedAlertUserId
    : activeAlert.responder_user_id || state.selectedAlertUserId;
  const payload = new FormData();
  if (responderUserId) {
    payload.append("responder_user_id", String(responderUserId));
  }
  payload.append("responder_name_text", "");
  if (state.alertNoteDraft.trim()) {
    payload.append("note", state.alertNoteDraft.trim());
  }
  let endpoint = `/api/andon/alerts/${alertId}/acknowledge`;
  if (activeAlert.status !== "OPEN") {
    endpoint = `/api/andon/alerts/${alertId}/cancel`;
    if (state.alertNoteDraft) payload.append("reason", state.alertNoteDraft);
  }
  const response = await fetch(endpoint, { method: "POST", body: payload });
  const data = await response.json();
  if (!data.success) {
    alert(data.error.message);
    return;
  }
  closeMachinePanel();
  window.AndonRefreshBus?.notify();
}

function renderBoard() {
  const visibleMachines = getVisibleMachines();
  const detailed = isDetailedOperatorView();
  const boardKey = buildBoardKey(visibleMachines, detailed);
  machineBoard.dataset.machineCount = String(visibleMachines.length);
  machineBoard.dataset.viewMode = detailed ? "detailed" : "compact";
  applyDetailedBoardDensity(visibleMachines.length, detailed);
  if (!visibleMachines.length) {
    machineBoard.innerHTML = renderEmptyBoard();
    machineBoard.dataset.boardKey = boardKey;
    syncLiveTimerNodes();
    renderStatusDock(visibleMachines, detailed);
    primeCreatePanelTransitions();
    return;
  }

  const patched = machineBoard.dataset.boardKey === boardKey && patchBoardTiles(visibleMachines, detailed);
  if (!patched) {
    machineBoard.innerHTML = renderGroupedBoard(visibleMachines, detailed);
  }
  machineBoard.dataset.boardKey = boardKey;
  syncLiveTimerNodes();
  renderStatusDock(visibleMachines, detailed);
  primeCreatePanelTransitions();
}

function buildBoardKey(visibleMachines, detailed) {
  return `${detailed ? "detailed" : "compact"}:${visibleMachines.map((machine) => machine.id).join(",")}`;
}

function buildMachineTileSignature(machine, detailed) {
  const active = Boolean(machine.active_alert);
  const alert = machine.active_alert;
  const selectedMachineId = state.selectedMachine ? Number(state.selectedMachine.id) : null;
  const selectedDepartmentId = state.selectedDepartment ? Number(state.selectedDepartment.id) : null;
  const selectedProblemId = state.selectedProblem ? Number(state.selectedProblem.id) : null;
  const selectedAlertId = state.selectedAlert ? Number(state.selectedAlert.id) : null;
  const isSelectedMachine = Number(machine.id) === selectedMachineId;
  const isSelectedAlert = active && selectedAlertId === Number(alert.id);
  const metadataState = isSelectedMachine || isSelectedAlert ? (state.metadataLoaded ? "meta" : "nometa") : "";
  const createDraftState = !active && isSelectedMachine ? state.createNoteDraft : "";
  const alertDraftState = isSelectedAlert ? state.alertNoteDraft : "";
  const alertUserState = isSelectedAlert ? String(state.selectedAlertUserId || "") : "";
  return [
    detailed ? "d" : "c",
    active ? "a" : "i",
    machine.is_active ? "1" : "0",
    active ? String(alert.status || "") : "",
    active ? String(alert.id || "") : "",
    active ? String(Math.max(0, Math.floor(alert.elapsed_seconds || 0))) : "",
    active ? String(alert.acknowledged_seconds ?? "") : "",
    active ? String(alert.responder_user_id ?? "") : "",
    active ? String(alert.responder_name_text || "") : "",
    isSelectedMachine ? "m" : "",
    isSelectedMachine && selectedDepartmentId ? `dep:${selectedDepartmentId}` : "",
    isSelectedMachine && selectedProblemId ? `prob:${selectedProblemId}` : "",
    isSelectedAlert ? "sel" : "",
    createDraftState,
    alertDraftState,
    alertUserState,
    metadataState,
  ].join("|");
}

function patchBoardTiles(visibleMachines, detailed) {
  const tiles = machineBoard.querySelectorAll(".operator-machine-tile");
  if (tiles.length !== visibleMachines.length) return false;

  for (let index = 0; index < visibleMachines.length; index += 1) {
    const machine = visibleMachines[index];
    const tile = tiles[index];
    if (!tile || Number(tile.dataset.machineId) !== Number(machine.id)) {
      return false;
    }
    const nextSignature = buildMachineTileSignature(machine, detailed);
    if (tile.dataset.renderSignature !== nextSignature) {
      tile.outerHTML = renderMachineTile(machine, detailed);
    }
  }

  return true;
}

function syncLiveTimerNodes() {
  liveTimerNodes = Array.from(machineBoard.querySelectorAll('.machine-tile__timer[data-live-timer="true"][data-elapsed-seconds]'));
}

function primeCreatePanelTransitions() {
  if (operatorCreateTransitionFrameId) {
    cancelAnimationFrame(operatorCreateTransitionFrameId);
  }
  operatorCreateTransitionFrameId = requestAnimationFrame(() => {
    operatorCreateTransitionFrameId = null;
    document
      .querySelectorAll('.machine-tile__inline-panel--create[data-followup="true"]')
      .forEach((panel) => panel.classList.add("is-followup-active"));
    document
      .querySelectorAll('.operator-machine-tile[data-create-followup="true"]')
      .forEach((tile) => tile.classList.add("is-followup-active"));
  });
}

function applyDetailedBoardDensity(machineCount, detailed) {
  if (!machineBoard) return;
  if (!detailed || !machineCount) {
    machineBoard.style.removeProperty("--operator-detailed-columns");
    machineBoard.style.removeProperty("--operator-detailed-card-min-height");
    machineBoard.style.removeProperty("--operator-detailed-request-square-min-height");
    return;
  }

  let columns = 1;
  let cardMinHeight = "min(72vh, 52rem)";
  let requestSquareMinHeight = "min(18vh, 13rem)";

  if (machineCount === 1) {
    columns = 1;
    cardMinHeight = "min(86vh, 64rem)";
    requestSquareMinHeight = "min(28vh, 18rem)";
  } else if (machineCount === 2) {
    columns = 2;
    cardMinHeight = "min(72vh, 50rem)";
    requestSquareMinHeight = "min(24vh, 16rem)";
  } else if (machineCount <= 4) {
    columns = 2;
    cardMinHeight = "min(64vh, 44rem)";
    requestSquareMinHeight = "min(22vh, 14rem)";
  } else if (machineCount <= 8) {
    columns = 4;
    cardMinHeight = "min(48vh, 34rem)";
    requestSquareMinHeight = "min(18vh, 12rem)";
  } else {
    columns = 4;
    cardMinHeight = "min(42vh, 30rem)";
    requestSquareMinHeight = "min(16vh, 10rem)";
  }

  machineBoard.style.setProperty("--operator-detailed-columns", String(columns));
  machineBoard.style.setProperty("--operator-detailed-card-min-height", cardMinHeight);
  machineBoard.style.setProperty("--operator-detailed-request-square-min-height", requestSquareMinHeight);
}

function renderGroupedBoard(rows, detailed) {
  const groups = new Map();
  rows.forEach((machine) => {
    const key = machine.machine_type || "Unassigned";
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(machine);
  });
  return [...groups.entries()]
    .map(([groupName, machines]) => renderGroupSection(groupName, machines, detailed))
    .join("");
}

function renderGroupSection(groupName, machines, detailed) {
  return `
    <section class="machine-group operator-machine-group ${detailed ? "operator-machine-group--detailed" : ""}">
      <div class="operator-machine-group__grid ${detailed ? "operator-machine-group__grid--detailed" : ""}">
        ${machines.map((machine) => renderMachineTile(machine, detailed)).join("")}
      </div>
    </section>`;
}

function renderMachineTile(machine, detailed) {
  const active = Boolean(machine.active_alert);
  const alert = machine.active_alert;
  const elapsedSeconds = active ? Math.max(0, Math.floor(alert.elapsed_seconds || 0)) : 0;
  const alertColor = alert?.color || "#ef476f";
  const tileStyle = active ? `style="--alert-accent:${alertColor}"` : "";
  const isOpen = active && alert.status === "OPEN";
  const isCreateFollowup = !active
    && state.selectedMachine
    && Number(state.selectedMachine.id) === Number(machine.id)
    && Boolean(state.selectedDepartment);
  const topTone = !active ? "healthy" : isOpen ? "open" : "warning";
  const renderSignature = buildMachineTileSignature(machine, detailed);
  return `
    <article class="machine-tile operator-machine-tile ${active ? "machine-tile--alert" : "machine-tile--idle"} ${machine.is_active ? "" : "machine-tile--off"} ${active && alert.status !== "OPEN" ? "machine-tile--working" : ""} ${isCreateFollowup ? "machine-tile--create-followup" : ""} ${detailed ? "operator-machine-tile--detailed" : ""}" ${tileStyle} data-machine-id="${machine.id}" data-create-followup="${isCreateFollowup ? "true" : "false"}" data-render-signature="${escapeHtml(renderSignature)}">
      <div class="machine-tile__top machine-tile__top--tone-${topTone} ${detailed ? "machine-tile__top--detailed" : ""}">
        <div class="machine-tile__identity ${detailed ? "machine-tile__identity--detailed" : ""}">
          <div class="machine-tile__name">${escapeHtml(machine.name)}</div>
        </div>
        <span class="status-pill ${active ? statusClass(alert.status) : "status-healthy"}">${active ? statusLabel(alert.status) : "Healthy"}</span>
      </div>
      ${active ? renderAlertInlinePanel(machine, alert, detailed) : renderCreateInlinePanel(machine, detailed)}
    </article>`;
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

function formatAlertElapsedDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  const parts = [];

  if (days > 0) {
    parts.push(`${days}d`);
  }

  parts.push(`${days > 0 ? String(hours).padStart(2, "0") : hours}h`);
  parts.push(`${String(minutes).padStart(2, "0")}m`);
  parts.push(`${String(remainingSeconds).padStart(2, "0")}s`);

  return parts.join(" ");
}

function statusClass(status) {
  return `status-${String(status || "").toLowerCase()}`;
}

function statusLabel(status) {
  if (status === "ACKNOWLEDGED") return "Working";
  return String(status || "");
}

function isDetailedOperatorView() {
  return Boolean(state.view.machineGroup || state.view.machineId);
}

function getGroupSummary(machines) {
  const activeAlerts = machines.filter((machine) => Boolean(machine.active_alert));
  return {
    activeAlerts: activeAlerts.length,
    healthyMachines: machines.length - activeAlerts.length,
  };
}

function renderDepartmentButtonsMarkup() {
  const preferredOrder = Object.keys(departmentLabelMap);
  const departments = [...state.departments].sort((left, right) => {
    const leftIndex = preferredOrder.indexOf(left.name);
    const rightIndex = preferredOrder.indexOf(right.name);
    if (leftIndex === -1 && rightIndex === -1) return left.name.localeCompare(right.name);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
  return departments
    .map((department) => {
      const label = departmentLabelMap[department.name] || department.name;
      const isSelected = state.selectedDepartment && Number(state.selectedDepartment.id) === Number(department.id);
      const iconClass = getDepartmentIconClass(department.name);
      return `
        <button type="button" class="btn btn-lg board-category-btn${isSelected ? " is-active" : ""}" data-department-id="${department.id}" data-department-name="${escapeHtml(department.name)}">
          <span class="board-category-btn__icon" aria-hidden="true"><i class="${iconClass}"></i></span>
          <span class="d-block">${escapeHtml(label)}</span>
        </button>`;
    })
    .join("");
}

function renderProblemOptionsMarkup(problems) {
  return problems.length
    ? problems
        .map(
          (problem) => `
            <button type="button" class="problem-btn${state.selectedProblem && Number(state.selectedProblem.id) === Number(problem.id) ? " is-active" : ""}" data-problem-id="${problem.id}">
              <span class="problem-btn__name">${escapeHtml(problem.name)}</span>
            </button>`,
        )
        .join("")
    : '<div class="problem-empty">No issues found for this department.</div>';
}

function getDepartmentIconClass(name) {
  switch (String(name || "").toLowerCase()) {
    case "maintenance":
      return "bi bi-tools";
    case "materials":
      return "bi bi-box-seam";
    case "quality":
      return "bi bi-patch-check-fill";
    case "supervisor":
      return "bi bi-person-badge-fill";
    case "safety":
      return "bi bi-shield-exclamation";
    case "production":
      return "bi bi-gear-wide-connected";
    default:
      return "bi bi-dot";
  }
}

function renderUserButtonsMarkup(users, selectedUserId, kind) {
  const validSelected = users.some((user) => Number(user.id) === Number(selectedUserId)) ? selectedUserId : null;
  if (!users.length) {
    return '<div class="problem-empty">No users found for this machine and department.</div>';
  }
  return users
    .map(
      (user) => `
        <button type="button" class="user-chip ${Number(validSelected) === Number(user.id) ? "is-selected" : ""}" data-user-choice="${kind}" data-user-id="${user.id}">
          <span class="user-chip__name">${escapeHtml(user.display_name)}</span>
          <span class="user-chip__meta">${escapeHtml(user.work_id || "")}</span>
        </button>`,
    )
    .join("");
}

function renderCreateInlinePanel(machine, detailed) {
  const preferredDepartment = state.selectedDepartment;
  const problems = preferredDepartment
    ? (state.issueGroups.find((entry) => Number(entry.department_id) === Number(preferredDepartment.id))?.problems || [])
    : [];
  const showFollowup = Boolean(preferredDepartment);
  const canSubmit = Boolean(machine && state.selectedDepartment && state.selectedProblem);
  const healthyTime = formatCurrentTime();
  return `
    <div class="machine-tile__inline-panel--create machine-modal--create machine-modal__create-stack ${detailed ? "machine-tile__inline-panel--detailed" : ""}" data-followup="${showFollowup ? "true" : "false"}">
      <div class="machine-tile__inline-panel machine-tile__inline-panel--healthy">
        <div class="machine-tile__healthy-band">
          <div class="machine-tile__healthy-intro">
            <div class="machine-tile__healthy-header">
              <div class="machine-tile__healthy-summary">
                <div class="machine-tile__healthy-summary-icon" aria-hidden="true">
                  <i class="bi bi-check-circle-fill"></i>
                  <span class="machine-tile__healthy-summary-pulse"></span>
                </div>
                <div class="machine-tile__healthy-summary-copy">
                  <div class="machine-tile__healthy-summary-title">Machine running healthy</div>
                  <div class="machine-tile__healthy-summary-time">Current time ${escapeHtml(healthyTime)}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="machine-tile__inline-panel machine-tile__inline-panel--create-body machine-modal__section--create-body">
        <div class="machine-modal__section machine-modal__section--departments machine-modal__section--create-departments">
          <div class="machine-tile__section-copy machine-tile__section-copy--departments">
            <div class="machine-tile__section-title">Departments</div>
            <div class="machine-tile__section-description">Select a department to continue.</div>
          </div>
          <div class="machine-tile__department-strip">
            ${renderDepartmentButtonsMarkup()}
          </div>
        </div>
        <div class="machine-modal__section machine-modal__section--reels machine-modal__section--create-reels">
          <div class="machine-tile__section-copy machine-tile__section-copy--create-alert">
            <div class="machine-tile__section-title">Issues</div>
            <div class="machine-tile__section-description">Pick an issue from the list below.</div>
          </div>
          <div class="problem-list">${renderProblemOptionsMarkup(problems)}</div>
        </div>
        <div class="machine-modal__section machine-modal__section--note machine-modal__section--create-note">
          <div class="machine-tile__section-copy machine-tile__section-copy--note">
            <div class="machine-tile__section-title">Note</div>
            <div class="machine-tile__section-description">Add context for the responder.</div>
          </div>
          <textarea class="form-control machine-tile__note-input" data-note-kind="create" rows="3" placeholder="Add context for the responder">${escapeHtml(state.createNoteDraft)}</textarea>
        </div>
        <div class="modal-footer machine-modal__footer machine-tile__inline-actions">
          <button class="btn btn-danger btn-lg machine-modal__footer-btn" type="button" data-inline-action="send-message" ${canSubmit ? "" : "disabled"}>Send Message</button>
        </div>
      </div>
    </div>`;
}

function formatCurrentTime() {
  return new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function renderAlertInlinePanel(machine, alert, detailed) {
  const isOpen = alert.status === "OPEN";
  const threadMarkup = renderAlertMessageThread(alert);
  const operatorNoteMarkup = renderOperatorCreatedNote(alert);
  const liveTimerText = formatAlertElapsedDuration(Math.max(0, Math.floor(alert.elapsed_seconds || 0)));
  const acknowledgedInText = Number.isFinite(Number(alert.acknowledged_seconds))
    ? formatAlertElapsedDuration(Math.max(0, Math.floor(Number(alert.acknowledged_seconds))))
    : "Pending";
  const noteMarkup = isOpen
    ? (operatorNoteMarkup ? `<div class="alert-note-summary">${operatorNoteMarkup}</div>` : '<div class="d-none" aria-hidden="true"></div>')
    : (threadMarkup ? `<div class="alert-note-summary">${threadMarkup}</div>` : '<div class="d-none" aria-hidden="true"></div>');
  return `
    <div class="machine-tile__inline-panel machine-tile__inline-panel--response machine-modal machine-modal--response ${isOpen ? "machine-modal--response-open" : "machine-modal--response-working"} ${detailed ? "machine-tile__inline-panel--detailed" : ""}">
      <div class="machine-tile__inline-panel-grid">
        ${isOpen ? `
          <div class="machine-modal__section machine-modal__section--response-waiting">
            <div class="machine-modal__response-waiting-title">Waiting on Response</div>
            <div class="machine-modal__response-waiting-subtitle">En espera de respuesta</div>
          </div>` : ""}
        ${isOpen ? "" : `
          <div class="machine-modal__section machine-modal__section--response-tile machine-modal__section--response-responder">
            <div class="machine-modal__response-label">Who is responding</div>
            <div class="machine-modal__response-value">${escapeHtml(alert.responder_name_text || "Unassigned")}</div>
          </div>
        `}
        <div class="machine-modal__section machine-modal__section--response-tile machine-modal__section--response-department">
          <div class="machine-modal__response-label">ISSUE TYPE</div>
          <div class="machine-modal__response-value">${escapeHtml(alert.department_name || machine.department_name || "Unknown")}</div>
        </div>
        <div class="machine-modal__section machine-modal__section--response-tile machine-modal__section--response-issue">
          <div class="machine-modal__response-label">Issue</div>
          <div class="machine-modal__response-value">${escapeHtml(alert.problem_name || alert.category_name || "Unknown")}</div>
        </div>
        ${isOpen ? "" : `
          <div class="machine-modal__section machine-modal__section--response-tile machine-modal__section--response-ack">
            <div class="machine-modal__response-label">Acknowledged in</div>
            <div class="machine-modal__response-value">${escapeHtml(acknowledgedInText)}</div>
          </div>
        `}
        ${isOpen ? "" : `
          <div class="machine-modal__section machine-modal__section--response-followup">
            <div class="machine-modal__followup-group machine-modal__followup-group--note">
              <div class="machine-tile__section-copy machine-tile__section-copy--note">
                <div class="machine-tile__section-title">NOTE</div>
              </div>
              ${noteMarkup}
            </div>
          </div>
        `}
        ${isOpen ? `
          <div class="machine-modal__section machine-modal__section--response-followup">
            <div class="machine-modal__followup-group machine-modal__followup-group--note">
              <div class="machine-modal__response-label">Note</div>
              ${noteMarkup}
            </div>
          </div>
        ` : ""}
        <div class="machine-modal__section machine-modal__section--timer-hero">
          <div class="machine-modal__timer-hero-label">Elapsed timer</div>
          <div class="machine-modal__timer-hero-value machine-tile__timer" data-live-timer="true" data-live-timer-format="alert-duration" data-elapsed-seconds="${Math.max(0, Math.floor(alert.elapsed_seconds || 0))}">${escapeHtml(liveTimerText)}</div>
        </div>
      </div>
    </div>`;
}

function renderOperatorCreatedNote(alert) {
  const createdNote = String(alert?.created_note || "").trim();
  if (!createdNote) return "";
  return `
    <div class="machine-tile__thread">
      <div class="alert-note-bubble is-left">
        <div class="alert-note-bubble__label">Operator Message</div>
        <div class="alert-note-bubble__text">${escapeHtml(createdNote)}</div>
      </div>
    </div>`;
}

function renderAlertMessageThread(alert) {
  const createdNote = String(alert?.created_note || "").trim();
  const currentNote = String(alert?.note || "").trim();
  const responderName = String(alert?.responder_name_text || "").trim();
  const bubbles = [];
  if (createdNote) {
    bubbles.push({
      side: "left",
      label: "Operator Message",
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
  if (!bubbles.length) return "";
  return `
    <div class="machine-tile__thread">
      ${bubbles
        .map(
          (bubble) => `
            <div class="alert-note-bubble ${bubble.side === "right" ? "is-right" : "is-left"}">
              <div class="alert-note-bubble__label">${escapeHtml(bubble.label)}</div>
              <div class="alert-note-bubble__text">${escapeHtml(bubble.text)}</div>
            </div>`,
        )
        .join("")}
    </div>`;
}

function renderAlertTimerBlocks(alert) {
  const isOpen = alert?.status === "OPEN";
  const liveSeconds = Math.max(0, Math.floor(alert?.elapsed_seconds || 0));
  const ackSeconds = Number.isFinite(Number(alert?.acknowledged_seconds))
    ? Math.max(0, Math.floor(Number(alert.acknowledged_seconds)))
    : null;
  const requestTimerLabel = "Ack timer";
  const requestTimerValue = isOpen ? "Pending" : formatElapsedSeconds(ackSeconds ?? liveSeconds);
  const liveTimerValue = formatElapsedSeconds(liveSeconds);

  return `
    <div class="machine-tile__timers">
      <div class="machine-tile__timer-block">
        <div class="machine-tile__timer-label">${escapeHtml(requestTimerLabel)}</div>
        <div class="machine-tile__timer machine-tile__timer--primary${isOpen ? " machine-tile__timer--live" : ""}">${requestTimerValue}</div>
      </div>
      ${isOpen ? "" : `
        <div class="machine-tile__timer-block machine-tile__timer-block--muted">
          <div class="machine-tile__timer-label">Live timer</div>
          <div class="machine-tile__timer machine-tile__timer--secondary machine-tile__timer--live" data-live-timer="true" data-elapsed-seconds="${liveSeconds}">${liveTimerValue}</div>
        </div>
        <div class="machine-tile__timer-block machine-tile__timer-block--muted">
          <div class="machine-tile__timer-label">Ack duration</div>
          <div class="machine-tile__timer machine-tile__timer--secondary">${formatElapsedSeconds(ackSeconds ?? liveSeconds)}</div>
        </div>`}
    </div>`;
}

async function refreshBoardState() {
  if (operatorRefreshInFlight) {
    operatorRefreshQueued = true;
    return;
  }

  operatorRefreshInFlight = true;
  try {
    await loadBoardState();
    try {
      await loadOperatorMetadata();
    } catch (_error) {
      console.warn("Failed to refresh operator metadata.");
    }
    normalizeViewState();
    renderViewControls();
    renderBoard();
  } finally {
    operatorRefreshInFlight = false;
    if (operatorRefreshQueued) {
      operatorRefreshQueued = false;
      scheduleOperatorRefresh();
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
  liveTimerNodes.forEach((timer) => {
    const currentSeconds = Number(timer.dataset.elapsedSeconds || "0") + 1;
    timer.dataset.elapsedSeconds = String(currentSeconds);
    timer.textContent = timer.dataset.liveTimerFormat === "alert-duration"
      ? formatAlertElapsedDuration(currentSeconds)
      : formatElapsedSeconds(currentSeconds);
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

function renderStatusDock(visibleMachines, detailed) {
  if (!operatorStatusDock) return;
  const total = visibleMachines.length;
  const activeAlerts = visibleMachines.filter((machine) => Boolean(machine.active_alert)).length;
  const healthy = total - activeAlerts;
  const offline = visibleMachines.filter((machine) => !machine.is_active).length;
  const lastRefresh = state.refreshedAt ? new Date(state.refreshedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "just now";
  const viewLabel = detailed ? "Focused view" : "Full floor";
  const steady = activeAlerts === 0;
  const tone = steady ? "steady" : "busy";

  operatorStatusDock.innerHTML = `
    <div class="operator-status-dock__panel operator-status-dock__panel--${tone}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__status">
          <span class="operator-status-dock__pulse"></span>
          <span class="operator-status-dock__label">${steady ? "Line steady" : "Active attention"}</span>
        </div>
        <div class="operator-status-dock__title">${steady ? "All stations are running well" : `${activeAlerts} station${activeAlerts === 1 ? "" : "s"} need attention`}</div>
        <div class="operator-status-dock__subcopy">${viewLabel} · refreshed ${escapeHtml(lastRefresh)}</div>
      </div>
      <div class="operator-status-dock__stats" role="list" aria-label="Operator status summary">
        <div class="operator-status-dock__stat" role="listitem">
          <i class="bi bi-check2-circle"></i>
          <div>
            <div class="operator-status-dock__stat-label">Healthy</div>
            <div class="operator-status-dock__stat-value">${healthy}</div>
          </div>
        </div>
        <div class="operator-status-dock__stat" role="listitem">
          <i class="bi bi-exclamation-triangle-fill"></i>
          <div>
            <div class="operator-status-dock__stat-label">Alerts</div>
            <div class="operator-status-dock__stat-value">${activeAlerts}</div>
          </div>
        </div>
        <div class="operator-status-dock__stat" role="listitem">
          <i class="bi bi-power"></i>
          <div>
            <div class="operator-status-dock__stat-label">Offline</div>
            <div class="operator-status-dock__stat-value">${offline}</div>
          </div>
        </div>
      </div>
      <div class="operator-status-dock__icons" aria-hidden="true">
        <span class="operator-status-dock__icon"><i class="bi bi-shield-check"></i></span>
        <span class="operator-status-dock__icon"><i class="bi bi-lightning-charge-fill"></i></span>
        <span class="operator-status-dock__icon"><i class="bi bi-gear-wide-connected"></i></span>
        <span class="operator-status-dock__icon"><i class="bi bi-diagram-3-fill"></i></span>
      </div>
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
  if (operatorAlertStatusPill) {
    operatorAlertStatusPill.textContent = "";
    operatorAlertStatusPill.className = "status-pill machine-modal__status-pill";
  }
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
  if (operatorAlertPriorityState) {
    operatorAlertPriorityState.textContent = "";
  }
  if (operatorAlertCreatedAt) {
    operatorAlertCreatedAt.textContent = "";
  }
  if (operatorAlertTimerState) {
    operatorAlertTimerState.textContent = "";
  }
  if (operatorAlertAckTimer) {
    operatorAlertAckTimer.textContent = "";
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
    button.hidden = false;
  });
}

function syncProblemButtonState() {
  machineBoard.querySelectorAll(".problem-btn").forEach((button) => {
    const active = state.selectedProblem && Number(button.dataset.problemId) === Number(state.selectedProblem.id);
    button.classList.toggle("is-active", Boolean(active));
  });
}

function syncCreateSubmitState() {
  const machineId = state.selectedMachine ? Number(state.selectedMachine.id) : null;
  if (!machineId) return;
  const submitButton = machineBoard.querySelector(`.operator-machine-tile[data-machine-id="${machineId}"] .machine-tile__inline-panel--create-body .machine-modal__footer-btn[data-inline-action="send-message"]`);
  if (!submitButton) return;
  submitButton.disabled = !(state.selectedMachine && state.selectedDepartment && state.selectedProblem);
}

boot();
