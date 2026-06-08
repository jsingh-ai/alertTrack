from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, insert, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
import time

from flask import Blueprint, current_app, jsonify, flash, redirect, request, url_for
from werkzeug.security import generate_password_hash

from ..company_context import get_current_company
from ..extensions import db
from ..extensions import socketio
from ..models.alert import AndonAlert, AndonAlertEvent
from ..models.department import Department
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.pager_device import PagerDevice
from ..models.user import USER_ROLES, USER_SCOPE_MODES, User, UserCompanyAccess
from ..security import fingerprint_pager_token, hash_pager_token, require_admin_authentication
from ..services.cache_service import invalidate_cache
from ..services.radius_service import resolve_radius_machine_id
from ..services.realtime_service import emit_admin_metadata_updated

admin_bp = Blueprint("admin", __name__, url_prefix="/andon/admin")


@admin_bp.before_request
def require_admin_session():
    require_admin_authentication()


def _int_or_none(value):
    if value in [None, ""]:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_ajax_request() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"


def _json_response(message: str, **payload):
    response = {"ok": True, "message": message}
    response.update(payload)
    return jsonify(response)


def _error_or_404(message: str):
    if _is_ajax_request():
        return jsonify({"ok": False, "message": message}), 404
    flash(message, "warning")
    return redirect(url_for("pages.admin_page"))


def _machine_group_count(group_name: str) -> int:
    company_id = _company_id()
    query = Machine.query.filter(Machine.machine_type == group_name)
    if company_id:
        query = query.filter(Machine.company_id == company_id)
    return query.count()


def _company_id():
    company = get_current_company()
    return company.id if company else None


def _invalidate_company_caches(company_id):
    if company_id is None:
        return
    app = current_app._get_current_object()

    def _run_invalidation():
        started_at = time.perf_counter()
        with app.app_context():
            invalidate_started_at = time.perf_counter()
            # A company-level version bump invalidates all company-scoped namespaces.
            invalidate_cache(company_id=company_id)
            invalidate_ms = (time.perf_counter() - invalidate_started_at) * 1000

            emit_started_at = time.perf_counter()
            emit_admin_metadata_updated(company_id)
            emit_ms = (time.perf_counter() - emit_started_at) * 1000

            if app.config.get("ANDON_PERF_LOGS"):
                app.logger.debug(
                    "PERF admin_cache_invalidate company_id=%s invalidate_ms=%.1f emit_ms=%.1f total_ms=%.1f",
                    company_id,
                    invalidate_ms,
                    emit_ms,
                    (time.perf_counter() - started_at) * 1000,
                )

    try:
        if socketio is not None:
            socketio.start_background_task(_run_invalidation)
            return
    except Exception:
        current_app.logger.exception("Unable to queue admin cache invalidation company_id=%s", company_id)

    _run_invalidation()


def _log_admin_mutation_perf(action: str, started_at: float, **segments):
    if not current_app.config.get("ANDON_PERF_LOGS"):
        return
    suffix = " ".join(f"{key}={value}" for key, value in segments.items())
    current_app.logger.debug(
        "PERF admin_mutation action=%s total_ms=%.1f %s",
        action,
        (time.perf_counter() - started_at) * 1000,
        suffix,
    )


def _is_postgresql() -> bool:
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


def _configure_admin_pg_transaction(*, lock_timeout_ms: int = 1500, statement_timeout_ms: int = 5000) -> None:
    if not _is_postgresql():
        return
    safe_lock_timeout_ms = max(1, int(lock_timeout_ms))
    safe_statement_timeout_ms = max(1, int(statement_timeout_ms))
    db.session.execute(text(f"SET LOCAL lock_timeout = '{safe_lock_timeout_ms}ms'"))
    db.session.execute(text(f"SET LOCAL statement_timeout = '{safe_statement_timeout_ms}ms'"))


def _admin_mutation_busy_response(message: str = "This record is busy. Please try again.") -> tuple:
    db.session.rollback()
    if _is_ajax_request():
        return jsonify({"ok": False, "message": message}), 409
    flash(message, "warning")
    return redirect(url_for("pages.admin_page"))


