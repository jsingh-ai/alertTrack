# Andon System MVP

Internal manufacturing Andon alert system built with Flask, SQLAlchemy, and PostgreSQL.

## Setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Set the database in `.env`:

```text
DATABASE_URL="postgresql+psycopg://andon_user:password@localhost:5432/andon_db"
```

## Initialize Database

```bash
python scripts/init_andon_db.py
python scripts/seed_andon_data.py
```

### Windows PostgreSQL Setup

After installing PostgreSQL on Windows and installing `requirements.txt`, run this from PowerShell in the repo folder:

```powershell
.\.venv\Scripts\Activate.ps1
.\scripts\setup_postgres_windows.ps1 -DatabaseName andon_db -AppUser andon_user -AppPassword "change_this_password"
```

The script creates the PostgreSQL role/database if needed, sets `DATABASE_URL` for that PowerShell session, creates the tables, and seeds the default data.

It also writes a local `.env` file with `DATABASE_URL`, `SECRET_KEY`, `HOST`, `PORT`, and `SOCKETIO_ENABLED`, so future runs only need:

```powershell
python run_socketio.py
```

## Run the App

For local development:

```bash
flask --app andon_system:create_app run
```

For local Socket.IO testing with the production entrypoint:

```bash
python run_socketio.py
```

`run_socketio.py` reads `.env` automatically and defaults to `0.0.0.0:5001` when `HOST` and `PORT` are not set.

For production deployments, use a WebSocket-capable worker. Normal Gunicorn sync workers are not enough for WebSockets.

Recommended production environment variables:

```bash
export DATABASE_URL=postgresql+psycopg://andon_user:password@host:5432/andon_db
export REDIS_URL=redis://host:6379/0
export REDIS_REQUIRED=true
export SOCKETIO_ENABLED=true
export SOCKETIO_MESSAGE_QUEUE=$REDIS_URL
export SECRET_KEY=use-a-long-random-secret
```

Recommended production command:

```bash
gunicorn --worker-class gthread --threads 8 -w 1 --bind 0.0.0.0:8000 wsgi:app
```

`REDIS_REQUIRED=true` is recommended in production. If `REDIS_REQUIRED` is left off, the app can fall back to in-memory cache behavior in development when Redis is unavailable. Redis is used for cache and the Socket.IO message queue only; it is not the source of truth for alerts, users, machines, reports, or machine status.

For multiple app instances or workers, Redis is required for Socket.IO message fan-out. Put Nginx or your reverse proxy in front with WebSocket upgrade headers for `/socket.io/`, including `Upgrade`, `Connection`, `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto`.

## Pages

- `/andon` dashboard landing page
- `/andon/operator` machine button board with modal alert creation
- `/andon/board` live TV board
- `/andon/reports` reporting dashboard
- `/andon/admin` admin setup

## API

The system exposes JSON endpoints under `/api/andon` for machines, departments, department-linked issues, alerts, reports, and escalation checks.

## Database Notes

- Timestamps are stored in UTC.
- `DATABASE_URL` must point to PostgreSQL.
- Flask-Migrate is supported if installed, but the project also ships with clean initialization scripts.
- Machines now support `machine_type` in addition to `machine_code`, `name`, `area`, and `line`.
- Departments own the issue list in the UI; the internal category table remains as a compatibility layer.

## Future Hardware Integration

- Operator screen can be wired to touchscreen kiosks or stack-light buttons.
- Escalation notifications currently use a placeholder service and can later be connected to SMS, email, radios, or PLC/OEE systems.
