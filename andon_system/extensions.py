from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

try:
    from flask_migrate import Migrate
except ImportError:  # pragma: no cover - optional dependency
    Migrate = None

try:
    from flask_socketio import SocketIO
except ImportError:  # pragma: no cover - optional dependency
    SocketIO = None


db = SQLAlchemy()
migrate = Migrate(compare_type=True, render_as_batch=True) if Migrate is not None else None
socketio = SocketIO() if SocketIO is not None else None
