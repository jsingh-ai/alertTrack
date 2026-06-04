import os
import shlex
import time
import logging

from flask import Flask, current_app, flash, g, has_request_context, redirect, request, session, url_for
from flask import jsonify
from sqlalchemy import event
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from werkzeug.middleware.proxy_fix import ProxyFix

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
from .services.cache_service import cache_runtime_status

WORKSPACE_PROMPT_SESSION_KEY = "andon_workspace_prompt"
_SA_PERF_LISTENERS_REGISTERED = False


class _PerfFocusFilter(logging.Filter):
    def __init__(self, patterns: list[str]):
        super().__init__()
        self._patterns = [str(pattern) for pattern in patterns if str(pattern).strip()]

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG:
            return True
        message = record.getMessage()
        return any(pattern in message for pattern in self._patterns)


def _configure_perf_focus_logging(app: Flask) -> None:
    focus_mode = str(app.config.get("ANDON_PERF_FOCUS") or "").strip().lower()
    if focus_mode != "alert_mutations":
        return
    patterns = [
        "PERF alert_acknowledge",
        "PERF alert_resolve",
        "PERF alert_cancel",
        "PERF alert_mutation action=acknowledge",
        "PERF alert_mutation action=resolve",
        "PERF alert_mutation action=cancel",
        "method=POST path=/api/andon/alerts/",
    ]
    extra_patterns = app.config.get("ANDON_PERF_FOCUS_PATTERNS") or []
    patterns.extend([str(item) for item in extra_patterns if str(item).strip()])
    perf_filter = _PerfFocusFilter(patterns)
    for handler in app.logger.handlers:
        handler.addFilter(perf_filter)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _register_sqlalchemy_perf_listeners() -> None:
    global _SA_PERF_LISTENERS_REGISTERED
    if _SA_PERF_LISTENERS_REGISTERED:
        return
    _SA_PERF_LISTENERS_REGISTERED = True

    def _listener_log(name: str, started_at: float, sess: Session):
        if has_request_context() and current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF sqlalchemy_listener listener_name=%s duration_ms=%.1f new=%s dirty=%s deleted=%s",
                name,
                (time.perf_counter() - started_at) * 1000,
                len(getattr(sess, "new", []) or []),
                len(getattr(sess, "dirty", []) or []),
                len(getattr(sess, "deleted", []) or []),
            )

    @event.listens_for(Session, "before_flush")
    def _before_flush(sess, flush_context, instances):  # noqa: ANN001
        started_at = time.perf_counter()
        sess.info["__perf_before_flush_at"] = started_at
        _listener_log("before_flush", started_at, sess)

    @event.listens_for(Session, "after_flush")
    def _after_flush(sess, flush_context):  # noqa: ANN001
        started_at = time.perf_counter()
        prev = sess.info.get("__perf_before_flush_at")
        if prev and has_request_context() and current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF sqlalchemy_listener listener_name=flush_roundtrip duration_ms=%.1f",
                (started_at - prev) * 1000,
            )
        _listener_log("after_flush", started_at, sess)

    @event.listens_for(Session, "after_flush_postexec")
    def _after_flush_postexec(sess, flush_context):  # noqa: ANN001
        started_at = time.perf_counter()
        _listener_log("after_flush_postexec", started_at, sess)

    @event.listens_for(Session, "before_commit")
    def _before_commit(sess):  # noqa: ANN001
        started_at = time.perf_counter()
        sess.info["__perf_before_commit_at"] = started_at
        _listener_log("before_commit", started_at, sess)

    @event.listens_for(Session, "after_commit")
    def _after_commit(sess):  # noqa: ANN001
        started_at = time.perf_counter()
        prev = sess.info.get("__perf_before_commit_at")
        if prev and has_request_context() and current_app.config.get("ANDON_PERF_LOGS"):
            current_app.logger.debug(
                "PERF sqlalchemy_listener listener_name=commit_roundtrip duration_ms=%.1f",
                (started_at - prev) * 1000,
            )
        _listener_log("after_commit", started_at, sess)


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
        _ensure_postgres_performance_indexes(allow_alter=True)
    _ensure_pager_device_token_fingerprint_schema(allow_alter=True)
    db.session.commit()


def _ensure_postgres_performance_indexes(allow_alter: bool = False) -> None:
    if db.engine.dialect.name != "postgresql":
        return
    if allow_alter:
        db.session.execute(text("DROP INDEX IF EXISTS uq_andon_alerts_active_machine"))
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_andon_alerts_active_machine "
                "ON andon_alerts (machine_id, department_id) "
                "WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'ARRIVED')"
            )
        )
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
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alerts_company_status_machine "
            "ON andon_alerts (company_id, status, machine_id)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alerts_company_machine_status "
            "ON andon_alerts (company_id, machine_id, status)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_andon_alerts_machine_status "
            "ON andon_alerts (machine_id, status)"
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
    # Operator metadata issue lookups.
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_issue_categories_company_active_department_id "
            "ON issue_categories (company_id, is_active, department_id, id)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_issue_problems_company_active_category_id "
            "ON issue_problems (company_id, is_active, category_id, id)"
        )
    )
    db.session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_issue_problems_category_active_id "
            "ON issue_problems (category_id, is_active, id)"
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


