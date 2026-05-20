const boardStateUrl = "/api/andon/board-state?compact=1";
const reportDetailsUrl = "/api/andon/reports/machine-details";
const managementDefaults = window.AndonManagementDefaults || {};

const managementFiltersBtn = document.getElementById("managementFiltersBtn");
const managementFiltersPanel = document.getElementById("managementFiltersPanel");
const managementGroupFilter = document.getElementById("managementGroupFilter");
const managementSearchFilter = document.getElementById("managementSearchFilter");
const managementClearFiltersBtn = document.getElementById("managementClearFiltersBtn");
const managementStatusDock = document.getElementById("managementStatusDock");
const managementOverviewTitle = document.getElementById("managementOverviewTitle");
const managementOverviewGrid = document.getElementById("managementOverviewGrid");
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
  machines: [],
  selectedGroup: "all",
  search: "",
  shiftStatsByMachineId: {},
  shiftStatsLoaded: false,
  loadedAt: Date.now(),
};

let elapsedTimerIntervalId = null;
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

function formatClockTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function radiusValue(value) {
  return escapeHtml(value || "N/A");
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.success === false) {
    throw new Error(data.error?.message || data.message || "Request failed");
  }
  return data.data;
}

function getHealth(machine) {
  if (!machine?.is_active) return { label: "Offline", className: "status-off" };
  const alertStatus = String(machine?.active_alert?.status || "").toUpperCase();
  if (alertStatus === "OPEN") return { label: "Alert Open", className: "status-open" };
  if (alertStatus === "ACKNOWLEDGED" || alertStatus === "ARRIVED") return { label: "Being Worked", className: "status-acknowledged" };
  return { label: "machine running healthy", className: "status-healthy" };
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

function getMachineStats(machineId) {
  return state.shiftStatsByMachineId[Number(machineId)] || {
    totalAlerts: "—",
    averageAcknowledge: "—",
    averageFix: "—",
    latestClosed: null,
  };
}

function renderMachineCard(machine) {
  const health = getHealth(machine);
  const active = machine.active_alert;
  const stats = getMachineStats(machine.id);
  const lastClosed = stats.latestClosed;
  const issue = active ? String(active.problem_name || active.category_name || "Unassigned").trim() : "";
  const responder = active ? String(active.responder_name_text || "").trim() : "";
  const lastIssue = lastClosed
    ? [lastClosed.department_name, lastClosed.issue_problem_name || lastClosed.issue_category_name].filter(Boolean).join(" - ") || "Unassigned"
    : "No recent issue";
  const elapsedBase = Number(active?.elapsed_seconds || 0);

  return `
    <article class="management-machine-card management-machine-card--clickable board-live-tile" data-machine-id="${machine.id}" role="button" tabindex="0">
      <div class="management-machine-card__hero management-machine-card__hero--${health.className}">
        <div class="management-machine-card__title-row">
          <div class="management-machine-card__title">${escapeHtml(machine.name || "Machine")}</div>
        </div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-text">${escapeHtml(health.label)}</span>
        </div>
      </div>
      <div class="management-machine-card__section management-machine-card__section--metrics">
        <div class="management-machine-card__section-title">Performance</div>
        <div class="management-machine-card__metrics">
          <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Today</div><div class="management-machine-card__metric-value">${escapeHtml(stats.totalAlerts)}</div></div>
          <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Avg ack</div><div class="management-machine-card__metric-value">${escapeHtml(stats.averageAcknowledge)}</div></div>
          <div class="management-machine-card__metric"><div class="management-machine-card__metric-label">Avg fix</div><div class="management-machine-card__metric-value">${escapeHtml(stats.averageFix)}</div></div>
        </div>
      </div>
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
              <div class="management-machine-card__state-value management-machine-card__state-value--elapsed" data-live-elapsed="1" data-base-seconds="${elapsedBase}">${formatElapsedDuration(elapsedBase)}</div>
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
      </div>
      <div class="management-machine-card__section management-machine-card__section--radius">
        <div class="management-machine-card__section-title">Radius</div>
        ${renderRadiusGroup(machine)}
      </div>
    </article>`;
}

function getMachineGroups(machines) {
  return [...new Set(machines.map((item) => String(item.machine_type || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function pickDefaultGroup(groups) {
  const press = groups.find((group) => group.toLowerCase() === "press") || groups.find((group) => group.toLowerCase().includes("press"));
  return press || "all";
}

function getFilteredMachines() {
  const search = state.search.trim().toLowerCase();
  return state.machines.filter((machine) => {
    if (state.selectedGroup !== "all" && String(machine.machine_type || "") !== state.selectedGroup) return false;
    if (!search) return true;
    return String(machine.name || "").toLowerCase().includes(search);
  });
}

function renderStatusDock(machines) {
  const total = machines.length;
  const openAlerts = machines.filter((machine) => String(machine.active_alert?.status || "").toUpperCase() === "OPEN").length;
  const workingAlerts = machines.filter((machine) => ["ACKNOWLEDGED", "ARRIVED"].includes(String(machine.active_alert?.status || "").toUpperCase())).length;
  const offline = machines.filter((machine) => !machine.is_active).length;
  const healthy = Math.max(0, total - openAlerts - workingAlerts - offline);
  managementStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${openAlerts === 0 && workingAlerts === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__title">Management Overview</div>
        <div class="operator-status-dock__subcopy">${total} machines</div>
      </div>
      <div class="operator-status-dock__stats">
        <div class="operator-status-dock__stat"><i class="bi bi-check2-circle"></i><div><div class="operator-status-dock__stat-label">Healthy</div><div class="operator-status-dock__stat-value">${healthy}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-exclamation-triangle-fill"></i><div><div class="operator-status-dock__stat-label">Open</div><div class="operator-status-dock__stat-value">${openAlerts}</div></div></div>
        <div class="operator-status-dock__stat"><i class="bi bi-tools"></i><div><div class="operator-status-dock__stat-label">Working</div><div class="operator-status-dock__stat-value">${workingAlerts}</div></div></div>
      </div>
    </div>`;
}

function renderMachines(machines) {
  if (!machines.length) {
    managementOverviewGrid.innerHTML = '<div class="board-builder-empty"><div class="h4 mb-2">No machines found.</div><div class="small text-secondary">Try another filter or machine group.</div></div>';
    return;
  }
  managementOverviewGrid.innerHTML = machines.map((machine) => renderMachineCard(machine)).join("");
}

function render() {
  const filtered = getFilteredMachines();
  const title = state.selectedGroup === "all" ? "All Machine Groups" : state.selectedGroup;
  managementOverviewTitle.textContent = title;
  renderStatusDock(filtered);
  renderMachines(filtered);
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

function openDetailModal(machine) {
  activeDetailMachine = machine;
  detailRequestId += 1;
  managementDetailModalTitle.textContent = `${machine.name} Summary`;
  managementDetailModalSubtitle.textContent = `${machine.machine_type || "Unassigned"} · ${managementDefaults.shiftLabel || "Current shift"}`;
  managementDetailStart.value = managementDefaults.shiftStart || "";
  managementDetailEnd.value = managementDefaults.shiftEnd || "";
  managementDetailSummary.innerHTML = "";
  managementDetailTableBody.innerHTML = '<tr><td colspan="8" class="text-secondary">Loading shift summary...</td></tr>';
  managementDetailModal?.show();
  void loadManagementDetailSummary();
}

async function loadManagementDetailSummary() {
  if (!activeDetailMachine) return;
  const requestId = ++detailRequestId;
  const startIso = new Date(managementDetailStart.value || managementDefaults.shiftStart || Date.now() - 12 * 60 * 60 * 1000).toISOString();
  const endIso = new Date(managementDetailEnd.value || managementDefaults.shiftEnd || Date.now()).toISOString();
  const params = new URLSearchParams({
    start: startIso,
    end: endIso,
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

function renderGroupFilter() {
  const groups = getMachineGroups(state.machines);
  const options = ['<option value="all">All Groups</option>']
    .concat(groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`));
  managementGroupFilter.innerHTML = options.join("");
  managementGroupFilter.value = state.selectedGroup;
}

async function boot() {
  const boardState = await fetchJson(boardStateUrl);
  state.machines = boardState.machines || [];
  state.loadedAt = Date.now();
  await loadShiftStats();
  const groups = getMachineGroups(state.machines);
  state.selectedGroup = pickDefaultGroup(groups);
  renderGroupFilter();
  if (managementGroupFilter.value !== state.selectedGroup) {
    managementGroupFilter.value = state.selectedGroup;
  }
  render();
}

async function loadShiftStats() {
  const start = managementDefaults.shiftStart ? new Date(managementDefaults.shiftStart) : new Date(Date.now() - 12 * 60 * 60 * 1000);
  const end = managementDefaults.shiftEnd ? new Date(managementDefaults.shiftEnd) : new Date();
  const params = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
  const details = await fetchJson(`${reportDetailsUrl}?${params.toString()}`);
  const nextStats = {};
  for (const detail of details || []) {
    const machineId = Number(detail.machine_id);
    if (!nextStats[machineId]) {
      nextStats[machineId] = { totalAlerts: 0, ackSum: 0, ackCount: 0, fixSum: 0, fixCount: 0, latestClosed: null };
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
      const candidate = new Date(detail.closed_at || detail.created_at || 0).getTime();
      const current = new Date(stats.latestClosed?.closed_at || stats.latestClosed?.created_at || 0).getTime();
      if (!stats.latestClosed || candidate > current) stats.latestClosed = detail;
    }
  }
  state.shiftStatsByMachineId = Object.fromEntries(
    Object.entries(nextStats).map(([machineId, stats]) => [
      Number(machineId),
      {
        totalAlerts: String(stats.totalAlerts),
        averageAcknowledge: stats.ackCount ? formatElapsedDuration(stats.ackSum / stats.ackCount) : "—",
        averageFix: stats.fixCount ? formatElapsedDuration(stats.fixSum / stats.fixCount) : "—",
        latestClosed: stats.latestClosed,
      },
    ]),
  );
  state.shiftStatsLoaded = true;
}

function startElapsedTimer() {
  if (elapsedTimerIntervalId) return;
  elapsedTimerIntervalId = window.setInterval(() => {
    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.loadedAt) / 1000));
    document.querySelectorAll("[data-live-elapsed='1']").forEach((node) => {
      const base = Number(node.getAttribute("data-base-seconds") || 0);
      node.textContent = formatElapsedDuration(base + elapsedSeconds);
    });
  }, 1000);
}

managementFiltersBtn?.addEventListener("click", () => {
  managementFiltersPanel?.classList.toggle("d-none");
});

managementGroupFilter?.addEventListener("change", () => {
  state.selectedGroup = managementGroupFilter.value || "all";
  render();
});

managementSearchFilter?.addEventListener("input", () => {
  state.search = managementSearchFilter.value || "";
  render();
});

managementClearFiltersBtn?.addEventListener("click", () => {
  state.search = "";
  managementSearchFilter.value = "";
  state.selectedGroup = "all";
  managementGroupFilter.value = "all";
  render();
});

managementOverviewGrid?.addEventListener("click", (event) => {
  const tile = event.target.closest("[data-machine-id]");
  if (!tile) return;
  const machine = state.machines.find((item) => Number(item.id) === Number(tile.dataset.machineId));
  if (!machine) return;
  openDetailModal(machine);
});

managementOverviewGrid?.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const tile = event.target.closest("[data-machine-id]");
  if (!tile) return;
  event.preventDefault();
  const machine = state.machines.find((item) => Number(item.id) === Number(tile.dataset.machineId));
  if (!machine) return;
  openDetailModal(machine);
});

managementDetailRefresh?.addEventListener("click", () => {
  void loadManagementDetailSummary();
});

managementDetailModalEl?.addEventListener("hidden.bs.modal", () => {
  activeDetailMachine = null;
  detailRequestId += 1;
});

boot().catch((error) => {
  managementOverviewTitle.textContent = "Management Unavailable";
  managementOverviewGrid.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load management overview")}</div>`;
});
startElapsedTimer();
