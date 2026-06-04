from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import time
from types import SimpleNamespace
from uuid import uuid4

from flask import current_app
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import select, text
from sqlalchemy.orm import joinedload, load_only, noload

from ..extensions import db
from ..company_context import get_current_company_id
from ..models.alert import (
    ALERT_STATUS_ACKNOWLEDGED,
    ALERT_STATUS_ARRIVED,
    ALERT_STATUS_CANCELLED,
    ALERT_STATUS_OPEN,
    ALERT_STATUS_RESOLVED,
    AndonAlert,
    AndonAlertEvent,
    EVENT_ACKNOWLEDGED,
    EVENT_ARRIVED,
    EVENT_CANCELLED,
    EVENT_CREATED,
    EVENT_NOTE_ADDED,
    EVENT_RESOLVED,
)
from ..models.escalation import EscalationRule
from ..models.issue import IssueCategory, IssueProblem
from ..models.machine import Machine
from ..models.user import User, UserCompanyAccess
from ..models.department import Department
from ..security import get_current_membership, get_scope_filters
from .cache_service import invalidate_live_alert_caches
from .active_alerts_service import fetch_active_alert_payloads
from .active_alerts_service import fetch_alert_payload_by_id
from .realtime_service import emit_alert_created, emit_alert_updated


class AlertServiceError(ValueError):
    def __init__(self, message, status_code=400, data=None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data or {}


def _deep_alert_debug_enabled() -> bool:
    return bool(current_app.config.get("ANDON_PERF_LOGS")) and bool(
        current_app.config.get("ANDON_DEEP_DEBUG_ALERT_LIFECYCLE")
    )


def utc_now():
    return datetime.now(timezone.utc)


def _duration_seconds(start, end):
    if not start or not end:
        return None
    start = _ensure_aware(start)
    end = _ensure_aware(end)
    return int((end - start).total_seconds())


def _ensure_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def list_active_alerts(status: str | None = None):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    membership = get_current_membership()
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    return fetch_active_alert_payloads(
        company_id=company_id,
        status=status,
        machine_ids=machine_ids,
        department_ids=department_ids,
        role=getattr(membership, "role", None),
    )


def get_alert(alert_id: int):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    membership = get_current_membership()
    role = membership.role if membership else None
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    query = AndonAlert.query.filter(AndonAlert.id == alert_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    if machine_ids:
        query = query.filter(AndonAlert.machine_id.in_(machine_ids))
    if department_ids and role != "Operator":
        query = query.filter(AndonAlert.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(AndonAlert.machine.has(Machine.machine_type.in_(machine_group_names)))
    alert = query.options(*_alert_response_options()).one_or_none()
    if not alert:
        raise AlertServiceError("Alert not found")
    return alert


def _get_alert_for_mutation(alert_id: int):
    company_id = get_current_company_id()
    scope = get_scope_filters()
    membership = get_current_membership()
    role = membership.role if membership else None
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])

    query = AndonAlert.query.options(
        load_only(
            AndonAlert.id,
            AndonAlert.company_id,
            AndonAlert.machine_id,
            AndonAlert.department_id,
            AndonAlert.status,
            AndonAlert.created_at,
            AndonAlert.acknowledged_at,
            AndonAlert.note,
            AndonAlert.responder_user_id,
            AndonAlert.responder_name_text,
            AndonAlert.resolution_note,
            AndonAlert.root_cause,
            AndonAlert.corrective_action,
        ),
        noload("*"),
    ).filter(AndonAlert.id == alert_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    if machine_ids:
        query = query.filter(AndonAlert.machine_id.in_(machine_ids))
    if department_ids and role != "Operator":
        query = query.filter(AndonAlert.department_id.in_(department_ids))
    if machine_group_names:
        query = query.filter(AndonAlert.machine.has(Machine.machine_type.in_(machine_group_names)))
    alert = query.one_or_none()
    if not alert:
        raise AlertServiceError("Alert not found")
    return alert


def _perf_log_alert_mutation(action: str, metrics: dict) -> None:
    if not current_app.config.get("ANDON_PERF_LOGS"):
        return
    current_app.logger.debug(
        "PERF alert_mutation action=%s alert_lookup_ms=%.1f user_lookup_ms=%.1f update_fields_ms=%.1f "
        "event_insert_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f socket_emit_ms=%.1f total_ms=%.1f",
        action,
        metrics.get("alert_lookup_ms", 0.0),
        metrics.get("user_lookup_ms", 0.0),
        metrics.get("update_fields_ms", 0.0),
        metrics.get("event_insert_ms", 0.0),
        metrics.get("db_commit_ms", 0.0),
        metrics.get("cache_invalidate_ms", 0.0),
        metrics.get("socket_emit_ms", 0.0),
        metrics.get("total_ms", 0.0),
    )


def _perf_alert_step(action: str, step: str, started_at: float, previous_at: float, *, alert_id=None, company_id=None, machine_id=None):
    now = time.perf_counter()
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_%s_step step=%s elapsed_ms_from_start=%.1f delta_ms_from_previous_step=%.1f alert_id=%s company_id=%s machine_id=%s",
            action,
            step,
            (now - started_at) * 1000,
            (now - previous_at) * 1000,
            alert_id,
            company_id,
            machine_id,
        )
    return now


def _perf_alert_reconcile(action: str, started_at: float, metrics: dict):
    if not current_app.config.get("ANDON_PERF_LOGS"):
        return
    known_keys = (
        "alert_lookup_ms",
        "user_lookup_ms",
        "update_fields_ms",
        "event_insert_ms",
        "db_commit_ms",
        "cache_invalidate_ms",
        "socket_emit_ms",
        "payload_fetch_ms",
    )
    summed = sum(float(metrics.get(key, 0.0) or 0.0) for key in known_keys)
    total = (time.perf_counter() - started_at) * 1000
    current_app.logger.debug(
        "PERF alert_%s_reconcile wall_clock_total_ms=%.1f summed_known_segments_ms=%.1f unexplained_gap_ms=%.1f",
        action,
        total,
        summed,
        max(0.0, total - summed),
    )


def _get_alert_for_mutation_scoped(alert_id: int, *, company_id: int, department_id: int | None = None):
    query = AndonAlert.query.options(
        load_only(
            AndonAlert.id,
            AndonAlert.company_id,
            AndonAlert.machine_id,
            AndonAlert.department_id,
            AndonAlert.status,
            AndonAlert.created_at,
            AndonAlert.acknowledged_at,
            AndonAlert.note,
            AndonAlert.responder_user_id,
            AndonAlert.responder_name_text,
            AndonAlert.resolution_note,
            AndonAlert.root_cause,
            AndonAlert.corrective_action,
        ),
        noload("*"),
    ).filter(AndonAlert.id == alert_id, AndonAlert.company_id == company_id)
    if department_id is not None:
        query = query.filter(AndonAlert.department_id == department_id)
    alert = query.one_or_none()
    if not alert:
        raise AlertServiceError("Alert not found", status_code=404)
    return alert


def _perf_log_create_alert(metrics: dict) -> None:
    if not current_app.config.get("ANDON_PERF_LOGS"):
        return
    current_app.logger.debug(
        "PERF alert_create_service machine_lookup_ms=%.1f permission_scope_ms=%.1f issue_category_lookup_ms=%.1f "
        "issue_problem_lookup_ms=%.1f duplicate_active_check_ms=%.1f alert_object_build_ms=%.1f db_add_ms=%.1f "
        "db_flush_ms=%.1f note_insert_ms=%.1f event_insert_ms=%.1f db_commit_ms=%.1f cache_invalidate_ms=%.1f "
        "before_cache_call_ms=%.1f actual_invalidate_call_ms=%.1f after_cache_call_ms=%.1f payload_fetch_ms=%.1f "
        "socket_emit_ms=%.1f escalation_check_ms=%.1f email_send_ms=%.1f notification_ms=%.1f total_ms=%.1f",
        metrics.get("machine_lookup_ms", 0.0),
        metrics.get("permission_scope_ms", 0.0),
        metrics.get("issue_category_lookup_ms", 0.0),
        metrics.get("issue_problem_lookup_ms", 0.0),
        metrics.get("duplicate_active_check_ms", 0.0),
        metrics.get("alert_object_build_ms", 0.0),
        metrics.get("db_add_ms", 0.0),
        metrics.get("db_flush_ms", 0.0),
        metrics.get("note_insert_ms", 0.0),
        metrics.get("event_insert_ms", 0.0),
        metrics.get("db_commit_ms", 0.0),
        metrics.get("cache_invalidate_ms", 0.0),
        metrics.get("before_cache_call_ms", 0.0),
        metrics.get("actual_invalidate_call_ms", 0.0),
        metrics.get("after_cache_call_ms", 0.0),
        metrics.get("payload_fetch_ms", 0.0),
        metrics.get("socket_emit_ms", 0.0),
        metrics.get("escalation_check_ms", 0.0),
        metrics.get("email_send_ms", 0.0),
        metrics.get("notification_ms", 0.0),
        metrics.get("total_ms", 0.0),
    )


def _perf_step(
    step: str,
    started_at: float,
    previous_at: float,
    *,
    alert_id=None,
    machine_id=None,
    company_id=None,
):
    now = time.perf_counter()
    elapsed_ms = (now - started_at) * 1000
    delta_ms = (now - previous_at) * 1000
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_create_step step=%s elapsed_ms_from_start=%.1f delta_ms_from_previous_step=%.1f alert_id=%s machine_id=%s company_id=%s",
            step,
            elapsed_ms,
            delta_ms,
            alert_id,
            machine_id,
            company_id,
        )
    return now


