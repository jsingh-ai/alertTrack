import os
import shlex
import time

from flask import Flask, flash, g, has_request_context, redirect, request, session, url_for
from sqlalchemy import inspect, text

from .company_context import get_companies, get_current_company
from .config import config_by_name
from .extensions import db, migrate, socketio
from .routes import register_blueprints
from .security import (
    enforce_csrf,
    ensure_session_company,
    generate_csrf_token,
    get_accessible_companies,
    get_authenticated_user,
    get_current_membership,
    is_admin_authenticated,
    is_authenticated,
    logout_user,
    validate_production_security_config,
)

WORKSPACE_PROMPT_SESSION_KEY = "andon_workspace_prompt"


def _detect_gunicorn_worker_count() -> int:
    for env_name in ("GUNICORN_WORKERS", "WEB_CONCURRENCY"):
        raw_value = os.getenv(env_name)
        if raw_value and str(raw_value).strip().isdigit():
            return max(1, int(raw_value))

    raw_args = os.getenv("GUNICORN_CMD_ARGS", "")
    if not raw_args:
        return 1

    try:
        parts = shlex.split(raw_args)
    except ValueError:
        return 1

    for index, part in enumerate(parts):
        if part in {"-w", "--workers"} and index + 1 < len(parts):
            next_value = parts[index + 1]
            if str(next_value).isdigit():
                return max(1, int(next_value))
        if part.startswith("--workers="):
            _, _, value = part.partition("=")
            if str(value).isdigit():
                return max(1, int(value))
    return 1


def _validate_socketio_runtime_config(config: dict) -> None:
    if not config.get("SOCKETIO_ENABLED"):
        return

    allow_multiworker = bool(config.get("SOCKETIO_ALLOW_MULTIWORKER"))
    message_queue = str(config.get("SOCKETIO_MESSAGE_QUEUE") or "").strip()
    async_mode = str(config.get("SOCKETIO_ASYNC_MODE") or "").strip().lower()

    worker_count = _detect_gunicorn_worker_count()
    if allow_multiworker and worker_count > 1 and not message_queue:
        raise RuntimeError(
            "SOCKETIO_ALLOW_MULTIWORKER=true requires SOCKETIO_MESSAGE_QUEUE "
            "(typically Redis) when running multiple workers."
        )

    if not allow_multiworker and worker_count > 1:
        raise RuntimeError(
            "Socket.IO is configured with multiple Gunicorn workers "
            f"(detected workers={worker_count}). This app defaults to single-worker "
            "Socket.IO mode to avoid sid/namespace room-join errors. "
            "Use one worker (`-w 1`) or explicitly set "
            "`SOCKETIO_ALLOW_MULTIWORKER=true` only if your deployment is prepared "
            "for it."
        )

    if async_mode == "threading" and worker_count > 1 and not allow_multiworker:
        raise RuntimeError(
            "SOCKETIO_ASYNC_MODE=threading with multiple workers requires "
            "SOCKETIO_ALLOW_MULTIWORKER=true and SOCKETIO_MESSAGE_QUEUE."
        )


