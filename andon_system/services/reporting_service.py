from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_
from sqlalchemy.orm import joinedload, load_only, noload

from ..company_context import get_current_company_id
from ..models.alert import ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_CANCELLED, ALERT_STATUS_OPEN, ALERT_STATUS_RESOLVED, AndonAlert
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..security import get_scope_filters
from .cache_service import get_cached, set_cached

REPORT_SUMMARY_CACHE_TTL_SECONDS = 15
REPORT_DETAILS_CACHE_TTL_SECONDS = 30
REPORT_MACHINE_STATS_CACHE_TTL_SECONDS = 10


def format_local_datetime(value, tz_name="America/Chicago"):
    if not value:
        return ""
    tz = ZoneInfo(tz_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def build_report_summary(filters: dict):
    company_id = get_current_company_id()
    cache_key = ("report_summary", company_id, _cache_filters(filters))
    cached = get_cached(cache_key)
    if cached is not None:
        return cached
    alerts = _filtered_alerts(filters)
    result = {
        "kpis": _build_kpis(alerts),
        "by_machine_group": _group_count_field(alerts, lambda alert: alert.machine.machine_type if alert.machine and alert.machine.machine_type else "Unassigned"),
        "by_department": _group_count(alerts, "department"),
        "by_machine": _group_count(alerts, "machine"),
        "top_machines": _top_machines(alerts),
        "top_problems": _top_problems(alerts),
        "calls_per_hour": _calls_per_hour(alerts),
    }
    set_cached(cache_key, result, REPORT_SUMMARY_CACHE_TTL_SECONDS)
    return result


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


def build_machine_details(filters: dict):
    company_id = get_current_company_id()
    cache_key = ("report_machine_details", company_id, _cache_filters(filters))
    cached = get_cached(cache_key)
    if cached is not None:
        return cached
    alerts = _filtered_alerts(filters)
    result = [_machine_detail(alert) for alert in alerts]
    set_cached(cache_key, result, REPORT_DETAILS_CACHE_TTL_SECONDS)
    return result


def build_problem_details(filters: dict):
    company_id = get_current_company_id()
    cache_key = ("report_problem_details", company_id, _cache_filters(filters))
    cached = get_cached(cache_key)
    if cached is not None:
        return cached
    alerts = _filtered_alerts(filters)
    result = [_machine_detail(alert) for alert in alerts]
    set_cached(cache_key, result, REPORT_DETAILS_CACHE_TTL_SECONDS)
    return result


def build_machine_stats(filters: dict):
    company_id = get_current_company_id()
    cache_key = ("report_machine_stats", company_id, _cache_filters(filters))
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    query = _base_alert_query(filters).options(
        load_only(
            AndonAlert.id,
            AndonAlert.machine_id,
            AndonAlert.department_id,
            AndonAlert.issue_category_id,
            AndonAlert.issue_problem_id,
            AndonAlert.status,
            AndonAlert.created_at,
            AndonAlert.acknowledged_seconds,
            AndonAlert.ack_to_clear_seconds,
            AndonAlert.resolved_at,
            AndonAlert.cancelled_at,
        ),
        noload("*"),
    )
    alerts = query.order_by(AndonAlert.created_at.asc()).all()
    if not alerts:
        return []

    dept_ids = {alert.department_id for alert in alerts if alert.department_id is not None}
    category_ids = {alert.issue_category_id for alert in alerts if alert.issue_category_id is not None}
    problem_ids = {alert.issue_problem_id for alert in alerts if alert.issue_problem_id is not None}

    department_by_id = {
        row.id: row.name
        for row in Department.query.options(load_only(Department.id, Department.name), noload("*"))
        .filter(Department.id.in_(dept_ids)).all()
    } if dept_ids else {}
    category_by_id = {
        row.id: row.name
        for row in IssueCategory.query.options(load_only(IssueCategory.id, IssueCategory.name), noload("*"))
        .filter(IssueCategory.id.in_(category_ids)).all()
    } if category_ids else {}
    problem_by_id = {
        row.id: row.name
        for row in IssueProblem.query.options(load_only(IssueProblem.id, IssueProblem.name), noload("*"))
        .filter(IssueProblem.id.in_(problem_ids)).all()
    } if problem_ids else {}

    grouped = {}
    for alert in alerts:
        machine_id = int(alert.machine_id)
        stats = grouped.get(machine_id)
        if stats is None:
            stats = {
                "machine_id": machine_id,
                "total_alerts": 0,
                "ack_sum": 0.0,
                "ack_count": 0,
                "fix_sum": 0.0,
                "fix_count": 0,
                "latest_closed": None,
            }
            grouped[machine_id] = stats

        stats["total_alerts"] += 1
        ack_value = alert.acknowledged_seconds
        if isinstance(ack_value, (int, float)) and ack_value >= 0:
            stats["ack_sum"] += float(ack_value)
            stats["ack_count"] += 1
        fix_value = alert.ack_to_clear_seconds
        if isinstance(fix_value, (int, float)) and fix_value >= 0:
            stats["fix_sum"] += float(fix_value)
            stats["fix_count"] += 1

        status = str(alert.status or "").upper()
        if status in {ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED}:
            closed_at = alert.resolved_at or alert.cancelled_at or alert.created_at
            closed_ts = _timestamp(closed_at)
            current = stats["latest_closed"]
            current_ts = _timestamp(current["closed_at"]) if current else -1
            if current is None or closed_ts > current_ts:
                stats["latest_closed"] = {
                    "status": status,
                    "department_id": alert.department_id,
                    "issue_category_id": alert.issue_category_id,
                    "issue_problem_id": alert.issue_problem_id,
                    "department_name": department_by_id.get(alert.department_id),
                    "issue_category_name": category_by_id.get(alert.issue_category_id),
                    "issue_problem_name": problem_by_id.get(alert.issue_problem_id),
                    "created_at": alert.created_at.isoformat() if alert.created_at else None,
                    "closed_at": closed_at.isoformat() if closed_at else None,
                }

    result = []
    for machine_id, stats in grouped.items():
        result.append(
            {
                "machine_id": machine_id,
                "total_alerts": int(stats["total_alerts"]),
                "average_acknowledge_seconds": round(stats["ack_sum"] / stats["ack_count"], 2) if stats["ack_count"] else None,
                "average_fix_seconds": round(stats["fix_sum"] / stats["fix_count"], 2) if stats["fix_count"] else None,
                "latest_closed": stats["latest_closed"],
            }
        )

    set_cached(cache_key, result, REPORT_MACHINE_STATS_CACHE_TTL_SECONDS)
    return result


def _filtered_alerts(filters):
    query = _base_alert_query(filters).options(
        load_only(
            AndonAlert.id,
            AndonAlert.company_id,
            AndonAlert.alert_number,
            AndonAlert.machine_id,
            AndonAlert.department_id,
            AndonAlert.issue_category_id,
            AndonAlert.issue_problem_id,
            AndonAlert.status,
            AndonAlert.priority,
            AndonAlert.responder_name_text,
            AndonAlert.note,
            AndonAlert.created_at,
            AndonAlert.acknowledged_at,
            AndonAlert.acknowledged_seconds,
            AndonAlert.arrived_at,
            AndonAlert.resolved_at,
            AndonAlert.ack_to_clear_seconds,
            AndonAlert.cancelled_at,
            AndonAlert.current_escalation_level,
            AndonAlert.resolution_note,
            AndonAlert.root_cause,
            AndonAlert.corrective_action,
        ),
        joinedload(AndonAlert.machine)
        .load_only(Machine.id, Machine.name, Machine.machine_type, Machine.department_id)
        .joinedload(Machine.department)
        .load_only(Department.id, Department.name),
        joinedload(AndonAlert.department).load_only(Department.id, Department.name),
        joinedload(AndonAlert.issue_category).load_only(IssueCategory.id, IssueCategory.name),
        joinedload(AndonAlert.issue_problem).load_only(IssueProblem.id, IssueProblem.name),
        noload(AndonAlert.company),
        noload(AndonAlert.operator_user),
        noload(AndonAlert.responder_user),
        noload(AndonAlert.events),
        noload(AndonAlert.escalations),
    )
    return query.order_by(AndonAlert.created_at.asc()).all()


def _base_alert_query(filters):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    query = AndonAlert.query
    conditions = []
    if company_id:
        conditions.append(AndonAlert.company_id == company_id)
    if machine_ids:
        conditions.append(AndonAlert.machine_id.in_(machine_ids))
    if department_ids:
        conditions.append(AndonAlert.department_id.in_(department_ids))
    if machine_group_names:
        conditions.append(AndonAlert.machine.has(Machine.machine_type.in_(machine_group_names)))

    start = _parse_dt(filters.get("start"))
    end = _parse_dt(filters.get("end"))
    department_id = filters.get("department_id")
    machine_id = filters.get("machine_id")
    category_id = filters.get("issue_category_id")
    problem_id = filters.get("issue_problem_id")
    machine_group = filters.get("machine_group")

    if start:
        conditions.append(AndonAlert.created_at >= start)
    if end:
        conditions.append(AndonAlert.created_at < end)
    if department_id:
        conditions.append(AndonAlert.department_id == department_id)
    if machine_id:
        conditions.append(AndonAlert.machine_id == machine_id)
    if machine_group:
        conditions.append(AndonAlert.machine.has(Machine.machine_type == machine_group))
    if category_id:
        conditions.append(AndonAlert.issue_category_id == category_id)
    if problem_id:
        conditions.append(AndonAlert.issue_problem_id == problem_id)

    if conditions:
        query = query.filter(and_(*conditions))
    return query


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


def _timestamp(value) -> float:
    if not value:
        return -1
    if isinstance(value, str):
        parsed = _parse_dt(value)
        if not parsed:
            return -1
        return parsed.timestamp()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _cache_filters(filters):
    relevant_keys = ["start", "end", "department_id", "machine_id", "machine_group", "issue_category_id", "issue_problem_id"]
    return tuple((key, str(filters.get(key)) if filters.get(key) is not None else None) for key in relevant_keys)


def _build_kpis(alerts):
    open_count = sum(1 for alert in alerts if alert.status in [ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED])
    closed_count = sum(1 for alert in alerts if alert.status in [ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED])
    escalated_count = sum(1 for alert in alerts if alert.current_escalation_level > 0)
    return {
        "total_alerts": len(alerts),
        "open_alerts": open_count,
        "closed_alerts": closed_count,
        "resolved_alerts": closed_count,
        "average_acknowledge_time": _avg([alert.wait_to_ack_seconds for alert in alerts]),
        "average_ack_to_clear_time": _avg([alert.ack_to_clear_seconds for alert in alerts]),
        "average_arrival_time": _avg([alert.wait_to_arrive_seconds for alert in alerts]),
        "average_resolution_time": _avg([_closed_seconds(alert) for alert in alerts]),
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


def _group_count_field(alerts, value_getter):
    counter = Counter()
    for alert in alerts:
        name = value_getter(alert)
        counter[name] += 1
    return [{"name": name, "count": count} for name, count in counter.most_common()]


def _top_machines(alerts):
    buckets = defaultdict(list)
    for alert in alerts:
        label = alert.machine.name if alert.machine else "Unassigned"
        buckets[(alert.machine_id, label)].append(alert)

    rows = []
    for (machine_id, machine_name), values in buckets.items():
        top_problem = _top_problem_for_alerts(values)
        rows.append(
            {
                "id": machine_id,
                "name": machine_name,
                "machine_group": values[0].machine.machine_type if values and values[0].machine and values[0].machine.machine_type else "Unassigned",
                "department_name": values[0].department.name if values and values[0].department else "Unassigned",
                "count": len(values),
                "average_acknowledge_seconds": _avg([alert.wait_to_ack_seconds for alert in values]),
                "average_ack_to_clear_seconds": _avg([alert.ack_to_clear_seconds for alert in values]),
                "average_total_seconds": _avg([_closed_seconds(alert) for alert in values]),
                "top_problem": top_problem["name"] if top_problem else None,
                "top_problem_count": top_problem["count"] if top_problem else None,
            }
        )
    return sorted(rows, key=lambda row: row["count"], reverse=True)


def _top_problems(alerts):
    buckets = defaultdict(list)
    for alert in alerts:
        label = alert.issue_problem.name if alert.issue_problem else "Unassigned"
        buckets[(alert.issue_problem_id, label)].append(alert)

    rows = []
    for (problem_id, problem_name), values in buckets.items():
        top_machine = _top_machine_for_alerts(values)
        top_group = _top_group_for_alerts(values)
        rows.append(
            {
                "id": problem_id,
                "name": problem_name,
                "category_name": values[0].issue_category.name if values and values[0].issue_category else "Unassigned",
                "count": len(values),
                "top_machine": top_machine["name"] if top_machine else None,
                "top_machine_count": top_machine["count"] if top_machine else None,
                "top_machine_group": top_group["name"] if top_group else None,
                "average_acknowledge_seconds": _avg([alert.wait_to_ack_seconds for alert in values]),
                "average_ack_to_clear_seconds": _avg([alert.ack_to_clear_seconds for alert in values]),
                "average_total_seconds": _avg([_closed_seconds(alert) for alert in values]),
            }
        )
    return sorted(rows, key=lambda row: row["count"], reverse=True)


def _top_problem_for_alerts(alerts):
    counter = Counter()
    names = {}
    for alert in alerts:
        key = alert.issue_problem_id
        name = alert.issue_problem.name if alert.issue_problem else "Unassigned"
        counter[key] += 1
        names[key] = name
    if not counter:
        return None
    problem_id, count = counter.most_common(1)[0]
    return {"id": problem_id, "name": names.get(problem_id, "Unassigned"), "count": count}


def _top_machine_for_alerts(alerts):
    counter = Counter()
    names = {}
    for alert in alerts:
        key = alert.machine_id
        name = alert.machine.name if alert.machine else "Unassigned"
        counter[key] += 1
        names[key] = name
    if not counter:
        return None
    machine_id, count = counter.most_common(1)[0]
    return {"id": machine_id, "name": names.get(machine_id, "Unassigned"), "count": count}


def _top_group_for_alerts(alerts):
    counter = Counter()
    names = {}
    for alert in alerts:
        key = alert.machine.machine_type if alert.machine and alert.machine.machine_type else "Unassigned"
        counter[key] += 1
        names[key] = key
    if not counter:
        return None
    group_name, count = counter.most_common(1)[0]
    return {"name": names.get(group_name, "Unassigned"), "count": count}


def _machine_detail(alert):
    closed_at = alert.resolved_at or alert.cancelled_at
    return {
        "id": alert.id,
        "alert_number": alert.alert_number,
        "status": alert.status,
        "machine_id": alert.machine_id,
        "department_name": alert.department.name if alert.department else "Unassigned",
        "machine_name": alert.machine.name if alert.machine else "Unassigned",
        "machine_group": alert.machine.machine_type if alert.machine and alert.machine.machine_type else "Unassigned",
        "issue_category_name": alert.issue_category.name if alert.issue_category else None,
        "issue_problem_name": alert.issue_problem.name if alert.issue_problem else None,
        "responder_name_text": alert.responder_name_text,
        "created_at": format_local_datetime(alert.created_at),
        "acknowledged_at": format_local_datetime(alert.acknowledged_at),
        "closed_at": format_local_datetime(closed_at),
        "acknowledged_seconds": alert.wait_to_ack_seconds,
        "ack_to_clear_seconds": alert.ack_to_clear_seconds,
        "total_seconds": _closed_seconds(alert),
        "note": alert.note,
        "resolution_note": alert.resolution_note,
        "root_cause": alert.root_cause,
        "corrective_action": alert.corrective_action,
    }


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
        if alert.ack_to_clear_seconds is not None:
            resolution_buckets[alert.responder_name_text].append(alert.ack_to_clear_seconds)
    rows = []
    for name, values in buckets.items():
        rows.append(
            {
                "name": name,
                "average_acknowledge_seconds": round(sum(values) / len(values), 2),
                "average_ack_to_clear_seconds": round(sum(resolution_buckets[name]) / len(resolution_buckets[name]), 2) if resolution_buckets[name] else None,
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


def _closed_seconds(alert):
    closed_at = alert.resolved_at or alert.cancelled_at
    if not alert.created_at or not closed_at:
        return None
    start = alert.created_at
    end = closed_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    return round((end - start).total_seconds(), 2)
