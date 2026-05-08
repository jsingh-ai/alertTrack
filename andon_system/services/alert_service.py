from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

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
from ..models.user import User
from ..models.department import Department
from .cache_service import invalidate_cache
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
    query = AndonAlert.query
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    if status == "active":
        query = query.filter(AndonAlert.status.in_([ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]))
    elif status:
        query = query.filter(AndonAlert.status == status)
    return query.order_by(AndonAlert.priority.desc(), AndonAlert.created_at.asc()).all()


def get_alert(alert_id: int):
    company_id = get_current_company_id()
    query = AndonAlert.query.filter(AndonAlert.id == alert_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    alert = query.one_or_none()
    if not alert:
        raise AlertServiceError("Alert not found")
    return alert


def create_alert(payload: dict):
    company_id = get_current_company_id()
    machine_query = Machine.query.filter(Machine.id == payload.get("machine_id"))
    if company_id:
        machine_query = machine_query.filter(Machine.company_id == company_id)
    machine_query = machine_query.with_for_update()
    machine = machine_query.one_or_none()
    department_id = payload.get("department_id")
    issue_category = None
    if payload.get("issue_category_id"):
        category_query = IssueCategory.query.filter(IssueCategory.id == payload.get("issue_category_id"))
        if company_id:
            category_query = category_query.filter(IssueCategory.company_id == company_id)
        issue_category = category_query.one_or_none()
    problem_query = IssueProblem.query.filter(IssueProblem.id == payload.get("issue_problem_id"))
    if company_id:
        problem_query = problem_query.filter(IssueProblem.company_id == company_id)
    issue_problem = problem_query.one_or_none()
    operator_user = None
    if payload.get("operator_user_id"):
        operator_query = User.query.filter(User.id == payload.get("operator_user_id"))
        if company_id:
            operator_query = operator_query.filter(User.company_id == company_id)
        operator_user = operator_query.one_or_none()

    if not machine:
        raise AlertServiceError("Valid machine_id is required")
    if company_id and machine.company_id != company_id:
        raise AlertServiceError("Machine does not belong to the selected company")
    # A true hard guarantee still belongs in the database schema later.
    # This application-level check blocks the normal path now and keeps the
    # request safe across SQLite and MySQL without a migration.
    existing_alert = _get_active_alert_for_machine(machine.id, company_id)
    if existing_alert:
        raise AlertServiceError(
            "An active alert already exists for this machine",
            status_code=409,
            data={"existing_alert": existing_alert.to_dict()},
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

    alert_number = f"AL-{utc_now():%Y%m%d%H%M%S}-{uuid4().hex[:6].upper()}"
    alert = AndonAlert(
        company_id=company_id or machine.company_id,
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
    db.session.add(alert)
    db.session.flush()

    _add_event(
        alert,
        EVENT_CREATED,
        user=operator_user,
        user_name_text=payload.get("operator_name_text") or (operator_user.display_name if operator_user else None),
        message="Alert created",
        metadata={"note": payload.get("note")},
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_created(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status)
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
    if alert.status != ALERT_STATUS_ARRIVED:
        raise AlertServiceError("Alert must be marked arrived before resolving")

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
    query = User.query.filter(User.id == user_id)
    if company_id:
        query = query.filter(User.company_id == company_id)
    return query.one_or_none()


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
        grouped[alert.status] += 1
    return grouped


def get_alert_history(limit=100):
    return AndonAlert.query.order_by(AndonAlert.created_at.desc()).limit(limit).all()


def get_alerts_for_machine(machine_id):
    return AndonAlert.query.filter(AndonAlert.machine_id == machine_id).order_by(AndonAlert.created_at.desc()).all()


def check_alerts_for_escalation():
    from .escalation_service import check_escalations

    return check_escalations()


def _invalidate_live_caches(company_id):
    invalidate_cache("board_state", company_id)
    invalidate_cache("report_summary", company_id)
    invalidate_cache("report_machine_details", company_id)
    invalidate_cache("report_problem_details", company_id)


def _get_active_alert_for_machine(machine_id, company_id):
    query = AndonAlert.query.filter(
        AndonAlert.machine_id == machine_id,
        AndonAlert.status.notin_([ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED]),
    )
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    return query.order_by(AndonAlert.created_at.desc()).first()