def _perf_pg_diagnostics(tag: str) -> None:
    if not _deep_alert_debug_enabled() or db.engine.dialect.name != "postgresql":
        return
    try:
        backend_pid = db.session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        txid = db.session.execute(text("SELECT txid_current_if_assigned()")).scalar_one()
        current = db.session.execute(
            text(
                """
                SELECT pid, state, wait_event_type, wait_event, LEFT(query, 180) AS query
                FROM pg_stat_activity
                WHERE pid = pg_backend_pid()
                """
            )
        ).mappings().first()
        locks = db.session.execute(
            text(
                """
                SELECT mode, granted
                FROM pg_locks
                WHERE pid = pg_backend_pid()
                """
            )
        ).mappings().all()
        lock_summary = ",".join(f"{row['mode']}:{'g' if row['granted'] else 'w'}" for row in locks[:12])
        current_app.logger.debug(
            "PERF alert_create_pg tag=%s backend_pid=%s txid=%s state=%s wait_event_type=%s wait_event=%s locks=%s query=%s",
            tag,
            backend_pid,
            txid,
            current.get("state") if current else None,
            current.get("wait_event_type") if current else None,
            current.get("wait_event") if current else None,
            lock_summary,
            current.get("query") if current else None,
        )
    except Exception:
        current_app.logger.exception("PERF alert_create_pg diagnostics failed tag=%s", tag)


def _find_active_alert_id_for_machine(machine_id: int, company_id: int | None) -> int | None:
    stmt = select(AndonAlert.id).where(
        AndonAlert.machine_id == machine_id,
        AndonAlert.status.in_([ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]),
    )
    if company_id:
        stmt = stmt.where(AndonAlert.company_id == company_id)
    return db.session.execute(stmt.limit(1)).scalar_one_or_none()


def _find_active_alert_id_for_machine_department(machine_id: int, department_id: int, company_id: int | None) -> int | None:
    stmt = select(AndonAlert.id).where(
        AndonAlert.machine_id == machine_id,
        AndonAlert.department_id == department_id,
        AndonAlert.status.in_([ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]),
    )
    if company_id:
        stmt = stmt.where(AndonAlert.company_id == company_id)
    return db.session.execute(stmt.limit(1)).scalar_one_or_none()


