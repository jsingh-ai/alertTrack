from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.db_maintenance import ensure_andon_schema
from andon_system.extensions import db


def main():
    app = create_app()
    instance_dir = ROOT / "instance"
    instance_dir.mkdir(exist_ok=True)
    with app.app_context():
        db.create_all()
        ensure_andon_schema()
    print("Initialized Andon database.")


if __name__ == "__main__":
    main()
