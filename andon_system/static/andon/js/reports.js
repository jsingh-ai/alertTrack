const reportStart = document.getElementById("reportStart");
const reportEnd = document.getElementById("reportEnd");
const refreshReports = document.getElementById("refreshReports");
const reportMachineGroupButtons = document.getElementById("reportMachineGroupButtons");
const reportInsights = document.getElementById("reportInsights");
const reportKpis = document.getElementById("reportKpis");
const machineTable = document.getElementById("machineTable");
const problemTable = document.getElementById("problemTable");
const reportDetailModalEl = document.getElementById("reportDetailModal");
const reportDetailModalTitle = document.getElementById("reportDetailModalTitle");
const reportDetailModalBody = document.getElementById("reportDetailModalBody");
const reportDetailModal = reportDetailModalEl ? new bootstrap.Modal(reportDetailModalEl) : null;

let machineChart;
let departmentChart;
let hourChart;
let currentSummary = null;

function toQuery() {
  const params = new URLSearchParams();
  if (reportStart.value) params.set("start", datetimeLocalToIso(reportStart.value));
  if (reportEnd.value) params.set("end", datetimeLocalToIso(reportEnd.value));
  const selectedGroup = getSelectedMachineGroup();
  if (selectedGroup) params.set("machine_group", selectedGroup);
  return params.toString();
}

function todayLocalDate() {
  const today = new Date();
  const year = today.getFullYear();
  const month = String(today.getMonth() + 1).padStart(2, "0");
  const day = String(today.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function kpiCard(title, value) {
  return `<div class="report-kpi-item"><div class="metric-card"><div class="text-secondary small">${title}</div><div class="metric-value">${value ?? "N/A"}</div></div></div>`;
}

function reportInsightCard(label, value, meta, tone = "blue") {
  return `
    <div>
      <div class="report-insight-card report-insight-card--${tone}">
        <div class="report-insight-card__eyebrow">${escapeHtml(label)}</div>
        <div class="report-insight-card__value">${escapeHtml(value ?? "N/A")}</div>
        <div class="report-insight-card__meta">${escapeHtml(meta ?? "")}</div>
      </div>
    </div>
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || seconds === "") {
    return "N/A";
  }
  const totalSeconds = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  return [
    String(hours).padStart(2, "0"),
    String(minutes).padStart(2, "0"),
    String(remainingSeconds).padStart(2, "0"),
  ].join(":");
}

function normalizeDetailStatus(status) {
  const normalized = String(status ?? "").trim().toUpperCase();
  if (normalized === "CANCELLED" || normalized === "RESOLVED") {
    return { label: "Cleared", className: "resolved" };
  }
  if (normalized === "ACKNOWLEDGED") {
    return { label: "Acknowledged", className: "acknowledged" };
  }
  if (normalized === "OPEN") {
    return { label: "Open", className: "open" };
  }
  return {
    label: normalized ? normalized[0] + normalized.slice(1).toLowerCase() : "Unknown",
    className: normalized ? normalized.toLowerCase() : "open",
  };
}

function buildDetailTitle(detail) {
  const parts = [detail.issue_category_name, detail.issue_problem_name].filter(Boolean);
  if (parts.length) {
    return parts.join(" - ");
  }
  return detail.department_name || detail.machine_name || "Alert Detail";
}

function formatDateTimeLocalInput(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${year}-${month}-${day}T${hours}:${minutes}:${seconds}`;
}

function defaultReportStartValue() {
  const now = new Date();
  const date = new Date(now);
  if (now.getHours() < 6) {
    date.setDate(date.getDate() - 1);
  }
  date.setHours(0, 0, 0, 0);
  return formatDateTimeLocalInput(date);
}

function defaultReportEndValue() {
  return formatDateTimeLocalInput(new Date());
}

function datetimeLocalToIso(value) {
  const [datePart, timePart = "00:00:00"] = String(value).split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour = 0, minute = 0, second = 0] = timePart.split(":").map(Number);
  const local = new Date(year, month - 1, day, hour, minute, second, 0);
  return local.toISOString();
}

function getSelectedMachineGroup() {
  return reportMachineGroupButtons?.querySelector(".report-group-btn.is-active")?.dataset.machineGroup || "";
}

function setSelectedMachineGroup(machineGroup) {
  if (!reportMachineGroupButtons) return;
  reportMachineGroupButtons.querySelectorAll(".report-group-btn").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.machineGroup === machineGroup);
  });
}

