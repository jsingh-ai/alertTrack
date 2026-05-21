from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy.orm import joinedload, load_only

from ..company_context import get_current_company, set_current_company_id, set_current_company_slug
from ..extensions import db
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import USER_ROLES, USER_SCOPE_MODES, User, UserCompanyAccess
from ..security import (
    PAGE_ADMIN,
    PAGE_BOARD,
    PAGE_MANAGEMENT,
    PAGE_OPERATOR,
    PAGE_REPORTS,
    authenticate_user,
    ensure_session_company,
    get_authenticated_user,
    get_current_membership,
    get_default_landing_endpoint,
    get_scope_filters,
    get_user_memberships,
    is_authenticated,
    is_safe_redirect_target,
    login_user,
    logout_user,
    require_admin_authentication,
    require_page_access,
)
from ..services.escalation_service import FIXED_ESCALATION_PHASES, ensure_fixed_escalation_rules

pages_bp = Blueprint("pages", __name__)
MANAGEMENT_TIMEZONE = ZoneInfo("America/Chicago")
WORKSPACE_PROMPT_SESSION_KEY = "andon_workspace_prompt"
WORKSPACE_OPTIONS_SESSION_KEY = "andon_workspace_options"
_LOGIN_RATE_LIMIT_LOCK = threading.Lock()
_LOGIN_RATE_LIMIT = {}


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


def _landing_redirect():
    return redirect(url_for(get_default_landing_endpoint()))


def _require_page_or_redirect(page_key: str):
    if not is_authenticated():
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("pages.home_page"))
    try:
        require_page_access(page_key)
    except Exception:
        flash("You do not have access to that page.", "warning")
        return _landing_redirect()
    return None


def _login_rate_limit_key(identity: str | None, remote_addr: str | None) -> str:
    normalized_identity = str(identity or "").strip().lower()
    identity_digest = hashlib.sha256(normalized_identity.encode("utf-8")).hexdigest()[:24] if normalized_identity else "anon"
    return f"{remote_addr or 'unknown'}:{identity_digest}"


