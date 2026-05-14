from __future__ import annotations

from ..company_context import ensure_default_companies
from ..extensions import db
from ..models import andon_alert_escalation_map
from ..models.company import Company
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.escalation import EscalationRule
from ..models.user import User


DEFAULT_DEPARTMENTS = {
    "Maintenance": "Maintenance department",
    "Quality": "Quality department",
    "Materials": "Materials department",
    "Shipping": "Shipping department",
    "Supervisor": "Supervisor team",
    "Safety": "Safety department",
    "Spot": "Spot department",
}

DEFAULT_CATEGORIES = [
    {"name": "Maintenance", "department": "Maintenance", "color": "#dc3545", "priority": 1},
    {"name": "Quality", "department": "Quality", "color": "#fd7e14", "priority": 2},
    {"name": "Materials", "department": "Materials", "color": "#0d6efd", "priority": 2},
    {"name": "Shipping", "department": "Shipping", "color": "#20c997", "priority": 2},
    {"name": "Safety", "department": "Safety", "color": "#198754", "priority": 2},
    {"name": "Supervisor", "department": "Supervisor", "color": "#6f42c1", "priority": 1},
    {"name": "Spot", "department": "Spot", "color": "#6c757d", "priority": 2},
]

DEFAULT_PROBLEMS = {
    "Maintenance": [
        "Mechanical jam",
        "Electrical fault",
        "Sensor issue",
        "Air leak",
        "Motor issue",
    ],
    "Quality": [
        "Print defect",
        "Dimension issue",
        "Bad seal",
        "Material contamination",
    ],
    "Materials": [
        "Material shortage",
        "Wrong material",
        "Roll change needed",
    ],
    "Shipping": [
        "Shipping assistance needed",
        "Truck loading delay",
        "Finished goods pickup needed",
    ],
    "Safety": [
        "PPE issue",
        "Spill cleanup needed",
        "Blocked aisle",
    ],
    "Supervisor": [
        "Supervisor assistance needed",
        "Line support needed",
        "Escalation review required",
    ],
    "Spot": [
        "Spot assistance needed",
        "Spot quality review",
        "Spot material check",
    ],
}


def seed_default_data():
    _clear_seeded_tables()
    ensure_default_companies()

    for company in Company.query.order_by(Company.id.asc()).all():
        _seed_departments(company)
        _seed_categories_and_problems(company)

    db.session.commit()


def _clear_seeded_tables():
    # These tables are preserved across reseeds, so clear their nullable
    # department references before deleting department rows.
    db.session.execute(db.update(User).values(department_id=None))
    db.session.execute(db.update(Machine).values(department_id=None))
    db.session.execute(
        db.update(EscalationRule).values(
            department_id=None,
            issue_category_id=None,
            issue_problem_id=None,
        )
    )

    for table in reversed(db.metadata.sorted_tables):
        if table.name in {"companies", "machines", "machine_groups", "users", "escalation_rules"}:
            continue
        if table is andon_alert_escalation_map:
            db.session.execute(andon_alert_escalation_map.delete())
            continue
        db.session.execute(table.delete())


def _seed_departments(company):
    for name, description in DEFAULT_DEPARTMENTS.items():
        _get_or_create(
            Department,
            company_id=company.id,
            name=name,
            defaults={
                "description": description,
                "is_active": True,
            },
        )


def _seed_categories_and_problems(company):
    department_rows = {
        department.name: department
        for department in Department.query.filter_by(company_id=company.id).all()
    }

    category_rows = {}
    for category in DEFAULT_CATEGORIES:
        category_rows[category["name"]] = _get_or_create(
            IssueCategory,
            company_id=company.id,
            name=category["name"],
            defaults={
                "department_id": department_rows[category["department"]].id,
                "color": category["color"],
                "priority_default": category["priority"],
                "is_active": True,
            },
        )

    for category_name, problem_names in DEFAULT_PROBLEMS.items():
        for name in problem_names:
            _get_or_create(
                IssueProblem,
                company_id=company.id,
                category_id=category_rows[category_name].id,
                name=name,
                defaults={
                    "description": f"Seeded problem: {name}",
                    "severity_default": 2 if category_name in {"Materials", "Spot"} else 3,
                    "is_active": True,
                },
            )


def _get_or_create(model, defaults=None, **filters):
    instance = model.query.filter_by(**filters).one_or_none()
    if instance:
        return instance
    params = dict(filters)
    if defaults:
        params.update(defaults)
    instance = model(**params)
    db.session.add(instance)
    db.session.flush()
    return instance
