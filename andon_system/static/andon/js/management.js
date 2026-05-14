const boardUrl = "/api/andon/board-state";
const reportDetailsUrl = "/api/andon/reports/machine-details";
const managementViewStorageKey = "andon-management-view";
const managementDefaults = window.AndonManagementDefaults || {};

const managementGrid = document.getElementById("managementGrid");
const managementStatusDock = document.getElementById("managementStatusDock");
const managementMachineGroupSelect = document.getElementById("managementMachineGroup");
const managementDepartmentSelect = document.getElementById("managementDepartment");
const managementLockView = document.getElementById("managementLockView");
const managementClearView = document.getElementById("managementClearView");
const managementLockStatus = document.getElementById("managementLockStatus");
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
  shiftRange: {
    start: managementDefaults.shiftStart || "",
    end: managementDefaults.shiftEnd || "",
    label: managementDefaults.shiftLabel || "Current shift",
  },
  filters: {
    machineGroup: "",
    department: "",
  },
  locked: false,
  refreshedAt: null,
};

let refreshTimeoutId = null;
let refreshInFlight = false;
let refreshQueued = false;
let fallbackPollIntervalId = null;
let liveTimerIntervalId = null;
let liveTimerNodes = [];
let detailRequestId = 0;
let activeDetailMachine = null;

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  return `status-${String(status || "").toLowerCase()}`;
}

function statusLabel(status) {
  if (status === "HEALTHY") return "Healthy";
  if (status === "OPEN") return "Open";
  if (status === "ACKNOWLEDGED") return "Working";
  if (status === "ARRIVED") return "Arrived";
  if (status === "RESOLVED") return "Resolved";
  if (status === "CANCELLED") return "Cancelled";
  if (status === "OFF") return "Offline";
  return String(status || "");
}

