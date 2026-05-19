const boardStateUrl = "/api/andon/board-state?compact=1";
const reportDetailsUrl = "/api/andon/reports/machine-details";
const boardsUrl = "/api/andon/boards";
const managementDefaults = window.AndonManagementDefaults || {};
const DRAFT_BOARD_ID = "draft";

const boardSelector = document.getElementById("boardSelector");
const boardNameInput = document.getElementById("boardNameInput");
const createBoardBtn = document.getElementById("createBoardBtn");
const saveBoardNameBtn = document.getElementById("saveBoardNameBtn");
const focusModeBtn = document.getElementById("focusModeBtn");
const previewBoardBtn = document.getElementById("previewBoardBtn");
const deleteBoardBtn = document.getElementById("deleteBoardBtn");
const previewModeBar = document.getElementById("previewModeBar");
const previewEditBtn = document.getElementById("previewEditBtn");
const previewSaveBtn = document.getElementById("previewSaveBtn");
const modulePerformance = document.getElementById("modulePerformance");
const moduleHistory = document.getElementById("moduleHistory");
const moduleRadius = document.getElementById("moduleRadius");
const toggleSourcesBtn = document.getElementById("toggleSourcesBtn");
const toggleSourcesText = document.getElementById("toggleSourcesText");
const groupDropSource = document.getElementById("groupDropSource");
const machineDropSource = document.getElementById("machineDropSource");
const boardBuilderEditLayout = document.getElementById("boardBuilderEditLayout");
const boardBuilderPreviewShell = document.getElementById("boardBuilderPreviewShell");
const boardBuilderCanvas = document.getElementById("boardBuilderCanvas");
const previewBoardTitle = document.getElementById("previewBoardTitle");
const previewBoardGrid = document.getElementById("previewBoardGrid");
const managementToastDock = document.getElementById("managementToastDock");
const managementStatusDock = document.getElementById("managementStatusDock");
const managementPageShell = document.getElementById("managementPageShell");
const headerEditBoardBtn = document.getElementById("headerEditBoardBtn");
const boardModeBanner = document.getElementById("boardModeBanner");
const boardNameLabel = document.getElementById("boardNameLabel");
const boardBuilderSteps = document.getElementById("boardBuilderSteps");
const groupSourceCount = document.getElementById("groupSourceCount");
const machineSourceCount = document.getElementById("machineSourceCount");
const canvasMachineCount = document.getElementById("canvasMachineCount");
const machineSearchInput = document.getElementById("machineSearchInput");
const managementBusyOverlay = document.getElementById("managementBusyOverlay");
const managementBusyText = document.getElementById("managementBusyText");
const managementDetailModalEl = document.getElementById("managementDetailModal");
const managementDetailModalTitle = document.getElementById("managementDetailModalTitle");
const managementDetailModalSubtitle = document.getElementById("managementDetailModalSubtitle");
const managementDetailStart = document.getElementById("managementDetailStart");
const managementDetailEnd = document.getElementById("managementDetailEnd");
const managementDetailRefresh = document.getElementById("managementDetailRefresh");
const managementDetailTableBody = document.getElementById("managementDetailTableBody");
const managementDetailSummary = document.getElementById("managementDetailSummary");
const managementDetailModal = managementDetailModalEl && window.bootstrap ? new bootstrap.Modal(managementDetailModalEl) : null;

const state = {
  boardState: { machines: [] },
  boards: [],
  draftBoard: null,
  activeBoardId: null,
  viewMode: "edit",
  sourcesCollapsed: false,
  focusMode: false,
  shiftRange: {
    start: managementDefaults.shiftStart || "",
    end: managementDefaults.shiftEnd || "",
    label: managementDefaults.shiftLabel || "Current shift",
  },
  shiftStatsByMachineId: {},
  shiftStatsLoadedRangeKey: null,
  shiftStatsLoadingPromise: null,
  machineSearchTerm: "",
};

let activeDetailMachine = null;
let detailRequestId = 0;
let nextDraftItemId = 1;
let isMutating = false;
let toastId = 0;
let renderQueued = false;
let modulePatchTimer = null;

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

function formatClockTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function getShiftRangeKey() {
  return `${state.shiftRange.start}|${state.shiftRange.end}`;
}

function createDraftBoard() {
  return {
    id: DRAFT_BOARD_ID,
    name: "New Board",
    show_performance: true,
    show_recent_history: true,
    show_radius: true,
    items: [],
    isDraft: true,
  };
}

function ensureDraftBoard() {
  if (!state.draftBoard) {
    state.draftBoard = createDraftBoard();
  }
  return state.draftBoard;
}

function getActiveBoard() {
  if (state.activeBoardId === DRAFT_BOARD_ID) {
    return state.draftBoard;
  }
  return state.boards.find((board) => Number(board.id) === Number(state.activeBoardId)) || null;
}

function isDraftBoard(board) {
  return Boolean(board?.isDraft);
}

function setMutatingState(next) {
  isMutating = Boolean(next);
  if (createBoardBtn) createBoardBtn.disabled = isMutating || !getActiveBoard();
  if (saveBoardNameBtn) saveBoardNameBtn.disabled = isMutating || !getActiveBoard() || isDraftBoard(getActiveBoard());
  if (deleteBoardBtn) deleteBoardBtn.disabled = isMutating || !getActiveBoard() || isDraftBoard(getActiveBoard());
  if (focusModeBtn) focusModeBtn.disabled = isMutating;
  if (previewBoardBtn) previewBoardBtn.disabled = isMutating || !getActiveBoard();
  if (previewSaveBtn) previewSaveBtn.disabled = isMutating || !isDraftBoard(getActiveBoard());
  if (boardSelector) boardSelector.disabled = isMutating;
  if (machineSearchInput) {
    machineSearchInput.disabled = isMutating;
  }
}

function setBusyOverlay(visible, message = "Working...") {
  if (!managementBusyOverlay || !managementBusyText) return;
  managementBusyText.textContent = message;
  managementBusyOverlay.hidden = !visible;
}

