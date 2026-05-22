from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
from types import SimpleNamespace
from urllib.parse import urlparse

from flask import abort, current_app, g, has_request_context, request, session
from sqlalchemy import inspect, or_, select, update
from sqlalchemy.orm import joinedload, load_only, noload
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models.company import Company
from .models.department import Department
from .models.machine import Machine
from .models.machine_group import MachineGroup
from .models.pager_device import PagerDevice
from .models.user import User, UserCompanyAccess, UserViewPreference

USER_SESSION_KEY = "andon_user_id"
COMPANY_SESSION_KEY = "andon_company_id"
WORKSPACE_NEXT_SESSION_KEY = "andon_workspace_next"
WORKSPACE_OPTIONS_SESSION_KEY = "andon_workspace_options"
MEMBERSHIPS_SESSION_KEY = "andon_memberships_snapshot"
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
    "Manager": {PAGE_HOME, PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT, PAGE_REPORTS},
    "Operator": {PAGE_HOME, PAGE_OPERATOR},
    "Viewer": {PAGE_HOME, PAGE_BOARD},
}
ROLE_LANDING_PAGE = {
    "Admin": "pages.management_page",
    "Manager": "pages.management_page",
    "Operator": "pages.operator_page",
    "Viewer": "pages.board_page",
}
PREFERENCE_ALLOWED_PAGE_KEYS = {PAGE_OPERATOR, PAGE_BOARD, PAGE_MANAGEMENT, PAGE_REPORTS, PAGE_ADMIN}
_PAGER_TOKEN_CACHE_TTL_SECONDS = 60
_PAGER_LAST_SEEN_THROTTLE_SECONDS = 30
_PAGER_CACHE_MAX_ENTRIES = 1024
_PAGER_TOKEN_DEVICE_CACHE = {}
_PAGER_LAST_SEEN_TRACKER = {}
_PAGER_CACHE_LOCK = threading.RLock()
_PAGER_FINGERPRINT_COLUMN_SUPPORTED: bool | None = None


def _perf_enabled() -> bool:
    return has_request_context() and bool(current_app.config.get("ANDON_PERF_LOGS"))


def _perf_log(event: str, started_at: float, **extra) -> None:
    if not _perf_enabled():
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    suffix = " ".join(f"{key}={value}" for key, value in extra.items())
    current_app.logger.debug("PERF %s duration_ms=%.1f %s", event, duration_ms, suffix)


def _safe_user_identity_id(user: User | None) -> int | None:
    if user is None:
        return None
    try:
        state = inspect(user)
        identity = getattr(state, "identity", None)
        if identity and len(identity) > 0:
            return identity[0]
    except Exception:
        return None
    return None


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
    if request.path.startswith("/api/andon/pager/") and get_authenticated_pager_device(update_last_seen=False):
        return
    if not validate_csrf_request():
        abort(400, description="CSRF validation failed")


def parse_bearer_token() -> str | None:
    auth_header = str(request.headers.get("Authorization") or "").strip()
    if not auth_header:
        return None
    scheme, _, value = auth_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _log_pager_auth_failure(reason: str) -> None:
    if not has_request_context():
        return
    if not request.path.startswith("/api/andon/pager/"):
        return
    current_app.logger.info(
        "PAGER_AUTH failed reason=%s remote=%s user_agent=%s",
        reason,
        request.remote_addr,
        request.user_agent.string[:160] if request.user_agent and request.user_agent.string else "",
    )


def hash_pager_token(raw_token: str) -> str:
    token = str(raw_token or "").strip()
    if not token:
        raise ValueError("Pager token cannot be blank")
    return generate_password_hash(token)


def fingerprint_pager_token(raw_token: str) -> str:
    token = str(raw_token or "").strip()
    if not token:
        raise ValueError("Pager token cannot be blank")
    return sha256(token.encode("utf-8")).hexdigest()


def verify_pager_token(token_hash: str | None, raw_token: str | None) -> bool:
    if not token_hash or not raw_token:
        return False
    return check_password_hash(token_hash, str(raw_token))