function formatElapsedDuration(totalSeconds) {
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

function formatAverageDuration(values) {
  const numericValues = values.filter((value) => Number.isFinite(value) && value >= 0);
  if (!numericValues.length) return "—";
  const average = Math.round(numericValues.reduce((sum, value) => sum + value, 0) / numericValues.length);
  return formatElapsedDuration(average);
}

function toLocalIsoString(date) {
  const pad = (value) => String(value).padStart(2, "0");
  const offsetMinutes = -date.getTimezoneOffset();
  const offsetSign = offsetMinutes >= 0 ? "+" : "-";
  const absOffset = Math.abs(offsetMinutes);
  const offsetHours = pad(Math.floor(absOffset / 60));
  const offsetMins = pad(absOffset % 60);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}${offsetSign}${offsetHours}:${offsetMins}`;
}

function formatDateTimeLocalValue(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function getDefaultShiftRange() {
  const start = managementDefaults.shiftStart || "";
  const end = managementDefaults.shiftEnd || "";
  return {
    start,
    end,
    label: managementDefaults.shiftLabel || "Current shift",
  };
}

function getTimeZoneOffsetMinutes(date, timeZone) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const parts = Object.fromEntries(formatter.formatToParts(date).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  const asUTC = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour),
    Number(parts.minute),
    Number(parts.second),
  );
  return (asUTC - date.getTime()) / 60000;
}

function zonedDateTimeToIso(value, timeZone = managementDefaults.timeZone || "America/Chicago") {
  if (!value) return "";
  const [datePart, timePart = "00:00:00"] = String(value).split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour = 0, minute = 0, second = 0] = timePart.split(":").map(Number);
  let utcMillis = Date.UTC(year, month - 1, day, hour, minute, second, 0);
  for (let index = 0; index < 3; index += 1) {
    const offset = getTimeZoneOffsetMinutes(new Date(utcMillis), timeZone);
    const adjusted = Date.UTC(year, month - 1, day, hour, minute, second, 0) - (offset * 60000);
    if (adjusted === utcMillis) break;
    utcMillis = adjusted;
  }
  return new Date(utcMillis).toISOString();
}

function getDetailRange() {
  return {
    start: managementDetailStart?.value || state.shiftRange.start || "",
    end: managementDetailEnd?.value || state.shiftRange.end || "",
  };
}

function loadSavedView() {
  try {
    const saved = JSON.parse(localStorage.getItem(managementViewStorageKey) || "{}");
    state.filters.machineGroup = saved.machineGroup || "";
    state.filters.department = saved.department || "";
    state.locked = typeof saved.locked === "boolean" ? saved.locked : false;
  } catch {
    state.filters.machineGroup = "";
    state.filters.department = "";
    state.locked = false;
  }
}

function saveView() {
  localStorage.setItem(managementViewStorageKey, JSON.stringify({
    machineGroup: state.filters.machineGroup,
    department: state.filters.department,
    locked: state.locked,
  }));
}

async function loadBoardState() {
  const response = await fetch(boardUrl);
  const data = await response.json();
  state.boardState = data.data || state.boardState;
  state.refreshedAt = Date.now();
}

async function loadShiftDetails() {
  const { start, end } = state.shiftRange;
  const response = await fetch(`${reportDetailsUrl}?${new URLSearchParams({
    start: zonedDateTimeToIso(start),
    end: zonedDateTimeToIso(end),
  }).toString()}`);
  const data = await response.json();
  state.shiftDetails = data.data || [];
}

function getGroups() {
  return [...new Set((state.boardState.machines || []).map((machine) => machine.machine_type || "").filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function getDepartments() {
  return [...new Set((state.boardState.departments || []).map((department) => department.name || "").filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function normalizeViewState() {
  const groups = new Set(getGroups());
  const departments = new Set(getDepartments());
  if (state.filters.machineGroup && !groups.has(state.filters.machineGroup)) {
    state.filters.machineGroup = "";
  }
  if (state.filters.department && !departments.has(state.filters.department)) {
    state.filters.department = "";
  }
  saveView();
}

function renderViewControls() {
  const groups = getGroups();
  const departments = getDepartments();
  managementMachineGroupSelect.innerHTML = `
    <option value="">All Groups</option>
    ${groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("")}
  `;
  managementDepartmentSelect.innerHTML = `
    <option value="">All Departments</option>
    ${departments.map((department) => `<option value="${escapeHtml(department)}">${escapeHtml(department)}</option>`).join("")}
  `;
  managementMachineGroupSelect.value = state.filters.machineGroup || "";
  managementDepartmentSelect.value = state.filters.department || "";
  managementMachineGroupSelect.disabled = state.locked;
  managementDepartmentSelect.disabled = state.locked;
  managementLockView.textContent = state.locked ? "Unlock View" : "Lock View";
  managementLockStatus.textContent = state.locked
    ? (state.filters.department ? `Locked to ${state.filters.department}` : state.filters.machineGroup ? `Locked to ${state.filters.machineGroup}` : "Locked")
    : "Unlocked";
}

function getVisibleMachines() {
  let machines = [...(state.boardState.machines || [])];
  if (state.filters.machineGroup) {
    machines = machines.filter((machine) => (machine.machine_type || "") === state.filters.machineGroup);
  }
  if (state.filters.department) {
    machines = machines.filter((machine) => (machine.department_name || "Unassigned") === state.filters.department);
  }
  return machines;
}

function groupByMachineGroup(machines) {
  const groups = new Map();
  machines.forEach((machine) => {
    const groupName = machine.machine_type || "Unassigned";
    if (!groups.has(groupName)) {
      groups.set(groupName, []);
    }
    groups.get(groupName).push(machine);
  });
  return groups;
}

function machineSortKey(machine) {
  const label = String(machine.name || "");
  const match = label.match(/(\d+)/);
  return {
    number: match ? Number(match[1]) : Number.POSITIVE_INFINITY,
    label,
  };
}

function sortMachinesInOrder(a, b) {
  const aKey = machineSortKey(a);
  const bKey = machineSortKey(b);
  if (Number.isFinite(aKey.number) && Number.isFinite(bKey.number) && aKey.number !== bKey.number) {
    return aKey.number - bKey.number;
  }
  if (Number.isFinite(aKey.number) !== Number.isFinite(bKey.number)) {
    return Number.isFinite(aKey.number) ? -1 : 1;
  }
  return aKey.label.localeCompare(bKey.label, undefined, { numeric: true, sensitivity: "base" });
}

function buildMachineStats(machineId, visibleDetails) {
  const alerts = visibleDetails.filter((detail) => detail.machine_id === machineId);
  const acknowledgedSeconds = alerts
    .map((detail) => Number(detail.acknowledged_seconds))
    .filter((value) => Number.isFinite(value));
  const ackToClearSeconds = alerts
    .map((detail) => Number(detail.ack_to_clear_seconds))
    .filter((value) => Number.isFinite(value));
  return {
    totalAlerts: alerts.length,
    averageAcknowledge: formatAverageDuration(acknowledgedSeconds),
    averageFix: formatAverageDuration(ackToClearSeconds),
  };
}

function getLatestClosedAlert(machineId, visibleDetails) {
  const closedAlerts = visibleDetails
    .filter((detail) => detail.machine_id === machineId && ["RESOLVED", "CANCELLED"].includes(String(detail.status || "").toUpperCase()))
    .sort((a, b) => {
      const aTime = new Date(a.closed_at || a.created_at || 0).getTime();
      const bTime = new Date(b.closed_at || b.created_at || 0).getTime();
      return bTime - aTime;
    });
  return closedAlerts[0] || null;
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

function renderRadiusGroup(machine) {
  const radius = machine?.radius || null;
  return `
    <div class="management-machine-card__radius">
      <div class="management-machine-card__radius-title">Radius</div>
      <div class="management-machine-card__radius-grid">
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Operator Code</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.operation_code)}</span>
        </div>
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Job Code</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.job_code)}</span>
        </div>
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Event Type</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.event_type)}</span>
        </div>
        <div class="management-machine-card__radius-item">
          <span class="management-machine-card__radius-label">Status</span>
          <span class="management-machine-card__radius-value">${radiusValue(radius?.status_label)}</span>
        </div>
      </div>
    </div>`;
}

function renderMachineCard(machine, visibleDetails) {
  const active = Boolean(machine.active_alert);
  const alert = machine.active_alert;
  const isOffline = !machine.is_active;
  const status = isOffline ? "OFF" : active ? alert.status : "HEALTHY";
  const issue = active ? String(alert.problem_name || alert.category_name || "Unassigned").trim() : "";
  const responder = active ? String(alert.responder_name_text || "").trim() : "";
  const timer = active ? formatElapsedDuration(Math.max(0, Math.floor(alert.elapsed_seconds || 0))) : "";
  const stats = buildMachineStats(machine.id, visibleDetails);
  const statusLabelText = isOffline
    ? "Offline"
    : active
      ? (status === "OPEN" ? "Waiting for acknowledgment" : "Working on it")
      : "Machine running healthy";
  const statusIcon = isOffline
    ? "bi-x-lg"
    : active
      ? (status === "OPEN" ? "bi-x-lg" : "bi-tools")
      : "bi-check2-circle";
  const heroClass = isOffline
    ? "status-off"
    : active
      ? statusClass(status)
      : "status-healthy";
  const issueStateClass = status === "ACKNOWLEDGED"
    ? "management-machine-card__state--issue-working"
    : "management-machine-card__state--issue-open";
  const timerStateClass = status === "ACKNOWLEDGED"
    ? "management-machine-card__state--timer-working"
    : "management-machine-card__state--timer-open";
  const lastClosedAlert = !active && !isOffline ? getLatestClosedAlert(machine.id, visibleDetails) : null;
  const lastIssueDepartment = lastClosedAlert ? String(lastClosedAlert.department_name || "").trim() : "";
  const lastIssueProblem = lastClosedAlert
    ? String(lastClosedAlert.issue_problem_name || lastClosedAlert.issue_category_name || "Unassigned").trim()
    : "";
  const lastIssueName = lastClosedAlert
    ? [lastIssueDepartment, lastIssueProblem].filter(Boolean).join(" - ") || "Unassigned"
    : "No recent issue";
  const startedAt = lastClosedAlert ? formatClockTime(lastClosedAlert.created_at) : "—";
  const resolvedAt = lastClosedAlert ? formatClockTime(lastClosedAlert.closed_at) : "—";
  return `
    <article
      class="management-machine-card management-machine-card--${statusClass(status)} management-machine-card--clickable"
      data-machine-id="${machine.id}"
      data-machine-name="${escapeHtml(machine.name)}"
      data-machine-group="${escapeHtml(machine.machine_type || "Unassigned")}"
      role="button"
      tabindex="0"
      aria-label="Open summary for ${escapeHtml(machine.name)}"
    >
      <div class="management-machine-card__hero management-machine-card__hero--${heroClass}">
        <div class="management-machine-card__title">${escapeHtml(machine.name)}</div>
        <div class="management-machine-card__hero-status">
          <span class="management-machine-card__hero-icon"><i class="bi ${statusIcon}"></i></span>
          <span class="management-machine-card__hero-text">${escapeHtml(statusLabelText)}</span>
        </div>
      </div>
      <div class="management-machine-card__meta">
        <span class="management-machine-card__meta-label">Radius Machine</span>
        <span class="management-machine-card__meta-value">${escapeHtml(machine.radius?.machine_id || machine.radius_machine_id || "N/A")}</span>
      </div>
      <div class="management-machine-card__metrics">
        <div class="management-machine-card__metric">
          <div class="management-machine-card__metric-label">Today</div>
          <div class="management-machine-card__metric-value">${stats.totalAlerts}</div>
        </div>
        <div class="management-machine-card__metric">
          <div class="management-machine-card__metric-label">Avg ack</div>
          <div class="management-machine-card__metric-value">${escapeHtml(stats.averageAcknowledge)}</div>
        </div>
        <div class="management-machine-card__metric">
          <div class="management-machine-card__metric-label">Avg fix</div>
          <div class="management-machine-card__metric-value">${escapeHtml(stats.averageFix)}</div>
        </div>
      </div>
      ${renderRadiusGroup(machine)}
      <div class="management-machine-card__body">
        ${isOffline ? `<div class="management-machine-card__state management-machine-card__state--offline">Offline</div>` : active ? `
          <div class="management-machine-card__state-row management-machine-card__state-row--two">
            <div class="management-machine-card__state ${issueStateClass}">
              <div class="management-machine-card__state-label">Issue</div>
              <div class="management-machine-card__state-value">${escapeHtml(issue)}</div>
            </div>
            <div class="management-machine-card__state ${timerStateClass}">
              <div class="management-machine-card__state-label">Elapsed</div>
              <div class="management-machine-card__state-value" data-live-timer="true" data-elapsed-seconds="${Math.max(0, Math.floor(alert.elapsed_seconds || 0))}">${escapeHtml(timer)}</div>
            </div>
          </div>
          <div class="management-machine-card__state management-machine-card__state--responder">
            <div class="management-machine-card__state-label">Responder</div>
            <div class="management-machine-card__state-value">${escapeHtml(responder || "No Responder Assigned")}</div>
          </div>
        ` : `
          <div class="management-machine-card__history">
            <div class="management-machine-card__history-label">Last issue</div>
            <div class="management-machine-card__history-value">${escapeHtml(lastIssueName)}</div>
            <div class="management-machine-card__history-times">
              <div class="management-machine-card__history-time">
                <span>Started</span>
                <strong>${escapeHtml(startedAt)}</strong>
              </div>
              <div class="management-machine-card__history-time">
                <span>Resolved</span>
                <strong>${escapeHtml(resolvedAt)}</strong>
              </div>
            </div>
          </div>
        `}
      </div>
    </article>`;
}

function renderMachineGroupSection(groupName, machines) {
  const machineIds = new Set(machines.map((machine) => machine.id));
  const visibleDetails = state.shiftDetails.filter((detail) => machineIds.has(detail.machine_id));
  const headerTitle = `${groupName} Machine Group`;
  return `
    <section class="board-card board-department-panel management-department-panel">
      <div class="board-department-panel__header">
        <h3 class="board-department-panel__title">${escapeHtml(headerTitle)}</h3>
      </div>
      <div class="board-department-panel__grid management-department-panel__grid">
        ${machines.map((machine) => renderMachineCard(machine, visibleDetails)).join("")}
      </div>
    </section>`;
}

function renderEmptyBoard() {
  return `
    <div class="board-empty text-center p-4 p-md-5">
      <div class="h4 mb-2">No machines match this view.</div>
      <div class="small text-secondary">Change the machine group or department filters.</div>
    </div>`;
}

function renderStatusDock(visibleMachines) {
  if (!managementStatusDock) return;
  const total = visibleMachines.length;
  const openAlerts = visibleMachines.filter((machine) => machine.active_alert && machine.active_alert.status === "OPEN").length;
  const workingAlerts = visibleMachines.filter((machine) => {
    const status = machine.active_alert && machine.active_alert.status;
    return status === "ACKNOWLEDGED" || status === "ARRIVED";
  }).length;
  const healthy = total - openAlerts - workingAlerts - visibleMachines.filter((machine) => !machine.is_active).length;
  const offline = visibleMachines.filter((machine) => !machine.is_active).length;
  const lastRefresh = state.refreshedAt
    ? new Date(state.refreshedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "just now";
  const viewLabel = state.filters.department
    ? `Department: ${state.filters.department}`
    : state.filters.machineGroup
      ? `Machine group: ${state.filters.machineGroup}`
      : "All departments";
  const shiftLabel = state.shiftRange.label || "Current shift";

  managementStatusDock.innerHTML = `
    <div class="operator-status-dock__panel ${openAlerts === 0 && workingAlerts === 0 ? "operator-status-dock__panel--steady" : "operator-status-dock__panel--busy"}">
      <div class="operator-status-dock__headline">
        <div class="operator-status-dock__status">
          <span class="operator-status-dock__pulse"></span>
          <span class="operator-status-dock__label">${openAlerts === 0 && workingAlerts === 0 ? "Line steady" : "Active attention"}</span>
        </div>
        <div class="operator-status-dock__title">Management Station</div>
        <div class="operator-status-dock__subcopy">${escapeHtml(viewLabel)} · ${escapeHtml(shiftLabel)} · refreshed ${escapeHtml(lastRefresh)}</div>
      </div>
      <div class="operator-status-dock__stats" role="list" aria-label="Management status summary">
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
            <div class="operator-status-dock__stat-label">Waiting for Ack</div>
            <div class="operator-status-dock__stat-value">${openAlerts}</div>
          </div>
        </div>
        <div class="operator-status-dock__stat" role="listitem">
          <i class="bi bi-tools"></i>
          <div>
            <div class="operator-status-dock__stat-label">Being Worked On</div>
            <div class="operator-status-dock__stat-value">${workingAlerts}</div>
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

