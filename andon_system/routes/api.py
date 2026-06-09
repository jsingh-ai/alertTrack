from __future__ import annotations

from datetime import datetime, timezone
import time

from flask import Blueprint, abort, current_app, g, jsonify, request
from sqlalchemy import or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, load_only, noload

from ..company_context import get_current_company_id
from ..models.alert import (
    ALERT_STATUS_ACKNOWLEDGED,
    ALERT_STATUS_ARRIVED,
    ALERT_STATUS_CANCELLED,
    ALERT_STATUS_OPEN,
    ALERT_STATUS_RESOLVED,
    AndonAlert,
)
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import User, UserBoard, UserBoardItem, UserCompanyAccess
from ..security import (
    PAGE_BOARD,
    PAGE_MANAGEMENT,
    PAGE_OPERATOR,
    PAGE_REPORTS,
    get_authenticated_user,
    get_current_membership,
    get_authenticated_pager_device,
    get_scope_filters,
    get_view_preference,
    require_authentication,
    save_view_preference,
    user_can_access_page,
)
from ..services.board_service import build_board_state, build_operator_metadata, build_operator_snapshot
from ..extensions import db
from ..services.alert_service import (
    AlertServiceError,
    acknowledge_alert,
    acknowledge_alert_scoped,
    add_note,
    cancel_alert,
    create_alert,
    get_alert,
    mark_arrived,
    resolve_alert,
    resolve_alert_scoped,
)
from ..services.active_alerts_service import fetch_active_alert_payloads, fetch_alert_payload_by_id
from ..services.reporting_service import (
    build_by_department,
    build_by_machine,
    build_by_problem,
    build_calls_per_hour,
    build_machine_details,
    build_machine_stats,
    build_report_summary,
    build_problem_details,
)
from ..services.escalation_service import check_escalations
from ..services.cache_service import get_cached, invalidate_cache, set_cached
from ..services.realtime_service import emit_machine_updated

api_bp = Blueprint("api", __name__, url_prefix="/api/andon")
PAGER_ACTIVE_ALERTS_CACHE_TTL_SECONDS = 15
PAGER_ACTIVE_ALERTS_STALE_TTL_SECONDS = 120


@api_bp.before_request
def require_user_session():
    if current_app.config.get("ANDON_PAGER_API_ONLY") and not request.path.startswith("/api/andon/pager/"):
        abort(404)
    if request.path.startswith("/api/andon/pager/"):
        pager_auth_started_at = time.perf_counter()
        pager = get_authenticated_pager_device(update_last_seen=False)
        g.api_authenticated_pager_device = pager
        g.api_pager_auth_ms = (time.perf_counter() - pager_auth_started_at) * 1000
        if pager:
            return
        abort(403)
    require_authentication()


def _require_any_page_access(*page_keys: str):
    if any(user_can_access_page(page_key) for page_key in page_keys):
        return
    abort(403)