async function withMutation(task, busyMessage = "Saving board changes...", options = {}) {
  if (isMutating) return;
  const background = Boolean(options.background);
  setMutatingState(true);
  if (!background) {
    setBusyOverlay(true, busyMessage);
  }
  try {
    return await task();
  } finally {
    if (!background) {
      setBusyOverlay(false);
    }
    setMutatingState(false);
  }
}

function showToast(message, level = "info", title = "Boards", timeoutMs = 2800) {
  if (!managementToastDock) return;
  const id = ++toastId;
  const toast = document.createElement("div");
  toast.className = `management-toast management-toast--${level}`;
  toast.dataset.toastId = String(id);
  toast.innerHTML = `
    <div class="management-toast__title">${escapeHtml(title)}</div>
    <div class="management-toast__message">${escapeHtml(message)}</div>
  `;
  managementToastDock.prepend(toast);
  requestAnimationFrame(() => toast.classList.add("is-visible"));

  const dismiss = () => {
    toast.classList.remove("is-visible");
    window.setTimeout(() => toast.remove(), 180);
  };
  window.setTimeout(dismiss, timeoutMs);
}

function reportError(error, fallback = "Request failed") {
  const message = error?.message || fallback;
  showToast(message, "error", "Error", 3600);
}

async function runSafely(task, fallbackError) {
  try {
    await task();
  } catch (error) {
    reportError(error, fallbackError);
  }
}

function getMachineById(machineId) {
  return (state.boardState.machines || []).find((machine) => Number(machine.id) === Number(machineId)) || null;
}

function getBoardMachineIds(board = getActiveBoard()) {
  return new Set((board?.items || []).map((item) => Number(item.machine_id)));
}

function getAvailableMachines() {
  const excludedIds = getBoardMachineIds();
  const search = state.machineSearchTerm.trim().toLowerCase();
  return (state.boardState.machines || [])
    .filter((machine) => !excludedIds.has(Number(machine.id)))
    .filter((machine) => {
      if (!search) return true;
      const haystack = `${machine.name || ""} ${machine.machine_type || ""} ${machine.department_name || ""}`.toLowerCase();
      return haystack.includes(search);
    });
}

function updateFlowSteps() {
  if (!boardBuilderSteps) return;
  const activeBoard = getActiveBoard();
  const machineCount = (activeBoard?.items || []).length;
  const isPreview = state.viewMode === "display";
  const isDraft = isDraftBoard(activeBoard);
  const completed = [
    Boolean(activeBoard),
    machineCount > 0,
    isPreview,
    !isDraft && machineCount > 0,
  ];
  boardBuilderSteps.querySelectorAll(".board-builder-step").forEach((stepNode, index) => {
    const done = completed[index];
    stepNode.classList.toggle("is-complete", done);
    stepNode.classList.toggle("is-active", !done && index === completed.findIndex((flag) => !flag));
  });
}

function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  window.requestAnimationFrame(() => {
    renderQueued = false;
    renderBoardSelector();
    renderSourceLists();
    renderCurrentView();
    updateFlowSteps();
  });
}

function getMachineStats(machineId) {
  const stats = state.shiftStatsByMachineId[machineId];
  if (stats) {
    return stats;
  }
  const loading = Boolean(state.shiftStatsLoadingPromise);
  return {
    totalAlerts: loading ? "..." : "—",
    averageAcknowledge: loading ? "Loading..." : "—",
    averageFix: loading ? "Loading..." : "—",
    latestClosed: null,
  };
}

function radiusValue(value) {
  return escapeHtml(value || "N/A");
}

function getMachineHealthState(machine) {
  if (!machine?.is_active) {
    return { heroClass: "status-off", label: "Offline" };
  }
  const alertStatus = String(machine?.active_alert?.status || "").toUpperCase();
  if (alertStatus === "OPEN") {
    return { heroClass: "status-open", label: "Alert Open" };
  }
  if (alertStatus === "ACKNOWLEDGED" || alertStatus === "ARRIVED") {
    return { heroClass: "status-acknowledged", label: "Being Worked" };
  }
  return { heroClass: "status-healthy", label: "Healthy" };
}

function renderRadiusGroup(machine) {
  const radius = machine?.radius || null;
  return `
    <div class="management-machine-card__radius">
      <div class="management-machine-card__radius-grid management-machine-card__radius-grid--pair">
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Operator Code</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.operation_code)}</span>
        </div>
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Job Code</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.job_code)}</span>
        </div>
      </div>
      <div class="management-machine-card__radius-grid management-machine-card__radius-grid--stack">
        <div class="management-machine-card__radius-item management-machine-card__radius-item--wide">
          <span class="management-machine-card__radius-label">Status</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.status_label)}</span>
        </div>
      </div>
    </div>`;
}

