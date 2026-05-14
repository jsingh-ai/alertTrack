from __future__ import annotations

from ..company_context import ensure_default_companies
from ..extensions import db


def seed_default_data():
    ensure_default_companies()
    db.session.commit()
