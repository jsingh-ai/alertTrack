from sqlalchemy import func

from ..extensions import db


class Machine(db.Model):
    __tablename__ = "machines"
    __table_args__ = (
        db.UniqueConstraint("company_id", "machine_code", name="uq_machines_company_code"),
        db.Index("ix_machines_company_id", "company_id"),
        db.Index("ix_machines_department_id", "department_id"),
        db.Index("ix_machines_machine_type", "machine_type"),
        db.Index("ix_machines_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    machine_code = db.Column(db.String(64), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    machine_type = db.Column(db.String(80), nullable=True)
    area = db.Column(db.String(120), nullable=True)
    line = db.Column(db.String(120), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="machines", lazy="noload")
    department = db.relationship("Department", back_populates="machines", lazy="selectin")
    alerts = db.relationship("AndonAlert", back_populates="machine", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "machine_code": self.machine_code,
            "name": self.name,
            "machine_type": self.machine_type,
            "area": self.area,
            "line": self.line,
            "department_id": self.department_id,
            "department_name": self.department.name if self.department else None,
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
