from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import (
    Goal,
    ObservabilityRecord,
    Project,
    ProjectMember,
    Run,
    Task,
    TeamMembership,
    TelemetryExportAttempt,
    TelemetryExportSetting,
    User,
)
from agentic_os.sandbox.availability import runtime_available

router = APIRouter(tags=["observability"])


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


def current_actor(
    session: Session = Depends(get_session),
    x_agentic_user_id: Annotated[uuid.UUID | None, Header()] = None,
) -> User:
    if x_agentic_user_id is None:
        return ensure_default_user(session)
    actor = session.get(User, x_agentic_user_id)
    if actor is None:
        raise HTTPException(status_code=401, detail="unknown actor")
    return actor


def _require_admin(actor: User) -> None:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


def _require_project_access(session: Session, actor: User, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if project.team_id != ensure_default_team(session).id:
        raise HTTPException(status_code=403, detail="project belongs to another team")
    if actor.role == "admin" or project.created_by == actor.id:
        return project
    team_member = session.execute(
        select(TeamMembership.id).where(
            TeamMembership.team_id == project.team_id,
            TeamMembership.user_id == actor.id,
        )
    ).scalar_one_or_none()
    project_member = session.execute(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == actor.id,
        )
    ).scalar_one_or_none()
    if team_member is None or project_member is None:
        raise HTTPException(status_code=403, detail="project access required")
    return project


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
        _require_admin(actor)
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
    _require_admin(actor)
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
    _require_admin(actor)
    session.execute(text("SELECT 1"))
    now = datetime.now(timezone.utc)
    queue_counts = dict(
        session.execute(select(Task.status, func.count(Task.id)).group_by(Task.status)).all()
    )
    active_workers = session.execute(
        select(func.count(func.distinct(Task.lease_owner))).where(
            Task.lease_owner.is_not(None), Task.lease_expires_at > now
        )
    ).scalar_one()
    latest_record_at = session.execute(
        select(func.max(ObservabilityRecord.occurred_at))
    ).scalar_one()
    delivery_counts = dict(
        session.execute(
            select(TelemetryExportAttempt.status, func.count(TelemetryExportAttempt.id)).group_by(
                TelemetryExportAttempt.status
            )
        ).all()
    )
    exporters = session.execute(
        select(TelemetryExportSetting).where(
            (TelemetryExportSetting.team_id == ensure_default_team(session).id)
            | TelemetryExportSetting.team_id.is_(None)
        ).order_by(TelemetryExportSetting.created_at)
    ).scalars()
    sandbox = {}
    for runtime in ("docker", "podman"):
        available, reason = runtime_available(runtime)
        sandbox[runtime] = {"status": "available" if available else "unavailable", "reason": reason}
    return {
        "database": {"status": "ok"},
        "queues": {"status": "ok", "tasks_by_status": queue_counts},
        "workers": {"status": "ok" if active_workers else "idle", "active": active_workers},
        "sandbox": sandbox,
        "event_stream": {"status": "ok", "latest_record_at": latest_record_at},
        "telemetry": {
            "status": "degraded" if delivery_counts.get("failed", 0) else "ok",
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
