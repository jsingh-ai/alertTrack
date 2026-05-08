const reportStart = document.getElementById("reportStart");
const reportEnd = document.getElementById("reportEnd");
const reportDepartment = document.getElementById("reportDepartment");
const reportMachine = document.getElementById("reportMachine");
const refreshReports = document.getElementById("refreshReports");
const reportKpis = document.getElementById("reportKpis");
const departmentTable = document.getElementById("departmentTable");
const machineTable = document.getElementById("machineTable");
const problemTable = document.getElementById("problemTable");
const responderTable = document.getElementById("responderTable");
const slowMachineTable = document.getElementById("slowMachineTable");
const paretoMachineTable = document.getElementById("paretoMachineTable");
const paretoProblemTable = document.getElementById("paretoProblemTable");

let departmentChart;
let hourChart;

function toQuery() {
  const params = new URLSearchParams();
  if (reportStart.value) params.set("start", new Date(reportStart.value).toISOString());
  if (reportEnd.value) params.set("end", new Date(reportEnd.value).toISOString());
  if (reportDepartment.value) params.set("department_id", reportDepartment.value);
  if (reportMachine.value) params.set("machine_id", reportMachine.value);
  return params.toString();
}

function kpiCard(title, value) {
  return `<div class="col-md-3"><div class="metric-card"><div class="text-secondary small">${title}</div><div class="metric-value">${value ?? "N/A"}</div></div></div>`;
}

function tableRows(rows, columns) {
  return rows.map((row) => `<tr>${columns.map((col) => `<td>${row[col] ?? ""}</td>`).join("")}</tr>`).join("");
}

async function loadReports() {
  const query = toQuery();
  const response = await fetch(`/api/andon/reports/summary?${query}`);
  const data = await response.json();
  const summary = data.data;
  const kpis = summary.kpis;
  reportKpis.innerHTML = [
    kpiCard("Total Alerts", kpis.total_alerts),
    kpiCard("Open Alerts", kpis.open_alerts),
    kpiCard("Resolved Alerts", kpis.resolved_alerts),
    kpiCard("Avg Ack (s)", kpis.average_acknowledge_time),
    kpiCard("Avg Arrive (s)", kpis.average_arrival_time),
    kpiCard("Avg Resolve (s)", kpis.average_resolution_time),
    kpiCard("Escalated", kpis.escalated_alerts),
  ].join("");

  renderDepartmentChart(summary.by_department);
  renderHourChart(summary.calls_per_hour);
  renderTables(summary);
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

function renderTables(summary) {
  departmentTable.innerHTML = tableRows(summary.by_department, ["name", "count"]);
  machineTable.innerHTML = tableRows(summary.by_machine, ["name", "count"]);
  problemTable.innerHTML = tableRows(summary.by_problem, ["name", "count"]);
  responderTable.innerHTML = tableRows(summary.fastest_responders, ["name", "average_acknowledge_seconds", "average_resolution_seconds", "count"]);
  slowMachineTable.innerHTML = tableRows(summary.slowest_machines, ["name", "average_acknowledge_seconds", "count"]);
  paretoMachineTable.innerHTML = tableRows(summary.pareto_machines, ["name", "count", "share", "cumulative_share"]);
  paretoProblemTable.innerHTML = tableRows(summary.pareto_problems, ["name", "count", "share", "cumulative_share"]);
}

refreshReports.addEventListener("click", loadReports);
loadReports();
