from sqlalchemy import func

from ..extensions import db


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.UniqueConstraint("company_id", "username", name="uq_users_company_username"),
        db.Index("ix_users_company_id", "company_id"),
        db.Index("ix_users_department_id", "department_id"),
        db.Index("ix_users_machine_group_id", "machine_group_id"),
        db.Index("ix_users_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    employee_id = db.Column(db.String(64), nullable=True, index=True)
    display_name = db.Column(db.String(160), nullable=False, index=True)
    username = db.Column(db.String(80), nullable=True, unique=True, index=True)
    role = db.Column(db.String(80), nullable=False, index=True)
    email = db.Column(db.String(160), nullable=True, index=True)
    phone_number = db.Column(db.String(32), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    machine_group_id = db.Column(db.Integer, db.ForeignKey("machine_groups.id"), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    company = db.relationship("Company", back_populates="users", lazy="noload")
    department = db.relationship("Department", back_populates="users", lazy="selectin")
    machine_group = db.relationship("MachineGroup", back_populates="users", lazy="selectin")
    operator_alerts = db.relationship(
        "AndonAlert",
        foreign_keys="AndonAlert.operator_user_id",
        lazy="selectin",
        overlaps="operator_user",
    )
    responder_alerts = db.relationship(
        "AndonAlert",
        foreign_keys="AndonAlert.responder_user_id",
        lazy="selectin",
        overlaps="responder_user",
    )
    events = db.relationship("AndonAlertEvent", back_populates="user", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "employee_id": self.employee_id,
            "work_id": self.employee_id,
            "display_name": self.display_name,
            "username": self.username,
            "role": self.role,
            "email": self.email,
            "phone_number": self.phone_number,
            "department_id": self.department_id,
            "department_name": self.department.name if self.department else None,
            "machine_group_id": self.machine_group_id,
            "machine_group_name": self.machine_group.name if self.machine_group else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