function renderBoardTile(machine, board, item, editable = true) {
  const stats = getMachineStats(machine.id);
  const active = machine.active_alert;
  const health = getMachineHealthState(machine);
  const lastClosed = stats.latestClosed;
  const issue = active ? String(active.problem_name || active.category_name || "Unassigned").trim() : "";
  const responder = active ? String(active.responder_name_text || "").trim() : "";
  const lastIssue = lastClosed
    ? [lastClosed.department_name, lastClosed.issue_problem_name || lastClosed.issue_category_name].filter(Boolean).join(" - ") || "Unassigned"
    : (state.shiftStatsLoadingPromise ? "Loading recent history..." : "No recent issue");
  return `
    <article
      class="management-machine-card board-builder-tile"
      data-board-item-id="${item.id}"
      data-machine-id="${machine.id}"
      draggable="${editable ? "true" : "false"}"
    >
      ${editable ? `<button class="board-builder-tile__remove" type="button" data-remove-board-item="${item.id}" aria-label="Remove ${escapeHtml(machine.name)}">×</button>` : ""}
      <div class="management-machine-card__hero management-machine-card__hero--${health.heroClass}">
        <div class="management-machine-card__title-row">
          <div class="management-machine-card__title">${escapeHtml(machine.name)}</div>
          <span class="board-builder-tile__meta">${escapeHtml(machine.machine_type || "Unassigned")}</span>
        </div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-text">${escapeHtml(health.label)}</span>
        </div>
      </div>
      ${board.show_performance ? `
        <div class="management-machine-card__section management-machine-card__section--metrics">
          <div class="management-machine-card__section-title">Performance</div>
          <div class="management-machine-card__metrics">
            <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Today</div><div class="management-machine-card__metric-value">${escapeHtml(stats.totalAlerts)}</div></div>
            <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Avg ack</div><div class="management-machine-card__metric-value">${escapeHtml(stats.averageAcknowledge)}</div></div>
            <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Avg fix</div><div class="management-machine-card__metric-value">${escapeHtml(stats.averageFix)}</div></div>
          </div>
        </div>` : ""}
      ${board.show_recent_history ? `
        <div class="management-machine-card__section management-machine-card__section--status">
          <div class="management-machine-card__section-title">${active ? "Current Alert" : "Recent History"}</div>
          <div class="management-machine-card__body">
            ${active ? `
              <div class="management-machine-card__state-row management-machine-card__state-row--two">
                <div class="management-machine-card__state management-machine-card__state--issue-open">
                  <div class="management-machine-card__state-label">Issue</div>
                  <div class="management-machine-card__state-value">${escapeHtml(issue)}</div>
                </div>
                <div class="management-machine-card__state management-machine-card__state--responder">
                  <div class="management-machine-card__state-label">Responder</div>
                  <div class="management-machine-card__state-value">${escapeHtml(responder || "No Responder Assigned")}</div>
                </div>
              </div>
              <div class="management-machine-card__state management-machine-card__state--timer-open management-machine-card__state--elapsed-full">
                <div class="management-machine-card__state-label">Elapsed</div>
                <div class="management-machine-card__state-value management-machine-card__state-value--elapsed">${formatElapsedDuration(active.elapsed_seconds || 0)}</div>
              </div>
            ` : `
              <div class="management-machine-card__history">
                <div class="management-machine-card__history-label">Last issue</div>
                <div class="management-machine-card__history-value">${escapeHtml(lastIssue)}</div>
                <div class="management-machine-card__history-times">
                  <div class="management-machine-card__history-time"><span>Started</span><strong>${escapeHtml(formatClockTime(lastClosed?.created_at))}</strong></div>
                  <div class="management-machine-card__history-time"><span>Resolved</span><strong>${escapeHtml(formatClockTime(lastClosed?.closed_at))}</strong></div>
                </div>
              </div>
            `}
          </div>
        </div>` : ""}
      ${board.show_radius ? `
        <div class="management-machine-card__section management-machine-card__section--radius">
          <div class="management-machine-card__section-title">Radius</div>
          ${renderRadiusGroup(machine)}
        </div>` : ""}
    </article>`;
}

function renderPreviewTile(machine, board) {
  return renderBoardTile(machine, board, { id: `preview-${machine.id}` }, false)
    .replace("board-builder-tile", "board-live-tile");
}

function upsertBoard(board) {
  const nextBoards = [...state.boards];
  const index = nextBoards.findIndex((item) => Number(item.id) === Number(board.id));
  if (index >= 0) {
    nextBoards[index] = board;
  } else {
    nextBoards.unshift(board);
  }
  state.boards = nextBoards;
}

function normalizeBoardName(value) {
  return String(value || "").trim() || "New Board";
}

function setSourcesCollapsed(nextCollapsed, options = {}) {
  state.sourcesCollapsed = Boolean(nextCollapsed);
  boardBuilderEditLayout.classList.toggle("is-sources-collapsed", state.sourcesCollapsed);
  toggleSourcesBtn?.setAttribute("aria-expanded", state.sourcesCollapsed ? "false" : "true");
  if (toggleSourcesText) {
    toggleSourcesText.textContent = state.sourcesCollapsed ? "Expand" : "Minimize";
  }
  if (!options.silent) {
    showToast(state.sourcesCollapsed ? "Sources minimized for more canvas space." : "Sources expanded.", "info", "Layout", 1800);
  }
}

function setFocusMode(nextFocusMode, options = {}) {
  state.focusMode = Boolean(nextFocusMode);
  const shouldApplyFocus = state.focusMode && state.viewMode === "edit";
  managementPageShell?.classList.toggle("is-focus-mode", shouldApplyFocus);
  if (focusModeBtn) {
    focusModeBtn.textContent = state.focusMode ? "Exit Focus" : "Focus";
    focusModeBtn.classList.toggle("btn-warning", state.focusMode);
    focusModeBtn.classList.toggle("btn-outline-light", !state.focusMode);
  }
  if (state.focusMode) {
    setSourcesCollapsed(true, { silent: true });
  }
  if (!options.silent) {
    showToast(state.focusMode ? "Focus mode enabled." : "Focus mode disabled.", "info", "Layout", 1500);
  }
}

function autoCollapseSourcesForBoard(board = getActiveBoard()) {
  return board;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    throw new Error(data.error?.message || data.message || "Request failed");
  }
  return data.data;
}

async function loadBoardState() {
  state.boardState = await fetchJson(boardStateUrl);
}

async function loadBoards() {
  const data = await fetchJson(boardsUrl);
  state.boards = data.boards || [];
  if (state.boards.length) {
    state.activeBoardId = data.active_board_id || state.boards[0].id;
    return;
  }
  ensureDraftBoard();
  state.activeBoardId = DRAFT_BOARD_ID;
}

