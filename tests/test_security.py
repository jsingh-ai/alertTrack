from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from andon_system import create_app
from andon_system.config import DevelopmentConfig, ProductionConfig, TestingConfig
from andon_system.extensions import db, socketio
from andon_system.models.alert import ALERT_STATUS_OPEN, AndonAlert
from andon_system.models.company import Company
from andon_system.models.department import Department
from andon_system.models.issue import IssueCategory, IssueProblem
from andon_system.models.machine import Machine
from andon_system.models.machine_group import MachineGroup
from andon_system.models.pager_device import PagerDevice
from andon_system.models.user import User, UserCompanyAccess
from andon_system.routes.admin import _resolve_scope_config
from andon_system.routes.pages import _session_cookie_domain_matches_request
from andon_system.security import ADMIN_SESSION_KEY, CSRF_SESSION_KEY, USER_SESSION_KEY, get_default_membership, get_scope_filters
from andon_system.services.board_service import _operator_metadata_cache_key, build_operator_metadata


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


def test_admin_page_get_does_not_create_pager_devices(admin_login_client):
    client, app = admin_login_client
    client.get("/andon/home")
    with client.session_transaction() as session:
        with app.app_context():
            admin_user = User.query.filter_by(username="admin.user").one()
            session[USER_SESSION_KEY] = admin_user.id
            session["andon_company_slug"] = "admin-test"
            session["andon_company_id"] = admin_user.company_id
            session[ADMIN_SESSION_KEY] = True

    with app.app_context():
        company = Company.query.filter_by(slug="admin-test").one()
        department = Department.query.filter_by(company_id=company.id, name="Pagerless Department").one()
        department_id = department.id
        assert PagerDevice.query.filter_by(company_id=company.id, department_id=department_id).count() == 0

    response = client.get("/andon/admin?section=departments")

    assert response.status_code == 200
    with app.app_context():
        company = Company.query.filter_by(slug="admin-test").one()
        assert PagerDevice.query.filter_by(company_id=company.id, department_id=department_id).count() == 0


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
            "issue_category_id": fixtures["category_id"],
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
            "issue_category_id": fixtures["category_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["machine"]["id"] == fixtures["machine_id"]


def test_create_alert_does_not_fetch_full_active_alert_list(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    from andon_system.services import alert_service as alert_service_module

    def fail_fetch_active_alerts(*_args, **_kwargs):
        raise AssertionError("create_alert must not fetch full active-alert list")

    monkeypatch.setattr(alert_service_module, "fetch_active_alert_payloads", fail_fetch_active_alerts)

    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_category_id": fixtures["category_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert "events" not in payload["data"]


def test_create_alert_duplicate_check_is_machine_company_scoped(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    from andon_system.services import alert_service as alert_service_module

    captured = {}
    original = alert_service_module._find_active_alert_id_for_machine

    def wrapped(machine_id, company_id):
        captured["machine_id"] = machine_id
        captured["company_id"] = company_id
        return original(machine_id, company_id)

    monkeypatch.setattr(alert_service_module, "_find_active_alert_id_for_machine", wrapped)

    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_category_id": fixtures["category_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    assert captured["machine_id"] == fixtures["machine_id"]
    assert captured["company_id"] == fixtures["company_id"]


def test_create_alert_uses_live_alert_cache_invalidation_only(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    from andon_system.services import alert_service as alert_service_module

    calls = {"live": 0}

    def fake_invalidate_live_alert_caches(company_id):
        calls["live"] += 1
        assert company_id == fixtures["company_id"]
        return {"mode": "local", "total_ms": 0.1}

    monkeypatch.setattr(alert_service_module, "invalidate_live_alert_caches", fake_invalidate_live_alert_caches)

    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_category_id": fixtures["category_id"],
            "issue_problem_id": fixtures["problem_id"],
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    assert calls["live"] == 1


def test_create_alert_requires_issue_category_id(client, app):
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

    assert response.status_code == 400
    payload = response.get_json()
    assert "issue_category_id is required" in payload["error"]["message"]


def test_create_alert_combined_quality_and_supervisor_uses_department_specific_issue_ids(tmp_path, monkeypatch):
    database_path = tmp_path / "combined-alert-create.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        suffix = uuid4().hex[:8]
        company = Company(name=f"Combined Alert {suffix}", slug=f"combined-alert-{suffix}", is_active=True)
        db.session.add(company)
        db.session.flush()

        combined_department = Department(company_id=company.id, name="Quality and Supervisor", is_active=True)
        quality_department = Department(company_id=company.id, name="Quality", is_active=True)
        supervisor_department = Department(company_id=company.id, name="Supervisor", is_active=True)
        group = MachineGroup(company_id=company.id, name=f"Combined Group {suffix}", is_active=True)
        db.session.add_all([combined_department, quality_department, supervisor_department, group])
        db.session.flush()

        quality_category = IssueCategory(
            company_id=company.id,
            department_id=quality_department.id,
            name=f"Quality Joint Escalation {suffix}",
            color="#fd7e14",
            priority_default=2,
            is_active=True,
        )
        supervisor_default_category = IssueCategory(
            company_id=company.id,
            department_id=supervisor_department.id,
            name=f"Supervisor Default {suffix}",
            color="#6f42c1",
            priority_default=1,
            is_active=True,
        )
        supervisor_joint_category = IssueCategory(
            company_id=company.id,
            department_id=supervisor_department.id,
            name=f"Supervisor Joint Escalation {suffix}",
            color="#6f42c1",
            priority_default=1,
            is_active=True,
        )
        db.session.add_all([quality_category, supervisor_default_category, supervisor_joint_category])
        db.session.flush()

        quality_problem = IssueProblem(
            company_id=company.id,
            category_id=quality_category.id,
            name=f"Shared Review {suffix}",
            severity_default=3,
            is_active=True,
        )
        supervisor_default_problem = IssueProblem(
            company_id=company.id,
            category_id=supervisor_default_category.id,
            name=f"Default Supervisor Problem {suffix}",
            severity_default=2,
            is_active=True,
        )
        supervisor_joint_problem = IssueProblem(
            company_id=company.id,
            category_id=supervisor_joint_category.id,
            name=f"Shared Review {suffix}",
            severity_default=1,
            is_active=True,
        )
        machine = Machine(
            company_id=company.id,
            machine_code=f"MC-{suffix}",
            name=f"Combined Machine {suffix}",
            machine_type=group.name,
            department_id=quality_department.id,
            is_active=True,
        )
        db.session.add_all([quality_problem, supervisor_default_problem, supervisor_joint_problem, machine])
        db.session.commit()

        from andon_system.services import alert_service as alert_service_module

        monkeypatch.setattr(alert_service_module, "get_current_company_id", lambda: company.id)
        monkeypatch.setattr(
            alert_service_module,
            "get_scope_filters",
            lambda: {"machine_ids": [], "department_ids": [], "machine_group_names": [], "department_id": None, "machine_group_name": None},
        )
        monkeypatch.setattr(alert_service_module, "_launch_create_post_commit_side_effects", lambda **_kwargs: None)

        result = alert_service_module.create_alert(
            {
                "machine_id": machine.id,
                "department_id": combined_department.id,
                "issue_category_id": quality_category.id,
                "issue_problem_id": quality_problem.id,
            }
        )

        created_alerts = result["created_alerts"]
        assert len(created_alerts) == 2

        alerts_by_department = {item["department_id"]: item for item in created_alerts}

        quality_alert = alerts_by_department[quality_department.id]
        assert quality_alert["issue_category_id"] == quality_category.id
        assert quality_alert["issue_problem_id"] == quality_problem.id

        supervisor_alert = alerts_by_department[supervisor_department.id]
        assert supervisor_alert["issue_category_id"] == supervisor_joint_category.id
        assert supervisor_alert["issue_problem_id"] == supervisor_joint_problem.id
        assert supervisor_alert["issue_category_id"] != supervisor_default_category.id
        assert supervisor_alert["issue_problem_id"] != supervisor_default_problem.id

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def _create_alert_for_mutation(client, fixtures, csrf_token):
    response = client.post(
        "/api/andon/alerts",
        json={
            "machine_id": fixtures["machine_id"],
            "department_id": fixtures["department_id"],
            "issue_category_id": fixtures["category_id"],
            "issue_problem_id": fixtures["problem_id"],
            "note": "initial note",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 201
    return response.get_json()["data"]["id"]


def test_acknowledge_uses_lightweight_response_and_live_cache_invalidation_only(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    alert_id = _create_alert_for_mutation(client, fixtures, csrf_token)

    from andon_system.models import alert as alert_model_module
    from andon_system.services import alert_service as alert_service_module

    monkeypatch.setattr(alert_model_module.AndonAlert, "to_dict", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("to_dict should not be used")))
    calls = {"live": 0}

    def fake_invalidate_live_alert_caches(company_id):
        calls["live"] += 1
        assert company_id == fixtures["company_id"]
        return {"mode": "local", "total_ms": 0.1}

    monkeypatch.setattr(alert_service_module, "invalidate_live_alert_caches", fake_invalidate_live_alert_caches)

    response = client.post(
        f"/api/andon/alerts/{alert_id}/acknowledge",
        json={"note": "ack note"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["id"] == alert_id
    assert calls["live"] == 1


def test_resolve_uses_lightweight_response_and_live_cache_invalidation_only(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    alert_id = _create_alert_for_mutation(client, fixtures, csrf_token)

    acknowledge_response = client.post(
        f"/api/andon/alerts/{alert_id}/acknowledge",
        json={"note": "ack note"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert acknowledge_response.status_code == 200

    from andon_system.models import alert as alert_model_module
    from andon_system.services import alert_service as alert_service_module

    monkeypatch.setattr(alert_model_module.AndonAlert, "to_dict", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("to_dict should not be used")))
    calls = {"live": 0}

    def fake_invalidate_live_alert_caches(company_id):
        calls["live"] += 1
        assert company_id == fixtures["company_id"]
        return {"mode": "local", "total_ms": 0.1}

    monkeypatch.setattr(alert_service_module, "invalidate_live_alert_caches", fake_invalidate_live_alert_caches)

    response = client.post(
        f"/api/andon/alerts/{alert_id}/resolve",
        json={"note": "resolved"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["id"] == alert_id
    assert calls["live"] == 1


def test_resolve_does_not_depend_on_post_commit_payload_refetch(client, app, monkeypatch):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    alert_id = _create_alert_for_mutation(client, fixtures, csrf_token)

    acknowledge_response = client.post(
        f"/api/andon/alerts/{alert_id}/acknowledge",
        json={"note": "ack note"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert acknowledge_response.status_code == 200

    from andon_system.routes import api as api_module

    monkeypatch.setattr(api_module, "fetch_alert_payload_by_id", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("post-commit payload refetch should not run")))

    response = client.post(
        f"/api/andon/alerts/{alert_id}/resolve",
        json={"note": "resolved"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["id"] == alert_id
    assert payload["data"]["status"] == "RESOLVED"


def test_operator_snapshot_includes_created_note_for_active_alerts(client, app):
    fixtures = _create_alert_api_fixtures(app)
    csrf_token = _seed_csrf(client)
    with client.session_transaction() as session:
        session["andon_company_slug"] = fixtures["company_slug"]

    alert_id = _create_alert_for_mutation(client, fixtures, csrf_token)

    response = client.get("/api/andon/operator-snapshot")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    machines = payload["data"]["machines"]
    machine = next(item for item in machines if item["id"] == fixtures["machine_id"])
    active_alert = next(item for item in machine["active_alerts"] if item["id"] == alert_id)
    assert active_alert["created_note"] == "initial note"


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


def test_runtime_requires_postgresql_outside_testing(monkeypatch):
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    monkeypatch.setattr(DevelopmentConfig, "SECRET_KEY", "production-secret-key")
    monkeypatch.setattr(DevelopmentConfig, "ADMIN_PASSWORD", "very-strong-admin-password")

    with pytest.raises(RuntimeError, match="PostgreSQL is required for runtime environments"):
        create_app("development")


def test_run_socketio_rejects_unsafe_werkzeug_outside_debug():
    from run_socketio import _ensure_safe_werkzeug

    with pytest.raises(RuntimeError, match="development/debug mode"):
        _ensure_safe_werkzeug(SimpleNamespace(debug=False))


def test_run_socketio_allows_werkzeug_in_debug():
    from run_socketio import _ensure_safe_werkzeug

    _ensure_safe_werkzeug(SimpleNamespace(debug=True))


def _build_login_client(tmp_path, monkeypatch, *, proxy_fix_x_proto: int):
    database_path = tmp_path / "login.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ANDON_ADMIN_PASSWORD", "very-strong-admin-password")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("PROXY_FIX_X_PROTO", str(proxy_fix_x_proto))

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    app.config.update(
        SECRET_KEY="test-secret-key",
        ADMIN_PASSWORD="very-strong-admin-password",
        SESSION_COOKIE_SECURE=True,
        PROXY_FIX_X_PROTO=proxy_fix_x_proto,
    )
    with app.app_context():
        db.create_all()
        company = Company(name="Proxy Test Co", slug="proxy-test", is_active=True)
        db.session.add(company)
        db.session.flush()

        user = User(
            company_id=company.id,
            display_name="Proxy Test User",
            username="proxy.user",
            role="Manager",
            is_active=True,
        )
        user.set_password("ProxyPass!2026")
        db.session.add(user)
        db.session.flush()
        db.session.add(
            UserCompanyAccess(
                user_id=user.id,
                company_id=company.id,
                role="Manager",
                scope_mode="all",
                is_active=True,
            )
        )
        operator_user = User(
            company_id=company.id,
            display_name="Proxy Operator User",
            username="proxy.operator",
            role="Operator",
            is_active=True,
        )
        operator_user.set_password("ProxyOp!2026")
        db.session.add(operator_user)
        db.session.flush()
        db.session.add(
            UserCompanyAccess(
                user_id=operator_user.id,
                company_id=company.id,
                role="Operator",
                scope_mode="all",
                is_active=True,
            )
        )
        db.session.commit()

    client = app.test_client()
    yield client

    with app.app_context():
        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


@pytest.fixture()
def login_client(tmp_path, monkeypatch):
    yield from _build_login_client(tmp_path, monkeypatch, proxy_fix_x_proto=0)


@pytest.fixture()
def proxied_login_client(tmp_path, monkeypatch):
    yield from _build_login_client(tmp_path, monkeypatch, proxy_fix_x_proto=1)


@pytest.fixture()
def admin_login_client(tmp_path, monkeypatch):
    database_path = tmp_path / "admin.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ANDON_ADMIN_PASSWORD", "very-strong-admin-password")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    app.config.update(
        SECRET_KEY="test-secret-key",
        ADMIN_PASSWORD="very-strong-admin-password",
        SOCKETIO_ENABLED=True,
    )
    with app.app_context():
        db.create_all()
        company = Company(name="Admin Test Co", slug="admin-test", is_active=True)
        department = Department(company=company, name="Pagerless Department", is_active=True)
        db.session.add_all([company, department])
        db.session.flush()
        admin_user = User(
            company_id=company.id,
            display_name="Admin User",
            username="admin.user",
            role="Admin",
            is_active=True,
        )
        admin_user.set_password("AdminPass!2026")
        db.session.add(admin_user)
        db.session.flush()
        db.session.add(
            UserCompanyAccess(
                user_id=admin_user.id,
                company_id=company.id,
                role="Admin",
                scope_mode="all",
                is_active=True,
            )
        )
        db.session.commit()

    client = app.test_client()
    yield client, app

    with app.app_context():
        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def _seed_admin_session_for_company(client, app):
    client.get("/andon/home")
    with app.app_context():
        admin_user = User.query.filter_by(username="admin.user").one()
        company = Company.query.filter_by(slug="admin-test").one()
    with client.session_transaction() as session:
        csrf_token = session[CSRF_SESSION_KEY]
        session[USER_SESSION_KEY] = admin_user.id
        session["andon_company_slug"] = company.slug
        session["andon_company_id"] = company.id
        session[ADMIN_SESSION_KEY] = True
    return csrf_token


def test_login_blocks_plain_http_when_secure_cookie_required(login_client):
    login_client.get("/andon/home", base_url="http://localhost")
    with login_client.session_transaction() as session:
        csrf_token = session[CSRF_SESSION_KEY]

    response = login_client.post(
        "/andon/login",
        data={
            "identity": "proxy.user",
            "password": "ProxyPass!2026",
            "csrf_token": csrf_token,
        },
        base_url="http://localhost",
    )

    assert response.status_code == 400
    assert b"secure cookies are enabled on an HTTP request" in response.data


def test_login_accepts_forwarded_https_when_proxy_fix_enabled(proxied_login_client):
    proxied_login_client.get("/andon/home", base_url="http://localhost", headers={"X-Forwarded-Proto": "https"})
    with proxied_login_client.session_transaction() as session:
        csrf_token = session[CSRF_SESSION_KEY]

    response = proxied_login_client.post(
        "/andon/login",
        data={
            "identity": "proxy.user",
            "password": "ProxyPass!2026",
            "csrf_token": csrf_token,
        },
        base_url="http://localhost",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/andon/management")


def test_session_cookie_domain_mismatch_detected(login_client):
    app = login_client.application
    app.config["SESSION_COOKIE_DOMAIN"] = "andon.example.com"

    with app.test_request_context("/andon/login", base_url="http://10.20.1.8"):
        assert _session_cookie_domain_matches_request() is False


def test_operator_login_redirects_to_operator_without_rendering_page(proxied_login_client, monkeypatch):
    proxied_login_client.get("/andon/home", base_url="http://localhost", headers={"X-Forwarded-Proto": "https"})
    with proxied_login_client.session_transaction() as session:
        csrf_token = session[CSRF_SESSION_KEY]

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("operator_page should not be called during /andon/login POST")

    monkeypatch.setattr("andon_system.routes.pages.operator_page", _fail_if_called)

    response = proxied_login_client.post(
        "/andon/login",
        data={
            "identity": "proxy.operator",
            "password": "ProxyOp!2026",
            "csrf_token": csrf_token,
        },
        base_url="http://localhost",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/andon/operator")


def test_admin_create_department_role_user_accepts_form_csrf(admin_login_client):
    client, app = admin_login_client
    csrf_token = _seed_admin_session_for_company(client, app)

    with app.app_context():
        company = Company.query.filter_by(slug="admin-test").one()
        department = Department.query.filter_by(company_id=company.id, name="Pagerless Department").one()

    response = client.post(
        "/andon/admin/user/create",
        data={
            "csrf_token": csrf_token,
            "display_name": "Dept User",
            "username": "dept.user",
            "password": "DeptPass!2026",
            "role": "Viewer",
            "department_id": str(department.id),
            "scope_department_ids": str(department.id),
        },
        headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["user"]["role"] == "Viewer"
    assert payload["user"]["scope_department_ids"] == [department.id]


def test_admin_create_operator_user_accepts_normalized_group_machine_scope(admin_login_client):
    client, app = admin_login_client
    csrf_token = _seed_admin_session_for_company(client, app)

    with app.app_context():
        company = Company.query.filter_by(slug="admin-test").one()
        department = Department.query.filter_by(company_id=company.id, name="Pagerless Department").one()
        group = MachineGroup(company_id=company.id, name="Press", is_active=True)
        db.session.add(group)
        db.session.flush()
        machine = Machine(
            company_id=company.id,
            machine_code="PRESS-01",
            name="Press 01",
            machine_type=" Press ",
            department_id=department.id,
            is_active=True,
        )
        db.session.add(machine)
        db.session.commit()
        group_id = group.id
        machine_id = machine.id

    response = client.post(
        "/andon/admin/user/create",
        data={
            "csrf_token": csrf_token,
            "display_name": "Operator User",
            "username": "operator.user",
            "password": "OperatorPass!2026",
            "role": "Operator",
            "scope_machine_group_ids": str(group_id),
            "scope_machine_ids": str(machine_id),
        },
        headers={
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["user"]["role"] == "Operator"
    assert payload["user"]["scope_machine_group_ids"] == [group_id]
    assert payload["user"]["scope_machine_ids"] == [machine_id]


def test_health_endpoint_returns_safe_json_without_auth(login_client):
    response = login_client.get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["checks"]["app"] == "ok"
    assert payload["checks"]["db"]["ok"] is True
    assert payload["checks"]["db"]["dialect"] == "sqlite"
    assert "SECRET_KEY" not in str(payload)
    assert "DATABASE_URL" not in str(payload)
    assert "ProxyPass!2026" not in str(payload)


def test_health_endpoint_returns_503_when_db_check_fails(login_client, monkeypatch):
    original_execute = db.session.execute

    def fail_execute(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db.session, "execute", fail_execute)
    try:
        response = login_client.get("/health")
    finally:
        monkeypatch.setattr(db.session, "execute", original_execute)

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["db"]["ok"] is False


def test_invalid_pager_token_checks_only_bounded_legacy_subset(tmp_path, monkeypatch):
    database_path = tmp_path / "pager-auth.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    app.config.update(PAGER_AUTH_LEGACY_FALLBACK_LIMIT=5)
    with app.app_context():
        db.create_all()
        company = Company(name="Pager Test Co", slug="pager-test", is_active=True)
        department = Department(company=company, name="Pager Department", is_active=True)
        db.session.add_all([company, department])
        db.session.flush()
        for idx in range(12):
            user_like_token = f"legacy-token-{idx}"
            db.session.add(
                PagerDevice(
                    company_id=company.id,
                    department_id=department.id,
                    name=f"Pager {idx}",
                    token_hash=generate_password_hash(user_like_token),
                    token_fingerprint=None,
                    active=True,
                )
            )
        db.session.commit()

    from andon_system import security as security_module

    verify_calls = {"count": 0}
    original_verify = security_module.verify_pager_token

    def counting_verify(token_hash, raw_token):
        verify_calls["count"] += 1
        return original_verify(token_hash, raw_token)

    monkeypatch.setattr(security_module, "verify_pager_token", counting_verify)

    client = app.test_client()
    response = client.get(
        "/api/andon/pager/alerts/active",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 403
    assert verify_calls["count"] <= 5

    with app.app_context():
        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_inline_escalation_endpoint_can_be_disabled(admin_login_client):
    client, app = admin_login_client
    app.config["ESCALATION_INLINE_CHECKS_ENABLED"] = False

    with app.app_context():
        admin_user = User.query.filter_by(username="admin.user").one()

    with client.session_transaction() as session:
        session[USER_SESSION_KEY] = admin_user.id
        session["andon_company_slug"] = "admin-test"
        session["andon_company_id"] = admin_user.company_id
        session[ADMIN_SESSION_KEY] = True
        session[CSRF_SESSION_KEY] = "test-csrf-token"

    response = client.post(
        "/api/andon/escalations/check",
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["success"] is False


def test_operator_metadata_cache_key_varies_by_user_scope():
    company_id = 7
    admin_user = SimpleNamespace(id=101)
    scoped_user = SimpleNamespace(id=202)
    admin_membership = SimpleNamespace(
        id=1,
        role="Manager",
        scope_mode="all",
        department_id=None,
        machine_group_id=None,
    )
    scoped_membership = SimpleNamespace(
        id=2,
        role="Operator",
        scope_mode="restricted",
        department_id=4,
        machine_group_id=9,
    )

    admin_key = _operator_metadata_cache_key(
        company_id,
        admin_user,
        admin_membership,
        {"department_ids": [], "machine_group_names": [], "machine_ids": []},
    )
    scoped_key = _operator_metadata_cache_key(
        company_id,
        scoped_user,
        scoped_membership,
        {"department_ids": [4], "machine_group_names": ["Press"], "machine_ids": [12, 13]},
    )

    assert admin_key != scoped_key


def test_pager_active_alerts_reuses_blueprint_authenticated_device(login_client, monkeypatch):
    from andon_system.routes import api as api_module

    calls = {"count": 0}

    def fake_get_authenticated_pager_device(update_last_seen=True):
        calls["count"] += 1
        return SimpleNamespace(company_id=1, department_id=2)

    monkeypatch.setattr(api_module, "get_authenticated_pager_device", fake_get_authenticated_pager_device)
    monkeypatch.setattr(api_module, "get_cached", lambda key: [])

    response = login_client.get(
        "/api/andon/pager/alerts/active",
        headers={"Authorization": "Bearer fake-token"},
    )

    assert response.status_code == 200
    assert calls["count"] == 1


def test_pager_active_alerts_uses_shared_lightweight_fetcher(login_client, monkeypatch):
    from andon_system.routes import api as api_module

    calls = {"fetch": 0}

    def fake_fetch_active_alert_payloads(**_kwargs):
        calls["fetch"] += 1
        return []

    monkeypatch.setattr(api_module, "fetch_active_alert_payloads", fake_fetch_active_alert_payloads)
    monkeypatch.setattr(api_module, "get_authenticated_pager_device", lambda update_last_seen=True: SimpleNamespace(company_id=1, department_id=2))
    monkeypatch.setattr(api_module, "get_cached", lambda key: None)
    monkeypatch.setattr(api_module, "set_cached", lambda *args, **kwargs: None)

    response = login_client.get(
        "/api/andon/pager/alerts/active",
        headers={"Authorization": "Bearer fake-token"},
    )

    assert response.status_code == 200
    assert calls["fetch"] == 1


def test_active_alert_fetcher_respects_company_and_visible_machine_ids(app):
    from andon_system.services.active_alerts_service import fetch_active_alert_payloads

    fixtures = _create_alert_api_fixtures(app)
    with app.app_context():
        machine_outside = Machine(
            company_id=fixtures["company_id"],
            machine_code=f"M-OUT-{uuid4().hex[:6]}",
            name="Outside Machine",
            machine_type="Outside",
            department_id=fixtures["department_id"],
            is_active=True,
        )
        db.session.add(machine_outside)
        db.session.flush()
        db.session.add_all(
            [
                AndonAlert(
                    company_id=fixtures["company_id"],
                    alert_number=f"AL-{uuid4().hex[:10].upper()}",
                    machine_id=fixtures["machine_id"],
                    department_id=fixtures["department_id"],
                    issue_category_id=fixtures["category_id"],
                    issue_problem_id=fixtures["problem_id"],
                    status=ALERT_STATUS_OPEN,
                    priority=3,
                ),
                AndonAlert(
                    company_id=fixtures["company_id"],
                    alert_number=f"AL-{uuid4().hex[:10].upper()}",
                    machine_id=machine_outside.id,
                    department_id=fixtures["department_id"],
                    issue_category_id=fixtures["category_id"],
                    issue_problem_id=fixtures["problem_id"],
                    status=ALERT_STATUS_OPEN,
                    priority=3,
                ),
            ]
        )
        db.session.commit()

        rows = fetch_active_alert_payloads(
            company_id=fixtures["company_id"],
            status="active",
            machine_ids=[fixtures["machine_id"]],
            use_cache=False,
        )

    assert len(rows) == 1
    assert rows[0]["machine"]["id"] == fixtures["machine_id"]


def test_operator_metadata_issue_groups_are_company_and_scope_filtered(tmp_path, monkeypatch):
    database_path = tmp_path / "operator-metadata.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company_a = Company(name="Company A", slug="company-a", is_active=True)
        company_b = Company(name="Company B", slug="company-b", is_active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        dept_a1 = Department(company_id=company_a.id, name="Dept A1", is_active=True)
        dept_a2 = Department(company_id=company_a.id, name="Dept A2", is_active=True)
        dept_b1 = Department(company_id=company_b.id, name="Dept B1", is_active=True)
        db.session.add_all([dept_a1, dept_a2, dept_b1])
        db.session.flush()

        cat_a1 = IssueCategory(company_id=company_a.id, department_id=dept_a1.id, name="Scoped Cat", is_active=True)
        cat_a2 = IssueCategory(company_id=company_a.id, department_id=dept_a2.id, name="Other Dept Cat", is_active=True)
        cat_b1 = IssueCategory(company_id=company_b.id, department_id=dept_b1.id, name="Other Company Cat", is_active=True)
        db.session.add_all([cat_a1, cat_a2, cat_b1])
        db.session.flush()

        db.session.add_all(
            [
                IssueProblem(company_id=company_a.id, category_id=cat_a1.id, name="Scoped Problem", is_active=True),
                IssueProblem(company_id=company_a.id, category_id=cat_a2.id, name="Other Dept Problem", is_active=True),
                IssueProblem(company_id=company_b.id, category_id=cat_b1.id, name="Other Company Problem", is_active=True),
            ]
        )
        db.session.commit()

        payload = build_operator_metadata(
            company_id=company_a.id,
            current_user=SimpleNamespace(id=99),
            membership=SimpleNamespace(
                id=321,
                role="Operator",
                scope_mode="restricted",
                department_id=dept_a1.id,
                machine_group_id=None,
            ),
            scope={
                "company_id": company_a.id,
                "department_id": dept_a1.id,
                "department_ids": [dept_a1.id],
                "machine_group_name": None,
                "machine_group_names": [],
                "machine_ids": [],
                "restricted": True,
            },
        )

        assert [group["category_name"] for group in payload["issue_groups"]] == ["Scoped Cat"]
        assert payload["issue_groups"][0]["department_name"] == "Dept A1"
        assert [problem["name"] for problem in payload["issue_groups"][0]["problems"]] == ["Scoped Problem"]

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_operator_metadata_combined_department_falls_back_to_quality_and_supervisor_issues(tmp_path, monkeypatch):
    database_path = tmp_path / "operator-metadata-combined.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company = Company(name="Combined Company", slug="combined-company", is_active=True)
        db.session.add(company)
        db.session.flush()

        combined_department = Department(company_id=company.id, name="Quality and Supervisor", is_active=True)
        quality_department = Department(company_id=company.id, name="Quality", is_active=True)
        supervisor_department = Department(company_id=company.id, name="Supervisor", is_active=True)
        db.session.add_all([combined_department, quality_department, supervisor_department])
        db.session.flush()

        quality_category = IssueCategory(
            company_id=company.id,
            department_id=quality_department.id,
            name="Quality Cat",
            is_active=True,
        )
        supervisor_category = IssueCategory(
            company_id=company.id,
            department_id=supervisor_department.id,
            name="Supervisor Cat",
            is_active=True,
        )
        db.session.add_all([quality_category, supervisor_category])
        db.session.flush()

        db.session.add_all(
            [
                IssueProblem(company_id=company.id, category_id=quality_category.id, name="Quality Problem", is_active=True),
                IssueProblem(company_id=company.id, category_id=supervisor_category.id, name="Supervisor Problem", is_active=True),
            ]
        )
        db.session.commit()

        payload = build_operator_metadata(
            company_id=company.id,
            current_user=SimpleNamespace(id=101),
            membership=SimpleNamespace(
                id=654,
                role="Operator",
                scope_mode="restricted",
                department_id=combined_department.id,
                machine_group_id=None,
            ),
            scope={
                "company_id": company.id,
                "department_id": combined_department.id,
                "department_ids": [combined_department.id],
                "machine_group_name": None,
                "machine_group_names": [],
                "machine_ids": [],
                "restricted": True,
            },
            metadata_department_ids_override=[combined_department.id],
        )

        assert payload["departments"] == [{"id": combined_department.id, "name": "Quality and Supervisor"}]
        assert [group["category_name"] for group in payload["issue_groups"]] == ["Quality Cat", "Supervisor Cat"]
        assert all(group["department_id"] == combined_department.id for group in payload["issue_groups"])
        assert all(group["department_name"] == "Quality and Supervisor" for group in payload["issue_groups"])
        assert [problem["name"] for problem in payload["issue_groups"][0]["problems"]] == ["Quality Problem"]
        assert [problem["name"] for problem in payload["issue_groups"][1]["problems"]] == ["Supervisor Problem"]

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_operator_metadata_departments_exclude_inactive_departments(tmp_path, monkeypatch):
    database_path = tmp_path / "operator-metadata-departments-active.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company = Company(name="Operator Department Company", slug="operator-dept-company", is_active=True)
        db.session.add(company)
        db.session.flush()

        active_department = Department(company_id=company.id, name="Quality", is_active=True)
        inactive_department = Department(company_id=company.id, name="Call Safety", is_active=False)
        db.session.add_all([active_department, inactive_department])
        db.session.commit()

        payload = build_operator_metadata(
            company_id=company.id,
            current_user=SimpleNamespace(id=777),
            membership=SimpleNamespace(
                id=778,
                role="Operator",
                scope_mode="restricted",
                department_id=None,
                machine_group_id=None,
            ),
            scope={
                "company_id": company.id,
                "department_id": None,
                "department_ids": [active_department.id, inactive_department.id],
                "machine_group_name": None,
                "machine_group_names": [],
                "machine_ids": [],
                "restricted": True,
            },
        )

        assert payload["departments"] == [{"id": active_department.id, "name": "Quality"}]

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_operator_scope_filters_exclude_inactive_groups_departments_and_machines(tmp_path, monkeypatch):
    database_path = tmp_path / "operator-scope-filters.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company = Company(name="Scope Company", slug="scope-company", is_active=True)
        db.session.add(company)
        db.session.flush()

        active_department = Department(company_id=company.id, name="Active Dept", is_active=True)
        inactive_department = Department(company_id=company.id, name="Inactive Dept", is_active=False)
        db.session.add_all([active_department, inactive_department])
        db.session.flush()

        active_group = MachineGroup(company_id=company.id, name="Press", is_active=True)
        inactive_group = MachineGroup(company_id=company.id, name="Weld", is_active=False)
        db.session.add_all([active_group, inactive_group])
        db.session.flush()

        valid_machine = Machine(
            company_id=company.id,
            machine_code="M-ACTIVE",
            name="Valid Machine",
            machine_type=active_group.name,
            department_id=active_department.id,
            is_active=True,
        )
        inactive_department_machine = Machine(
            company_id=company.id,
            machine_code="M-INACTIVE-DEPT",
            name="Inactive Department Machine",
            machine_type=active_group.name,
            department_id=inactive_department.id,
            is_active=True,
        )
        inactive_group_machine = Machine(
            company_id=company.id,
            machine_code="M-INACTIVE-GROUP",
            name="Inactive Group Machine",
            machine_type=inactive_group.name,
            department_id=active_department.id,
            is_active=True,
        )
        inactive_machine = Machine(
            company_id=company.id,
            machine_code="M-OFF",
            name="Inactive Machine",
            machine_type=active_group.name,
            department_id=active_department.id,
            is_active=False,
        )
        db.session.add_all([valid_machine, inactive_department_machine, inactive_group_machine, inactive_machine])
        db.session.commit()

        scope = get_scope_filters(
            membership=SimpleNamespace(
                company_id=company.id,
                role="Operator",
                scope_mode="restricted",
                department_id=None,
                machine_group_id=None,
                scope_config_json=json.dumps(
                    {
                        "machine_ids": [
                            valid_machine.id,
                            inactive_department_machine.id,
                            inactive_group_machine.id,
                            inactive_machine.id,
                        ],
                        "machine_group_ids": [active_group.id],
                        "department_ids": [active_department.id, inactive_department.id],
                    }
                ),
                is_restricted=True,
            )
        )

        assert scope["machine_ids"] == [valid_machine.id]
        assert scope["department_ids"] == [active_department.id]
        assert scope["machine_group_names"] == [active_group.name]

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_manager_scope_filters_only_include_active_group_and_department_machines(tmp_path, monkeypatch):
    database_path = tmp_path / "manager-scope-filters.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company = Company(name="Manager Scope Company", slug="manager-scope-company", is_active=True)
        db.session.add(company)
        db.session.flush()

        active_department = Department(company_id=company.id, name="Assembly", is_active=True)
        inactive_department = Department(company_id=company.id, name="Retired Dept", is_active=False)
        db.session.add_all([active_department, inactive_department])
        db.session.flush()

        active_group = MachineGroup(company_id=company.id, name="Line 1", is_active=True)
        inactive_group = MachineGroup(company_id=company.id, name="Line 2", is_active=False)
        db.session.add_all([active_group, inactive_group])
        db.session.flush()

        active_machine = Machine(
            company_id=company.id,
            machine_code="M-L1",
            name="Line 1 Machine",
            machine_type=active_group.name,
            department_id=active_department.id,
            is_active=True,
        )
        inactive_group_machine = Machine(
            company_id=company.id,
            machine_code="M-L2",
            name="Line 2 Machine",
            machine_type=inactive_group.name,
            department_id=active_department.id,
            is_active=True,
        )
        inactive_department_machine = Machine(
            company_id=company.id,
            machine_code="M-OLD",
            name="Old Department Machine",
            machine_type=active_group.name,
            department_id=inactive_department.id,
            is_active=True,
        )
        db.session.add_all([active_machine, inactive_group_machine, inactive_department_machine])
        db.session.commit()

        scope = get_scope_filters(
            membership=SimpleNamespace(
                company_id=company.id,
                role="Manager",
                scope_mode="restricted",
                department_id=None,
                machine_group_id=None,
                scope_config_json=json.dumps(
                    {
                        "machine_group_ids": [active_group.id, inactive_group.id],
                        "department_ids": [active_department.id, inactive_department.id],
                    }
                ),
                is_restricted=True,
            )
        )

        assert scope["machine_ids"] == [active_machine.id]
        assert scope["department_ids"] == [active_department.id]
        assert scope["machine_group_names"] == [active_group.name]

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_operator_scope_config_matches_machine_group_names_after_normalization(tmp_path, monkeypatch):
    database_path = tmp_path / "operator-scope-config-normalized.sqlite3"
    monkeypatch.setenv("TEST_DATABASE_URL", f"sqlite:///{database_path}")

    original_database_uri = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        company = Company(name="Normalized Scope Company", slug="normalized-scope-company", is_active=True)
        db.session.add(company)
        db.session.flush()

        department = Department(company_id=company.id, name="Quality", is_active=True)
        group = MachineGroup(company_id=company.id, name="Press", is_active=True)
        db.session.add_all([department, group])
        db.session.flush()

        machine = Machine(
            company_id=company.id,
            machine_code="PRESS-01",
            name="Press 01",
            machine_type="  press  ",
            department_id=department.id,
            is_active=True,
        )
        db.session.add(machine)
        db.session.commit()

        scope_config, error_response = _resolve_scope_config(
            company_id=company.id,
            role="Operator",
            machine_ids=[machine.id],
            machine_group_ids=[group.id],
            department_ids=[department.id],
        )

        assert error_response is None
        assert scope_config == {
            "machine_ids": [machine.id],
            "machine_group_ids": [group.id],
            "department_ids": [department.id],
        }

        db.session.remove()
        db.drop_all()
    TestingConfig.SQLALCHEMY_DATABASE_URI = original_database_uri


def test_get_default_membership_prefers_users_primary_company(monkeypatch):
    current_user = SimpleNamespace(id=10, company_id=7)
    non_primary = SimpleNamespace(company_id=3, role="Operator", company=SimpleNamespace(id=3, slug="operator-company"))
    primary = SimpleNamespace(company_id=7, role="Admin", company=SimpleNamespace(id=7, slug="admin-company"))

    monkeypatch.setattr("andon_system.security.get_user_memberships", lambda user=None: [non_primary, primary])
    monkeypatch.setattr("andon_system.security.get_authenticated_user", lambda: current_user)

    selected = get_default_membership()

    assert selected is primary


def test_get_default_membership_prefers_single_admin_membership_when_primary_missing(monkeypatch):
    current_user = SimpleNamespace(id=12, company_id=99)
    operator_membership = SimpleNamespace(company_id=3, role="Operator", company=SimpleNamespace(id=3, slug="operator-company"))
    admin_membership = SimpleNamespace(company_id=7, role="Admin", company=SimpleNamespace(id=7, slug="admin-company"))

    monkeypatch.setattr("andon_system.security.get_user_memberships", lambda user=None: [operator_membership, admin_membership])
    monkeypatch.setattr("andon_system.security.get_authenticated_user", lambda: current_user)

    selected = get_default_membership()

    assert selected is admin_membership


def test_cached_pager_device_lookup_does_not_hit_db(monkeypatch):
    from andon_system import security as security_module

    fake_device = SimpleNamespace(
        id=7,
        company_id=3,
        department_id=5,
        name="Dept Pager",
        token_hash="hash-value",
        department=SimpleNamespace(id=5, company_id=3, is_active=True, name="Maintenance"),
    )

    security_module._PAGER_TOKEN_DEVICE_CACHE.clear()
    security_module._cache_pager_device_for_token("cached-token", fake_device)

    class ExplodingQuery:
        def __getattr__(self, _name):
            raise AssertionError("PagerDevice.query should not be used on cache hit")

    setattr(security_module.PagerDevice, "query", ExplodingQuery())
    try:
        cached = security_module._get_cached_pager_device_for_token("cached-token")
    finally:
        delattr(security_module.PagerDevice, "query")

    assert cached is not None
    assert cached.id == 7
    assert cached.company_id == 3
    assert cached.department_id == 5
    assert cached.name == "Dept Pager"
