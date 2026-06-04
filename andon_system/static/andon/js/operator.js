const boardUrl = "/api/andon/operator-snapshot";
const operatorMetadataUrl = "/api/andon/operator-metadata";
const operatorViewStorageKey = "andon-operator-view";
const operatorMetadataCacheScope = [
  String(window.AndonRealtimeConfig?.companyId ?? "none"),
  String((document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "").slice(0, 16) || "anon"),
].join(":");
const operatorMetadataCacheKey = `andon-operator-metadata-cache-v1:${operatorMetadataCacheScope}`;
const operatorDepartmentsCacheKey = `andon-operator-departments-cache-v1:${operatorMetadataCacheScope}`;
const operatorMetadataCacheTtlMs = 5 * 60 * 1000;
const defaultOperatorMachineGroup = "Press";

const machineBoard = document.getElementById("machineBoard");
const operatorViewToggle = document.querySelector(".operator-page-header__badge");
const operatorViewPanel = document.getElementById("operatorViewPanel");
const operatorMachineGroupSelect = document.getElementById("operatorMachineGroup");
const operatorMachineSelect = document.getElementById("operatorMachine");
const operatorLockView = document.getElementById("operatorLockView");
const operatorClearView = document.getElementById("operatorClearView");
const operatorViewSummary = document.getElementById("operatorViewSummary");
const operatorBoardStage = document.getElementById("operatorBoardStage");
const operatorBoardStageInner = document.getElementById("operatorBoardStageInner");
const operatorStatusDock = document.getElementById("operatorStatusDock");
const csrfHeaders = (headers = {}) => window.AndonSecurity?.withCsrfHeaders(headers) || headers;

const departmentLabelMap = {
  maintenance: "Call Maintenance",
  materials: "Call AVG",
  shipping: "Call Shipping",
  quality: "Call Quality",
  supervisor: "Call Supervisor",
  safety: "Call Safety",
  production: "Call Shipping",
  avg: "Call AVG",
  spot: "Call SPOT",
};

const departmentPreferredOrder = ["Maintenance", "Safety", "Shipping", "Production", "Quality", "Supervisor", "Materials", "AVG", "SPOT", "Spot"];
const singleRowDepartmentNames = new Set(["Materials", "AVG", "SPOT", "Spot"]);

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
  departmentsLoaded: false,
  detailMetadataLoaded: false,
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
let operatorSingleMachineFitFrameId = null;
let operatorRefreshInFlight = false;
let operatorRefreshQueued = false;
let operatorInteractionLockUntil = 0;
let liveTimerNodes = [];
let operatorMetadataLoadPromise = null;
let operatorDepartmentsLoadPromise = null;
let operatorLastPersistedViewKey = "";
let createAlertInFlight = false;
let createAlertMachineId = null;
let departmentButtonsMarkupCacheKey = "";
let departmentButtonsMarkupCacheValue = "";
let problemOptionsMarkupCacheKey = "";
let problemOptionsMarkupCacheValue = "";
let localMutationRefreshLockUntil = 0;

function normalizeDepartmentName(name) {
  return String(name || "").trim().toLowerCase();
}

