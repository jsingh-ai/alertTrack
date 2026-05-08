from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..company_context import get_current_company_id
from ..extensions import db
from ..models.alert import ALERT_STATUSES_ACTIVE, EVENT_ESCALATED, AndonAlert, AndonAlertEvent
from ..models.escalation import EscalationRule

logger = logging.getLogger(__name__)

FIXED_ESCALATION_PHASES = {
    1: {"name": "Warning", "delay_seconds": 300},
    2: {"name": "Critical", "delay_seconds": 900},
    3: {"name": "Emergency", "delay_seconds": 1800},
}


def utc_now():
    return datetime.now(timezone.utc)


def check_escalations():
    ensure_fixed_escalation_rules()
    company_id = get_current_company_id()
    alerts_query = AndonAlert.query.filter(AndonAlert.status.in_(ALERT_STATUSES_ACTIVE))
    if company_id:
        alerts_query = alerts_query.filter(AndonAlert.company_id == company_id)
    alerts = alerts_query.all()
    escalated = []

    for alert in alerts:
        applicable_rules = _matching_rules(alert)
        if not applicable_rules:
            continue
        applicable_rules.sort(key=lambda rule: rule.level)
        next_level = alert.current_escalation_level + 1
        now = utc_now()

        for rule in applicable_rules:
            if rule.level < next_level:
                continue
            age_seconds = int((now - alert.created_at).total_seconds()) if alert.created_at else 0
            if age_seconds < rule.delay_seconds:
                break
            alert.current_escalation_level = rule.level
            alert.last_escalated_at = now
            event = AndonAlertEvent(
                company_id=alert.company_id,
                alert=alert,
                event_type=EVENT_ESCALATED,
                message=f"Escalation level {rule.level} triggered",
                metadata_json={
                    "rule_id": rule.id,
                    "level": rule.level,
                    "delay_seconds": rule.delay_seconds,
                    "notify_role": rule.notify_role,
                    "notify_target": rule.notify_target,
                },
            )
            db.session.add(event)
            send_notification(alert, rule)
            escalated.append({"alert_id": alert.id, "rule_id": rule.id, "level": rule.level})
            next_level = rule.level + 1

    if escalated:
        db.session.commit()
    return escalated


def send_notification(alert, rule):
    logger.info(
        "Notification placeholder for alert %s using escalation rule %s to %s/%s",
        alert.alert_number,
        rule.id,
        rule.notify_role,
        rule.notify_target,
    )


def _matching_rules(alert):
    rules = EscalationRule.query.filter(
        EscalationRule.is_active.is_(True),
        EscalationRule.level.in_(FIXED_ESCALATION_PHASES.keys()),
        EscalationRule.company_id == alert.company_id,
    ).all()
    matched = []
    for rule in rules:
        matched.append(rule)
    return matched


def ensure_fixed_escalation_rules():
    company_id = get_current_company_id()
    if company_id is None:
        return {}
    existing_rules = (
        EscalationRule.query.filter(EscalationRule.company_id == company_id)
        .order_by(EscalationRule.level.asc(), EscalationRule.id.asc())
        .all()
    )
    canonical_by_level = {}

    for level in FIXED_ESCALATION_PHASES:
        level_rules = [rule for rule in existing_rules if rule.level == level]
        canonical = level_rules[0] if level_rules else None
        if canonical is None:
            canonical = EscalationRule(
                company_id=company_id,
                level=level,
                delay_seconds=FIXED_ESCALATION_PHASES[level]["delay_seconds"],
                notify_role=None,
                notify_target=None,
                is_active=True,
            )
            db.session.add(canonical)
            db.session.flush()
        else:
            canonical.department_id = None
            canonical.issue_category_id = None
            canonical.issue_problem_id = None
            canonical.machine_id = None
            canonical.notify_role = None
            canonical.notify_target = None
            if canonical.delay_seconds is None:
                canonical.delay_seconds = FIXED_ESCALATION_PHASES[level]["delay_seconds"]
            canonical.is_active = True
        canonical_by_level[level] = canonical

        for duplicate in level_rules[1:]:
            db.session.delete(duplicate)

    for rule in existing_rules:
        if rule.level not in FIXED_ESCALATION_PHASES:
            db.session.delete(rule)

    db.session.commit()
    return canonical_by_level
