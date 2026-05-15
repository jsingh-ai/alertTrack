import os
import shlex

from flask import Flask, g

from .company_context import get_companies, get_current_company
from .config import config_by_name
from .extensions import db, migrate, socketio
from .routes import register_blueprints
from .security import enforce_csrf, generate_csrf_token, is_admin_authenticated, validate_production_security_config


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
    if config.get("SOCKETIO_ALLOW_MULTIWORKER"):
        return

    worker_count = _detect_gunicorn_worker_count()
    if worker_count <= 1:
        return

    raise RuntimeError(
        "Socket.IO is configured with multiple Gunicorn workers "
        f"(detected workers={worker_count}). This app defaults to single-worker "
        "Socket.IO mode to avoid sid/namespace room-join errors. "
        "Use one worker (`-w 1`) or explicitly set "
        "`SOCKETIO_ALLOW_MULTIWORKER=true` only if your deployment is prepared "
        "for it."
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
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL must be configured for PostgreSQL.")
    if config_key == "production":
        validate_production_security_config(app.config)
    _validate_socketio_runtime_config(app.config)

    db.init_app(app)
    if migrate is not None:
        migrate.init_app(app, db)
    if socketio is not None and app.config.get("SOCKETIO_ENABLED"):
        socketio.init_app(
            app,
            async_mode=app.config.get("SOCKETIO_ASYNC_MODE"),
            message_queue=app.config.get("SOCKETIO_MESSAGE_QUEUE"),
            cors_allowed_origins=None,
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
        current_company = get_current_company()
        return {
            "app_name": "ProcessGuard AI - Live Alert System",
            "app_brand": "ProcessGuard AI",
            "app_subtitle": "Live Alert System",
            "current_company": current_company,
            "companies": get_companies(),
            "socketio_enabled": socketio is not None and app.config.get("SOCKETIO_ENABLED"),
            "admin_authenticated": is_admin_authenticated(),
            "csrf_token": generate_csrf_token(),
        }

    @app.before_request
    def load_current_company():
        enforce_csrf()
        g.current_company = get_current_company()

    with app.app_context():
        if app.config.get("ANDON_AUTO_SCHEMA_MAINTENANCE"):
            db.create_all()
        app.logger.debug("Andon app initialized")

    return app