def get_authenticated_pager_device(update_last_seen: bool = True) -> PagerDevice | None:
    started_at = time.perf_counter()
    if not has_request_context():
        return None
    if hasattr(g, "authenticated_pager_device"):
        device = getattr(g, "authenticated_pager_device")
        if device is not None and update_last_seen and not getattr(g, "authenticated_pager_device_last_seen_updated", False):
            _maybe_update_pager_last_seen(device, update_last_seen=True)
        _perf_log("pager_auth(request_cache)", started_at, found=bool(device))
        return device

    parse_started_at = time.perf_counter()
    token = parse_bearer_token()
    parse_ms = (time.perf_counter() - parse_started_at) * 1000
    if not token:
        g.authenticated_pager_device = None
        g.authenticated_pager_device_last_seen_updated = False
        _log_pager_auth_failure("missing_bearer")
        _perf_log("pager_auth(missing_bearer)", started_at, parse_ms=round(parse_ms, 1))
        return None

    cache_started_at = time.perf_counter()
    cache_hit = _get_cached_pager_device_for_token(token)
    cache_ms = (time.perf_counter() - cache_started_at) * 1000
    if cache_hit is not None:
        g.authenticated_pager_device = cache_hit
        g.authenticated_pager_device_last_seen_updated = False
        last_seen_started_at = time.perf_counter()
        _maybe_update_pager_last_seen(cache_hit, update_last_seen)
        _perf_log(
            "pager_auth(cache_hit)",
            started_at,
            parse_ms=round(parse_ms, 1),
            cache_ms=round(cache_ms, 1),
            last_seen_ms=round((time.perf_counter() - last_seen_started_at) * 1000, 1),
            pager_device_id=getattr(cache_hit, "id", None),
        )
        return cache_hit

    fingerprint_support_started_at = time.perf_counter()
    if _pager_token_fingerprint_supported():
        fingerprint_lookup_started_at = time.perf_counter()
        device = _find_pager_device_by_fingerprint(token)
        fingerprint_lookup_ms = (time.perf_counter() - fingerprint_lookup_started_at) * 1000
        verify_started_at = time.perf_counter()
        if (
            device is not None
            and verify_pager_token(device.token_hash, token)
            and device.department
            and device.department.is_active
            and device.department.company_id == device.company_id
        ):
            g.authenticated_pager_device = device
            g.authenticated_pager_device_last_seen_updated = False
            _cache_pager_device_for_token(token, device)
            last_seen_started_at = time.perf_counter()
            _maybe_update_pager_last_seen(device, update_last_seen)
            _perf_log(
                "pager_auth(fingerprint_db_hit)",
                started_at,
                parse_ms=round(parse_ms, 1),
                cache_ms=round(cache_ms, 1),
                fingerprint_support_ms=round((time.perf_counter() - fingerprint_support_started_at) * 1000, 1),
                fingerprint_lookup_ms=round(fingerprint_lookup_ms, 1),
                verify_ms=round((time.perf_counter() - verify_started_at) * 1000, 1),
                last_seen_ms=round((time.perf_counter() - last_seen_started_at) * 1000, 1),
                pager_device_id=getattr(device, "id", None),
            )
            return device

    legacy_started_at = time.perf_counter()
    device = _find_legacy_pager_device_by_token(token)
    if device is not None:
        _maybe_persist_pager_token_fingerprint(device, token)
        g.authenticated_pager_device = device
        g.authenticated_pager_device_last_seen_updated = False
        _cache_pager_device_for_token(token, device)
        last_seen_started_at = time.perf_counter()
        _maybe_update_pager_last_seen(device, update_last_seen)
        _perf_log(
            "pager_auth(legacy_hit)",
            started_at,
            parse_ms=round(parse_ms, 1),
            cache_ms=round(cache_ms, 1),
            legacy_ms=round((time.perf_counter() - legacy_started_at) * 1000, 1),
            last_seen_ms=round((time.perf_counter() - last_seen_started_at) * 1000, 1),
            pager_device_id=getattr(device, "id", None),
        )
        return device

    g.authenticated_pager_device = None
    g.authenticated_pager_device_last_seen_updated = False
    _log_pager_auth_failure("invalid_token")
    _perf_log(
        "pager_auth(invalid_token)",
        started_at,
        parse_ms=round(parse_ms, 1),
        cache_ms=round(cache_ms, 1),
        legacy_ms=round((time.perf_counter() - legacy_started_at) * 1000, 1),
    )
    return None


