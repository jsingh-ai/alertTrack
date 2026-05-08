from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import joinedload, selectinload

from ..company_context import get_current_company_id
from ..models.alert import ALERT_STATUSES_ACTIVE, EVENT_CREATED, AndonAlert
from ..models.department import Department
from ..models.issue import IssueCategory
from ..models.machine import Machine
from ..models.user import User


def utc_now():
    return datetime.now(timezone.utc)


def build_board_state():
    company_id = get_current_company_id()
    machine_query = Machine.query.options(joinedload(Machine.department))
    department_query = Department.query
    issue_query = IssueCategory.query.options(joinedload(IssueCategory.department), joinedload(IssueCategory.problems))
    user_query = User.query.options(joinedload(User.department), joinedload(User.machine_group))
    alert_query = AndonAlert.query.filter(AndonAlert.status.in_(ALERT_STATUSES_ACTIVE))
    if company_id:
        machine_query = machine_query.filter(Machine.company_id == company_id)
        department_query = department_query.filter(Department.company_id == company_id)
        issue_query = issue_query.filter(IssueCategory.company_id == company_id)
        user_query = user_query.filter(User.company_id == company_id)
        alert_query = alert_query.filter(AndonAlert.company_id == company_id)

    machines = machine_query.order_by(Machine.machine_type.asc().nullslast(), Machine.name.asc()).all()
    departments = department_query.filter_by(is_active=True).order_by(Department.name.asc()).all()
    issue_categories = issue_query.filter_by(is_active=True).order_by(IssueCategory.name.asc()).all()
    active_alerts = (
        alert_query.options(
            joinedload(AndonAlert.issue_category),
            joinedload(AndonAlert.issue_problem),
            selectinload(AndonAlert.events),
        )
        .order_by(AndonAlert.created_at.desc())
        .all()
    )
    users = user_query.filter_by(is_active=True).order_by(User.display_name.asc()).all()

    alert_by_machine = {}
    for alert in active_alerts:
        alert_by_machine.setdefault(alert.machine_id, alert)

    return {
        "machines": [
            {
                "id": machine.id,
                "name": machine.name,
                "machine_code": machine.machine_code,
                "machine_type": machine.machine_type,
                "area": machine.area,
                "line": machine.line,
                "department_id": machine.department_id,
                "department_name": machine.department.name if machine.department else None,
                "is_active": machine.is_active,
                "active_alert": _serialize_active_alert(alert_by_machine.get(machine.id)),
            }
            for machine in machines
        ],
        "departments": [
            {
                "id": department.id,
                "name": department.name,
            }
            for department in departments
        ],
        "issue_groups": [
            {
                "department_id": category.department_id,
                "department_name": category.department.name if category.department else None,
                "category_id": category.id,
                "category_name": category.name,
                "problems": [
                    {
                        "id": problem.id,
                        "name": problem.name,
                        "description": problem.description,
                    }
                    for problem in sorted(category.problems or [], key=lambda item: item.name.lower())
                ],
            }
            for category in issue_categories
            if category.department and category.department.is_active
        ],
        "users": [
            {
                "id": user.id,
                "display_name": user.display_name,
                "work_id": user.employee_id,
                "department_id": user.department_id,
                "department_name": user.department.name if user.department else None,
                "machine_group_id": user.machine_group_id,
                "machine_group_name": user.machine_group.name if user.machine_group else None,
            }
            for user in users
        ],
        "filters": {
            "machine_types": _unique_values(machine.machine_type for machine in machines),
            "areas": _unique_values(machine.area for machine in machines),
            "lines": _unique_values(machine.line for machine in machines),
            "departments": _unique_values(machine.department.name for machine in machines if machine.department),
        },
    }


def _serialize_active_alert(alert):
    if not alert:
        return None
    now = _ensure_aware(utc_now())
    created_at = _ensure_aware(alert.created_at)
    acknowledged_at = _ensure_aware(alert.acknowledged_at)
    if alert.status == "OPEN" or not acknowledged_at:
        elapsed_start = created_at
    else:
        elapsed_start = acknowledged_at
    elapsed_seconds = int((now - elapsed_start).total_seconds()) if elapsed_start else None
    return {
        "id": alert.id,
        "department_id": alert.department_id,
        "department_name": alert.department.name if alert.department else None,
        "responder_user_id": alert.responder_user_id,
        "responder_name_text": alert.responder_name_text,
        "note": alert.note,
        "created_note": _get_created_note(alert),
        "category_name": alert.issue_category.name if alert.issue_category else None,
        "problem_name": alert.issue_problem.name if alert.issue_problem else None,
        "status": alert.status,
        "priority": alert.priority,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "elapsed_seconds": elapsed_seconds,
        "acknowledged_seconds": alert.acknowledged_seconds,
        "ack_to_clear_seconds": alert.ack_to_clear_seconds,
        "color": alert.issue_category.color if alert.issue_category and alert.issue_category.color else "#ef476f",
    }


def _ensure_aware(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unique_values(values):
    return [value for value in dict.fromkeys(value for value in values if value)]


def _get_created_note(alert):
    for event in sorted(alert.events or [], key=lambda item: item.event_at or alert.created_at):
        if event.event_type == EVENT_CREATED:
            metadata = event.metadata_json or {}
            note = str(metadata.get("note") or "").strip()
            return note or None
    return None