def _is_login_rate_limited(identity: str | None, remote_addr: str | None) -> tuple[bool, int]:
    now = time.monotonic()
    window = int(current_app.config.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
    max_attempts = int(current_app.config.get("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 8))
    key = _login_rate_limit_key(identity, remote_addr)
    with _LOGIN_RATE_LIMIT_LOCK:
        if len(_LOGIN_RATE_LIMIT) > 2048:
            stale = [entry_key for entry_key, value in _LOGIN_RATE_LIMIT.items() if float(value.get("expires_at", 0)) <= now]
            for stale_key in stale:
                _LOGIN_RATE_LIMIT.pop(stale_key, None)
        entry = _LOGIN_RATE_LIMIT.get(key)
        if not entry:
            return False, 0
        attempts = int(entry.get("attempts", 0))
        expires_at = float(entry.get("expires_at", 0))
        if expires_at <= now:
            _LOGIN_RATE_LIMIT.pop(key, None)
            return False, 0
        if attempts < max_attempts:
            return False, 0
        retry_after = max(1, int(expires_at - now))
        return True, retry_after


def _record_login_failure(identity: str | None, remote_addr: str | None) -> None:
    now = time.monotonic()
    window = int(current_app.config.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
    key = _login_rate_limit_key(identity, remote_addr)
    with _LOGIN_RATE_LIMIT_LOCK:
        entry = _LOGIN_RATE_LIMIT.get(key)
        if not entry or float(entry.get("expires_at", 0)) <= now:
            _LOGIN_RATE_LIMIT[key] = {"attempts": 1, "expires_at": now + window}
            return
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        _LOGIN_RATE_LIMIT[key] = entry


def _clear_login_failures(identity: str | None, remote_addr: str | None) -> None:
    key = _login_rate_limit_key(identity, remote_addr)
    with _LOGIN_RATE_LIMIT_LOCK:
        _LOGIN_RATE_LIMIT.pop(key, None)


@pages_bp.route("/andon")
def landing_page():
    if is_authenticated():
        ensure_session_company()
        return _landing_redirect()
    return redirect(url_for("pages.home_page"))


@pages_bp.get("/andon/home")
def home_page():
    if is_authenticated():
        if session.get(WORKSPACE_PROMPT_SESSION_KEY):
            cached_companies = session.get(WORKSPACE_OPTIONS_SESSION_KEY) or []
            if cached_companies:
                companies = cached_companies
            else:
                memberships = get_user_memberships(active_only=True)
                companies = [
                    {"id": membership.company.id, "name": membership.company.name}
                    for membership in memberships
                    if membership.company
                ]
                session[WORKSPACE_OPTIONS_SESSION_KEY] = companies
            return render_template("andon/home.html", workspace_companies=companies)
        ensure_session_company()
        return _landing_redirect()
    return render_template("andon/home.html")


@pages_bp.post("/andon/login")
def login_page():
    identity = request.form.get("identity")
    password = request.form.get("password")
    next_url = request.form.get("next")
    is_blocked, retry_after = _is_login_rate_limited(identity, request.remote_addr)
    if is_blocked:
        flash(f"Too many login attempts. Please try again in {retry_after} seconds.", "warning")
        return redirect(url_for("pages.home_page"))
    user = authenticate_user(identity, password)
    if user is None:
        _record_login_failure(identity, request.remote_addr)
        flash("Invalid username/email or password.", "warning")
        return redirect(url_for("pages.home_page"))
    _clear_login_failures(identity, request.remote_addr)
    login_user(user)
    memberships = get_user_memberships(user=user, active_only=True)
    if not memberships:
        logout_user()
        flash("This account does not have any active company access.", "warning")
        return redirect(url_for("pages.home_page"))
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
    membership = memberships[0]
    if len(memberships) == 1:
        session.pop(WORKSPACE_PROMPT_SESSION_KEY, None)
        set_current_company_id(membership.company_id)
    else:
        session[WORKSPACE_PROMPT_SESSION_KEY] = True
        session[WORKSPACE_OPTIONS_SESSION_KEY] = [
            {"id": member.company.id, "name": member.company.name}
            for member in memberships
            if member.company
        ]
        if next_url and is_safe_redirect_target(next_url):
            session["andon_workspace_next"] = next_url
        return redirect(url_for("pages.home_page"))
    if next_url and is_safe_redirect_target(next_url):
        return redirect(next_url)
    return redirect(url_for(get_default_landing_endpoint(user, membership)))


@pages_bp.get("/andon/workspace/select")
def workspace_select_page():
    if not is_authenticated():
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("pages.home_page"))
    session[WORKSPACE_PROMPT_SESSION_KEY] = True
    return redirect(url_for("pages.home_page"))


@pages_bp.post("/andon/workspace/select")
def workspace_select_submit():
    if not is_authenticated():
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("pages.home_page"))
    company_id = request.form.get("company_id")
    company = set_current_company_id(company_id)
    if company is None:
        flash("You do not have access to that company.", "warning")
        session[WORKSPACE_PROMPT_SESSION_KEY] = True
        return redirect(url_for("pages.home_page"))
    session.pop(WORKSPACE_PROMPT_SESSION_KEY, None)
    session.pop(WORKSPACE_OPTIONS_SESSION_KEY, None)
    membership = ensure_session_company()
    next_url = session.pop("andon_workspace_next", None)
    if next_url and is_safe_redirect_target(next_url):
        return redirect(next_url)
    return redirect(url_for(get_default_landing_endpoint(get_authenticated_user(), membership)))


@pages_bp.post("/andon/logout")
def logout_page():
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("pages.home_page"))


@pages_bp.post("/andon/company/select")
def select_company():
    if not is_authenticated():
        return redirect(url_for("pages.home_page"))
    company_id = request.form.get("company_id")
    slug = request.form.get("company_slug")
    next_url = request.form.get("next") or request.referrer
    company = set_current_company_id(company_id) if company_id else None
    if company is None and slug:
        company = set_current_company_slug(slug)
    if company is None:
        flash("You do not have access to that company.", "warning")
        return _landing_redirect()
    if next_url and is_safe_redirect_target(next_url):
        return redirect(next_url)
    return _landing_redirect()


@pages_bp.route("/andon/operator")
def operator_page():
    redirect_response = _require_page_or_redirect(PAGE_OPERATOR)
    if redirect_response is not None:
        return redirect_response
    return render_template("andon/operator.html", current_company=get_current_company())


@pages_bp.route("/andon/management")
def management_page():
    redirect_response = _require_page_or_redirect(PAGE_MANAGEMENT)
    if redirect_response is not None:
        return redirect_response
    shift_window = _management_shift_window()
    return render_template(
        "andon/management.html",
        current_company=get_current_company(),
        management_shift_start=shift_window["start"],
        management_shift_end=shift_window["end"],
        management_shift_label=shift_window["label"],
    )


@pages_bp.route("/andon/custom-boards")
def custom_boards_page():
    if not is_authenticated():
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("pages.home_page"))
    try:
        require_admin_authentication()
    except Exception:
        flash("Admin access required.", "warning")
        return _landing_redirect()
    shift_window = _management_shift_window()
    return render_template(
        "andon/custom_boards.html",
        current_company=get_current_company(),
        management_shift_start=shift_window["start"],
        management_shift_end=shift_window["end"],
        management_shift_label=shift_window["label"],
    )


