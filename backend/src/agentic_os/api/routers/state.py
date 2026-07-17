from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.api.authorization import current_actor, require_project_access, require_resource_access
from agentic_os.domain.models import AuditEvent, CostLedgerEntry, Goal, Run, Task, User

router = APIRouter(tags=["state"])


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    title: str
    description: str | None
    status: str
    required_capabilities: dict
    capability_rationale: dict
    expected_outputs: list
    resource_intent: list
    policy_ids: list
    budget_id: uuid.UUID | None
    assigned_agent_version_id: uuid.UUID | None
    assignment_status: str
    assignment_candidates: list
    assignment_rationale: dict
    assignment_updated_at: datetime | None
    lease_owner: str | None
    lease_token: int
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    attempt_number: int
    idempotency_key: str
    agent_version_id: uuid.UUID
    langgraph_thread_id: str | None
    status: str
    snapshot: dict
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence_number: int
    project_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    event_type: str
    payload: dict
    occurred_at: datetime


class CostLedgerEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    budget_id: uuid.UUID | None
    run_id: uuid.UUID | None
    action_type: str
    reserved_amount_minor_units: int
    actual_amount_minor_units: int | None
    currency: str
    is_zero_cost: bool
    status: str
    created_at: datetime


@router.get("/goals/{goal_id}/tasks", response_model=list[TaskRead])
def list_tasks(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[Task]:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    require_resource_access(session, actor, goal, action="task.list", resource_type="goal")
    return list(session.execute(select(Task).where(Task.goal_id == goal_id).order_by(Task.created_at)).scalars())


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    require_resource_access(session, actor, task, action="task.read", resource_type="task")
    return task


@router.get("/tasks/{task_id}/runs", response_model=list[RunRead])
def list_runs(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[Run]:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    require_resource_access(session, actor, task, action="run.list", resource_type="task")
    return list(
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).scalars()
    )


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    require_resource_access(session, actor, run, action="run.read", resource_type="run")
    return run


@router.get("/audit-events", response_model=list[AuditEventRead])
def list_audit_events(
    project_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[AuditEvent]:
    resolved_project_id = project_id
    if run_id is not None:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        resolved_project_id = require_resource_access(
            session, actor, run, action="audit.list", resource_type="run"
        ).id
    elif task_id is not None:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        resolved_project_id = require_resource_access(
            session, actor, task, action="audit.list", resource_type="task"
        ).id
    elif goal_id is not None:
        goal = session.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="goal not found")
        resolved_project_id = require_resource_access(
            session, actor, goal, action="audit.list", resource_type="goal"
        ).id
    elif project_id is not None:
        require_project_access(session, actor, project_id, action="audit.list")
    elif actor.role != "admin":
        raise HTTPException(
            status_code=422,
            detail="a project, goal, task, or run scope is required for regular users",
        )
    if project_id is not None and resolved_project_id != project_id:
        raise HTTPException(status_code=404, detail="resource not found in project")
    stmt = select(AuditEvent)
    if resolved_project_id is not None:
        stmt = stmt.where(AuditEvent.project_id == resolved_project_id)
    if goal_id is not None:
        stmt = stmt.where(AuditEvent.goal_id == goal_id)
    if task_id is not None:
        stmt = stmt.where(AuditEvent.task_id == task_id)
    if run_id is not None:
        stmt = stmt.where(AuditEvent.run_id == run_id)
    stmt = stmt.order_by(AuditEvent.sequence_number).limit(limit)
    return list(session.execute(stmt).scalars())


@router.get("/cost-ledger-entries", response_model=list[CostLedgerEntryRead])
def list_cost_ledger_entries(
    run_id: uuid.UUID | None = None,
    budget_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[CostLedgerEntry]:
    if run_id is not None:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        require_resource_access(session, actor, run, action="cost_ledger.list", resource_type="run")
    elif actor.role != "admin":
        raise HTTPException(status_code=422, detail="run_id is required for regular users")
    stmt = select(CostLedgerEntry)
    if run_id is not None:
        stmt = stmt.where(CostLedgerEntry.run_id == run_id)
    if budget_id is not None:
        stmt = stmt.where(CostLedgerEntry.budget_id == budget_id)
    stmt = stmt.order_by(CostLedgerEntry.created_at).limit(limit)
    return list(session.execute(stmt).scalars())