@api_bp.get("/machines")
def machines():
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = Machine.query.options(
        joinedload(Machine.department).load_only(Department.id, Department.name),
        noload(Machine.alerts),
    ).filter_by(is_active=True)
    if company_id:
        query = query.filter(Machine.company_id == company_id)
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    if machine_ids:
        query = query.filter(Machine.id.in_(machine_ids))
    if department_ids:
        query = query.filter(Machine.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(Machine.machine_type.in_(machine_group_names))
    return jsonify({"success": True, "data": [machine.to_dict() for machine in query.order_by(Machine.name.asc()).all()]})


@api_bp.get("/departments")
def departments():
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = Department.query.options(noload("*")).filter_by(is_active=True)
    if company_id:
        query = query.filter(Department.company_id == company_id)
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    if department_ids:
        query = query.filter(Department.id.in_(department_ids))
    return jsonify({"success": True, "data": [department.to_dict() for department in query.order_by(Department.name.asc()).all()]})


@api_bp.get("/issue-categories")
def issue_categories():
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = IssueCategory.query.options(
        joinedload(IssueCategory.department).load_only(Department.id, Department.name),
        noload(IssueCategory.problems),
        noload(IssueCategory.alerts),
    ).filter_by(is_active=True)
    if company_id:
        query = query.filter(IssueCategory.company_id == company_id)
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    if department_ids:
        query = query.filter(IssueCategory.department_id.in_(department_ids))
    return jsonify({"success": True, "data": [category.to_dict() for category in query.order_by(IssueCategory.name.asc()).all()]})


@api_bp.get("/issue-problems")
def issue_problems():
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    category_id = request.args.get("category_id", type=int)
    query = IssueProblem.query.options(
        joinedload(IssueProblem.category)
        .load_only(IssueCategory.id, IssueCategory.name, IssueCategory.department_id)
        .joinedload(IssueCategory.department)
        .load_only(Department.id, Department.name),
        noload(IssueProblem.alerts),
    ).filter_by(is_active=True)
    company_id = get_current_company_id()
    scope = get_scope_filters()
    if company_id:
        query = query.filter(IssueProblem.company_id == company_id)
    if category_id:
        query = query.filter_by(category_id=category_id)
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    if department_ids:
        query = query.join(IssueProblem.category).filter(IssueCategory.department_id.in_(department_ids))
    data = [problem.to_dict() for problem in query.order_by(IssueProblem.name.asc()).all()]
    return jsonify({"success": True, "data": data})


@api_bp.get("/users")
def users():
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = UserCompanyAccess.query.options(
        load_only(
            UserCompanyAccess.id,
            UserCompanyAccess.user_id,
            UserCompanyAccess.company_id,
            UserCompanyAccess.role,
            UserCompanyAccess.scope_mode,
            UserCompanyAccess.department_id,
            UserCompanyAccess.machine_group_id,
            UserCompanyAccess.is_active,
            UserCompanyAccess.created_at,
            UserCompanyAccess.updated_at,
        ),
        joinedload(UserCompanyAccess.user).load_only(
            User.id,
            User.company_id,
            User.employee_id,
            User.display_name,
            User.username,
            User.role,
            User.email,
            User.phone_number,
            User.department_id,
            User.machine_group_id,
            User.is_active,
            User.last_login_at,
            User.created_at,
        ),
        joinedload(UserCompanyAccess.department).load_only(Department.id, Department.name, Department.is_active),
        joinedload(UserCompanyAccess.machine_group).load_only(MachineGroup.id, MachineGroup.name, MachineGroup.is_active),
        noload(UserCompanyAccess.company),
    ).filter_by(is_active=True)
    if company_id:
        query = query.filter(UserCompanyAccess.company_id == company_id)
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    if department_ids:
        query = query.filter(UserCompanyAccess.department_id.in_(department_ids))
    if machine_group_names:
        query = query.outerjoin(UserCompanyAccess.machine_group).filter(
            or_(
                MachineGroup.name.in_(machine_group_names),
                UserCompanyAccess.machine_group_id.is_(None),
            )
        )
    allowed_machine_groups = set()
    if machine_ids and company_id:
        machine_rows = Machine.query.options(noload("*")).with_entities(Machine.machine_type).filter(
            Machine.company_id == company_id,
            Machine.id.in_(machine_ids),
        ).all()
        allowed_machine_groups = {row.machine_type for row in machine_rows if row.machine_type}
    data = []
    for access in query.order_by(UserCompanyAccess.id.asc()).all():
        if access.user and access.user.is_active:
            access_group_name = access.machine_group.name if access.machine_group else None
            if machine_ids and access_group_name and allowed_machine_groups and access_group_name not in allowed_machine_groups:
                continue
            data.append(
                {
                    "id": access.user.id,
                    "display_name": access.user.display_name,
                    "work_id": access.user.employee_id,
                    "department_id": access.department_id or access.user.department_id,
                    "department_name": access.department.name if access.department else None,
                    "machine_group_id": access.machine_group_id or access.user.machine_group_id,
                    "machine_group_name": access.machine_group.name if access.machine_group else None,
                    "role": access.role,
                    "scope_mode": access.scope_mode,
                }
            )
    return jsonify({"success": True, "data": data})


@api_bp.get("/board-state")
def board_state():
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    compact = str(request.args.get("compact") or "").strip().lower() in {"1", "true", "yes", "on"}
    return jsonify({"success": True, "data": build_board_state(include_metadata=not compact)})


@api_bp.get("/operator-snapshot")
def operator_snapshot():
    started_at = time.perf_counter()
    access_started_at = time.perf_counter()
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    access_ms = (time.perf_counter() - access_started_at) * 1000

    user_started_at = time.perf_counter()
    current_user = get_authenticated_user()
    user_ms = (time.perf_counter() - user_started_at) * 1000

    company_started_at = time.perf_counter()
    company_id = get_current_company_id()
    company_ms = (time.perf_counter() - company_started_at) * 1000

    membership_started_at = time.perf_counter()
    membership = get_current_membership(user=current_user)
    membership_ms = (time.perf_counter() - membership_started_at) * 1000

    scope_started_at = time.perf_counter()
    scope = get_scope_filters(membership=membership)
    scope_ms = (time.perf_counter() - scope_started_at) * 1000

    include_radius = str(request.args.get("include_radius") or "").strip().lower() not in {"0", "false", "no", "off"}
    include_alerts = str(request.args.get("include_alerts") or "").strip().lower() not in {"0", "false", "no", "off"}

    service_metrics = {}
    service_started_at = time.perf_counter()
    payload = build_operator_snapshot(
        company_id=company_id,
        current_user=current_user,
        membership=membership,
        scope=scope,
        metrics=service_metrics,
        include_radius=include_radius,
        include_alerts=include_alerts,
    )
    service_ms = (time.perf_counter() - service_started_at) * 1000

    jsonify_started_at = time.perf_counter()
    response = jsonify({"success": True, "data": payload})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    jsonify_ms = (time.perf_counter() - jsonify_started_at) * 1000

    if current_app.config.get("ANDON_PERF_LOGS"):
        counts = service_metrics.get("counts") or {}
        current_app.logger.debug(
            "PERF operator_snapshot access_ms=%.1f user_ms=%.1f company_ms=%.1f membership_ms=%.1f scope_ms=%.1f "
            "service_ms=%.1f alert_query_ms=%s machine_query_ms=%s board_query_ms=%s created_notes_query_ms=%s "
            "serialize_ms=%s jsonify_ms=%.1f cache_lookup_ms=%s cache_store_ms=%s total_ms=%.1f cache=%s include_radius=%s include_alerts=%s "
            "active_alert_count=%s filtered_alert_count=%s visible_machine_count=%s company_id=%s user_id=%s",
            access_ms,
            user_ms,
            company_ms,
            membership_ms,
            scope_ms,
            service_ms,
            service_metrics.get("alert_query_ms"),
            service_metrics.get("machine_query_ms"),
            service_metrics.get("board_query_ms"),
            service_metrics.get("created_notes_query_ms"),
            service_metrics.get("serialize_ms"),
            jsonify_ms,
            service_metrics.get("cache_lookup_ms"),
            service_metrics.get("cache_store_ms"),
            (time.perf_counter() - started_at) * 1000,
            service_metrics.get("cache", "unknown"),
            include_radius,
            include_alerts,
            service_metrics.get("active_alert_count"),
            service_metrics.get("filtered_alert_count"),
            counts.get("machines"),
            company_id,
            getattr(current_user, "id", None),
        )
    return response


@api_bp.get("/operator-metadata")
def operator_metadata():
    started_at = time.perf_counter()
    access_started_at = time.perf_counter()
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    access_ms = (time.perf_counter() - access_started_at) * 1000

    user_started_at = time.perf_counter()
    current_user = get_authenticated_user()
    user_ms = (time.perf_counter() - user_started_at) * 1000

    company_started_at = time.perf_counter()
    company_id = get_current_company_id()
    company_ms = (time.perf_counter() - company_started_at) * 1000

    membership_started_at = time.perf_counter()
    membership = get_current_membership(user=current_user)
    membership_ms = (time.perf_counter() - membership_started_at) * 1000

    scope_started_at = time.perf_counter()
    scope = get_scope_filters(membership=membership)
    scope_ms = (time.perf_counter() - scope_started_at) * 1000

    service_metrics = {}
    service_started_at = time.perf_counter()
    departments_only = str(request.args.get("departments_only") or "").strip().lower() in {"1", "true", "yes", "on"}
    include_issue_groups = str(request.args.get("include_issue_groups") or "").strip().lower() not in {"0", "false", "no", "off"}
    include_users = str(request.args.get("include_users") or "").strip().lower() not in {"0", "false", "no", "off"}
    department_id = request.args.get("department_id", type=int)
    metadata_department_ids_override = [department_id] if department_id else None
    payload = build_operator_metadata(
        company_id=company_id,
        current_user=current_user,
        membership=membership,
        scope=scope,
        metrics=service_metrics,
        include_issue_groups=(not departments_only) and include_issue_groups,
        include_users=(not departments_only) and include_users,
        metadata_department_ids_override=metadata_department_ids_override,
    )
    service_ms = (time.perf_counter() - service_started_at) * 1000

    jsonify_started_at = time.perf_counter()
    response = jsonify({"success": True, "data": payload})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    jsonify_ms = (time.perf_counter() - jsonify_started_at) * 1000

    if current_app.config.get("ANDON_PERF_LOGS"):
        counts = service_metrics.get("counts") or {}
        current_app.logger.debug(
            "PERF operator_metadata access_ms=%.1f user_ms=%.1f company_ms=%.1f membership_ms=%.1f scope_ms=%.1f "
            "service_ms=%.1f jsonify_ms=%.1f total_ms=%.1f cache=%s cache_lookup_ms=%s dept_query_ms=%s issue_query_ms=%s "
            "category_query_ms=%s problem_query_ms=%s grouping_ms=%s category_count=%s problem_count=%s problem_cap_reached=%s "
            "user_query_ms=%s serialize_ms=%s cache_store_ms=%s company_id=%s user_id=%s role=%s departments=%s issue_groups=%s users=%s",
            access_ms,
            user_ms,
            company_ms,
            membership_ms,
            scope_ms,
            service_ms,
            jsonify_ms,
            (time.perf_counter() - started_at) * 1000,
            service_metrics.get("cache", "unknown"),
            service_metrics.get("cache_lookup_ms"),
            service_metrics.get("department_query_ms"),
            service_metrics.get("issue_query_ms"),
            service_metrics.get("category_query_ms"),
            service_metrics.get("problem_query_ms"),
            service_metrics.get("grouping_ms"),
            service_metrics.get("category_count"),
            service_metrics.get("problem_count"),
            service_metrics.get("problem_cap_reached"),
            service_metrics.get("user_query_ms"),
            service_metrics.get("serialize_ms"),
            service_metrics.get("cache_store_ms"),
            company_id,
            getattr(current_user, "id", None),
            getattr(membership, "role", None),
            counts.get("departments"),
            counts.get("issue_groups"),
            counts.get("users"),
        )
    return response


@api_bp.post("/alerts")
def api_create_alert():
    started_at = time.perf_counter()
    auth_started_at = time.perf_counter()
    _require_any_page_access(PAGE_OPERATOR)
    auth_ms = (time.perf_counter() - auth_started_at) * 1000
    payload_started_at = time.perf_counter()
    body = _payload()
    payload_ms = (time.perf_counter() - payload_started_at) * 1000
    current_app.logger.debug("API alert_create received payload=%s", body)
    try:
        service_metrics = {}
        write_started_at = time.perf_counter()
        result = create_alert(body, metrics=service_metrics)
        insert_or_update_ms = (time.perf_counter() - write_started_at) * 1000
        current_app.logger.debug(
            "API alert_create service returned type=%s payload=%s",
            type(result).__name__,
            body,
        )
        if isinstance(result, dict) and "created_alerts" in result:
            response_data = result
            created_alerts = result.get("created_alerts") or []
            existing_alerts = result.get("existing_alerts") or []
        else:
            created_alerts = result if isinstance(result, list) else [result]
            existing_alerts = []
            response_data = {
                "created_alerts": [
                    {"id": item.id, "company_id": item.company_id, "machine_id": item.machine_id, "status": item.status}
                    for item in created_alerts
                ],
                "existing_alerts": [],
                "warnings": [],
            }
        serialize_ms = 0.0
        current_app.logger.debug(
            "API alert_create prepared response created_ids=%s existing_ids=%s serialize_ms=%.1f",
            [item.get("id") if isinstance(item, dict) else item.id for item in created_alerts],
            [item.get("id") if isinstance(item, dict) else item.id for item in existing_alerts],
            serialize_ms,
        )
        service_metrics["payload_fetch_ms"] = serialize_ms
        if current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF alert_create auth_ms=%.1f payload_ms=%.1f insert_or_update_ms=%.1f serialize_ms=%.1f total_ms=%.1f alert_id=%s",
                auth_ms,
                payload_ms,
                insert_or_update_ms,
                serialize_ms,
                (time.perf_counter() - started_at) * 1000,
                (created_alerts[0].get("id") if created_alerts and isinstance(created_alerts[0], dict) else created_alerts[0].id) if created_alerts else None,
            )
        current_app.logger.debug(
            "API alert_create returning success created_ids=%s existing_ids=%s",
            [item.get("id") if isinstance(item, dict) else item.id for item in created_alerts],
            [item.get("id") if isinstance(item, dict) else item.id for item in existing_alerts],
        )
        status_code = 201 if created_alerts else 200
        return jsonify({"success": True, "data": response_data}), status_code
    except AlertServiceError as exc:
        return _error(str(exc), getattr(exc, "status_code", 400), getattr(exc, "data", None))
    except Exception:
        current_app.logger.exception("API alert_create unexpected failure payload=%s", body)
        return _error("Unable to create alert", 500)


