from __future__ import annotations

from flask import current_app
from flask_socketio import disconnect, emit, join_room, leave_room

from .company_context import get_current_company_id
from .services.realtime_service import BOARD_ROOM, OPERATOR_ROOM, REPORTS_ROOM, room_name

VALID_ROOMS = {BOARD_ROOM, OPERATOR_ROOM, REPORTS_ROOM}


def register_socket_events(socketio):
    if getattr(socketio, "_andon_events_registered", False):
        return
    socketio._andon_events_registered = True

    @socketio.on("connect")
    def on_connect():
        current_app.logger.debug("Socket.IO client connected")
        emit("connected", {"success": True})

    @socketio.on("disconnect")
    def on_disconnect():
        current_app.logger.debug("Socket.IO client disconnected")

    @socketio.on("join_company_room")
    def on_join_company_room(payload=None):
        data = payload or {}
        room_type = data.get("room") or BOARD_ROOM
        if room_type not in VALID_ROOMS:
            disconnect()
            return
        company_id = get_current_company_id() or data.get("company_id")
        if not company_id:
            emit("room_error", {"message": "No active company"})
            return
        room = room_name(company_id, room_type)
        join_room(room)
        emit("joined_company_room", {"room": room, "company_id": company_id})

    @socketio.on("leave_company_room")
    def on_leave_company_room(payload=None):
        data = payload or {}
        room_type = data.get("room") or BOARD_ROOM
        if room_type not in VALID_ROOMS:
            return
        company_id = get_current_company_id() or data.get("company_id")
        if not company_id:
            return
        leave_room(room_name(company_id, room_type))
