from sqlalchemy import func

from ..extensions import db


class EscalationRule(db.Model):
    __tablename__ = "escalation_rules"
    __table_args__ = (
        db.UniqueConstraint("company_id", "level", name="uq_escalation_rules_company_level"),
        db.Index("ix_escalation_rules_company_id", "company_id"),
        db.Index("ix_escalation_rules_department_id", "department_id"),
        db.Index("ix_escalation_rules_issue_category_id", "issue_category_id"),
        db.Index("ix_escalation_rules_issue_problem_id", "issue_problem_id"),
        db.Index("ix_escalation_rules_machine_id", "machine_id"),
        db.Index("ix_escalation_rules_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    issue_category_id = db.Column(db.Integer, db.ForeignKey("issue_categories.id"), nullable=True)
    issue_problem_id = db.Column(db.Integer, db.ForeignKey("issue_problems.id"), nullable=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=True)
    level = db.Column(db.Integer, nullable=False, default=1)
    delay_seconds = db.Column(db.Integer, nullable=False, default=300)
    notify_role = db.Column(db.String(80), nullable=True)
    notify_target = db.Column(db.String(160), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="escalation_rules", lazy="noload")
    department = db.relationship("Department", back_populates="escalation_rules", lazy="selectin")
    issue_category = db.relationship("IssueCategory", lazy="selectin")
    issue_problem = db.relationship("IssueProblem", lazy="selectin")
    machine = db.relationship("Machine", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "department_id": self.department_id,
            "issue_category_id": self.issue_category_id,
            "issue_problem_id": self.issue_problem_id,
            "machine_id": self.machine_id,
            "level": self.level,
            "delay_seconds": self.delay_seconds,
            "notify_role": self.notify_role,
            "notify_target": self.notify_target,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
