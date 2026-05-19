from __future__ import annotations

import json
import secrets
import time
from hmac import compare_digest
from urllib.parse import urlparse

from flask import abort, current_app, g, has_request_context, request, session
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from .extensions import db
from .models.company import Company
from .models.user import User, UserCompanyAccess, UserViewPreference

USER_SESSION_KEY = "andon_user_id"
COMPANY_SESSION_KEY = "andon_company_id"
WORKSPACE_NEXT_SESSION_KEY = "andon_workspace_next"
WORKSPACE_OPTIONS_SESSION_KEY = "andon_workspace_options"
ADMIN_SESSION_KEY = "andon_admin_authenticated"
CSRF_SESSION_KEY = "andon_csrf_token"
DEV_SECRET_KEY = "dev-andon-secret-key"
PAGE_HOME = "home"
PAGE_OPERATOR = "operator"
PAGE_BOARD = "board"
PAGE_MANAGEMENT = "management"
PAGE_REPORTS = "reports"
PAGE_ADMIN = "admin"

ROLE_PAGE_ACCESS = {
    "Admin": {PAGE_HOME, PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT, PAGE_REPORTS, PAGE_ADMIN},
    "Manager": {PAGE_HOME, PAGE_BOARD, PAGE_MANAGEMENT, PAGE_REPORTS},
    "Operator": {PAGE_HOME, PAGE_OPERATOR},
}
ROLE_LANDING_PAGE = {
    "Admin": "pages.management_page",
    "Manager": "pages.board_page",
    "Operator": "pages.operator_page",
}


def _perf_enabled() -> bool:
    return has_request_context() and bool(current_app.config.get("ANDON_PERF_LOGS"))


def _perf_log(event: str, started_at: float, **extra) -> None:
    if not _perf_enabled():
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    suffix = " ".join(f"{key}={value}" for key, value in extra.items())
    current_app.logger.debug("PERF %s duration_ms=%.1f %s", event, duration_ms, suffix)


def generate_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_request() -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not provided:
        return False
    return compare_digest(str(expected), str(provided))


def enforce_csrf() -> None:
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return
    if not validate_csrf_request():
        abort(400, description="CSRF validation failed")


def get_authenticated_user() -> User | None:
    started_at = time.perf_counter()
    if not has_request_context():
        return None
    cached = getattr(g, "authenticated_user", None)
    if cached is not None:
        _perf_log("get_authenticated_user(cache)", started_at, user_id=getattr(cached, "id", None))
        return cached
    user_id = session.get(USER_SESSION_KEY)
    user = User.query.filter_by(id=user_id).one_or_none() if user_id else None
    g.authenticated_user = user
    _perf_log("get_authenticated_user(db)", started_at, user_id=user_id, found=bool(user))
    return user


def is_authenticated() -> bool:
    user = get_authenticated_user()
    return bool(user and user.is_active)


def login_user(user: User) -> None:
    session[USER_SESSION_KEY] = user.id
    session.pop(ADMIN_SESSION_KEY, None)
    g.authenticated_user = user


def logout_user() -> None:
    session.pop(USER_SESSION_KEY, None)
    session.pop(ADMIN_SESSION_KEY, None)
    session.pop(COMPANY_SESSION_KEY, None)
    session.pop(WORKSPACE_NEXT_SESSION_KEY, None)
    session.pop(WORKSPACE_OPTIONS_SESSION_KEY, None)
    session.pop("andon_company_slug", None)
    if has_request_context():
        g.authenticated_user = None
        g.current_user_membership = None
        g.current_company = None
        g.current_companies = None


def authenticate_user(identity: str | None, password: str | None) -> User | None:
    normalized_identity = str(identity or "").strip()
    if not normalized_identity:
        return None
    user = User.query.filter(
        or_(User.username == normalized_identity, User.email == normalized_identity)
    ).one_or_none()
    if not user or not user.is_active:
        return None
    if not user.check_password(password):
        return None
    return user


