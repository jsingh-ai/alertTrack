from sqlalchemy import func

from ..extensions import db


class IssueCategory(db.Model):
    __tablename__ = "issue_categories"
    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_issue_categories_company_name"),
        db.Index("ix_issue_categories_company_id", "company_id"),
        db.Index("ix_issue_categories_department_id", "department_id"),
        db.Index("ix_issue_categories_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    color = db.Column(db.String(24), nullable=True, default="#0d6efd")
    priority_default = db.Column(db.Integer, nullable=False, default=3)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="issue_categories", lazy="noload")
    department = db.relationship("Department", back_populates="issue_categories", lazy="selectin")
    problems = db.relationship("IssueProblem", back_populates="category", lazy="selectin")
    alerts = db.relationship("AndonAlert", back_populates="issue_category", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "name": self.name,
            "department_id": self.department_id,
            "department_name": self.department.name if self.department else None,
            "color": self.color,
            "priority_default": self.priority_default,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class IssueProblem(db.Model):
    __tablename__ = "issue_problems"
    __table_args__ = (
        db.UniqueConstraint("company_id", "category_id", "name", name="uq_issue_problems_company_category_name"),
        db.Index("ix_issue_problems_company_id", "company_id"),
        db.Index("ix_issue_problems_category_id", "category_id"),
        db.Index("ix_issue_problems_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("issue_categories.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    severity_default = db.Column(db.Integer, nullable=False, default=3)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="issue_problems", lazy="noload")
    category = db.relationship("IssueCategory", back_populates="problems", lazy="selectin")
    alerts = db.relationship("AndonAlert", back_populates="issue_problem", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else None,
            "department_id": self.category.department_id if self.category else None,
            "department_name": self.category.department.name if self.category and self.category.department else None,
            "name": self.name,
            "description": self.description,
            "severity_default": self.severity_default,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
