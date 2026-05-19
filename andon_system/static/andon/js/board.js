const boardStateUrl = "/api/andon/board-state";
const reportDetailsUrl = "/api/andon/reports/machine-details";
const boardsUrl = "/api/andon/boards";
const boardGrid = document.getElementById("boardGrid");
const boardTitle = document.getElementById("boardTitle");
const boardStatusDock = document.getElementById("boardStatusDock");

const state = {
  boardState: { machines: [] },
  shiftDetails: [],
  boards: [],
  activeBoardId: null,
};

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

function renderTile(machine, board) {
  const stats = getMachineStats(machine.id);
  const active = machine.active_alert;
  const lastClosed = stats.latestClosed;
  const issue = active ? String(active.problem_name || active.category_name || "Unassigned").trim() : "";
  const responder = active ? String(active.responder_name_text || "").trim() : "";
  const lastIssue = lastClosed
    ? [lastClosed.department_name, lastClosed.issue_problem_name || lastClosed.issue_category_name].filter(Boolean).join(" - ") || "Unassigned"
    : "No recent issue";
  return `
    <article class="management-machine-card board-live-tile">
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
  const end = new Date();
  const start = new Date(end.getTime() - (12 * 60 * 60 * 1000));
  const params = new URLSearchParams({
    start: start.toISOString(),
    end: end.toISOString(),
  });
  state.shiftDetails = await fetchJson(`${reportDetailsUrl}?${params.toString()}`);
}

async function loadBoards() {
  const data = await fetchJson(boardsUrl);
  state.boards = data.boards || [];
  state.activeBoardId = data.active_board_id || (state.boards[0]?.id ?? null);
}

function renderStatusDock(machines, boardName) {
  const total = machines.length;
  const openAlerts = machines.filter((machine) => machine.active_alert && machine.active_alert.status === "OPEN").length;
  const workingAlerts = machines.filter((machine) => ["ACKNOWLEDGED", "ARRIVED"].includes(machine.active_alert?.status)).length;
  const healthy = total - openAlerts - workingAlerts - machines.filter((machine) => !machine.is_active).length;
  boardStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${openAlerts === 0 && workingAlerts === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__title">${escapeHtml(boardName || "Saved Board")}</div>
        <div class="operator-status-dock__subcopy">${total} machines on this board</div>
      </div>
      <div class="operator-status-dock__stats">
        <div class="operator-status-dock__stat"><i class="bi bi-check2-circle"></i><div><div class="operator-status-dock__stat-label">Healthy</div><div class="operator-status-dock__stat-value">${healthy}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-exclamation-triangle-fill"></i><div><div class="operator-status-dock__stat-label">Open</div><div class="operator-status-dock__stat-value">${openAlerts}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-tools"></i><div><div class="operator-status-dock__stat-label">Working</div><div class="operator-status-dock__stat-value">${workingAlerts}</div></div></div>
      </div>
    </div>`;
}

function renderBoard() {
  const board = getActiveBoard();
  if (!board) {
    boardTitle.textContent = "No saved board";
    boardGrid.innerHTML = '<div class="board-builder-empty"><div class="h4 mb-2">No board has been built yet.</div><div class="small text-secondary">Open Management to create and save a board.</div></div>';
    renderStatusDock([], "No board selected");
    return;
  }
  boardTitle.textContent = board.name;
  const machines = (board.items || []).map((item) => getMachineById(item.machine_id)).filter(Boolean);
  boardGrid.innerHTML = machines.length
    ? machines.map((machine) => renderTile(machine, board)).join("")
    : '<div class="board-builder-empty"><div class="h4 mb-2">This board is empty.</div><div class="small text-secondary">Open Management to add machines to the board.</div></div>';
  renderStatusDock(machines, board.name);
}

async function boot() {
  await Promise.all([loadBoardState(), loadShiftDetails(), loadBoards()]);
  renderBoard();
}

boot().catch((error) => {
  boardTitle.textContent = "Board unavailable";
  boardGrid.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load board")}</div>`;
});