function getDepartmentLabel(name) {
  const rawName = String(name || "").trim();
  const normalizedName = normalizeDepartmentName(rawName);
  if (!rawName) return "Call Department";
  return departmentLabelMap[normalizedName] || `Call ${rawName}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: csrfHeaders({
      ...(options.headers || {}),
    }),
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  if (!response.ok) {
    throw new Error(data?.error?.message || `Request failed (${response.status})`);
  }
  if (data?.success === false) {
    throw new Error(data?.error?.message || "Request failed");
  }
  return data?.data ?? data;
}

function departmentNameIncludes(name, ...needles) {
  const normalized = normalizeDepartmentName(name);
  return needles.some((needle) => normalized.includes(String(needle || "").trim().toLowerCase()));
}

function markOperatorInteractionActive(durationMs = 1800) {
  operatorInteractionLockUntil = Date.now() + durationMs;
}

function getOperatorInteractionLockRemaining() {
  return Math.max(0, operatorInteractionLockUntil - Date.now());
}

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
  hydrateOperatorMetadataFromCache();
  hydrateOperatorDepartmentsFromCache();
  const metadataBootstrapPromise = loadOperatorDepartments({ force: true }).catch((_error) => {
    console.warn("Unable to load operator departments during boot.");
    return null;
  });
  void restoreViewState().then(() => {
    normalizeViewState();
    renderViewControls();
    renderBoard();
  }).catch((_error) => {
    console.warn("Failed to restore operator view state.");
  });
  await loadBoardState();
  normalizeViewState();
  wireEvents();
  renderViewControls();
  renderBoard();
  void metadataBootstrapPromise.then(() => {
    normalizeViewState();
    renderViewControls();
    renderBoard();
  });
  startElapsedTimers();
  document.addEventListener("visibilitychange", handleVisibilityChange);
  window.addEventListener("resize", scheduleSingleMachineFit, { passive: true });
  window.visualViewport?.addEventListener("resize", scheduleSingleMachineFit, { passive: true });
  window.AndonRefreshBus?.onRefresh(scheduleOperatorRefresh);
  window.AndonRealtime?.onEvent((event) => {
    if (["alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
      if (Date.now() < localMutationRefreshLockUntil) return;
      if (event.type === "admin_metadata_updated") {
        state.metadataLoaded = false;
        state.departmentsLoaded = false;
        state.detailMetadataLoaded = false;
        window.sessionStorage.removeItem(operatorMetadataCacheKey);
        window.sessionStorage.removeItem(operatorDepartmentsCacheKey);
        operatorDepartmentsLoadPromise = null;
        operatorMetadataLoadPromise = null;
        if (state.selectedMachine || state.selectedAlert) {
          void loadOperatorDepartments({ force: true }).then(renderBoard).catch(() => {});
          void loadOperatorMetadata({ force: true }).then(renderBoard).catch(() => {});
        }
      }
      scheduleOperatorRefresh();
    }
  });
  window.AndonRealtime?.onStatus((status) => setOperatorFallbackPolling(!status.connected));
  setOperatorFallbackPolling(!window.AndonRealtime?.connected);
  scheduleOperatorMetadataWarmup();
}

async function loadBoardState() {
  const response = await fetch(boardUrl);
  const data = await response.json();
  state.board = data.data || state.board;
  state.refreshedAt = Date.now();
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

function getCachedOperatorDepartments() {
  try {
    const raw = window.sessionStorage.getItem(operatorDepartmentsCacheKey);
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

function setCachedOperatorDepartments(metadata) {
  try {
    window.sessionStorage.setItem(
      operatorDepartmentsCacheKey,
      JSON.stringify({ cachedAt: Date.now(), data: metadata || {} }),
    );
  } catch (_error) {
    // Ignore storage failures in locked-down kiosk environments.
  }
}

function applyOperatorMetadata(metadata, options = {}) {
  const level = options.level || "full";
  state.departments = metadata?.departments || [];
  state.departmentsLoaded = Array.isArray(state.departments) && state.departments.length > 0;
  if (level === "full") {
    state.issueGroups = metadata?.issue_groups || [];
    state.users = metadata?.users || [];
    state.detailMetadataLoaded = true;
    state.metadataLoaded = true;
    return;
  }
  state.metadataLoaded = state.departmentsLoaded && state.detailMetadataLoaded;
}

function getProblemsForDepartment(departmentId) {
  const targetDepartmentId = Number(departmentId || 0);
  if (!targetDepartmentId) return [];
  const flattened = [];
  (state.issueGroups || []).forEach((group) => {
    if (Number(group?.department_id || 0) !== targetDepartmentId) return;
    const categoryId = Number(group?.category_id || 0) || null;
    (group?.problems || []).forEach((problem) => {
      flattened.push({
        ...problem,
        issue_category_id: categoryId,
      });
    });
  });
  return flattened;
}

function hydrateOperatorMetadataFromCache() {
  const cached = getCachedOperatorMetadata();
  if (!cached) return false;
  applyOperatorMetadata(cached, { level: "full" });
  return true;
}

function hydrateOperatorDepartmentsFromCache() {
  const cached = getCachedOperatorDepartments();
  if (!cached) return false;
  applyOperatorMetadata(cached, { level: "departments" });
  return true;
}

async function loadOperatorMetadata(options = {}) {
  const force = Boolean(options.force);
  if (state.detailMetadataLoaded && !force) return;
  if (!operatorMetadataLoadPromise) {
    operatorMetadataLoadPromise = (async () => {
      if (!force && hydrateOperatorMetadataFromCache()) {
        return;
      }
      const response = await fetch(operatorMetadataUrl);
      const data = await response.json();
      const metadata = data.data || {};
      applyOperatorMetadata(metadata, { level: "full" });
      setCachedOperatorMetadata(metadata);
    })().finally(() => {
      operatorMetadataLoadPromise = null;
    });
  }
  await operatorMetadataLoadPromise;
}

async function loadOperatorDepartments(options = {}) {
  const force = Boolean(options.force);
  if (state.departmentsLoaded && !force) return;
  if (!operatorDepartmentsLoadPromise) {
    operatorDepartmentsLoadPromise = (async () => {
      if (!force && hydrateOperatorDepartmentsFromCache()) {
        return;
      }
      const response = await fetch(`${operatorMetadataUrl}?departments_only=1`);
      const data = await response.json();
      const metadata = data.data || {};
      applyOperatorMetadata(metadata, { level: "departments" });
      setCachedOperatorDepartments(metadata);
    })().finally(() => {
      operatorDepartmentsLoadPromise = null;
    });
  }
  await operatorDepartmentsLoadPromise;
}

async function ensureOperatorDepartmentsLoaded() {
  if (state.departmentsLoaded && Array.isArray(state.departments) && state.departments.length > 0) return;
  await loadOperatorDepartments({ force: true });
}

function scheduleOperatorMetadataWarmup() {
  if (state.detailMetadataLoaded || operatorMetadataLoadPromise) return;
  const runWarmup = () => {
    void loadOperatorMetadata().then(() => {
      renderBoard();
    }).catch((_error) => {
      console.warn("Failed to preload operator metadata.");
    });
  };
  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(runWarmup, { timeout: 1200 });
    return;
  }
  setTimeout(runWarmup, 250);
}

function wireEvents() {
  machineBoard?.addEventListener("click", onBoardClick);
  machineBoard?.addEventListener("input", onBoardInput);
  operatorMachineGroupSelect?.addEventListener("change", onMachineGroupChange);
  operatorMachineSelect?.addEventListener("change", onMachineChange);
  operatorLockView?.addEventListener("click", toggleOperatorViewLock);
  operatorClearView?.addEventListener("click", clearOperatorView);
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
    markOperatorInteractionActive();
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
    await ensureOperatorDepartmentsLoaded();
  } catch (_error) {
    window.alert("Unable to load operator departments.");
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
  void loadOperatorMetadata().then(renderBoard).catch(() => {});
}

async function openCreatePanel(machine) {
  try {
    await ensureOperatorDepartmentsLoaded();
  } catch (_error) {
    window.alert("Unable to load operator departments.");
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
  void loadOperatorMetadata().then(renderBoard).catch(() => {});
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
  const departments = getRenderableDepartments().sort((left, right) => {
    const leftIndex = departmentPreferredOrder.indexOf(left.name);
    const rightIndex = departmentPreferredOrder.indexOf(right.name);
    if (leftIndex === -1 && rightIndex === -1) return left.name.localeCompare(right.name);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
  departmentButtons.innerHTML = departments
    .map((department) => {
      const label = getDepartmentLabel(department.name);
      const fullRowClass = singleRowDepartmentNames.has(department.name) ? " board-category-btn--full-row" : "";
      const disabledAttr = department.isPlaceholder ? " disabled" : "";
      return `
        <button type="button" class="btn btn-lg board-category-btn${fullRowClass}" data-department-id="${department.id}" data-department-name="${escapeHtml(department.name)}"${disabledAttr}>
          <span class="d-block">${escapeHtml(label)}</span>
        </button>`;
    })
    .join("");
  syncDepartmentButtonState();
  syncDepartmentButtonVisibility();
}

function onDepartmentButtonClick(button) {
  if (!button) return;
  if (button.disabled) return;
  markOperatorInteractionActive();
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
  if (!state.detailMetadataLoaded) {
    void loadOperatorMetadata().then(renderBoard).catch(() => {});
  }
}

function onProblemClick(button) {
  if (!button) return;
  markOperatorInteractionActive();
  const tile = button.closest(".operator-machine-tile");
  const machineId = Number(tile?.dataset.machineId);
  const machine = state.board.machines.find((row) => Number(row.id) === machineId) || null;
  const problemId = Number(button.dataset.problemId);
  const department = state.selectedDepartment;
  if (!department) return;
  const problems = getProblemsForDepartment(department.id);
  const problem = problems.find((entry) => Number(entry.id) === problemId);
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
  if (createAlertInFlight) {
    return;
  }
  if (!state.selectedMachine || !state.selectedDepartment || !state.selectedProblem) {
    alert("Select a department and problem.");
    return;
  }
  const issueCategoryId = Number(state.selectedProblem.issue_category_id || 0);
  if (!issueCategoryId) {
    alert("Selected issue is missing a category. Refresh metadata and try again.");
    return;
  }
  createAlertInFlight = true;
  createAlertMachineId = Number(state.selectedMachine.id);
  renderBoard();
  const payload = {
    machine_id: state.selectedMachine.id,
    department_id: state.selectedDepartment.id,
    issue_category_id: issueCategoryId,
    issue_problem_id: Number(state.selectedProblem.id),
    operator_name_text: null,
    note: state.createNoteDraft.trim() || null,
  };
  try {
    const response = await fetch("/api/andon/alerts", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      credentials: "same-origin",
    });
    const data = await response.json();
    if (response.status === 409 && data?.error?.existing_alert) {
      const targetMachine = state.board.machines.find((row) => Number(row.id) === Number(state.selectedMachine.id)) || state.selectedMachine;
      const activeAlert = normalizeActiveAlert(data.error.existing_alert, targetMachine);
      if (targetMachine) {
        targetMachine.active_alert = activeAlert;
      }
      await openActiveAlertModal(activeAlert, targetMachine);
      localMutationRefreshLockUntil = Date.now() + 700;
      window.AndonRefreshBus?.notify();
      return;
    }
    if (!data.success) {
      alert(data.error?.message || "Unable to create alert.");
      return;
    }
    const createdAlert = normalizeActiveAlert(data.data, state.selectedMachine);
    const machine = state.board.machines.find((row) => Number(row.id) === Number(state.selectedMachine.id));
    if (machine) {
      machine.active_alert = createdAlert;
    }
    closeMachinePanel();
    localMutationRefreshLockUntil = Date.now() + 700;
    window.AndonRefreshBus?.notify();
  } catch (_error) {
    alert("Unable to create alert. Please try again.");
  } finally {
    createAlertInFlight = false;
    createAlertMachineId = null;
    renderBoard();
  }
}

async function actOnActiveAlert() {
  try {
    const liveMachine = state.board.machines.find((machine) => Number(machine.id) === Number(state.selectedMachine?.id));
    const activeAlert = liveMachine?.active_alert || state.selectedAlert || null;
    const alertId = activeAlert?.id;
    if (!alertId || !activeAlert) return;
    if (activeAlert.status === "OPEN") {
      window.alert("This alert must be acknowledged from the board before it can be closed here.");
      return;
    }
    const responderUserId = activeAlert.responder_user_id || state.selectedAlertUserId;
    const payload = {};
    if (responderUserId) {
      payload.responder_user_id = Number(responderUserId);
    }
    const note = state.alertNoteDraft.trim();
    if (note) {
      payload.note = note;
    }
    await fetchJson(`/api/andon/alerts/${alertId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (liveMachine) {
      liveMachine.active_alert = null;
    }
    if (state.selectedAlert && Number(state.selectedAlert.id) === Number(alertId)) {
      state.selectedAlert = null;
    }
    closeMachinePanel();
    localMutationRefreshLockUntil = Date.now() + 700;
    window.AndonRefreshBus?.notify();
    renderBoard();
  } catch (error) {
    window.alert(error?.message || "Unable to close alert.");
  }
}