def _normalize_department_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def create_alert(payload: dict, metrics: dict | None = None):
    started_at = time.perf_counter()
    previous_step_at = started_at
    perf = metrics if isinstance(metrics, dict) else {}
    known_segments = {}
    created_alert_id = None
    created_alert_ids: list[int] = []
    created_machine_id = None
    created_company_id = None
    created_status = None
    created_at_iso = None
    commit_done_at = started_at
    company_id = get_current_company_id()
    current_app.logger.debug(
        "SERVICE alert_create start machine_id=%s department_id=%s issue_category_id=%s issue_problem_id=%s",
        payload.get("machine_id"),
        payload.get("department_id"),
        payload.get("issue_category_id"),
        payload.get("issue_problem_id"),
    )
    previous_step_at = _perf_step("start", started_at, previous_step_at, machine_id=payload.get("machine_id"), company_id=company_id)
    scope = get_scope_filters()
    machine_ids = scope.get("machine_ids") or []
    department_ids = scope.get("department_ids") or ([scope["department_id"]] if scope.get("department_id") is not None else [])
    machine_group_names = scope.get("machine_group_names") or ([scope["machine_group_name"]] if scope.get("machine_group_name") else [])
    machine_lookup_started_at = time.perf_counter()
    machine_query = Machine.query.options(
        load_only(
            Machine.id,
            Machine.company_id,
            Machine.machine_type,
            Machine.department_id,
            Machine.is_active,
        ),
        noload("*"),
    ).filter(Machine.id == payload.get("machine_id"))
    if company_id:
        machine_query = machine_query.filter(Machine.company_id == company_id)
    try:
        machine = machine_query.one_or_none()
    except OperationalError as exc:
        # On PostgreSQL, NOWAIT raises when another transaction currently holds
        # the row lock; returning quickly avoids multi-second UI jitter.
        raise AlertServiceError(
            "Machine is busy with another in-progress call. Please try again.",
            status_code=409,
        ) from exc
    perf["machine_lookup_ms"] = (time.perf_counter() - machine_lookup_started_at) * 1000
    known_segments["machine_lookup_ms"] = perf["machine_lookup_ms"]
    previous_step_at = _perf_step("after_machine_lookup", started_at, previous_step_at, machine_id=getattr(machine, "id", None), company_id=getattr(machine, "company_id", company_id))
    department_id = payload.get("department_id")
    issue_category_lookup_started_at = time.perf_counter()
    issue_category = None
    if payload.get("issue_category_id"):
        category_query = IssueCategory.query.options(
            load_only(
                IssueCategory.id,
                IssueCategory.company_id,
                IssueCategory.department_id,
                IssueCategory.name,
                IssueCategory.priority_default,
                IssueCategory.is_active,
            ),
            noload("*"),
        ).filter(IssueCategory.id == payload.get("issue_category_id"))
        if company_id:
            category_query = category_query.filter(IssueCategory.company_id == company_id)
        issue_category = category_query.one_or_none()
    perf["issue_category_lookup_ms"] = (time.perf_counter() - issue_category_lookup_started_at) * 1000
    known_segments["issue_category_lookup_ms"] = perf["issue_category_lookup_ms"]
    previous_step_at = _perf_step("after_issue_category_lookup", started_at, previous_step_at, machine_id=getattr(machine, "id", None), company_id=company_id)
    issue_problem_lookup_started_at = time.perf_counter()
    problem_query = IssueProblem.query.options(
        load_only(
            IssueProblem.id,
            IssueProblem.company_id,
            IssueProblem.category_id,
            IssueProblem.name,
            IssueProblem.severity_default,
            IssueProblem.is_active,
        ),
        noload("*"),
    ).filter(IssueProblem.id == payload.get("issue_problem_id"))
    if company_id:
        problem_query = problem_query.filter(IssueProblem.company_id == company_id)
    issue_problem = problem_query.one_or_none()
    perf["issue_problem_lookup_ms"] = (time.perf_counter() - issue_problem_lookup_started_at) * 1000
    known_segments["issue_problem_lookup_ms"] = perf["issue_problem_lookup_ms"]
    previous_step_at = _perf_step("after_issue_problem_lookup", started_at, previous_step_at, machine_id=getattr(machine, "id", None), company_id=company_id)
    operator_user = None
    if payload.get("operator_user_id"):
        operator_query = User.query.options(
            load_only(User.id, User.company_id, User.display_name, User.is_active),
            noload("*"),
        ).filter(User.id == payload.get("operator_user_id"))
        if company_id:
            operator_query = operator_query.filter(User.company_id == company_id)
        operator_user = operator_query.one_or_none()

    permission_scope_started_at = time.perf_counter()
    if not machine:
        raise AlertServiceError("Valid machine_id is required")
    if machine_ids and machine.id not in set(machine_ids):
        raise AlertServiceError("Machine is outside your assigned scope", status_code=403)
    if company_id and machine.company_id != company_id:
        raise AlertServiceError("Machine does not belong to the selected company")
    if department_ids and machine.department_id not in set(department_ids):
        raise AlertServiceError("Machine is outside your assigned department scope", status_code=403)
    if machine_group_names and machine.machine_type not in set(machine_group_names):
        raise AlertServiceError("Machine is outside your assigned machine group scope", status_code=403)
    perf["permission_scope_ms"] = (time.perf_counter() - permission_scope_started_at) * 1000
    known_segments["permission_scope_ms"] = perf["permission_scope_ms"]
    previous_step_at = _perf_step("after_permission_scope", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    # Keep the friendly conflict path even though the database also enforces
    # one active alert per machine now.
    gap_started_at = time.perf_counter()
    previous_step_at = _perf_step("gap_enter_after_duplicate", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if not department_id and not issue_category:
        if _deep_alert_debug_enabled():
            current_app.logger.debug(
                "PERF alert_create_category_fallback query_ms=0.0 used=false reason=missing_issue_category_id",
            )
        raise AlertServiceError("issue_category_id is required")
    previous_step_at = _perf_step("gap_after_department_required_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if issue_category:
        if _deep_alert_debug_enabled():
            current_app.logger.debug(
                "PERF alert_create_category_fallback query_ms=0.0 used=false reason=category_id_provided",
            )
    else:
        if _deep_alert_debug_enabled():
            current_app.logger.debug(
                "PERF alert_create_category_fallback query_ms=0.0 used=false reason=fallback_disabled",
            )
        raise AlertServiceError("Valid issue_category_id is required")
    if not issue_category:
        previous_step_at = _perf_step("gap_missing_issue_category", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
        raise AlertServiceError("Valid issue category is required for the selected department")
    previous_step_at = _perf_step("gap_after_issue_category_presence_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if not issue_problem:
        previous_step_at = _perf_step("gap_missing_issue_problem", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
        raise AlertServiceError("Valid issue_problem_id is required")
    previous_step_at = _perf_step("gap_after_issue_problem_presence_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if issue_problem.category_id != issue_category.id:
        previous_step_at = _perf_step("gap_issue_problem_category_mismatch", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
        raise AlertServiceError("Issue problem must belong to the selected department")
    previous_step_at = _perf_step("gap_after_problem_category_match_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if company_id and issue_category.company_id != company_id:
        previous_step_at = _perf_step("gap_issue_category_company_mismatch", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
        raise AlertServiceError("Issue category does not belong to the selected company")
    previous_step_at = _perf_step("gap_after_issue_category_company_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if company_id and issue_problem.company_id != company_id:
        previous_step_at = _perf_step("gap_issue_problem_company_mismatch", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
        raise AlertServiceError("Issue problem does not belong to the selected company")
    previous_step_at = _perf_step("gap_after_issue_problem_company_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)

    source_department = None
    if payload.get("department_id"):
        source_department = Department.query.options(
            load_only(Department.id, Department.company_id, Department.name, Department.is_active),
            noload("*"),
        ).filter(Department.id == payload.get("department_id"))
        if company_id:
            source_department = source_department.filter(Department.company_id == company_id)
        source_department = source_department.one_or_none()

    target_departments = []
    selected_department_name = _normalize_department_name(getattr(source_department, "name", None))
    if selected_department_name == "quality and supervisor":
        current_app.logger.debug(
            "SERVICE alert_create combined department detected source_department_id=%s source_department_name=%s",
            getattr(source_department, "id", None),
            getattr(source_department, "name", None),
        )
        target_departments = (
            Department.query.options(
                load_only(Department.id, Department.company_id, Department.name, Department.is_active),
                noload("*"),
            )
            .filter(
                Department.company_id == company_id,
                Department.name.in_(["Quality", "Supervisor"]),
                Department.is_active.is_(True),
            )
            .order_by(Department.name.asc())
            .all()
        )
        if len(target_departments) != 2:
            raise AlertServiceError("Quality and Supervisor departments must both exist and be active")
    else:
        department_id = issue_category.department_id
        target_departments = [
            SimpleNamespace(
                id=department_id,
                name=getattr(source_department, "name", None),
                company_id=company_id,
                is_active=True,
            )
        ]
    current_app.logger.debug(
        "SERVICE alert_create expanded targets=%s source_department_id=%s issue_category_id=%s issue_problem_id=%s",
        [{"id": int(target.id), "name": getattr(target, "name", None)} for target in target_departments],
        getattr(source_department, "id", None),
        issue_category.id if issue_category else None,
        issue_problem.id if issue_problem else None,
    )

    duplicate_check_started_at = time.perf_counter()
    existing_alert_id = None
    for target_department in target_departments:
        existing_alert_id = _find_active_alert_id_for_machine_department(machine.id, int(target_department.id), company_id)
        if existing_alert_id:
            break
    perf["duplicate_active_check_ms"] = (time.perf_counter() - duplicate_check_started_at) * 1000
    known_segments["duplicate_active_check_ms"] = perf["duplicate_active_check_ms"]
    previous_step_at = _perf_step("after_duplicate_check", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    if existing_alert_id:
        existing_payload = fetch_alert_payload_by_id(existing_alert_id, company_id=company_id)
        raise AlertServiceError(
            "An active alert already exists for this machine and department",
            status_code=409,
            data={"existing_alert": existing_payload or {"id": existing_alert_id}},
        )

    gap_ms = (time.perf_counter() - gap_started_at) * 1000
    perf["after_duplicate_to_before_build_ms"] = gap_ms
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_create_gap after_duplicate_to_before_build_ms=%.1f detail=category_fallback_ms:%s",
            gap_ms,
            0.0,
        )

    previous_step_at = _perf_step("before_alert_build", started_at, previous_step_at, machine_id=machine.id, company_id=company_id)
    alert_build_started_at = time.perf_counter()
    resolved_company_id = company_id or machine.company_id
    alerts = []
    for target_department in target_departments:
        alert_number = f"AL-{utc_now():%Y%m%d%H%M%S}-{uuid4().hex[:6].upper()}"
        current_app.logger.debug(
            "SERVICE alert_create building alert target_department_id=%s target_department_name=%s machine_id=%s issue_problem_id=%s",
            int(target_department.id),
            getattr(target_department, "name", None),
            machine.id,
            issue_problem.id if issue_problem else None,
        )
        alerts.append(
            AndonAlert(
                company_id=resolved_company_id,
                alert_number=alert_number,
                machine_id=machine.id,
                department_id=int(target_department.id),
                issue_category_id=issue_category.id,
                issue_problem_id=issue_problem.id,
                status=ALERT_STATUS_OPEN,
                priority=payload.get("priority") or issue_category.priority_default or issue_problem.severity_default or 3,
                operator_user_id=operator_user.id if operator_user else None,
                operator_name_text=payload.get("operator_name_text"),
                note=payload.get("note"),
            )
        )
    perf["alert_object_build_ms"] = (time.perf_counter() - alert_build_started_at) * 1000
    known_segments["alert_object_build_ms"] = perf["alert_object_build_ms"]
    previous_step_at = _perf_step("after_alert_build", started_at, previous_step_at, machine_id=machine.id, company_id=resolved_company_id)
    try:
        previous_step_at = _perf_step("before_db_add", started_at, previous_step_at, machine_id=machine.id, company_id=resolved_company_id)
        add_started_at = time.perf_counter()
        for alert in alerts:
            db.session.add(alert)
        perf["db_add_ms"] = (time.perf_counter() - add_started_at) * 1000
        known_segments["db_add_ms"] = perf["db_add_ms"]
        previous_step_at = _perf_step("after_db_add", started_at, previous_step_at, machine_id=machine.id, company_id=resolved_company_id)
        previous_step_at = _perf_step("before_flush", started_at, previous_step_at, machine_id=machine.id, company_id=resolved_company_id)
        _perf_pg_diagnostics("before_flush")
        flush_started_at = time.perf_counter()
        db.session.flush()
        perf["db_flush_ms"] = (time.perf_counter() - flush_started_at) * 1000
        known_segments["db_flush_ms"] = perf["db_flush_ms"]
        previous_step_at = _perf_step("after_flush", started_at, previous_step_at, alert_id=alerts[0].id if alerts else None, machine_id=machine.id, company_id=resolved_company_id)
        _perf_pg_diagnostics("after_flush")
        current_app.logger.debug(
            "SERVICE alert_create flush complete created_alert_ids=%s target_departments=%s",
            [alert.id for alert in alerts],
            [{"id": int(target.id), "name": getattr(target, "name", None)} for target in target_departments],
        )

        previous_step_at = _perf_step("before_event_insert", started_at, previous_step_at, alert_id=alerts[0].id if alerts else None, machine_id=machine.id, company_id=resolved_company_id)
        event_started_at = time.perf_counter()
        for alert in alerts:
            _add_event(
                alert,
                EVENT_CREATED,
                user=operator_user,
                user_name_text=payload.get("operator_name_text") or (operator_user.display_name if operator_user else None),
                message="Alert created",
                metadata={"note": payload.get("note")},
            )
        perf["event_insert_ms"] = (time.perf_counter() - event_started_at) * 1000
        known_segments["event_insert_ms"] = perf["event_insert_ms"]
        previous_step_at = _perf_step("after_event_insert", started_at, previous_step_at, alert_id=alerts[0].id if alerts else None, machine_id=machine.id, company_id=resolved_company_id)
        perf["note_insert_ms"] = 0.0
        created_alert_ids = [alert.id for alert in alerts]
        created_alert_id = created_alert_ids[0] if created_alert_ids else None
        created_company_id = alerts[0].company_id
        created_machine_id = alerts[0].machine_id
        created_status = alerts[0].status
        created_at_iso = alerts[0].created_at.isoformat() if alerts[0].created_at else None
        previous_step_at = _perf_step("before_commit", started_at, previous_step_at, alert_id=created_alert_id, machine_id=created_machine_id, company_id=created_company_id)
        _perf_pg_diagnostics("before_commit")
        commit_started_at = time.perf_counter()
        db.session.commit()
        commit_done_at = time.perf_counter()
        perf["db_commit_ms"] = (commit_done_at - commit_started_at) * 1000
        known_segments["db_commit_ms"] = perf["db_commit_ms"]
        previous_step_at = _perf_step("after_commit", started_at, previous_step_at, alert_id=created_alert_ids[0] if created_alert_ids else None, machine_id=created_machine_id, company_id=created_company_id)
        _perf_pg_diagnostics("after_commit")
        current_app.logger.debug(
            "SERVICE alert_create commit complete created_alert_ids=%s machine_id=%s company_id=%s",
            created_alert_ids,
            created_machine_id,
            created_company_id,
        )
    except IntegrityError as exc:
        db.session.rollback()
        current_app.logger.exception(
            "SERVICE alert_create integrity failure machine_id=%s source_department_id=%s targets=%s",
            machine.id if machine else None,
            getattr(source_department, "id", None),
            [{"id": int(target.id), "name": getattr(target, "name", None)} for target in target_departments],
        )
        existing_alert_id = None
        for target_department in target_departments:
            existing_alert_id = _find_active_alert_id_for_machine_department(machine.id, int(target_department.id), resolved_company_id)
            if existing_alert_id:
                break
        if existing_alert_id:
            existing_payload = fetch_alert_payload_by_id(existing_alert_id, company_id=resolved_company_id)
            raise AlertServiceError(
                "An active alert already exists for this machine and department",
                status_code=409,
                data={"existing_alert": existing_payload or {"id": existing_alert_id}},
            ) from exc
        existing_any = _get_latest_alert_for_machine(machine.id, resolved_company_id)
        if existing_any:
            raise AlertServiceError(
                "A machine-level alert uniqueness rule blocked this alert. Close or cancel the existing alert for this machine and try again.",
                status_code=409,
                data={"existing_alert": existing_any.to_dict()},
            ) from exc
        raise AlertServiceError("Unable to create alert due to a database constraint", status_code=409) from exc
    previous_step_at = _perf_step("before_cache", started_at, previous_step_at, alert_id=created_alert_id, machine_id=created_machine_id, company_id=created_company_id)
    cache_started_at = time.perf_counter()
    perf["before_cache_call_ms"] = (cache_started_at - commit_done_at) * 1000
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_create_checkpoint phase=before_cache after_commit_to_cache_start_ms=%.1f alert_id=%s alert_ids=%s company_id=%s machine_id=%s status=%s created_at=%s",
            perf["before_cache_call_ms"],
            created_alert_id,
            created_alert_ids,
            created_company_id,
            created_machine_id,
            created_status,
            created_at_iso,
        )
    try:
        _invalidate_live_caches(created_company_id)
    except Exception:
        current_app.logger.exception(
            "Alert created but cache invalidation failed alert_id=%s alert_ids=%s company_id=%s",
            created_alert_id,
            created_alert_ids,
            created_company_id,
        )
    cache_call_done_at = time.perf_counter()
    perf["actual_invalidate_call_ms"] = (cache_call_done_at - cache_started_at) * 1000
    perf["after_cache_call_ms"] = (time.perf_counter() - cache_call_done_at) * 1000
    perf["cache_invalidate_ms"] = perf["before_cache_call_ms"] + perf["actual_invalidate_call_ms"] + perf["after_cache_call_ms"]
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_create_checkpoint phase=after_cache after_commit_to_cache_start_ms=%.1f actual_cache_call_ms=%.1f after_cache_call_ms=%.1f alert_id=%s alert_ids=%s",
            perf["before_cache_call_ms"],
            perf["actual_invalidate_call_ms"],
            perf["after_cache_call_ms"],
            created_alert_id,
            created_alert_ids,
        )
    previous_step_at = _perf_step("after_cache", started_at, previous_step_at, alert_id=created_alert_id, machine_id=created_machine_id, company_id=created_company_id)
    previous_step_at = _perf_step("before_socket_emit", started_at, previous_step_at, alert_id=created_alert_id, machine_id=created_machine_id, company_id=created_company_id)
    emit_started_at = time.perf_counter()
    try:
        for emitted_alert_id in created_alert_ids:
            current_app.logger.debug(
                "SERVICE alert_create emitting realtime alert_id=%s machine_id=%s company_id=%s",
                emitted_alert_id,
                created_machine_id,
                created_company_id,
            )
            emit_alert_created(created_company_id, emitted_alert_id, machine_id=created_machine_id, status=created_status)
    except Exception:
        current_app.logger.exception(
            "Alert created but realtime emit failed alert_id=%s alert_ids=%s company_id=%s",
            created_alert_id,
            created_alert_ids,
            created_company_id,
        )
    perf["socket_emit_ms"] = (time.perf_counter() - emit_started_at) * 1000
    known_segments["socket_emit_ms"] = perf["socket_emit_ms"]
    previous_step_at = _perf_step("after_socket_emit", started_at, previous_step_at, alert_id=created_alert_id, machine_id=created_machine_id, company_id=created_company_id)
    previous_step_at = _perf_step("before_return", started_at, previous_step_at, alert_id=created_alert_ids[0] if created_alert_ids else None, machine_id=created_machine_id, company_id=created_company_id)
    perf.setdefault("payload_fetch_ms", 0.0)
    perf.setdefault("escalation_check_ms", 0.0)
    perf.setdefault("email_send_ms", 0.0)
    perf.setdefault("notification_ms", 0.0)
    perf["total_ms"] = (time.perf_counter() - started_at) * 1000
    summed_known = sum(float(value or 0.0) for value in known_segments.values())
    unexplained = max(0.0, perf["total_ms"] - summed_known)
    if _deep_alert_debug_enabled():
        current_app.logger.debug(
            "PERF alert_create_reconcile wall_clock_total_ms=%.1f summed_known_segments_ms=%.1f unexplained_gap_ms=%.1f",
            perf["total_ms"],
            summed_known,
            unexplained,
        )
    _perf_log_create_alert(perf)
    _perf_step("final_return", started_at, previous_step_at, alert_id=created_alert_ids[0] if created_alert_ids else None, machine_id=created_machine_id, company_id=created_company_id)
    current_app.logger.debug(
        "SERVICE alert_create returning created_alert_ids=%s source_department_id=%s",
        created_alert_ids,
        getattr(source_department, "id", None),
    )
    if len(created_alert_ids) == 1:
        return SimpleNamespace(id=created_alert_ids[0], company_id=created_company_id, machine_id=created_machine_id, status=created_status)
    return [
        SimpleNamespace(id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id, status=alert.status)
        for alert in alerts
    ]


def acknowledge_alert(alert_id: int, payload: dict, metrics: dict | None = None):
    return acknowledge_alert_scoped(alert_id, payload, metrics=metrics, company_id=None, department_id=None)


def acknowledge_alert_scoped(
    alert_id: int,
    payload: dict,
    metrics: dict | None = None,
    *,
    company_id: int | None,
    department_id: int | None = None,
    responder_name_fallback: str | None = None,
    event_message: str | None = None,
    event_metadata: dict | None = None,
):
    perf = metrics if isinstance(metrics, dict) else {}
    started_at = time.perf_counter()
    previous_step_at = started_at
    previous_step_at = _perf_alert_step("ack", "start", started_at, previous_step_at, alert_id=alert_id, company_id=company_id)
    previous_step_at = _perf_alert_step("ack", "before_alert_lookup", started_at, previous_step_at, alert_id=alert_id, company_id=company_id)
    lookup_started_at = time.perf_counter()
    alert = _get_alert_for_mutation(alert_id) if company_id is None else _get_alert_for_mutation_scoped(alert_id, company_id=company_id, department_id=department_id)
    perf["alert_lookup_ms"] = (time.perf_counter() - lookup_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_alert_lookup", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    previous_step_at = _perf_alert_step("ack", "before_company_scope_validation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    previous_step_at = _perf_alert_step("ack", "after_validation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    if alert.status != ALERT_STATUS_OPEN:
        raise AlertServiceError("Alert can only be acknowledged from OPEN state")

    responder_started_at = time.perf_counter()
    responder = _resolve_user(payload.get("responder_user_id"))
    perf["user_lookup_ms"] = (time.perf_counter() - responder_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "before_status_mutation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    update_started_at = time.perf_counter()
    now = utc_now()
    alert.acknowledged_at = now
    alert.acknowledged_seconds = _duration_seconds(alert.created_at, now)
    alert.status = ALERT_STATUS_ACKNOWLEDGED
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    alert.responder_name_text = alert.responder_name_text or payload.get("responder_name_text") or responder_name_fallback
    _replace_alert_note(alert, payload.get("note"))
    perf["update_fields_ms"] = (time.perf_counter() - update_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_status_mutation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)

    previous_step_at = _perf_alert_step("ack", "before_event_note_insert", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    event_started_at = time.perf_counter()
    _add_event(
        alert,
        EVENT_ACKNOWLEDGED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or event_message or "Alert acknowledged",
        metadata=event_metadata or {"responder_name_text": alert.responder_name_text},
    )
    perf["event_insert_ms"] = (time.perf_counter() - event_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_event_note_insert", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    captured_alert_id = alert.id
    captured_company_id = alert.company_id
    captured_machine_id = alert.machine_id
    captured_status = alert.status
    previous_step_at = _perf_alert_step("ack", "before_commit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    commit_started_at = time.perf_counter()
    db.session.commit()
    perf["db_commit_ms"] = (time.perf_counter() - commit_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_commit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    previous_step_at = _perf_alert_step("ack", "before_cache_invalidate", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    cache_started_at = time.perf_counter()
    _invalidate_live_caches(captured_company_id)
    perf["cache_invalidate_ms"] = (time.perf_counter() - cache_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_cache_invalidate", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    previous_step_at = _perf_alert_step("ack", "before_socket_emit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    emit_started_at = time.perf_counter()
    emit_alert_updated(captured_company_id, captured_alert_id, machine_id=captured_machine_id, status=captured_status, action="acknowledged")
    perf["socket_emit_ms"] = (time.perf_counter() - emit_started_at) * 1000
    previous_step_at = _perf_alert_step("ack", "after_socket_emit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    perf["total_ms"] = (time.perf_counter() - started_at) * 1000
    _perf_alert_reconcile("ack", started_at, perf)
    _perf_log_alert_mutation("acknowledge", perf)
    _perf_alert_step("ack", "final_return", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    return SimpleNamespace(id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id, status=captured_status)


def mark_arrived(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    if alert.status != ALERT_STATUS_ACKNOWLEDGED:
        raise AlertServiceError("Alert must be acknowledged before marking arrived")

    responder = _resolve_user(payload.get("responder_user_id"))
    now = utc_now()
    if alert.status != ALERT_STATUS_ARRIVED:
        alert.arrived_at = now
        alert.status = ALERT_STATUS_ARRIVED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    _replace_alert_note(alert, payload.get("note"))

    _add_event(
        alert,
        EVENT_ARRIVED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Responder arrived",
        metadata={"responder_name_text": alert.responder_name_text},
    )
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="arrived")
    return alert


def resolve_alert(alert_id: int, payload: dict, metrics: dict | None = None):
    return resolve_alert_scoped(alert_id, payload, metrics=metrics, company_id=None, department_id=None)


def resolve_alert_scoped(
    alert_id: int,
    payload: dict,
    metrics: dict | None = None,
    *,
    company_id: int | None,
    department_id: int | None = None,
    responder_name_fallback: str | None = None,
    event_message: str | None = None,
    event_metadata: dict | None = None,
):
    perf = metrics if isinstance(metrics, dict) else {}
    started_at = time.perf_counter()
    previous_step_at = started_at
    previous_step_at = _perf_alert_step("resolve", "start", started_at, previous_step_at, alert_id=alert_id, company_id=company_id)
    previous_step_at = _perf_alert_step("resolve", "before_alert_lookup", started_at, previous_step_at, alert_id=alert_id, company_id=company_id)
    lookup_started_at = time.perf_counter()
    alert = _get_alert_for_mutation(alert_id) if company_id is None else _get_alert_for_mutation_scoped(alert_id, company_id=company_id, department_id=department_id)
    perf["alert_lookup_ms"] = (time.perf_counter() - lookup_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_alert_lookup", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    previous_step_at = _perf_alert_step("resolve", "before_company_scope_validation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    previous_step_at = _perf_alert_step("resolve", "after_validation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    if alert.status in [ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED]:
        raise AlertServiceError("Alert is already closed")
    if alert.status not in [ALERT_STATUS_ARRIVED, ALERT_STATUS_ACKNOWLEDGED]:
        raise AlertServiceError("Alert must be acknowledged before resolving")

    responder_started_at = time.perf_counter()
    responder = _resolve_user(payload.get("responder_user_id"))
    perf["user_lookup_ms"] = (time.perf_counter() - responder_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "before_status_mutation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    update_started_at = time.perf_counter()
    now = utc_now()
    alert.resolved_at = now
    if alert.acknowledged_at:
        alert.ack_to_clear_seconds = _duration_seconds(alert.acknowledged_at, now)
    alert.status = ALERT_STATUS_RESOLVED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    alert.responder_name_text = alert.responder_name_text or responder_name_fallback
    alert.resolution_note = payload.get("resolution_note")
    alert.root_cause = payload.get("root_cause")
    alert.corrective_action = payload.get("corrective_action")
    _append_alert_note(alert, payload.get("note"))
    perf["update_fields_ms"] = (time.perf_counter() - update_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_status_mutation", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)

    previous_step_at = _perf_alert_step("resolve", "before_event_note_insert", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    event_started_at = time.perf_counter()
    _add_event(
        alert,
        EVENT_RESOLVED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or event_message or "Alert resolved",
        metadata=event_metadata or {
            "resolution_note": alert.resolution_note,
            "root_cause": alert.root_cause,
            "corrective_action": alert.corrective_action,
        },
    )
    perf["event_insert_ms"] = (time.perf_counter() - event_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_event_note_insert", started_at, previous_step_at, alert_id=alert.id, company_id=alert.company_id, machine_id=alert.machine_id)
    captured_alert_id = alert.id
    captured_company_id = alert.company_id
    captured_machine_id = alert.machine_id
    captured_status = alert.status
    previous_step_at = _perf_alert_step("resolve", "before_commit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    commit_started_at = time.perf_counter()
    db.session.commit()
    perf["db_commit_ms"] = (time.perf_counter() - commit_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_commit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    previous_step_at = _perf_alert_step("resolve", "before_cache_invalidate", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    cache_started_at = time.perf_counter()
    _invalidate_live_caches(captured_company_id)
    perf["cache_invalidate_ms"] = (time.perf_counter() - cache_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_cache_invalidate", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    previous_step_at = _perf_alert_step("resolve", "before_socket_emit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    emit_started_at = time.perf_counter()
    emit_alert_updated(captured_company_id, captured_alert_id, machine_id=captured_machine_id, status=captured_status, action="resolved")
    perf["socket_emit_ms"] = (time.perf_counter() - emit_started_at) * 1000
    previous_step_at = _perf_alert_step("resolve", "after_socket_emit", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    perf["total_ms"] = (time.perf_counter() - started_at) * 1000
    _perf_alert_reconcile("resolve", started_at, perf)
    _perf_log_alert_mutation("resolve", perf)
    _perf_alert_step("resolve", "final_return", started_at, previous_step_at, alert_id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id)
    return SimpleNamespace(id=captured_alert_id, company_id=captured_company_id, machine_id=captured_machine_id, status=captured_status)


def cancel_alert(alert_id: int, payload: dict, metrics: dict | None = None):
    perf = metrics if isinstance(metrics, dict) else {}
    started_at = time.perf_counter()
    lookup_started_at = time.perf_counter()
    alert = _get_alert_for_mutation(alert_id)
    perf["alert_lookup_ms"] = (time.perf_counter() - lookup_started_at) * 1000
    if alert.status not in [ALERT_STATUS_OPEN, ALERT_STATUS_ACKNOWLEDGED, ALERT_STATUS_ARRIVED]:
        raise AlertServiceError("Alert can only be cancelled before arrival or resolution")
    responder_started_at = time.perf_counter()
    responder = _resolve_user(payload.get("responder_user_id"))
    perf["user_lookup_ms"] = (time.perf_counter() - responder_started_at) * 1000
    update_started_at = time.perf_counter()
    now = utc_now()
    alert.cancelled_at = now
    if alert.acknowledged_at:
        alert.ack_to_clear_seconds = _duration_seconds(alert.acknowledged_at, now)
    alert.status = ALERT_STATUS_CANCELLED
    if responder:
        alert.responder_user_id = responder.id
        alert.responder_name_text = responder.display_name
    if payload.get("responder_name_text"):
        alert.responder_name_text = payload.get("responder_name_text")
    _append_alert_note(alert, payload.get("note"))
    perf["update_fields_ms"] = (time.perf_counter() - update_started_at) * 1000

    event_started_at = time.perf_counter()
    _add_event(
        alert,
        EVENT_CANCELLED,
        user=responder,
        user_name_text=alert.responder_name_text,
        message=payload.get("message") or "Alert cancelled",
        metadata={"reason": payload.get("reason")},
    )
    perf["event_insert_ms"] = (time.perf_counter() - event_started_at) * 1000
    commit_started_at = time.perf_counter()
    db.session.commit()
    perf["db_commit_ms"] = (time.perf_counter() - commit_started_at) * 1000
    cache_started_at = time.perf_counter()
    _invalidate_live_caches(alert.company_id)
    perf["cache_invalidate_ms"] = (time.perf_counter() - cache_started_at) * 1000
    emit_started_at = time.perf_counter()
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="cancelled")
    perf["socket_emit_ms"] = (time.perf_counter() - emit_started_at) * 1000
    perf["total_ms"] = (time.perf_counter() - started_at) * 1000
    _perf_log_alert_mutation("cancel", perf)
    return alert


def add_note(alert_id: int, payload: dict):
    alert = get_alert(alert_id)
    _add_event(
        alert,
        EVENT_NOTE_ADDED,
        user=_resolve_user(payload.get("user_id")),
        user_name_text=payload.get("user_name_text"),
        message=payload.get("message"),
        metadata=payload.get("metadata") or {},
    )
    if payload.get("note"):
        _append_alert_note(alert, payload.get("note"))
    db.session.commit()
    _invalidate_live_caches(alert.company_id)
    emit_alert_updated(alert.company_id, alert.id, machine_id=alert.machine_id, status=alert.status, action="note_added")
    return alert


def _resolve_user(user_id):
    if not user_id:
        return None
    company_id = get_current_company_id()
    query = User.query.options(
        load_only(User.id, User.display_name, User.is_active),
        noload("*"),
    ).filter(User.id == user_id)
    user = query.one_or_none()
    if user is None:
        return None
    if company_id:
        access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id, is_active=True).one_or_none()
        if access is None:
            return None
    return user


def _add_event(alert, event_type, user=None, user_name_text=None, message=None, metadata=None):
    event = AndonAlertEvent(
        company_id=alert.company_id,
        alert=alert,
        event_type=event_type,
        user=user,
        user_name_text=user_name_text or (user.display_name if user else None),
        message=message,
        metadata_json=metadata or {},
    )
    db.session.add(event)


def _append_alert_note(alert, note_text):
    note = str(note_text or "").strip()
    if not note:
        return
    if alert.note:
        alert.note = f"{alert.note}\n{note}".strip()
    else:
        alert.note = note


def _replace_alert_note(alert, note_text):
    note = str(note_text or "").strip()
    if not note:
        return
    alert.note = note


def get_active_alert_metrics():
    alerts = list_active_alerts(status="active")
    grouped = defaultdict(int)
    for alert in alerts:
        status = alert.get("status") if isinstance(alert, dict) else getattr(alert, "status", None)
        grouped[status] += 1
    return grouped


def _invalidate_live_caches(company_id):
    # Keep invalidation scoped to live alert views only.
    invalidate_live_alert_caches(company_id)


def _get_active_alert_for_machine(machine_id, company_id):
    query = AndonAlert.query.filter(
        AndonAlert.machine_id == machine_id,
        AndonAlert.status.notin_([ALERT_STATUS_RESOLVED, ALERT_STATUS_CANCELLED]),
    )
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    return query.options(*_alert_response_options()).order_by(AndonAlert.created_at.desc()).first()


def _get_latest_alert_for_machine(machine_id, company_id):
    query = AndonAlert.query.filter(AndonAlert.machine_id == machine_id)
    if company_id:
        query = query.filter(AndonAlert.company_id == company_id)
    return query.options(*_alert_response_options()).order_by(AndonAlert.created_at.desc()).first()


def _alert_response_options():
    return (
        joinedload(AndonAlert.machine)
        .load_only(
            Machine.id,
            Machine.company_id,
            Machine.machine_code,
            Machine.name,
            Machine.machine_type,
            Machine.radius_machine_id,
            Machine.area,
            Machine.line,
            Machine.department_id,
            Machine.description,
            Machine.is_active,
            Machine.created_at,
        )
        .joinedload(Machine.department)
        .load_only(Department.id, Department.name),
        joinedload(AndonAlert.department).load_only(Department.id, Department.name),
        joinedload(AndonAlert.issue_category)
        .load_only(
            IssueCategory.id,
            IssueCategory.company_id,
            IssueCategory.name,
            IssueCategory.department_id,
            IssueCategory.color,
            IssueCategory.priority_default,
            IssueCategory.is_active,
            IssueCategory.created_at,
        )
        .joinedload(IssueCategory.department)
        .load_only(Department.id, Department.name),
        joinedload(AndonAlert.issue_problem)
        .load_only(
            IssueProblem.id,
            IssueProblem.company_id,
            IssueProblem.category_id,
            IssueProblem.name,
            IssueProblem.description,
            IssueProblem.severity_default,
            IssueProblem.is_active,
            IssueProblem.created_at,
        )
        .joinedload(IssueProblem.category)
        .load_only(IssueCategory.id, IssueCategory.name, IssueCategory.department_id),
        noload(AndonAlert.company),
        noload(AndonAlert.operator_user),
        noload(AndonAlert.responder_user),
        noload(AndonAlert.events),
        noload(AndonAlert.escalations),
    )