@api_bp.post("/alerts/<int:alert_id>/toggle-machine-active")
def api_toggle_machine_from_alert(alert_id):
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    alert = get_alert(alert_id)
    alert.machine.is_active = not alert.machine.is_active
    db.session.commit()
    invalidate_cache("board_state", alert.company_id)
    invalidate_cache("report_summary", alert.company_id)
    invalidate_cache("report_machine_details", alert.company_id)
    invalidate_cache("report_machine_stats", alert.company_id)
    invalidate_cache("report_problem_details", alert.company_id)
    emit_machine_updated(alert.company_id, machine_id=alert.machine.id, action="toggle_from_alert")
    return jsonify({"success": True, "data": {"machine_id": alert.machine.id, "is_active": alert.machine.is_active}})


@api_bp.get("/alerts")
def api_list_alerts():
    started_at = time.perf_counter()
    auth_started_at = time.perf_counter()
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    auth_ms = (time.perf_counter() - auth_started_at) * 1000
    status = request.args.get("status")
    scope_started_at = time.perf_counter()
    company_id = get_current_company_id()
    membership = get_current_membership()
    scope = get_scope_filters(membership=membership)
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    scope_ms = (time.perf_counter() - scope_started_at) * 1000
    fetch_metrics = {}
    alerts = fetch_active_alert_payloads(
        company_id=company_id,
        status=status,
        machine_ids=machine_ids,
        department_ids=department_ids,
        role=getattr(membership, "role", None),
        metrics=fetch_metrics,
    )
    if current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF alerts_list auth_ms=%.1f scope_ms=%.1f cache_lookup_ms=%s alert_base_query_ms=%s machine_lookup_ms=0.0 "
            "department_lookup_ms=0.0 issue_lookup_ms=0.0 user_lookup_ms=0.0 notes_lookup_ms=0.0 serialize_ms=%s cache_store_ms=%s "
            "total_ms=%.1f alert_count=%s visible_machine_count=%s cache=%s",
            auth_ms,
            scope_ms,
            fetch_metrics.get("cache_lookup_ms"),
            fetch_metrics.get("alert_base_query_ms", 0.0),
            fetch_metrics.get("serialize_ms", 0.0),
            fetch_metrics.get("cache_store_ms", 0.0),
            (time.perf_counter() - started_at) * 1000,
            len(alerts),
            len(machine_ids),
            fetch_metrics.get("cache", "unknown"),
        )
    return jsonify({"success": True, "data": alerts})


