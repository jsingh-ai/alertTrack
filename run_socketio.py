import os

from andon_system import create_app
from andon_system.extensions import socketio


app = create_app(os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "development")


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    if socketio is not None and app.config.get("SOCKETIO_ENABLED"):
        socketio.run(app, host=host, port=port, debug=app.debug, allow_unsafe_werkzeug=True)
    else:
        app.run(host=host, port=port, debug=app.debug)
