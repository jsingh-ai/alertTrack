import os

from flask import Flask, g

from .company_context import get_companies, get_current_company, set_current_company_slug
from .config import config_by_name
from .db_maintenance import ensure_andon_schema
from .extensions import db, migrate, socketio
from .routes import register_blueprints


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="templates",
        static_folder="static",
    )

    config_key = config_name or os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "default"
    app.config.from_object(config_by_name.get(config_key, config_by_name["default"]))

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

    from .models import (  # noqa: WPS433
        AndonAlert,
        AndonAlertEvent,
        Company,
        Department,
        EscalationRule,
        IssueCategory,
        IssueProblem,
        Machine,
        MachineGroup,
        User,
    )

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
            "model_refs": {
                "Department": Department,
                "Company": Company,
                "Machine": Machine,
                "MachineGroup": MachineGroup,
                "User": User,
                "IssueCategory": IssueCategory,
                "IssueProblem": IssueProblem,
                "AndonAlert": AndonAlert,
                "AndonAlertEvent": AndonAlertEvent,
                "EscalationRule": EscalationRule,
            },
        }

    @app.before_request
    def load_current_company():
        g.current_company = get_current_company()

    with app.app_context():
        if app.config.get("ANDON_AUTO_SCHEMA_MAINTENANCE"):
            db.create_all()
            ensure_andon_schema()
        app.logger.debug("Andon app initialized")

    return app
