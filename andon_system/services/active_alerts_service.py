from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import and_, select

from ..extensions import db
from ..models.alert import ALERT_STATUSES_ACTIVE, AndonAlert
from ..models.department import Department
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from .cache_service import get_cached, set_cached

DEFAULT_ACTIVE_ALERTS_CACHE_TTL_SECONDS = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_seconds_for_status(status: str | None, created_at, acknowledged_at, resolved_at, cancelled_at):
    created = _ensure_aware(created_at)
    if not created:
        return None
    if status in {"ACKNOWLEDGED", "ARRIVED"}:
        start = _ensure_aware(acknowledged_at) or created
        return max(0, int((_utc_now() - start).total_seconds()))
    if status == "RESOLVED":
        end = _ensure_aware(resolved_at)
        return max(0, int((end - created).total_seconds())) if end else None
    if status == "CANCELLED":
        end = _ensure_aware(cancelled_at)
        return max(0, int((end - created).total_seconds())) if end else None
    return max(0, int((_utc_now() - created).total_seconds()))


def fetch_active_alert_payloads(
    *,
    company_id: int | None,
    status: str | None = "active",
    machine_ids: list[int] | None = None,
    department_ids: list[int] | None = None,
    role: str | None = None,
    pager_minimal: bool = False,
    alert_id: int | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: int = DEFAULT_ACTIVE_ALERTS_CACHE_TTL_SECONDS,
    metrics: dict | None = None,
):
    machine_ids = sorted({int(item) for item in (machine_ids or []) if item is not None})
    department_ids = sorted({int(item) for item in (department_ids or []) if item is not None})
    cache_key = (
        "active_alerts_list",
        company_id,
        status or "all",
        tuple(machine_ids),
        tuple(department_ids),
        role or "",
        "pager" if pager_minimal else "full",
    )

    cache_lookup_started_at = time.perf_counter()
    cached = get_cached(cache_key) if use_cache else None
    cache_lookup_ms = (time.perf_counter() - cache_lookup_started_at) * 1000
    if metrics is not None:
        metrics["cache_lookup_ms"] = round(cache_lookup_ms, 1)
    if cached is not None:
        if metrics is not None:
            metrics["cache"] = "hit"
            metrics["alert_count"] = len(cached)
        return cached

    query_started_at = time.perf_counter()
    stmt = (
        select(
            AndonAlert.id,
            AndonAlert.company_id,
            AndonAlert.alert_number,
            AndonAlert.machine_id,
            AndonAlert.department_id,
            AndonAlert.issue_category_id,
            AndonAlert.issue_problem_id,
            AndonAlert.status,
            AndonAlert.priority,
            AndonAlert.responder_user_id,
            AndonAlert.responder_name_text,
            AndonAlert.note,
            AndonAlert.created_at,
            AndonAlert.acknowledged_at,
            AndonAlert.acknowledged_seconds,
            AndonAlert.arrived_at,
            AndonAlert.resolved_at,
            AndonAlert.ack_to_clear_seconds,
            AndonAlert.cancelled_at,
            Machine.id.label("machine_id_ref"),
            Machine.name.label("machine_name"),
            Machine.machine_code.label("machine_code"),
            Machine.machine_type.label("machine_type"),
            Department.id.label("department_id_ref"),
            Department.name.label("department_name"),
            IssueCategory.id.label("category_id_ref"),
            IssueCategory.name.label("category_name"),
            IssueCategory.color.label("category_color"),
            IssueProblem.id.label("problem_id_ref"),
            IssueProblem.name.label("problem_name"),
        )
        .select_from(AndonAlert)
        .outerjoin(Machine, AndonAlert.machine_id == Machine.id)
        .outerjoin(Department, AndonAlert.department_id == Department.id)
        .outerjoin(IssueCategory, AndonAlert.issue_category_id == IssueCategory.id)
        .outerjoin(IssueProblem, AndonAlert.issue_problem_id == IssueProblem.id)
        .order_by(AndonAlert.priority.desc(), AndonAlert.created_at.asc())
    )

    predicates = []
    if company_id:
        predicates.append(AndonAlert.company_id == company_id)
    if machine_ids:
        predicates.append(AndonAlert.machine_id.in_(machine_ids))
    if department_ids and role != "Operator":
        predicates.append(AndonAlert.department_id.in_(department_ids))
    if status == "active":
        predicates.append(AndonAlert.status.in_(ALERT_STATUSES_ACTIVE))
    elif status:
        predicates.append(AndonAlert.status == status)
    if alert_id is not None:
        predicates.append(AndonAlert.id == int(alert_id))
    if predicates:
        stmt = stmt.where(and_(*predicates))

    rows = db.session.execute(stmt).mappings().all()
    alert_base_query_ms = (time.perf_counter() - query_started_at) * 1000

    serialize_started_at = time.perf_counter()
    payload = []
    for row in rows:
        item = {
            "id": row["id"],
            "company_id": row["company_id"],
            "alert_number": row["alert_number"],
            "status": row["status"],
            "priority": row["priority"],
            "responder_user_id": row["responder_user_id"],
            "responder_name_text": row["responder_name_text"],
            "note": row["note"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "acknowledged_at": row["acknowledged_at"].isoformat() if row["acknowledged_at"] else None,
            "acknowledged_seconds": row["acknowledged_seconds"],
            "arrived_at": row["arrived_at"].isoformat() if row["arrived_at"] else None,
            "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
            "ack_to_clear_seconds": row["ack_to_clear_seconds"],
            "elapsed_seconds": _elapsed_seconds_for_status(
                row["status"],
                row["created_at"],
                row["acknowledged_at"],
                row["resolved_at"],
                row["cancelled_at"],
            ),
            "machine_id": row["machine_id"],
            "department_id": row["department_id"],
            "issue_category_id": row["issue_category_id"],
            "issue_problem_id": row["issue_problem_id"],
            "machine": {
                "id": row["machine_id_ref"] or row["machine_id"],
                "name": row["machine_name"],
                "machine_code": row["machine_code"],
                "machine_type": row["machine_type"],
            },
            "department": {
                "id": row["department_id_ref"] or row["department_id"],
                "name": row["department_name"],
            },
            "issue_category": {
                "id": row["category_id_ref"] or row["issue_category_id"],
                "name": row["category_name"],
                "color": row["category_color"] or "#ef476f",
            },
            "issue_problem": {
                "id": row["problem_id_ref"] or row["issue_problem_id"],
                "name": row["problem_name"],
            },
        }
        item["department_name"] = item["department"]["name"]
        item["category_name"] = item["issue_category"]["name"]
        item["problem_name"] = item["issue_problem"]["name"]
        item["color"] = item["issue_category"]["color"]
        if pager_minimal:
            item["status_label"] = {
                "OPEN": "Open",
                "ACKNOWLEDGED": "Acknowledged",
                "ARRIVED": "Working",
            }.get(item["status"], str(item["status"] or "").title())
            item["action_available"] = (
                "acknowledge"
                if item["status"] == "OPEN"
                else "resolve"
                if item["status"] in {"ACKNOWLEDGED", "ARRIVED"}
                else None
            )
        payload.append(item)
    serialize_ms = (time.perf_counter() - serialize_started_at) * 1000

    cache_store_started_at = time.perf_counter()
    if use_cache:
        set_cached(cache_key, payload, ttl_seconds=max(1, int(cache_ttl_seconds)))
    cache_store_ms = (time.perf_counter() - cache_store_started_at) * 1000
    if metrics is not None:
        metrics["cache"] = "miss"
        metrics["alert_base_query_ms"] = round(alert_base_query_ms, 1)
        metrics["serialize_ms"] = round(serialize_ms, 1)
        metrics["cache_store_ms"] = round(cache_store_ms, 1)
        metrics["alert_count"] = len(payload)
    return payload


def fetch_alert_payload_by_id(alert_id: int, *, company_id: int | None = None):
    rows = fetch_active_alert_payloads(company_id=company_id, status=None, alert_id=alert_id, use_cache=False)
    return next((item for item in rows if int(item.get("id") or 0) == int(alert_id)), None)
