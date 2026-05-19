from __future__ import annotations

import time
from types import SimpleNamespace

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from flask import current_app, g, has_request_context, session

from .extensions import db
from .models.company import Company
from .security import can_access_company, get_accessible_companies, is_authenticated

CURRENT_COMPANY_ID_SESSION_KEY = "andon_company_id"
CURRENT_COMPANY_SLUG_SESSION_KEY = "andon_company_slug"
DEFAULT_COMPANY_SLUG = "starpak"
DEFAULT_COMPANIES = [
    SimpleNamespace(id=1, name="Five Star", slug="five-star", is_active=True),
    SimpleNamespace(id=2, name="Polytex", slug="polytex", is_active=True),
    SimpleNamespace(id=3, name="Starpak", slug="starpak", is_active=True),
    SimpleNamespace(id=4, name="Superbag", slug="superbag", is_active=True),
    SimpleNamespace(id=5, name="Ultrapak", slug="ultrapak", is_active=True),
]

_COMPANIES_TABLE_EXISTS: bool | None = None


def _perf_enabled() -> bool:
    return has_request_context() and bool(current_app.config.get("ANDON_PERF_LOGS"))


def _perf_log(event: str, started_at: float, **extra) -> None:
    if not _perf_enabled():
        return
    duration_ms = (time.perf_counter() - started_at) * 1000
    suffix = " ".join(f"{key}={value}" for key, value in extra.items())
    current_app.logger.debug("PERF %s duration_ms=%.1f %s", event, duration_ms, suffix)


def get_companies():
    started_at = time.perf_counter()
    if has_request_context():
        cached = getattr(g, "current_companies", None)
        if cached is not None:
            _perf_log("get_companies(cache)", started_at, count=len(cached))
            return cached
        if not is_authenticated():
            companies = DEFAULT_COMPANIES
            g.current_companies = companies
            _perf_log("get_companies(default_unauth)", started_at, count=len(companies))
            return companies
        if is_authenticated():
            companies = get_accessible_companies()
            g.current_companies = companies
            _perf_log("get_companies(auth_memberships)", started_at, count=len(companies))
            return companies
    if not _companies_table_exists():
        companies = DEFAULT_COMPANIES
        if has_request_context():
            g.current_companies = companies
        return companies
    try:
        companies = Company.query.filter_by(is_active=True).order_by(Company.name.asc()).all()
    except OperationalError:
        companies = DEFAULT_COMPANIES
    companies = companies or DEFAULT_COMPANIES
    if has_request_context():
        g.current_companies = companies
    _perf_log("get_companies(db_or_default)", started_at, count=len(companies))
    return companies


def get_current_company():
    started_at = time.perf_counter()
    if has_request_context():
        company = getattr(g, "current_company", None)
        if company is not None:
            _perf_log("get_current_company(cache)", started_at, slug=getattr(company, "slug", None))
            return company
        if not is_authenticated():
            company_id = session.get(CURRENT_COMPANY_ID_SESSION_KEY)
            slug = session.get(CURRENT_COMPANY_SLUG_SESSION_KEY) or DEFAULT_COMPANY_SLUG
            company = None
            if company_id is not None:
                try:
                    normalized_id = int(company_id)
                except (TypeError, ValueError):
                    normalized_id = None
                if normalized_id is not None:
                    company = next((item for item in DEFAULT_COMPANIES if item.id == normalized_id), None)
            if company is None:
                company = next((item for item in DEFAULT_COMPANIES if item.slug == slug), DEFAULT_COMPANIES[2])
            g.current_company = company
            session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
            session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
            _perf_log("get_current_company(default_unauth)", started_at, slug=getattr(company, "slug", None))
            return company
    if not _companies_table_exists():
        if has_request_context():
            company_id = session.get(CURRENT_COMPANY_ID_SESSION_KEY)
            slug = session.get(CURRENT_COMPANY_SLUG_SESSION_KEY) or DEFAULT_COMPANY_SLUG
        else:
            company_id = None
            slug = DEFAULT_COMPANY_SLUG
        company = None
        if company_id is not None:
            try:
                normalized_id = int(company_id)
            except (TypeError, ValueError):
                normalized_id = None
            if normalized_id is not None:
                company = next((item for item in DEFAULT_COMPANIES if item.id == normalized_id), None)
        if company is None:
            company = next((item for item in DEFAULT_COMPANIES if item.slug == slug), DEFAULT_COMPANIES[2])
        if has_request_context():
            g.current_company = company
        _perf_log("get_current_company(default_no_table)", started_at, slug=getattr(company, "slug", None))
        return company

    # For authenticated users, prefer membership-resolved companies first.
    # This avoids additional Company table lookups on every request.
    if has_request_context() and is_authenticated():
        memberships = get_accessible_companies()
        if memberships:
            session_company_id = session.get(CURRENT_COMPANY_ID_SESSION_KEY)
            session_slug = session.get(CURRENT_COMPANY_SLUG_SESSION_KEY)
            company = None
            if session_company_id is not None:
                try:
                    normalized_id = int(session_company_id)
                except (TypeError, ValueError):
                    normalized_id = None
                if normalized_id is not None:
                    company = next((item for item in memberships if item.id == normalized_id), None)
            if company is None:
                company = next((item for item in memberships if item.slug == session_slug), None)
            if company is None:
                company = next((item for item in memberships if item.slug == DEFAULT_COMPANY_SLUG), memberships[0])
            g.current_company = company
            session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
            session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
            _perf_log("get_current_company(memberships)", started_at, slug=getattr(company, "slug", None), count=len(memberships))
            return company

    company = None
    requested_slug = None
    requested_company_id = None
    if has_request_context():
        slug = session.get(CURRENT_COMPANY_SLUG_SESSION_KEY)
        requested_company_id = session.get(CURRENT_COMPANY_ID_SESSION_KEY)
        requested_slug = slug
        if requested_company_id is not None:
            try:
                normalized_id = int(requested_company_id)
            except (TypeError, ValueError):
                normalized_id = None
            if normalized_id is not None:
                try:
                    company = Company.query.filter_by(id=normalized_id, is_active=True).one_or_none()
                except OperationalError:
                    company = None
        if company is None and slug:
            try:
                company = Company.query.filter_by(slug=slug, is_active=True).one_or_none()
            except OperationalError:
                company = None

    if company is None and requested_slug != DEFAULT_COMPANY_SLUG:
        try:
            company = Company.query.filter_by(slug=DEFAULT_COMPANY_SLUG, is_active=True).one_or_none()
        except OperationalError:
            company = None
    if company is None:
        try:
            company = Company.query.filter_by(is_active=True).order_by(Company.name.asc()).first()
        except OperationalError:
            company = None
    if has_request_context() and is_authenticated():
        memberships = get_accessible_companies()
        if memberships and (company is None or all(item.id != company.id for item in memberships)):
            company = memberships[0]
    if company is None:
        company = next((item for item in DEFAULT_COMPANIES if item.slug == DEFAULT_COMPANY_SLUG), DEFAULT_COMPANIES[2])

    if has_request_context():
        g.current_company = company
        if company is not None:
            session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
            session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
    _perf_log("get_current_company(db_or_default)", started_at, slug=getattr(company, "slug", None))
    return company


