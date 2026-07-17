from __future__ import annotations

import uuid
from datetime import datetime, timezone
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_admin, require_project_access
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import (
    AuditEvent,
    Goal,
    ObservabilityRecord,
    Project,
    Run,
    Task,
    TelemetryExportAttempt,
    TelemetryExportSetting,
    User,
)
from agentic_os.health import deployment_health
from agentic_os.sandbox.availability import runtime_available
from agentic_os.worker.scheduler import summarize_worker_heartbeats

router = APIRouter(tags=["observability"])

EVENT_STREAM_DELAY_SECONDS = 120


class TelemetryAttemptRead(BaseModel):
    id: uuid.UUID
    observability_record_id: uuid.UUID
    destination: str
    attempt_number: int
    status: str
    last_attempted_at: datetime | None
    delivered_at: datetime | None
    retry_after: datetime | None
    failure_code: str | None
    failure_message: str | None
    delivery_evidence: dict
    created_at: datetime


class ObservabilityRecordRead(BaseModel):
    id: uuid.UUID
    correlation_id: uuid.UUID
    request_id: uuid.UUID | None
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    event_kind: str
    operation_name: str
    status: str | None
    occurred_at: datetime
    team_id: uuid.UUID | None
    user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    audit_event_id: uuid.UUID | None
    cost_ledger_entry_id: uuid.UUID | None
    approval_request_id: uuid.UUID | None
    approval_decision_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    artifact_version_id: uuid.UUID | None
    model_call_id: uuid.UUID | None
    tool_call_id: uuid.UUID | None
    mcp_call_id: uuid.UUID | None
    sandbox_id: uuid.UUID | None
    checkpoint_id: uuid.UUID | None
    attributes: dict
    capture_policy_evidence: dict
    redaction_evidence: dict
    telemetry_attempts: list[TelemetryAttemptRead]


def _require_admin(session: Session, actor: User) -> None:
    require_admin(session, actor, action="observability.admin")


def _require_project_access(session: Session, actor: User, project_id: uuid.UUID) -> Project:
    return require_project_access(session, actor, project_id, action="observability.access")


def _project_id_for_record(session: Session, record: ObservabilityRecord) -> uuid.UUID | None:
    if record.project_id is not None:
        return record.project_id
    if record.goal_id is not None:
        goal = session.get(Goal, record.goal_id)
        return goal.project_id if goal else None
    if record.task_id is not None:
        task = session.get(Task, record.task_id)
        goal = session.get(Goal, task.goal_id) if task else None
        return goal.project_id if goal else None
    if record.run_id is not None:
        run = session.get(Run, record.run_id)
        task = session.get(Task, run.task_id) if run else None
        goal = session.get(Goal, task.goal_id) if task else None
        return goal.project_id if goal else None
    return None


def _safe_attempt(attempt: TelemetryExportAttempt) -> dict:
    return {
        "id": attempt.id,
        "observability_record_id": attempt.observability_record_id,
        "destination": attempt.destination,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "last_attempted_at": attempt.last_attempted_at,
        "delivered_at": attempt.delivered_at,
        "retry_after": attempt.retry_after,
        "failure_code": attempt.failure_code,
        "failure_message": "[REDACTED]" if attempt.failure_message else None,
        "delivery_evidence": redact_mapping(attempt.delivery_evidence or {}),
        "created_at": attempt.created_at,
    }