def get_user_memberships(user: User | None = None, active_only: bool = True) -> list[UserCompanyAccess]:
    started_at = time.perf_counter()
    current_user = user or get_authenticated_user()
    if current_user is None:
        _perf_log("get_user_memberships(skip_no_user)", started_at)
        return []
    if has_request_context():
        cache = getattr(g, "user_memberships_cache", None)
        if cache is None:
            cache = {}
            g.user_memberships_cache = cache
        cache_user_id = current_user.id if current_user else None
        cache_key = (cache_user_id, bool(active_only))
        if cache_key in cache:
            memberships = cache[cache_key]
            _perf_log("get_user_memberships(cache)", started_at, total=len(memberships), active=len(memberships))
            return memberships
    query = UserCompanyAccess.query.filter_by(user_id=current_user.id)
    if active_only:
        query = query.filter_by(is_active=True)
    memberships = (
        query.options(joinedload(UserCompanyAccess.company))
        .order_by(UserCompanyAccess.company_id.asc())
        .all()
    )
    filtered = [membership for membership in memberships if membership.company and membership.company.is_active]
    if has_request_context():
        g.user_memberships_cache[(current_user.id, bool(active_only))] = filtered
    _perf_log("get_user_memberships(db)", started_at, total=len(memberships), active=len(filtered))
    return filtered


def get_accessible_companies(user: User | None = None) -> list[Company]:
    return [membership.company for membership in get_user_memberships(user=user) if membership.company]


def get_default_membership(user: User | None = None) -> UserCompanyAccess | None:
    memberships = get_user_memberships(user=user)
    return memberships[0] if memberships else None


def get_default_landing_endpoint(user: User | None = None, membership: UserCompanyAccess | None = None) -> str:
    effective_membership = membership or get_default_membership(user)
    if effective_membership is None:
        return "pages.home_page"
    return ROLE_LANDING_PAGE.get(effective_membership.role, "pages.home_page")


def can_access_company(company: Company | None, user: User | None = None) -> bool:
    if company is None:
        return False
    return any(item.id == company.id for item in get_accessible_companies(user=user))


def get_current_membership(user: User | None = None, company: Company | None = None) -> UserCompanyAccess | None:
    started_at = time.perf_counter()
    if not has_request_context():
        return None
    cached = getattr(g, "current_user_membership", None)
    if cached is not None and user is None and company is None:
        _perf_log("get_current_membership(cache)", started_at, found=True)
        return cached
    current_user = user or get_authenticated_user()
    if current_user is None:
        _perf_log("get_current_membership(skip_no_user)", started_at)
        return None
    target_company = company or getattr(g, "current_company", None)
    if target_company is None:
        from .company_context import get_current_company

        target_company = get_current_company()
    if target_company is None:
        _perf_log("get_current_membership(skip_no_company)", started_at)
        return None
    memberships = get_user_memberships(user=current_user, active_only=True)
    membership = next((item for item in memberships if item.company_id == target_company.id), None)
    if user is None and company is None:
        g.current_user_membership = membership
    _perf_log("get_current_membership(memberships)", started_at, company_id=target_company.id, found=bool(membership))
    return membership


def ensure_session_company(user: User | None = None) -> UserCompanyAccess | None:
    started_at = time.perf_counter()
    current_user = user or get_authenticated_user()
    if current_user is None:
        _perf_log("ensure_session_company(skip_no_user)", started_at)
        return None
    from .company_context import get_current_company, set_current_company_slug

    memberships = get_user_memberships(user=current_user, active_only=True) if user else get_user_memberships(active_only=True)
    if not memberships:
        _perf_log("ensure_session_company(no_memberships)", started_at)
        return None
    current_company = get_current_company()
    membership = None
    if current_company is not None:
        membership = next((item for item in memberships if item.company_id == current_company.id), None)
    if membership is not None:
        if has_request_context():
            g.current_user_membership = membership
        _perf_log("ensure_session_company(current_ok)", started_at, company_slug=current_company.slug)
        return membership
    default_membership = memberships[0]
    if default_membership and default_membership.company:
        set_current_company_slug(default_membership.company.slug)
        if has_request_context():
            g.current_user_membership = default_membership
        _perf_log("ensure_session_company(default_set)", started_at, company_slug=default_membership.company.slug)
        return default_membership
    _perf_log("ensure_session_company(no_membership)", started_at)
    return None


