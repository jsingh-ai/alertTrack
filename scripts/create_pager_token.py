from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.extensions import db
from andon_system.models.department import Department
from andon_system.models.pager_device import PagerDevice
from andon_system.security import fingerprint_pager_token, hash_pager_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or rotate a pager device token.")
    parser.add_argument("--department-id", type=int, required=True, help="Department ID")
    parser.add_argument("--name", type=str, required=True, help="Pager device name")
    parser.add_argument("--config", type=str, default="development", help="Flask config name (default: development)")
    args = parser.parse_args()

    app = create_app(args.config)
    with app.app_context():
        department = Department.query.filter_by(id=args.department_id, is_active=True).one_or_none()
        if department is None:
            raise SystemExit(f"Department not found or inactive: {args.department_id}")

        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_pager_token(raw_token)
        token_fingerprint = fingerprint_pager_token(raw_token)
        name = args.name.strip()
        if not name:
            raise SystemExit("Device name cannot be empty")

        device = PagerDevice.query.filter_by(
            company_id=department.company_id,
            department_id=department.id,
            name=name,
        ).one_or_none()
        if device is None:
            device = PagerDevice(
                company_id=department.company_id,
                department_id=department.id,
                name=name,
                token_hash=token_hash,
                token_fingerprint=token_fingerprint,
                active=True,
            )
            db.session.add(device)
        else:
            device.token_hash = token_hash
            device.token_fingerprint = token_fingerprint
            device.active = True
        db.session.commit()

        print("Pager device token created/rotated. Save this now; it will not be shown again.")
        print(f"device_id={device.id}")
        print(f"company_id={device.company_id}")
        print(f"department_id={device.department_id}")
        print(f"name={device.name}")
        print(f"token={raw_token}")


if __name__ == "__main__":
    main()
