from __future__ import annotations

from sqlalchemy import update
from flask import Blueprint, jsonify, flash, redirect, render_template, request, url_for

from ..company_context import get_current_company, get_current_company_id
from ..extensions import db
from ..models.company import Company
from ..models.alert import AndonAlert, AndonAlertEvent
from ..models.department import Department
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import User

admin_bp = Blueprint("admin", __name__, url_prefix="/andon/admin")


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


def _company_query(model):
    query = model.query
    company_id = _company_id()
    if company_id and hasattr(model, "company_id"):
        query = query.filter(model.company_id == company_id)
    return query


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


@admin_bp.post("/department/create")
def create_department():
    company_id = _company_id()
    name = request.form["name"].strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Department name is required"}), 400
        flash("Department name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    department = Department(company_id=company_id, name=name, is_active=True)
    db.session.add(department)
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Department created",
            department={
                "id": department.id,
                "name": department.name,
                "is_active": department.is_active,
            },
        )
    flash("Department created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/department/<int:department_id>/toggle")
def toggle_department(department_id):
    company_id = _company_id()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not department:
        return _error_or_404("Department not found")
    department.is_active = not department.is_active
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Department updated",
            department={
                "id": department.id,
                "name": department.name,
                "is_active": department.is_active,
            },
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

    db.session.delete(department)
    db.session.commit()
    if _is_ajax_request():
        return _json_response("Department and linked issues removed", department_id=department_id, affected_user_ids=affected_user_ids)
    flash("Department and linked issues removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine/create")
def create_machine():
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
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine name is required"}), 400
        flash("Machine name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    machine = Machine(
        company_id=company_id,
        machine_code=_machine_code_from_name(name, company_id),
        name=name,
        machine_type=group.name,
        department_id=_int_or_none(request.form.get("department_id")),
        is_active=True,
    )
    db.session.add(machine)
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Machine created",
            machine={
                "id": machine.id,
                "machine_code": machine.machine_code,
                "name": machine.name,
                "machine_type": machine.machine_type,
                "machine_group": machine.machine_type,
                "department_id": machine.department_id,
                "department_name": machine.department.name if machine.department else None,
                "is_active": machine.is_active,
            },
        )
    flash("Machine created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine/<int:machine_id>/toggle")
def toggle_machine(machine_id):
    company_id = _company_id()
    machine = Machine.query.filter_by(id=machine_id, company_id=company_id).one_or_none()
    if not machine:
        return _error_or_404("Machine not found")
    machine.is_active = not machine.is_active
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Machine updated",
            machine={
                "id": machine.id,
                "name": machine.name,
                "machine_type": machine.machine_type,
                "machine_group": machine.machine_type,
                "department_id": machine.department_id,
                "department_name": machine.department.name if machine.department else None,
                "is_active": machine.is_active,
            },
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
    if _is_ajax_request():
        return _json_response("Machine removed", machine_id=machine_id, machine_group=group_name)
    flash("Machine removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/create")
def create_machine_group():
    company_id = _company_id()
    name = request.form["name"].strip()
    if not name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine group name is required"}), 400
        flash("Machine group name is required", "warning")
        return redirect(url_for("pages.admin_page"))

    existing = MachineGroup.query.filter_by(company_id=company_id, name=name).one_or_none()
    if existing:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Machine group already exists"}), 400
        flash("Machine group already exists", "warning")
        return redirect(url_for("pages.admin_page"))

    group = MachineGroup(company_id=company_id, name=name, is_active=True)
    db.session.add(group)
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Machine group created",
            machine_group={
                "id": group.id,
                "name": group.name,
                "is_active": group.is_active,
                "machine_count": 0,
            },
        )
    flash("Machine group created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/machine-group/<int:group_id>/toggle")