function renderBoardSelector() {
  if (!boardSelector) return;
  const activeBoard = getActiveBoard();
  const options = ['<option value="draft">New Board</option>']
    .concat(state.boards.map((board) => `<option value="${board.id}">${escapeHtml(board.name)}</option>`));
  boardSelector.innerHTML = options.join("");
  boardSelector.value = activeBoard ? String(activeBoard.id) : DRAFT_BOARD_ID;
  boardNameInput.value = activeBoard?.name || "New Board";
  modulePerformance.checked = Boolean(activeBoard?.show_performance);
  moduleHistory.checked = Boolean(activeBoard?.show_recent_history);
  moduleRadius.checked = Boolean(activeBoard?.show_radius);
  if (createBoardBtn) createBoardBtn.textContent = isDraftBoard(activeBoard) ? "Create Board" : "Create New Board";
  if (createBoardBtn) {
    createBoardBtn.classList.toggle("btn-primary", isDraftBoard(activeBoard));
    createBoardBtn.classList.toggle("btn-warning", !isDraftBoard(activeBoard));
  }
  if (saveBoardNameBtn) saveBoardNameBtn.disabled = !activeBoard || isDraftBoard(activeBoard);
  if (deleteBoardBtn) deleteBoardBtn.disabled = !activeBoard || isDraftBoard(activeBoard);
  if (previewBoardBtn) {
    previewBoardBtn.disabled = !activeBoard;
    previewBoardBtn.textContent = isDraftBoard(activeBoard) ? "Preview Draft" : "Back to Board View";
    previewBoardBtn.classList.toggle("btn-outline-info", !isDraftBoard(activeBoard));
    previewBoardBtn.classList.toggle("btn-outline-light", isDraftBoard(activeBoard));
  }
  if (previewSaveBtn) {
    previewSaveBtn.textContent = isDraftBoard(activeBoard) ? "Save Board" : "Board Saved";
    previewSaveBtn.disabled = !isDraftBoard(activeBoard);
  }
  const activeCount = (activeBoard?.items || []).length;
  if (canvasMachineCount) {
    canvasMachineCount.textContent = String(activeCount);
  }
  if (boardNameLabel) {
    boardNameLabel.textContent = isDraftBoard(activeBoard) ? "New Board Name" : "Edit Board Name";
  }
  if (boardModeBanner) {
    if (state.viewMode === "display") {
      boardModeBanner.textContent = `Mode: Board View${activeBoard?.name ? ` · ${activeBoard.name}` : ""}`;
    } else if (isDraftBoard(activeBoard)) {
      boardModeBanner.textContent = "Mode: Create New Board";
    } else {
      boardModeBanner.textContent = `Mode: Edit Existing Board · ${activeBoard?.name || "Board"}`;
    }
  }
  setMutatingState(isMutating);
}

function renderSourceLists() {
  const machines = [...getAvailableMachines()].sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { numeric: true }));
  const groups = [...new Set(machines.map((machine) => machine.machine_type).filter(Boolean))];
  if (groupSourceCount) {
    groupSourceCount.textContent = String(groups.length);
  }
  if (machineSourceCount) {
    machineSourceCount.textContent = String(machines.length);
  }

  groupDropSource.innerHTML = groups.length
    ? groups.map((group) => `
      <button class="board-builder-chip" type="button" draggable="true" data-source-type="machine_group" data-source-value="${escapeHtml(group)}">
        <i class="bi bi-collection"></i><span>${escapeHtml(group)}</span>
      </button>`).join("")
    : '<div class="small text-secondary">All machine groups already on the canvas.</div>';

  machineDropSource.innerHTML = machines.length
    ? machines.map((machine) => `
      <button class="board-builder-machine" type="button" draggable="true" data-source-type="machine" data-machine-id="${machine.id}">
        <span class="board-builder-machine__name">${escapeHtml(machine.name)}</span>
        <span class="board-builder-machine__meta">${escapeHtml(machine.machine_type || "Unassigned")} · ${escapeHtml(machine.department_name || "Unassigned")}</span>
      </button>`).join("")
    : `<div class="small text-secondary">${state.machineSearchTerm ? "No machines match this search." : "Every available machine is already on the canvas."}</div>`;
}

