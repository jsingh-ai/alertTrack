from ..extensions import db


andon_alert_escalation_map = db.Table(
    "andon_alert_escalation_map",
    db.Column("alert_id", db.Integer, db.ForeignKey("andon_alerts.id"), primary_key=True),
    db.Column("rule_id", db.Integer, db.ForeignKey("escalation_rules.id"), primary_key=True),
)