@pages_bp.route("/andon/board")
def board_page():
    redirect_response = _require_page_or_redirect(PAGE_BOARD)
    if redirect_response is not None:
        return redirect_response
    return render_template("andon/board.html", current_company=get_current_company())


@pages_bp.route("/andon/reports")
def reports_page():
    redirect_response = _require_page_or_redirect(PAGE_REPORTS)
    if redirect_response is not None:
        return redirect_response
    company = get_current_company()
    scope = get_scope_filters()
    query = Machine.query.with_entities(Machine.machine_type).filter(
        Machine.company_id == company.id,
        Machine.machine_type.isnot(None),
        Machine.machine_type != "",
    )
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    if machine_ids:
        query = query.filter(Machine.id.in_(machine_ids))
    if department_ids:
        query = query.filter(Machine.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(Machine.machine_type.in_(machine_group_names))
    machine_groups = [row.machine_type for row in query.distinct().order_by(Machine.machine_type.asc()).all()] if company else []
    return render_template(
        "andon/reports.html",
        current_company=company,
        machine_groups=machine_groups,
    )


@pages_bp.route("/andon/admin")
def admin_page():
    if not is_authenticated():
        flash("Please sign in to continue.", "warning")
        return redirect(url_for("pages.home_page"))
    try:
        require_admin_authentication()
    except Exception:
        flash("Admin access required.", "warning")
        return _landing_redirect()
    company = get_current_company()
    company_id = company.id if company else None
    active_section = (request.args.get("section") or "machine-groups").strip().lower()
    if active_section not in {"machine-groups", "departments", "users", "escalation"}:
        active_section = "machine-groups"
    escalation_rules_map = ensure_fixed_escalation_rules() if active_section == "escalation" else {}
    needs_machine_data = active_section in {"machine-groups", "users"}
    needs_user_data = active_section == "users"
    needs_department_data = active_section in {"departments", "users"}
    needs_issue_data = active_section == "departments"
    machines = (
        Machine.query.options(
            load_only(
                Machine.id,
                Machine.name,
                Machine.machine_code,
                Machine.machine_type,
                Machine.department_id,
                Machine.is_active,
            )
        ).filter_by(company_id=company_id).order_by(Machine.machine_type.asc().nullslast(), Machine.name.asc()).all()
        if company_id and needs_machine_data
        else []
    )
    access_rows = (
        UserCompanyAccess.query.options(
            load_only(
                UserCompanyAccess.id,
                UserCompanyAccess.role,
                UserCompanyAccess.scope_mode,
                UserCompanyAccess.department_id,
                UserCompanyAccess.machine_group_id,
                UserCompanyAccess.scope_config_json,
                UserCompanyAccess.is_active,
            ),
            joinedload(UserCompanyAccess.user).load_only(
                User.id,
                User.display_name,
                User.username,
                User.employee_id,
                User.email,
                User.phone_number,
                User.password_hash,
            ),
            joinedload(UserCompanyAccess.department).load_only(Department.id, Department.name),
            joinedload(UserCompanyAccess.machine_group).load_only(MachineGroup.id, MachineGroup.name),
        )
        .filter_by(company_id=company_id)
        .order_by(UserCompanyAccess.is_active.desc(), UserCompanyAccess.role.asc(), UserCompanyAccess.id.asc())
        .all()
        if company_id and needs_user_data
        else []
    )
    users = []
    for access in access_rows:
        if access.user is None:
            continue
        user_payload = access.user.to_dict()
        try:
            scope_config = json.loads(access.scope_config_json or "{}")
        except json.JSONDecodeError:
            scope_config = {}
        user_payload.update(
            {
                "role": access.role,
                "scope_mode": access.scope_mode,
                "department_id": access.department_id,
                "department_name": access.department.name if access.department else None,
                "machine_group_id": access.machine_group_id,
                "machine_group_name": access.machine_group.name if access.machine_group else None,
                "scope_machine_ids": scope_config.get("machine_ids") or [],
                "scope_machine_group_ids": scope_config.get("machine_group_ids") or [],
                "scope_department_ids": scope_config.get("department_ids") or [],
                "is_active": access.is_active,
            }
        )
        users.append(user_payload)
    machine_groups = []
    machine_group_rows = (
        MachineGroup.query.options(
            load_only(MachineGroup.id, MachineGroup.name, MachineGroup.is_active)
        ).filter_by(company_id=company_id).order_by(MachineGroup.name.asc()).all()
        if company_id and (needs_machine_data or needs_user_data or needs_department_data)
        else []
    )
    machine_group_id_by_name = {group.name: group.id for group in machine_group_rows}
    departments = (
        Department.query.options(
            load_only(Department.id, Department.name, Department.is_active)
        ).filter_by(company_id=company_id).order_by(Department.name.asc()).all()
        if company_id and needs_department_data
        else []
    )
    department_name_by_id = {department.id: department.name for department in departments}
    machine_count_by_group_name = defaultdict(int)
    for machine in machines:
        machine_count_by_group_name[machine.machine_type] += 1
    for group in machine_group_rows:
        machine_groups.append(
            {
                "id": group.id,
                "name": group.name,
                "is_active": group.is_active,
                "machine_count": machine_count_by_group_name.get(group.name, 0),
            }
        )
    machine_scope_catalog = [
        {
            "id": machine.id,
            "name": machine.name,
            "machine_code": machine.machine_code,
            "machine_group_name": machine.machine_type,
            "machine_group_id": machine_group_id_by_name.get(machine.machine_type),
            "department_id": machine.department_id,
            "department_name": department_name_by_id.get(machine.department_id),
            "is_active": machine.is_active,
        }
        for machine in machines
    ]
    departments_catalog = [
        {
            "id": department.id,
            "name": department.name,
            "is_active": department.is_active,
        }
        for department in departments
    ]
    problems = (
        IssueProblem.query.options(
            load_only(IssueProblem.id, IssueProblem.name, IssueProblem.is_active),
            joinedload(IssueProblem.category).load_only(IssueCategory.department_id),
        ).join(IssueCategory)
        .join(Department)
        .filter(IssueProblem.company_id == company_id)
        .order_by(Department.name.asc(), IssueProblem.name.asc())
        .all()
        if company_id and needs_issue_data
        else []
    )
    problems_by_department_id = defaultdict(list)
    for problem in problems:
        if problem.category and problem.category.department_id is not None:
            problems_by_department_id[problem.category.department_id].append(problem)
    issue_groups = []
    for department in departments:
        issue_groups.append(
            {
                "department": department,
                "problems": problems_by_department_id.get(department.id, []),
            }
        )
    return render_template(
        "andon/admin.html",
        departments=departments,
        machines=machines,
        users=users,
        machine_groups=machine_groups,
        issue_groups=issue_groups,
        escalation_rules=[escalation_rules_map[level] for level in sorted(escalation_rules_map.keys())] if escalation_rules_map else [],
        escalation_phase_labels=FIXED_ESCALATION_PHASES,
        user_roles=USER_ROLES,
        user_scope_modes=USER_SCOPE_MODES,
        current_company=company,
        machine_scope_catalog=machine_scope_catalog,
        departments_catalog=departments_catalog,
        active_section=active_section,
    )
