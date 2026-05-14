import os

from andon_system import create_app
from andon_system.extensions import socketio


def _ensure_safe_werkzeug(app):
    if app.debug:
        return
    raise RuntimeError("run_socketio.py only supports Werkzeug in development/debug mode.")


def main():
    app = create_app(os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "development")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5001"))
    if socketio is not None and app.config.get("SOCKETIO_ENABLED"):
        _ensure_safe_werkzeug(app)
        socketio.run(app, host=host, port=port, debug=app.debug, allow_unsafe_werkzeug=True)
        return
    app.run(host=host, port=port, debug=app.debug)


if __name__ == "__main__":
    main()
