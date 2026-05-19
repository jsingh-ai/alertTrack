const boardStateUrl = "/api/andon/board-state?compact=1";

const managementFiltersBtn = document.getElementById("managementFiltersBtn");
const managementFiltersPanel = document.getElementById("managementFiltersPanel");
const managementGroupFilter = document.getElementById("managementGroupFilter");
const managementSearchFilter = document.getElementById("managementSearchFilter");
const managementClearFiltersBtn = document.getElementById("managementClearFiltersBtn");
const managementStatusDock = document.getElementById("managementStatusDock");
const managementOverviewTitle = document.getElementById("managementOverviewTitle");
const managementOverviewGrid = document.getElementById("managementOverviewGrid");

const state = {
  machines: [],
  selectedGroup: "all",
  search: "",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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
  return { label: "Healthy", className: "status-healthy" };
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
  managementOverviewGrid.innerHTML = machines.map((machine) => {
    const health = getHealth(machine);
    return `
      <article class="management-machine-card board-live-tile">
        <div class="management-machine-card__hero management-machine-card__hero--${health.className}">
          <div class="management-machine-card__title-row">
            <div class="management-machine-card__title">${escapeHtml(machine.name || "Machine")}</div>
            <span class="board-builder-tile__meta">${escapeHtml(machine.machine_type || "Unassigned")}</span>
          </div>
          <div class="management-machine-card__hero-status">
            <span class="management-machine-card__hero-text">${escapeHtml(health.label)}</span>
          </div>
        </div>
      </article>`;
  }).join("");
}

function render() {
  const filtered = getFilteredMachines();
  const title = state.selectedGroup === "all" ? "All Machine Groups" : state.selectedGroup;
  managementOverviewTitle.textContent = title;
  renderStatusDock(filtered);
  renderMachines(filtered);
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
  const groups = getMachineGroups(state.machines);
  state.selectedGroup = pickDefaultGroup(groups);
  renderGroupFilter();
  if (managementGroupFilter.value !== state.selectedGroup) {
    managementGroupFilter.value = state.selectedGroup;
  }
  render();
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

boot().catch((error) => {
  managementOverviewTitle.textContent = "Management Unavailable";
  managementOverviewGrid.innerHTML = `<div class="board-builder-empty text-danger">${escapeHtml(error.message || "Unable to load management overview")}</div>`;
});
