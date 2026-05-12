from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..company_context import get_current_company_id
from ..models.alert import (
    ALERT_STATUS_ACKNOWLEDGED,
    ALERT_STATUS_ARRIVED,
    ALERT_STATUS_CANCELLED,
    ALERT_STATUS_OPEN,
    ALERT_STATUS_RESOLVED,
)
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.user import User
from ..services.board_service import build_board_state, build_operator_metadata, build_operator_snapshot
from ..extensions import db
from ..services.alert_service import (
    AlertServiceError,
    acknowledge_alert,
    add_note,
    cancel_alert,
    create_alert,
    get_alert,
    list_active_alerts,
    mark_arrived,
    resolve_alert,
)
from ..services.reporting_service import (
    build_by_department,
    build_by_machine,
    build_by_problem,
    build_calls_per_hour,
    build_machine_details,
    build_report_summary,
    build_problem_details,
)
from ..services.escalation_service import check_escalations
from ..services.cache_service import invalidate_cache
from ..services.realtime_service import emit_machine_updated

api_bp = Blueprint("api", __name__, url_prefix="/api/andon")


@api_bp.get("/machines")
def machines():
    company_id = get_current_company_id()
    query = Machine.query.filter_by(is_active=True)
    if company_id:
        query = query.filter(Machine.company_id == company_id)
    return jsonify({"success": True, "data": [machine.to_dict() for machine in query.order_by(Machine.name.asc()).all()]})


@api_bp.get("/departments")
def departments():
    company_id = get_current_company_id()
    query = Department.query.filter_by(is_active=True)
    if company_id:
        query = query.filter(Department.company_id == company_id)
    return jsonify({"success": True, "data": [department.to_dict() for department in query.order_by(Department.name.asc()).all()]})


@api_bp.get("/issue-categories")
def issue_categories():
    company_id = get_current_company_id()
    query = IssueCategory.query.filter_by(is_active=True)
    if company_id:
        query = query.filter(IssueCategory.company_id == company_id)
    return jsonify({"success": True, "data": [category.to_dict() for category in query.order_by(IssueCategory.name.asc()).all()]})


@api_bp.get("/issue-problems")
def issue_problems():
    category_id = request.args.get("category_id", type=int)
    query = IssueProblem.query.filter_by(is_active=True)
    company_id = get_current_company_id()
    if company_id:
        query = query.filter(IssueProblem.company_id == company_id)
    if category_id:
        query = query.filter_by(category_id=category_id)
    data = [problem.to_dict() for problem in query.order_by(IssueProblem.name.asc()).all()]
    return jsonify({"success": True, "data": data})


@api_bp.get("/users")
def users():
    company_id = get_current_company_id()
    query = User.query.filter_by(is_active=True)
    if company_id:
        query = query.filter(User.company_id == company_id)
    data = [user.to_dict() for user in query.order_by(User.display_name.asc()).all()]
    return jsonify({"success": True, "data": data})


@api_bp.get("/board-state")
def board_state():
    return jsonify({"success": True, "data": build_board_state()})


@api_bp.get("/operator-snapshot")
def operator_snapshot():
    return jsonify({"success": True, "data": build_operator_snapshot()})


@api_bp.get("/operator-metadata")
def operator_metadata():
    return jsonify({"success": True, "data": build_operator_metadata()})


@api_bp.post("/alerts")
def api_create_alert():
    try:
        alert = create_alert(_payload())
        return jsonify({"success": True, "data": alert.to_dict()}), 201
    except AlertServiceError as exc:
        return _error(str(exc), getattr(exc, "status_code", 400), getattr(exc, "data", None))


@api_bp.post("/alerts/<int:alert_id>/toggle-machine-active")
def api_toggle_machine_from_alert(alert_id):
    alert = get_alert(alert_id)
    alert.machine.is_active = not alert.machine.is_active
    db.session.commit()
    invalidate_cache("board_state", alert.company_id)
    invalidate_cache("report_summary", alert.company_id)
    invalidate_cache("report_machine_details", alert.company_id)
    invalidate_cache("report_problem_details", alert.company_id)
    emit_machine_updated(alert.company_id, machine_id=alert.machine.id, action="toggle_from_alert")
    return jsonify({"success": True, "data": {"machine_id": alert.machine.id, "is_active": alert.machine.is_active}})


@api_bp.get("/alerts")
def api_list_alerts():
    status = request.args.get("status")
    alerts = list_active_alerts(status=status)
    return jsonify({"success": True, "data": [alert.to_dict() for alert in alerts]})


@api_bp.get("/alerts/<int:alert_id>")
def api_get_alert(alert_id):
    try:
        alert = get_alert(alert_id)
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 404)


@api_bp.post("/alerts/<int:alert_id>/acknowledge")
def api_acknowledge_alert(alert_id):
    try:
        alert = acknowledge_alert(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/arrive")
def api_arrive_alert(alert_id):
    try:
        alert = mark_arrived(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/resolve")
def api_resolve_alert(alert_id):
    try:
        alert = resolve_alert(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/cancel")
def api_cancel_alert(alert_id):
    try:
        alert = cancel_alert(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.post("/alerts/<int:alert_id>/note")
def api_add_note(alert_id):
    try:
        alert = add_note(alert_id, _payload())
        return jsonify({"success": True, "data": alert.to_dict()})
    except AlertServiceError as exc:
        return _error(str(exc), 400)


@api_bp.get("/reports/summary")
def api_report_summary():
    return jsonify({"success": True, "data": build_report_summary(_filters())})


@api_bp.get("/reports/machine-details")
def api_report_machine_details():
    return jsonify({"success": True, "data": build_machine_details(_filters())})


@api_bp.get("/reports/problem-details")
def api_report_problem_details():
    return jsonify({"success": True, "data": build_problem_details(_filters())})


@api_bp.get("/reports/by-machine")
def api_report_by_machine():
    return jsonify({"success": True, "data": build_by_machine(_filters())})


@api_bp.get("/reports/by-department")
def api_report_by_department():
    return jsonify({"success": True, "data": build_by_department(_filters())})


@api_bp.get("/reports/by-problem")
def api_report_by_problem():
    return jsonify({"success": True, "data": build_by_problem(_filters())})


@api_bp.get("/reports/calls-per-hour")
def api_report_calls_per_hour():
    return jsonify({"success": True, "data": build_calls_per_hour(_filters())})


@api_bp.post("/escalations/check")
def api_check_escalations():
    escalated = check_escalations()
    return jsonify({"success": True, "data": escalated})


@api_bp.post("/machines/<int:machine_id>/toggle-active")
def api_toggle_machine_active(machine_id):
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
