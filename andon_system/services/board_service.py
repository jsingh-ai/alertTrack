from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import joinedload

from ..company_context import get_current_company_id
from ..models.alert import ALERT_STATUSES_ACTIVE, EVENT_CREATED, AndonAlert, AndonAlertEvent
from ..models.department import Department
from ..models.issue import IssueCategory
from ..models.machine import Machine
from ..models.user import User
from .cache_service import get_cached, set_cached
from .radius_service import build_radius_status_map

BOARD_STATE_CACHE_TTL_SECONDS = 5
OPERATOR_METADATA_CACHE_TTL_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc)


def build_board_state():
    company_id = get_current_company_id()
    cache_key = ("board_state", company_id)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    context = _load_board_context(company_id, include_alerts=True)

    result = {
        "machines": [
            _serialize_machine(
                machine,
                context["alert_by_machine"],
                context["created_notes_by_alert_id"],
                context["radius_status_by_machine"],
            )
            for machine in context["visible_machines"]
        ],
        "departments": [
            {
                "id": department.id,
                "name": department.name,
            }
            for department in context["visible_departments"]
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
            for category in context["visible_issue_categories"]
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
            for user in context["visible_users"]
        ],
        "filters": {
            "machine_types": _unique_values(machine.machine_type for machine in context["visible_machines"]),
            "areas": _unique_values(machine.area for machine in context["visible_machines"]),
            "lines": _unique_values(machine.line for machine in context["visible_machines"]),
            "departments": _unique_values(machine.department.name for machine in context["visible_machines"] if machine.department),
        },
    }
    set_cached(cache_key, result, BOARD_STATE_CACHE_TTL_SECONDS)
    return result


def build_operator_snapshot():
    company_id = get_current_company_id()
    cache_key = ("operator_snapshot", company_id)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    context = _load_board_context(company_id, include_alerts=True)
    result = {
        "machines": [
            _serialize_machine(
                machine,
                context["alert_by_machine"],
                context["created_notes_by_alert_id"],
                context["radius_status_by_machine"],
            )
            for machine in context["visible_machines"]
        ],
    }
    set_cached(cache_key, result, BOARD_STATE_CACHE_TTL_SECONDS)
    return result


def build_operator_metadata():
    company_id = get_current_company_id()
    cache_key = ("operator_metadata", company_id)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    context = _load_board_context(company_id, include_alerts=False)
    result = {
        "departments": [
            {
                "id": department.id,
                "name": department.name,
            }
            for department in context["visible_departments"]
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
            for category in context["visible_issue_categories"]
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
            for user in context["visible_users"]
        ],
    }
    set_cached(cache_key, result, OPERATOR_METADATA_CACHE_TTL_SECONDS)
    return result


def _load_board_context(company_id, include_alerts: bool):
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
    visible_machines = [machine for machine in machines if machine.is_active and (machine.department is None or machine.department.is_active)]
    visible_departments = departments
    visible_issue_categories = [
        category
        for category in issue_categories
        if category.department and category.department.is_active
    ]
    visible_users = [
        user
        for user in user_query.filter_by(is_active=True).order_by(User.display_name.asc()).all()
        if (user.department is None or user.department.is_active)
        and (user.machine_group is None or user.machine_group.is_active)
    ]
    radius_status_by_machine = build_radius_status_map(visible_machines)

    alert_by_machine = {}
    created_notes_by_alert_id = {}
    if include_alerts:
        active_alerts = (
            alert_query.options(
                joinedload(AndonAlert.issue_category),
                joinedload(AndonAlert.issue_problem),
            )
            .order_by(AndonAlert.created_at.desc())
            .all()
        )
        visible_machine_ids = {machine.id for machine in visible_machines}
        visible_category_ids = {category.id for category in visible_issue_categories}
        visible_department_ids = {department.id for department in visible_departments}
        active_alerts = [
            alert
            for alert in active_alerts
            if alert.machine_id in visible_machine_ids
            and (alert.department_id is None or alert.department_id in visible_department_ids)
            and (alert.issue_category_id is None or alert.issue_category_id in visible_category_ids)
        ]
        created_notes_by_alert_id = _created_notes_by_alert_id(active_alerts, company_id)
        for alert in active_alerts:
            alert_by_machine.setdefault(alert.machine_id, alert)

    return {
        "visible_machines": visible_machines,
        "visible_departments": visible_departments,
        "visible_issue_categories": visible_issue_categories,
        "visible_users": visible_users,
        "radius_status_by_machine": radius_status_by_machine,
        "alert_by_machine": alert_by_machine,
        "created_notes_by_alert_id": created_notes_by_alert_id,
    }


def _serialize_active_alert(alert, created_note=None):
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
        "created_note": created_note,
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


def _serialize_machine(machine, alert_by_machine, created_notes_by_alert_id, radius_status_by_machine):
    active_alert = alert_by_machine.get(machine.id)
    created_note = created_notes_by_alert_id.get(active_alert.id) if active_alert else None
    return {
        "id": machine.id,
        "name": machine.name,
        "machine_code": machine.machine_code,
        "machine_type": machine.machine_type,
        "radius_machine_id": machine.radius_machine_id,
        "area": machine.area,
        "line": machine.line,
        "department_id": machine.department_id,
        "department_name": machine.department.name if machine.department else None,
        "is_active": machine.is_active,
        "radius": radius_status_by_machine.get(machine.id),
        "active_alert": _serialize_active_alert(active_alert, created_note),
    }


def _ensure_aware(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _unique_values(values):
    return [value for value in dict.fromkeys(value for value in values if value)]


def _created_notes_by_alert_id(alerts, company_id):
    alert_ids = [alert.id for alert in alerts if alert.id]
    if not alert_ids:
        return {}
    query = AndonAlertEvent.query.filter(
        AndonAlertEvent.alert_id.in_(alert_ids),
        AndonAlertEvent.event_type == EVENT_CREATED,
    )
    if company_id:
        query = query.filter(AndonAlertEvent.company_id == company_id)
    created_notes = {}
    for event in query.order_by(AndonAlertEvent.event_at.asc()).all():
        metadata = event.metadata_json or {}
        note = str(metadata.get("note") or "").strip()
        if note and event.alert_id not in created_notes:
            created_notes[event.alert_id] = note
    return created_notes