function getMachineById(machineId) {
  return (state.boardState.machines || []).find((machine) => String(machine.id) === String(machineId));
}

function formatTotalDuration(values) {
  const total = values
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value >= 0)
    .reduce((sum, value) => sum + value, 0);
  return total > 0 ? formatElapsedDuration(total) : "—";
}

function renderManagementDetailRows(details) {
  if (!details.length) {
    return '<tr><td colspan="8" class="text-secondary">No alerts in this range.</td></tr>';
  }
  return details.map((detail) => {
    const issueParts = [detail.issue_category_name, detail.issue_problem_name].filter(Boolean);
    const issue = issueParts.length ? issueParts.join(" - ") : "Unassigned";
    const totalTime = detail.total_seconds !== null && detail.total_seconds !== undefined
      ? formatElapsedDuration(detail.total_seconds)
      : "—";
    const ackTime = detail.acknowledged_seconds !== null && detail.acknowledged_seconds !== undefined
      ? formatElapsedDuration(detail.acknowledged_seconds)
      : "—";
    const fixTime = detail.ack_to_clear_seconds !== null && detail.ack_to_clear_seconds !== undefined
      ? formatElapsedDuration(detail.ack_to_clear_seconds)
      : "—";
    const operator = detail.responder_name_text || "—";
    const finished = detail.closed_at || "—";
    return `
      <tr>
        <td>${escapeHtml(detail.department_name || "Unassigned")}</td>
        <td>${escapeHtml(issue)}</td>
        <td>${escapeHtml(operator)}</td>
        <td>${escapeHtml(detail.created_at || "—")}</td>
        <td>${escapeHtml(finished)}</td>
        <td>${escapeHtml(ackTime)}</td>
        <td>${escapeHtml(fixTime)}</td>
        <td>${escapeHtml(totalTime)}</td>
      </tr>`;
  }).join("");
}

