from pathlib import Path
import sys

from sqlalchemy import inspect


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.extensions import db
from andon_system.models import AndonAlert, Machine
from andon_system.services.radius_service import resolve_radius_machine_id
from andon_system.services.seed_service import seed_default_data


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        _ensure_machine_radius_column()
        _backfill_machine_radius_ids()
        for index in AndonAlert.__table__.indexes:
            index.create(bind=db.engine, checkfirst=True)
        for index in Machine.__table__.indexes:
            index.create(bind=db.engine, checkfirst=True)
        seed_default_data()
    print("Initialized Andon database.")


def _ensure_machine_radius_column():
    inspector = inspect(db.engine)
    columns = {column["name"] for column in inspector.get_columns("machines")}
    if "radius_machine_id" not in columns:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE machines ADD COLUMN radius_machine_id INTEGER")


def _backfill_machine_radius_ids():
    updated = False
    for machine in Machine.query.filter(Machine.radius_machine_id.is_(None)).all():
        radius_machine_id = resolve_radius_machine_id(machine)
        if radius_machine_id is None:
            continue
        machine.radius_machine_id = radius_machine_id
        updated = True
    if updated:
        db.session.commit()


if __name__ == "__main__":
    main()
