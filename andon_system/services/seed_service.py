from __future__ import annotations

from ..company_context import ensure_default_companies
from ..extensions import db
from ..models import andon_alert_escalation_map


def seed_default_data():
    _clear_non_company_data()
    ensure_default_companies()
    db.session.commit()


def _clear_non_company_data():
    for table in reversed(db.metadata.sorted_tables):
        if table.name == "companies":
            continue
        if table is andon_alert_escalation_map:
            db.session.execute(andon_alert_escalation_map.delete())
            continue
        db.session.execute(table.delete())
