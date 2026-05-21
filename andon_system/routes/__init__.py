from flask import Flask

from .admin import admin_bp
from .api import api_bp
from .pages import pages_bp


def register_blueprints(app: Flask):
    pager_api_only = bool(app.config.get("ANDON_PAGER_API_ONLY"))
    if not pager_api_only:
        app.register_blueprint(pages_bp)
        app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