@api_bp.get("/alerts/<int:alert_id>")
def api_get_alert(alert_id):
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    try:
        alert = get_alert(alert_id)
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 404)


@api_bp.post("/alerts/<int:alert_id>/acknowledge")
def api_acknowledge_alert(alert_id):
    started_at = time.perf_counter()
    auth_started_at = time.perf_counter()
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    auth_ms = (time.perf_counter() - auth_started_at) * 1000
    payload_started_at = time.perf_counter()
    body = _payload()
    payload_ms = (time.perf_counter() - payload_started_at) * 1000
    try:
        update_started_at = time.perf_counter()
        service_metrics = {}
        alert = acknowledge_alert(alert_id, body, metrics=service_metrics)
        insert_or_update_ms = (time.perf_counter() - update_started_at) * 1000
        serialize_ms = 0.0
        alert_payload = {"id": alert.id, "status": alert.status, "company_id": alert.company_id, "machine_id": alert.machine_id}
        if current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF alert_acknowledge auth_ms=%.1f payload_ms=%.1f insert_or_update_ms=%.1f serialize_ms=%.1f "
                "alert_lookup_ms=%.1f user_lookup_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f total_ms=%.1f alert_id=%s",
                auth_ms,
                payload_ms,
                insert_or_update_ms,
                serialize_ms,
                service_metrics.get("alert_lookup_ms", 0.0),
                service_metrics.get("user_lookup_ms", 0.0),
                service_metrics.get("db_commit_ms", 0.0),
                service_metrics.get("cache_invalidate_ms", 0.0),
                service_metrics.get("socket_emit_ms", 0.0),
                (time.perf_counter() - started_at) * 1000,
                alert.id,
            )
        return jsonify({"success": True, "data": alert_payload})
    except AlertServiceError as exc:
        return _error(str(exc), 400)
    except Exception:
        current_app.logger.exception("API alert_acknowledge unexpected failure alert_id=%s payload=%s", alert_id, body)
        return _error("Unable to acknowledge alert", 500)