function renderStatusDock(machines) {
  const total = machines.length;
  const openAlerts = machines.filter((machine) => machine.active_alert && machine.active_alert.status === "OPEN").length;
  const workingAlerts = machines.filter((machine) => ["ACKNOWLEDGED", "ARRIVED"].includes(machine.active_alert?.status)).length;
  const healthy = total - openAlerts - workingAlerts - machines.filter((machine) => !machine.is_active).length;
  managementStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${openAlerts === 0 && workingAlerts === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__title">Build Mode</div>
        <div class="operator-status-dock__subcopy">${escapeHtml(getActiveBoard()?.name || "New Board")} · ${total} machines</div>
      </div>
      <div class="operator-status-dock__stats">
        <div class="operator-status-dock__stat"><i class="bi bi-check2-circle"></i><div><div class="operator-status-dock__stat-label">Healthy</div><div class="operator-status-dock__stat-value">${healthy}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-exclamation-triangle-fill"></i><div><div class="operator-status-dock__stat-label">Open</div><div class="operator-status-dock__stat-value">${openAlerts}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-tools"></i><div><div class="operator-status-dock__stat-label">Working</div><div class="operator-status-dock__stat-value">${workingAlerts}</div></div></div>
      </div>
    </div>`;
}

function activeBoardNeedsShiftStats(board = getActiveBoard()) {
  return Boolean(board && board.items?.length && (board.show_performance || board.show_recent_history));
}

async function ensureShiftStatsLoaded(force = false) {
  if (!activeBoardNeedsShiftStats()) return;
  const rangeKey = getShiftRangeKey();
  if (!force && state.shiftStatsLoadedRangeKey === rangeKey) return;
  if (state.shiftStatsLoadingPromise) return state.shiftStatsLoadingPromise;

  const params = new URLSearchParams({
    start: new Date(state.shiftRange.start).toISOString(),
    end: new Date(state.shiftRange.end).toISOString(),
  });
  state.shiftStatsLoadingPromise = fetchJson(`${reportDetailsUrl}?${params.toString()}`)
    .then((details) => {
      const nextStats = {};
      for (const detail of details) {
        const machineId = Number(detail.machine_id);
        if (!nextStats[machineId]) {
          nextStats[machineId] = {
            totalAlerts: 0,
            ackSum: 0,
            ackCount: 0,
            fixSum: 0,
            fixCount: 0,
            latestClosed: null,
          };
        }
        const stats = nextStats[machineId];
        stats.totalAlerts += 1;
        if (Number.isFinite(Number(detail.acknowledged_seconds)) && Number(detail.acknowledged_seconds) >= 0) {
          stats.ackSum += Number(detail.acknowledged_seconds);
          stats.ackCount += 1;
        }
        if (Number.isFinite(Number(detail.ack_to_clear_seconds)) && Number(detail.ack_to_clear_seconds) >= 0) {
          stats.fixSum += Number(detail.ack_to_clear_seconds);
          stats.fixCount += 1;
        }
        if (["RESOLVED", "CANCELLED"].includes(String(detail.status || "").toUpperCase())) {
          const candidateTime = new Date(detail.closed_at || detail.created_at || 0).getTime();
          const currentTime = new Date(stats.latestClosed?.closed_at || stats.latestClosed?.created_at || 0).getTime();
          if (!stats.latestClosed || candidateTime > currentTime) {
            stats.latestClosed = detail;
          }
        }
      }
      state.shiftStatsByMachineId = Object.fromEntries(
        Object.entries(nextStats).map(([machineId, stats]) => [
          machineId,
          {
            totalAlerts: String(stats.totalAlerts),
            averageAcknowledge: stats.ackCount ? formatElapsedDuration(stats.ackSum / stats.ackCount) : "—",
            averageFix: stats.fixCount ? formatElapsedDuration(stats.fixSum / stats.fixCount) : "—",
            latestClosed: stats.latestClosed,
          },
        ]),
      );
      state.shiftStatsLoadedRangeKey = rangeKey;
    })
    .finally(() => {
      state.shiftStatsLoadingPromise = null;
      renderCurrentView();
    });
  renderCurrentView();
  return state.shiftStatsLoadingPromise;
}

function renderCanvas() {
  const activeBoard = getActiveBoard();
  if (!activeBoard) {
    boardBuilderCanvas.innerHTML = `
      <div class="board-builder-empty">
        <div class="h4 mb-2">Create a board.</div>
        <div class="small text-secondary">Add machines or machine groups to the canvas, then save the board.</div>
      </div>`;
    renderStatusDock([]);
    return;
  }
  const items = (activeBoard.items || []).map((item) => ({ item, machine: getMachineById(item.machine_id) })).filter((row) => row.machine);
  boardBuilderCanvas.innerHTML = items.length
    ? `<div class="board-builder-grid">${items.map(({ item, machine }) => renderBoardTile(machine, activeBoard, item, true)).join("")}</div>`
    : `<div class="board-builder-empty">
        <div class="h4 mb-2">${escapeHtml(activeBoard.name || "New Board")} is empty.</div>
        <div class="small text-secondary">Drag a machine group or individual machine into the canvas.</div>
      </div>`;
  renderStatusDock(items.map((row) => row.machine));
  if (activeBoardNeedsShiftStats(activeBoard)) {
    ensureShiftStatsLoaded().catch(() => {});
  }
}

function renderPreview() {
  const activeBoard = getActiveBoard();
  if (!activeBoard) {
    previewBoardTitle.textContent = "Board Preview";
    previewBoardGrid.innerHTML = '<div class="board-builder-empty"><div class="h4 mb-2">No board selected.</div><div class="small text-secondary">Go back to edit mode and build a board first.</div></div>';
    renderStatusDock([]);
    return;
  }
  const machines = (activeBoard.items || []).map((item) => getMachineById(item.machine_id)).filter(Boolean);
  previewBoardTitle.textContent = activeBoard.name || "Board Preview";
  previewBoardGrid.innerHTML = machines.length
    ? machines.map((machine) => renderPreviewTile(machine, activeBoard)).join("")
    : '<div class="board-builder-empty"><div class="h4 mb-2">This board is empty.</div><div class="small text-secondary">Go back to edit mode and drag machines into the canvas.</div></div>';
  renderStatusDock(machines);
  if (activeBoardNeedsShiftStats(activeBoard)) {
    ensureShiftStatsLoaded().catch(() => {});
  }
}

function renderCurrentView() {
  const isPreview = state.viewMode === "display";
  managementPageShell?.classList.toggle("is-focus-mode", state.focusMode && !isPreview);
  boardBuilderEditLayout.classList.toggle("d-none", isPreview);
  boardBuilderPreviewShell.classList.toggle("d-none", !isPreview);
  previewModeBar.classList.toggle("d-none", !isPreview);
  previewBoardBtn.classList.toggle("d-none", isPreview);
  focusModeBtn?.classList.toggle("d-none", isPreview);
  createBoardBtn.classList.toggle("d-none", isPreview);
  saveBoardNameBtn?.classList.toggle("d-none", isPreview);
  deleteBoardBtn.classList.toggle("d-none", isPreview);
  managementStatusDock?.classList.toggle("d-none", !isPreview);
  headerEditBoardBtn?.classList.toggle("d-none", !isPreview);
  document.querySelector(".board-builder-toolbar")?.classList.toggle("d-none", isPreview);
  if (isPreview) {
    renderPreview();
    return;
  }
  boardBuilderEditLayout.classList.toggle("is-sources-collapsed", state.sourcesCollapsed);
  renderCanvas();
}

function setActiveBoard(boardId) {
  if (String(boardId) === DRAFT_BOARD_ID) {
    ensureDraftBoard();
    state.activeBoardId = DRAFT_BOARD_ID;
    state.viewMode = "edit";
    queueRender();
    return;
  }
  state.activeBoardId = Number(boardId);
}

function normalizeItemPositions(board) {
  board.items = (board.items || []).map((item, index) => ({ ...item, position: index }));
}

function updateDraftBoard(patch) {
  const draftBoard = ensureDraftBoard();
  Object.assign(draftBoard, patch);
  state.activeBoardId = DRAFT_BOARD_ID;
  state.viewMode = "edit";
  queueRender();
}

async function createOrSaveBoard() {
  await withMutation(async () => {
    const activeBoard = getActiveBoard();
    if (!isDraftBoard(activeBoard)) {
      ensureDraftBoard();
      state.activeBoardId = DRAFT_BOARD_ID;
      queueRender();
      return;
    }
    const draftBoard = ensureDraftBoard();
    const name = normalizeBoardName(boardNameInput.value || draftBoard.name);
    const board = await fetchJson(boardsUrl, {
      method: "POST",
      headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify({
        name,
        show_performance: draftBoard.show_performance,
        show_recent_history: draftBoard.show_recent_history,
        show_radius: draftBoard.show_radius,
        machine_ids: draftBoard.items.map((item) => item.machine_id),
      }),
    });
    upsertBoard(board);
    state.draftBoard = createDraftBoard();
    state.activeBoardId = board.id;
    state.viewMode = "display";
    queueRender();
    showToast(`Saved board "${board.name}".`, "success", "Saved");
  }, "Saving board...");
}

async function activateBoard(boardId) {
  const localBoard = state.boards.find((item) => Number(item.id) === Number(boardId));
  if (localBoard) {
    state.activeBoardId = localBoard.id;
    queueRender();
  }
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${boardId}/activate`, {
      method: "POST",
      headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    });
    upsertBoard(board);
    state.activeBoardId = board.id;
    state.viewMode = "display";
    autoCollapseSourcesForBoard(board);
    queueRender();
    showToast(`Loaded board "${board.name}".`, "success", "Loaded", 2000);
  }, "Loading board...");
}

