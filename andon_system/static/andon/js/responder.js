const responderQueue = document.getElementById("responderQueue");
const responderAlertCount = document.getElementById("responderAlertCount");
const responderUser = document.getElementById("responderUser");
const responderNameText = document.getElementById("responderNameText");

function actionButtons(alert) {
  const buttons = [];
  if (alert.status === "OPEN") {
    buttons.push(`<button class="btn btn-sm btn-warning" data-action="ack" data-id="${alert.id}">Acknowledge</button>`);
    buttons.push(`<button class="btn btn-sm btn-outline-light" data-action="cancel" data-id="${alert.id}">Cancel</button>`);
  }
  if (alert.status === "ACKNOWLEDGED") {
    buttons.push(`<button class="btn btn-sm btn-info" data-action="arrive" data-id="${alert.id}">Arrive</button>`);
    buttons.push(`<button class="btn btn-sm btn-outline-light" data-action="cancel" data-id="${alert.id}">Cancel</button>`);
  }
  if (alert.status === "ARRIVED") {
    buttons.push(`<button class="btn btn-sm btn-success" data-action="resolve" data-id="${alert.id}">Resolve</button>`);
  }
  return `
    <div class="d-flex flex-wrap gap-2 mt-3">
      ${buttons.join("")}
    </div>`;
}

function renderQueueCard(alert) {
  return `
    <div class="board-card">
      <div class="d-flex justify-content-between gap-3">
        <div>
          <div class="fw-semibold">${alert.alert_number}</div>
          <div class="small text-secondary">${alert.machine?.name || ""} · ${alert.department?.name || ""}</div>
          <div class="mt-2">${alert.issue_problem?.name || ""}</div>
        </div>
        <span class="status-pill status-${alert.status.toLowerCase()}">${alert.status}</span>
      </div>
      <div class="small text-secondary mt-2">Created ${alert.created_at}</div>
      ${actionButtons(alert)}
    </div>`;
}

async function loadQueue() {
  const response = await fetch("/api/andon/alerts?status=active");
  const data = await response.json();
  const alerts = data.data;
  responderAlertCount.textContent = String(alerts.length);
  responderQueue.innerHTML = alerts.length ? alerts.map(renderQueueCard).join("") : '<div class="text-secondary">No active alerts in the queue.</div>';
}

responderQueue.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  const payload = new FormData();
  payload.append("responder_user_id", responderUser.value || "");
  payload.append("responder_name_text", responderNameText.value || "");
  let endpoint = `/api/andon/alerts/${id}/acknowledge`;
  if (action === "arrive") endpoint = `/api/andon/alerts/${id}/arrive`;
  if (action === "resolve") {
    endpoint = `/api/andon/alerts/${id}/resolve`;
    const resolutionNote = prompt("Resolution note");
    const rootCause = prompt("Root cause");
    const correctiveAction = prompt("Corrective action");
    if (resolutionNote) payload.append("resolution_note", resolutionNote);
    if (rootCause) payload.append("root_cause", rootCause);
    if (correctiveAction) payload.append("corrective_action", correctiveAction);
  }
  if (action === "cancel") endpoint = `/api/andon/alerts/${id}/cancel`;
  const response = await fetch(endpoint, { method: "POST", body: payload });
  const data = await response.json();
  if (!data.success) {
    alert(data.error.message);
    return;
  }
  loadQueue();
  window.AndonRefreshBus?.notify();
});

loadQueue();
window.AndonRefreshBus?.onRefresh(loadQueue);
