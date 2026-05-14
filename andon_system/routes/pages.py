from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..company_context import get_current_company, set_current_company_slug
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import USER_ROLES, User
from ..security import (
    is_admin_authenticated,
    is_safe_redirect_target,
    set_admin_authenticated,
    validate_admin_password,
)
from ..services.escalation_service import FIXED_ESCALATION_PHASES, ensure_fixed_escalation_rules

pages_bp = Blueprint("pages", __name__)
MANAGEMENT_TIMEZONE = ZoneInfo("America/Chicago")


def _management_shift_window(now: datetime | None = None):
    if now is None:
        current = datetime.now(MANAGEMENT_TIMEZONE)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=MANAGEMENT_TIMEZONE)
    else:
        current = now.astimezone(MANAGEMENT_TIMEZONE)
    if current.hour >= 6 and current.hour < 18:
        start = current.replace(hour=6, minute=0, second=0, microsecond=0)
        end = current.replace(hour=18, minute=0, second=0, microsecond=0)
        label = "Day shift"
    elif current.hour >= 18:
        start = current.replace(hour=18, minute=0, second=0, microsecond=0)
        end = (current + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        label = "Night shift"
    else:
        start = (current - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        end = current.replace(hour=6, minute=0, second=0, microsecond=0)
        label = "Night shift"

    def _display_time(value: datetime) -> str:
        return value.strftime("%I:%M %p").lstrip("0")

    return {
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": end.strftime("%Y-%m-%dT%H:%M"),
        "label": f"{label} ({_display_time(start)} - {_display_time(end)})",
    }


@pages_bp.route("/andon")
def landing_page():
    return redirect(url_for("pages.operator_page"))


@pages_bp.post("/andon/company/select")
def select_company():
    slug = request.form.get("company_slug")
    set_current_company_slug(slug)
    return redirect(request.referrer or url_for("pages.operator_page"))


@pages_bp.post("/andon/admin/login")
def admin_login():
    password = request.form.get("password")
    next_url = request.form.get("next") or url_for("pages.admin_page")
    if not is_safe_redirect_target(next_url):
        next_url = url_for("pages.admin_page")
    if not validate_admin_password(password):
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "message": "Invalid admin password"}), 403
        flash("Invalid admin password", "warning")
        return redirect(url_for("pages.operator_page"))
    set_admin_authenticated(True)
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "redirect_to": next_url})
    return redirect(next_url)


@pages_bp.post("/andon/admin/logout")
def admin_logout():
    set_admin_authenticated(False)
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "redirect_to": url_for("pages.operator_page")})
    return redirect(url_for("pages.operator_page"))


@pages_bp.route("/andon/operator")
def operator_page():
    company = get_current_company()
    return render_template(
        "andon/operator.html",
        current_company=company,
    )


@pages_bp.route("/andon/management")
def management_page():
    company = get_current_company()
    shift_window = _management_shift_window()
    return render_template(
        "andon/management.html",
        current_company=company,
        management_shift_start=shift_window["start"],
        management_shift_end=shift_window["end"],
        management_shift_label=shift_window["label"],
    )


@pages_bp.route("/andon/board")
def board_page():
    return render_template("andon/board.html", current_company=get_current_company())


@pages_bp.route("/andon/reports")
def reports_page():
    company = get_current_company()
    machine_groups = [
        row.machine_type
        for row in (
            Machine.query.with_entities(Machine.machine_type)
            .filter(Machine.company_id == company.id, Machine.machine_type.isnot(None), Machine.machine_type != "")
            .distinct()
            .order_by(Machine.machine_type.asc())
            .all()
            if company
            else []
        )
    ]
    return render_template(
        "andon/reports.html",
        current_company=company,
        machine_groups=machine_groups,
    )


@pages_bp.route("/andon/admin")
def admin_page():
    if not is_admin_authenticated():
        flash("Admin authentication required", "warning")
        return redirect(url_for("pages.operator_page"))
    company = get_current_company()
    company_id = company.id if company else None
    escalation_rules_map = ensure_fixed_escalation_rules()
    machines = Machine.query.filter_by(company_id=company_id).order_by(Machine.machine_type.asc().nullslast(), Machine.name.asc()).all() if company_id else []
    users = User.query.filter_by(company_id=company_id).order_by(User.display_name.asc()).all() if company_id else []
    machine_groups = []
    for group in MachineGroup.query.filter_by(company_id=company_id).order_by(MachineGroup.name.asc()).all() if company_id else []:
        grouped_machines = [machine for machine in machines if machine.machine_type == group.name]
        machine_groups.append(
            {
                "id": group.id,
                "name": group.name,
                "is_active": group.is_active,
                "machine_count": len(grouped_machines),
            }
        )
    departments = Department.query.filter_by(company_id=company_id).order_by(Department.name.asc()).all() if company_id else []
    problems = (
        IssueProblem.query.join(IssueCategory)
        .join(Department)
        .filter(IssueProblem.company_id == company_id)
        .order_by(Department.name.asc(), IssueProblem.name.asc())
        .all()
        if company_id
        else []
    )
    issue_groups = []
    for department in departments:
        department_problems = [
            problem
            for problem in problems
            if problem.category and problem.category.department_id == department.id
        ]
        issue_groups.append(
            {
                "department": department,
                "problems": department_problems,
            }
        )
    return render_template(
        "andon/admin.html",
        departments=departments,
        machines=machines,
        users=users,
        machine_groups=machine_groups,
        issue_groups=issue_groups,
        escalation_rules=[escalation_rules_map[level] for level in sorted(escalation_rules_map.keys())],
        escalation_phase_labels=FIXED_ESCALATION_PHASES,
        user_roles=USER_ROLES,
        current_company=company,
    )
