from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from andon_system import create_app
from andon_system.extensions import db
from andon_system.models.user import UserBoard, UserBoardItem, UserCompanyAccess, UserViewPreference


def main():
    app = create_app()
    with app.app_context():
        # Safe to run repeatedly against an existing database.
        UserCompanyAccess.__table__.create(bind=db.engine, checkfirst=True)
        UserViewPreference.__table__.create(bind=db.engine, checkfirst=True)
        UserBoard.__table__.create(bind=db.engine, checkfirst=True)
        UserBoardItem.__table__.create(bind=db.engine, checkfirst=True)
    print("Ensured management board tables exist.")


if __name__ == "__main__":
    main()