def _get_cached_pager_device_for_token(token: str) -> PagerDevice | None:
    now = time.monotonic()
    fingerprint = fingerprint_pager_token(token)
    with _PAGER_CACHE_LOCK:
        cached = _PAGER_TOKEN_DEVICE_CACHE.get(fingerprint)
        if not cached:
            return None
        if cached["expires_at"] <= now:
            _PAGER_TOKEN_DEVICE_CACHE.pop(fingerprint, None)
            return None
        return SimpleNamespace(
            id=cached["device_id"],
            company_id=cached["company_id"],
            department_id=cached["department_id"],
            name=cached["name"],
            token_hash=cached.get("token_hash"),
            active=True,
            department=SimpleNamespace(
                id=cached["department_id"],
                company_id=cached["company_id"],
                is_active=True,
                name=cached.get("department_name"),
            ),
        )


def _cache_pager_device_for_token(token: str, device: PagerDevice) -> None:
    if not token or not device or not device.id or not device.token_hash:
        return
    fingerprint = fingerprint_pager_token(token)
    with _PAGER_CACHE_LOCK:
        _prune_pager_state_locked()
        _PAGER_TOKEN_DEVICE_CACHE[fingerprint] = {
            "device_id": int(device.id),
            "company_id": int(device.company_id),
            "department_id": int(device.department_id),
            "name": str(device.name or ""),
            "department_name": getattr(getattr(device, "department", None), "name", None),
            "token_hash": str(device.token_hash),
            "expires_at": time.monotonic() + _PAGER_TOKEN_CACHE_TTL_SECONDS,
        }


def _pager_token_fingerprint_supported() -> bool:
    global _PAGER_FINGERPRINT_COLUMN_SUPPORTED
    with _PAGER_CACHE_LOCK:
        if _PAGER_FINGERPRINT_COLUMN_SUPPORTED is not None:
            return _PAGER_FINGERPRINT_COLUMN_SUPPORTED
    try:
        columns = {column["name"] for column in inspect(db.engine).get_columns("pager_devices")}
        supported = "token_fingerprint" in columns
    except Exception:
        supported = False
    with _PAGER_CACHE_LOCK:
        _PAGER_FINGERPRINT_COLUMN_SUPPORTED = supported
    return supported


def _prune_pager_state_locked() -> None:
    now = time.monotonic()
    expired_tokens = [
        fingerprint
        for fingerprint, payload in _PAGER_TOKEN_DEVICE_CACHE.items()
        if float(payload.get("expires_at") or 0) <= now
    ]
    for fingerprint in expired_tokens:
        _PAGER_TOKEN_DEVICE_CACHE.pop(fingerprint, None)
    stale_last_seen = [
        device_id
        for device_id, last_seen in _PAGER_LAST_SEEN_TRACKER.items()
        if (now - float(last_seen or 0)) > (_PAGER_LAST_SEEN_THROTTLE_SECONDS * 4)
    ]
    for device_id in stale_last_seen:
        _PAGER_LAST_SEEN_TRACKER.pop(device_id, None)
    if len(_PAGER_TOKEN_DEVICE_CACHE) <= _PAGER_CACHE_MAX_ENTRIES:
        return
    overflow = len(_PAGER_TOKEN_DEVICE_CACHE) - _PAGER_CACHE_MAX_ENTRIES
    oldest_fingerprints = sorted(
        _PAGER_TOKEN_DEVICE_CACHE,
        key=lambda item: float(_PAGER_TOKEN_DEVICE_CACHE[item].get("expires_at") or 0),
    )[:overflow]
    for fingerprint in oldest_fingerprints:
        _PAGER_TOKEN_DEVICE_CACHE.pop(fingerprint, None)


def _find_pager_device_by_fingerprint(token: str) -> PagerDevice | None:
    fingerprint = fingerprint_pager_token(token)
    if not fingerprint:
        return None
    return (
        PagerDevice.query.options(
            joinedload(PagerDevice.department).load_only(
                Department.id,
                Department.company_id,
                Department.is_active,
                Department.name,
            ),
            noload(PagerDevice.company),
        )
        .filter(
            PagerDevice.token_fingerprint == fingerprint,
            PagerDevice.active.is_(True),
        )
        .order_by(PagerDevice.id.asc())
        .first()
    )


