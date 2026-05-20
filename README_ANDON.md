# Andon System MVP

Internal manufacturing Andon alert system built with Flask, SQLAlchemy, and PostgreSQL. Local development can use SQLite when PostgreSQL is not configured.

## Setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. For production-like local testing, set PostgreSQL in `.env`:

```text
DATABASE_URL="postgresql+psycopg://andon_user:password@localhost:5432/andon_db"
```

For quick local testing without PostgreSQL, omit `DATABASE_URL` or set:

```text
LOCAL_SQLITE_FALLBACK="true"
LOCAL_SQLITE_PATH="instance/andon_local.sqlite3"
FLASK_CONFIG="development"
```

Then initialize demo data:

```bash
python scripts/init_local_sqlite.py
```

This creates `instance/andon_local.sqlite3` with demo machines and logins:

```text
admin.demo / AdminDemo!2026
manager.demo / ManagerDemo!2026
operator.demo / OperatorDemo!2026
```

To remove the local database, stop the app and delete `instance/andon_local.sqlite3`. For deployment, set `FLASK_CONFIG=production`, set a PostgreSQL `DATABASE_URL`, and optionally set `LOCAL_SQLITE_FALLBACK=false`.

## Initialize Database

```bash
python scripts/init_andon_db.py
python scripts/seed_andon_data.py
python scripts/seed_auth_users.py
```

If your database was created before the board builder / management page was added, run this repair script once to create the missing board tables:

```bash
python scripts/init_management_board_tables.py
```

`scripts/seed_auth_users.py` creates or updates demo login accounts, applies passwords, ensures base machine groups exist, and adds multi-company memberships for admin/manager/operator examples.

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

For production deployments, use a WebSocket-capable worker and Redis-backed Socket.IO fanout.

Recommended production environment variables:

```bash
export DATABASE_URL=postgresql+psycopg://andon_user:password@host:5432/andon_db
export REDIS_URL=redis://host:6379/0
export REDIS_REQUIRED=true
export SOCKETIO_ENABLED=true
export SOCKETIO_MESSAGE_QUEUE=$REDIS_URL
export SOCKETIO_ASYNC_MODE=gevent
export SOCKETIO_ALLOW_MULTIWORKER=true
export SOCKETIO_FORCE_POLLING=false
export ANDON_RUNTIME_SCHEMA_REPAIR=false
export SESSION_COOKIE_SECURE=true
export SECRET_KEY=use-a-long-random-secret
```

Recommended production command:

```bash
gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 2 --bind 0.0.0.0:8000 wsgi:app
```

`REDIS_REQUIRED=true` is recommended in production. If `REDIS_REQUIRED` is left off, the app can fall back to in-memory cache behavior in development when Redis is unavailable. Redis is used for cache and the Socket.IO message queue only; it is not the source of truth for alerts, users, machines, reports, or machine status.

Set `ANDON_RUNTIME_SCHEMA_REPAIR=false` in production so schema/data repair logic does not run at app startup. Run schema changes through migrations instead.

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
- Production `DATABASE_URL` must point to PostgreSQL.
- Development falls back to SQLite at `instance/andon_local.sqlite3` when `DATABASE_URL` is not set and `LOCAL_SQLITE_FALLBACK` is enabled.
- Flask-Migrate is supported if installed, but the project also ships with clean initialization scripts.
- Machines now support `machine_type` in addition to `machine_code`, `name`, `area`, and `line`.
- Departments own the issue list in the UI; the internal category table remains as a compatibility layer.

## Future Hardware Integration

- Operator screen can be wired to touchscreen kiosks or stack-light buttons.
- Escalation notifications currently use a placeholder service and can later be connected to SMS, email, radios, or PLC/OEE systems.