def _ensure_auth_schema() -> None:
    inspector = inspect(db.engine)
    if "users" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    statements = []
    if "password_hash" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
    if "last_login_at" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN last_login_at TIMESTAMPTZ")

    for statement in statements:
        db.session.execute(text(statement))

    dialect_name = db.engine.dialect.name
    if dialect_name == "postgresql":
        db.session.execute(text("UPDATE users SET role = 'Manager' WHERE role = 'Supervisor'"))
        db.session.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role_allowed"))
        db.session.execute(
            text(
                "ALTER TABLE users "
                "ADD CONSTRAINT ck_users_role_allowed "
                "CHECK (role IN ('Admin', 'Manager', 'Operator', 'Viewer'))"
            )
        )

    from .models.user import UserBoard, UserBoardItem, UserCompanyAccess, UserViewPreference

    UserCompanyAccess.__table__.create(bind=db.engine, checkfirst=True)
    UserViewPreference.__table__.create(bind=db.engine, checkfirst=True)
    UserBoard.__table__.create(bind=db.engine, checkfirst=True)
    UserBoardItem.__table__.create(bind=db.engine, checkfirst=True)
    db.session.flush()

    users = db.session.execute(
        text(
            "SELECT id, company_id, role, department_id, machine_group_id, is_active "
            "FROM users"
        )
    ).mappings().all()
    for row in users:
        legacy_role = row["role"] if row["role"] in {"Admin", "Manager", "Operator"} else "Manager"
        access_exists = db.session.execute(
            text(
                "SELECT 1 FROM user_company_access "
                "WHERE user_id = :user_id AND company_id = :company_id"
            ),
            {"user_id": row["id"], "company_id": row["company_id"]},
        ).scalar()
        if access_exists:
            continue
        db.session.execute(
            text(
                "INSERT INTO user_company_access "
                "(user_id, company_id, role, scope_mode, department_id, machine_group_id, scope_config_json, is_active) "
                "VALUES (:user_id, :company_id, :role, :scope_mode, :department_id, :machine_group_id, :scope_config_json, :is_active)"
            ),
            {
                "user_id": row["id"],
                "company_id": row["company_id"],
                "role": legacy_role,
                "scope_mode": "all" if legacy_role == "Admin" else "restricted",
                "department_id": row["department_id"],
                "machine_group_id": row["machine_group_id"],
                "scope_config_json": "{}",
                "is_active": row["is_active"],
            },
        )
    existing_access_columns = {column["name"] for column in inspector.get_columns("user_company_access")}
    if "scope_config_json" not in existing_access_columns:
        db.session.execute(text("ALTER TABLE user_company_access ADD COLUMN scope_config_json TEXT DEFAULT '{}'"))
        db.session.execute(text("UPDATE user_company_access SET scope_config_json = '{}' WHERE scope_config_json IS NULL"))
    if dialect_name == "postgresql":
        db.session.execute(text("UPDATE user_company_access SET role = 'Manager' WHERE role = 'Supervisor'"))
        db.session.execute(text("ALTER TABLE user_company_access DROP CONSTRAINT IF EXISTS ck_user_company_access_role"))
        db.session.execute(
            text(
                "ALTER TABLE user_company_access "
                "ADD CONSTRAINT ck_user_company_access_role "
                "CHECK (role IN ('Admin', 'Manager', 'Operator', 'Viewer'))"
            )
        )
        _ensure_postgres_performance_indexes()
    _ensure_pager_device_token_fingerprint_schema(allow_alter=True)
    db.session.commit()
    _ensure_sqlite_user_role_constraints()
    _ensure_sqlite_active_alert_unique_index()


def _ensure_sqlite_active_alert_unique_index() -> None:
    if db.engine.dialect.name != "sqlite":
        return

    inspector = inspect(db.engine)
    if "andon_alerts" not in inspector.get_table_names():
        return

    expected_where = "WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'ARRIVED')"
    existing_sql = db.session.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'uq_andon_alerts_active_machine'"
        )
    ).scalar()

    normalized_sql = (existing_sql or "").upper()
    if expected_where in normalized_sql:
        return

    duplicate_rows = db.session.execute(
        text(
            "SELECT machine_id, COUNT(*) AS row_count "
            "FROM andon_alerts "
            "WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'ARRIVED') "
            "GROUP BY machine_id "
            "HAVING COUNT(*) > 1"
        )
    ).mappings().all()
    if duplicate_rows:
        row_preview = ", ".join(f"machine_id={row['machine_id']} count={row['row_count']}" for row in duplicate_rows[:5])
        raise RuntimeError(
            "Cannot repair uq_andon_alerts_active_machine because duplicate active alerts exist: "
            f"{row_preview}"
        )

    db.session.execute(text("DROP INDEX IF EXISTS uq_andon_alerts_active_machine"))
    db.session.execute(
        text(
            "CREATE UNIQUE INDEX uq_andon_alerts_active_machine "
            "ON andon_alerts (machine_id) "
            "WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'ARRIVED')"
        )
    )
    db.session.commit()