@api_bp.get("/pager/alerts/active")
def api_pager_active_alerts():
    started_at = time.perf_counter()
    pager = getattr(g, "api_authenticated_pager_device", None)
    auth_ms = float(getattr(g, "api_pager_auth_ms", 0.0) or 0.0)
    if pager is None:
        auth_started_at = time.perf_counter()
        pager = get_authenticated_pager_device(update_last_seen=False)
        auth_ms = (time.perf_counter() - auth_started_at) * 1000
    if pager is None:
        abort(403)

    cache_key = ("pager_active_alerts", pager.company_id, pager.department_id)
    stale_cache_key = ("pager_active_alerts_stale", pager.company_id, pager.department_id)
    cached = get_cached(cache_key)
    if cached is not None:
        if current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF pager_active_alerts auth_ms=%.1f cache=hit query_ms=0.0 serialize_ms=0.0 count=%s",
                auth_ms,
                len(cached) if isinstance(cached, list) else -1,
            )
        return jsonify({"success": True, "data": cached})

    stale_cached = get_cached(stale_cache_key)
    fetch_metrics = {}
    try:
        payload = fetch_active_alert_payloads(
            company_id=pager.company_id,
            status="active",
            department_ids=[pager.department_id],
            pager_minimal=True,
            use_cache=False,
            metrics=fetch_metrics,
        )
    except OperationalError:
        db.session.rollback()
        if stale_cached is not None:
            if current_app.config.get("ANDON_PERF_LOGS"):
                current_app.logger.debug(
                    "PERF pager_active_alerts auth_ms=%.1f cache=stale_fallback query_ms=0.0 serialize_ms=0.0 count=%s",
                    auth_ms,
                    len(stale_cached) if isinstance(stale_cached, list) else -1,
                )
            return jsonify({"success": True, "data": stale_cached})
        payload = []
    set_cached(cache_key, payload, ttl_seconds=PAGER_ACTIVE_ALERTS_CACHE_TTL_SECONDS)
    set_cached(stale_cache_key, payload, ttl_seconds=PAGER_ACTIVE_ALERTS_STALE_TTL_SECONDS)
    if current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF pager_active_alerts auth_ms=%.1f cache=miss query_ms=%.1f serialize_ms=%.1f count=%s",
            auth_ms,
            fetch_metrics.get("alert_base_query_ms", 0.0),
            fetch_metrics.get("serialize_ms", 0.0),
            len(payload),
        )
    return jsonify({"success": True, "data": payload})


@api_bp.post("/pager/alerts/<int:alert_id>/acknowledge")
def api_pager_acknowledge_alert(alert_id: int):
    started_at = time.perf_counter()
    pager = get_authenticated_pager_device()
    if pager is None:
        abort(403)

    payload = _payload()
    payload.setdefault("note", "Acknowledged on department pager")
    service_metrics = {}
    try:
        alert = acknowledge_alert_scoped(
            alert_id,
            payload,
            metrics=service_metrics,
            company_id=pager.company_id,
            department_id=pager.department_id,
            responder_name_fallback=pager.name,
            event_message="Alert acknowledged from pager",
            event_metadata={"source": "pager_device", "pager_device_id": pager.id, "pager_name": pager.name},
        )
    except AlertServiceError as exc:
        return _error(str(exc), exc.status_code)
    fetch_started_at = time.perf_counter()
    alert_payload = fetch_alert_payload_by_id(alert.id, company_id=alert.company_id) or {"id": alert.id, "status": alert.status}
    service_metrics["payload_fetch_ms"] = (time.perf_counter() - fetch_started_at) * 1000
    if current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF pager_alert_acknowledge total_ms=%.1f alert_lookup_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f payload_fetch_ms=%.1f alert_id=%s",
            (time.perf_counter() - started_at) * 1000,
            service_metrics.get("alert_lookup_ms", 0.0),
            service_metrics.get("db_commit_ms", 0.0),
            service_metrics.get("cache_invalidate_ms", 0.0),
            service_metrics.get("socket_emit_ms", 0.0),
            service_metrics.get("payload_fetch_ms", 0.0),
            alert.id,
        )
    return jsonify({"success": True, "data": alert_payload})


@api_bp.post("/pager/alerts/<int:alert_id>/resolve")
def api_pager_resolve_alert(alert_id: int):
    started_at = time.perf_counter()
    pager = get_authenticated_pager_device()
    if pager is None:
        abort(403)

    payload = _payload()
    payload.setdefault("note", "Resolved on department pager")
    service_metrics = {}
    try:
        alert = resolve_alert_scoped(
            alert_id,
            payload,
            metrics=service_metrics,
            company_id=pager.company_id,
            department_id=pager.department_id,
            responder_name_fallback=pager.name,
            event_message="Alert resolved from pager",
            event_metadata={"source": "pager_device", "pager_device_id": pager.id, "pager_name": pager.name},
        )
    except AlertServiceError as exc:
        return _error(str(exc), exc.status_code)
    fetch_started_at = time.perf_counter()
    alert_payload = fetch_alert_payload_by_id(alert.id, company_id=alert.company_id) or {"id": alert.id, "status": alert.status}
    service_metrics["payload_fetch_ms"] = (time.perf_counter() - fetch_started_at) * 1000
    if current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF pager_alert_resolve total_ms=%.1f alert_lookup_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f payload_fetch_ms=%.1f alert_id=%s",
            (time.perf_counter() - started_at) * 1000,
            service_metrics.get("alert_lookup_ms", 0.0),
            service_metrics.get("db_commit_ms", 0.0),
            service_metrics.get("cache_invalidate_ms", 0.0),
            service_metrics.get("socket_emit_ms", 0.0),
            service_metrics.get("payload_fetch_ms", 0.0),
            alert.id,
        )
    return jsonify({"success": True, "data": alert_payload})


