from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import update


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.extensions import db
from andon_system.models.alert import AndonAlert, AndonAlertEvent
from andon_system.models.company import Company
from andon_system.models.department import Department
from andon_system.models.user import User, UserBoard, UserBoardItem, UserCompanyAccess, UserViewPreference


TARGET_USERS = [
    {
        "username": "admin.demo",
        "password": "AdminDemo!2026",
        "display_name": "Demo Admin",
        "email": "admin.demo@processguard.local",
        "role": "Admin",
    },
    {
        "username": "manager.demo",
        "password": "ManagerDemo!2026",
        "display_name": "Demo Manager",
        "email": "manager.demo@processguard.local",
        "role": "Manager",
    },
    {
        "username": "operator.demo",
        "password": "OperatorDemo!2026",
        "display_name": "Demo Operator",
        "email": "operator.demo@processguard.local",
        "role": "Operator",
    },
    {
        "username": "department.demo",
        "password": "DepartmentDemo!2026",
        "display_name": "Demo Department",
        "email": "department.demo@processguard.local",
        "role": "Viewer",
    },
]
TARGET_COMPANY_SLUG = "starpak"


def _delete_only_target_users() -> None:
    usernames = [spec["username"] for spec in TARGET_USERS]
    users = User.query.filter(User.username.in_(usernames)).all()
    if not users:
        return

    user_ids = [user.id for user in users]

    db.session.execute(
        update(AndonAlert)
        .where(AndonAlert.operator_user_id.in_(user_ids))
        .values(operator_user_id=None, operator_name_text=None)
    )
    db.session.execute(
        update(AndonAlert)
        .where(AndonAlert.responder_user_id.in_(user_ids))
        .values(responder_user_id=None, responder_name_text=None)
    )
    db.session.execute(
        update(AndonAlertEvent)
        .where(AndonAlertEvent.user_id.in_(user_ids))
        .values(user_id=None)
    )

    db.session.query(UserCompanyAccess).filter(UserCompanyAccess.user_id.in_(user_ids)).delete(synchronize_session=False)
    db.session.query(UserViewPreference).filter(UserViewPreference.user_id.in_(user_ids)).delete(synchronize_session=False)
    db.session.query(UserBoardItem).filter(
        UserBoardItem.board_id.in_(
            db.session.query(UserBoard.id).filter(UserBoard.user_id.in_(user_ids))
        )
    ).delete(synchronize_session=False)
    db.session.query(UserBoard).filter(UserBoard.user_id.in_(user_ids)).delete(synchronize_session=False)
    db.session.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.session.flush()


def _create_target_users() -> None:
    primary_company = Company.query.filter_by(slug=TARGET_COMPANY_SLUG, is_active=True).one_or_none()
    if primary_company is None:
        raise RuntimeError(f"Active company with slug '{TARGET_COMPANY_SLUG}' was not found.")
    first_department = (
        Department.query.filter_by(company_id=primary_company.id, is_active=True)
        .order_by(Department.id.asc())
        .one_or_none()
    )

    for spec in TARGET_USERS:
        role = spec["role"]
        user = User(
            company_id=primary_company.id,
            username=spec["username"],
            display_name=spec["display_name"],
            email=spec["email"],
            role=role,
            is_active=True,
        )
        user.set_password(spec["password"])
        if role == "Viewer" and first_department is not None:
            user.department_id = first_department.id
        db.session.add(user)
        db.session.flush()

        if role == "Admin":
            access = UserCompanyAccess(
                user_id=user.id,
                company_id=primary_company.id,
                role="Admin",
                scope_mode="all",
                scope_config_json="{}",
                is_active=True,
            )
            db.session.add(access)
            continue

        scope_mode = "restricted" if role == "Viewer" else "all"
        department_id = first_department.id if role == "Viewer" and first_department is not None else None
        scope_config = {"department_ids": [department_id]} if department_id else {}
        access = UserCompanyAccess(
            user_id=user.id,
            company_id=primary_company.id,
            role=role,
            scope_mode=scope_mode,
            department_id=department_id,
            scope_config_json=json.dumps(scope_config, separators=(",", ":"), sort_keys=True),
            is_active=True,
        )
        db.session.add(access)

    db.session.flush()


def main() -> None:
    app = create_app()
    with app.app_context():
        db.create_all()
        _delete_only_target_users()
        _create_target_users()
        db.session.commit()

    print(f"Reset the following users in company '{TARGET_COMPANY_SLUG}':")
    for spec in TARGET_USERS:
        print(f"  {spec['username']} / {spec['password']}")


if __name__ == "__main__":
    main()
