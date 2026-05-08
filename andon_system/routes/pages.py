from flask import Blueprint, redirect, render_template, request, url_for

from ..company_context import get_current_company, set_current_company_slug
from ..models.department import Department
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import User
from ..services.escalation_service import FIXED_ESCALATION_PHASES, ensure_fixed_escalation_rules

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/andon")
def landing_page():
    return redirect(url_for("pages.operator_page"))


@pages_bp.post("/andon/company/select")
def select_company():
    slug = request.form.get("company_slug")
    set_current_company_slug(slug)
    return redirect(request.referrer or url_for("pages.operator_page"))


@pages_bp.route("/andon/operator")
def operator_page():
    company = get_current_company()
    return render_template(
        "andon/operator.html",
        current_company=company,
        departments=Department.query.filter_by(company_id=company.id, is_active=True).order_by(Department.name.asc()).all() if company else [],
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
        current_company=company,
    )
