from sqlalchemy import func

from ..extensions import db


class Company(db.Model):
    __tablename__ = "companies"
    __table_args__ = (db.Index("ix_companies_is_active", "is_active"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    slug = db.Column(db.String(80), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    departments = db.relationship("Department", back_populates="company", lazy="selectin")
    machine_groups = db.relationship("MachineGroup", back_populates="company", lazy="selectin")
    machines = db.relationship("Machine", back_populates="company", lazy="selectin")
    users = db.relationship("User", back_populates="company", lazy="selectin")
    issue_categories = db.relationship("IssueCategory", back_populates="company", lazy="selectin")
    issue_problems = db.relationship("IssueProblem", back_populates="company", lazy="selectin")
    alerts = db.relationship("AndonAlert", back_populates="company", lazy="selectin")
    escalation_rules = db.relationship("EscalationRule", back_populates="company", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