def _maybe_apply_proxy_fix(app: Flask) -> None:
    proxy_kwargs = {
        "x_for": int(app.config.get("PROXY_FIX_X_FOR") or 0),
        "x_proto": int(app.config.get("PROXY_FIX_X_PROTO") or 0),
        "x_host": int(app.config.get("PROXY_FIX_X_HOST") or 0),
        "x_port": int(app.config.get("PROXY_FIX_X_PORT") or 0),
        "x_prefix": int(app.config.get("PROXY_FIX_X_PREFIX") or 0),
    }
    if not any(proxy_kwargs.values()):
        return
    app.wsgi_app = ProxyFix(app.wsgi_app, **proxy_kwargs)


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="templates",
        static_folder="static",
    )

    config_key = config_name or os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "default"
    app.config.from_object(config_by_name.get(config_key, config_by_name["default"]))
    _configure_perf_focus_logging(app)
    if not app.config.get("ANDON_HTTP_ACCESS_LOGS"):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if app.config.get("ANDON_PAGER_API_ONLY"):
        app.config["SOCKETIO_ENABLED"] = False
    _maybe_apply_proxy_fix(app)
    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not database_uri:
        raise RuntimeError("DATABASE_URL must be configured for PostgreSQL.")
    if not app.config.get("TESTING"):
        try:
            dialect_name = make_url(database_uri).get_backend_name()
        except Exception as exc:  # pragma: no cover - defensive config validation.
            raise RuntimeError("DATABASE_URL is invalid; a PostgreSQL URL is required.") from exc
        if dialect_name != "postgresql":
            raise RuntimeError(
                "PostgreSQL is required for runtime environments. "
                "Set DATABASE_URL to a PostgreSQL connection string."
            )
    if config_key == "production":
        validate_production_security_config(app.config)
    _validate_socketio_runtime_config(app.config)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    _register_sqlalchemy_perf_listeners()
    if migrate is not None:
        migrate.init_app(app, db)
    if socketio is not None and app.config.get("SOCKETIO_ENABLED"):
        socketio.init_app(
            app,
            async_mode=app.config.get("SOCKETIO_ASYNC_MODE"),
            # Pilot mode: disable Redis-backed fanout and keep Socket.IO local.
            message_queue=None,
            cors_allowed_origins=None,
            ping_interval=app.config.get("SOCKETIO_PING_INTERVAL"),
            ping_timeout=app.config.get("SOCKETIO_PING_TIMEOUT"),
            http_compression=app.config.get("SOCKETIO_HTTP_COMPRESSION"),
        )
        from .socket_events import register_socket_events  # noqa: WPS433

        register_socket_events(socketio)

    register_blueprints(app)

    from . import models  # noqa: F401,WPS433

    @app.get("/health")
    def health_check():
        dialect_name = None
        try:
            dialect_name = db.engine.dialect.name
        except Exception:
            dialect_name = None
        payload = {
            "status": "ok",
            "checks": {
                "app": "ok",
                "db": {"ok": False, "dialect": dialect_name},
                "cache": cache_runtime_status(),
                "socketio": {
                    "enabled": bool(app.config.get("SOCKETIO_ENABLED")),
                    "async_mode": str(app.config.get("SOCKETIO_ASYNC_MODE") or ""),
                    "message_queue_configured": bool(app.config.get("SOCKETIO_MESSAGE_QUEUE")),
                    "multiworker_enabled": bool(app.config.get("SOCKETIO_ALLOW_MULTIWORKER")),
                },
            },
        }
        status_code = 200
        try:
            db.session.execute(text("SELECT 1"))
            payload["checks"]["db"] = {
                "ok": True,
                "dialect": dialect_name,
            }
        except Exception:
            db.session.rollback()
            payload["status"] = "degraded"
            payload["checks"]["db"] = {
                "ok": False,
                "dialect": dialect_name,
            }
            status_code = 503
            app.logger.exception("HEALTHCHECK failed component=db")
        return jsonify(payload), status_code

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
        after_request_started_at = time.perf_counter()
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
            if request.path == "/andon/login" and request.method == "POST":
                route_total_ms = float(getattr(g, "login_route_total_ms", 0.0) or 0.0)
                if route_total_ms > 0:
                    app.logger.debug(
                        "PERF login_post_hooks route_total_ms=%.1f request_total_ms=%.1f outside_route_ms=%.1f after_request_ms=%.1f",
                        route_total_ms,
                        duration_ms,
                        max(0.0, duration_ms - route_total_ms),
                        (time.perf_counter() - after_request_started_at) * 1000,
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
        # PostgreSQL-only runtime: schema creation/repair must be migration-driven.
        if db.engine.dialect.name == "postgresql":
            _ensure_postgres_performance_indexes(allow_alter=False)
        _ensure_pager_device_token_fingerprint_schema(allow_alter=False)
        db.session.commit()
        app.logger.debug(
            "Andon app initialized config=%s db_driver=%s secure_cookie=%s proxy_fix=%s/%s/%s/%s/%s pager_api_only=%s socketio_enabled=%s",
            config_key,
            db.engine.dialect.name,
            bool(app.config.get("SESSION_COOKIE_SECURE")),
            app.config.get("PROXY_FIX_X_FOR", 0),
            app.config.get("PROXY_FIX_X_PROTO", 0),
            app.config.get("PROXY_FIX_X_HOST", 0),
            app.config.get("PROXY_FIX_X_PORT", 0),
            app.config.get("PROXY_FIX_X_PREFIX", 0),
            bool(app.config.get("ANDON_PAGER_API_ONLY")),
            bool(app.config.get("SOCKETIO_ENABLED")),
        )

    return app
