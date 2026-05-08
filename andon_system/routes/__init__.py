from flask import Flask

from .admin import admin_bp
from .api import api_bp
from .pages import pages_bp


def register_blueprints(app: Flask):
    app.register_blueprint(pages_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