def _find_legacy_pager_device_by_token(token: str) -> PagerDevice | None:
    # Compatibility path for older rows that predate token_fingerprint backfill.
    # Keep this bounded so invalid bearer tokens cannot scan all active devices.
    try:
        fallback_limit = int(current_app.config.get("PAGER_AUTH_LEGACY_FALLBACK_LIMIT", 25))
    except (TypeError, ValueError):
        fallback_limit = 25
    fallback_limit = max(0, min(fallback_limit, 250))
    if fallback_limit <= 0:
        return None

    devices = (
        PagerDevice.query.options(
            joinedload(PagerDevice.department).load_only(Department.id, Department.company_id, Department.is_active, Department.name),
            noload(PagerDevice.company),
        )
        .filter(
            PagerDevice.active.is_(True),
            or_(PagerDevice.token_fingerprint.is_(None), PagerDevice.token_fingerprint == ""),
        )
        .order_by(PagerDevice.id.asc())
        .limit(fallback_limit)
        .all()
    )
    for device in devices:
        if verify_pager_token(device.token_hash, token):
            if not device.department or not device.department.is_active or device.department.company_id != device.company_id:
                continue
            current_app.logger.warning(
                "PAGER_AUTH legacy_fingerprint_fallback pager_device_id=%s company_id=%s department_id=%s",
                device.id,
                device.company_id,
                device.department_id,
            )
            return device
    return None


def _maybe_persist_pager_token_fingerprint(device: PagerDevice, token: str) -> None:
    if not _pager_token_fingerprint_supported():
        return
    expected = fingerprint_pager_token(token)
    if device.token_fingerprint == expected:
        return
    try:
        device.token_fingerprint = expected
        db.session.commit()
    except Exception:
        db.session.rollback()


def _maybe_update_pager_last_seen(device: PagerDevice, update_last_seen: bool) -> None:
    if not update_last_seen:
        return
    now_mono = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    with _PAGER_CACHE_LOCK:
        last_seen = _PAGER_LAST_SEEN_TRACKER.get(device.id, 0.0)
        if (now_mono - last_seen) < _PAGER_LAST_SEEN_THROTTLE_SECONDS:
            return
        _PAGER_LAST_SEEN_TRACKER[device.id] = now_mono
    try:
        db.session.execute(
            update(PagerDevice)
            .where(PagerDevice.id == device.id, PagerDevice.active.is_(True))
            .values(last_seen_at=now_utc)
        )
        db.session.commit()
        g.authenticated_pager_device_last_seen_updated = True
    except Exception:
        db.session.rollback()
        with _PAGER_CACHE_LOCK:
            _PAGER_LAST_SEEN_TRACKER.pop(device.id, None)
        current_app.logger.warning(
            "Pager last-seen update failed pager_device_id=%s department_id=%s company_id=%s",
            getattr(device, "id", None),
            getattr(device, "department_id", None),
            getattr(device, "company_id", None),
            exc_info=True,
        )


def get_authenticated_user() -> User | None:
    started_at = time.perf_counter()
    if not has_request_context():
        return None
    cached = getattr(g, "authenticated_user", None)
    if cached is not None:
        _perf_log("get_authenticated_user(cache)", started_at, user_id=_safe_user_identity_id(cached))
        return cached
    user_id = session.get(USER_SESSION_KEY)
    user = (
        User.query.options(
            load_only(
                User.id,
                User.company_id,
                User.display_name,
                User.username,
                User.role,
                User.email,
                User.is_active,
            ),
            noload("*"),
        )
        .filter_by(id=user_id)
        .one_or_none()
        if user_id
        else None
    )
    g.authenticated_user = user
    _perf_log("get_authenticated_user(db)", started_at, user_id=user_id, found=bool(user))
    return user


def is_authenticated() -> bool:
    user = get_authenticated_user()
    return bool(user and user.is_active)


def login_user(user: User) -> None:
    session.clear()
    session[USER_SESSION_KEY] = user.id
    session.pop(ADMIN_SESSION_KEY, None)
    session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    g.authenticated_user = user


def logout_user() -> None:
    session.clear()
    if has_request_context():
        g.authenticated_user = None
        g.current_user_membership = None
        g.current_company = None
        g.current_companies = None


def authenticate_user(identity: str | None, password: str | None) -> User | None:
    user, _reason = authenticate_user_with_reason(identity, password)
    return user


