from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from flask import current_app

from ..company_context import get_current_company_id
from ..extensions import db
from ..models.alert import ALERT_STATUSES_ACTIVE, EVENT_ESCALATED, AndonAlert, AndonAlertEvent
from ..models.escalation import EscalationRule
from ..models.machine_group import MachineGroup
from ..models.user import User, UserCompanyAccess
from .email_service import send_email

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
    recipients = _resolve_escalation_recipients(alert=alert, rule=rule)
    if not recipients:
        logger.info(
            "No escalation email recipients matched for alert %s at level %s",
            alert.alert_number,
            rule.level,
        )
        return

    phase_name = FIXED_ESCALATION_PHASES.get(rule.level, {}).get("name", f"Level {rule.level}")
    machine_name = alert.machine.name if alert.machine else f"Machine #{alert.machine_id}"
    machine_group_name = alert.machine.machine_type if alert.machine and alert.machine.machine_type else "Unknown"
    department_name = alert.department.name if alert.department else "Unknown"
    issue_name = alert.issue_problem.name if alert.issue_problem else "Unknown"
    created_at_text = alert.created_at.isoformat() if alert.created_at else "Unknown"

    subject = f"[{phase_name}] Andon Alert {alert.alert_number} - {machine_name}"
    body_lines = [
        f"Escalation Level: {phase_name}",
        f"Alert Number: {alert.alert_number}",
        f"Machine: {machine_name}",
        f"Machine Group: {machine_group_name}",
        f"Department: {department_name}",
        f"Issue: {issue_name}",
        f"Status: {alert.status}",
        f"Created At (UTC): {created_at_text}",
        "",
        "This message was sent by the escalation service.",
    ]
    body = "\n".join(body_lines)
    send_email(subject=subject, recipients=recipients, body_text=body)


def _resolve_escalation_recipients(alert: AndonAlert, rule: EscalationRule) -> list[str]:
    machine_group_id, department_id = _alert_scope_ids(alert)
    recipients: list[str] = []

    if rule.level == 1:
        recipients.extend(_emails_for_role(role="Viewer", alert=alert, machine_group_id=machine_group_id, department_id=department_id))
    elif rule.level == 2:
        recipients.extend(_emails_for_role(role="Manager", alert=alert, machine_group_id=machine_group_id, department_id=None))

    recipients.extend(_manual_escalation_recipients())
    return sorted({email for email in recipients if email})


def _alert_scope_ids(alert: AndonAlert) -> tuple[int | None, int | None]:
    department_id = alert.department_id or (alert.machine.department_id if alert.machine else None)
    machine_group_name = alert.machine.machine_type if alert.machine else None
    if not machine_group_name:
        return None, department_id
    machine_group = MachineGroup.query.filter_by(
        company_id=alert.company_id,
        name=machine_group_name,
        is_active=True,
    ).one_or_none()
    return (machine_group.id if machine_group else None), department_id


def _emails_for_role(role: str, alert: AndonAlert, machine_group_id: int | None, department_id: int | None) -> list[str]:
    access_rows = (
        UserCompanyAccess.query.join(User, User.id == UserCompanyAccess.user_id)
        .filter(
            UserCompanyAccess.company_id == alert.company_id,
            UserCompanyAccess.role == role,
            UserCompanyAccess.is_active.is_(True),
            User.is_active.is_(True),
            User.email.isnot(None),
            User.email != "",
        )
        .all()
    )

    emails = []
    for access in access_rows:
        if _access_matches_scope(
            access=access,
            alert_machine_id=alert.machine_id,
            machine_group_id=machine_group_id,
            department_id=department_id,
            role=role,
        ):
            if access.user and access.user.email:
                emails.append(access.user.email.strip())
    return emails


def _access_matches_scope(
    access: UserCompanyAccess,
    alert_machine_id: int,
    machine_group_id: int | None,
    department_id: int | None,
    role: str,
) -> bool:
    if not access.is_restricted:
        return True

    scope_config = _scope_config(access)
    scoped_machine_ids = _scoped_ids(scope_config.get("machine_ids"), fallback_id=None)
    scoped_group_ids = _scoped_ids(scope_config.get("machine_group_ids"), fallback_id=access.machine_group_id)
    scoped_department_ids = _scoped_ids(scope_config.get("department_ids"), fallback_id=access.department_id)

    if role == "Viewer":
        if machine_group_id is None or department_id is None:
            return False
        if scoped_group_ids and machine_group_id not in scoped_group_ids:
            return False
        if scoped_department_ids and department_id not in scoped_department_ids:
            return False
        # Legacy fallback if older viewer records only had machine IDs.
        if not scoped_group_ids and not scoped_department_ids and scoped_machine_ids:
            return alert_machine_id in scoped_machine_ids
        return bool(scoped_group_ids and scoped_department_ids)

    if role == "Manager":
        if machine_group_id is None:
            return False
        if scoped_group_ids:
            return machine_group_id in scoped_group_ids
        # If manager was configured only by machine IDs, keep it compatible.
        return alert_machine_id in scoped_machine_ids if scoped_machine_ids else False

    return False


def _scope_config(access: UserCompanyAccess) -> dict:
    try:
        return json.loads(access.scope_config_json or "{}")
    except json.JSONDecodeError:
        return {}


def _scoped_ids(value, fallback_id: int | None) -> set[int]:
    scoped = {int(item) for item in (value or []) if str(item).isdigit()}
    if fallback_id is not None:
        scoped.add(int(fallback_id))
    return scoped


def _manual_escalation_recipients() -> list[str]:
    recipients: list[str] = []
    for idx in range(1, 6):
        email_value = (current_app.config.get(f"ESCALATION_MANUAL_USER_{idx}_EMAIL") or "").strip()
        if email_value:
            recipients.append(email_value)
    return recipients


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
