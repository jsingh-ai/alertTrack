const boardStateUrl = "/api/andon/board-state";
const reportDetailsUrl = "/api/andon/reports/machine-details";
const boardsUrl = "/api/andon/boards";
const managementDefaults = window.AndonManagementDefaults || {};

const boardSelector = document.getElementById("boardSelector");
const boardNameInput = document.getElementById("boardNameInput");
const createBoardBtn = document.getElementById("createBoardBtn");
const saveBoardNameBtn = document.getElementById("saveBoardNameBtn");
const deleteBoardBtn = document.getElementById("deleteBoardBtn");
const modulePerformance = document.getElementById("modulePerformance");
const moduleHistory = document.getElementById("moduleHistory");
const moduleRadius = document.getElementById("moduleRadius");
const groupDropSource = document.getElementById("groupDropSource");
const departmentDropSource = document.getElementById("departmentDropSource");
const machineDropSource = document.getElementById("machineDropSource");
const boardBuilderCanvas = document.getElementById("boardBuilderCanvas");
const managementStatusDock = document.getElementById("managementStatusDock");
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
  boardState: { machines: [], departments: [] },
  shiftDetails: [],
  boards: [],
  activeBoardId: null,
  shiftRange: {
    start: managementDefaults.shiftStart || "",
    end: managementDefaults.shiftEnd || "",
    label: managementDefaults.shiftLabel || "Current shift",
  },
};

let activeDetailMachine = null;
let detailRequestId = 0;

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

function formatAverageDuration(values) {
  const usable = values.filter((value) => Number.isFinite(value) && value >= 0);
  if (!usable.length) return "—";
  return formatElapsedDuration(usable.reduce((sum, value) => sum + value, 0) / usable.length);
}

function formatClockTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function getActiveBoard() {
  return state.boards.find((board) => Number(board.id) === Number(state.activeBoardId)) || null;
}

function getMachineById(machineId) {
  return (state.boardState.machines || []).find((machine) => Number(machine.id) === Number(machineId)) || null;
}

function getMachineStats(machineId) {
  const details = state.shiftDetails.filter((detail) => Number(detail.machine_id) === Number(machineId));
  return {
    totalAlerts: details.length,
    averageAcknowledge: formatAverageDuration(details.map((detail) => Number(detail.acknowledged_seconds))),
    averageFix: formatAverageDuration(details.map((detail) => Number(detail.ack_to_clear_seconds))),
    latestClosed: details
      .filter((detail) => ["RESOLVED", "CANCELLED"].includes(String(detail.status || "").toUpperCase()))
      .sort((a, b) => new Date(b.closed_at || b.created_at || 0).getTime() - new Date(a.closed_at || a.created_at || 0).getTime())[0] || null,
  };
}

function radiusValue(value) {
  return escapeHtml(value || "N/A");
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
  const lastClosed = stats.latestClosed;
  const issue = active ? String(active.problem_name || active.category_name || "Unassigned").trim() : "";
  const responder = active ? String(active.responder_name_text || "").trim() : "";
  const lastIssue = lastClosed
    ? [lastClosed.department_name, lastClosed.issue_problem_name || lastClosed.issue_category_name].filter(Boolean).join(" - ") || "Unassigned"
    : "No recent issue";
  return `
    <article
      class="management-machine-card board-builder-tile"
      data-board-item-id="${item.id}"
      data-machine-id="${machine.id}"
      draggable="${editable ? "true" : "false"}"
    >
      ${editable ? `<button class="board-builder-tile__remove" type="button" data-remove-board-item="${item.id}" aria-label="Remove ${escapeHtml(machine.name)}">×</button>` : ""}
      <div class="management-machine-card__hero management-machine-card__hero--${machine.active_alert ? "status-open" : "status-healthy"}">
        <div class="management-machine-card__title-row">
          <div class="management-machine-card__title">${escapeHtml(machine.name)}</div>
          <span class="board-builder-tile__meta">${escapeHtml(machine.machine_type || "Unassigned")}</span>
        </div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-text">${escapeHtml(machine.department_name || "Unassigned")}</span>
        </div>
      </div>
      ${board.show_performance ? `
        <div class="management-machine-card__section management-machine-card__section--metrics">
          <div class="management-machine-card__section-title">Performance</div>
          <div class="management-machine-card__metrics">
            <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Today</div><div class="management-machine-card__metric-value">${stats.totalAlerts}</div></div>
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

async function loadShiftDetails() {
  const params = new URLSearchParams({
    start: new Date(state.shiftRange.start).toISOString(),
    end: new Date(state.shiftRange.end).toISOString(),
  });
  state.shiftDetails = await fetchJson(`${reportDetailsUrl}?${params.toString()}`);
}

async function loadBoards() {
  const data = await fetchJson(boardsUrl);
  state.boards = data.boards || [];
  state.activeBoardId = data.active_board_id || (state.boards[0]?.id ?? null);
}

function renderBoardSelector() {
  if (!boardSelector) return;
  boardSelector.innerHTML = state.boards.length
    ? state.boards.map((board) => `<option value="${board.id}">${escapeHtml(board.name)}</option>`).join("")
    : '<option value="">No boards yet</option>';
  boardSelector.value = state.activeBoardId ? String(state.activeBoardId) : "";
  const activeBoard = getActiveBoard();
  boardNameInput.value = activeBoard?.name || "";
  modulePerformance.checked = Boolean(activeBoard?.show_performance);
  moduleHistory.checked = Boolean(activeBoard?.show_recent_history);
  moduleRadius.checked = Boolean(activeBoard?.show_radius);
  const disabled = !activeBoard;
  boardNameInput.disabled = disabled;
  saveBoardNameBtn.disabled = disabled;
  deleteBoardBtn.disabled = disabled;
  modulePerformance.disabled = disabled;
  moduleHistory.disabled = disabled;
  moduleRadius.disabled = disabled;
}

function renderSourceLists() {
  const machines = [...(state.boardState.machines || [])].sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { numeric: true }));
  const groups = [...new Set(machines.map((machine) => machine.machine_type).filter(Boolean))];
  const departments = [...new Set(machines.map((machine) => machine.department_name).filter(Boolean))];

  groupDropSource.innerHTML = groups.map((group) => `
    <button class="board-builder-chip" type="button" draggable="true" data-source-type="machine_group" data-source-value="${escapeHtml(group)}">
      <i class="bi bi-collection"></i><span>${escapeHtml(group)}</span>
    </button>`).join("");

  departmentDropSource.innerHTML = departments.map((department) => `
    <button class="board-builder-chip" type="button" draggable="true" data-source-type="department" data-source-value="${escapeHtml(department)}">
      <i class="bi bi-diagram-3"></i><span>${escapeHtml(department)}</span>
    </button>`).join("");

  machineDropSource.innerHTML = machines.map((machine) => `
    <button class="board-builder-machine" type="button" draggable="true" data-source-type="machine" data-machine-id="${machine.id}">
      <span class="board-builder-machine__name">${escapeHtml(machine.name)}</span>
      <span class="board-builder-machine__meta">${escapeHtml(machine.machine_type || "Unassigned")} · ${escapeHtml(machine.department_name || "Unassigned")}</span>
    </button>`).join("");
}