@api_bp.post("/alerts/<int:alert_id>/arrive")
def api_arrive_alert(alert_id):
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    try:
        alert = mark_arrived(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/resolve")
def api_resolve_alert(alert_id):
    started_at = time.perf_counter()
    auth_started_at = time.perf_counter()
    _require_any_page_access(PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT)
    auth_ms = (time.perf_counter() - auth_started_at) * 1000
    payload_started_at = time.perf_counter()
    body = _payload()
    payload_ms = (time.perf_counter() - payload_started_at) * 1000
    if current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF api_resolve_alert entry alert_id=%s payload_keys=%s user_id=%s company_id=%s",
            alert_id,
            sorted(body.keys()),
            getattr(get_authenticated_user(), "id", None),
            get_current_company_id(),
        )
    try:
        update_started_at = time.perf_counter()
        service_metrics = {}
        alert = resolve_alert(alert_id, body, metrics=service_metrics)
        insert_or_update_ms = (time.perf_counter() - update_started_at) * 1000
        serialize_ms = 0.0
        alert_payload = {"id": alert.id, "status": alert.status, "company_id": alert.company_id, "machine_id": alert.machine_id}
        if current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF alert_resolve auth_ms=%.1f payload_ms=%.1f insert_or_update_ms=%.1f serialize_ms=%.1f "
                "alert_lookup_ms=%.1f user_lookup_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f total_ms=%.1f alert_id=%s",
                auth_ms,
                payload_ms,
                insert_or_update_ms,
                serialize_ms,
                service_metrics.get("alert_lookup_ms", 0.0),
                service_metrics.get("user_lookup_ms", 0.0),
                service_metrics.get("db_commit_ms", 0.0),
                service_metrics.get("cache_invalidate_ms", 0.0),
                service_metrics.get("socket_emit_ms", 0.0),
                (time.perf_counter() - started_at) * 1000,
                alert.id,
            )
        return jsonify({"success": True, "data": alert_payload})
    except AlertServiceError as exc:
        return _error(str(exc), 400)
    except Exception:
        current_app.logger.exception("API alert_resolve unexpected failure alert_id=%s payload=%s", alert_id, body)
        return _error("Unable to close alert", 500)


@api_bp.post("/alerts/<int:alert_id>/cancel")
def api_cancel_alert(alert_id):
    started_at = time.perf_counter()
    auth_started_at = time.perf_counter()
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    auth_ms = (time.perf_counter() - auth_started_at) * 1000
    payload_started_at = time.perf_counter()
    body = _payload()
    payload_ms = (time.perf_counter() - payload_started_at) * 1000
    try:
        update_started_at = time.perf_counter()
        service_metrics = {}
        alert = cancel_alert(alert_id, body, metrics=service_metrics)
        insert_or_update_ms = (time.perf_counter() - update_started_at) * 1000
        serialize_started_at = time.perf_counter()
        alert_payload = fetch_alert_payload_by_id(alert.id, company_id=alert.company_id) or {"id": alert.id, "status": alert.status}
        serialize_ms = (time.perf_counter() - serialize_started_at) * 1000
        if current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF alert_cancel auth_ms=%.1f payload_ms=%.1f insert_or_update_ms=%.1f serialize_ms=%.1f "
                "alert_lookup_ms=%.1f user_lookup_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f total_ms=%.1f alert_id=%s",
                auth_ms,
                payload_ms,
                insert_or_update_ms,
                serialize_ms,
                service_metrics.get("alert_lookup_ms", 0.0),
                service_metrics.get("user_lookup_ms", 0.0),
                service_metrics.get("db_commit_ms", 0.0),
                service_metrics.get("cache_invalidate_ms", 0.0),
                service_metrics.get("socket_emit_ms", 0.0),
                (time.perf_counter() - started_at) * 1000,
                alert.id,
            )
        return jsonify({"success": True, "data": alert_payload})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/note")
def api_add_note(alert_id):
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    try:
        alert = add_note(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.get("/reports/summary")
def api_report_summary():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_report_summary(_filters())})


@api_bp.get("/reports/machine-details")
def api_report_machine_details():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_machine_details(_filters())})


@api_bp.get("/reports/machine-stats")
def api_report_machine_stats():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_machine_stats(_filters())})


@api_bp.get("/reports/problem-details")
def api_report_problem_details():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_problem_details(_filters())})


@api_bp.get("/reports/by-machine")
def api_report_by_machine():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_by_machine(_filters())})


@api_bp.get("/reports/by-department")
def api_report_by_department():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_by_department(_filters())})


@api_bp.get("/reports/by-problem")
def api_report_by_problem():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_by_problem(_filters())})


@api_bp.get("/reports/calls-per-hour")
def api_report_calls_per_hour():
    _require_any_page_access(PAGE_REPORTS, PAGE_MANAGEMENT)
    return jsonify({"success": True, "data": build_calls_per_hour(_filters())})


