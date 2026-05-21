import os

os.environ.setdefault("ANDON_PAGER_API_ONLY", "true")
os.environ.setdefault("SOCKETIO_ENABLED", "false")
os.environ.setdefault("HOST", "0.0.0.0")
os.environ.setdefault("PORT", "5003")

from andon_system import create_app


def main():
    app = create_app(os.getenv("FLASK_CONFIG") or os.getenv("APP_ENV") or "development")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5003"))
    app.run(host=host, port=port, debug=app.debug)


if __name__ == "__main__":
    main()
