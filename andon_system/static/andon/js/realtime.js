(function () {
  const config = window.AndonRealtimeConfig || {};
  const eventName = "andon-realtime-event";
  const statusEventName = "andon-realtime-status";
  const events = [
    "board_refresh",
    "alert_created",
    "alert_updated",
    "alert_resolved",
    "alert_cancelled",
    "machine_updated",
    "admin_metadata_updated",
    "reports_invalidated",
  ];

  function dispatch(type, payload) {
    window.dispatchEvent(new CustomEvent(eventName, { detail: { type, payload: payload || {} } }));
  }

  function onEvent(handler) {
    window.addEventListener(eventName, (event) => handler(event.detail || {}));
  }

  function dispatchStatus(connected, reason) {
    window.dispatchEvent(new CustomEvent(statusEventName, { detail: { connected, reason } }));
  }

  function onStatus(handler) {
    window.addEventListener(statusEventName, (event) => handler(event.detail || {}));
  }

  window.AndonRealtime = {
    socket: null,
    connected: false,
    onEvent,
    onStatus,
  };

  if (!config.enabled || !config.companyId || !config.room || typeof window.io !== "function") {
    window.setTimeout(() => dispatchStatus(false, "unavailable"), 0);
    return;
  }

  const socket = window.io({
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 500,
    reconnectionDelayMax: 5000,
  });
  window.AndonRealtime.socket = socket;

  socket.on("connect", () => {
    window.AndonRealtime.connected = true;
    dispatchStatus(true, "connected");
    socket.emit("join_company_room", { room: config.room });
  });

  socket.on("disconnect", () => {
    window.AndonRealtime.connected = false;
    dispatchStatus(false, "disconnected");
  });

  socket.on("connect_error", () => {
    window.AndonRealtime.connected = false;
    dispatchStatus(false, "connect_error");
  });

  events.forEach((type) => {
    socket.on(type, (payload) => dispatch(type, payload));
  });
})();