def authenticate_user_with_reason(identity: str | None, password: str | None) -> tuple[User | None, str]:
    started_at = time.perf_counter()
    normalized_identity = str(identity or "").strip()
    if not normalized_identity:
        _perf_log("authenticate_user(missing_identity)", started_at)
        return None, "missing_identity"
    lookup_started_at = time.perf_counter()
    user = _find_user_by_identity(normalized_identity)
    lookup_ms = (time.perf_counter() - lookup_started_at) * 1000
    if not user:
        _perf_log("authenticate_user(unknown_identity)", started_at, lookup_ms=round(lookup_ms, 1))
        return None, "unknown_identity"
    if not user.is_active:
        _perf_log("authenticate_user(inactive_user)", started_at, lookup_ms=round(lookup_ms, 1), user_id=user.id)
        return None, "inactive_user"
    verify_started_at = time.perf_counter()
    password_ok = user.check_password(password)
    verify_ms = (time.perf_counter() - verify_started_at) * 1000
    _perf_log(
        "authenticate_user(password_verify)",
        started_at,
        lookup_ms=round(lookup_ms, 1),
        verify_ms=round(verify_ms, 1),
        user_id=user.id,
    )
    if not password_ok:
        return None, "invalid_password"
    return user, "ok"


def _find_user_by_identity(identity: str) -> User | None:
    options = (
        load_only(
            User.id,
            User.company_id,
            User.display_name,
            User.username,
            User.role,
            User.password_hash,
            User.is_active,
            User.last_login_at,
        ),
        noload("*"),
    )
    # Email login is intentionally disabled until end-to-end email identity
    # workflows are implemented (verification, uniqueness, and recovery).
    return User.query.options(*options).filter(User.username == identity).one_or_none()


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
        session_memberships = _memberships_from_session(current_user, active_only)
        if session_memberships is not None:
            cache[cache_key] = session_memberships
            _perf_log("get_user_memberships(session)", started_at, total=len(session_memberships), active=len(session_memberships))
            return session_memberships
    memberships = _load_memberships_from_db(current_user.id, active_only)
    filtered = memberships
    if has_request_context():
        g.user_memberships_cache[(current_user.id, bool(active_only))] = filtered
        _store_memberships_in_session(current_user, filtered, active_only)
    _perf_log("get_user_memberships(db)", started_at, total=len(filtered), active=len(filtered))
    return filtered


def _load_memberships_from_db(user_id: int, active_only: bool) -> list[SimpleNamespace]:
    stmt = (
        select(
            UserCompanyAccess.id.label("access_id"),
            UserCompanyAccess.user_id,
            UserCompanyAccess.company_id,
            UserCompanyAccess.role,
            UserCompanyAccess.scope_mode,
            UserCompanyAccess.department_id,
            UserCompanyAccess.machine_group_id,
            UserCompanyAccess.is_active,
            Company.id.label("company_id_value"),
            Company.name.label("company_name"),
            Company.slug.label("company_slug"),
            Company.is_active.label("company_is_active"),
        )
        .select_from(UserCompanyAccess)
        .join(Company, Company.id == UserCompanyAccess.company_id)
        .where(UserCompanyAccess.user_id == user_id, Company.is_active.is_(True))
        .order_by(UserCompanyAccess.company_id.asc())
    )
    if active_only:
        stmt = stmt.where(UserCompanyAccess.is_active.is_(True))

    rows = db.session.execute(stmt).mappings().all()
    memberships = []
    for row in rows:
        company = SimpleNamespace(
            id=row["company_id_value"],
            name=row["company_name"],
            slug=row["company_slug"],
            is_active=row["company_is_active"],
        )
        memberships.append(
            SimpleNamespace(
                id=row["access_id"],
                user_id=row["user_id"],
                company_id=row["company_id"],
                role=row["role"],
                scope_mode=row["scope_mode"],
                department_id=row["department_id"],
                machine_group_id=row["machine_group_id"],
                is_active=row["is_active"],
                is_admin=row["role"] == "Admin",
                is_restricted=row["scope_mode"] == "restricted" and row["role"] != "Admin",
                company=company,
                department=None,
                machine_group=None,
            )
        )
    return memberships