function setDetailModalDefaults(machine) {
  activeDetailMachine = machine || null;
  if (managementDetailModalTitle) {
    managementDetailModalTitle.textContent = machine ? `${machine.name} Summary` : "Press Summary";
  }
  if (managementDetailStart) {
    managementDetailStart.value = state.shiftRange.start || "";
  }
  if (managementDetailEnd) {
    managementDetailEnd.value = state.shiftRange.end || "";
  }
  if (managementDetailModalSubtitle) {
    const machineGroup = machine?.machine_type || "Unassigned";
    managementDetailModalSubtitle.textContent = `${machineGroup} · ${state.shiftRange.label || "Current shift"}`;
  }
  if (managementDetailSummary) {
    managementDetailSummary.innerHTML = "";
  }
}

async function loadManagementDetailSummary() {
  if (!activeDetailMachine || !managementDetailTableBody) return;
  const requestId = ++detailRequestId;
  const { start, end } = getDetailRange();
  const params = new URLSearchParams({
    start: zonedDateTimeToIso(start),
    end: zonedDateTimeToIso(end),
    machine_id: activeDetailMachine.id,
  });
  managementDetailTableBody.innerHTML = '<tr><td colspan="8" class="text-secondary">Loading shift summary...</td></tr>';
  try {
    const response = await fetch(`${reportDetailsUrl}?${params.toString()}`);
    const data = await response.json();
    if (requestId !== detailRequestId) return;
    if (!data.success) {
      throw new Error(data.error?.message || "Unable to load machine details");
    }
    const details = Array.isArray(data.data) ? data.data : [];
    const totalDuration = formatTotalDuration(details.map((detail) => detail.total_seconds));
    const averageAck = formatAverageDuration(details.map((detail) => Number(detail.acknowledged_seconds)));
    const averageFix = formatAverageDuration(details.map((detail) => Number(detail.ack_to_clear_seconds)));
    if (managementDetailSummary) {
      managementDetailSummary.innerHTML = `
        <div class="management-detail-modal__summary-chip">
          <div class="management-detail-modal__summary-label">Alerts</div>
          <div class="management-detail-modal__summary-value">${escapeHtml(details.length)}</div>
        </div>
        <div class="management-detail-modal__summary-chip">
          <div class="management-detail-modal__summary-label">Total Duration</div>
          <div class="management-detail-modal__summary-value">${escapeHtml(totalDuration)}</div>
        </div>
        <div class="management-detail-modal__summary-chip">
          <div class="management-detail-modal__summary-label">Avg Ack</div>
          <div class="management-detail-modal__summary-value">${escapeHtml(averageAck)}</div>
        </div>
        <div class="management-detail-modal__summary-chip">
          <div class="management-detail-modal__summary-label">Avg Fix</div>
          <div class="management-detail-modal__summary-value">${escapeHtml(averageFix)}</div>
        </div>`;
    }
    managementDetailTableBody.innerHTML = renderManagementDetailRows(details);
    if (managementDetailModalSubtitle) {
      managementDetailModalSubtitle.textContent = `${activeDetailMachine.machine_type || "Unassigned"} · ${state.shiftRange.label || "Current shift"} · ${details.length} alert${details.length === 1 ? "" : "s"}`;
    }
  } catch (error) {
    if (requestId !== detailRequestId) return;
    managementDetailTableBody.innerHTML = `<tr><td colspan="8" class="text-danger">${escapeHtml(error.message || "Unable to load machine details")}</td></tr>`;
  }
}

