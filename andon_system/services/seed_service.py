from __future__ import annotations

from ..company_context import ensure_default_companies
from ..extensions import db
from ..models.company import Company
from ..models.alert import ALERT_STATUS_OPEN
from ..models.department import Department
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.machine_group import MachineGroup
from ..models.user import User
from .escalation_service import ensure_fixed_escalation_rules


def seed_default_data():
    ensure_default_companies()
    starpak = Company.query.filter_by(slug="starpak").one_or_none()
    if starpak is None:
        starpak = Company.query.order_by(Company.id.asc()).first()
    if starpak is None:
        raise RuntimeError("No company rows available for seed data")

    departments = {
        "Maintenance": "Maintenance department",
        "Quality": "Quality department",
        "Materials": "Materials department",
        "Supervisor": "Supervisor team",
        "Safety": "Safety department",
        "Production": "Production team",
    }

    department_rows = {}
    for name, description in departments.items():
        department_rows[name] = _get_or_create(Department, company_id=starpak.id, name=name, defaults={"description": description})

    machine_groups = ["Press", "Extrusion", "Slitter", "Bag Machine"]
    group_rows = {}
    for group_name in machine_groups:
        group_rows[group_name] = _get_or_create(MachineGroup, company_id=starpak.id, name=group_name, defaults={"is_active": True})

    machines = [
        {"machine_code": "PRESS-1", "name": "Press 1", "machine_type": "Press", "area": "Press Room", "line": "Line 1", "department": "Production"},
        {"machine_code": "PRESS-2", "name": "Press 2", "machine_type": "Press", "area": "Press Room", "line": "Line 1", "department": "Production"},
        {"machine_code": "PRESS-3", "name": "Press 3", "machine_type": "Press", "area": "Press Room", "line": "Line 2", "department": "Production"},
        {"machine_code": "PRESS-4", "name": "Press 4", "machine_type": "Press", "area": "Press Room", "line": "Line 2", "department": "Production"},
        {"machine_code": "EXTRUSION-1", "name": "Extrusion 1", "machine_type": "Extrusion", "area": "Extrusion", "line": "Line 5", "department": "Production"},
        {"machine_code": "EXTRUSION-2", "name": "Extrusion 2", "machine_type": "Extrusion", "area": "Extrusion", "line": "Line 5", "department": "Production"},
        {"machine_code": "SLITTER-1", "name": "Slitter 1", "machine_type": "Slitter", "area": "Converting", "line": "Line 3", "department": "Materials"},
        {"machine_code": "SLITTER-2", "name": "Slitter 2", "machine_type": "Slitter", "area": "Converting", "line": "Line 3", "department": "Materials"},
        {"machine_code": "BAG-1", "name": "Bag Machine 1", "machine_type": "Bag Machine", "area": "Packaging", "line": "Line 4", "department": "Production"},
        {"machine_code": "BAG-2", "name": "Bag Machine 2", "machine_type": "Bag Machine", "area": "Packaging", "line": "Line 4", "department": "Production"},
    ]
    machine_rows = {}
    for machine in machines:
        machine_rows[machine["name"]] = _get_or_create(
            Machine,
            company_id=starpak.id,
            machine_code=machine["machine_code"],
            defaults={
                "name": machine["name"],
                "machine_type": machine["machine_type"],
                "area": machine["area"],
                "line": machine["line"],
                "department_id": department_rows[machine["department"]].id,
                "description": f"Seeded machine {machine['name']}",
            },
        )
        machine_rows[machine["name"]].name = machine["name"]
        machine_rows[machine["name"]].machine_type = machine["machine_type"]
        machine_rows[machine["name"]].area = machine["area"]
        machine_rows[machine["name"]].line = machine["line"]
        machine_rows[machine["name"]].department_id = department_rows[machine["department"]].id
        machine_rows[machine["name"]].is_active = True
        group_rows[machine["machine_type"]].is_active = True

    users = [
        {"display_name": "Maintenance Tech 1", "username": "maint1", "role": "Operator", "department": "Maintenance", "machine_group": "Press", "work_id": "M-1001", "email": "maint1@example.com", "phone_number": "555-0101"},
        {"display_name": "Maintenance Tech 2", "username": "maint2", "role": "Operator", "department": "Maintenance", "machine_group": "Extrusion", "work_id": "M-1002", "email": "maint2@example.com", "phone_number": "555-0102"},
        {"display_name": "Quality Tech 1", "username": "qual1", "role": "Operator", "department": "Quality", "machine_group": "Slitter", "work_id": "Q-2001", "email": "qual1@example.com", "phone_number": "555-0201"},
        {"display_name": "Material Handler 1", "username": "mat1", "role": "Operator", "department": "Materials", "machine_group": "Bag Machine", "work_id": "M-3001", "email": "mat1@example.com", "phone_number": "555-0301"},
        {"display_name": "Production Manager 1", "username": "mgr1", "role": "Manager", "department": "Production", "machine_group": "Press", "work_id": "P-5001", "email": "mgr1@example.com", "phone_number": "555-0501"},
        {"display_name": "Supervisor 1", "username": "sup1", "role": "Supervisor", "department": "Supervisor", "machine_group": "Press", "work_id": "S-4001", "email": "sup1@example.com", "phone_number": "555-0401"},
    ]
    for user in users:
        created = _get_or_create(
            User,
            company_id=starpak.id,
            username=user["username"],
            defaults={
                "display_name": user["display_name"],
                "role": user["role"],
                "employee_id": user["work_id"],
                "email": user["email"],
                "phone_number": user["phone_number"],
                "department_id": department_rows[user["department"]].id,
                "machine_group_id": group_rows[user["machine_group"]].id,
            },
        )
        created.display_name = user["display_name"]
        created.role = user["role"]
        created.employee_id = user["work_id"]
        created.email = user["email"]
        created.phone_number = user["phone_number"]
        created.department_id = department_rows[user["department"]].id
        created.machine_group_id = group_rows[user["machine_group"]].id

    categories = [
        {"name": "Maintenance", "department": "Maintenance", "color": "#dc3545", "priority": 1},
        {"name": "Quality", "department": "Quality", "color": "#fd7e14", "priority": 2},
        {"name": "Materials", "department": "Materials", "color": "#0d6efd", "priority": 2},
        {"name": "Safety", "department": "Safety", "color": "#198754", "priority": 2},
        {"name": "Supervisor", "department": "Supervisor", "color": "#6f42c1", "priority": 1},
        {"name": "Production", "department": "Production", "color": "#20c997", "priority": 2},
    ]
    category_rows = {}
    for category in categories:
        category_rows[category["name"]] = _get_or_create(
            IssueCategory,
            company_id=starpak.id,
            name=category["name"],
            defaults={
                "department_id": department_rows[category["department"]].id,
                "color": category["color"],
                "priority_default": category["priority"],
            },
        )

    problems = {
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
        "Production": [
            "Line stop",
            "Operator assistance needed",
            "Startup issue",
        ],
    }
    for category_name, problem_names in problems.items():
        for name in problem_names:
            _get_or_create(
                IssueProblem,
                company_id=starpak.id,
                category_id=category_rows[category_name].id,
                name=name,
                defaults={
                    "description": f"Seeded problem: {name}",
                    "severity_default": 2 if category_name == "Materials" else 3,
                },
            )

    _seed_default_escalation_rules(department_rows, category_rows)
    db.session.commit()


def _seed_default_escalation_rules(departments, categories):
    ensure_fixed_escalation_rules()


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