def _memberships_from_session(user: User, active_only: bool) -> list[SimpleNamespace] | None:
    if not active_only:
        return None
    snapshot = session.get(MEMBERSHIPS_SESSION_KEY)
    if not snapshot or snapshot.get("user_id") != user.id:
        return None
    memberships = []
    for item in snapshot.get("memberships") or []:
        company_data = item.get("company") or {}
        company = SimpleNamespace(
            id=company_data.get("id"),
            name=company_data.get("name"),
            slug=company_data.get("slug"),
            is_active=company_data.get("is_active", True),
        )
        memberships.append(
            SimpleNamespace(
                id=item.get("id"),
                user_id=item.get("user_id"),
                company_id=item.get("company_id"),
                role=item.get("role"),
                scope_mode=item.get("scope_mode"),
                department_id=item.get("department_id"),
                machine_group_id=item.get("machine_group_id"),
                is_active=item.get("is_active", True),
                is_admin=item.get("role") == "Admin",
                is_restricted=item.get("scope_mode") == "restricted" and item.get("role") != "Admin",
                company=company,
                department=None,
                machine_group=None,
            )
        )
    return memberships


def _store_memberships_in_session(user: User, memberships: list[UserCompanyAccess], active_only: bool) -> None:
    if not active_only or session.get(USER_SESSION_KEY) != user.id:
        return
    session[MEMBERSHIPS_SESSION_KEY] = {
        "user_id": user.id,
        "memberships": [
            {
                "id": membership.id,
                "user_id": membership.user_id,
                "company_id": membership.company_id,
                "role": membership.role,
                "scope_mode": membership.scope_mode,
                "department_id": membership.department_id,
                "machine_group_id": membership.machine_group_id,
                "is_active": membership.is_active,
                "company": {
                    "id": membership.company.id,
                    "name": membership.company.name,
                    "slug": membership.company.slug,
                    "is_active": membership.company.is_active,
                }
                if membership.company
                else None,
            }
            for membership in memberships
        ],
    }


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
    if cached is not None and company is None:
        if user is None or user.id == session.get(USER_SESSION_KEY):
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
    admin_password = config.get("ADMIN_PASSWORD")
    if not admin_password or len(str(admin_password)) < 12:
        raise RuntimeError("Production requires a strong ANDON_ADMIN_PASSWORD.")


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
    if not is_authenticated():
        request.andon_authorized_company = None
        return None
    company = getattr(request, "andon_authorized_company", None)
    if company is not None:
        return company.id if company else None
    from .company_context import get_current_company

    company = get_current_company()
    request.andon_authorized_company = company
    return company.id if company else None