function openManagementDetailModal(machine) {
  if (!machine) return;
  setDetailModalDefaults(machine);
  if (managementDetailModal) {
    managementDetailModal.show();
  }
  loadManagementDetailSummary();
}

function renderBoard() {
  const visibleMachines = getVisibleMachines();
  const groups = groupByMachineGroup(visibleMachines);
  managementGrid.innerHTML = visibleMachines.length
    ? [...groups.entries()]
        .map(([groupName, machines]) => renderMachineGroupSection(groupName, machines.sort(sortMachinesInOrder)))
        .join("")
    : renderEmptyBoard();
  renderStatusDock(visibleMachines);
}

function updateTimers() {
  liveTimerNodes.forEach((timer) => {
    const nextSeconds = Number(timer.dataset.elapsedSeconds || "0") + 1;
    timer.dataset.elapsedSeconds = String(nextSeconds);
    timer.textContent = formatElapsedDuration(nextSeconds);
  });
}

function syncTimers() {
  liveTimerNodes = Array.from(managementGrid.querySelectorAll('[data-live-timer="true"][data-elapsed-seconds]'));
}

async function refreshBoard() {
  if (refreshInFlight) {
    refreshQueued = true;
    return;
  }
  refreshInFlight = true;
  try {
    await Promise.all([loadBoardState(), loadShiftDetails()]);
    normalizeViewState();
    renderViewControls();
    renderBoard();
    syncTimers();
  } finally {
    refreshInFlight = false;
    if (refreshQueued) {
      refreshQueued = false;
      scheduleRefresh();
    }
  }
}

