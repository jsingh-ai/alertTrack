from __future__ import annotations

from sqlalchemy import func

from ..extensions import db


class PagerDevice(db.Model):
    __tablename__ = "pager_devices"
    __table_args__ = (
        db.Index("ix_pager_devices_company_id", "company_id"),
        db.Index("ix_pager_devices_department_id", "department_id"),
        db.Index("ix_pager_devices_active", "active"),
        db.Index("ix_pager_devices_token_fingerprint", "token_fingerprint"),
        db.UniqueConstraint("company_id", "department_id", "name", name="uq_pager_devices_company_department_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    token_hash = db.Column(db.String(255), nullable=False)
    token_fingerprint = db.Column(db.String(64), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    company = db.relationship("Company", back_populates="pager_devices", lazy="joined")
    department = db.relationship("Department", back_populates="pager_devices", lazy="joined")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "department_id": self.department_id,
            "name": self.name,
            "active": self.active,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