def get_scope_filters(membership: UserCompanyAccess | None = None) -> dict:
    if membership is None and has_request_context():
        cached_scope = getattr(g, "scope_filters_cache", None)
        if cached_scope is not None:
            return cached_scope

    effective_membership = membership or get_current_membership()
    if effective_membership is None:
        result = {
            "company_id": None,
            "department_id": None,
            "department_ids": [],
            "machine_group_name": None,
            "machine_group_names": [],
            "machine_ids": [],
            "restricted": False,
        }
        if membership is None and has_request_context():
            g.scope_filters_cache = result
        return result

    company_id = effective_membership.company_id
    if not effective_membership.is_restricted:
        result = {
            "company_id": company_id,
            "department_id": None,
            "department_ids": [],
            "machine_group_name": None,
            "machine_group_names": [],
            "machine_ids": [],
            "restricted": False,
        }
        if membership is None and has_request_context():
            g.scope_filters_cache = result
        return result

    current_user = get_authenticated_user()
    access = None
    if current_user is not None:
        access = UserCompanyAccess.query.options(
            noload(UserCompanyAccess.user),
            noload(UserCompanyAccess.company),
            noload(UserCompanyAccess.department),
            noload(UserCompanyAccess.machine_group),
        ).filter_by(user_id=current_user.id, company_id=company_id, is_active=True).one_or_none()

    raw_config = {}
    if access and access.scope_config_json:
        try:
            raw_config = json.loads(access.scope_config_json) if isinstance(access.scope_config_json, str) else {}
        except json.JSONDecodeError:
            raw_config = {}
    elif membership and getattr(membership, "scope_config_json", None):
        try:
            raw_config = json.loads(membership.scope_config_json) if isinstance(membership.scope_config_json, str) else {}
        except json.JSONDecodeError:
            raw_config = {}

    config_machine_ids = sorted({int(item) for item in (raw_config.get("machine_ids") or []) if str(item).isdigit()})
    config_group_ids = sorted({int(item) for item in (raw_config.get("machine_group_ids") or []) if str(item).isdigit()})
    config_department_ids = sorted({int(item) for item in (raw_config.get("department_ids") or []) if str(item).isdigit()})

    legacy_department_ids = [effective_membership.department_id] if effective_membership.department_id is not None else []
    legacy_group_ids = [effective_membership.machine_group_id] if effective_membership.machine_group_id is not None else []
    all_department_ids = sorted(set(legacy_department_ids + config_department_ids))
    all_group_ids = sorted(set(legacy_group_ids + config_group_ids))

    if effective_membership.role == "Viewer":
        valid_department_ids = []
        if all_department_ids:
            valid_department_ids = [
                row.id
                for row in Department.query.options(noload("*")).with_entities(Department.id).filter(
                    Department.company_id == company_id,
                    Department.id.in_(all_department_ids),
                    Department.is_active.is_(True),
                ).all()
                if row.id is not None
            ]
        resolved_department_ids = sorted(set(valid_department_ids))
        result = {
            "company_id": company_id,
            "department_id": resolved_department_ids[0] if len(resolved_department_ids) == 1 else None,
            "department_ids": resolved_department_ids,
            "machine_group_name": None,
            "machine_group_names": [],
            "machine_ids": [],
            "restricted": True,
        }
        if membership is None and has_request_context():
            g.scope_filters_cache = result
        return result

    if effective_membership.role == "Operator" and config_machine_ids:
        scoped_rows = (
            Machine.query.options(noload("*"))
            .with_entities(Machine.id, Machine.department_id, Machine.machine_type)
            .filter(
                Machine.company_id == company_id,
                Machine.id.in_(config_machine_ids),
                Machine.is_active.is_(True),
            )
            .all()
        )
        machine_ids = sorted({row.id for row in scoped_rows if row.id is not None})
        resolved_department_ids = sorted({row.department_id for row in scoped_rows if row.department_id is not None})
        machine_group_names = sorted({row.machine_type for row in scoped_rows if row.machine_type})
        result = {
            "company_id": company_id,
            "department_id": resolved_department_ids[0] if len(resolved_department_ids) == 1 else None,
            "department_ids": resolved_department_ids,
            "machine_group_name": machine_group_names[0] if len(machine_group_names) == 1 else None,
            "machine_group_names": machine_group_names,
            "machine_ids": machine_ids,
            "restricted": True,
        }
        if membership is None and has_request_context():
            g.scope_filters_cache = result
        return result

    machine_query = Machine.query.options(noload("*")).with_entities(
        Machine.id,
        Machine.department_id,
        Machine.machine_type,
    ).filter(Machine.company_id == company_id, Machine.is_active.is_(True))

    candidate_machine_ids = set(config_machine_ids)
    if all_group_ids:
        group_name_rows = (
            MachineGroup.query.options(noload("*"))
            .with_entities(MachineGroup.name)
            .filter(
                MachineGroup.company_id == company_id,
                MachineGroup.id.in_(all_group_ids),
                MachineGroup.is_active.is_(True),
            )
            .all()
        )
        group_names = sorted({row.name for row in group_name_rows if row.name})
    else:
        group_names = []

    scoped_machines = []
    if all_department_ids or group_names:
        scoped_query = machine_query
        if all_department_ids:
            scoped_query = scoped_query.filter(Machine.department_id.in_(all_department_ids))
        if group_names:
            scoped_query = scoped_query.filter(Machine.machine_type.in_(group_names))
        scoped_machines = scoped_query.all()
        candidate_machine_ids.update({row.id for row in scoped_machines if row.id is not None})

    # If no explicit config exists, retain legacy single-scope behavior.
    if not candidate_machine_ids and (legacy_department_ids or legacy_group_ids):
        fallback_query = machine_query
        if legacy_department_ids:
            fallback_query = fallback_query.filter(Machine.department_id.in_(legacy_department_ids))
        if group_names:
            fallback_query = fallback_query.filter(Machine.machine_type.in_(group_names))
        fallback_rows = fallback_query.all()
        candidate_machine_ids.update({row.id for row in fallback_rows if row.id is not None})
        if not all_department_ids:
            all_department_ids = sorted({row.department_id for row in fallback_rows if row.department_id is not None})

    machine_ids = sorted(candidate_machine_ids)
    if machine_ids:
        dept_rows = (
            Machine.query.options(noload("*"))
            .with_entities(Machine.department_id, Machine.machine_type)
            .filter(Machine.company_id == company_id, Machine.id.in_(machine_ids))
            .all()
        )
        machine_group_names = sorted({row.machine_type for row in dept_rows if row.machine_type})
        resolved_department_ids = sorted({row.department_id for row in dept_rows if row.department_id is not None})
    else:
        machine_group_names = group_names
        resolved_department_ids = all_department_ids

    result = {
        "company_id": company_id,
        "department_id": resolved_department_ids[0] if len(resolved_department_ids) == 1 else None,
        "department_ids": resolved_department_ids,
        "machine_group_name": machine_group_names[0] if len(machine_group_names) == 1 else None,
        "machine_group_names": machine_group_names,
        "machine_ids": machine_ids,
        "restricted": True,
    }
    if membership is None and has_request_context():
        g.scope_filters_cache = result
    return result


