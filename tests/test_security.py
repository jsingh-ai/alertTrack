from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from andon_system import create_app
from andon_system.config import ProductionConfig, TestingConfig
from andon_system.extensions import db, socketio
from andon_system.models.alert import ALERT_STATUS_OPEN, AndonAlert
from andon_system.models.company import Company
from andon_system.models.department import Department
from andon_system.models.issue import IssueCategory, IssueProblem
from andon_system.models.machine import Machine
from andon_system.models.machine_group import MachineGroup
from andon_system.security import ADMIN_SESSION_KEY, CSRF_SESSION_KEY


ADMIN_POST_CASES = [
    ("admin.create_department", "/andon/admin/department/create", {"name": "Coverage Department"}),
    ("admin.toggle_department", "/andon/admin/department/1/toggle", {}),
    ("admin.update_department", "/andon/admin/department/1/update", {"name": "Updated Department"}),
    ("admin.delete_department", "/andon/admin/department/1/delete", {}),
    ("admin.create_machine", "/andon/admin/machine/create", {"machine_group": "Press", "name": "Coverage Machine"}),
    ("admin.toggle_machine", "/andon/admin/machine/1/toggle", {}),
    ("admin.delete_machine", "/andon/admin/machine/1/delete", {}),
    ("admin.create_machine_group", "/andon/admin/machine-group/create", {"name": "Coverage Group"}),
    ("admin.toggle_machine_group", "/andon/admin/machine-group/1/toggle", {}),
    ("admin.update_machine_group", "/andon/admin/machine-group/1/update", {"name": "Updated Group"}),
    ("admin.delete_machine_group", "/andon/admin/machine-group/1/delete", {}),
    ("admin.toggle_machine_type", "/andon/admin/machine-type/Press/toggle", {}),
    (
        "admin.create_user",
        "/andon/admin/user/create",
        {
            "display_name": "Coverage User",
            "role": "Operator",
            "machine_group_id": "1",
            "department_id": "1",
        },
    ),
    (
        "admin.update_user",
        "/andon/admin/user/1/update",
        {
            "display_name": "Updated User",
            "role": "Operator",
            "machine_group_id": "1",
            "department_id": "1",
        },
    ),
    ("admin.toggle_user", "/andon/admin/user/1/toggle", {}),
    ("admin.delete_user", "/andon/admin/user/1/delete", {}),
    ("admin.create_problem", "/andon/admin/problem/create", {"department_id": "1", "name": "Coverage Problem"}),
    ("admin.toggle_problem", "/andon/admin/problem/1/toggle", {}),
    ("admin.delete_problem", "/andon/admin/problem/1/delete", {}),
    ("admin.create_escalation_rule", "/andon/admin/escalation/create", {"level": "1", "delay_seconds": "300"}),
    ("admin.update_escalation_rule", "/andon/admin/escalation/1/update", {"delay_seconds": "600"}),
    ("admin.toggle_escalation_rule", "/andon/admin/escalation/1/toggle", {}),
]