async function loadReports() {
  const query = toQuery();
  const response = await fetch(`/api/andon/reports/summary?${query}`);
  const data = await response.json();
  const summary = data.data;
  currentSummary = summary;
  const kpis = summary.kpis;
  renderInsights(summary);
  reportKpis.innerHTML = [
    kpiCard("Total Alerts", kpis.total_alerts),
    kpiCard("Open Alerts", kpis.open_alerts),
    kpiCard("Closed Alerts", kpis.closed_alerts ?? kpis.resolved_alerts),
    kpiCard("Avg Ack Time (hh:mm:ss)", formatDuration(kpis.average_acknowledge_time)),
    kpiCard("Avg Clear Time (hh:mm:ss)", formatDuration(kpis.average_ack_to_clear_time)),
  ].join("");

  renderMachineChart(summary.by_machine);
  renderDepartmentChart(summary.by_department);
  renderHourChart(summary.calls_per_hour);
  renderTables(summary);
}

function renderMachineChart(rows) {
  const ctx = document.getElementById("machineChart");
  if (machineChart) machineChart.destroy();
  machineChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: rows.map((row) => row.name),
      datasets: [{ label: "Alerts", data: rows.map((row) => row.count), backgroundColor: "#2ec4b6" }],
    },
    options: { responsive: true, plugins: { legend: { display: false } } },
  });
}

function renderDepartmentChart(rows) {
  const ctx = document.getElementById("departmentChart");
  if (departmentChart) departmentChart.destroy();
  departmentChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: rows.map((row) => row.name),
      datasets: [{ label: "Alerts", data: rows.map((row) => row.count), backgroundColor: "#ffb703" }],
    },
    options: { responsive: true, plugins: { legend: { display: false } } },
  });
}

function renderInsights(summary) {
  if (!reportInsights) return;
  const kpis = summary?.kpis || {};
  const selectedGroup = getSelectedMachineGroup() || "All machine groups";
  const topMachine = (summary?.top_machines || [])[0];
  const topProblem = (summary?.top_problems || [])[0];
  const topGroup = (summary?.by_machine_group || [])[0];
  const alertCount = Number(kpis.total_alerts || 0);
  const openCount = Number(kpis.open_alerts || 0);
  const closedCount = Number(kpis.closed_alerts ?? kpis.resolved_alerts ?? 0);
  const machineMeta = topMachine
    ? `${topMachine.count ?? 0} alerts · ${topMachine.department_name ?? "Unassigned"}`
    : "No machine data in this range";
  const problemMeta = topProblem
    ? `${topProblem.count ?? 0} alerts · ${topProblem.category_name ?? "Unassigned"}`
    : "No problem data in this range";
  const groupMeta = topGroup
    ? `${topGroup.count ?? 0} alerts · ${topGroup.name ?? "Unassigned"}`
    : "No group data in this range";

  reportInsights.innerHTML = [
    reportInsightCard("Selected Scope", selectedGroup, `${alertCount} alerts in range`, "blue"),
    reportInsightCard("Top Machine Group", topGroup?.name ?? "N/A", groupMeta, "teal"),
    reportInsightCard("Most Active Machine", topMachine?.name ?? "N/A", machineMeta, "amber"),
    reportInsightCard("Top Problem", topProblem?.name ?? "N/A", problemMeta, "red"),
  ].join("");
}