def toggle_machine_group(group_id):
    company_id = _company_id()
    group = MachineGroup.query.filter_by(id=group_id, company_id=company_id).one_or_none()
    if not group:
        return _error_or_404("Machine group not found")
    group.is_active = not group.is_active
    target_state = group.is_active
    for machine in Machine.query.filter(Machine.company_id == company_id, Machine.machine_type == group.name).all():
        machine.is_active = target_state
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Machine group updated",
            machine_group={
                "id": group.id,
                "name": group.name,
                "is_active": group.is_active,
                "machine_count": _machine_group_count(group.name),
            },
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
    db.session.delete(group)
    db.session.commit()
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
    company_id = _company_id()
    machines = Machine.query.filter(Machine.company_id == company_id, Machine.machine_type == machine_type).all()
    if not machines:
        flash("Machine group not found", "warning")
        return redirect(url_for("pages.admin_page"))

    target_state = request.form.get("is_active")
    if target_state in [None, ""]:
        target_value = not all(machine.is_active for machine in machines)
    else:
        target_value = target_state.lower() in {"1", "true", "yes", "on"}

    for machine in machines:
        machine.is_active = target_value

    db.session.commit()
    flash(f"{machine_type} group updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/create")
def create_user():
    company_id = _company_id()
    display_name = (request.form.get("display_name") or "").strip()
    work_id = (request.form.get("work_id") or "").strip() or None
    machine_group_id = _int_or_none(request.form.get("machine_group_id"))
    department_id = _int_or_none(request.form.get("department_id"))
    if not display_name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "User name is required"}), 400
        flash("User name is required", "warning")
        return redirect(url_for("pages.admin_page"))
    if machine_group_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a machine group"}), 400
        flash("Please select a machine group", "warning")
        return redirect(url_for("pages.admin_page"))
    if department_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a department"}), 400
        flash("Please select a department", "warning")
        return redirect(url_for("pages.admin_page"))

    machine_group = MachineGroup.query.filter_by(id=machine_group_id, company_id=company_id).one_or_none()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not machine_group:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a machine group"}), 400
        flash("Please select a machine group", "warning")
        return redirect(url_for("pages.admin_page"))
    if not department:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a department"}), 400
        flash("Please select a department", "warning")
        return redirect(url_for("pages.admin_page"))

    user = User(
        company_id=company_id,
        employee_id=work_id,
        display_name=display_name,
        username=None,
        role="Staff",
        email=request.form.get("email") or None,
        phone_number=request.form.get("phone_number") or None,
        department_id=department_id,
        machine_group_id=machine_group_id,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "User created",
            user={
                "id": user.id,
                "display_name": user.display_name,
                "work_id": user.employee_id,
                "email": user.email,
                "phone_number": user.phone_number,
                "department_id": user.department_id,
                "department_name": user.department.name if user.department else None,
                "machine_group_id": user.machine_group_id,
                "machine_group_name": user.machine_group.name if user.machine_group else None,
                "is_active": user.is_active,
            },
        )
    flash("User created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/update")
def update_user(user_id):
    company_id = _company_id()
    user = User.query.filter_by(id=user_id, company_id=company_id).one_or_none()
    if not user:
        return _error_or_404("User not found")
    display_name = (request.form.get("display_name") or "").strip()
    work_id = (request.form.get("work_id") or "").strip() or None
    machine_group_id = _int_or_none(request.form.get("machine_group_id"))
    department_id = _int_or_none(request.form.get("department_id"))
    email = (request.form.get("email") or "").strip() or None
    phone_number = (request.form.get("phone_number") or "").strip() or None

    if not display_name:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "User name is required"}), 400
        flash("User name is required", "warning")
        return redirect(url_for("pages.admin_page"))
    if machine_group_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a machine group"}), 400
        flash("Please select a machine group", "warning")
        return redirect(url_for("pages.admin_page"))
    if department_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a department"}), 400
        flash("Please select a department", "warning")
        return redirect(url_for("pages.admin_page"))

    machine_group = MachineGroup.query.filter_by(id=machine_group_id, company_id=company_id).one_or_none()
    department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
    if not machine_group:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a machine group"}), 400
        flash("Please select a machine group", "warning")
        return redirect(url_for("pages.admin_page"))
    if not department:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please select a department"}), 400
        flash("Please select a department", "warning")
        return redirect(url_for("pages.admin_page"))

    user.display_name = display_name
    user.employee_id = work_id
    user.email = email
    user.phone_number = phone_number
    user.machine_group_id = machine_group.id
    user.department_id = department.id
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "User updated",
            user={
                "id": user.id,
                "display_name": user.display_name,
                "work_id": user.employee_id,
                "email": user.email,
                "phone_number": user.phone_number,
                "department_id": user.department_id,
                "department_name": user.department.name if user.department else None,
                "machine_group_id": user.machine_group_id,
                "machine_group_name": user.machine_group.name if user.machine_group else None,
                "is_active": user.is_active,
            },
        )
    flash("User updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/toggle")