async function patchActiveBoard(payload, options = {}) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (isDraftBoard(activeBoard)) {
    updateDraftBoard({
      name: normalizeBoardName(boardNameInput.value || activeBoard.name),
      show_performance: Boolean(payload.show_performance ?? modulePerformance.checked),
      show_recent_history: Boolean(payload.show_recent_history ?? moduleHistory.checked),
      show_radius: Boolean(payload.show_radius ?? moduleRadius.checked),
    });
    return;
  }
  const normalizedPayload = { ...payload };
  if ("name" in normalizedPayload) {
    normalizedPayload.name = normalizeBoardName(normalizedPayload.name);
  }
  const noNameChange = !("name" in normalizedPayload) || normalizedPayload.name === activeBoard.name;
  const noPerformanceChange = !("show_performance" in normalizedPayload) || Boolean(normalizedPayload.show_performance) === Boolean(activeBoard.show_performance);
  const noHistoryChange = !("show_recent_history" in normalizedPayload) || Boolean(normalizedPayload.show_recent_history) === Boolean(activeBoard.show_recent_history);
  const noRadiusChange = !("show_radius" in normalizedPayload) || Boolean(normalizedPayload.show_radius) === Boolean(activeBoard.show_radius);
  if (noNameChange && noPerformanceChange && noHistoryChange && noRadiusChange) return;
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${activeBoard.id}`, {
      method: "PATCH",
      headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify(normalizedPayload),
    });
    upsertBoard(board);
    state.activeBoardId = board.id;
    queueRender();
    if (options.toast !== false) {
      showToast("Board updated.", "success", "Saved", 1800);
    }
  }, "Updating board settings...", { background: Boolean(options.background) });
}

function scheduleModulePatch() {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  activeBoard.show_performance = modulePerformance.checked;
  activeBoard.show_recent_history = moduleHistory.checked;
  activeBoard.show_radius = moduleRadius.checked;
  queueRender();
  if (modulePatchTimer) {
    window.clearTimeout(modulePatchTimer);
  }
  modulePatchTimer = window.setTimeout(() => {
    runSafely(
      () =>
        patchActiveBoard(
          {
            show_performance: modulePerformance.checked,
            show_recent_history: moduleHistory.checked,
            show_radius: moduleRadius.checked,
          },
          { background: true, toast: false },
        ),
      "Unable to update board modules",
    );
  }, 180);
}

async function deleteActiveBoard() {
  const activeBoard = getActiveBoard();
  if (!activeBoard || isDraftBoard(activeBoard)) return;
  if (!window.confirm(`Delete ${activeBoard.name}?`)) return;
  await withMutation(async () => {
    await fetchJson(`${boardsUrl}/${activeBoard.id}`, {
      method: "DELETE",
      headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    });
    state.boards = state.boards.filter((board) => Number(board.id) !== Number(activeBoard.id));
    if (state.boards.length) {
      state.activeBoardId = state.boards[0].id;
    } else {
      ensureDraftBoard();
      state.activeBoardId = DRAFT_BOARD_ID;
    }
    queueRender();
    showToast("Board deleted.", "success", "Deleted", 1800);
  }, "Deleting board...");
}

function addMachineToDraft(machineId) {
  const board = ensureDraftBoard();
  if (getBoardMachineIds(board).has(Number(machineId))) return;
  board.items.push({ id: `draft-${nextDraftItemId++}`, machine_id: Number(machineId), position: board.items.length });
  normalizeItemPositions(board);
  autoCollapseSourcesForBoard(board);
  queueRender();
  showToast("Machine added to draft board.", "success", "Builder", 1400);
}

function resolveBulkMachineIds(sourceType, sourceValue) {
  const availableMachineIds = getBoardMachineIds();
  const machines = [...(state.boardState.machines || [])];
  if (sourceType === "machine_group") {
    return machines
      .filter((machine) => machine.machine_type === sourceValue && !availableMachineIds.has(Number(machine.id)))
      .sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { numeric: true }))
      .map((machine) => Number(machine.id));
  }
  return [];
}

function bulkAddToDraft(sourceType, sourceValue) {
  const board = ensureDraftBoard();
  const currentIds = getBoardMachineIds(board);
  const startLength = board.items.length;
  for (const machineId of resolveBulkMachineIds(sourceType, sourceValue)) {
    if (currentIds.has(machineId)) continue;
    board.items.push({ id: `draft-${nextDraftItemId++}`, machine_id: machineId, position: board.items.length });
  }
  normalizeItemPositions(board);
  autoCollapseSourcesForBoard(board);
  queueRender();
  const addedCount = board.items.length - startLength;
  if (addedCount > 0) {
    showToast(`Added ${addedCount} machines to draft board.`, "success", "Builder", 1600);
  }
}

async function addMachineToBoard(machineId) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (isDraftBoard(activeBoard)) {
    addMachineToDraft(machineId);
    return;
  }
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items`, {
      method: "POST",
      headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify({ machine_id: machineId }),
    });
    upsertBoard(board);
    autoCollapseSourcesForBoard(board);
    queueRender();
    showToast("Machine added to board.", "success", "Builder", 1400);
  }, "Adding machine...");
}

