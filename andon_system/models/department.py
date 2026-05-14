from sqlalchemy import func

from ..extensions import db


class Department(db.Model):
    __tablename__ = "departments"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_departments_company_name"),
        db.Index("ix_departments_company_id", "company_id"),
        db.Index("ix_departments_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="departments", lazy="noload")
    machines = db.relationship("Machine", back_populates="department", lazy="selectin")
    users = db.relationship("User", back_populates="department", lazy="selectin")
    issue_categories = db.relationship("IssueCategory", back_populates="department", lazy="selectin")
    alerts = db.relationship("AndonAlert", back_populates="department", lazy="selectin")
    escalation_rules = db.relationship("EscalationRule", back_populates="department", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
