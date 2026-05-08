from sqlalchemy import func

from ..extensions import db


class MachineGroup(db.Model):
    __tablename__ = "machine_groups"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_machine_groups_company_name"),
        db.Index("ix_machine_groups_company_id", "company_id"),
        db.Index("ix_machine_groups_is_active", "is_active"),
        {"sqlite_autoincrement": True},
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="machine_groups", lazy="noload")
    users = db.relationship("User", back_populates="machine_group", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "name": self.name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