@pytest.fixture(scope="module")
def app():
    database_uri = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_uri:
        pytest.skip("TEST_DATABASE_URL or DATABASE_URL must be configured to run security tests.")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = database_uri
    app = create_app("testing")
    app.config.update(
        ADMIN_PASSWORD="very-strong-admin-password",
        SECRET_KEY="test-secret-key",
        SOCKETIO_ENABLED=True,
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.add_all(
            [
                Company(name="Five Star", slug="five-star", is_active=True),
                Company(name="Starpak", slug="starpak", is_active=True),
            ]
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


@pytest.fixture()
def client(app):
    return app.test_client()


def _seed_csrf(client):
    client.get("/andon/operator")
    with client.session_transaction() as session:
        return session[CSRF_SESSION_KEY]


def _authenticate_admin(client):
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session[ADMIN_SESSION_KEY] = True
    return csrf_token


def _create_alert_api_fixtures(app):
    suffix = uuid4().hex[:8]
    with app.app_context():
        company = Company.query.filter_by(slug="five-star").one()
        department = Department(company_id=company.id, name=f"Dept {suffix}", is_active=True)
        db.session.add(department)
        db.session.flush()

        category = IssueCategory(
            company_id=company.id,
            department_id=department.id,
            name=f"Category {suffix}",
            color="#0d6efd",
            priority_default=3,
            is_active=True,
        )
        db.session.add(category)
        db.session.flush()

        problem = IssueProblem(
            company_id=company.id,
            category_id=category.id,
            name=f"Problem {suffix}",
            severity_default=3,
            is_active=True,
        )
        group = MachineGroup(company_id=company.id, name=f"Group {suffix}", is_active=True)
        db.session.add(group)
        db.session.flush()

        machine = Machine(
            company_id=company.id,
            machine_code=f"M-{suffix}",
            name=f"Machine {suffix}",
            machine_type=group.name,
            department_id=department.id,
            is_active=True,
        )
        db.session.add_all([problem, machine])
        db.session.commit()
        return {
            "company_slug": company.slug,
            "company_id": company.id,
            "department_id": department.id,
            "category_id": category.id,
            "problem_id": problem.id,
            "machine_id": machine.id,
        }


def test_admin_page_requires_server_side_authentication(client):
    response = client.get("/andon/admin", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/andon/operator")


def test_admin_login_requires_csrf(client):
    response = client.post(
        "/andon/admin/login",
        data={"password": "very-strong-admin-password", "next": "/andon/admin"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert b"CSRF validation failed" in response.data


def test_admin_login_sets_session_and_allows_admin_page(client):
    csrf_token = _seed_csrf(client)
    response = client.post(
        "/andon/admin/login",
        data={
            "password": "very-strong-admin-password",
            "next": "/andon/admin",
            "csrf_token": csrf_token,
        },
        headers={"Accept": "application/json", "X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    admin_page = client.get("/andon/admin")

    assert admin_page.status_code == 200
    assert b"Admin Setup" in admin_page.data


def test_all_admin_mutation_routes_are_covered(app):
    actual_endpoints = {
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if rule.endpoint.startswith("admin.") and "POST" in rule.methods
    }
    covered_endpoints = {endpoint for endpoint, _path, _data in ADMIN_POST_CASES}

    assert covered_endpoints == actual_endpoints


@pytest.mark.parametrize(("endpoint", "path", "data"), ADMIN_POST_CASES)
def test_all_admin_post_routes_reject_unauthenticated_users(client, endpoint, path, data):
    response = client.post(
        path,
        data=data,
        headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    assert response.status_code == 403, endpoint


@pytest.mark.parametrize(("endpoint", "path", "data"), ADMIN_POST_CASES)
def test_all_admin_post_routes_reject_missing_csrf_for_authenticated_users(client, endpoint, path, data):
    _authenticate_admin(client)

    response = client.post(
        path,
        data=data,
        headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    assert response.status_code == 400, endpoint
    assert b"CSRF validation failed" in response.data


def test_json_api_rejects_missing_csrf_token(client, app):
    fixtures = _create_alert_api_fixtures(app)
    client.get("/andon/operator")
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
    )

    assert response.status_code == 400
    assert b"CSRF validation failed" in response.data


def test_json_api_accepts_valid_csrf_token(client, app):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["machine"]["id"] == fixtures["machine_id"]


def test_database_unique_index_rejects_duplicate_active_alerts(app):
    fixtures = _create_alert_api_fixtures(app)
    with app.app_context():
        first_alert = AndonAlert(
            company_id=fixtures["company_id"],
            alert_number=f"AL-{uuid4().hex[:10].upper()}",
            machine_id=fixtures["machine_id"],
            department_id=fixtures["department_id"],
            issue_category_id=fixtures["category_id"],
            issue_problem_id=fixtures["problem_id"],
            status=ALERT_STATUS_OPEN,
            priority=3,
        )
        duplicate_alert = AndonAlert(
            company_id=fixtures["company_id"],
            alert_number=f"AL-{uuid4().hex[:10].upper()}",
            machine_id=fixtures["machine_id"],
            department_id=fixtures["department_id"],
            issue_category_id=fixtures["category_id"],
            issue_problem_id=fixtures["problem_id"],
            status=ALERT_STATUS_OPEN,
            priority=3,
        )
        db.session.add(first_alert)
        db.session.commit()

        db.session.add(duplicate_alert)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_socket_join_uses_server_side_company_context_not_client_payload(app, client):
    with app.app_context():
        authorized_company = Company.query.filter_by(slug="five-star").one()
        unauthorized_company = Company.query.filter_by(slug="starpak").one()

    client.get("/andon/operator")
    with client.session_transaction() as session:
        session["andon_company_slug"] = "five-star"

    socket_client = socketio.test_client(app, flask_test_client=client)
    try:
        socket_client.emit(
            "join_company_room",
            {"company_id": unauthorized_company.id, "room": "board"},
        )
        received = socket_client.get_received()
    finally:
        socket_client.disconnect()

    joined_events = [event for event in received if event["name"] == "joined_company_room"]
    assert joined_events, received
    joined_payload = joined_events[-1]["args"][0]
    assert joined_payload["company_id"] == authorized_company.id
    assert joined_payload["company_id"] != unauthorized_company.id


@pytest.mark.parametrize(
    ("secret_key", "admin_password", "expected_message"),
    [
        ("dev-andon-secret-key", "very-strong-admin-password", "SECRET_KEY"),
        ("production-secret-key", None, "ANDON_ADMIN_PASSWORD"),
        ("production-secret-key", "short", "ANDON_ADMIN_PASSWORD"),
    ],
)
def test_production_config_fails_fast_for_weak_security(monkeypatch, secret_key, admin_password, expected_message):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", secret_key)
    monkeypatch.setattr(ProductionConfig, "ADMIN_PASSWORD", admin_password)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", "postgresql://test:test@localhost/andon_test")

    with pytest.raises(RuntimeError, match=expected_message):
        create_app("production")


def test_run_socketio_rejects_unsafe_werkzeug_outside_debug():
    from run_socketio import _ensure_safe_werkzeug

    with pytest.raises(RuntimeError, match="development/debug mode"):
        _ensure_safe_werkzeug(SimpleNamespace(debug=False))


def test_run_socketio_allows_werkzeug_in_debug():
    from run_socketio import _ensure_safe_werkzeug

    _ensure_safe_werkzeug(SimpleNamespace(debug=True))
