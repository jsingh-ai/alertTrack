from __future__ import annotations

import time

from flask import current_app, request
from flask_socketio import disconnect, emit, join_room, leave_room

from .security import get_authorized_company_id, is_authenticated
from .services.realtime_service import BOARD_ROOM, OPERATOR_ROOM, REPORTS_ROOM, room_name

VALID_ROOMS = {BOARD_ROOM, OPERATOR_ROOM, REPORTS_ROOM}


def register_socket_events(socketio):
    if getattr(socketio, "_andon_events_registered", False):
        return
    socketio._andon_events_registered = True

    @socketio.on("connect")
    def on_connect():
        if not is_authenticated():
            disconnect()
            return
        current_app.logger.debug(
            "Socket.IO client connected sid=%s ip=%s namespace=%s",
            getattr(request, "sid", None),
            request.remote_addr,
            getattr(request, "namespace", "/"),
        )
        emit("connected", {"success": True})

    @socketio.on("disconnect")
    def on_disconnect():
        current_app.logger.debug(
            "Socket.IO client disconnected sid=%s ip=%s namespace=%s",
            getattr(request, "sid", None),
            request.remote_addr,
            getattr(request, "namespace", "/"),
        )

    @socketio.on("join_company_room")
    def on_join_company_room(payload=None):
        started_at = time.perf_counter()
        if not is_authenticated():
            disconnect()
            return
        data = payload or {}
        room_type = data.get("room") or BOARD_ROOM
        if room_type not in VALID_ROOMS:
            disconnect()
            return
        company_id = get_authorized_company_id()
        if not company_id:
            emit("room_error", {"message": "No active company"})
            disconnect()
            return
        room = room_name(company_id, room_type)
        try:
            join_room(room)
        except (ValueError, KeyError):
            current_app.logger.warning(
                "Socket.IO join_room skipped for disconnected sid=%s namespace=%s room=%s",
                getattr(request, "sid", None),
                getattr(request, "namespace", "/"),
                room,
            )
            return
        duration_ms = (time.perf_counter() - started_at) * 1000
        current_app.logger.debug(
            "PERF socket_join sid=%s room=%s company_id=%s duration_ms=%.1f",
            getattr(request, "sid", None),
            room_type,
            company_id,
            duration_ms,
        )
        emit("joined_company_room", {"room": room, "company_id": company_id})

    @socketio.on("leave_company_room")
    def on_leave_company_room(payload=None):
        data = payload or {}
        room_type = data.get("room") or BOARD_ROOM
        if room_type not in VALID_ROOMS:
            return
        company_id = get_authorized_company_id()
        if not company_id:
            return
        room = room_name(company_id, room_type)
        try:
            leave_room(room)
        except (ValueError, KeyError):
            current_app.logger.debug(
                "Socket.IO leave_room skipped for disconnected sid=%s namespace=%s room=%s",
                getattr(request, "sid", None),
                getattr(request, "namespace", "/"),
                room,
            )
