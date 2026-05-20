from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("FLASK_CONFIG", "development")
os.environ.setdefault("LOCAL_SQLITE_FALLBACK", "true")

from andon_system import create_app
from andon_system.company_context import ensure_default_companies
from andon_system.extensions import db
from andon_system.models.company import Company
from andon_system.models.department import Department
from andon_system.models.machine import Machine
from andon_system.models.machine_group import MachineGroup
from andon_system.models.user import User, UserCompanyAccess
from andon_system.services.seed_service import seed_default_data


DEFAULT_GROUPS = ("Press", "Converting", "Extrusion")
DEMO_PASSWORDS = {
    "admin.demo": "AdminDemo!2026",
    "manager.demo": "ManagerDemo!2026",
    "operator.demo": "OperatorDemo!2026",
    "viewer.demo": "ViewerDemo!2026",
}


def main():
    app = create_app("development")
    with app.app_context():
        # Local bootstrap is intentionally resettable; deployment uses PostgreSQL
        # and should use migrations/seed scripts instead.
        db.drop_all()
        db.create_all()
        ensure_default_companies()
        seed_default_data()
        _ensure_machine_groups()
        _ensure_demo_machines()
        _ensure_demo_users()
        db.session.commit()
        database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    print(f"Initialized local SQLite database: {database_uri}")
    print("Demo logins:")
    for username, password in DEMO_PASSWORDS.items():
        print(f"  {username} / {password}")


def _ensure_machine_groups():
    for company in Company.query.filter_by(is_active=True).all():
        for group_name in DEFAULT_GROUPS:
            group = MachineGroup.query.filter_by(company_id=company.id, name=group_name).one_or_none()
            if group is None:
                db.session.add(MachineGroup(company_id=company.id, name=group_name, is_active=True))
    db.session.flush()


def _ensure_demo_machines():
    for company in Company.query.filter_by(is_active=True).all():
        departments = {department.name: department for department in Department.query.filter_by(company_id=company.id).all()}
        specs = [
            ("P-100", "Press 100", "Press", "Maintenance"),
            ("P-200", "Press 200", "Press", "Quality"),
            ("C-100", "Converting 100", "Converting", "Shipping"),
            ("E-100", "Extrusion 100", "Extrusion", "Materials"),
        ]
        for code, name, machine_type, department_name in specs:
            machine = Machine.query.filter_by(company_id=company.id, machine_code=code).one_or_none()
            if machine is None:
                machine = Machine(company_id=company.id, machine_code=code)
                db.session.add(machine)
            machine.name = name
            machine.machine_type = machine_type
            machine.department_id = departments.get(department_name).id if departments.get(department_name) else None
            machine.is_active = True
    db.session.flush()


def _ensure_demo_users():
    companies = {company.slug: company for company in Company.query.filter_by(is_active=True).all()}
    specs = [
        {
            "username": "admin.demo",
            "email": "admin.demo@processguard.local",
            "display_name": "Demo Admin",
            "password": DEMO_PASSWORDS["admin.demo"],
            "memberships": [
                ("starpak", "Admin", "all", None, None),
                ("five-star", "Admin", "all", None, None),
            ],
        },
        {
            "username": "manager.demo",
            "email": "manager.demo@processguard.local",
            "display_name": "Demo Manager",
            "password": DEMO_PASSWORDS["manager.demo"],
            "memberships": [
                ("starpak", "Manager", "restricted", "Maintenance", "Press"),
            ],
        },
        {
            "username": "operator.demo",
            "email": "operator.demo@processguard.local",
            "display_name": "Demo Operator",
            "password": DEMO_PASSWORDS["operator.demo"],
            "memberships": [
                ("starpak", "Operator", "restricted", "Quality", "Press"),
            ],
        },
        {
            "username": "viewer.demo",
            "email": "viewer.demo@processguard.local",
            "display_name": "Line Manager",
            "password": DEMO_PASSWORDS["viewer.demo"],
            "memberships": [
                ("five-star", "Viewer", "restricted", "Shipping", "Converting"),
            ],
        },
    ]
    for spec in specs:
        primary_company = companies[spec["memberships"][0][0]]
        user = User.query.filter_by(username=spec["username"]).one_or_none()
        if user is None:
            user = User(username=spec["username"], company_id=primary_company.id, role=spec["memberships"][0][1], is_active=True)
            db.session.add(user)
        user.email = spec["email"]
        user.display_name = spec["display_name"]
        user.role = spec["memberships"][0][1]
        user.company_id = primary_company.id
        user.is_active = True
        user.set_password(spec["password"])
        db.session.flush()

        for company_slug, role, scope_mode, department_name, machine_group_name in spec["memberships"]:
            company = companies[company_slug]
            department = Department.query.filter_by(company_id=company.id, name=department_name).one_or_none() if department_name else None
            group = MachineGroup.query.filter_by(company_id=company.id, name=machine_group_name).one_or_none() if machine_group_name else None
            access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company.id).one_or_none()
            if access is None:
                access = UserCompanyAccess(user_id=user.id, company_id=company.id)
                db.session.add(access)
            access.role = role
            access.scope_mode = scope_mode
            access.department_id = department.id if department else None
            access.machine_group_id = group.id if group else None
            access.is_active = True
            if company.id == primary_company.id:
                user.department_id = access.department_id
                user.machine_group_id = access.machine_group_id
        db.session.flush()


if __name__ == "__main__":
    main()
