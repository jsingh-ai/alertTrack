from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from flask import g, has_request_context, session

from .extensions import db
from .models.company import Company

DEFAULT_COMPANY_SLUG = "starpak"
DEFAULT_COMPANIES = [
    SimpleNamespace(id=1, name="Five Star", slug="five-star", is_active=True),
    SimpleNamespace(id=2, name="Polytex", slug="polytex", is_active=True),
    SimpleNamespace(id=3, name="Starpak", slug="starpak", is_active=True),
    SimpleNamespace(id=4, name="Ultrapak", slug="ultrapak", is_active=True),
    SimpleNamespace(id=5, name="Superbag", slug="superbag", is_active=True),
]

_COMPANIES_TABLE_EXISTS: bool | None = None


def get_companies():
    if has_request_context():
        cached = getattr(g, "current_companies", None)
        if cached is not None:
            return cached
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
    return companies


def get_current_company():
    if has_request_context():
        company = getattr(g, "current_company", None)
        if company is not None:
            return company
    if not _companies_table_exists():
        if has_request_context():
            slug = session.get("andon_company_slug") or DEFAULT_COMPANY_SLUG
        else:
            slug = DEFAULT_COMPANY_SLUG
        company = next((company for company in DEFAULT_COMPANIES if company.slug == slug), DEFAULT_COMPANIES[2])
        if has_request_context():
            g.current_company = company
        return company

    company = None
    if has_request_context():
        slug = session.get("andon_company_slug")
        if slug:
            try:
                company = Company.query.filter_by(slug=slug, is_active=True).one_or_none()
            except OperationalError:
                company = None

    if company is None:
        try:
            company = Company.query.filter_by(slug=DEFAULT_COMPANY_SLUG, is_active=True).one_or_none()
        except OperationalError:
            company = None
    if company is None:
        try:
            company = Company.query.filter_by(is_active=True).order_by(Company.name.asc()).first()
        except OperationalError:
            company = None
    if company is None:
        company = next((item for item in DEFAULT_COMPANIES if item.slug == DEFAULT_COMPANY_SLUG), DEFAULT_COMPANIES[2])

    if has_request_context():
        g.current_company = company
        if company is not None:
            session["andon_company_slug"] = company.slug
    return company


def get_current_company_id():
    company = get_current_company()
    return company.id if company else None


def set_current_company_slug(slug: str | None):
    if not slug:
        return None
    fallback = next((company for company in DEFAULT_COMPANIES if company.slug == slug), None)
    try:
        company = Company.query.filter_by(slug=slug, is_active=True).one_or_none()
    except OperationalError:
        company = None
    if company is None:
        company = fallback
    if company is None:
        return None
    if has_request_context():
        session["andon_company_slug"] = company.slug
        g.current_company = company
    return company


def ensure_default_companies():
    if not _companies_table_exists():
        return
    defaults = [
        ("Five Star", "five-star"),
        ("Polytex", "polytex"),
        ("Starpak", "starpak"),
        ("Ultrapak", "ultrapak"),
        ("Superbag", "superbag"),
    ]
    created = False
    for name, slug in defaults:
        try:
            company = Company.query.filter_by(slug=slug).one_or_none()
        except OperationalError:
            return
        if company is None:
            company = Company(name=name, slug=slug, is_active=True)
            from .extensions import db

            db.session.add(company)
            created = True
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
