from __future__ import annotations

from datetime import datetime, timezone
import time

from flask import current_app, has_request_context
from sqlalchemy.orm import joinedload, load_only, noload

from ..company_context import get_current_company_id
from ..models.alert import ALERT_STATUSES_ACTIVE, EVENT_CREATED, AndonAlert, AndonAlertEvent
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import User, UserCompanyAccess
from ..security import get_authenticated_user, get_current_membership, get_scope_filters
from .cache_service import get_cached, set_cached
from .radius_service import build_radius_status_map

BOARD_STATE_CACHE_TTL_SECONDS = 10
DEFAULT_OPERATOR_METADATA_CACHE_TTL_SECONDS = 60


def utc_now():
    return datetime.now(timezone.utc)


def _perf_enabled() -> bool:
    return has_request_context() and bool(current_app.config.get("ANDON_PERF_LOGS"))


def _perf_log(event: str, started_at: float, **extra) -> None:
    if not _perf_enabled():
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    suffix = " ".join(f"{key}={value}" for key, value in extra.items())
    current_app.logger.debug("PERF %s duration_ms=%.1f %s", event, duration_ms, suffix)


def build_board_state(include_metadata: bool = True):
    company_id = get_current_company_id()
    cache_key = ("board_state", company_id, include_metadata)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    context = _load_board_context(company_id, include_alerts=True, include_metadata=include_metadata)

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
        "filters": {
            "machine_types": _unique_values(machine.machine_type for machine in context["visible_machines"]),
            "areas": _unique_values(machine.area for machine in context["visible_machines"]),
            "lines": _unique_values(machine.line for machine in context["visible_machines"]),
            "departments": _unique_values(machine.department.name for machine in context["visible_machines"] if machine.department),
        },
    }
    if include_metadata:
        result.update(
            {
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
        )
    set_cached(cache_key, result, BOARD_STATE_CACHE_TTL_SECONDS)
    return result


def build_operator_snapshot():
    company_id = get_current_company_id()
    cache_key = ("operator_snapshot", company_id)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    context = _load_board_context(
        company_id,
        include_alerts=True,
        include_metadata=False,
        include_radius=False,
    )
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


def build_operator_metadata(company_id=None, current_user=None, membership=None, scope=None, metrics: dict | None = None):
    started_at = time.perf_counter()
    company_id = company_id if company_id is not None else get_current_company_id()
    current_user = current_user or get_authenticated_user()
    membership = membership or get_current_membership(user=current_user)
    scope = scope or get_scope_filters(membership=membership)
    cache_key = _operator_metadata_cache_key(company_id, current_user, membership, scope)

    cache_started_at = time.perf_counter()
    cached = get_cached(cache_key)
    cache_lookup_ms = (time.perf_counter() - cache_started_at) * 1000
    if metrics is not None:
        metrics["cache_lookup_ms"] = round(cache_lookup_ms, 1)
    if cached is not None:
        if metrics is not None:
            metrics["cache"] = "hit"
            metrics["counts"] = {
                "departments": len(cached.get("departments") or []),
                "issue_groups": len(cached.get("issue_groups") or []),
                "users": len(cached.get("users") or []),
            }
        _perf_log(
            "operator_metadata(service)",
            started_at,
            cache="hit",
            company_id=company_id,
            user_id=getattr(current_user, "id", None),
            role=getattr(membership, "role", None),
        )
        return cached

    context_metrics = {}
    context = _load_operator_metadata_context(
        company_id,
        membership=membership,
        scope=scope,
        metrics=context_metrics,
    )
    serialize_started_at = time.perf_counter()
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
    serialize_ms = (time.perf_counter() - serialize_started_at) * 1000
    ttl_seconds = _operator_metadata_cache_ttl_seconds()
    cache_store_started_at = time.perf_counter()
    set_cached(cache_key, result, ttl_seconds)
    cache_store_ms = (time.perf_counter() - cache_store_started_at) * 1000
    if metrics is not None:
        metrics.update(context_metrics)
        metrics["cache"] = "miss"
        metrics["serialize_ms"] = round(serialize_ms, 1)
        metrics["cache_store_ms"] = round(cache_store_ms, 1)
        metrics["cache_ttl_seconds"] = ttl_seconds
        metrics["counts"] = {
            "departments": len(result["departments"]),
            "issue_groups": len(result["issue_groups"]),
            "users": len(result["users"]),
        }
    _perf_log(
        "operator_metadata(service)",
        started_at,
        cache="miss",
        company_id=company_id,
        user_id=getattr(current_user, "id", None),
        role=getattr(membership, "role", None),
        departments=len(result["departments"]),
        issue_groups=len(result["issue_groups"]),
        users=len(result["users"]),
    )
    return result


def _load_operator_metadata_context(company_id, membership=None, scope=None, metrics: dict | None = None):
    scope_started_at = time.perf_counter()
    scope = scope or get_scope_filters()
    if metrics is not None and "scope_ms" not in metrics:
        metrics["scope_ms"] = round((time.perf_counter() - scope_started_at) * 1000, 1)

    membership_started_at = time.perf_counter()
    membership = membership or get_current_membership()
    if metrics is not None and "membership_ms" not in metrics:
        metrics["membership_ms"] = round((time.perf_counter() - membership_started_at) * 1000, 1)

    role = membership.role if membership else None
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    metadata_department_ids = list(department_ids)

    if role == "Operator" and company_id:
        all_departments_started_at = time.perf_counter()
        all_department_rows = (
            Department.query.options(noload("*"))
            .with_entities(Department.id)
            .filter(Department.company_id == company_id)
            .all()
        )
        all_department_ids = sorted({row.id for row in all_department_rows if row.id is not None})
        if all_department_ids:
            metadata_department_ids = all_department_ids
        if metrics is not None:
            metrics["operator_department_scope_ms"] = round((time.perf_counter() - all_departments_started_at) * 1000, 1)

    department_query = Department.query.options(
        load_only(Department.id, Department.company_id, Department.name, Department.is_active),
        noload("*"),
    )
    issue_query = IssueCategory.query.options(
        load_only(
            IssueCategory.id,
            IssueCategory.company_id,
            IssueCategory.name,
            IssueCategory.department_id,
            IssueCategory.color,
            IssueCategory.priority_default,
            IssueCategory.is_active,
        ),
        joinedload(IssueCategory.department).load_only(Department.id, Department.name, Department.is_active),
        joinedload(IssueCategory.problems).load_only(
            IssueProblem.id,
            IssueProblem.name,
            IssueProblem.description,
            IssueProblem.is_active,
        ),
        noload(IssueCategory.alerts),
    )
    user_query = UserCompanyAccess.query.options(
        load_only(
            UserCompanyAccess.id,
            UserCompanyAccess.company_id,
            UserCompanyAccess.user_id,
            UserCompanyAccess.department_id,
            UserCompanyAccess.machine_group_id,
            UserCompanyAccess.is_active,
        ),
        joinedload(UserCompanyAccess.user).load_only(
            User.id,
            User.display_name,
            User.employee_id,
            User.department_id,
            User.machine_group_id,
            User.is_active,
        ),
        joinedload(UserCompanyAccess.department).load_only(Department.id, Department.name, Department.is_active),
        joinedload(UserCompanyAccess.machine_group).load_only(MachineGroup.id, MachineGroup.name, MachineGroup.is_active),
        noload(UserCompanyAccess.company),
    )

    if company_id:
        department_query = department_query.filter(Department.company_id == company_id)
        issue_query = issue_query.filter(IssueCategory.company_id == company_id)
        user_query = user_query.filter(UserCompanyAccess.company_id == company_id)
    if metadata_department_ids:
        department_query = department_query.filter(Department.id.in_(metadata_department_ids))
        issue_query = issue_query.filter(IssueCategory.department_id.in_(metadata_department_ids))
        user_query = user_query.filter(UserCompanyAccess.department_id.in_(metadata_department_ids))
    if machine_group_names:
        user_query = user_query.join(UserCompanyAccess.machine_group).filter(MachineGroup.name.in_(machine_group_names))

    departments_started_at = time.perf_counter()
    if role == "Operator":
        departments = department_query.order_by(Department.name.asc()).all()
    else:
        departments = department_query.filter_by(is_active=True).order_by(Department.name.asc()).all()
    departments_ms = (time.perf_counter() - departments_started_at) * 1000

    issue_started_at = time.perf_counter()
    issue_categories = issue_query.filter_by(is_active=True).order_by(IssueCategory.name.asc()).all()
    issue_ms = (time.perf_counter() - issue_started_at) * 1000

    visible_departments = [department for department in departments if role == "Operator" or department.is_active]
    visible_issue_categories = [
        category
        for category in issue_categories
        if category.department and category.department.is_active
    ]

    users_started_at = time.perf_counter()
    visible_users = [
        access.user
        for access in user_query.filter_by(is_active=True).order_by(UserCompanyAccess.id.asc()).all()
        if access.user
        and access.user.is_active
        and (access.department is None or access.department.is_active)
        and (access.machine_group is None or access.machine_group.is_active)
    ]
    users_ms = (time.perf_counter() - users_started_at) * 1000

    if metrics is not None:
        metrics["department_query_ms"] = round(departments_ms, 1)
        metrics["issue_query_ms"] = round(issue_ms, 1)
        metrics["user_query_ms"] = round(users_ms, 1)
        metrics["scope_department_count"] = len(metadata_department_ids)
        metrics["scope_machine_group_count"] = len(machine_group_names)

    return {
        "visible_departments": visible_departments,
        "visible_issue_categories": visible_issue_categories,
        "visible_users": visible_users,
    }


def _operator_metadata_cache_key(company_id, current_user, membership, scope):
    return (
        "operator_metadata",
        company_id,
        getattr(current_user, "id", None),
        getattr(membership, "id", None),
        getattr(membership, "role", None),
        getattr(membership, "scope_mode", None),
        getattr(membership, "department_id", None),
        getattr(membership, "machine_group_id", None),
        tuple(sorted(scope.get("department_ids") or [])),
        tuple(sorted(scope.get("machine_group_names") or [])),
        tuple(sorted(scope.get("machine_ids") or [])),
    )


def _operator_metadata_cache_ttl_seconds() -> int:
    try:
        raw_value = int(current_app.config.get("OPERATOR_METADATA_CACHE_TTL_SECONDS", DEFAULT_OPERATOR_METADATA_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        raw_value = DEFAULT_OPERATOR_METADATA_CACHE_TTL_SECONDS
    return max(1, min(raw_value, 300))


def _load_board_context(company_id, include_alerts: bool, include_metadata: bool = True, include_radius: bool = True):
    scope = get_scope_filters()
    membership = get_current_membership()
    role = membership.role if membership else None
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    metadata_department_ids = list(department_ids)

    if include_metadata and role == "Operator" and company_id:
        # Operator "Call <Department>" choices should be stable company-wide
        # and not depend on machine group or user scope narrowing.
        all_department_rows = (
            Department.query.options(noload("*"))
            .with_entities(Department.id)
            .filter(
                Department.company_id == company_id,
            )
            .all()
        )
        all_department_ids = sorted({row.id for row in all_department_rows if row.id is not None})
        if all_department_ids:
            metadata_department_ids = all_department_ids
    machine_query = Machine.query.options(
        load_only(
            Machine.id,
            Machine.company_id,
            Machine.machine_code,
            Machine.name,
            Machine.machine_type,
            Machine.radius_machine_id,
            Machine.area,
            Machine.line,
            Machine.department_id,
            Machine.is_active,
        ),
        joinedload(Machine.department).load_only(Department.id, Department.name, Department.is_active),
        noload(Machine.alerts),
    )
    department_query = (
        Department.query.options(
            load_only(Department.id, Department.company_id, Department.name, Department.is_active),
            noload("*"),
        )
        if include_metadata
        else None
    )
    issue_query = (
        IssueCategory.query.options(
            load_only(
                IssueCategory.id,
                IssueCategory.company_id,
                IssueCategory.name,
                IssueCategory.department_id,
                IssueCategory.color,
                IssueCategory.priority_default,
                IssueCategory.is_active,
            ),
            joinedload(IssueCategory.department).load_only(Department.id, Department.name, Department.is_active),
            joinedload(IssueCategory.problems).load_only(
                IssueProblem.id,
                IssueProblem.name,
                IssueProblem.description,
                IssueProblem.is_active,
            ),
            noload(IssueCategory.alerts),
        )
        if include_metadata
        else None
    )
    user_query = (
        UserCompanyAccess.query.options(
            load_only(
                UserCompanyAccess.id,
                UserCompanyAccess.company_id,
                UserCompanyAccess.user_id,
                UserCompanyAccess.department_id,
                UserCompanyAccess.machine_group_id,
                UserCompanyAccess.is_active,
            ),
            joinedload(UserCompanyAccess.user).load_only(
                User.id,
                User.display_name,
                User.employee_id,
                User.department_id,
                User.machine_group_id,
                User.is_active,
            ),
            joinedload(UserCompanyAccess.department).load_only(Department.id, Department.name, Department.is_active),
            joinedload(UserCompanyAccess.machine_group).load_only(MachineGroup.id, MachineGroup.name, MachineGroup.is_active),
            noload(UserCompanyAccess.company),
        )
        if include_metadata
        else None
    )
    alert_query = AndonAlert.query.filter(AndonAlert.status.in_(ALERT_STATUSES_ACTIVE))
    if company_id:
        machine_query = machine_query.filter(Machine.company_id == company_id)
        if include_metadata:
            department_query = department_query.filter(Department.company_id == company_id)
            issue_query = issue_query.filter(IssueCategory.company_id == company_id)
            user_query = user_query.filter(UserCompanyAccess.company_id == company_id)
        alert_query = alert_query.filter(AndonAlert.company_id == company_id)
    if machine_ids:
        machine_query = machine_query.filter(Machine.id.in_(machine_ids))
        alert_query = alert_query.filter(AndonAlert.machine_id.in_(machine_ids))
    if department_ids:
        machine_query = machine_query.filter(Machine.department_id.in_(department_ids))
        # For Operator scope we anchor on allowed machine IDs; filtering alerts
        # again by alert.department_id can hide valid calls when the selected
        # call department differs from the machine's native department.
        if role != "Operator":
            alert_query = alert_query.filter(AndonAlert.department_id.in_(department_ids))
    if include_metadata and metadata_department_ids:
        department_query = department_query.filter(Department.id.in_(metadata_department_ids))
        issue_query = issue_query.filter(IssueCategory.department_id.in_(metadata_department_ids))
        user_query = user_query.filter(UserCompanyAccess.department_id.in_(metadata_department_ids))
    if machine_group_names:
        machine_query = machine_query.filter(Machine.machine_type.in_(machine_group_names))
        if include_metadata:
            user_query = user_query.join(UserCompanyAccess.machine_group).filter(MachineGroup.name.in_(machine_group_names))
        alert_query = alert_query.filter(AndonAlert.machine.has(Machine.machine_type.in_(machine_group_names)))

    machines = machine_query.order_by(Machine.machine_type.asc().nullslast(), Machine.name.asc()).all()
    if include_metadata and role == "Operator":
        departments = department_query.order_by(Department.name.asc()).all()
    else:
        departments = department_query.filter_by(is_active=True).order_by(Department.name.asc()).all() if include_metadata else []
    issue_categories = issue_query.filter_by(is_active=True).order_by(IssueCategory.name.asc()).all() if include_metadata else []
    visible_machines = [machine for machine in machines if machine.is_active and (machine.department is None or machine.department.is_active)]
    visible_departments = [
        department
        for department in departments
        if role == "Operator" or department.is_active
    ]
    visible_issue_categories = [
        category
        for category in issue_categories
        if category.department and category.department.is_active
    ]
    visible_users = (
        [
            access.user
            for access in user_query.filter_by(is_active=True).order_by(UserCompanyAccess.id.asc()).all()
            if access.user
            and access.user.is_active
            and (access.department is None or access.department.is_active)
            and (access.machine_group is None or access.machine_group.is_active)
        ]
        if include_metadata
        else []
    )
    radius_status_by_machine = build_radius_status_map(visible_machines) if include_radius else {}

    alert_by_machine = {}
    created_notes_by_alert_id = {}
    if include_alerts:
        active_alerts = (
            alert_query.options(
                load_only(
                    AndonAlert.id,
                    AndonAlert.company_id,
                    AndonAlert.machine_id,
                    AndonAlert.department_id,
                    AndonAlert.issue_category_id,
                    AndonAlert.issue_problem_id,
                    AndonAlert.status,
                    AndonAlert.priority,
                    AndonAlert.responder_user_id,
                    AndonAlert.responder_name_text,
                    AndonAlert.note,
                    AndonAlert.created_at,
                    AndonAlert.acknowledged_at,
                    AndonAlert.acknowledged_seconds,
                    AndonAlert.ack_to_clear_seconds,
                ),
                joinedload(AndonAlert.department).load_only(Department.id, Department.name),
                joinedload(AndonAlert.issue_category).load_only(IssueCategory.id, IssueCategory.name, IssueCategory.color),
                joinedload(AndonAlert.issue_problem).load_only(IssueProblem.id, IssueProblem.name),
                noload(AndonAlert.machine),
                noload(AndonAlert.operator_user),
                noload(AndonAlert.responder_user),
                noload(AndonAlert.events),
                noload(AndonAlert.escalations),
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
            and (
                not include_metadata
                or (
                    (alert.department_id is None or alert.department_id in visible_department_ids)
                    and (alert.issue_category_id is None or alert.issue_category_id in visible_category_ids)
                )
            )
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
    for event in query.options(noload("*")).order_by(AndonAlertEvent.event_at.asc()).all():
        metadata = event.metadata_json or {}
        note = str(metadata.get("note") or "").strip()
        if note and event.alert_id not in created_notes:
            created_notes[event.alert_id] = note
    return created_notes