def toggle_user(user_id):
    company_id = _company_id()
    user = User.query.filter_by(id=user_id, company_id=company_id).one_or_none()
    if not user:
        return _error_or_404("User not found")
    user.is_active = not user.is_active
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "User updated",
            user={
                "id": user.id,
                "display_name": user.display_name,
                "work_id": user.employee_id,
                "email": user.email,
                "phone_number": user.phone_number,
                "department_id": user.department_id,
                "department_name": user.department.name if user.department else None,
                "machine_group_id": user.machine_group_id,
                "machine_group_name": user.machine_group.name if user.machine_group else None,
                "is_active": user.is_active,
            },
        )
    flash("User updated", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/user/<int:user_id>/delete")
def delete_user(user_id):
    company_id = _company_id()
    user = User.query.filter_by(id=user_id, company_id=company_id).one_or_none()
    if not user:
        return _error_or_404("User not found")
    for alert in AndonAlert.query.filter(
        AndonAlert.company_id == company_id,
        (AndonAlert.operator_user_id == user.id) | (AndonAlert.responder_user_id == user.id)
    ).all():
        if alert.operator_user_id == user.id:
            alert.operator_user_id = None
            alert.operator_name_text = None
        if alert.responder_user_id == user.id:
            alert.responder_user_id = None
            alert.responder_name_text = None
    db.session.execute(
        update(AndonAlertEvent)
        .where(AndonAlertEvent.company_id == company_id, AndonAlertEvent.user_id == user.id)
        .values(user_id=None, user_name_text=None)
    )
    db.session.delete(user)
    db.session.commit()
    if _is_ajax_request():
        return _json_response("User removed", user_id=user_id)
    flash("User removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/problem/create")
def create_problem():
    company_id = _company_id()
    department_id = _int_or_none(request.form.get("department_id"))
    if department_id is None:
        if _is_ajax_request():
            return jsonify({"ok": False, "message": "Please add a department"}), 400
        flash("Please add a department", "warning")
        return redirect(url_for("pages.admin_page"))
    category = IssueCategory.query.filter_by(company_id=company_id, department_id=department_id).one_or_none() if department_id else None
    if not category and department_id:
        department = Department.query.filter_by(id=department_id, company_id=company_id).one_or_none()
        if department:
            category = IssueCategory(
                name=department.name,
                department_id=department.id,
                company_id=department.company_id,
                color="#0d6efd",
                priority_default=3,
                is_active=True,
            )
            db.session.add(category)
            db.session.flush()
    if not category:
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

    problem = IssueProblem(
        company_id=company_id,
        category_id=category.id,
        name=name,
        severity_default=3,
        is_active=True,
    )
    db.session.add(problem)
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Issue problem created",
            problem={
                "id": problem.id,
                "name": problem.name,
                "is_active": problem.is_active,
                "department_id": department_id,
                "department_name": category.department.name if category.department else None,
            },
        )
    flash("Issue problem created", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/problem/<int:problem_id>/toggle")
def toggle_problem(problem_id):
    company_id = _company_id()
    problem = IssueProblem.query.filter_by(id=problem_id, company_id=company_id).one_or_none()
    if not problem:
        return _error_or_404("Issue problem not found")
    problem.is_active = not problem.is_active
    db.session.commit()
    if _is_ajax_request():
        return _json_response(
            "Issue problem updated",
            problem={
                "id": problem.id,
                "name": problem.name,
                "is_active": problem.is_active,
                "department_id": problem.category.department_id if problem.category else None,
                "department_name": problem.category.department.name if problem.category and problem.category.department else None,
            },
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
    if _is_ajax_request():
        return _json_response("Issue removed", problem_id=problem_id, department_id=department_id)
    flash("Issue removed", "success")
    return redirect(url_for("pages.admin_page"))


@admin_bp.post("/escalation/create")
def create_escalation_rule():
    company_id = _company_id()
    rule = EscalationRule(
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
    db.session.add(rule)
    db.session.commit()
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
    company_id = _company_id()
    rule = EscalationRule.query.filter_by(id=rule_id, company_id=company_id).one_or_none()
    if not rule:
        return _error_or_404("Escalation rule not found")
    rule.is_active = not rule.is_active
    db.session.commit()
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
