# Andon System MVP

Internal manufacturing Andon alert system built with Flask, SQLAlchemy, and SQLite by default.

## Setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install Flask Flask-SQLAlchemy Flask-Migrate python-dotenv
```

3. Set the database if needed:

```bash
export DATABASE_URL=sqlite:///instance/andon.db
```

For MySQL later, switch to:

```bash
export DATABASE_URL=mysql+pymysql://user:password@host/dbname
```

## Initialize Database

```bash
python scripts/init_andon_db.py
python scripts/seed_andon_data.py
```

The SQLite database lives at `instance/andon.db`.

If you are upgrading an existing SQLite file after schema changes, rerun the same two commands. The app also includes a small SQLite schema helper that adds `machines.machine_type` when needed.

## Run the App

```bash
flask --app andon_system:create_app run
```

## Pages

- `/andon` dashboard landing page
- `/andon/operator` machine button board with modal alert creation
- `/andon/board` live TV board
- `/andon/responder` responder queue
- `/andon/reports` reporting dashboard
- `/andon/admin` admin setup

## API

The system exposes JSON endpoints under `/api/andon` for machines, departments, department-linked issues, alerts, reports, and escalation checks.

## Database Notes

- Timestamps are stored in UTC.
- Use `DATABASE_URL` to move from SQLite to MySQL without code changes.
- Flask-Migrate is supported if installed, but the project also ships with clean initialization scripts.
- Machines now support `machine_type` in addition to `machine_code`, `name`, `area`, and `line`.
- Departments own the issue list in the UI; the internal category table remains as a compatibility layer.

## Future Hardware Integration

- Operator screen can be wired to touchscreen kiosks or stack-light buttons.
- Responder actions can be extended to scan badges or call handheld devices.
- Escalation notifications currently use a placeholder service and can later be connected to SMS, email, radios, or PLC/OEE systems.