def _safe_record(session: Session, record: ObservabilityRecord) -> dict:
    attempts = session.execute(
        select(TelemetryExportAttempt)
        .where(TelemetryExportAttempt.observability_record_id == record.id)
        .order_by(TelemetryExportAttempt.attempt_number)
    ).scalars()
    return {
        "id": record.id,
        "correlation_id": record.correlation_id,
        "request_id": record.request_id,
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "parent_span_id": record.parent_span_id,
        "event_kind": record.event_kind,
        "operation_name": record.operation_name,
        "status": record.status,
        "occurred_at": record.occurred_at,
        "team_id": record.team_id,
        "user_id": record.user_id,
        "project_id": record.project_id,
        "goal_id": record.goal_id,
        "task_id": record.task_id,
        "run_id": record.run_id,
        "audit_event_id": record.audit_event_id,
        "cost_ledger_entry_id": record.cost_ledger_entry_id,
        "approval_request_id": record.approval_request_id,
        "approval_decision_id": record.approval_decision_id,
        "artifact_id": record.artifact_id,
        "artifact_version_id": record.artifact_version_id,
        "model_call_id": record.model_call_id,
        "tool_call_id": record.tool_call_id,
        "mcp_call_id": record.mcp_call_id,
        "sandbox_id": record.sandbox_id,
        "checkpoint_id": record.checkpoint_id,
        "attributes": redact_mapping(record.attributes or {}),
        "capture_policy_evidence": redact_mapping(record.capture_policy_evidence or {}),
        "redaction_evidence": redact_mapping(record.redaction_evidence or {}),
        "telemetry_attempts": [_safe_attempt(attempt) for attempt in attempts],
    }


def _timeline(
    session: Session,
    actor: User,
    *,
    project_id: uuid.UUID,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: int,
) -> list[dict]:
    _require_project_access(session, actor, project_id)
    stmt = select(ObservabilityRecord).where(ObservabilityRecord.project_id == project_id)
    if goal_id is not None:
        stmt = stmt.where(ObservabilityRecord.goal_id == goal_id)
    if task_id is not None:
        stmt = stmt.where(ObservabilityRecord.task_id == task_id)
    if run_id is not None:
        stmt = stmt.where(ObservabilityRecord.run_id == run_id)
    records = session.execute(
        stmt.order_by(ObservabilityRecord.occurred_at, ObservabilityRecord.id).limit(limit)
    ).scalars()
    return [_safe_record(session, record) for record in records]


