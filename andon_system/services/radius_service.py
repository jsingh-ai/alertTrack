from __future__ import annotations

import re
import threading

from flask import current_app, has_app_context
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError


_RADIUS_ENGINE = None
_RADIUS_ENGINE_URL = None
_RADIUS_LOCK = threading.RLock()

_PRESS_MACHINE_OVERRIDES = {
    2: 201,
}
_MIN_SUPPORTED_PRESS_NUMBER = 2
_MAX_SUPPORTED_PRESS_NUMBER = 15


def build_radius_status_map(machines):
    radius_ids = {}
    for machine in machines:
        radius_machine_id = resolve_radius_machine_id(machine)
        if radius_machine_id is not None:
            radius_ids[machine.id] = radius_machine_id

    if not radius_ids:
        return {}

    rows_by_radius_id = _fetch_radius_rows(set(radius_ids.values()))
    status_map = {}
    for machine_id, radius_machine_id in radius_ids.items():
        row = rows_by_radius_id.get(radius_machine_id)
        status_map[machine_id] = {
            "machine_id": radius_machine_id,
            "operation_code": row.get("operation_code") if row else None,
            "job_code": row.get("job_code") if row else None,
            "event_type": row.get("event_type") if row else None,
            "status_code": row.get("status_code") if row else None,
            "status_description": row.get("status_description") if row else None,
            "status_label": _status_label(row) if row else None,
        }
    return status_map


def resolve_radius_machine_id(machine):
    explicit_value = getattr(machine, "radius_machine_id", None)
    if explicit_value not in (None, ""):
        try:
            return int(explicit_value)
        except (TypeError, ValueError):
            return None

    machine_type = str(getattr(machine, "machine_type", "") or "").strip().lower()
    if machine_type != "press":
        return None

    machine_number = _extract_machine_number(machine)
    if machine_number is None:
        return None
    if machine_number < _MIN_SUPPORTED_PRESS_NUMBER or machine_number > _MAX_SUPPORTED_PRESS_NUMBER:
        return None
    return _PRESS_MACHINE_OVERRIDES.get(machine_number, 200 + machine_number)


def _extract_machine_number(machine):
    candidates = [
        str(getattr(machine, "name", "") or ""),
        str(getattr(machine, "machine_code", "") or ""),
    ]
    for candidate in candidates:
        match = re.search(r"(\d+)", candidate)
        if match:
            return int(match.group(1))
    return None


def _fetch_radius_rows(radius_machine_ids):
    engine = _get_radius_engine()
    if engine is None or not radius_machine_ids:
        return {}

    statement = text(
        """
        SELECT machine_id, operation_code, job_code, status_code, status_description, event_type
        FROM machine_status_current
        WHERE machine_id IN :machine_ids
        """,
    ).bindparams(bindparam("machine_ids", expanding=True))

    try:
        with engine.connect() as connection:
            rows = connection.execute(statement, {"machine_ids": sorted(radius_machine_ids)}).mappings().all()
    except SQLAlchemyError:
        if has_app_context():
            current_app.logger.exception("Unable to load Radius machine status data")
        return {}

    return {int(row["machine_id"]): dict(row) for row in rows if row.get("machine_id") is not None}


def _get_radius_engine():
    global _RADIUS_ENGINE
    global _RADIUS_ENGINE_URL

    url = _radius_database_url()
    if not url:
        return None

    with _RADIUS_LOCK:
        if _RADIUS_ENGINE is not None and _RADIUS_ENGINE_URL == url:
            return _RADIUS_ENGINE
        _RADIUS_ENGINE = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        _RADIUS_ENGINE_URL = url
        return _RADIUS_ENGINE


def _radius_database_url():
    if has_app_context():
        return current_app.config.get("PRESS_RADIUS_DATABASE_URL")
    return None


def _status_label(row):
    status_code = row.get("status_code")
    status_description = row.get("status_description")
    if status_code and status_description:
        return f"{status_code} - {status_description}"
    return status_code or status_description or None
