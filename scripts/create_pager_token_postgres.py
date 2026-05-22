from __future__ import annotations

import argparse
import secrets

from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash
from hashlib import sha256


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or rotate a pager token in PostgreSQL.")
    parser.add_argument("--database-url", required=True, help="PostgreSQL SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host/db")
    parser.add_argument("--department-id", required=True, type=int, help="Department ID")
    parser.add_argument("--name", required=True, help="Pager device name")
    args = parser.parse_args()

    device_name = args.name.strip()
    if not device_name:
        raise SystemExit("Device name cannot be empty")

    raw_token = secrets.token_urlsafe(32)
    token_hash = generate_password_hash(raw_token)
    token_fingerprint = sha256(raw_token.encode("utf-8")).hexdigest()

    engine = create_engine(args.database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pager_devices ADD COLUMN IF NOT EXISTS token_fingerprint VARCHAR(64)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pager_devices_token_fingerprint "
                "ON pager_devices (token_fingerprint)"
            )
        )
        department_row = conn.execute(
            text(
                """
                SELECT id, company_id, name, is_active
                FROM departments
                WHERE id = :department_id
                """
            ),
            {"department_id": args.department_id},
        ).mappings().first()
        if department_row is None:
            raise SystemExit(f"Department not found: id={args.department_id}")
        if not department_row["is_active"]:
            raise SystemExit(f"Department is inactive: id={args.department_id}")

        conn.execute(
            text(
                """
                INSERT INTO pager_devices (company_id, department_id, name, token_hash, token_fingerprint, active)
                VALUES (:company_id, :department_id, :name, :token_hash, :token_fingerprint, TRUE)
                ON CONFLICT (company_id, department_id, name)
                DO UPDATE SET
                    token_hash = EXCLUDED.token_hash,
                    token_fingerprint = EXCLUDED.token_fingerprint,
                    active = TRUE,
                    updated_at = NOW()
                """
            ),
            {
                "company_id": department_row["company_id"],
                "department_id": department_row["id"],
                "name": device_name,
                "token_hash": token_hash,
                "token_fingerprint": token_fingerprint,
            },
        )

        device_row = conn.execute(
            text(
                """
                SELECT id, company_id, department_id, name
                FROM pager_devices
                WHERE company_id = :company_id
                  AND department_id = :department_id
                  AND name = :name
                """
            ),
            {
                "company_id": department_row["company_id"],
                "department_id": department_row["id"],
                "name": device_name,
            },
        ).mappings().first()

    print("Pager token created/rotated. Save this now; it will not be shown again.")
    print(f"device_id={device_row['id']}")
    print(f"company_id={device_row['company_id']}")
    print(f"department_id={device_row['department_id']}")
    print(f"name={device_row['name']}")
    print(f"token={raw_token}")


if __name__ == "__main__":
    main()
