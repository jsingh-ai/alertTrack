from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db

USER_ROLES = ("Admin", "Manager", "Operator")
USER_SCOPE_MODES = ("restricted", "all")


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.UniqueConstraint("company_id", "username", name="uq_users_company_username"),
        db.CheckConstraint("role IN ('Admin', 'Manager', 'Operator')", name="ck_users_role_allowed"),
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
    password_hash = db.Column(db.String(255), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    machine_group_id = db.Column(db.Integer, db.ForeignKey("machine_groups.id"), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
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
    company_access = db.relationship(
        "UserCompanyAccess",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    view_preferences = db.relationship(
        "UserViewPreference",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str | None) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, str(password or ""))

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
            "has_password": bool(self.password_hash),
            "department_id": self.department_id,
            "department_name": self.department.name if self.department else None,
            "machine_group_id": self.machine_group_id,
            "machine_group_name": self.machine_group.name if self.machine_group else None,
            "is_active": self.is_active,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserCompanyAccess(db.Model):
    __tablename__ = "user_company_access"
    __table_args__ = (
        db.UniqueConstraint("user_id", "company_id", name="uq_user_company_access_user_company"),
        db.CheckConstraint("role IN ('Admin', 'Manager', 'Operator')", name="ck_user_company_access_role"),
        db.CheckConstraint("scope_mode IN ('all', 'restricted')", name="ck_user_company_access_scope_mode"),
        db.Index("ix_user_company_access_user_id", "user_id"),
        db.Index("ix_user_company_access_company_id", "company_id"),
        db.Index("ix_user_company_access_is_active", "is_active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    role = db.Column(db.String(80), nullable=False, default="Operator")
    scope_mode = db.Column(db.String(32), nullable=False, default="restricted")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    machine_group_id = db.Column(db.Integer, db.ForeignKey("machine_groups.id"), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = db.relationship("User", back_populates="company_access", lazy="joined")
    company = db.relationship("Company", lazy="joined")
    department = db.relationship("Department", lazy="joined")
    machine_group = db.relationship("MachineGroup", lazy="joined")

    @property
    def is_admin(self) -> bool:
        return self.role == "Admin"

    @property
    def is_restricted(self) -> bool:
        return self.scope_mode == "restricted" and not self.is_admin

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "company_name": self.company.name if self.company else None,
            "role": self.role,
            "scope_mode": self.scope_mode,
            "department_id": self.department_id,
            "department_name": self.department.name if self.department else None,
            "machine_group_id": self.machine_group_id,
            "machine_group_name": self.machine_group.name if self.machine_group else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserViewPreference(db.Model):
    __tablename__ = "user_view_preferences"
    __table_args__ = (
        db.UniqueConstraint("user_id", "company_id", "page_key", name="uq_user_view_preferences_scope"),
        db.Index("ix_user_view_preferences_user_id", "user_id"),
        db.Index("ix_user_view_preferences_company_id", "company_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=True)
    page_key = db.Column(db.String(80), nullable=False)
    preferences_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = db.relationship("User", back_populates="view_preferences", lazy="joined")
    company = db.relationship("Company", lazy="joined")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "page_key": self.page_key,
            "preferences_json": self.preferences_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