function renderTables(summary) {
  if (machineTable) {
    const rows = summary.top_machines || [];
    machineTable.innerHTML = rows.length
      ? rows
          .map(
            (row, index) => {
              const machineId = row.id ?? "";
              return `
              <tr class="report-machine-row" data-report-machine-index="${index}">
                <td class="report-machine-row__toggle-cell">
                  <button
                    class="btn btn-sm report-machine-toggle"
                    type="button"
                    data-report-machine-id="${machineId}"
                    data-report-machine-index="${index}"
                    aria-label="Open machine detail"
                  >+</button>
                </td>
                <td>${escapeHtml(row.name ?? "")}</td>
                <td>${escapeHtml(row.machine_group ?? "")}</td>
                <td>${escapeHtml(row.department_name ?? "")}</td>
                <td>${row.count ?? ""}</td>
                <td>${escapeHtml(row.top_problem ?? "")}</td>
              </tr>
              `;
            },
          )
          .join("")
      : '<tr><td colspan="5" class="text-secondary">No machine data for this selection.</td></tr>';
  }
  if (problemTable) {
    const rows = summary.top_problems || [];
    problemTable.innerHTML = rows.length
      ? rows
          .map(
            (row, index) => `
              <tr class="report-machine-row" data-report-problem-index="${index}">
                <td class="report-machine-row__toggle-cell">
                  <button
                    class="btn btn-sm report-machine-toggle"
                    type="button"
                    data-report-problem-id="${row.id ?? ""}"
                    data-report-problem-index="${index}"
                    aria-label="Open problem detail"
                  >+</button>
                </td>
                <td>${escapeHtml(row.name ?? "")}</td>
                <td>${escapeHtml(row.category_name ?? "")}</td>
                <td>${row.count ?? ""}</td>
                <td>${escapeHtml(row.top_machine ?? "")}</td>
              </tr>`,
          )
          .join("")
      : '<tr><td colspan="5" class="text-secondary">No problem data for this selection.</td></tr>';
  }
}

function renderHourChart(rows) {
  const ctx = document.getElementById("hourChart");
  if (hourChart) hourChart.destroy();
  hourChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: rows.map((row) => `${row.hour}:00`),
      datasets: [{ label: "Calls", data: rows.map((row) => row.count), borderColor: "#2ec4b6", tension: 0.28 }],
    },
    options: { responsive: true, plugins: { legend: { display: false } } },
  });
}

refreshReports.addEventListener("click", loadReports);
reportMachineGroupButtons?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-machine-group]");
  if (!button) return;
  const machineGroup = button.dataset.machineGroup || "";
  setSelectedMachineGroup(machineGroup);
  loadReports();
});
machineTable?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-report-machine-id]");
  if (!button || !currentSummary) return;
  const machineId = button.dataset.reportMachineId;
  const index = Number(button.dataset.reportMachineIndex);
  const rows = currentSummary.top_machines || [];
  const row = machineId ? rows.find((item) => String(item.id) === String(machineId)) : rows[index];
  if (!row) return;
  openDetailModal("Machine Detail", row, "machine");
});
problemTable?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-report-problem-id]");
  if (!button || !currentSummary) return;
  const problemId = button.dataset.reportProblemId;
  const index = Number(button.dataset.reportProblemIndex);
  const rows = currentSummary.top_problems || [];
  const row = problemId ? rows.find((item) => String(item.id) === String(problemId)) : rows[index];
  if (!row) return;
  openDetailModal("Problem Detail", row, "problem");
});
const defaultEndDate = defaultReportEndValue();
if (reportStart && !reportStart.value) reportStart.value = defaultReportStartValue();
if (reportEnd && !reportEnd.value) reportEnd.value = defaultEndDate;
if (reportMachineGroupButtons) {
  setSelectedMachineGroup("");
}
loadReports();

