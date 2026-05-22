from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import time
from uuid import uuid4

from flask import current_app
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import select
from sqlalchemy.orm import joinedload, load_only, noload

from ..extensions import db
from ..company_context import get_current_company_id
from ..models.alert import (
    ALERT_STATUS_ACKNOWLEDGED,
    ALERT_STATUS_ARRIVED,
    ALERT_STATUS_CANCELLED,
    ALERT_STATUS_OPEN,
    ALERT_STATUS_RESOLVED,
    AndonAlert,
    AndonAlertEvent,
    EVENT_ACKNOWLEDGED,
    EVENT_ARRIVED,
    EVENT_CANCELLED,
    EVENT_CREATED,
    EVENT_NOTE_ADDED,
    EVENT_RESOLVED,
)
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.user import User, UserCompanyAccess
from ..models.department import Department
from ..security import get_current_membership, get_scope_filters
from .cache_service import invalidate_cache
from .active_alerts_service import fetch_active_alert_payloads
from .active_alerts_service import fetch_alert_payload_by_id
from .realtime_service import emit_alert_created, emit_alert_updated


class AlertServiceError(ValueError):
    def __init__(self, message, status_code=400, data=None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data or {}


def utc_now():
    return datetime.now(timezone.utc)


def _duration_seconds(start, end):
    if not start or not end:
        return None
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    return int((end - start).total_seconds())


def _ensure_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def list_active_alerts(status: str | None = None):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    membership = get_current_membership()
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    return fetch_active_alert_payloads(
        company_id=company_id,
        status=status,
        machine_ids=machine_ids,
        department_ids=department_ids,
        role=getattr(membership, "role", None),
    )


def get_alert(alert_id: int):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    membership = get_current_membership()
    role = membership.role if membership else None
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    query = AndonAlert.query.filter(AndonAlert.id == alert_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    if machine_ids:
        query = query.filter(AndonAlert.machine_id.in_(machine_ids))
    if department_ids and role != "Operator":
        query = query.filter(AndonAlert.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(AndonAlert.machine.has(Machine.machine_type.in_(machine_group_names)))
    alert = query.options(*_alert_response_options()).one_or_none()
    if not alert:
        raise AlertServiceError("Alert not found")
    return alert


def _perf_log_create_alert(metrics: dict) -> None:
    if not current_app.config.get("ANDON_PERF_LOGS"):
        return
    current_app.logger.debug(
        "PERF alert_create_service machine_lookup_ms=%.1f permission_scope_ms=%.1f issue_category_lookup_ms=%.1f "
        "issue_problem_lookup_ms=%.1f duplicate_active_check_ms=%.1f alert_object_build_ms=%.1f db_add_ms=%.1f "
        "db_flush_ms=%.1f note_insert_ms=%.1f event_insert_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f "
        "payload_fetch_ms=%.1f socket_emit_ms=%.1f escalation_check_ms=%.1f email_send_ms=%.1f notification_ms=%.1f total_ms=%.1f",
        metrics.get("machine_lookup_ms", 0.0),
        metrics.get("permission_scope_ms", 0.0),
        metrics.get("issue_category_lookup_ms", 0.0),
        metrics.get("issue_problem_lookup_ms", 0.0),
        metrics.get("duplicate_active_check_ms", 0.0),
        metrics.get("alert_object_build_ms", 0.0),
        metrics.get("db_add_ms", 0.0),
        metrics.get("db_flush_ms", 0.0),
        metrics.get("note_insert_ms", 0.0),
        metrics.get("event_insert_ms", 0.0),
        metrics.get("db_commit_ms", 0.0),
        metrics.get("cache_invalidate_ms", 0.0),
        metrics.get("payload_fetch_ms", 0.0),
        metrics.get("socket_emit_ms", 0.0),
        metrics.get("escalation_check_ms", 0.0),
        metrics.get("email_send_ms", 0.0),
        metrics.get("notification_ms", 0.0),
        metrics.get("total_ms", 0.0),
    )


def _find_active_alert_id_for_machine(machine_id: int, company_id: int | None) -> int | None:
    stmt = select(AndonAlert.id).where(
        AndonAlert.machine_id == machine_id,
        AndonAlert.status.in_([ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]),
    )
    if company_id:
        stmt = stmt.where(AndonAlert.company_id == company_id)
    return db.session.execute(stmt.limit(1)).scalar_one_or_none()


def create_alert(payload: dict, metrics: dict | None = None):
    started_at = time.perf_counter()
    perf = metrics if isinstance(metrics, dict) else {}
    company_id = get_current_company_id()
    scope = get_scope_filters()
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    machine_lookup_started_at = time.perf_counter()
    machine_query = Machine.query.options(
        load_only(
            Machine.id,
            Machine.company_id,
            Machine.machine_type,
            Machine.department_id,
            Machine.is_active,
        ),
        noload("*"),
    ).filter(Machine.id == payload.get("machine_id"))
    if company_id:
        machine_query = machine_query.filter(Machine.company_id == company_id)
    try:
        machine = machine_query.one_or_none()
    except OperationalError as exc:
        # On PostgreSQL, NOWAIT raises when another transaction currently holds
        # the row lock; returning quickly avoids multi-second UI jitter.
        raise AlertServiceError(
            "Machine is busy with another in-progress call. Please try again.",
            status_code=409,
        ) from exc
    perf["machine_lookup_ms"] = (time.perf_counter() - machine_lookup_started_at) * 1000
    department_id = payload.get("department_id")
    issue_category_lookup_started_at = time.perf_counter()
    issue_category = None
    if payload.get("issue_category_id"):
        category_query = IssueCategory.query.options(
            load_only(
                IssueCategory.id,
                IssueCategory.company_id,
                IssueCategory.department_id,
                IssueCategory.name,
                IssueCategory.priority_default,
                IssueCategory.is_active,
            ),
            noload("*"),
        ).filter(IssueCategory.id == payload.get("issue_category_id"))
        if company_id:
            category_query = category_query.filter(IssueCategory.company_id == company_id)
        issue_category = category_query.one_or_none()
    perf["issue_category_lookup_ms"] = (time.perf_counter() - issue_category_lookup_started_at) * 1000
    issue_problem_lookup_started_at = time.perf_counter()
    problem_query = IssueProblem.query.options(
        load_only(
            IssueProblem.id,
            IssueProblem.company_id,
            IssueProblem.category_id,
            IssueProblem.name,
            IssueProblem.severity_default,
            IssueProblem.is_active,
        ),
        noload("*"),
    ).filter(IssueProblem.id == payload.get("issue_problem_id"))
    if company_id:
        problem_query = problem_query.filter(IssueProblem.company_id == company_id)
    issue_problem = problem_query.one_or_none()
    perf["issue_problem_lookup_ms"] = (time.perf_counter() - issue_problem_lookup_started_at) * 1000
    operator_user = None
    if payload.get("operator_user_id"):
        operator_query = User.query.options(
            load_only(User.id, User.company_id, User.display_name, User.is_active),
            noload("*"),
        ).filter(User.id == payload.get("operator_user_id"))
        if company_id:
            operator_query = operator_query.filter(User.company_id == company_id)
        operator_user = operator_query.one_or_none()

    permission_scope_started_at = time.perf_counter()
    if not machine:
        raise AlertServiceError("Valid machine_id is required")
    if machine_ids and machine.id not in set(machine_ids):
        raise AlertServiceError("Machine is outside your assigned scope", status_code=403)
    if company_id and machine.company_id != company_id:
        raise AlertServiceError("Machine does not belong to the selected company")
    if department_ids and machine.department_id not in set(department_ids):
        raise AlertServiceError("Machine is outside your assigned department scope", status_code=403)
    if machine_group_names and machine.machine_type not in set(machine_group_names):
        raise AlertServiceError("Machine is outside your assigned machine group scope", status_code=403)
    perf["permission_scope_ms"] = (time.perf_counter() - permission_scope_started_at) * 1000
    # Keep the friendly conflict path even though the database also enforces
    # one active alert per machine now.
    duplicate_check_started_at = time.perf_counter()
    existing_alert_id = _find_active_alert_id_for_machine(machine.id, company_id)
    perf["duplicate_active_check_ms"] = (time.perf_counter() - duplicate_check_started_at) * 1000
    if existing_alert_id:
        existing_payload = fetch_alert_payload_by_id(existing_alert_id, company_id=company_id)
        raise AlertServiceError(
            "An active alert already exists for this machine",
            status_code=409,
            data={"existing_alert": existing_payload or {"id": existing_alert_id}},
        )
    if not department_id and not issue_category:
        raise AlertServiceError("department_id is required")
    if not issue_category:
        category_query = IssueCategory.query.filter_by(department_id=department_id, is_active=True)
        if company_id:
            category_query = category_query.filter(IssueCategory.company_id == company_id)
        issue_category = category_query.one_or_none()
    if not issue_category and department_id:
        department = Department.query.get(department_id)
        if department and department.company_id == company_id:
            issue_category = IssueCategory(
                name=department.name,
                department_id=department.id,
                company_id=department.company_id,
                color="#0d6efd",
                priority_default=3,
                is_active=True,
            )
            db.session.add(issue_category)
            db.session.flush()
    if not issue_category:
        raise AlertServiceError("Valid issue category is required for the selected department")
    if not issue_problem:
        raise AlertServiceError("Valid issue_problem_id is required")
    if issue_problem.category_id != issue_category.id:
        raise AlertServiceError("Issue problem must belong to the selected department")
    if company_id and issue_category.company_id != company_id:
        raise AlertServiceError("Issue category does not belong to the selected company")
    if company_id and issue_problem.company_id != company_id:
        raise AlertServiceError("Issue problem does not belong to the selected company")

    department_id = issue_category.department_id

    alert_build_started_at = time.perf_counter()
    resolved_company_id = company_id or machine.company_id
    alert_number = f"AL-{utc_now():%Y%m%d%H%M%S}-{uuid4().hex[:6].upper()}"
    alert = AndonAlert(
        company_id=resolved_company_id,
        alert_number=alert_number,
        machine_id=machine.id,
        department_id=department_id,
        issue_category_id=issue_category.id,
        issue_problem_id=issue_problem.id,
        status=ALERT_STATUS_OPEN,
        priority=payload.get("priority") or issue_category.priority_default or issue_problem.severity_default or 3,
        operator_user_id=operator_user.id if operator_user else None,
        operator_name_text=payload.get("operator_name_text"),
        note=payload.get("note"),
    )
    perf["alert_object_build_ms"] = (time.perf_counter() - alert_build_started_at) * 1000
    try:
        add_started_at = time.perf_counter()
        db.session.add(alert)
        perf["db_add_ms"] = (time.perf_counter() - add_started_at) * 1000
        flush_started_at = time.perf_counter()
        db.session.flush()
        perf["db_flush_ms"] = (time.perf_counter() - flush_started_at) * 1000

        event_started_at = time.perf_counter()
        _add_event(
            alert,
            EVENT_CREATED,
            user=operator_user,
            user_name_text=payload.get("operator_name_text") or (operator_user.display_name if operator_user else None),
            message="Alert created",
            metadata={"note": payload.get("note")},
        )
        perf["event_insert_ms"] = (time.perf_counter() - event_started_at) * 1000
        perf["note_insert_ms"] = 0.0
        commit_started_at = time.perf_counter()
        db.session.commit()
        perf["db_commit_ms"] = (time.perf_counter() - commit_started_at) * 1000
    except IntegrityError as exc:
        db.session.rollback()
        existing_alert_id = _find_active_alert_id_for_machine(machine.id, resolved_company_id)
        if existing_alert_id:
            existing_payload = fetch_alert_payload_by_id(existing_alert_id, company_id=resolved_company_id)
            raise AlertServiceError(
                "An active alert already exists for this machine",
                status_code=409,
                data={"existing_alert": existing_payload or {"id": existing_alert_id}},
            ) from exc
        existing_any = _get_latest_alert_for_machine(machine.id, resolved_company_id)
        if existing_any:
            raise AlertServiceError(
                "A machine-level alert uniqueness rule blocked this alert. Close or cancel the existing alert for this machine and try again.",
                status_code=409,
                data={"existing_alert": existing_any.to_dict()},
            ) from exc
        raise AlertServiceError("Unable to create alert due to a database constraint", status_code=409) from exc
    cache_started_at = time.perf_counter()
    _invalidate_live_caches(alert.company_id)
    perf["cache_invalidate_ms"] = (time.perf_counter() - cache_started_at) * 1000
    emit_started_at = time.perf_counter()
    emit_alert_created(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status)
    perf["socket_emit_ms"] = (time.perf_counter() - emit_started_at) * 1000
    perf.setdefault("payload_fetch_ms", 0.0)
    perf.setdefault("escalation_check_ms", 0.0)
    perf.setdefault("email_send_ms", 0.0)
    perf.setdefault("notification_ms", 0.0)
    perf["total_ms"] = (time.perf_counter() - started_at) * 1000
    _perf_log_create_alert(perf)
    return alert


def acknowledge_alert(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    if alert.status != ALERT_STATUS_OPEN:
        raise AlertServiceError("Alert can only be acknowledged from OPEN state")

    responder = _resolve_user(payload.get("responder_user_id"))
    now = utc_now()
    if alert.status == ALERT_STATUS_OPEN:
        alert.acknowledged_at = now
        alert.acknowledged_seconds = _duration_seconds(alert.created_at, now)
        alert.status = ALERT_STATUS_ACKNOWLEDGED
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    alert.responder_name_text = alert.responder_name_text or payload.get("responder_name_text")
    _replace_alert_note(alert, payload.get("note"))

    _add_event(
        alert,
        EVENT_ACKNOWLEDGED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Alert acknowledged",
        metadata={"responder_name_text": alert.responder_name_text},
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="acknowledged")
    return alert


def mark_arrived(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    if alert.status != ALERT_STATUS_ACKNOWLEDGED:
        raise AlertServiceError("Alert must be acknowledged before marking arrived")

    responder = _resolve_user(payload.get("responder_user_id"))
    now = utc_now()
    if alert.status != ALERT_STATUS_ARRIVED:
        alert.arrived_at = now
        alert.status = ALERT_STATUS_ARRIVED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    _replace_alert_note(alert, payload.get("note"))

    _add_event(
        alert,
        EVENT_ARRIVED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Responder arrived",
        metadata={"responder_name_text": alert.responder_name_text},
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="arrived")
    return alert


def resolve_alert(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    if alert.status in [ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED]:
        raise AlertServiceError("Alert is already closed")
    if alert.status not in [ALERT_STATUS_ARRIVED, ALERT_STATUS_ACKNOWLEDGED]:
        raise AlertServiceError("Alert must be acknowledged before resolving")

    responder = _resolve_user(payload.get("responder_user_id"))
    now = utc_now()
    alert.resolved_at = now
    if alert.acknowledged_at:
        alert.ack_to_clear_seconds = _duration_seconds(alert.acknowledged_at, now)
    alert.status = ALERT_STATUS_RESOLVED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    alert.resolution_note = payload.get("resolution_note")
    alert.root_cause = payload.get("root_cause")
    alert.corrective_action = payload.get("corrective_action")
    _append_alert_note(alert, payload.get("note"))

    _add_event(
        alert,
        EVENT_RESOLVED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Alert resolved",
        metadata={
            "resolution_note": alert.resolution_note,
            "root_cause": alert.root_cause,
            "corrective_action": alert.corrective_action,
        },
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="resolved")
    return alert


def cancel_alert(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    if alert.status not in [ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]:
        raise AlertServiceError("Alert can only be cancelled before arrival or resolution")
    responder = _resolve_user(payload.get("responder_user_id"))
    now = utc_now()
    alert.cancelled_at = now
    if alert.acknowledged_at:
        alert.ack_to_clear_seconds = _duration_seconds(alert.acknowledged_at, now)
    alert.status = ALERT_STATUS_CANCELLED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    _append_alert_note(alert, payload.get("note"))

    _add_event(
        alert,
        EVENT_CANCELLED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Alert cancelled",
        metadata={"reason": payload.get("reason")},
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="cancelled")
    return alert


def add_note(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    _add_event(
        alert,
        EVENT_NOTE_ADDED,
        user=_resolve_user(payload.get("user_id")),
        user_name_text=payload.get("user_name_text"),
        message=payload.get("message"),
        metadata=payload.get("metadata") or {},
    )
    if payload.get("note"):
        _append_alert_note(alert, payload.get("note"))
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="note_added")
    return alert


def _resolve_user(user_id):
    if not user_id:
        return None
    company_id = get_current_company_id()
    query = User.query.options(
        load_only(User.id, User.display_name, User.is_active),
        noload("*"),
    ).filter(User.id == user_id)
    user = query.one_or_none()
    if user is None:
        return None
    if company_id:
        access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id, is_active=True).one_or_none()
        if access is None:
            return None
    return user


def _add_event(alert, event_type, user=None, user_name_text=None, message=None, metadata=None):
    event = AndonAlertEvent(
        company_id=alert.company_id,
        alert=alert,
        event_type=event_type,
        user=user,
        user_name_text=user_name_text or (user.display_name if user else None),
        message=message,
        metadata_json=metadata or {},
    )
    db.session.add(event)


def _append_alert_note(alert, note_text):
    note = str(note_text or "").strip()
    if not note:
        return
    if alert.note:
        alert.note = f"{alert.note}\n{note}".strip()
    else:
        alert.note = note


def _replace_alert_note(alert, note_text):
    note = str(note_text or "").strip()
    if not note:
        return
    alert.note = note


def get_active_alert_metrics():
    alerts = list_active_alerts(status="active")
    grouped = defaultdict(int)
    for alert in alerts:
        status = alert.get("status") if isinstance(alert, dict) else getattr(alert, "status", None)
        grouped[status] += 1
    return grouped


def _invalidate_live_caches(company_id):
    # Alert lifecycle updates do not change operator metadata. Keep cache
    # invalidation scoped to live board/report/pager views to avoid expensive
    # metadata recompute on every call/ack/close.
    invalidate_cache(company_id=company_id)


def _get_active_alert_for_machine(machine_id, company_id):
    query = AndonAlert.query.filter(
        AndonAlert.machine_id == machine_id,
        AndonAlert.status.notin_([ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED]),
    )
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    return query.options(*_alert_response_options()).order_by(AndonAlert.created_at.desc()).first()


def _get_latest_alert_for_machine(machine_id, company_id):
    query = AndonAlert.query.filter(AndonAlert.machine_id == machine_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    return query.options(*_alert_response_options()).order_by(AndonAlert.created_at.desc()).first()


def _alert_response_options():
    return (
        joinedload(AndonAlert.machine)
        .load_only(
            Machine.id,
            Machine.company_id,
            Machine.machine_code,
            Machine.name,
            Machine.machine_type,
            Machine.radius_machine_id,
            Machine.area,
            Machine.line,
            Machine.department_id,
            Machine.description,
            Machine.is_active,
            Machine.created_at,
        )
        .joinedload(Machine.department)
        .load_only(Department.id, Department.name),
        joinedload(AndonAlert.department).load_only(Department.id, Department.name),
        joinedload(AndonAlert.issue_category)
        .load_only(
            IssueCategory.id,
            IssueCategory.company_id,
            IssueCategory.name,
            IssueCategory.department_id,
            IssueCategory.color,
            IssueCategory.priority_default,
            IssueCategory.is_active,
            IssueCategory.created_at,
        )
        .joinedload(IssueCategory.department)
        .load_only(Department.id, Department.name),
        joinedload(AndonAlert.issue_problem)
        .load_only(
            IssueProblem.id,
            IssueProblem.company_id,
            IssueProblem.category_id,
            IssueProblem.name,
            IssueProblem.description,
            IssueProblem.severity_default,
            IssueProblem.is_active,
            IssueProblem.created_at,
        )
        .joinedload(IssueProblem.category)
        .load_only(IssueCategory.id, IssueCategory.name, IssueCategory.department_id),
        noload(AndonAlert.company),
        noload(AndonAlert.operator_user),
        noload(AndonAlert.responder_user),
        noload(AndonAlert.events),
        noload(AndonAlert.escalations),
    )