def require_authentication() -> User:
    user = get_authenticated_user()
    if user and user.is_active:
        return user
    abort(403)


def user_can_access_page(page_key: str, membership: UserCompanyAccess | None = None) -> bool:
    effective_membership = membership or get_current_membership()
    if effective_membership is None:
        return False
    return page_key in ROLE_PAGE_ACCESS.get(effective_membership.role, set())


def require_page_access(page_key: str) -> UserCompanyAccess:
    require_authentication()
    membership = ensure_session_company()
    if membership is None or not user_can_access_page(page_key, membership):
        abort(403)
    return membership


def is_admin_authenticated() -> bool:
    membership = get_current_membership()
    return bool(membership and membership.role == "Admin")


def set_admin_authenticated(value: bool) -> None:
    session[ADMIN_SESSION_KEY] = bool(value)


def validate_production_security_config(config: dict) -> None:
    secret_key = config.get("SECRET_KEY")
    if not secret_key or str(secret_key).strip() == DEV_SECRET_KEY:
        raise RuntimeError("Production requires a non-default SECRET_KEY.")


def require_admin_authentication() -> None:
    membership = require_page_access(PAGE_ADMIN)
    if membership.role != "Admin":
        abort(403)


def is_safe_redirect_target(target: str | None) -> bool:
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return target.startswith("/")


def get_authorized_company_id() -> int | None:
    if not has_request_context():
        return None
    company = getattr(request, "andon_authorized_company", None)
    if company is not None:
        return company.id if company else None
    from .company_context import get_current_company

    company = get_current_company()
    request.andon_authorized_company = company
    return company.id if company else None


def get_scope_filters(membership: UserCompanyAccess | None = None) -> dict:
    effective_membership = membership or get_current_membership()
    if effective_membership is None:
        return {"company_id": None, "department_id": None, "machine_group_name": None, "restricted": False}
    if effective_membership.role == "Admin" or effective_membership.scope_mode == "all":
        return {
            "company_id": effective_membership.company_id,
            "department_id": None,
            "machine_group_name": None,
            "restricted": False,
        }
    machine_group_name = effective_membership.machine_group.name if effective_membership.machine_group else None
    return {
        "company_id": effective_membership.company_id,
        "department_id": effective_membership.department_id,
        "machine_group_name": machine_group_name,
        "restricted": True,
    }


def get_view_preference(page_key: str, company_id: int | None = None, user: User | None = None) -> dict:
    current_user = user or get_authenticated_user()
    if current_user is None:
        return {}
    resolved_company_id = company_id
    if resolved_company_id is None:
        membership = get_current_membership(user=current_user)
        resolved_company_id = membership.company_id if membership else None
    preference = UserViewPreference.query.filter_by(
        user_id=current_user.id,
        company_id=resolved_company_id,
        page_key=page_key,
    ).one_or_none()
    if preference is None or not preference.preferences_json:
        return {}
    try:
        return json.loads(preference.preferences_json)
    except json.JSONDecodeError:
        return {}


def save_view_preference(page_key: str, payload: dict, company_id: int | None = None, user: User | None = None) -> dict:
    current_user = user or get_authenticated_user()
    if current_user is None:
        abort(403)
    resolved_company_id = company_id
    if resolved_company_id is None:
        membership = get_current_membership(user=current_user)
        resolved_company_id = membership.company_id if membership else None
    preference = UserViewPreference.query.filter_by(
        user_id=current_user.id,
        company_id=resolved_company_id,
        page_key=page_key,
    ).one_or_none()
    serialized = json.dumps(payload or {})
    if preference is None:
        preference = UserViewPreference(
            user_id=current_user.id,
            company_id=resolved_company_id,
            page_key=page_key,
            preferences_json=serialized,
        )
        db.session.add(preference)
    else:
        preference.preferences_json = serialized
    db.session.commit()
    return payload or {}