function scheduleRefresh() {
  if (refreshTimeoutId) {
    clearTimeout(refreshTimeoutId);
  }
  refreshTimeoutId = setTimeout(() => {
    refreshTimeoutId = null;
    refreshBoard();
  }, 150);
}

function toggleLock() {
  state.locked = !state.locked;
  if (state.locked && !state.filters.machineGroup && !state.filters.department) {
    state.filters.department = "";
  }
  saveView();
  renderViewControls();
  renderBoard();
}

function clearView() {
  state.filters.machineGroup = "";
  state.filters.department = "";
  state.locked = false;
  saveView();
  renderViewControls();
  renderBoard();
}

function handleManagementCardActivation(event) {
  const card = event.target.closest(".management-machine-card[data-machine-id]");
  if (!card || !managementGrid.contains(card)) return;
  if (event.target.closest("button, a, input, select, textarea, label")) return;
  const machine = getMachineById(card.dataset.machineId);
  if (!machine) return;
  openManagementDetailModal(machine);
}

function handleManagementCardKeydown(event) {
  if (event.key !== "Enter" && event.key !== " ") return;
  const card = event.target.closest(".management-machine-card[data-machine-id]");
  if (!card || !managementGrid.contains(card)) return;
  event.preventDefault();
  const machine = getMachineById(card.dataset.machineId);
  if (!machine) return;
  openManagementDetailModal(machine);
}