@api_bp.post("/escalations/check")
def api_check_escalations():
    _require_any_page_access(PAGE_MANAGEMENT)
    if not current_app.config.get("ESCALATION_INLINE_CHECKS_ENABLED", True):
        current_app.logger.warning("ESCALATION inline_checks_disabled remote=%s", request.remote_addr)
        return _error("Inline escalation checks are disabled in this environment", 503)
    started_at = time.perf_counter()
    escalated = check_escalations()
    current_app.logger.info(
        "ESCALATION inline_check completed count=%s duration_ms=%.1f remote=%s",
        len(escalated),
        (time.perf_counter() - started_at) * 1000,
        request.remote_addr,
    )
    return jsonify({"success": True, "data": escalated})


@api_bp.post("/machines/<int:machine_id>/toggle-active")
def api_toggle_machine_active(machine_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    company_id = get_current_company_id()
    machine = Machine.query.filter_by(id=machine_id, company_id=company_id).one_or_none() if company_id else Machine.query.get_or_404(machine_id)
    if machine is None:
        return _error("Machine not found", 404)
    payload = _payload()
    desired = payload.get("is_active")
    if desired is None:
        machine.is_active = not machine.is_active
    else:
        machine.is_active = bool(desired)
    db.session.commit()
    invalidate_cache(company_id=company_id)
    emit_machine_updated(company_id, machine_id=machine.id, action="toggle_active")
    return jsonify({"success": True, "data": machine.to_dict()})


@api_bp.post("/machine-types/<string:machine_type>/toggle-active")
def api_toggle_machine_type_active(machine_type):
    _require_any_page_access(PAGE_MANAGEMENT)
    payload = _payload()
    desired = payload.get("is_active")
    company_id = get_current_company_id()
    machines_query = Machine.query.filter(Machine.machine_type == machine_type)
    if company_id:
        machines_query = machines_query.filter(Machine.company_id == company_id)
    machines = machines_query.all()
    if desired is None:
        target_value = not all(machine.is_active for machine in machines)
    else:
        target_value = bool(desired)
    for machine in machines:
        machine.is_active = target_value
    db.session.commit()
    invalidate_cache(company_id=company_id)
    emit_machine_updated(company_id, action="toggle_machine_type")
    return jsonify({"success": True, "data": {"machine_type": machine_type, "is_active": target_value, "count": len(machines)}})


@api_bp.get("/preferences/<string:page_key>")
def api_get_preference(page_key):
    return jsonify({"success": True, "data": get_view_preference(page_key)})


@api_bp.post("/preferences/<string:page_key>")
def api_save_preference(page_key):
    payload = request.get_json(silent=True) or {}
    return jsonify({"success": True, "data": save_view_preference(page_key, payload)})


@api_bp.get("/boards")
def api_list_boards():
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    user = get_authenticated_user()
    company_id = get_current_company_id()
    boards = (
        UserBoard.query.options(
            joinedload(UserBoard.items).noload(UserBoardItem.board),
            noload(UserBoard.user),
            noload(UserBoard.company),
        )
        .filter_by(user_id=user.id, company_id=company_id)
        .order_by(UserBoard.last_opened_at.desc().nullslast(), UserBoard.updated_at.desc())
        .all()
    )
    active_board = boards[0] if boards else None
    return jsonify({
        "success": True,
        "data": {
            "boards": [board.to_dict() for board in boards],
            "active_board_id": active_board.id if active_board else None,
        },
    })


@api_bp.post("/boards")
def api_create_board():
    _require_any_page_access(PAGE_MANAGEMENT)
    user = get_authenticated_user()
    company_id = get_current_company_id()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip() or "New Board"
    board = UserBoard(
        user_id=user.id,
        company_id=company_id,
        name=name,
        show_performance=bool(payload.get("show_performance", True)),
        show_recent_history=bool(payload.get("show_recent_history", True)),
        show_radius=bool(payload.get("show_radius", True)),
        last_opened_at=datetime.now(timezone.utc),
    )
    db.session.add(board)
    db.session.flush()
    seen_machine_ids = set()
    machine_ids = payload.get("machine_ids") or []
    if isinstance(machine_ids, list):
        position = 0
        for raw_machine_id in machine_ids:
            try:
                machine_id = int(raw_machine_id)
            except (TypeError, ValueError):
                continue
            if machine_id in seen_machine_ids:
                continue
            machine = _get_scoped_machine(machine_id)
            if machine is None:
                continue
            seen_machine_ids.add(machine_id)
            db.session.add(UserBoardItem(board_id=board.id, machine_id=machine_id, position=position))
            position += 1
    db.session.commit()
    board = _get_user_board(board.id)
    return jsonify({"success": True, "data": board.to_dict()}), 201


@api_bp.delete("/boards/<int:board_id>")
def api_delete_board(board_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    db.session.delete(board)
    db.session.commit()
    return jsonify({"success": True, "data": {"board_id": board_id}})


@api_bp.post("/boards/<int:board_id>/activate")
def api_activate_board(board_id):
    _require_any_page_access(PAGE_BOARD, PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"success": True, "data": board.to_dict()})


@api_bp.patch("/boards/<int:board_id>")
def api_update_board(board_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if name:
        board.name = name
    for field in ("show_performance", "show_recent_history", "show_radius"):
        if field in payload:
            setattr(board, field, bool(payload.get(field)))
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"success": True, "data": board.to_dict()})


