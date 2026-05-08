from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import joinedload

from ..company_context import get_current_company_id
from ..models.alert import ALERT_STATUS_CANCELLED, ALERT_STATUS_RESOLVED, AndonAlert


def format_local_datetime(value, tz_name="America/Chicago"):
    if not value:
        return ""
    tz = ZoneInfo(tz_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def build_report_summary(filters: dict):
    alerts = _filtered_alerts(filters)
    return {
        "kpis": _build_kpis(alerts),
        "by_department": _group_count(alerts, "department"),
        "by_machine": _group_count(alerts, "machine"),
        "by_issue_category": _group_count(alerts, "issue_category"),
        "by_problem": _group_count(alerts, "issue_problem"),
        "fastest_responders": _fastest_responders(alerts),
        "slowest_machines": _slowest_machines(alerts),
        "calls_per_hour": _calls_per_hour(alerts),
        "alerts_by_day": _alerts_by_day(alerts),
        "pareto_machines": _pareto(alerts, "machine"),
        "pareto_problems": _pareto(alerts, "issue_problem"),
    }


def build_by_machine(filters: dict):
    alerts = _filtered_alerts(filters)
    return _group_count(alerts, "machine")


def build_by_department(filters: dict):
    alerts = _filtered_alerts(filters)
    return _group_count(alerts, "department")


def build_by_problem(filters: dict):
    alerts = _filtered_alerts(filters)
    return _group_count(alerts, "issue_problem")


def build_calls_per_hour(filters: dict):
    alerts = _filtered_alerts(filters)
    return _calls_per_hour(alerts)


def build_responders(filters: dict):
    alerts = _filtered_alerts(filters)
    return _fastest_responders(alerts)


def _filtered_alerts(filters):
    company_id = get_current_company_id()
    query = AndonAlert.query.options(
        joinedload(AndonAlert.machine),
        joinedload(AndonAlert.department),
        joinedload(AndonAlert.issue_category),
        joinedload(AndonAlert.issue_problem),
    )
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)

    start = _parse_dt(filters.get("start"))
    end = _parse_dt(filters.get("end"))
    department_id = filters.get("department_id")
    machine_id = filters.get("machine_id")
    category_id = filters.get("issue_category_id")

    if start:
        query = query.filter(AndonAlert.created_at >= start)
    if end:
        query = query.filter(AndonAlert.created_at < end)
    if department_id:
        query = query.filter(AndonAlert.department_id == department_id)
    if machine_id:
        query = query.filter(AndonAlert.machine_id == machine_id)
    if category_id:
        query = query.filter(AndonAlert.issue_category_id == category_id)

    return query.order_by(AndonAlert.created_at.asc()).all()


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_kpis(alerts):
    open_count = sum(1 for alert in alerts if alert.status not in [ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED])
    resolved_count = sum(1 for alert in alerts if alert.status == ALERT_STATUS_RESOLVED)
    escalated_count = sum(1 for alert in alerts if alert.current_escalation_level > 0)
    return {
        "total_alerts": len(alerts),
        "open_alerts": open_count,
        "resolved_alerts": resolved_count,
        "average_acknowledge_time": _avg([alert.wait_to_ack_seconds for alert in alerts]),
        "average_arrival_time": _avg([alert.wait_to_arrive_seconds for alert in alerts]),
        "average_resolution_time": _avg([alert.total_resolution_seconds for alert in alerts]),
        "escalated_alerts": escalated_count,
    }


def _group_count(alerts, relation_name):
    counter = Counter()
    for alert in alerts:
        relation = getattr(alert, relation_name)
        if relation is None:
            key = {"id": None, "name": "Unassigned"}
        else:
            key = {"id": relation.id, "name": getattr(relation, "name", None) or getattr(relation, "display_name", None)}
        counter[(key["id"], key["name"])] += 1
    return [{"id": item[0][0], "name": item[0][1], "count": item[1]} for item in counter.most_common()]


def _calls_per_hour(alerts):
    counter = Counter()
    for alert in alerts:
        if not alert.created_at:
            continue
        hour = alert.created_at.astimezone(timezone.utc).hour if alert.created_at.tzinfo else alert.created_at.hour
        counter[hour] += 1
    return [{"hour": hour, "count": counter.get(hour, 0)} for hour in range(24)]


def _alerts_by_day(alerts):
    counter = Counter()
    for alert in alerts:
        if not alert.created_at:
            continue
        day = alert.created_at.date().isoformat()
        counter[day] += 1
    return [{"day": day, "count": count} for day, count in sorted(counter.items())]


def _fastest_responders(alerts):
    buckets = defaultdict(list)
    resolution_buckets = defaultdict(list)
    for alert in alerts:
        if not alert.responder_name_text or alert.wait_to_ack_seconds is None:
            continue
        buckets[alert.responder_name_text].append(alert.wait_to_ack_seconds)
        if alert.total_resolution_seconds is not None:
            resolution_buckets[alert.responder_name_text].append(alert.total_resolution_seconds)
    rows = []
    for name, values in buckets.items():
        rows.append(
            {
                "name": name,
                "average_acknowledge_seconds": round(sum(values) / len(values), 2),
                "average_resolution_seconds": round(sum(resolution_buckets[name]) / len(resolution_buckets[name]), 2) if resolution_buckets[name] else None,
                "count": len(values),
            }
        )
    return sorted(rows, key=lambda row: row["average_acknowledge_seconds"])[:10]


def _slowest_machines(alerts):
    buckets = defaultdict(list)
    names = {}
    for alert in alerts:
        if alert.wait_to_ack_seconds is None:
            continue
        buckets[alert.machine_id].append(alert.wait_to_ack_seconds)
        names[alert.machine_id] = alert.machine.name if alert.machine else f"Machine {alert.machine_id}"
    rows = []
    for machine_id, values in buckets.items():
        rows.append(
            {
                "machine_id": machine_id,
                "name": names.get(machine_id, f"Machine {machine_id}"),
                "average_acknowledge_seconds": round(sum(values) / len(values), 2),
                "count": len(values),
            }
        )
    return sorted(rows, key=lambda row: row["average_acknowledge_seconds"], reverse=True)[:10]


def _pareto(alerts, relation_name):
    counts = Counter()
    names = {}
    for alert in alerts:
        relation = getattr(alert, relation_name)
        if relation is None:
            continue
        counts[relation.id] += 1
        names[relation.id] = getattr(relation, "name", None) or getattr(relation, "display_name", None)
    total = sum(counts.values()) or 1
    cumulative = 0
    rows = []
    for item_id, count in counts.most_common():
        cumulative += count
        rows.append(
            {
                "id": item_id,
                "name": names.get(item_id, str(item_id)),
                "count": count,
                "share": round((count / total) * 100, 2),
                "cumulative_share": round((cumulative / total) * 100, 2),
            }
        )
    return rows


def _avg(values):
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 2)
