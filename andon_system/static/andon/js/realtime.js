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
    console.info("Andon realtime disabled or unavailable; using HTTP refresh fallback.");
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
    console.info("Andon realtime connected.");
    dispatchStatus(true, "connected");
    socket.emit("join_company_room", {
      company_id: config.companyId,
      room: config.room,
    });
  });

  socket.on("disconnect", () => {
    window.AndonRealtime.connected = false;
    console.info("Andon realtime disconnected; HTTP refresh fallback remains available.");
    dispatchStatus(false, "disconnected");
  });

  socket.on("connect_error", (error) => {
    window.AndonRealtime.connected = false;
    console.info("Andon realtime connection failed; HTTP refresh fallback remains available.", error?.message || error);
    dispatchStatus(false, "connect_error");
  });

  events.forEach((type) => {
    socket.on(type, (payload) => dispatch(type, payload));
  });
})();