def get_view_preference(page_key: str, company_id: int | None = None, user: User | None = None) -> dict:
    page_key = _validate_preference_page_key(page_key)
    current_user = user or get_authenticated_user()
    if current_user is None:
        return {}
    if not user_can_access_page(page_key):
        abort(403)
    resolved_company_id = company_id
    if resolved_company_id is None:
        membership = get_current_membership(user=current_user)
        resolved_company_id = membership.company_id if membership else None
    cache_key = None
    if has_request_context():
        cache_key = (current_user.id, resolved_company_id, page_key)
        pref_cache = getattr(g, "view_preferences_cache", None)
        if pref_cache is None:
            pref_cache = {}
            g.view_preferences_cache = pref_cache
        if cache_key in pref_cache:
            return pref_cache[cache_key]
    preference = UserViewPreference.query.filter_by(
        user_id=current_user.id,
        company_id=resolved_company_id,
        page_key=page_key,
    ).one_or_none()
    if preference is None or not preference.preferences_json:
        if has_request_context() and cache_key is not None:
            g.view_preferences_cache[cache_key] = {}
        return {}
    try:
        result = json.loads(preference.preferences_json)
        if has_request_context() and cache_key is not None:
            g.view_preferences_cache[cache_key] = result
        return result
    except json.JSONDecodeError:
        if has_request_context() and cache_key is not None:
            g.view_preferences_cache[cache_key] = {}
        return {}


def save_view_preference(page_key: str, payload: dict, company_id: int | None = None, user: User | None = None) -> dict:
    page_key = _validate_preference_page_key(page_key)
    current_user = user or get_authenticated_user()
    if current_user is None:
        abort(403)
    if not user_can_access_page(page_key):
        abort(403)
    if not isinstance(payload, dict):
        abort(400, description="Invalid preference payload")
    resolved_company_id = company_id
    if resolved_company_id is None:
        membership = get_current_membership(user=current_user)
        resolved_company_id = membership.company_id if membership else None
    preference = UserViewPreference.query.filter_by(
        user_id=current_user.id,
        company_id=resolved_company_id,
        page_key=page_key,
    ).one_or_none()
    serialized = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
    max_bytes = int(current_app.config.get("PREFERENCE_PAYLOAD_MAX_BYTES", 16384))
    if len(serialized.encode("utf-8")) > max_bytes:
        abort(413, description="Preference payload is too large")
    if preference is None:
        preference = UserViewPreference(
            user_id=current_user.id,
            company_id=resolved_company_id,
            page_key=page_key,
            preferences_json=serialized,
        )
        db.session.add(preference)
        db.session.commit()
    else:
        if preference.preferences_json == serialized:
            if has_request_context():
                pref_cache = getattr(g, "view_preferences_cache", None)
                if pref_cache is None:
                    pref_cache = {}
                    g.view_preferences_cache = pref_cache
                pref_cache[(current_user.id, resolved_company_id, page_key)] = payload or {}
            return payload or {}
        preference.preferences_json = serialized
        db.session.commit()
    if has_request_context():
        pref_cache = getattr(g, "view_preferences_cache", None)
        if pref_cache is None:
            pref_cache = {}
            g.view_preferences_cache = pref_cache
        pref_cache[(current_user.id, resolved_company_id, page_key)] = payload or {}
    return payload or {}


def _validate_preference_page_key(page_key: str) -> str:
    normalized = str(page_key or "").strip().lower()
    if normalized not in PREFERENCE_ALLOWED_PAGE_KEYS:
        abort(400, description="Unknown preference page key")
    return normalized