def _pager_device_payload(company_id: int, department_id: int) -> dict:
    row = (
        db.session.query(PagerDevice.active, PagerDevice.name, PagerDevice.last_seen_at)
        .filter(PagerDevice.company_id == company_id, PagerDevice.department_id == department_id)
        .order_by(PagerDevice.id.asc())
        .first()
    )
    if row is None:
        return {"active": False, "name": None, "last_seen_at": None}
    return {
        "active": bool(row.active),
        "name": row.name,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _create_department_pager_device(company_id: int, department_id: int, department_name: str) -> dict:
    placeholder_token = secrets.token_urlsafe(32)
    placeholder_token_hash = hash_pager_token(placeholder_token)
    row = db.session.execute(
        insert(PagerDevice)
        .values(
            company_id=company_id,
            department_id=department_id,
            name=f"{department_name} Pager",
            token_hash=placeholder_token_hash,
            token_fingerprint=fingerprint_pager_token(placeholder_token),
            active=False,
        )
        .returning(PagerDevice.active, PagerDevice.name, PagerDevice.last_seen_at),
    ).first()
    if row is None:
        return {"active": False, "name": None, "last_seen_at": None}
    return {
        "active": bool(row.active),
        "name": row.name,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _machine_payload(company_id: int, machine_id: int) -> dict | None:
    row = (
        db.session.query(
            Machine.id,
            Machine.name,
            Machine.machine_type,
            Machine.department_id,
            Machine.is_active,
            Department.name.label("department_name"),
        )
        .outerjoin(Department, Department.id == Machine.department_id)
        .filter(Machine.company_id == company_id, Machine.id == machine_id)
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "machine_type": row.machine_type,
        "machine_group": row.machine_type,
        "department_id": row.department_id,
        "department_name": row.department_name,
        "is_active": bool(row.is_active),
    }


def _issue_problem_payload(company_id: int, problem_id: int) -> dict | None:
    row = (
        db.session.query(
            IssueProblem.id,
            IssueProblem.name,
            IssueProblem.is_active,
            IssueCategory.department_id,
            Department.name.label("department_name"),
        )
        .join(IssueCategory, IssueProblem.category_id == IssueCategory.id)
        .join(Department, Department.id == IssueCategory.department_id)
        .filter(IssueProblem.company_id == company_id, IssueProblem.id == problem_id)
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "is_active": bool(row.is_active),
        "department_id": row.department_id,
        "department_name": row.department_name,
    }


def _escalation_rule_payload(company_id: int, rule_id: int) -> dict | None:
    row = (
        db.session.query(
            EscalationRule.id,
            EscalationRule.level,
            EscalationRule.delay_seconds,
            EscalationRule.is_active,
        )
        .filter(EscalationRule.company_id == company_id, EscalationRule.id == rule_id)
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "level": row.level,
        "delay_seconds": row.delay_seconds,
        "is_active": bool(row.is_active),
    }


def _machine_code_from_name(name: str, company_id: int | None) -> str:
    base = "".join(ch if ch.isalnum() else "-" for ch in name.upper()).strip("-")
    base = base or "MACHINE"
    candidate = base
    suffix = 2
    query = Machine.query
    if company_id is not None:
        query = query.filter(Machine.company_id == company_id)
    while query.filter_by(machine_code=candidate).first():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _membership_payload(access: UserCompanyAccess):
    user = access.user
    try:
        scope_config = json.loads(access.scope_config_json or "{}")
    except json.JSONDecodeError:
        scope_config = {}
    return {
        "id": user.id,
        "display_name": user.display_name,
        "username": user.username,
        "work_id": user.employee_id,
        "email": user.email,
        "phone_number": user.phone_number,
        "role": access.role,
        "scope_mode": access.scope_mode,
        "department_id": access.department_id,
        "department_name": access.department.name if access.department else None,
        "machine_group_id": access.machine_group_id,
        "machine_group_name": access.machine_group.name if access.machine_group else None,
        "scope_machine_ids": scope_config.get("machine_ids") or [],
        "scope_machine_group_ids": scope_config.get("machine_group_ids") or [],
        "scope_department_ids": scope_config.get("department_ids") or [],
        "is_active": access.is_active,
        "has_password": bool(user.password_hash),
    }


def _resolve_membership_scope(company_id, role, scope_mode, machine_group_id, department_id):
    if role not in USER_ROLES:
        return None, None, _validation_error("Please select a valid role")
    if scope_mode not in USER_SCOPE_MODES:
        return None, None, _validation_error("Please select a valid scope")
    if role == "Admin":
        scope_mode = "all"
    machine_group = MachineGroup.query.filter_by(id=machine_group_id, company_id=company_id).one_or_none() if machine_group_id is not None else None
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none() if department_id is not None else None
    if machine_group_id is not None and not machine_group:
        return None, None, _validation_error("Please select a valid machine group")
    if department_id is not None and not department:
        return None, None, _validation_error("Please select a valid department")
    return machine_group, department, None


def _int_list_from_csv(value: str | None) -> list[int]:
    raw = str(value or "").strip()
    if not raw:
        return []
    values = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            values.append(int(token))
    return sorted(set(values))


def _int_list_from_form(field_name: str) -> list[int]:
    values = []
    for raw in request.form.getlist(field_name):
        values.extend(_int_list_from_csv(raw))
    if values:
        return sorted(set(values))
    return _int_list_from_csv(request.form.get(field_name))


def _normalized_machine_group_names(values: set[str] | list[str] | tuple[str, ...]) -> set[str]:
    return {str(value or "").strip().lower() for value in (values or []) if str(value or "").strip()}


def _resolve_scope_config(company_id: int, role: str, machine_ids: list[int], machine_group_ids: list[int], department_ids: list[int]):
    valid_machine_ids = set()
    valid_group_ids = set()
    valid_department_ids = set()
    group_names_by_id = {}
    machine_department_ids = set()
    group_department_ids = set()
    active_group_rows = MachineGroup.query.with_entities(MachineGroup.id, MachineGroup.name).filter(
        MachineGroup.company_id == company_id,
        MachineGroup.is_active.is_(True),
    ).all()
    active_group_name_by_id = {row.id: row.name for row in active_group_rows if row.name}
    active_group_names = {row.name for row in active_group_rows if row.name}

    if machine_group_ids:
        valid_group_ids = {group_id for group_id in machine_group_ids if group_id in active_group_name_by_id}
        group_names_by_id = {group_id: active_group_name_by_id[group_id] for group_id in valid_group_ids}
    if department_ids:
        rows = Department.query.with_entities(Department.id).filter(
            Department.company_id == company_id,
            Department.id.in_(department_ids),
            Department.is_active.is_(True),
        ).all()
        valid_department_ids = {row.id for row in rows}

    selected_group_names = {name for name in group_names_by_id.values() if name}
    normalized_active_group_names = _normalized_machine_group_names(active_group_names)
    normalized_selected_group_names = _normalized_machine_group_names(selected_group_names)
    if machine_ids and active_group_names:
        rows = (
            Machine.query.with_entities(Machine.id, Machine.department_id, Machine.machine_type)
            .join(Department, Department.id == Machine.department_id)
            .filter(
                Machine.company_id == company_id,
                Machine.id.in_(machine_ids),
                Machine.is_active.is_(True),
                Department.is_active.is_(True),
                func.lower(func.trim(Machine.machine_type)).in_(list(normalized_active_group_names)),
            )
            .all()
        )
        if role == "Operator" and normalized_selected_group_names:
            rows = [row for row in rows if str(row.machine_type or "").strip().lower() in normalized_selected_group_names]
        valid_machine_ids = {row.id for row in rows}
        machine_department_ids = {row.department_id for row in rows if row.department_id is not None}

    group_machine_ids = set()

    if role == "Operator":
        if not valid_group_ids:
            return None, _validation_error("Operator requires at least one machine group in scope")
        if not valid_machine_ids:
            return None, _validation_error("Operator requires at least one machine ID in scope")
        resolved_department_ids = sorted(machine_department_ids)
        return {
            "machine_ids": sorted(valid_machine_ids),
            "machine_group_ids": sorted(valid_group_ids),
            "department_ids": resolved_department_ids,
        }, None
    if role == "Viewer":
        if not valid_department_ids:
            return None, _validation_error("Department role requires at least one department in scope")
        return {
            "machine_ids": [],
            "machine_group_ids": sorted(valid_group_ids),
            "department_ids": sorted(valid_department_ids),
        }, None
    if role == "Manager":
        if group_names_by_id:
            rows = (
                Machine.query.with_entities(Machine.id, Machine.department_id)
                .join(Department, Department.id == Machine.department_id)
                .filter(
                    Machine.company_id == company_id,
                    func.lower(func.trim(Machine.machine_type)).in_(list(_normalized_machine_group_names(group_names_by_id.values()))),
                    Machine.is_active.is_(True),
                    Department.is_active.is_(True),
                )
                .all()
            )
            group_machine_ids = {row.id for row in rows}
            group_department_ids = {row.department_id for row in rows if row.department_id is not None}
        if not valid_group_ids:
            valid_group_ids = {row.id for row in active_group_rows}
            group_names_by_id = {row.id: row.name for row in active_group_rows if row.name}
            if valid_group_ids:
                rows = (
                    Machine.query.with_entities(Machine.id, Machine.department_id)
                    .join(Department, Department.id == Machine.department_id)
                    .filter(
                        Machine.company_id == company_id,
                        func.lower(func.trim(Machine.machine_type)).in_(list(_normalized_machine_group_names(group_names_by_id.values()))),
                        Machine.is_active.is_(True),
                        Department.is_active.is_(True),
                    )
                    .all()
                )
                group_machine_ids = {row.id for row in rows}
                group_department_ids = {row.department_id for row in rows if row.department_id is not None}
        resolved_machine_ids = set(valid_machine_ids)
        resolved_machine_ids.update(group_machine_ids)
        resolved_department_ids = sorted(group_department_ids | machine_department_ids | valid_department_ids)
        return {
            "machine_ids": sorted(resolved_machine_ids),
            "machine_group_ids": sorted(valid_group_ids),
            "department_ids": resolved_department_ids,
        }, None
    return {
        "machine_ids": [],
        "machine_group_ids": [],
        "department_ids": [],
    }, None


def _serialize_pager_device(device: PagerDevice | None) -> dict:
    if device is None:
        return {"active": False, "name": None, "last_seen_at": None}
    return {
        "active": bool(device.active),
        "name": device.name,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
    }


def _ensure_department_pager_device(department: Department) -> PagerDevice:
    device = PagerDevice.query.filter_by(
        company_id=department.company_id,
        department_id=department.id,
    ).order_by(PagerDevice.id.asc()).first()
    if device is not None:
        return device
    placeholder_token = secrets.token_urlsafe(32)
    placeholder_token_hash = hash_pager_token(placeholder_token)
    device = PagerDevice(
        company_id=department.company_id,
        department_id=department.id,
        name=f"{department.name} Pager",
        token_hash=placeholder_token_hash,
        token_fingerprint=fingerprint_pager_token(placeholder_token),
        active=False,
    )
    db.session.add(device)
    db.session.flush()
    return device


def _validation_error(message: str):
    if _is_ajax_request():
        return jsonify({"ok": False, "message": message}), 400
    flash(message, "warning")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/create")
def create_department():
    started_at = time.perf_counter()
    company_id = _company_id()
    name = request.form["name"].strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Department name is required"}), 400
        flash("Department name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        existing = Department.query.filter(Department.company_id == company_id, Department.name == name).one_or_none()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if existing:
            db.session.rollback()
            if _is_ajax_request():
                return jsonify({"ok": False, "message": "Department already exists"}), 400
            flash("Department already exists", "warning")
            return redirect(url_for("pages.admin_page"))

        insert_started_at = time.perf_counter()
        row = db.session.execute(
            insert(Department)
            .values(company_id=company_id, name=name, is_active=True)
            .returning(Department.id, Department.name, Department.is_active),
        ).first()
        if row is None:
            db.session.rollback()
            return _error_or_404("Department not found")
        pager_device = _create_department_pager_device(company_id, row.id, row.name)
        insert_ms = (time.perf_counter() - insert_started_at) * 1000
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        _log_admin_mutation_perf(
            "create_department",
            started_at,
            company_id=company_id,
            lookup_ms=f"{lookup_ms:.1f}",
            insert_ms=f"{insert_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
        )
        return _json_response(
            "Department created",
            department={
                "id": row.id,
                "name": row.name,
                "is_active": bool(row.is_active),
                "pager_device": _serialize_pager_device(pager_device),
            },
        )
    _log_admin_mutation_perf(
        "create_department",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        insert_ms=f"{insert_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Department created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/<int:department_id>/toggle")
def toggle_department(department_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(Department)
            .where(Department.id == department_id, Department.company_id == company_id)
            .values(is_active=~Department.is_active)
            .returning(Department.id, Department.name, Department.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Department not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        pager_started_at = time.perf_counter()
        pager_device = _pager_device_payload(company_id, department_id)
        pager_ms = (time.perf_counter() - pager_started_at) * 1000
        _log_admin_mutation_perf(
            "toggle_department",
            started_at,
            company_id=company_id,
            department_id=department_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            pager_lookup_ms=f"{pager_ms:.1f}",
        )
        return _json_response(
            "Department updated",
            department={
                "id": row.id,
                "name": row.name,
                "is_active": bool(row.is_active),
                "pager_device": pager_device,
            },
        )
    _log_admin_mutation_perf(
        "toggle_department",
        started_at,
        company_id=company_id,
        department_id=department_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Department updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/<int:department_id>/update")
def update_department(department_id):
    company_id = _company_id()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not department:
        return _error_or_404("Department not found")
    name = (request.form.get("name") or "").strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Department name is required"}), 400
        flash("Department name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    existing = Department.query.filter(Department.company_id == company_id, Department.name == name, Department.id != department.id).one_or_none()
    if existing:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Department already exists"}), 400
        flash("Department already exists", "warning")
        return redirect(url_for("pages.admin_page"))

    old_name = department.name
    department.name = name
    for category in IssueCategory.query.filter_by(company_id=company_id, department_id=department.id).all():
        category.name = name
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Department updated",
            department={
                "id": department.id,
                "name": department.name,
                "old_name": old_name,
                "is_active": department.is_active,
            },
        )
    flash("Department updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/<int:department_id>/delete")
def delete_department(department_id):
    company_id = _company_id()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not department:
        return _error_or_404("Department not found")

    linked_category_ids = [category.id for category in IssueCategory.query.filter_by(company_id=company_id, department_id=department.id).all()]
    affected_user_ids = [user.id for user in User.query.filter_by(company_id=company_id, department_id=department.id).all()]

    for alert in AndonAlert.query.filter_by(company_id=company_id, department_id=department.id).all():
        db.session.delete(alert)

    if linked_category_ids:
        for rule in EscalationRule.query.filter(
            EscalationRule.company_id == company_id,
            (EscalationRule.department_id == department.id) | (EscalationRule.issue_category_id.in_(linked_category_ids))
        ).all():
            db.session.delete(rule)

        for problem in IssueProblem.query.filter(IssueProblem.company_id == company_id, IssueProblem.category_id.in_(linked_category_ids)).all():
            db.session.delete(problem)

        for category in IssueCategory.query.filter(IssueCategory.company_id == company_id, IssueCategory.id.in_(linked_category_ids)).all():
            db.session.delete(category)

    for machine in Machine.query.filter_by(company_id=company_id, department_id=department.id).all():
        machine.department_id = None

    for user in User.query.filter_by(company_id=company_id, department_id=department.id).all():
        user.department_id = None
    for access in UserCompanyAccess.query.filter_by(company_id=company_id, department_id=department.id).all():
        access.department_id = None
        if access.scope_mode == "restricted":
            access.scope_mode = "all"
    for pager in PagerDevice.query.filter_by(company_id=company_id, department_id=department.id).all():
        db.session.delete(pager)

    db.session.delete(department)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response("Department and linked issues removed", department_id=department_id, affected_user_ids=affected_user_ids)
    flash("Department and linked issues removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/<int:department_id>/pager-token/rotate")
def rotate_department_pager_token(department_id: int):
    company_id = _company_id()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not department:
        return _error_or_404("Department not found")
    pager_device = _ensure_department_pager_device(department)
    raw_token = secrets.token_urlsafe(32)
    pager_device.token_hash = hash_pager_token(raw_token)
    pager_device.token_fingerprint = fingerprint_pager_token(raw_token)
    pager_device.active = True
    pager_device.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Pager token generated",
            department_id=department.id,
            pager_device=_serialize_pager_device(pager_device),
            pager_token=raw_token,
        )
    flash("Pager token generated", "success")
    return redirect(url_for("pages.admin_page", section="departments"))


@admin_bp.post("/department/<int:department_id>/pager-token/toggle")
def toggle_department_pager_token(department_id: int):
    company_id = _company_id()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not department:
        return _error_or_404("Department not found")
    pager_device = _ensure_department_pager_device(department)
    pager_device.active = not bool(pager_device.active)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Pager device updated",
            department_id=department.id,
            pager_device=_serialize_pager_device(pager_device),
        )
    flash("Pager device updated", "success")
    return redirect(url_for("pages.admin_page", section="departments"))


@admin_bp.post("/machine/create")
def create_machine():
    started_at = time.perf_counter()
    company_id = _company_id()
    group_name = request.form.get("machine_group") or None
    if not group_name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please add a machine group"}), 400
        flash("Please add a machine group", "warning")
        return redirect(url_for("pages.admin_page"))

    group = MachineGroup.query.filter_by(company_id=company_id, name=group_name).one_or_none()
    if not group:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please add a machine group"}), 400
        flash("Please add a machine group", "warning")
        return redirect(url_for("pages.admin_page"))

    name = request.form["name"].strip()
    machine_code = (request.form.get("machine_code") or "").strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine name is required"}), 400
        flash("Machine name is required", "warning")
        return redirect(url_for("pages.admin_page"))
    if not machine_code:
        machine_code = _machine_code_from_name(name, company_id)

    department_id = _int_or_none(request.form.get("department_id"))
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        existing_machine = Machine.query.filter_by(company_id=company_id, machine_code=machine_code).one_or_none()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if existing_machine:
            db.session.rollback()
            if _is_ajax_request():
                return jsonify({"ok": False, "message": "Machine code already exists"}), 400
            flash("Machine code already exists", "warning")
            return redirect(url_for("pages.admin_page"))

        radius_machine_id = int(machine_code) if machine_code.isdigit() else resolve_radius_machine_id(
            Machine(
                company_id=company_id,
                machine_code=machine_code,
                name=name,
                machine_type=group.name,
                radius_machine_id=None,
                department_id=department_id,
                is_active=True,
            )
        )
        insert_started_at = time.perf_counter()
        row = db.session.execute(
            insert(Machine)
            .values(
                company_id=company_id,
                machine_code=machine_code,
                name=name,
                machine_type=group.name,
                radius_machine_id=radius_machine_id,
                department_id=department_id,
                is_active=True,
            )
            .returning(Machine.id, Machine.machine_code),
        ).first()
        if row is None:
            db.session.rollback()
            return _error_or_404("Machine not found")
        insert_ms = (time.perf_counter() - insert_started_at) * 1000
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        payload = _machine_payload(company_id, row.id)
        if payload is None:
            return _error_or_404("Machine not found")
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "create_machine",
            started_at,
            company_id=company_id,
            lookup_ms=f"{lookup_ms:.1f}",
            insert_ms=f"{insert_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response(
            "Machine created",
            machine=payload,
        )
    _log_admin_mutation_perf(
        "create_machine",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        insert_ms=f"{insert_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Machine created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine/<int:machine_id>/toggle")
def toggle_machine(machine_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(Machine)
            .where(Machine.id == machine_id, Machine.company_id == company_id)
            .values(is_active=~Machine.is_active)
            .returning(Machine.id, Machine.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Machine not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        machine_payload = _machine_payload(company_id, machine_id)
        if machine_payload is None:
            return _error_or_404("Machine not found")
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "toggle_machine",
            started_at,
            company_id=company_id,
            machine_id=machine_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response(
            "Machine updated",
            machine=machine_payload,
        )
    _log_admin_mutation_perf(
        "toggle_machine",
        started_at,
        company_id=company_id,
        machine_id=machine_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Machine updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine/<int:machine_id>/delete")
def delete_machine(machine_id):
    company_id = _company_id()
    machine = Machine.query.filter_by(id=machine_id, company_id=company_id).one_or_none()
    if not machine:
        return _error_or_404("Machine not found")
    group_name = machine.machine_type
    for alert in AndonAlert.query.filter_by(company_id=company_id, machine_id=machine.id).all():
        db.session.delete(alert)
    db.session.delete(machine)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response("Machine removed", machine_id=machine_id, machine_group=group_name)
    flash("Machine removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/create")
def create_machine_group():
    started_at = time.perf_counter()
    company_id = _company_id()
    name = request.form["name"].strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine group name is required"}), 400
        flash("Machine group name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        existing = MachineGroup.query.filter_by(company_id=company_id, name=name).one_or_none()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if existing:
            db.session.rollback()
            if _is_ajax_request():
                return jsonify({"ok": False, "message": "Machine group already exists"}), 400
            flash("Machine group already exists", "warning")
            return redirect(url_for("pages.admin_page"))

        insert_started_at = time.perf_counter()
        row = db.session.execute(
            insert(MachineGroup)
            .values(company_id=company_id, name=name, is_active=True)
            .returning(MachineGroup.id, MachineGroup.name, MachineGroup.is_active),
        ).first()
        if row is None:
            db.session.rollback()
            return _error_or_404("Machine group not found")
        insert_ms = (time.perf_counter() - insert_started_at) * 1000
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        _log_admin_mutation_perf(
            "create_machine_group",
            started_at,
            company_id=company_id,
            lookup_ms=f"{lookup_ms:.1f}",
            insert_ms=f"{insert_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
        )
        return _json_response(
            "Machine group created",
            machine_group={
                "id": row.id,
                "name": row.name,
                "is_active": bool(row.is_active),
                "machine_count": 0,
            },
        )
    _log_admin_mutation_perf(
        "create_machine_group",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        insert_ms=f"{insert_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Machine group created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/<int:group_id>/toggle")
def toggle_machine_group(group_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(MachineGroup)
            .where(MachineGroup.id == group_id, MachineGroup.company_id == company_id)
            .values(is_active=~MachineGroup.is_active)
            .returning(MachineGroup.id, MachineGroup.name, MachineGroup.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Machine group not found")

        machine_load_started_at = time.perf_counter()
        db.session.execute(
            update(Machine)
            .where(Machine.company_id == company_id, Machine.machine_type == row.name)
            .values(is_active=bool(row.is_active)),
        )
        machine_mutation_ms = (time.perf_counter() - machine_load_started_at) * 1000

        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        count_started_at = time.perf_counter()
        machine_count = (
            db.session.query(func.count(Machine.id))
            .filter(Machine.company_id == company_id, Machine.machine_type == row.name)
            .scalar()
            or 0
        )
        count_ms = (time.perf_counter() - count_started_at) * 1000
        _log_admin_mutation_perf(
            "toggle_machine_group",
            started_at,
            company_id=company_id,
            group_id=group_id,
            lookup_ms=f"{lookup_ms:.1f}",
            machine_mutation_ms=f"{machine_mutation_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            count_ms=f"{count_ms:.1f}",
        )
        return _json_response(
            "Machine group updated",
            machine_group={
                "id": row.id,
                "name": row.name,
                "is_active": bool(row.is_active),
                "machine_count": machine_count,
            },
        )
    _log_admin_mutation_perf(
        "toggle_machine_group",
        started_at,
        company_id=company_id,
        group_id=group_id,
        lookup_ms=f"{lookup_ms:.1f}",
        machine_mutation_ms=f"{machine_mutation_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Machine group updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/<int:group_id>/update")
def update_machine_group(group_id):
    company_id = _company_id()
    group = MachineGroup.query.filter_by(id=group_id, company_id=company_id).one_or_none()
    if not group:
        return _error_or_404("Machine group not found")
    name = (request.form.get("name") or "").strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine group name is required"}), 400
        flash("Machine group name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    existing = MachineGroup.query.filter(MachineGroup.company_id == company_id, MachineGroup.name == name, MachineGroup.id != group.id).one_or_none()
    if existing:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine group already exists"}), 400
        flash("Machine group already exists", "warning")
        return redirect(url_for("pages.admin_page"))

    old_name = group.name
    group.name = name
    for machine in Machine.query.filter(Machine.company_id == company_id, Machine.machine_type == old_name).all():
        machine.machine_type = name
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Machine group updated",
            machine_group={
                "id": group.id,
                "name": group.name,
                "old_name": old_name,
                "is_active": group.is_active,
                "machine_count": _machine_group_count(group.name),
            },
        )
    flash("Machine group updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/<int:group_id>/delete")
def delete_machine_group(group_id):
    company_id = _company_id()
    group = MachineGroup.query.filter_by(id=group_id, company_id=company_id).one_or_none()
    if not group:
        return _error_or_404("Machine group not found")
    machines = Machine.query.filter(Machine.company_id == company_id, Machine.machine_type == group.name).all()
    affected_machine_ids = [machine.id for machine in machines]
    affected_user_ids = [user.id for user in User.query.filter_by(company_id=company_id, machine_group_id=group.id).all()]
    for machine in machines:
        for alert in AndonAlert.query.filter_by(machine_id=machine.id).all():
            db.session.delete(alert)
        db.session.delete(machine)
    for user in User.query.filter_by(company_id=company_id, machine_group_id=group.id).all():
        user.machine_group_id = None
    for access in UserCompanyAccess.query.filter_by(company_id=company_id, machine_group_id=group.id).all():
        access.machine_group_id = None
        if access.scope_mode == "restricted":
            access.scope_mode = "all"
    db.session.delete(group)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Machine group removed",
            machine_group_id=group_id,
            affected_machine_ids=affected_machine_ids,
            affected_user_ids=affected_user_ids,
        )
    flash("Machine group removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-type/<string:machine_type>/toggle")
def toggle_machine_type(machine_type):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        target_state = request.form.get("is_active")
        if target_state in [None, ""]:
            current_state = db.session.query(func.bool_and(Machine.is_active)).filter(
                Machine.company_id == company_id,
                Machine.machine_type == machine_type,
            ).scalar()
            if current_state is None:
                db.session.rollback()
                flash("Machine group not found", "warning")
                return redirect(url_for("pages.admin_page"))
            target_value = not bool(current_state)
        else:
            target_value = target_state.lower() in {"1", "true", "yes", "on"}
        result = db.session.execute(
            update(Machine)
            .where(Machine.company_id == company_id, Machine.machine_type == machine_type)
            .values(is_active=target_value),
        )
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if (result.rowcount or 0) == 0:
            db.session.rollback()
            flash("Machine group not found", "warning")
            return redirect(url_for("pages.admin_page"))
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    _log_admin_mutation_perf(
        "toggle_machine_type",
        started_at,
        company_id=company_id,
        machine_type=machine_type,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash(f"{machine_type} group updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/create")
def create_user():
    started_at = time.perf_counter()
    company_id = _company_id()
    display_name = (request.form.get("display_name") or "").strip()
    username = (request.form.get("username") or "").strip() or None
    work_id = (request.form.get("work_id") or "").strip() or None
    password = request.form.get("password") or ""
    role = (request.form.get("role") or USER_ROLES[0]).strip()
    scope_mode = (request.form.get("scope_mode") or USER_SCOPE_MODES[0]).strip()
    if role == "Admin":
        scope_mode = "all"
    else:
        scope_mode = "restricted"
    machine_group_id = _int_or_none(request.form.get("machine_group_id"))
    department_id = _int_or_none(request.form.get("department_id"))
    scope_machine_ids = _int_list_from_form("scope_machine_ids")
    scope_machine_group_ids = _int_list_from_form("scope_machine_group_ids")
    scope_department_ids = _int_list_from_form("scope_department_ids")
    if role == "Viewer" and department_id is not None and not scope_department_ids:
        scope_department_ids = [department_id]
    email = (request.form.get("email") or "").strip() or None
    phone_number = (request.form.get("phone_number") or "").strip() or None
    if not display_name:
        return _validation_error("User name is required")
    if not username:
        return _validation_error("Username is required")
    if not password.strip():
        return _validation_error("Password is required")
    duplicate_user = User.query.filter(User.username == username).one_or_none() if username else None
    if duplicate_user is not None and UserCompanyAccess.query.filter_by(user_id=duplicate_user.id, company_id=company_id).one_or_none() is not None:
        return _validation_error("Username is already in use")
    duplicate_email = User.query.filter(User.email == email).one_or_none() if email else None
    if duplicate_email is not None and duplicate_email.username != username:
        return _validation_error("Email is already in use")
    machine_group, department, error_response = _resolve_membership_scope(
        company_id, role, scope_mode, machine_group_id, department_id
    )
    if error_response is not None:
        return error_response
    scope_config, error_response = _resolve_scope_config(
        company_id=company_id,
        role=role,
        machine_ids=scope_machine_ids,
        machine_group_ids=scope_machine_group_ids,
        department_ids=scope_department_ids,
    )
    if error_response is not None:
        return error_response

    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        user_department_id = department.id if role in {"Admin", "Viewer"} and department else None
        user_machine_group_id = machine_group.id if role == "Admin" and machine_group else None
        user = None
        if username:
            user = User.query.filter_by(username=username).one_or_none()
        if user is None and email:
            user = User.query.filter_by(email=email).one_or_none()
        if user is None:
            user_row = db.session.execute(
                insert(User)
                .values(
                    company_id=company_id,
                    employee_id=work_id,
                    display_name=display_name,
                    username=username,
                    role=role,
                    email=email,
                    phone_number=phone_number,
                    password_hash=generate_password_hash(password.strip()),
                    department_id=user_department_id,
                    machine_group_id=user_machine_group_id,
                    is_active=True,
                )
                .returning(User.id),
            ).first()
            if user_row is None:
                db.session.rollback()
                return _error_or_404("User not found")
            user_id = user_row.id
        else:
            existing_access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id).one_or_none()
            if existing_access is not None:
                db.session.rollback()
                return _validation_error("This user already has access to the selected company")
            db.session.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    display_name=display_name,
                    employee_id=work_id,
                    username=username or user.username,
                    email=email,
                    phone_number=phone_number,
                    password_hash=generate_password_hash(password.strip()) if password.strip() else user.password_hash,
                    role=role,
                    department_id=user_department_id,
                    machine_group_id=user_machine_group_id,
                ),
            )
            user_id = user.id

        access_row = db.session.execute(
            insert(UserCompanyAccess)
            .values(
                user_id=user_id,
                company_id=company_id,
                role=role,
                scope_mode="all" if role == "Admin" else scope_mode,
                department_id=user_department_id,
                machine_group_id=user_machine_group_id,
                scope_config_json=json.dumps(scope_config or {}, separators=(",", ":"), sort_keys=True),
                is_active=True,
            )
            .returning(UserCompanyAccess.id),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if access_row is None:
            db.session.rollback()
            return _error_or_404("User not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except IntegrityError:
        db.session.rollback()
        return _validation_error("Username or email is already in use")
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        access = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).one_or_none()
        if access is None:
            return _error_or_404("User not found")
        payload = _membership_payload(access)
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "create_user",
            started_at,
            company_id=company_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response("User created", user=payload)
    _log_admin_mutation_perf(
        "create_user",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("User created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/update")
def update_user(user_id):
    company_id = _company_id()
    user = User.query.filter_by(id=user_id).one_or_none()
    access = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).one_or_none()
    if not user or not access:
        return _error_or_404("User not found")
    display_name = (request.form.get("display_name") or "").strip()
    username = (request.form.get("username") or "").strip() or None
    work_id = (request.form.get("work_id") or "").strip() or None
    password = request.form.get("password") or ""
    role = (request.form.get("role") or access.role or USER_ROLES[0]).strip()
    scope_mode = (request.form.get("scope_mode") or access.scope_mode or USER_SCOPE_MODES[0]).strip()
    if role == "Admin":
        scope_mode = "all"
    else:
        scope_mode = "restricted"
    machine_group_id = _int_or_none(request.form.get("machine_group_id"))
    department_id = _int_or_none(request.form.get("department_id"))
    scope_machine_ids = _int_list_from_form("scope_machine_ids")
    scope_machine_group_ids = _int_list_from_form("scope_machine_group_ids")
    scope_department_ids = _int_list_from_form("scope_department_ids")
    if role == "Viewer" and department_id is not None and not scope_department_ids:
        scope_department_ids = [department_id]
    email = (request.form.get("email") or "").strip() or None
    phone_number = (request.form.get("phone_number") or "").strip() or None

    if not display_name:
        return _validation_error("User name is required")
    if not username:
        return _validation_error("Username is required")
    duplicate_user = User.query.filter(User.id != user.id, User.username == username).one_or_none() if username else None
    if duplicate_user is not None:
        return _validation_error("Username is already in use")
    duplicate_email = User.query.filter(User.id != user.id, User.email == email).one_or_none() if email else None
    if duplicate_email is not None:
        return _validation_error("Email is already in use")
    machine_group, department, error_response = _resolve_membership_scope(
        company_id, role, scope_mode, machine_group_id, department_id
    )
    if error_response is not None:
        return error_response
    scope_config, error_response = _resolve_scope_config(
        company_id=company_id,
        role=role,
        machine_ids=scope_machine_ids,
        machine_group_ids=scope_machine_group_ids,
        department_ids=scope_department_ids,
    )
    if error_response is not None:
        return error_response

    user.display_name = display_name
    user.employee_id = work_id
    user.username = username
    user.email = email
    user.phone_number = phone_number
    user.role = role
    user.machine_group_id = machine_group.id if role == "Admin" and machine_group else None
    user.department_id = department.id if role in {"Admin", "Viewer"} and department else None
    if password.strip():
        user.set_password(password.strip())
    access.role = role
    access.scope_mode = "all" if role == "Admin" else scope_mode
    access.machine_group_id = machine_group.id if role == "Admin" and machine_group else None
    access.department_id = department.id if role in {"Admin", "Viewer"} and department else None
    access.scope_config_json = json.dumps(scope_config or {}, separators=(",", ":"), sort_keys=True)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response("User updated", user=_membership_payload(access))
    flash("User updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/toggle")
def toggle_user(user_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(UserCompanyAccess)
            .where(UserCompanyAccess.user_id == user_id, UserCompanyAccess.company_id == company_id)
            .values(is_active=~UserCompanyAccess.is_active)
            .returning(UserCompanyAccess.id),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("User not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        access = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).one_or_none()
        if not access or access.user is None:
            return _error_or_404("User not found")
        user_payload = _membership_payload(access)
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "toggle_user",
            started_at,
            company_id=company_id,
            user_id=user_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response("User updated", user=user_payload)
    _log_admin_mutation_perf(
        "toggle_user",
        started_at,
        company_id=company_id,
        user_id=user_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("User updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/delete")
def delete_user(user_id):
    company_id = _company_id()
    access = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).one_or_none()
    user = access.user if access else None
    if user is None or access is None:
        return _error_or_404("User not found")

    db.session.execute(
        update(AndonAlert)
        .where(
            AndonAlert.company_id == company_id,
            AndonAlert.operator_user_id == user.id,
        )
        .values(operator_user_id=None, operator_name_text=None)
    )
    db.session.execute(
        update(AndonAlert)
        .where(
            AndonAlert.company_id == company_id,
            AndonAlert.responder_user_id == user.id,
        )
        .values(responder_user_id=None, responder_name_text=None)
    )
    db.session.execute(
        update(AndonAlertEvent)
        .where(AndonAlertEvent.company_id == company_id, AndonAlertEvent.user_id == user.id)
        .values(user_id=None, user_name_text=None)
    )

    db.session.delete(access)
    has_other_access = (
        db.session.query(UserCompanyAccess.id)
        .filter(
            UserCompanyAccess.user_id == user.id,
            UserCompanyAccess.id != access.id,
        )
        .limit(1)
        .first()
        is not None
    )
    if not has_other_access:
        user.is_active = False
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response("User removed", user_id=user_id)
    flash("User removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/problem/create")
def create_problem():
    started_at = time.perf_counter()
    company_id = _company_id()
    department_id = _int_or_none(request.form.get("department_id"))
    if department_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please add a department"}), 400
        flash("Please add a department", "warning")
        return redirect(url_for("pages.admin_page"))

    name = request.form["name"].strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Issue name is required"}), 400
        flash("Issue name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
        if not department:
            db.session.rollback()
            if _is_ajax_request():
                return jsonify({"ok": False, "message": "Please add a department"}), 400
            flash("Please add a department", "warning")
            return redirect(url_for("pages.admin_page"))

        category = IssueCategory.query.filter_by(company_id=company_id, department_id=department_id).one_or_none()
        if category is None:
            category_row = db.session.execute(
                insert(IssueCategory)
                .values(
                    name=department.name,
                    department_id=department.id,
                    company_id=department.company_id,
                    color="#0d6efd",
                    priority_default=3,
                    is_active=True,
                )
                .returning(IssueCategory.id),
            ).first()
            category_id = category_row.id if category_row is not None else None
        else:
            category_id = category.id
        if category_id is None:
            db.session.rollback()
            return _error_or_404("Issue category not found")

        existing_problem = IssueProblem.query.filter_by(company_id=company_id, category_id=category_id, name=name).one_or_none()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if existing_problem:
            db.session.rollback()
            if _is_ajax_request():
                return jsonify({"ok": False, "message": "Issue already exists"}), 400
            flash("Issue already exists", "warning")
            return redirect(url_for("pages.admin_page"))

        insert_started_at = time.perf_counter()
        row = db.session.execute(
            insert(IssueProblem)
            .values(
                company_id=company_id,
                category_id=category_id,
                name=name,
                severity_default=3,
                is_active=True,
            )
            .returning(IssueProblem.id, IssueProblem.name, IssueProblem.is_active),
        ).first()
        if row is None:
            db.session.rollback()
            return _error_or_404("Issue problem not found")
        insert_ms = (time.perf_counter() - insert_started_at) * 1000
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        payload = _issue_problem_payload(company_id, row.id)
        if payload is None:
            return _error_or_404("Issue problem not found")
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "create_problem",
            started_at,
            company_id=company_id,
            lookup_ms=f"{lookup_ms:.1f}",
            insert_ms=f"{insert_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response(
            "Issue problem created",
            problem=payload,
        )
    _log_admin_mutation_perf(
        "create_problem",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        insert_ms=f"{insert_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Issue problem created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/problem/<int:problem_id>/toggle")
def toggle_problem(problem_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(IssueProblem)
            .where(IssueProblem.id == problem_id, IssueProblem.company_id == company_id)
            .values(is_active=~IssueProblem.is_active)
            .returning(IssueProblem.id, IssueProblem.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Issue problem not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        payload_started_at = time.perf_counter()
        problem_payload = _issue_problem_payload(company_id, problem_id)
        if problem_payload is None:
            return _error_or_404("Issue problem not found")
        payload_ms = (time.perf_counter() - payload_started_at) * 1000
        _log_admin_mutation_perf(
            "toggle_problem",
            started_at,
            company_id=company_id,
            problem_id=problem_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
            payload_ms=f"{payload_ms:.1f}",
        )
        return _json_response(
            "Issue problem updated",
            problem=problem_payload,
        )
    _log_admin_mutation_perf(
        "toggle_problem",
        started_at,
        company_id=company_id,
        problem_id=problem_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Issue problem updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/problem/<int:problem_id>/delete")
def delete_problem(problem_id):
    company_id = _company_id()
    problem = IssueProblem.query.filter_by(id=problem_id, company_id=company_id).one_or_none()
    if not problem:
        return _error_or_404("Issue problem not found")
    department_id = problem.category.department_id if problem.category else None
    for alert in AndonAlert.query.filter_by(company_id=company_id, issue_problem_id=problem.id).all():
        db.session.delete(alert)
    db.session.delete(problem)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response("Issue removed", problem_id=problem_id, department_id=department_id)
    flash("Issue removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/escalation/create")
def create_escalation_rule():
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            insert(EscalationRule)
            .values(
                company_id=company_id,
                department_id=_int_or_none(request.form.get("department_id")),
                issue_category_id=_int_or_none(request.form.get("issue_category_id")),
                issue_problem_id=_int_or_none(request.form.get("issue_problem_id")),
                machine_id=_int_or_none(request.form.get("machine_id")),
                level=int(request.form.get("level") or 1),
                delay_seconds=int(request.form.get("delay_seconds") or 300),
                notify_role=request.form.get("notify_role"),
                notify_target=request.form.get("notify_target"),
                is_active=True,
            )
            .returning(EscalationRule.id, EscalationRule.level, EscalationRule.delay_seconds, EscalationRule.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Escalation rule not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    _log_admin_mutation_perf(
        "create_escalation_rule",
        started_at,
        company_id=company_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Escalation rule created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/escalation/<int:rule_id>/update")
def update_escalation_rule(rule_id):
    company_id = _company_id()
    rule = EscalationRule.query.filter_by(id=rule_id, company_id=company_id).one_or_none()
    if not rule:
        return _error_or_404("Escalation rule not found")
    rule.delay_seconds = int(request.form.get("delay_seconds") or rule.delay_seconds or 0)
    db.session.commit()
    _invalidate_company_caches(company_id)
    if _is_ajax_request():
        return _json_response(
            "Escalation rule updated",
            rule={
                "id": rule.id,
                "level": rule.level,
                "delay_seconds": rule.delay_seconds,
                "is_active": rule.is_active,
            },
        )
    flash("Escalation rule updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/escalation/<int:rule_id>/toggle")
def toggle_escalation_rule(rule_id):
    started_at = time.perf_counter()
    company_id = _company_id()
    try:
        lookup_started_at = time.perf_counter()
        _configure_admin_pg_transaction()
        row = db.session.execute(
            update(EscalationRule)
            .where(EscalationRule.id == rule_id, EscalationRule.company_id == company_id)
            .values(is_active=~EscalationRule.is_active)
            .returning(EscalationRule.id, EscalationRule.level, EscalationRule.delay_seconds, EscalationRule.is_active),
        ).first()
        lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
        if row is None:
            db.session.rollback()
            return _error_or_404("Escalation rule not found")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_ms = (time.perf_counter() - commit_started_at) * 1000
    except OperationalError:
        return _admin_mutation_busy_response()

    invalidate_started_at = time.perf_counter()
    _invalidate_company_caches(company_id)
    invalidate_queue_ms = (time.perf_counter() - invalidate_started_at) * 1000
    if _is_ajax_request():
        rule_payload = _escalation_rule_payload(company_id, rule_id)
        if rule_payload is None:
          return _error_or_404("Escalation rule not found")
        _log_admin_mutation_perf(
            "toggle_escalation_rule",
            started_at,
            company_id=company_id,
            rule_id=rule_id,
            lookup_ms=f"{lookup_ms:.1f}",
            commit_ms=f"{commit_ms:.1f}",
            invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
        )
        return _json_response(
            "Escalation rule updated",
            rule=rule_payload,
        )
    _log_admin_mutation_perf(
        "toggle_escalation_rule",
        started_at,
        company_id=company_id,
        rule_id=rule_id,
        lookup_ms=f"{lookup_ms:.1f}",
        commit_ms=f"{commit_ms:.1f}",
        invalidate_queue_ms=f"{invalidate_queue_ms:.1f}",
    )
    flash("Escalation rule updated", "success")
    return redirect(url_for("pages.admin_page"))
