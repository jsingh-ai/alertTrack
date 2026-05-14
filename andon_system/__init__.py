import os

from flask import Flask, g

from .company_context import get_companies, get_current_company
from .config import config_by_name
from .extensions import db, migrate, socketio
from .routes import register_blueprints
from .security import enforce_csrf, generate_csrf_token, is_admin_authenticated, validate_production_security_config


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