function renderCanvas() {
  const activeBoard = getActiveBoard();
  if (!activeBoard) {
    boardBuilderCanvas.innerHTML = `
      <div class="board-builder-empty">
        <div class="h4 mb-2">No saved board selected.</div>
        <div class="small text-secondary">Create a new board, then drag in a machine group, department, or individual machine.</div>
      </div>`;
    renderStatusDock([]);
    return;
  }
  const items = (activeBoard.items || []).map((item) => ({ item, machine: getMachineById(item.machine_id) })).filter((row) => row.machine);
  boardBuilderCanvas.innerHTML = items.length
    ? `<div class="board-builder-grid">${items.map(({ item, machine }) => renderBoardTile(machine, activeBoard, item, true)).join("")}</div>`
    : `<div class="board-builder-empty">
        <div class="h4 mb-2">${escapeHtml(activeBoard.name)} is empty.</div>
        <div class="small text-secondary">Drag a machine group, department, or machine into the canvas to build the board.</div>
      </div>`;
  renderStatusDock(items.map((row) => row.machine));
}

function renderStatusDock(machines) {
  const total = machines.length;
  const openAlerts = machines.filter((machine) => machine.active_alert && machine.active_alert.status === "OPEN").length;
  const workingAlerts = machines.filter((machine) => ["ACKNOWLEDGED", "ARRIVED"].includes(machine.active_alert?.status)).length;
  const healthy = total - openAlerts - workingAlerts - machines.filter((machine) => !machine.is_active).length;
  managementStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${openAlerts === 0 && workingAlerts === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__title">Board Builder</div>
        <div class="operator-status-dock__subcopy">${escapeHtml(getActiveBoard()?.name || "No board selected")} · ${total} machines</div>
      </div>
      <div class="operator-status-dock__stats">
        <div class="operator-status-dock__stat"><i class="bi bi-check2-circle"></i><div><div class="operator-status-dock__stat-label">Healthy</div><div class="operator-status-dock__stat-value">${healthy}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-exclamation-triangle-fill"></i><div><div class="operator-status-dock__stat-label">Open</div><div class="operator-status-dock__stat-value">${openAlerts}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-tools"></i><div><div class="operator-status-dock__stat-label">Working</div><div class="operator-status-dock__stat-value">${workingAlerts}</div></div></div>
      </div>
    </div>`;
}

async function createBoard() {
  const name = window.prompt("Board name", "New Board");
  if (name === null) return;
  const board = await fetchJson(boardsUrl, {
    method: "POST",
    headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
    credentials: "same-origin",
    body: JSON.stringify({ name }),
  });
  state.boards.unshift(board);
  state.activeBoardId = board.id;
  renderBoardSelector();
  renderCanvas();
}

async function activateBoard(boardId) {
  const board = await fetchJson(`${boardsUrl}/${boardId}/activate`, {
    method: "POST",
    headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
    credentials: "same-origin",
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  state.activeBoardId = board.id;
  renderBoardSelector();
  renderCanvas();
}

async function patchActiveBoard(payload) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  const board = await fetchJson(`${boardsUrl}/${activeBoard.id}`, {
    method: "PATCH",
    headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
    credentials: "same-origin",
    body: JSON.stringify(payload),
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  state.activeBoardId = board.id;
  renderBoardSelector();
  renderCanvas();
}

async function deleteActiveBoard() {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  if (!window.confirm(`Delete ${activeBoard.name}?`)) return;
  await fetchJson(`${boardsUrl}/${activeBoard.id}`, {
    method: "DELETE",
    headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
    credentials: "same-origin",
  });
  await loadBoards();
  renderBoardSelector();
  renderCanvas();
}

async function addMachineToBoard(machineId) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items`, {
    method: "POST",
    headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
    credentials: "same-origin",
    body: JSON.stringify({ machine_id: machineId }),
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  renderBoardSelector();
  renderCanvas();
}

async function bulkAddToBoard(sourceType, sourceValue) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/bulk-add`, {
    method: "POST",
    headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
    credentials: "same-origin",
    body: JSON.stringify({ source_type: sourceType, source_value: sourceValue }),
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  renderBoardSelector();
  renderCanvas();
}

async function removeBoardItem(itemId) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items/${itemId}`, {
    method: "DELETE",
    headers: window.AndonSecurity.withCsrfHeaders({ Accept: "application/json" }),
    credentials: "same-origin",
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  renderBoardSelector();
  renderCanvas();
}

async function reorderBoardItems(itemIds) {
  const activeBoard = getActiveBoard();
  if (!activeBoard) return;
  const board = await fetchJson(`${boardsUrl}/${activeBoard.id}/items/reorder`, {
    method: "PATCH",
    headers: window.AndonSecurity.withCsrfHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
    credentials: "same-origin",
    body: JSON.stringify({ item_ids: itemIds }),
  });
  state.boards = state.boards.map((item) => (Number(item.id) === Number(board.id) ? board : item));
  renderBoardSelector();
  renderCanvas();
}

function getDraggedPayload(target) {
  if (target.dataset.machineId) {
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
    const currentIds = Array.from(boardBuilderCanvas.querySelectorAll("[data-board-item-id]")).map((node) => Number(node.dataset.boardItemId));
    const draggedId = Number(payload.boardItemId);
    const targetId = Number(dropTarget.dataset.boardItemId);
    const withoutDragged = currentIds.filter((id) => id !== draggedId);
    const targetIndex = withoutDragged.indexOf(targetId);
    withoutDragged.splice(targetIndex, 0, draggedId);
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
    if (event.target.closest("[data-drop-zone='board-items'], [data-board-item-id]")) {
      event.preventDefault();
    }
  });
  document.addEventListener("drop", async (event) => {
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
    managementDetailSummary.innerHTML = `
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Alerts</div><div class="management-detail-modal__summary-value">${details.length}</div></div>
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Avg Ack</div><div class="management-detail-modal__summary-value">${escapeHtml(formatAverageDuration(details.map((detail) => Number(detail.acknowledged_seconds))))}</div></div>
      <div class="management-detail-modal__summary-chip"><div class="management-detail-modal__summary-label">Avg Fix</div><div class="management-detail-modal__summary-value">${escapeHtml(formatAverageDuration(details.map((detail) => Number(detail.ack_to_clear_seconds))))}</div></div>`;
    managementDetailTableBody.innerHTML = renderManagementDetailRows(details);
  } catch (error) {
    if (requestId !== detailRequestId) return;
    managementDetailTableBody.innerHTML = `<tr><td colspan="8" class="text-danger">${escapeHtml(error.message || "Unable to load machine details")}</td></tr>`;
  }
}

function wireEvents() {
  createBoardBtn?.addEventListener("click", () => createBoard().catch((error) => window.alert(error.message)));
  saveBoardNameBtn?.addEventListener("click", () => patchActiveBoard({ name: boardNameInput.value }).catch((error) => window.alert(error.message)));
  deleteBoardBtn?.addEventListener("click", () => deleteActiveBoard().catch((error) => window.alert(error.message)));
  boardSelector?.addEventListener("change", () => activateBoard(boardSelector.value).catch((error) => window.alert(error.message)));
  [modulePerformance, moduleHistory, moduleRadius].forEach((input) => {
    input?.addEventListener("change", () => patchActiveBoard({
      show_performance: modulePerformance.checked,
      show_recent_history: moduleHistory.checked,
      show_radius: moduleRadius.checked,
    }).catch((error) => window.alert(error.message)));
  });
  boardBuilderCanvas?.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-board-item]");
    if (removeButton) {
      removeBoardItem(removeButton.dataset.removeBoardItem).catch((error) => window.alert(error.message));
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
  await Promise.all([loadBoardState(), loadShiftDetails(), loadBoards()]);
  renderBoardSelector();
  renderSourceLists();
  renderCanvas();
}

boot().catch((error) => {
  boardBuilderCanvas.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load board builder")}</div>`;
});
