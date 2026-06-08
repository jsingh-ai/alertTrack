import json

from sqlalchemy import func
from flask import current_app, has_app_context
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db

USER_ROLES = ("Admin", "Manager", "Operator", "Viewer")
USER_SCOPE_MODES = ("restricted", "all")


def _password_hash_config():
    if has_app_context():
        method = str(current_app.config.get("USER_PASSWORD_HASH_METHOD") or "pbkdf2:sha256:300000").strip()
        salt_length = int(current_app.config.get("USER_PASSWORD_SALT_LENGTH", 16) or 16)
        return method, max(8, salt_length)
    return "pbkdf2:sha256:300000", 16


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        db.UniqueConstraint("company_id", "username", name="uq_users_company_username"),
        db.CheckConstraint("role IN ('Admin', 'Manager', 'Operator', 'Viewer')", name="ck_users_role_allowed"),
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
        method, salt_length = _password_hash_config()
        self.password_hash = generate_password_hash(password, method=method, salt_length=salt_length)

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
        db.CheckConstraint("role IN ('Admin', 'Manager', 'Operator', 'Viewer')", name="ck_user_company_access_role"),
        db.CheckConstraint("scope_mode IN ('all', 'restricted')", name="ck_user_company_access_scope_mode"),
        db.Index("ix_user_company_access_user_id", "user_id"),
        db.Index("ix_user_company_access_user_active_company", "user_id", "is_active", "company_id"),
        db.Index("ix_user_company_access_company_id", "company_id"),
        db.Index("ix_user_company_access_is_active", "is_active"),
        db.Index("ix_user_company_access_company_active_role_id", "company_id", "is_active", "role", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    role = db.Column(db.String(80), nullable=False, default="Operator")
    scope_mode = db.Column(db.String(32), nullable=False, default="restricted")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    machine_group_id = db.Column(db.Integer, db.ForeignKey("machine_groups.id"), nullable=True)
    scope_config_json = db.Column(db.Text, nullable=False, default="{}")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = db.relationship("User", back_populates="company_access", lazy="joined")
    company = db.relationship("Company", back_populates="user_access", lazy="joined")
    department = db.relationship("Department", lazy="joined")
    machine_group = db.relationship("MachineGroup", lazy="joined")

    @property
    def is_admin(self) -> bool:
        return self.role == "Admin"

    @property
    def is_restricted(self) -> bool:
        return self.scope_mode == "restricted" and not self.is_admin

    def to_dict(self):
        try:
            scope_config = json.loads(self.scope_config_json or "{}")
        except json.JSONDecodeError:
            scope_config = {}
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
            "scope_config": scope_config,
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
    company = db.relationship("Company", back_populates="user_view_preferences", lazy="joined")

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


class UserBoard(db.Model):
    __tablename__ = "user_boards"
    __table_args__ = (
        db.Index("ix_user_boards_user_id", "user_id"),
        db.Index("ix_user_boards_company_id", "company_id"),
        db.Index("ix_user_boards_last_opened_at", "last_opened_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    show_performance = db.Column(db.Boolean, nullable=False, default=True)
    show_recent_history = db.Column(db.Boolean, nullable=False, default=True)
    show_radius = db.Column(db.Boolean, nullable=False, default=True)
    last_opened_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    user = db.relationship("User", lazy="joined")
    company = db.relationship("Company", back_populates="user_boards", lazy="joined")
    items = db.relationship(
        "UserBoardItem",
        back_populates="board",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="UserBoardItem.position.asc()",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "company_id": self.company_id,
            "name": self.name,
            "show_performance": self.show_performance,
            "show_recent_history": self.show_recent_history,
            "show_radius": self.show_radius,
            "last_opened_at": self.last_opened_at.isoformat() if self.last_opened_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "items": [item.to_dict() for item in self.items],
        }


class UserBoardItem(db.Model):
    __tablename__ = "user_board_items"
    __table_args__ = (
        db.UniqueConstraint("board_id", "machine_id", name="uq_user_board_items_board_machine"),
        db.Index("ix_user_board_items_board_id", "board_id"),
        db.Index("ix_user_board_items_machine_id", "machine_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.Integer, db.ForeignKey("user_boards.id"), nullable=False)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

    board = db.relationship("UserBoard", back_populates="items", lazy="joined")
    machine = db.relationship("Machine", lazy="joined")

    def to_dict(self):
        return {
            "id": self.id,
            "board_id": self.board_id,
            "machine_id": self.machine_id,
            "position": self.position,
            "machine_name": self.machine.name if self.machine else None,
            "machine_group": self.machine.machine_type if self.machine else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