def get_current_company_id():
    company = get_current_company()
    return company.id if company else None


def set_current_company_slug(slug: str | None):
    if not slug:
        return None
    if has_request_context() and is_authenticated():
        memberships = get_accessible_companies()
        company = next((item for item in memberships if item.slug == slug), None)
        if company is None:
            return None
        session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
        session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
        g.current_company = company
        return company
    fallback = next((company for company in DEFAULT_COMPANIES if company.slug == slug), None)
    try:
        company = Company.query.filter_by(slug=slug, is_active=True).one_or_none()
    except OperationalError:
        company = None
    if company is None:
        company = fallback
    if company is None:
        return None
    if has_request_context() and is_authenticated() and not can_access_company(company):
        return None
    if has_request_context():
        session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
        session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
        g.current_company = company
    return company


def set_current_company_id(company_id: int | str | None):
    if company_id in {None, ""}:
        return None
    try:
        normalized_id = int(company_id)
    except (TypeError, ValueError):
        return None
    if has_request_context() and is_authenticated():
        memberships = get_accessible_companies()
        company = next((item for item in memberships if item.id == normalized_id), None)
        if company is None:
            return None
        session[CURRENT_COMPANY_ID_SESSION_KEY] = company.id
        session[CURRENT_COMPANY_SLUG_SESSION_KEY] = company.slug
        g.current_company = company
        return company
    fallback = next((item for item in DEFAULT_COMPANIES if item.id == normalized_id), None)
    if fallback is None:
        return None
    if has_request_context():
        session[CURRENT_COMPANY_ID_SESSION_KEY] = fallback.id
        session[CURRENT_COMPANY_SLUG_SESSION_KEY] = fallback.slug
        g.current_company = fallback
    return fallback


def ensure_default_companies():
    if not _companies_table_exists():
        return
    defaults = [
        (1, "Five Star", "five-star"),
        (2, "Polytex", "polytex"),
        (3, "Starpak", "starpak"),
        (4, "Superbag", "superbag"),
        (5, "Ultrapak", "ultrapak"),
    ]
    created = False
    ultrapak = None
    try:
        ultrapak = Company.query.filter_by(slug="ultrapak").one_or_none()
    except OperationalError:
        return

    superbag = None
    try:
        superbag = Company.query.filter_by(slug="superbag").one_or_none()
    except OperationalError:
        return

    if ultrapak is not None and superbag is None:
        ultrapak.name = "Superbag"
        ultrapak.slug = "superbag"
        superbag = ultrapak
        created = True

    for company_id, name, slug in defaults:
        try:
            company = Company.query.filter_by(slug=slug).one_or_none()
        except OperationalError:
            return
        if company is None:
            company = Company(id=company_id, name=name, slug=slug, is_active=True)
            from .extensions import db

            db.session.add(company)
            created = True
        else:
            if company.name != name:
                company.name = name
                created = True
            if company.id == company_id:
                continue
    if created:
        from .extensions import db

        db.session.commit()


def _companies_table_exists() -> bool:
    global _COMPANIES_TABLE_EXISTS
    if _COMPANIES_TABLE_EXISTS is not None:
        return _COMPANIES_TABLE_EXISTS
    try:
        _COMPANIES_TABLE_EXISTS = "companies" in inspect(db.engine).get_table_names()
    except Exception:
        _COMPANIES_TABLE_EXISTS = False
    return _COMPANIES_TABLE_EXISTS