function openDetailModal(title, row, kind) {
  if (!reportDetailModal || !reportDetailModalTitle || !reportDetailModalBody) return;
  reportDetailModalTitle.textContent = title;
  const details = Array.isArray(row.details) ? row.details : [];
  const detailCount = details.length;
  const summaryChips = kind === "machine"
    ? [
        ["Machine", row.name],
        ["Avg Ack", formatDuration(row.average_acknowledge_seconds)],
        ["Avg Clear", formatDuration(row.average_ack_to_clear_seconds)],
        ["Avg Total", formatDuration(row.average_total_seconds)],
      ]
    : [
        ["Problem", row.name],
        ["Category", row.category_name],
        ["Avg Ack", formatDuration(row.average_acknowledge_seconds)],
        ["Avg Clear", formatDuration(row.average_ack_to_clear_seconds)],
        ["Avg Total", formatDuration(row.average_total_seconds)],
      ];

  const summaryHtml = summaryChips
    .map(([label, value]) => `<div class="report-detail-chip"><div class="report-detail-chip__label">${escapeHtml(label)}</div><div class="report-detail-chip__value">${escapeHtml(value ?? "N/A")}</div></div>`)
    .join("");

  const detailsHtml = details.length
    ? details
        .map(
          (detail) => {
            const status = normalizeDetailStatus(detail.status);
            const issueTitle = buildDetailTitle(detail);
            return `
            <div class="report-detail-item report-detail-item--${escapeHtml(status.className)}">
              <div class="report-detail-item__top">
                <div class="report-detail-item__title">${escapeHtml(issueTitle)}</div>
                <div class="report-detail-item__status">${escapeHtml(status.label)}</div>
              </div>
              <div class="report-detail-item__subtitle">
                ${detail.responder_name_text ? `<span>Responder ${escapeHtml(detail.responder_name_text)}</span>` : `<span>&nbsp;</span>`}
              </div>
              <div class="report-detail-item__meta">
                <span>${escapeHtml([detail.department_name, issueTitle].filter(Boolean).join(" - "))}</span>
              </div>
              <div class="report-detail-item__times report-detail-item__times--lifecycle">
                <span><strong>Created</strong> ${escapeHtml(detail.created_at ?? "")}</span>
                <span><strong>Ack</strong> ${escapeHtml(detail.acknowledged_at ?? "")}</span>
                <span><strong>Cleared</strong> ${escapeHtml(detail.closed_at ?? "")}</span>
              </div>
              <div class="report-detail-item__times report-detail-item__times--metrics">
                <span>Ack Time ${formatDuration(detail.acknowledged_seconds)}</span>
                <span>Clear Time ${formatDuration(detail.ack_to_clear_seconds)}</span>
                <span>Total Time ${formatDuration(detail.total_seconds)}</span>
              </div>
              ${detail.responder_name_text ? `<div class="report-detail-item__note"><strong>Responder:</strong> ${escapeHtml(detail.responder_name_text)}</div>` : ""}
              ${detail.note ? `<div class="report-detail-item__note"><strong>Note:</strong> ${escapeHtml(detail.note)}</div>` : ""}
              ${detail.resolution_note ? `<div class="report-detail-item__note"><strong>Resolution:</strong> ${escapeHtml(detail.resolution_note)}</div>` : ""}
              ${detail.root_cause ? `<div class="report-detail-item__note"><strong>Root Cause:</strong> ${escapeHtml(detail.root_cause)}</div>` : ""}
              ${detail.corrective_action ? `<div class="report-detail-item__note"><strong>Corrective Action:</strong> ${escapeHtml(detail.corrective_action)}</div>` : ""}
            </div>`;
          },
        )
        .join("")
    : '<div class="text-secondary small">No alert details available.</div>';

  reportDetailModalBody.innerHTML = `
    <div class="report-detail-intro">
      <div class="report-detail-intro__label">${escapeHtml(kind === "machine" ? "Machine drilldown" : "Problem drilldown")}</div>
      <div class="report-detail-intro__value">${escapeHtml(detailCount)} alert${detailCount === 1 ? "" : "s"} in this detail set</div>
    </div>
    <div class="report-detail-summary">${summaryHtml}</div>
    <div class="report-detail-list">${detailsHtml}</div>
  `;
  reportDetailModal.show();
}
