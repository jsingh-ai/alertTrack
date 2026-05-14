from __future__ import annotations

from sqlalchemy import func, text

from ..extensions import db


ALERT_STATUS_OPEN = "OPEN"
ALERT_STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
ALERT_STATUS_ARRIVED = "ARRIVED"
ALERT_STATUS_RESOLVED = "RESOLVED"
ALERT_STATUS_CANCELLED = "CANCELLED"

ALERT_STATUSES_ACTIVE = [
    ALERT_STATUS_OPEN,
    ALERT_STATUS_ACKNOWLEDGED,
    ALERT_STATUS_ARRIVED,
]

EVENT_CREATED = "CREATED"
EVENT_ACKNOWLEDGED = "ACKNOWLEDGED"
EVENT_ARRIVED = "ARRIVED"
EVENT_RESOLVED = "RESOLVED"
EVENT_CANCELLED = "CANCELLED"
EVENT_ESCALATED = "ESCALATED"
EVENT_NOTE_ADDED = "NOTE_ADDED"


class AndonAlert(db.Model):
    __tablename__ = "andon_alerts"
    __table_args__ = (
        db.Index("ix_andon_alerts_company_id", "company_id"),
        db.Index("ix_andon_alerts_status", "status"),
        db.Index("ix_andon_alerts_machine_id", "machine_id"),
        db.Index("ix_andon_alerts_department_id", "department_id"),
        db.Index("ix_andon_alerts_created_at", "created_at"),
        db.Index("ix_andon_alerts_acknowledged_at", "acknowledged_at"),
        db.Index("ix_andon_alerts_arrived_at", "arrived_at"),
        db.Index("ix_andon_alerts_resolved_at", "resolved_at"),
        db.Index("ix_andon_alerts_cancelled_at", "cancelled_at"),
        db.Index(
            "uq_andon_alerts_active_machine",
            "machine_id",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'ACKNOWLEDGED', 'ARRIVED')"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    alert_number = db.Column(db.String(40), nullable=False, unique=True, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    issue_category_id = db.Column(db.Integer, db.ForeignKey("issue_categories.id"), nullable=False)
    issue_problem_id = db.Column(db.Integer, db.ForeignKey("issue_problems.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=ALERT_STATUS_OPEN)
    priority = db.Column(db.Integer, nullable=False, default=3, index=True)
    operator_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    operator_name_text = db.Column(db.String(160), nullable=True)
    responder_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    responder_name_text = db.Column(db.String(160), nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    acknowledged_at = db.Column(db.DateTime(timezone=True), nullable=True)
    acknowledged_seconds = db.Column(db.Integer, nullable=True)
    arrived_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ack_to_clear_seconds = db.Column(db.Integer, nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    current_escalation_level = db.Column(db.Integer, nullable=False, default=0)
    last_escalated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolution_note = db.Column(db.Text, nullable=True)
    root_cause = db.Column(db.Text, nullable=True)
    corrective_action = db.Column(db.Text, nullable=True)

    company = db.relationship("Company", back_populates="alerts", lazy="noload")
    machine = db.relationship("Machine", back_populates="alerts", lazy="selectin")
    department = db.relationship("Department", back_populates="alerts", lazy="selectin")
    issue_category = db.relationship("IssueCategory", back_populates="alerts", lazy="selectin")
    issue_problem = db.relationship("IssueProblem", back_populates="alerts", lazy="selectin")
    operator_user = db.relationship("User", foreign_keys=[operator_user_id], lazy="selectin")
    responder_user = db.relationship("User", foreign_keys=[responder_user_id], lazy="selectin")
    events = db.relationship("AndonAlertEvent", back_populates="alert", cascade="all, delete-orphan", lazy="selectin")
    escalations = db.relationship("EscalationRule", secondary="andon_alert_escalation_map", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "alert_number": self.alert_number,
            "machine": self.machine.to_dict() if self.machine else None,
            "department": self.department.to_dict() if self.department else None,
            "issue_category": self.issue_category.to_dict() if self.issue_category else None,
            "issue_problem": self.issue_problem.to_dict() if self.issue_problem else None,
            "status": self.status,
            "priority": self.priority,
            "operator_user_id": self.operator_user_id,
            "operator_name_text": self.operator_name_text,
            "responder_user_id": self.responder_user_id,
            "responder_name_text": self.responder_name_text,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_seconds": self.acknowledged_seconds,
            "arrived_at": self.arrived_at.isoformat() if self.arrived_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "ack_to_clear_seconds": self.ack_to_clear_seconds,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "current_escalation_level": self.current_escalation_level,
            "last_escalated_at": self.last_escalated_at.isoformat() if self.last_escalated_at else None,
            "resolution_note": self.resolution_note,
            "root_cause": self.root_cause,
            "corrective_action": self.corrective_action,
            "wait_to_ack_seconds": self.wait_to_ack_seconds,
            "wait_to_arrive_seconds": self.wait_to_arrive_seconds,
            "repair_seconds": self.repair_seconds,
            "total_resolution_seconds": self.total_resolution_seconds,
        }

    @property
    def wait_to_ack_seconds(self):
        return _duration_seconds(self.created_at, self.acknowledged_at)

    @property
    def wait_to_arrive_seconds(self):
        return _duration_seconds(self.created_at, self.arrived_at)

    @property
    def repair_seconds(self):
        return _duration_seconds(self.arrived_at, self.resolved_at)

    @property
    def total_resolution_seconds(self):
        return _duration_seconds(self.created_at, self.resolved_at)


class AndonAlertEvent(db.Model):
    __tablename__ = "andon_alert_events"
    __table_args__ = (
        db.Index("ix_andon_alert_events_company_id", "company_id"),
        db.Index("ix_andon_alert_events_alert_id", "alert_id"),
        db.Index("ix_andon_alert_events_event_type", "event_type"),
        db.Index("ix_andon_alert_events_event_at", "event_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    alert_id = db.Column(db.Integer, db.ForeignKey("andon_alerts.id"), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    event_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    user_name_text = db.Column(db.String(160), nullable=True)
    message = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)

    company = db.relationship("Company", lazy="noload")
    alert = db.relationship("AndonAlert", back_populates="events", lazy="selectin")
    user = db.relationship("User", back_populates="events", lazy="selectin")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "alert_id": self.alert_id,
            "event_type": self.event_type,
            "event_at": self.event_at.isoformat() if self.event_at else None,
            "user_id": self.user_id,
            "user_name_text": self.user_name_text,
            "message": self.message,
            "metadata_json": self.metadata_json,
        }


def _duration_seconds(start, end):
    if not start or not end:
        return None
    delta = end - start
    return int(delta.total_seconds())
