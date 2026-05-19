from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.company_context import ensure_default_companies
from andon_system.extensions import db
from andon_system.models.company import Company
from andon_system.models.department import Department
from andon_system.models.machine_group import MachineGroup
from andon_system.models.user import User, UserCompanyAccess
from andon_system.services.seed_service import seed_default_data


DEFAULT_MACHINE_GROUPS = ("Press", "Converting", "Extrusion")

SEEDED_USERS = [
    {
        "username": "admin.demo",
        "email": "admin.demo@processguard.local",
        "display_name": "Demo Admin",
        "employee_id": "A100",
        "phone_number": "555-0100",
        "password": "AdminDemo!2026",
        "memberships": [
            {"company_slug": "starpak", "role": "Admin", "scope_mode": "all"},
            {"company_slug": "five-star", "role": "Admin", "scope_mode": "all"},
        ],
    },
    {
        "username": "manager.demo",
        "email": "manager.demo@processguard.local",
        "display_name": "Demo Manager",
        "employee_id": "M210",
        "phone_number": "555-0210",
        "password": "ManagerDemo!2026",
        "memberships": [
            {
                "company_slug": "starpak",
                "role": "Manager",
                "scope_mode": "restricted",
                "department_name": "Maintenance",
                "machine_group_name": "Press",
            },
            {
                "company_slug": "polytex",
                "role": "Manager",
                "scope_mode": "all",
            },
        ],
    },
    {
        "username": "operator.demo",
        "email": "operator.demo@processguard.local",
        "display_name": "Demo Operator",
        "employee_id": "O310",
        "phone_number": "555-0310",
        "password": "OperatorDemo!2026",
        "memberships": [
            {
                "company_slug": "starpak",
                "role": "Operator",
                "scope_mode": "restricted",
                "department_name": "Quality",
                "machine_group_name": "Press",
            },
        ],
    },
    {
        "username": "viewer.demo",
        "email": "viewer.demo@processguard.local",
        "display_name": "Line Manager",
        "employee_id": "M410",
        "phone_number": "555-0410",
        "password": "ViewerDemo!2026",
        "memberships": [
            {
                "company_slug": "five-star",
                "role": "Manager",
                "scope_mode": "restricted",
                "department_name": "Shipping",
                "machine_group_name": "Converting",
            },
        ],
    },
]


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        ensure_default_companies()
        seed_default_data()
        _ensure_machine_groups()
        _seed_auth_users()
        db.session.commit()
    print("Seeded login accounts and company memberships.")


def _ensure_machine_groups():
    companies = Company.query.filter_by(is_active=True).order_by(Company.id.asc()).all()
    for company in companies:
        for group_name in DEFAULT_MACHINE_GROUPS:
            group = MachineGroup.query.filter_by(company_id=company.id, name=group_name).one_or_none()
            if group is None:
                db.session.add(MachineGroup(company_id=company.id, name=group_name, is_active=True))
    db.session.flush()


def _seed_auth_users():
    companies_by_slug = {company.slug: company for company in Company.query.filter_by(is_active=True).all()}
    for spec in SEEDED_USERS:
        first_membership = spec["memberships"][0]
        primary_company = companies_by_slug[first_membership["company_slug"]]
        user = User.query.filter_by(username=spec["username"]).one_or_none()
        if user is None:
            user = User(
                username=spec["username"],
                company_id=primary_company.id,
                role=first_membership["role"],
                is_active=True,
            )
            db.session.add(user)

        user.display_name = spec["display_name"]
        user.email = spec["email"]
        user.employee_id = spec["employee_id"]
        user.phone_number = spec["phone_number"]
        user.is_active = True
        user.set_password(spec["password"])
        user.company_id = primary_company.id
        user.role = first_membership["role"]
        user.department_id = None
        user.machine_group_id = None
        db.session.flush()

        for membership_spec in spec["memberships"]:
            company = companies_by_slug[membership_spec["company_slug"]]
            department = _lookup_department(company.id, membership_spec.get("department_name"))
            machine_group = _lookup_machine_group(company.id, membership_spec.get("machine_group_name"))
            access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company.id).one_or_none()
            if access is None:
                access = UserCompanyAccess(user_id=user.id, company_id=company.id)
                db.session.add(access)
            access.role = membership_spec["role"]
            access.scope_mode = membership_spec["scope_mode"]
            access.department_id = department.id if department else None
            access.machine_group_id = machine_group.id if machine_group else None
            access.is_active = True

            if company.id == primary_company.id:
                user.department_id = access.department_id
                user.machine_group_id = access.machine_group_id

        db.session.flush()


def _lookup_department(company_id: int, name: str | None):
    if not name:
        return None
    return Department.query.filter_by(company_id=company_id, name=name).one_or_none()


def _lookup_machine_group(company_id: int, name: str | None):
    if not name:
        return None
    return MachineGroup.query.filter_by(company_id=company_id, name=name).one_or_none()


if __name__ == "__main__":
    main()