def _ensure_sqlite_user_role_constraints() -> None:
    if db.engine.dialect.name != "sqlite":
        return

    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if "users" not in table_names or "user_company_access" not in table_names:
        return

    users_sql = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
    ).scalar() or ""
    access_sql = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_company_access'")
    ).scalar() or ""

    users_ok = "ROLE IN ('ADMIN', 'MANAGER', 'OPERATOR', 'VIEWER')" in users_sql.upper()
    access_ok = "ROLE IN ('ADMIN', 'MANAGER', 'OPERATOR', 'VIEWER')" in access_sql.upper()
    has_scope_config = "scope_config_json" in {column["name"] for column in inspector.get_columns("user_company_access")}
    if users_ok and access_ok and has_scope_config:
        return

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.execute(text("BEGIN"))
    try:
        db.session.execute(
            text(
                """
                CREATE TABLE users_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    company_id INTEGER NOT NULL,
                    employee_id VARCHAR(64),
                    display_name VARCHAR(160) NOT NULL,
                    username VARCHAR(80),
                    role VARCHAR(80) NOT NULL,
                    email VARCHAR(160),
                    phone_number VARCHAR(32),
                    password_hash VARCHAR(255),
                    department_id INTEGER,
                    machine_group_id INTEGER,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    last_login_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_users_company_username UNIQUE (company_id, username),
                    CONSTRAINT ck_users_role_allowed CHECK (role IN ('Admin', 'Manager', 'Operator', 'Viewer')),
                    FOREIGN KEY(company_id) REFERENCES companies (id),
                    FOREIGN KEY(department_id) REFERENCES departments (id),
                    FOREIGN KEY(machine_group_id) REFERENCES machine_groups (id)
                )
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO users_new
                (id, company_id, employee_id, display_name, username, role, email, phone_number, password_hash, department_id, machine_group_id, is_active, last_login_at, created_at)
                SELECT id, company_id, employee_id, display_name, username,
                    CASE WHEN role = 'Supervisor' THEN 'Manager' ELSE role END,
                    email, phone_number, password_hash, department_id, machine_group_id, is_active, last_login_at, created_at
                FROM users
                """
            )
        )
        db.session.execute(text("DROP TABLE users"))
        db.session.execute(text("ALTER TABLE users_new RENAME TO users"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_company_id ON users (company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_department_id ON users (department_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_machine_group_id ON users (machine_group_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_is_active ON users (is_active)"))
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_role ON users (role)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_phone_number ON users (phone_number)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_users_employee_id ON users (employee_id)"))

        db.session.execute(
            text(
                """
                CREATE TABLE user_company_access_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    company_id INTEGER NOT NULL,
                    role VARCHAR(80) NOT NULL,
                    scope_mode VARCHAR(32) NOT NULL,
                    department_id INTEGER,
                    machine_group_id INTEGER,
                    scope_config_json TEXT NOT NULL DEFAULT '{}',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_company_access_user_company UNIQUE (user_id, company_id),
                    CONSTRAINT ck_user_company_access_role CHECK (role IN ('Admin', 'Manager', 'Operator', 'Viewer')),
                    CONSTRAINT ck_user_company_access_scope_mode CHECK (scope_mode IN ('all', 'restricted')),
                    FOREIGN KEY(user_id) REFERENCES users (id),
                    FOREIGN KEY(company_id) REFERENCES companies (id),
                    FOREIGN KEY(department_id) REFERENCES departments (id),
                    FOREIGN KEY(machine_group_id) REFERENCES machine_groups (id)
                )
                """
            )
        )
        if has_scope_config:
            db.session.execute(
                text(
                    """
                    INSERT INTO user_company_access_new
                    (id, user_id, company_id, role, scope_mode, department_id, machine_group_id, scope_config_json, is_active, created_at, updated_at)
                    SELECT id, user_id, company_id,
                        CASE WHEN role = 'Supervisor' THEN 'Manager' ELSE role END,
                        scope_mode, department_id, machine_group_id,
                        COALESCE(scope_config_json, '{}'),
                        is_active, created_at, updated_at
                    FROM user_company_access
                    """
                )
            )
        else:
            db.session.execute(
                text(
                    """
                    INSERT INTO user_company_access_new
                    (id, user_id, company_id, role, scope_mode, department_id, machine_group_id, scope_config_json, is_active, created_at, updated_at)
                    SELECT id, user_id, company_id,
                        CASE WHEN role = 'Supervisor' THEN 'Manager' ELSE role END,
                        scope_mode, department_id, machine_group_id, '{}',
                        is_active, created_at, updated_at
                    FROM user_company_access
                    """
                )
            )
        db.session.execute(text("DROP TABLE user_company_access"))
        db.session.execute(text("ALTER TABLE user_company_access_new RENAME TO user_company_access"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_user_company_access_user_id ON user_company_access (user_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_user_company_access_user_active_company ON user_company_access (user_id, is_active, company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_user_company_access_company_id ON user_company_access (company_id)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_user_company_access_is_active ON user_company_access (is_active)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_user_company_access_company_active_role_id ON user_company_access (company_id, is_active, role, id)"))
        db.session.execute(text("COMMIT"))
    except Exception:
        db.session.execute(text("ROLLBACK"))
        raise
    finally:
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        db.session.commit()


def _ensure_postgres_performance_indexes() -> None:
    if db.engine.dialect.name != "postgresql":
        return
    # Pager polls and operator snapshot reads.
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alerts_company_department_status_priority_created "
            "ON andon_alerts (company_id, department_id, status, priority DESC, created_at ASC)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alerts_company_status_created "
            "ON andon_alerts (company_id, status, created_at DESC)"
        )
    )
    # Created-note lookup for active alerts.
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alert_events_company_type_alert_event "
            "ON andon_alert_events (company_id, event_type, alert_id, event_at ASC)"
        )
    )
    # Operator metadata user resolution by company + scope.
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_user_company_access_company_active_department "
            "ON user_company_access (company_id, is_active, department_id)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_user_company_access_company_active_group "
            "ON user_company_access (company_id, is_active, machine_group_id)"
        )
    )
    # Machine lists for board/operator reads.
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_machines_company_type_name "
            "ON machines (company_id, machine_type, name)"
        )
    )


def _ensure_pager_device_token_fingerprint_schema(allow_alter: bool) -> None:
    inspector = inspect(db.engine)
    if "pager_devices" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("pager_devices")}
    has_fingerprint = "token_fingerprint" in columns
    if not has_fingerprint and allow_alter:
        db.session.execute(text("ALTER TABLE pager_devices ADD COLUMN token_fingerprint VARCHAR(64)"))
        has_fingerprint = True
    if not has_fingerprint:
        return
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pager_devices_token_fingerprint "
            "ON pager_devices (token_fingerprint)"
        )
    )


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="templates",
        static_folder="static",
    )

    config_key = config_name or os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "default"
    app.config.from_object(config_by_name.get(config_key, config_by_name["default"]))
    if app.config.get("ANDON_PAGER_API_ONLY"):
        app.config["SOCKETIO_ENABLED"] = False
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL must be configured for PostgreSQL.")
    if config_key == "production":
        validate_production_security_config(app.config)
    _validate_socketio_runtime_config(app.config)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    if migrate is not None:
        migrate.init_app(app, db)
    if socketio is not None and app.config.get("SOCKETIO_ENABLED"):
        socketio.init_app(
            app,
            async_mode=app.config.get("SOCKETIO_ASYNC_MODE"),
            message_queue=app.config.get("SOCKETIO_MESSAGE_QUEUE"),
            cors_allowed_origins=None,
            ping_interval=app.config.get("SOCKETIO_PING_INTERVAL"),
            ping_timeout=app.config.get("SOCKETIO_PING_TIMEOUT"),
            http_compression=app.config.get("SOCKETIO_HTTP_COMPRESSION"),
        )
        from .socket_events import register_socket_events  # noqa: WPS433

        register_socket_events(socketio)

    register_blueprints(app)

    from . import models  # noqa: F401,WPS433

    @app.template_filter("utc_local")
    def utc_local(value):
        from .services.reporting_service import format_local_datetime

        return format_local_datetime(value)

    @app.context_processor
    def inject_globals():
        current_user = get_authenticated_user()
        show_workspace_prompt = (
            request.endpoint == "pages.home_page"
            and is_authenticated()
            and bool(session.get(WORKSPACE_PROMPT_SESSION_KEY))
        )
        current_company = None if show_workspace_prompt else get_current_company()
        current_membership = None if show_workspace_prompt else get_current_membership()
        companies = []
        if show_workspace_prompt:
            companies = []
        elif is_authenticated():
            companies = get_accessible_companies()
        else:
            companies = get_companies()
        return {
            "app_name": "ProcessGuard AI - Live Alert System",
            "app_brand": "ProcessGuard AI",
            "app_subtitle": "Live Alert System",
            "current_company": current_company,
            "companies": companies,
            "current_user": current_user,
            "current_membership": current_membership,
            "socketio_enabled": socketio is not None and app.config.get("SOCKETIO_ENABLED"),
            "socketio_async_mode": app.config.get("SOCKETIO_ASYNC_MODE"),
            "socketio_force_polling": bool(app.config.get("SOCKETIO_FORCE_POLLING")),
            "admin_authenticated": is_admin_authenticated(),
            "user_authenticated": is_authenticated(),
            "csrf_token": generate_csrf_token(),
        }

    @app.before_request
    def load_current_company():
        if has_request_context():
            g.request_started_at = time.perf_counter()
        # Static assets, Socket.IO handshakes, and logout should not pay
        # auth/company preload cost.
        if (
            request.path.startswith("/static/")
            or request.path.startswith("/socket.io/")
            or request.path.startswith("/api/andon/pager/")
        ):
            return
        enforce_csrf()
        if request.endpoint == "pages.logout_page":
            return
        if request.path == "/andon/home" and is_authenticated() and session.get(WORKSPACE_PROMPT_SESSION_KEY):
            return
        g.current_company = get_current_company()
        if is_authenticated():
            ensure_session_company()

    @app.after_request
    def log_request_timing(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if request.path in {"/andon/home", "/andon/login"}:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if not app.config.get("ANDON_PERF_LOGS"):
            return response
        started_at = getattr(g, "request_started_at", None)
        if started_at is None:
            return response
        duration_ms = (time.perf_counter() - started_at) * 1000
        # Always log dynamic app routes; static routes only when noticeably slow.
        should_log = (not request.path.startswith("/static/")) or duration_ms >= 200
        if should_log:
            app.logger.debug(
                "PERF request method=%s path=%s status=%s duration_ms=%.1f remote=%s",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
                request.remote_addr,
            )
        return response

    @app.errorhandler(400)
    def handle_bad_request(error):
        description = str(getattr(error, "description", "") or "")
        is_login_csrf = (
            request.path == "/andon/login"
            and request.method == "POST"
            and "CSRF validation failed" in description
        )
        if is_login_csrf:
            logout_user()
            flash("Your session expired. Please sign in again.", "warning")
            return redirect(url_for("pages.home_page"))
        return error

    with app.app_context():
        if app.config.get("ANDON_AUTO_SCHEMA_MAINTENANCE"):
            db.create_all()
        if app.config.get("ANDON_RUNTIME_SCHEMA_REPAIR"):
            _ensure_auth_schema()
        else:
            # Keep critical read-path indexes present when runtime repair is off.
            if db.engine.dialect.name == "postgresql":
                _ensure_postgres_performance_indexes()
            # Pager token fingerprint support is required by the current model.
            _ensure_pager_device_token_fingerprint_schema(allow_alter=True)
            db.session.commit()
        app.logger.debug("Andon app initialized")

    return app
