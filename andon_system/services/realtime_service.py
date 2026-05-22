from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app, has_app_context

from ..extensions import socketio

BOARD_ROOM = "board"
OPERATOR_ROOM = "operator"
REPORTS_ROOM = "reports"

BOARD_ROOMS = (BOARD_ROOM, OPERATOR_ROOM)
REPORT_ROOMS = (REPORTS_ROOM,)


def room_name(company_id, room_type: str) -> str:
    return f"company:{company_id}:{room_type}"


def emit_alert_created(company_id, alert_id, machine_id=None, status=None):
    payload = _payload(company_id, alert_id=alert_id, machine_id=machine_id, action="created", status=status)
    _emit_to_rooms("alert_created", payload, BOARD_ROOMS)
    emit_reports_invalidated(company_id, source="alert_created")


def emit_alert_updated(company_id, alert_id, machine_id=None, status=None, action="updated"):
    event_name = {
        "resolved": "alert_resolved",
        "cancelled": "alert_cancelled",
    }.get(action, "alert_updated")
    payload = _payload(company_id, alert_id=alert_id, machine_id=machine_id, action=action, status=status)
    _emit_to_rooms(event_name, payload, BOARD_ROOMS)
    emit_reports_invalidated(company_id, source=event_name)


def emit_machine_updated(company_id, machine_id=None, action="updated"):
    payload = _payload(company_id, machine_id=machine_id, action=action)
    _emit_to_rooms("machine_updated", payload, BOARD_ROOMS)
    emit_reports_invalidated(company_id, source="machine_updated")


def emit_admin_metadata_updated(company_id, action="metadata_updated"):
    payload = _payload(company_id, action=action)
    _emit_to_rooms("admin_metadata_updated", payload, (BOARD_ROOM, OPERATOR_ROOM, REPORTS_ROOM))
    emit_reports_invalidated(company_id, source="admin_metadata_updated")


def emit_reports_invalidated(company_id, source="data_changed"):
    payload = _payload(company_id, action=source)
    _emit_to_rooms("reports_invalidated", payload, REPORT_ROOMS)


def _payload(company_id, **values):
    data = {
        "event_type": values.pop("event_type", None),
        "company_id": company_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data.update({key: value for key, value in values.items() if value is not None})
    data["event_type"] = data.get("event_type") or data.get("action") or "updated"
    return data


def _emit_to_rooms(event_name: str, payload: dict, rooms):
    if not _enabled() or not payload.get("company_id"):
        return
    for room_type in rooms:
        room = room_name(payload["company_id"], room_type)
        try:
            if _emit_async_enabled():
                socketio.start_background_task(_safe_emit, event_name, payload, room)
            else:
                _safe_emit(event_name, payload, room)
        except Exception:
            if has_app_context():
                current_app.logger.exception("Unable to emit realtime event %s", event_name)


def _enabled() -> bool:
    if socketio is None or not has_app_context():
        return False
    return bool(current_app.config.get("SOCKETIO_ENABLED", True))


def _emit_async_enabled() -> bool:
    if not has_app_context():
        return False
    async_mode = str(current_app.config.get("SOCKETIO_ASYNC_MODE") or "").strip().lower()
    configured = current_app.config.get("SOCKETIO_EMIT_ASYNC")
    if configured is not None:
        return bool(configured)
    return async_mode in {"threading", "gevent", "eventlet"}


def _safe_emit(event_name: str, payload: dict, room: str):
    try:
        socketio.emit(event_name, payload, to=room)
    except Exception:
        if has_app_context():
            current_app.logger.exception("Unable to emit realtime event %s", event_name)