@router.get(
    "/projects/{project_id}/observability-records",
    response_model=list[ObservabilityRecordRead],
)
def list_project_observability(
    project_id: uuid.UUID,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    return _timeline(
        session,
        actor,
        project_id=project_id,
        goal_id=goal_id,
        task_id=task_id,
        run_id=run_id,
        limit=limit,
    )


@router.get("/goals/{goal_id}/observability-timeline", response_model=list[ObservabilityRecordRead])
def goal_timeline(
    goal_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return _timeline(session, actor, project_id=goal.project_id, goal_id=goal.id, limit=limit)


@router.get("/tasks/{task_id}/observability-timeline", response_model=list[ObservabilityRecordRead])
def task_timeline(
    task_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    task = session.get(Task, task_id)
    goal = session.get(Goal, task.goal_id) if task else None
    if task is None or goal is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _timeline(session, actor, project_id=goal.project_id, task_id=task.id, limit=limit)


@router.get("/runs/{run_id}/observability-timeline", response_model=list[ObservabilityRecordRead])
def run_timeline(
    run_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    run = session.get(Run, run_id)
    task = session.get(Task, run.task_id) if run else None
    goal = session.get(Goal, task.goal_id) if task else None
    if run is None or task is None or goal is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _timeline(session, actor, project_id=goal.project_id, run_id=run.id, limit=limit)


@router.get("/observability-records/{record_id}", response_model=ObservabilityRecordRead)
def get_observability_record(
    record_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict:
    record = session.get(ObservabilityRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="observability record not found")
    project_id = _project_id_for_record(session, record)
    if project_id is None:
        _require_admin(session, actor)
    else:
        _require_project_access(session, actor, project_id)
    return _safe_record(session, record)


@router.get("/admin/telemetry-export-attempts", response_model=list[TelemetryAttemptRead])
def list_telemetry_attempts(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    _require_admin(session, actor)
    stmt = select(TelemetryExportAttempt)
    if status is not None:
        stmt = stmt.where(TelemetryExportAttempt.status == status)
    attempts = session.execute(
        stmt.order_by(TelemetryExportAttempt.created_at.desc()).limit(limit)
    ).scalars()
    return [_safe_attempt(attempt) for attempt in attempts]


@router.get("/admin/observability/health")
def observability_health(
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict:
    _require_admin(session, actor)
    deployment = deployment_health(session.get_bind())
    database_check_started = perf_counter()
    session.execute(text("SELECT 1"))
    database_latency_ms = round((perf_counter() - database_check_started) * 1000, 3)
    now = datetime.now(timezone.utc)
    queue_counts = dict(
        session.execute(select(Task.status, func.count(Task.id)).group_by(Task.status)).all()
    )
    queue_depth = sum(queue_counts.get(status, 0) for status in ("pending", "ready"))
    active_workers = session.execute(
        select(func.count(func.distinct(Task.lease_owner))).where(
            Task.lease_owner.is_not(None), Task.lease_expires_at > now
        )
    ).scalar_one()
    active_lease_count = session.execute(
        select(func.count(Task.id)).where(
            Task.lease_owner.is_not(None), Task.lease_expires_at > now
        )
    ).scalar_one()
    stale_worker_rows = list(
        session.execute(
            select(Task.id, Task.lease_owner, Task.lease_expires_at).where(
                Task.lease_owner.is_not(None),
                (Task.lease_expires_at.is_(None)) | (Task.lease_expires_at <= now),
            )
        ).all()
    )
    stale_worker_ids = sorted({row.lease_owner for row in stale_worker_rows if row.lease_owner})
    heartbeat_summary = summarize_worker_heartbeats(session, now=now)
    retry_count = session.execute(
        select(func.count(Run.id)).where(Run.attempt_number > 1)
    ).scalar_one()
    failure_count = session.execute(
        select(func.count(Run.id)).where(Run.status == "failed")
    ).scalar_one()
    latest_record = session.execute(
        select(ObservabilityRecord)
        .order_by(ObservabilityRecord.occurred_at.desc(), ObservabilityRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    latest_record_at = latest_record.occurred_at if latest_record else None
    latest_record_age_seconds = (
        max(0.0, (now - latest_record_at).total_seconds()) if latest_record_at else None
    )
    delivery_counts = dict(
        session.execute(
            select(TelemetryExportAttempt.status, func.count(TelemetryExportAttempt.id)).group_by(
                TelemetryExportAttempt.status
            )
        ).all()
    )
    oldest_queued_delivery_at = session.execute(
        select(func.min(TelemetryExportAttempt.created_at)).where(
            TelemetryExportAttempt.status.in_(("pending", "delayed"))
        )
    ).scalar_one()
    delivery_delay_seconds = (
        max(0.0, (now - oldest_queued_delivery_at).total_seconds())
        if oldest_queued_delivery_at
        else None
    )
    exporters = session.execute(
        select(TelemetryExportSetting).order_by(TelemetryExportSetting.created_at)
    ).scalars()
    maintenance_events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type.like("operations.%"))
            .order_by(AuditEvent.sequence_number.desc())
            .limit(10)
        ).scalars()
    )
    sandbox = {}
    for runtime in ("docker", "podman"):
        available, reason = runtime_available(runtime)
        sandbox[runtime] = {"status": "available" if available else "unavailable", "reason": reason}
    available_sandbox_count = sum(
        runtime["status"] == "available" for runtime in sandbox.values()
    )
    sandbox_status = (
        "healthy"
        if available_sandbox_count == len(sandbox)
        else "degraded"
        if available_sandbox_count
        else "unavailable"
    )
    live_heartbeat_present = bool(heartbeat_summary.live_worker_ids)
    workers_status = (
        # A worker that heartbeated recently is either reclaiming the stale
        # lease itself (having outlived a crashed sibling) or will pick it up
        # on its next claim attempt, so this is recovery-in-progress rather
        # than a stuck fleet with nobody left to reconcile the expired lease.
        "recovering"
        if stale_worker_ids and live_heartbeat_present
        else "stale"
        if stale_worker_ids
        # No worker has claimed the backlog and none is even polling for
        # work: nothing will process it without operator intervention.
        else "unavailable"
        if queue_depth and not active_workers and not live_heartbeat_present
        # Either a known worker id fell out of the live window without
        # releasing its leases cleanly, or a live worker is polling but
        # hasn't claimed the current backlog yet: reduced/delayed capacity
        # rather than a full outage in either case.
        else "degraded"
        if heartbeat_summary.missing_worker_ids or (queue_depth and not active_workers)
        else "healthy"
    )
    event_stream_status = (
        "unavailable"
        if latest_record_at is None
        else "delayed"
        if latest_record_age_seconds is not None
        and latest_record_age_seconds > EVENT_STREAM_DELAY_SECONDS
        and (queue_depth or active_workers or stale_worker_rows)
        else "healthy"
    )
    telemetry_status = (
        "degraded"
        if delivery_counts.get("failed", 0) or delivery_counts.get("dropped", 0)
        else "delayed"
        if delivery_counts.get("delayed", 0)
        or (
            delivery_delay_seconds is not None
            and delivery_delay_seconds > EVENT_STREAM_DELAY_SECONDS
        )
        else "healthy"
    )
    component_statuses = (
        deployment["status"],
        "healthy",
        "degraded" if queue_counts.get("failed", 0) else "healthy",
        workers_status,
        sandbox_status,
        event_stream_status,
        telemetry_status,
    )
    overall_status = next(
        (
            status
            for status in ("unavailable", "stale", "recovering", "degraded", "delayed")
            if status in component_statuses
        ),
        "healthy",
    )
    return {
        "status": overall_status,
        "checked_at": now,
        "deployment": deployment,
        "maintenance": {
            "events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "occurred_at": event.occurred_at,
                    "evidence": redact_mapping(event.payload or {}),
                }
                for event in maintenance_events
            ],
            "commands": {
                "setup_check": "./agentic-os operations setup-check",
                "migration_status": "./agentic-os operations migrations status",
                "backup": "./agentic-os operations backup --output <backup.tar.gz>",
                "restore": "./agentic-os operations restore <backup.tar.gz> --target-database-url <url> --target-artifact-root <path>",
                "upgrade_preflight": "./agentic-os operations upgrade-preflight",
            },
        },
        "database": {"status": "healthy", "latency_ms": database_latency_ms},
        "queues": {
            "status": "degraded" if queue_counts.get("failed", 0) else "healthy",
            "depth": queue_depth,
            "tasks_by_status": queue_counts,
        },
        "workers": {
            "status": workers_status,
            "active": active_workers,
            "stale": len(stale_worker_ids),
            "stale_worker_ids": stale_worker_ids,
            "stale_task_ids": [row.id for row in stale_worker_rows],
            "lease_count": active_lease_count + len(stale_worker_rows),
            "retry_count": retry_count,
            "failure_count": failure_count,
            "capacity": heartbeat_summary.configured_capacity,
            "live_worker_ids": heartbeat_summary.live_worker_ids,
            "missing_worker_ids": heartbeat_summary.missing_worker_ids,
        },
        "sandbox": {"status": sandbox_status, "runtimes": sandbox},
        "event_stream": {
            "status": event_stream_status,
            "latest_record_at": latest_record_at,
            "latest_record_age_seconds": latest_record_age_seconds,
            "latest_correlation_id": latest_record.correlation_id if latest_record else None,
            "deliveries_by_status": delivery_counts,
            "oldest_queued_delivery_at": oldest_queued_delivery_at,
            "delivery_delay_seconds": delivery_delay_seconds,
        },
        "telemetry": {
            "status": telemetry_status,
            "deliveries_by_status": delivery_counts,
            "exporters": [
                {
                    "id": setting.id,
                    "exporter_type": setting.exporter_type,
                    "enabled": setting.enabled,
                    "configured": bool(setting.endpoint_reference),
                    "capture_prompts": setting.capture_prompts,
                    "capture_outputs": setting.capture_outputs,
                    "redaction_policy_evidence": redact_mapping(
                        setting.redaction_policy_evidence or {}
                    ),
                }
                for setting in exporters
            ],
        },
    }
