from __future__ import annotations

from sqlalchemy import inspect, text

from .extensions import db
from .company_context import ensure_default_companies
from .models.company import Company


def ensure_andon_schema():
    engine = db.engine
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "companies" not in tables:
        Company.__table__.create(bind=engine, checkfirst=True)
        tables = set(inspector.get_table_names())

    if "companies" in tables:
        ensure_default_companies()

    with engine.begin() as connection:
        _add_column_if_missing(connection, inspector, "machines", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "machines", "machine_type", "VARCHAR(80)")
        _add_column_if_missing(connection, inspector, "departments", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "machine_groups", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "users", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "users", "email", "VARCHAR(160)")
        _add_column_if_missing(connection, inspector, "users", "phone_number", "VARCHAR(32)")
        _add_column_if_missing(connection, inspector, "users", "machine_group_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "issue_categories", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "issue_problems", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "andon_alerts", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "andon_alerts", "acknowledged_seconds", "INTEGER")
        _add_column_if_missing(connection, inspector, "andon_alerts", "ack_to_clear_seconds", "INTEGER")
        _add_column_if_missing(connection, inspector, "andon_alert_events", "company_id", "INTEGER")
        _add_column_if_missing(connection, inspector, "escalation_rules", "company_id", "INTEGER")

    _backfill_machine_types()
    _backfill_company_ids()
    _backfill_alert_durations()


def _backfill_machine_types():
    rows = db.session.execute(text("SELECT id, machine_code, name, machine_type FROM machines")).all()
    updated = False
    for row in rows:
        if row.machine_type:
          continue
        machine_type = _infer_machine_type(row.machine_code or "", row.name or "")
        if machine_type:
            db.session.execute(
                text("UPDATE machines SET machine_type = :machine_type WHERE id = :id"),
                {"machine_type": machine_type, "id": row.id},
            )
            updated = True
    if updated:
        db.session.commit()


def _backfill_company_ids():
    if "companies" not in set(inspect(db.engine).get_table_names()):
        return
    company_row = db.session.execute(text("SELECT id FROM companies WHERE slug = 'starpak' ORDER BY id ASC LIMIT 1")).first()
    if company_row is None:
        company_row = db.session.execute(text("SELECT id FROM companies ORDER BY id ASC LIMIT 1")).first()
    if company_row is None:
        return
    company_id = company_row.id
    tables = set(inspect(db.engine).get_table_names())
    for table in [
        "departments",
        "machine_groups",
        "machines",
        "users",
        "issue_categories",
        "issue_problems",
        "andon_alerts",
        "andon_alert_events",
        "escalation_rules",
    ]:
        if table not in tables:
            continue
        columns = {column["name"] for column in inspect(db.engine).get_columns(table)}
        if "company_id" not in columns:
            continue
        db.session.execute(text(f"UPDATE {table} SET company_id = :company_id WHERE company_id IS NULL"), {"company_id": company_id})
    db.session.commit()


def _backfill_alert_durations():
    tables = set(inspect(db.engine).get_table_names())
    if "andon_alerts" not in tables:
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("andon_alerts")}
    if "acknowledged_seconds" not in columns and "ack_to_clear_seconds" not in columns:
        return

    rows = db.session.execute(
        text(
            """
            SELECT id, created_at, acknowledged_at, resolved_at, cancelled_at, acknowledged_seconds, ack_to_clear_seconds
            FROM andon_alerts
            """
        )
    ).all()
    updated = False
    for row in rows:
        acknowledged_seconds = row.acknowledged_seconds
        if acknowledged_seconds is None and row.created_at and row.acknowledged_at:
            acknowledged_seconds = int((row.acknowledged_at - row.created_at).total_seconds())
            db.session.execute(
                text("UPDATE andon_alerts SET acknowledged_seconds = :value WHERE id = :id"),
                {"value": acknowledged_seconds, "id": row.id},
            )
            updated = True

        clear_at = row.resolved_at or row.cancelled_at
        ack_to_clear_seconds = row.ack_to_clear_seconds
        if ack_to_clear_seconds is None and row.acknowledged_at and clear_at:
            ack_to_clear_seconds = int((clear_at - row.acknowledged_at).total_seconds())
            db.session.execute(
                text("UPDATE andon_alerts SET ack_to_clear_seconds = :value WHERE id = :id"),
                {"value": ack_to_clear_seconds, "id": row.id},
            )
            updated = True

    if updated:
        db.session.commit()


def _add_column_if_missing(connection, inspector, table_name, column_name, column_type):
    columns = {column["name"] for column in inspector.get_columns(table_name)} if table_name in set(inspector.get_table_names()) else set()
    if column_name not in columns:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))


def _infer_machine_type(machine_code: str, name: str) -> str | None:
    combined = f"{machine_code} {name}".upper()
    if "PRESS" in combined:
        return "Press"
    if "EXTRUSION" in combined:
        return "Extrusion"
    if "SLITTER" in combined:
        return "Slitter"
    if "BAG" in combined:
        return "Bag Machine"
    return None
