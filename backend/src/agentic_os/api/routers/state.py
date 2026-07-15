from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.deps import get_session
from agentic_os.domain.models import Artifact, AuditEvent, CostLedgerEntry, Goal, Run, Task

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


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    goal_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    name: str
    created_at: datetime


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
def list_tasks(goal_id: uuid.UUID, session: Session = Depends(get_session)) -> list[Task]:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return list(session.execute(select(Task).where(Task.goal_id == goal_id).order_by(Task.created_at)).scalars())


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/tasks/{task_id}/runs", response_model=list[RunRead])
def list_runs(task_id: uuid.UUID, session: Session = Depends(get_session)) -> list[Run]:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return list(
        session.execute(select(Run).where(Run.task_id == task_id).order_by(Run.attempt_number)).scalars()
    )


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: uuid.UUID, session: Session = Depends(get_session)) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/projects/{project_id}/artifacts", response_model=list[ArtifactRead])
def list_artifacts(
    project_id: uuid.UUID,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
) -> list[Artifact]:
    stmt = select(Artifact).where(Artifact.project_id == project_id)
    if goal_id is not None:
        stmt = stmt.where(Artifact.goal_id == goal_id)
    if task_id is not None:
        stmt = stmt.where(Artifact.task_id == task_id)
    if run_id is not None:
        stmt = stmt.where(Artifact.run_id == run_id)
    return list(session.execute(stmt.order_by(Artifact.created_at)).scalars())


@router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
def get_artifact(artifact_id: uuid.UUID, session: Session = Depends(get_session)) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return artifact


@router.get("/audit-events", response_model=list[AuditEventRead])
def list_audit_events(
    project_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if project_id is not None:
        stmt = stmt.where(AuditEvent.project_id == project_id)
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
) -> list[CostLedgerEntry]:
    stmt = select(CostLedgerEntry)
    if run_id is not None:
        stmt = stmt.where(CostLedgerEntry.run_id == run_id)
    if budget_id is not None:
        stmt = stmt.where(CostLedgerEntry.budget_id == budget_id)
    stmt = stmt.order_by(CostLedgerEntry.created_at).limit(limit)
    return list(session.execute(stmt).scalars())