async function bulkAddToBoard(sourceType, sourceValue) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (isDraftBoard(activeBoard)) {
    bulkAddToDraft(sourceType, sourceValue);
    return;
  }
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/bulk-add`, {
      method: "POST",
      headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify({ source_type: sourceType, source_value: sourceValue }),
    });
    upsertBoard(board);
    autoCollapseSourcesForBoard(board);
    queueRender();
    showToast("Machine group added to board.", "success", "Builder", 1600);
  }, "Adding machine group...");
}

async function removeBoardItem(itemId) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (isDraftBoard(activeBoard)) {
    activeBoard.items = activeBoard.items.filter((item) => String(item.id) !== String(itemId));
    normalizeItemPositions(activeBoard);
    queueRender();
    showToast("Machine removed from draft board.", "info", "Builder", 1400);
    return;
  }
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items/${itemId}`, {
      method: "DELETE",
      headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
      credentials: "same-origin",
    });
    upsertBoard(board);
    queueRender();
    showToast("Machine removed from board.", "info", "Builder", 1400);
  }, "Removing machine...");
}

async function reorderBoardItems(itemIds) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (isDraftBoard(activeBoard)) {
    const itemsById = Object.fromEntries(activeBoard.items.map((item) => [String(item.id), item]));
    activeBoard.items = itemIds.map((itemId) => itemsById[String(itemId)]).filter(Boolean);
    normalizeItemPositions(activeBoard);
    queueRender();
    return;
  }
  await withMutation(async () => {
    const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items/reorder`, {
      method: "PATCH",
      headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify({ item_ids: itemIds }),
    });
    upsertBoard(board);
    queueRender();
  }, "Reordering...");
}

function getDraggedPayload(target) {
  if (target.dataset.machineId && target.dataset.sourceType === "machine") {
    return { sourceType: "machine", machineId: target.dataset.machineId };
  }
  if (target.dataset.sourceType && target.dataset.sourceValue) {
    return { sourceType: target.dataset.sourceType, sourceValue: target.dataset.sourceValue };
  }
  if (target.dataset.boardItemId) {
    return { boardItemId: target.dataset.boardItemId };
  }
  return null;
}

async function handleDropPayload(payload, dropTarget) {
  if (!payload) return;
  if (payload.machineId) {
    await addMachineToBoard(payload.machineId);
    return;
  }
  if (payload.sourceType && payload.sourceValue) {
    await bulkAddToBoard(payload.sourceType, payload.sourceValue);
    return;
  }
  if (payload.boardItemId && dropTarget?.dataset.boardItemId && payload.boardItemId !== dropTarget.dataset.boardItemId) {
    const currentIds = Array.from(boardBuilderCanvas.querySelectorAll("[data-board-item-id]")).map((node) => node.dataset.boardItemId);
    const draggedId = String(payload.boardItemId);
    const targetId = String(dropTarget.dataset.boardItemId);
    const withoutDragged = currentIds.filter((id) => id !== draggedId);
    const targetIndex = withoutDragged.indexOf(targetId);
    if (targetIndex < 0) {
      withoutDragged.push(draggedId);
    } else {
      withoutDragged.splice(targetIndex, 0, draggedId);
    }
    await reorderBoardItems(withoutDragged);
  }
}

function wireDragAndDrop() {
  document.addEventListener("dragstart", (event) => {
    const target = event.target.closest("[data-machine-id], [data-source-type], [data-board-item-id]");
    if (!target) return;
    const payload = getDraggedPayload(target);
    if (!payload) return;
    event.dataTransfer.setData("application/json", JSON.stringify(payload));
  });
  document.addEventListener("dragover", (event) => {
    const dropNode = event.target.closest("[data-drop-zone='board-items'], [data-board-item-id]");
    if (dropNode) {
      event.preventDefault();
      boardBuilderCanvas?.classList.add("is-drop-active");
    }
  });
  document.addEventListener("dragleave", (event) => {
    if (!event.relatedTarget || !boardBuilderCanvas?.contains(event.relatedTarget)) {
      boardBuilderCanvas?.classList.remove("is-drop-active");
    }
  });
  document.addEventListener("drop", async (event) => {
    boardBuilderCanvas?.classList.remove("is-drop-active");
    const zone = event.target.closest("[data-drop-zone='board-items'], [data-board-item-id]");
    if (!zone) return;
    event.preventDefault();
    try {
      const payload = JSON.parse(event.dataTransfer.getData("application/json") || "{}");
      await handleDropPayload(payload, zone.closest("[data-board-item-id]"));
    } catch (_error) {
      // Ignore malformed drag payloads.
    }
  });
}

function setDetailModalDefaults(machine) {
  activeDetailMachine = machine || null;
  managementDetailModalTitle.textContent = machine ? `${machine.name} Summary` : "Press Summary";
  managementDetailStart.value = state.shiftRange.start || "";
  managementDetailEnd.value = state.shiftRange.end || "";
  managementDetailModalSubtitle.textContent = `${machine?.machine_type || "Unassigned"} · ${state.shiftRange.label || "Current shift"}`;
  managementDetailSummary.innerHTML = "";
}

function renderManagementDetailRows(details) {
  if (!details.length) {
    return '<tr><td colspan="8" class="text-secondary">No alerts in this range.</td></tr>';
  }
  return details.map((detail) => `
    <tr>
      <td>${escapeHtml(detail.department_name || "Unassigned")}</td>
      <td>${escapeHtml([detail.issue_category_name, detail.issue_problem_name].filter(Boolean).join(" - ") || "Unassigned")}</td>
      <td>${escapeHtml(detail.responder_name_text || "—")}</td>
      <td>${escapeHtml(detail.created_at || "—")}</td>
      <td>${escapeHtml(detail.closed_at || "—")}</td>
      <td>${escapeHtml(detail.acknowledged_seconds != null ? formatElapsedDuration(detail.acknowledged_seconds) : "—")}</td>
      <td>${escapeHtml(detail.ack_to_clear_seconds != null ? formatElapsedDuration(detail.ack_to_clear_seconds) : "—")}</td>
      <td>${escapeHtml(detail.total_seconds != null ? formatElapsedDuration(detail.total_seconds) : "—")}</td>
    </tr>`).join("");
}

async function loadManagementDetailSummary() {
  if (!activeDetailMachine) return;
  const requestId = ++detailRequestId;
  const params = new URLSearchParams({
    start: new Date(managementDetailStart.value || state.shiftRange.start).toISOString(),
    end: new Date(managementDetailEnd.value || state.shiftRange.end).toISOString(),
    machine_id: activeDetailMachine.id,
  });
  managementDetailTableBody.innerHTML = '<tr><td colspan="8" class="text-secondary">Loading shift summary...</td></tr>';
  try {
    const details = await fetchJson(`${reportDetailsUrl}?${params.toString()}`);
    if (requestId !== detailRequestId) return;
    const ackValues = details.map((detail) => Number(detail.acknowledged_seconds)).filter((value) => Number.isFinite(value) && value >= 0);
    const fixValues = details.map((detail) => Number(detail.ack_to_clear_seconds)).filter((value) => Number.isFinite(value) && value >= 0);
    managementDetailSummary.innerHTML = `
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Alerts</div><div class="management-detail-modal__summary-value">${details.length}</div></div>
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Avg Ack</div><div class="management-detail-modal__summary-value">${escapeHtml(ackValues.length ? formatElapsedDuration(ackValues.reduce((sum, value) => sum + value, 0) / ackValues.length) : "—")}</div></div>
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Avg Fix</div><div class="management-detail-modal__summary-value">${escapeHtml(fixValues.length ? formatElapsedDuration(fixValues.reduce((sum, value) => sum + value, 0) / fixValues.length) : "—")}</div></div>`;
    managementDetailTableBody.innerHTML = renderManagementDetailRows(details);
  } catch (error) {
    if (requestId !== detailRequestId) return;
    managementDetailTableBody.innerHTML = `<tr><td colspan="8" class="text-danger">${escapeHtml(error.message || "Unable to load machine details")}</td></tr>`;
  }
}

function wireEvents() {
  createBoardBtn?.addEventListener("click", () => runSafely(() => createOrSaveBoard(), "Unable to save board"));
  saveBoardNameBtn?.addEventListener("click", () => runSafely(() => patchActiveBoard({ name: boardNameInput.value }), "Unable to rename board"));
  focusModeBtn?.addEventListener("click", () => {
    setFocusMode(!state.focusMode);
    renderCurrentView();
  });
  previewBoardBtn?.addEventListener("click", () => {
    state.viewMode = "display";
    renderCurrentView();
    renderBoardSelector();
    updateFlowSteps();
    showToast("Board view enabled.", "info", "View", 1500);
  });
  previewEditBtn?.addEventListener("click", () => {
    state.viewMode = "edit";
    renderCurrentView();
    renderBoardSelector();
    updateFlowSteps();
    showToast("Build mode enabled.", "info", "Builder", 1500);
  });
  headerEditBoardBtn?.addEventListener("click", () => {
    state.viewMode = "edit";
    renderCurrentView();
    renderBoardSelector();
    updateFlowSteps();
    showToast("Build mode enabled.", "info", "Builder", 1500);
  });
  previewSaveBtn?.addEventListener("click", () => runSafely(() => createOrSaveBoard(), "Unable to save board"));
  deleteBoardBtn?.addEventListener("click", () => runSafely(() => deleteActiveBoard(), "Unable to delete board"));
  toggleSourcesBtn?.addEventListener("click", () => {
    setSourcesCollapsed(!state.sourcesCollapsed);
    renderCurrentView();
  });
  boardSelector?.addEventListener("change", () => {
    if (boardSelector.value === DRAFT_BOARD_ID) {
      setActiveBoard(DRAFT_BOARD_ID);
      return;
    }
    runSafely(() => activateBoard(boardSelector.value), "Unable to load board");
  });
  machineSearchInput?.addEventListener("input", () => {
    state.machineSearchTerm = machineSearchInput.value || "";
    queueRender();
  });
  boardNameInput?.addEventListener("input", () => {
    const activeBoard = getActiveBoard();
    if (isDraftBoard(activeBoard)) {
      activeBoard.name = boardNameInput.value || "New Board";
    }
  });
  boardNameInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const activeBoard = getActiveBoard();
    if (!activeBoard) return;
    if (isDraftBoard(activeBoard)) {
      runSafely(() => createOrSaveBoard(), "Unable to save board");
      return;
    }
    runSafely(() => patchActiveBoard({ name: boardNameInput.value }), "Unable to rename board");
  });
  [modulePerformance, moduleHistory, moduleRadius].forEach((input) => {
    input?.addEventListener("change", scheduleModulePatch);
  });
  boardBuilderCanvas?.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-board-item]");
    if (removeButton) {
      runSafely(() => removeBoardItem(removeButton.dataset.removeBoardItem), "Unable to remove machine");
      return;
    }
    const tile = event.target.closest("[data-machine-id]");
    if (!tile) return;
    const machine = getMachineById(tile.dataset.machineId);
    if (!machine) return;
    setDetailModalDefaults(machine);
    managementDetailModal?.show();
    loadManagementDetailSummary();
  });
  managementDetailRefresh?.addEventListener("click", loadManagementDetailSummary);
  managementDetailModalEl?.addEventListener("hidden.bs.modal", () => {
    activeDetailMachine = null;
    detailRequestId += 1;
  });
}

async function boot() {
  wireDragAndDrop();
  wireEvents();
  setBusyOverlay(true, "Preparing board builder...");
  await Promise.all([loadBoardState(), loadBoards()]);
  setBusyOverlay(false);
  setSourcesCollapsed(false, { silent: true });
  setFocusMode(false, { silent: true });
  state.viewMode = state.boards.length > 0 ? "display" : "edit";
  queueRender();
  showToast(state.boards.length > 0 ? "Board view loaded." : "Build mode ready.", "success", "Ready", 1400);
}

boot().catch((error) => {
  setBusyOverlay(false);
  boardBuilderCanvas.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load board builder")}</div>`;
  reportError(error, "Unable to load board builder");
});
