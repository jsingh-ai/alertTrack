from __future__ import annotations

import secrets
from hmac import compare_digest
from urllib.parse import urlparse

from flask import abort, current_app, has_request_context, request, session

ADMIN_SESSION_KEY = "andon_admin_authenticated"
CSRF_SESSION_KEY = "andon_csrf_token"
DEV_SECRET_KEY = "dev-andon-secret-key"
MIN_ADMIN_PASSWORD_LENGTH = 12
WEAK_ADMIN_PASSWORDS = {
    "123",
    "123456",
    "password",
    "admin",
    "changeme",
    "change-this-password",
    "dev-admin-password",
}


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
    if request.blueprint == "admin" and not is_admin_authenticated():
        return
    if not validate_csrf_request():
        abort(400, description="CSRF validation failed")


def is_admin_authenticated() -> bool:
    return bool(session.get(ADMIN_SESSION_KEY))


def set_admin_authenticated(value: bool) -> None:
    if value:
        session[ADMIN_SESSION_KEY] = True
    else:
        session.pop(ADMIN_SESSION_KEY, None)


def validate_admin_password(password: str | None) -> bool:
    expected = current_app.config.get("ADMIN_PASSWORD")
    if not expected:
        return False
    return compare_digest(str(password or ""), str(expected))


def is_weak_admin_password(password: str | None) -> bool:
    normalized = str(password or "").strip()
    if len(normalized) < MIN_ADMIN_PASSWORD_LENGTH:
        return True
    return normalized.lower() in WEAK_ADMIN_PASSWORDS


def validate_production_security_config(config: dict) -> None:
    secret_key = config.get("SECRET_KEY")
    admin_password = config.get("ADMIN_PASSWORD")
    if not secret_key or str(secret_key).strip() == DEV_SECRET_KEY:
        raise RuntimeError("Production requires a non-default SECRET_KEY.")
    if not admin_password or is_weak_admin_password(admin_password):
        raise RuntimeError("Production requires a strong ANDON_ADMIN_PASSWORD.")


def require_admin_authentication() -> None:
    if is_admin_authenticated():
        return
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
