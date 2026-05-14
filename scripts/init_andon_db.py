from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.extensions import db
from andon_system.models import AndonAlert


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        for index in AndonAlert.__table__.indexes:
            index.create(bind=db.engine, checkfirst=True)
    print("Initialized Andon database.")


if __name__ == "__main__":
    main()