function renderBoard() {
  const visibleMachines = getVisibleMachines();
  const detailed = isDetailedOperatorView();
  const singleMachineMode = visibleMachines.length === 1;
  const groupedCardSizing = !detailed;
  const boardKey = buildBoardKey(visibleMachines, detailed);
  machineBoard.dataset.machineCount = String(visibleMachines.length);
  machineBoard.dataset.viewMode = detailed ? "detailed" : "compact";
  machineBoard.dataset.groupCardSizing = groupedCardSizing ? "detailed" : "native";
  machineBoard.dataset.singleMachine = singleMachineMode ? "true" : "false";
  document.body.classList.toggle("operator-single-machine-mode", singleMachineMode);
  applyDetailedBoardDensity(visibleMachines.length, detailed || groupedCardSizing);
  if (!visibleMachines.length) {
    machineBoard.innerHTML = renderEmptyBoard();
    machineBoard.dataset.boardKey = boardKey;
    syncLiveTimerNodes();
    renderStatusDock(visibleMachines, detailed);
    primeCreatePanelTransitions();
    applySingleMachineFit(false);
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
  applySingleMachineFit(singleMachineMode);
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
  const metadataState = active
    ? (isSelectedMachine || isSelectedAlert ? (state.metadataLoaded ? "meta" : "nometa") : "")
    : `${state.metadataLoaded ? "meta" : "nometa"}:${Array.isArray(state.departments) ? state.departments.length : 0}`;
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

function applySingleMachineFit(singleMachineMode) {
  if (!operatorBoardStageInner) return;
  if (!singleMachineMode) {
    if (operatorSingleMachineFitFrameId) {
      cancelAnimationFrame(operatorSingleMachineFitFrameId);
      operatorSingleMachineFitFrameId = null;
    }
    operatorBoardStageInner.style.removeProperty("--operator-single-machine-scale");
    return;
  }
  if (operatorSingleMachineFitFrameId) {
    cancelAnimationFrame(operatorSingleMachineFitFrameId);
  }
  operatorSingleMachineFitFrameId = requestAnimationFrame(() => {
    operatorSingleMachineFitFrameId = null;
    const availableHeight = operatorBoardStage?.clientHeight || 0;
    const currentScale = Number.parseFloat(
      window.getComputedStyle(operatorBoardStageInner).getPropertyValue("--operator-single-machine-scale"),
    ) || 1;
    const renderedHeight = operatorBoardStageInner.getBoundingClientRect().height || 0;
    const contentHeight = renderedHeight > 0 ? renderedHeight / currentScale : operatorBoardStageInner.scrollHeight || 0;
    if (!availableHeight || !contentHeight) {
      operatorBoardStageInner.style.setProperty("--operator-single-machine-scale", "1");
      return;
    }
    const scale = Math.min(1, Math.max(0.4, availableHeight / contentHeight));
    operatorBoardStageInner.style.setProperty("--operator-single-machine-scale", scale.toFixed(4));
  });
}

function scheduleSingleMachineFit() {
  applySingleMachineFit(machineBoard?.dataset.singleMachine === "true");
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
  if (!state.departmentsLoaded && (!Array.isArray(state.departments) || !state.departments.length)) {
    return '<div class="problem-empty">Loading departments...</div>';
  }
  const selectedDepartmentId = state.selectedDepartment ? Number(state.selectedDepartment.id) : null;
  const metadataKey = getRenderableDepartments()
    .map((department) => `${department.id}:${department.name}:${department.isPlaceholder ? 1 : 0}`)
    .join("|");
  const cacheKey = `${selectedDepartmentId || ""}|${metadataKey}`;
  if (cacheKey === departmentButtonsMarkupCacheKey) {
    return departmentButtonsMarkupCacheValue;
  }
  const departments = getRenderableDepartments().sort((left, right) => {
    const leftIndex = departmentPreferredOrder.indexOf(left.name);
    const rightIndex = departmentPreferredOrder.indexOf(right.name);
    if (leftIndex === -1 && rightIndex === -1) return left.name.localeCompare(right.name);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
  const markup = departments
    .map((department) => {
      const label = getDepartmentLabel(department.name);
      const isSelected = state.selectedDepartment && Number(state.selectedDepartment.id) === Number(department.id);
      const iconClass = getDepartmentIconClass(department.name);
      const fullRowClass = singleRowDepartmentNames.has(department.name) ? " board-category-btn--full-row" : "";
      const disabledAttr = department.isPlaceholder ? " disabled" : "";
      return `
        <button type="button" class="btn btn-lg board-category-btn${isSelected ? " is-active" : ""}${fullRowClass}" data-department-id="${department.id}" data-department-name="${escapeHtml(department.name)}"${disabledAttr}>
          <span class="board-category-btn__icon" aria-hidden="true"><i class="${iconClass}"></i></span>
          <span class="d-block">${escapeHtml(label)}</span>
        </button>`;
    })
    .join("");
  departmentButtonsMarkupCacheKey = cacheKey;
  departmentButtonsMarkupCacheValue = markup;
  return markup;
}

function getRenderableDepartments() {
  return [...state.departments];
}

function renderProblemOptionsMarkup(problems) {
  if (state.selectedDepartment && !state.detailMetadataLoaded) {
    return '<div class="problem-empty">Loading issues...</div>';
  }
  const selectedProblemId = state.selectedProblem ? Number(state.selectedProblem.id) : null;
  const problemKey = problems.map((problem) => `${problem.id}:${problem.name}`).join("|");
  const cacheKey = `${selectedProblemId || ""}|${problemKey}`;
  if (cacheKey === problemOptionsMarkupCacheKey) {
    return problemOptionsMarkupCacheValue;
  }
  const markup = problems.length
    ? problems
      .map(
        (problem) => `
            <button type="button" class="problem-btn${state.selectedProblem && Number(state.selectedProblem.id) === Number(problem.id) ? " is-active" : ""}" data-problem-id="${problem.id}">
              <span class="problem-btn__name">${escapeHtml(problem.name)}</span>
            </button>`,
      )
      .join("")
    : '<div class="problem-empty">No issues found for this department.</div>';
  problemOptionsMarkupCacheKey = cacheKey;
  problemOptionsMarkupCacheValue = markup;
  return markup;
}

function getDepartmentIconClass(name) {
  if (departmentNameIncludes(name, "quality") && departmentNameIncludes(name, "supervisor")) {
    return "bi bi-person-check-fill";
  }
  if (departmentNameIncludes(name, "maintenance")) {
    return "bi bi-tools";
  }
  if (departmentNameIncludes(name, "materials", "avg")) {
    return "bi bi-broadcast-pin";
  }
  if (departmentNameIncludes(name, "quality")) {
    return "bi bi-patch-check-fill";
  }
  if (departmentNameIncludes(name, "supervisor")) {
    return "bi bi-person-badge-fill";
  }
  if (departmentNameIncludes(name, "safety")) {
    return "bi bi-shield-exclamation";
  }
  if (departmentNameIncludes(name, "production", "shipping")) {
    return "bi bi-truck";
  }
  if (departmentNameIncludes(name, "spot")) {
    return "bi bi-bullseye";
  }
  return "bi bi-telephone-forward-fill";
}

function renderUserButtonsMarkup(users, selectedUserId, kind) {
  const validSelected = users.some((user) => Number(user.id) === Number(selectedUserId)) ? selectedUserId : null;
  if (!state.detailMetadataLoaded) {
    return '<div class="problem-empty">Loading users...</div>';
  }
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

function radiusValue(value) {
  return escapeHtml(value || "N/A");
}

function renderRadiusEventBadge(machine) {
  const value = String(machine?.radius?.event_type || "").trim();
  if (!value) return "";
  return `<span class="radius-event-badge" aria-label="Radius event type">${escapeHtml(value)}</span>`;
}

function renderRadiusPanel(machine, modifier = "") {
  const radius = machine?.radius || null;
  return `
    <div class="radius-panel ${modifier}">
      <div class="radius-panel__header">
        <div class="radius-panel__title">Radius</div>
        <div class="radius-panel__machine">Machine ${radiusValue(radius?.machine_id || machine?.radius_machine_id)}</div>
      </div>
      <div class="radius-panel__grid radius-panel__grid--operator">
        <div class="radius-panel__item">
          <div class="radius-panel__label">Status</div>
          <div class="radius-panel__value">${radiusValue(radius?.status_label)}</div>
        </div>
        <div class="radius-panel__item">
          <div class="radius-panel__label">Operator Code</div>
          <div class="radius-panel__value">${radiusValue(radius?.operation_code)}</div>
        </div>
        <div class="radius-panel__item">
          <div class="radius-panel__label">Job Code</div>
          <div class="radius-panel__value">${radiusValue(radius?.job_code)}</div>
        </div>
      </div>
    </div>`;
}

function renderCreateInlinePanel(machine, detailed) {
  const preferredDepartment = state.selectedDepartment;
  const problems = preferredDepartment ? getProblemsForDepartment(preferredDepartment.id) : [];
  const showFollowup = Boolean(preferredDepartment);
  const isSubmitting = createAlertInFlight && Number(createAlertMachineId) === Number(machine?.id);
  const canSubmit = Boolean(machine && state.selectedDepartment && state.selectedProblem) && !isSubmitting;
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
              ${renderRadiusEventBadge(machine)}
            </div>
          </div>
        </div>
      </div>
      ${renderRadiusPanel(machine, "machine-tile__radius-panel")}
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
          <button class="btn btn-danger btn-lg machine-modal__footer-btn" type="button" data-inline-action="send-message" ${canSubmit ? "" : "disabled"}>${isSubmitting ? "Calling..." : "Call"}</button>
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
  const closeNotePreviewMarkup = threadMarkup || operatorNoteMarkup || '<div class="machine-modal__close-note-empty">No existing note</div>';
  const actionLabel = "Close Alert";
  return `
    <div class="machine-tile__inline-panel machine-tile__inline-panel--response machine-modal machine-modal--response ${isOpen ? "machine-modal--response-open" : "machine-modal--response-working"} ${detailed ? "machine-tile__inline-panel--detailed" : ""}">
      ${isOpen ? `
        <div class="machine-modal__section machine-modal__section--response-waiting">
          <div class="machine-modal__response-waiting-title">Waiting on Response</div>
          <div class="machine-modal__response-waiting-subtitle">En espera de respuesta</div>
        </div>
      ` : ""}
      ${renderRadiusPanel(machine, "machine-tile__radius-panel machine-tile__radius-panel--response")}
      <div class="machine-tile__inline-panel-grid">
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
        <div class="machine-modal__section machine-modal__section--timer-hero">
          <div class="machine-modal__timer-hero-label">Elapsed timer</div>
          <div class="machine-modal__timer-hero-value machine-tile__timer" data-live-timer="true" data-live-timer-format="alert-duration" data-elapsed-seconds="${Math.max(0, Math.floor(alert.elapsed_seconds || 0))}">${escapeHtml(liveTimerText)}</div>
        </div>
        ${isOpen ? `
          <div class="machine-modal__section machine-modal__section--response-waiting machine-modal__section--response-waiting--closed">
            <div class="machine-modal__response-waiting-title">Awaiting acknowledgement</div>
            <div class="machine-modal__response-waiting-subtitle">Close Alert appears after the response is acknowledged.</div>
          </div>
        ` : `
          <div class="machine-modal__section machine-modal__section--response-close-row">
            <div class="machine-modal__close-note-block machine-modal__close-note-block--existing">
              <div class="machine-modal__response-label">Note</div>
              <div class="machine-modal__close-row-preview">${closeNotePreviewMarkup}</div>
            </div>
            <div class="machine-modal__close-note-block machine-modal__close-note-block--append">
              <div class="machine-modal__response-label">Add note</div>
              <textarea class="form-control machine-tile__note-input machine-modal__close-note" data-note-kind="alert" rows="2" placeholder="Append note before closing">${escapeHtml(state.alertNoteDraft)}</textarea>
            </div>
          </div>
          <div class="machine-modal__section machine-modal__section--response-close-action machine-tile__inline-actions">
            <button class="btn btn-primary machine-modal__footer-btn machine-modal__footer-btn--full" type="button" data-inline-action="act-on-alert">${actionLabel}</button>
          </div>
        `}
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
    normalizeViewState();
    renderViewControls();
    renderBoard();
    void loadOperatorMetadata().then(() => {
      normalizeViewState();
      renderViewControls();
      renderBoard();
    }).catch((_error) => {
      console.warn("Failed to refresh operator metadata.");
    });
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
  const interactionLockRemaining = state.selectedMachine && !state.selectedAlert
    ? getOperatorInteractionLockRemaining()
    : 0;
  operatorRefreshTimeoutId = setTimeout(() => {
    operatorRefreshTimeoutId = null;
    refreshBoardState();
  }, Math.max(150, interactionLockRemaining + 50));
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
  if (!operatorMachineGroupSelect || !operatorMachineSelect || !operatorLockView) return;
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
  if (!operatorMachineGroupSelect) return;
  if (state.view.locked) return;
  state.view.machineGroup = operatorMachineGroupSelect.value;
  state.view.machineId = "";
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function onMachineChange() {
  if (!operatorMachineSelect) return;
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
  if (!operatorLockView) return;
  state.view.locked = !state.view.locked;
  normalizeViewState();
  persistOperatorViewState();
  renderViewControls();
  renderBoard();
}

function clearOperatorView() {
  if (!operatorMachineGroupSelect || !operatorMachineSelect) return;
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

async function restoreViewState() {
  try {
    const remote = await window.AndonPreferences?.load?.("operator");
    if (remote && Object.keys(remote).length) {
      state.view.machineGroup = typeof remote.machineGroup === "string" ? remote.machineGroup : "";
      state.view.machineId = typeof remote.machineId === "string" || typeof remote.machineId === "number" ? String(remote.machineId) : "";
      state.view.locked = Boolean(remote.locked);
      operatorLastPersistedViewKey = getOperatorViewStateKey(state.view);
      return;
    }
  } catch (_error) {
    // Fall back to local storage when remote preferences are unavailable.
  }
  try {
    const raw = window.localStorage.getItem(operatorViewStorageKey);
    if (!raw) {
      state.view.machineGroup = defaultOperatorMachineGroup;
      state.view.machineId = "";
      state.view.locked = true;
      operatorLastPersistedViewKey = "";
      return;
    }
    const parsed = JSON.parse(raw);
    state.view.machineGroup = typeof parsed.machineGroup === "string" ? parsed.machineGroup : "";
    state.view.machineId = typeof parsed.machineId === "string" || typeof parsed.machineId === "number" ? String(parsed.machineId) : "";
    state.view.locked = Boolean(parsed.locked);
    operatorLastPersistedViewKey = getOperatorViewStateKey(state.view);
  } catch (_error) {
    state.view.machineGroup = defaultOperatorMachineGroup;
    state.view.machineId = "";
    state.view.locked = true;
    operatorLastPersistedViewKey = "";
  }
}

function getOperatorViewStateKey(view) {
  return JSON.stringify({
    machineGroup: view?.machineGroup || "",
    machineId: view?.machineId || "",
    locked: Boolean(view?.locked),
  });
}

function persistOperatorViewState() {
  const nextKey = getOperatorViewStateKey(state.view);
  if (nextKey === operatorLastPersistedViewKey) {
    return;
  }
  try {
    window.localStorage.setItem(operatorViewStorageKey, JSON.stringify(state.view));
  } catch (_error) {
    // localStorage can be unavailable in locked-down kiosk browsers.
  }
  operatorLastPersistedViewKey = nextKey;
  window.AndonPreferences?.save?.("operator", state.view);
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