function onMachineGroupChange() {
  if (state.locked) return;
  state.filters.machineGroup = managementMachineGroupSelect.value || "";
  saveView();
  renderViewControls();
  renderBoard();
}

function onDepartmentChange() {
  if (state.locked) return;
  state.filters.department = managementDepartmentSelect.value || "";
  saveView();
  renderViewControls();
  renderBoard();
}

function handleVisibilityChange() {
  if (document.hidden) {
    if (liveTimerIntervalId) {
      clearInterval(liveTimerIntervalId);
      liveTimerIntervalId = null;
    }
    return;
  }
  if (!liveTimerIntervalId) {
    liveTimerIntervalId = setInterval(updateTimers, 1000);
  }
}

function setFallbackPolling(enabled) {
  if (!enabled && fallbackPollIntervalId) {
    clearInterval(fallbackPollIntervalId);
    fallbackPollIntervalId = null;
    return;
  }
  if (enabled && !fallbackPollIntervalId) {
    fallbackPollIntervalId = setInterval(scheduleRefresh, 15000);
  }
}

async function boot() {
  if (!state.shiftRange.start || !state.shiftRange.end) {
    const defaults = getDefaultShiftRange();
    state.shiftRange.start = defaults.start;
    state.shiftRange.end = defaults.end;
    state.shiftRange.label = defaults.label;
  }
  loadSavedView();
  await refreshBoard();
  renderViewControls();
  syncTimers();
  liveTimerIntervalId = setInterval(updateTimers, 1000);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  managementGrid.addEventListener("click", handleManagementCardActivation);
  managementGrid.addEventListener("keydown", handleManagementCardKeydown);
  managementMachineGroupSelect.addEventListener("change", onMachineGroupChange);
  managementDepartmentSelect.addEventListener("change", onDepartmentChange);
  managementLockView.addEventListener("click", toggleLock);
  managementClearView.addEventListener("click", clearView);
  managementDetailRefresh?.addEventListener("click", loadManagementDetailSummary);
  managementDetailStart?.addEventListener("change", () => {
    if (activeDetailMachine) {
      loadManagementDetailSummary();
    }
  });
  managementDetailEnd?.addEventListener("change", () => {
    if (activeDetailMachine) {
      loadManagementDetailSummary();
    }
  });
  managementDetailModalEl?.addEventListener("hidden.bs.modal", () => {
    activeDetailMachine = null;
    detailRequestId += 1;
  });
  window.AndonRefreshBus?.onRefresh(scheduleRefresh);
  window.AndonRealtime?.onEvent((event) => {
    if (["board_refresh", "alert_created", "alert_updated", "alert_resolved", "alert_cancelled", "machine_updated", "admin_metadata_updated"].includes(event.type)) {
      scheduleRefresh();
    }
  });
  window.AndonRealtime?.onStatus((status) => setFallbackPolling(!status.connected));
  setFallbackPolling(!window.AndonRealtime?.connected);
}

boot();