@api_bp.post("/boards/<int:board_id>/items")
def api_add_board_item(board_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    payload = request.get_json(silent=True) or {}
    machine_id = payload.get("machine_id")
    if machine_id is None:
        return _error("machine_id is required", 400)
    machine = _get_scoped_machine(machine_id)
    if machine is None:
        return _error("Machine not found", 404)
    existing = UserBoardItem.query.filter_by(board_id=board.id, machine_id=machine.id).one_or_none()
    if existing is not None:
        board.last_opened_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"success": True, "data": board.to_dict()})
    position = len(board.items)
    db.session.add(UserBoardItem(board_id=board.id, machine_id=machine.id, position=position))
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    board = _get_user_board(board.id)
    return jsonify({"success": True, "data": board.to_dict()}), 201


@api_bp.post("/boards/<int:board_id>/bulk-add")
def api_bulk_add_board_items(board_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    payload = request.get_json(silent=True) or {}
    source_type = str(payload.get("source_type") or "").strip()
    source_value = str(payload.get("source_value") or "").strip()
    machine_ids = _resolve_bulk_machine_ids(source_type, source_value)
    existing_machine_ids = {item.machine_id for item in board.items}
    position = len(board.items)
    for machine_id in machine_ids:
        if machine_id in existing_machine_ids:
            continue
        db.session.add(UserBoardItem(board_id=board.id, machine_id=machine_id, position=position))
        position += 1
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    board = _get_user_board(board.id)
    return jsonify({"success": True, "data": board.to_dict()})


@api_bp.delete("/boards/<int:board_id>/items/<int:item_id>")
def api_delete_board_item(board_id, item_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    item = UserBoardItem.query.filter_by(id=item_id, board_id=board.id).one_or_none()
    if item is None:
        return _error("Board item not found", 404)
    db.session.delete(item)
    db.session.flush()
    _normalize_board_item_positions(board.id)
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    board = _get_user_board(board.id)
    return jsonify({"success": True, "data": board.to_dict()})


@api_bp.patch("/boards/<int:board_id>/items/reorder")
def api_reorder_board_items(board_id):
    _require_any_page_access(PAGE_MANAGEMENT)
    board = _get_user_board(board_id)
    payload = request.get_json(silent=True) or {}
    item_ids = payload.get("item_ids") or []
    items_by_id = {item.id: item for item in board.items}
    for position, item_id in enumerate(item_ids):
        item = items_by_id.get(int(item_id))
        if item is not None:
            item.position = position
    board.last_opened_at = datetime.now(timezone.utc)
    db.session.commit()
    board = _get_user_board(board.id)
    return jsonify({"success": True, "data": board.to_dict()})


def _payload():
    payload = request.get_json(silent=True) or {}
    payload.update(request.form.to_dict(flat=True))
    for key in ["machine_id", "department_id", "issue_category_id", "issue_problem_id", "operator_user_id", "responder_user_id", "priority"]:
        if key in payload and payload[key] not in [None, ""]:
            try:
                payload[key] = int(payload[key])
            except (TypeError, ValueError):
                pass
    return payload


def _filters():
    return {
        "start": request.args.get("start"),
        "end": request.args.get("end"),
        "department_id": request.args.get("department_id", type=int),
        "machine_id": request.args.get("machine_id", type=int),
        "machine_group": request.args.get("machine_group"),
        "issue_category_id": request.args.get("issue_category_id", type=int),
        "issue_problem_id": request.args.get("issue_problem_id", type=int),
    }


def _error(message, code, extra=None):
    error = {"message": message}
    if extra:
        error.update(extra)
    return jsonify({"success": False, "error": error}), code


def _get_user_board(board_id: int) -> UserBoard:
    user = get_authenticated_user()
    company_id = get_current_company_id()
    board = (
        UserBoard.query.options(
            joinedload(UserBoard.items)
            .load_only(UserBoardItem.id, UserBoardItem.board_id, UserBoardItem.machine_id, UserBoardItem.position, UserBoardItem.created_at)
            .joinedload(UserBoardItem.machine)
            .load_only(Machine.id, Machine.name, Machine.machine_type),
            noload(UserBoard.user),
            noload(UserBoard.company),
        )
        .filter_by(id=board_id, user_id=user.id, company_id=company_id)
        .one_or_none()
    )
    if board is None:
        abort(404)
    return board


def _get_scoped_machine(machine_id: int | str):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = Machine.query.options(noload("*")).filter_by(id=int(machine_id), company_id=company_id)
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    if machine_ids:
        query = query.filter(Machine.id.in_(machine_ids))
    if department_ids:
        query = query.filter(Machine.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(Machine.machine_type.in_(machine_group_names))
    return query.one_or_none()


def _resolve_bulk_machine_ids(source_type: str, source_value: str) -> list[int]:
    company_id = get_current_company_id()
    scope = get_scope_filters()
    query = Machine.query.options(load_only(Machine.id, Machine.name, Machine.machine_type, Machine.department_id), noload("*")).filter(Machine.company_id == company_id)
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    if machine_ids:
        query = query.filter(Machine.id.in_(machine_ids))
    if department_ids:
        query = query.filter(Machine.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(Machine.machine_type.in_(machine_group_names))
    if source_type == "department":
        query = query.join(Machine.department).filter(Department.name == source_value)
    elif source_type == "machine_group":
        query = query.filter(Machine.machine_type == source_value)
    else:
        return []
    return [machine.id for machine in query.order_by(Machine.name.asc()).all()]


def _normalize_board_item_positions(board_id: int) -> None:
    items = UserBoardItem.query.filter_by(board_id=board_id).order_by(UserBoardItem.position.asc(), UserBoardItem.id.asc()).all()
    for position, item in enumerate(items):
        item.position = position
