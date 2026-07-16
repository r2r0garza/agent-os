from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import (
    AdminOverride,
    ApprovalDecisionRecord,
    ApprovalModeConfiguration,
    ApprovalRequest,
    AuditEvent,
    BudgetReservation,
    CostLedgerEntry,
    Goal,
    Project,
    ProjectMember,
    Run,
    Task,
    TeamMembership,
    User,
)

router = APIRouter(tags=["governance"])


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
            TeamMembership.team_id == project.team_id, TeamMembership.user_id == actor.id
        )
    ).scalar_one_or_none()
    project_member = session.execute(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == actor.id
        )
    ).scalar_one_or_none()
    if team_member is None or project_member is None:
        raise HTTPException(status_code=403, detail="project access required")
    return project


def _require_admin(actor: User) -> None:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


class ApprovalModeWrite(BaseModel):
    mode: Literal["auto", "consequential", "every_tool_call"]
    consequential_action_types: list[str] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_actions(self) -> ApprovalModeWrite:
        if self.mode == "consequential" and not self.consequential_action_types:
            raise ValueError("consequential mode requires at least one action type")
        if any(not item.strip() for item in self.consequential_action_types):
            raise ValueError("action types must be non-empty")
        return self


class ApprovalModeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID
    project_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    configured_by: uuid.UUID
    version_number: int
    mode: str
    consequential_action_types: list
    context: dict
    created_at: datetime


class ApprovalDecisionWrite(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    context: dict = Field(default_factory=dict)


class ApprovalDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    approval_request_id: uuid.UUID
    decision: str
    actor_id: uuid.UUID | None
    reason: str | None
    context: dict
    evaluated_policy_version_ids: list
    created_at: datetime


class ApprovalRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID
    project_id: uuid.UUID
    goal_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    agent_version_id: uuid.UUID
    configuration_id: uuid.UUID | None
    requested_by: uuid.UUID | None
    mode: str
    status: str
    action_type: str
    action_preview: dict
    policy_version_ids: list
    policy_evidence: dict
    expires_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OverrideWrite(BaseModel):
    scope_type: Literal["project", "goal", "task", "run"]
    scope_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)
    starts_at: datetime | None = None
    expires_at: datetime
    evaluated_policy_version_ids: list[uuid.UUID] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_window(self) -> OverrideWrite:
        if self.expires_at.tzinfo is None or (
            self.starts_at is not None and self.starts_at.tzinfo is None
        ):
            raise ValueError("override timestamps must include a timezone")
        return self


class OverrideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID
    project_id: uuid.UUID | None
    goal_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_by: uuid.UUID
    scope_type: str
    scope_id: uuid.UUID
    reason: str
    starts_at: datetime
    expires_at: datetime
    evaluated_policy_version_ids: list
    context: dict
    created_at: datetime


def _redacted_mode(configuration: ApprovalModeConfiguration) -> ApprovalModeRead:
    result = ApprovalModeRead.model_validate(configuration)
    return result.model_copy(update={"context": redact_mapping(result.context)})


@router.post("/projects/{project_id}/approval-mode-configurations", response_model=ApprovalModeRead, status_code=201)
def configure_project_approval_mode(
    project_id: uuid.UUID,
    payload: ApprovalModeWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalModeRead:
    project = _require_project_access(session, actor, project_id)
    version = session.execute(
        select(func.coalesce(func.max(ApprovalModeConfiguration.version_number), 0)).where(
            ApprovalModeConfiguration.team_id == project.team_id,
            ApprovalModeConfiguration.project_id == project.id,
            ApprovalModeConfiguration.goal_id.is_(None),
        )
    ).scalar_one()
    configuration = ApprovalModeConfiguration(
        team_id=project.team_id, project_id=project.id, configured_by=actor.id,
        version_number=version + 1, mode=payload.mode,
        consequential_action_types=payload.consequential_action_types, context=payload.context,
    )
    session.add(configuration)
    session.flush()
    session.refresh(configuration)
    return _redacted_mode(configuration)


@router.get("/projects/{project_id}/approval-mode-configurations", response_model=list[ApprovalModeRead])
def list_project_approval_modes(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ApprovalModeRead]:
    _require_project_access(session, actor, project_id)
    values = session.execute(
        select(ApprovalModeConfiguration).where(
            ApprovalModeConfiguration.project_id == project_id,
            ApprovalModeConfiguration.goal_id.is_(None),
        ).order_by(ApprovalModeConfiguration.version_number)
    ).scalars()
    return [_redacted_mode(value) for value in values]


@router.get("/approval-mode-configurations/{configuration_id}", response_model=ApprovalModeRead)
def get_approval_mode(
    configuration_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalModeRead:
    configuration = session.get(ApprovalModeConfiguration, configuration_id)
    if configuration is None:
        raise HTTPException(status_code=404, detail="approval mode configuration not found")
    if configuration.project_id is None:
        _require_admin(actor)
    else:
        _require_project_access(session, actor, configuration.project_id)
    return _redacted_mode(configuration)


@router.post("/goals/{goal_id}/approval-mode-configurations", response_model=ApprovalModeRead, status_code=201)
def configure_goal_approval_mode(
    goal_id: uuid.UUID,
    payload: ApprovalModeWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalModeRead:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    project = _require_project_access(session, actor, goal.project_id)
    version = session.execute(
        select(func.coalesce(func.max(ApprovalModeConfiguration.version_number), 0)).where(
            ApprovalModeConfiguration.goal_id == goal.id
        )
    ).scalar_one()
    configuration = ApprovalModeConfiguration(
        team_id=project.team_id, project_id=project.id, goal_id=goal.id,
        configured_by=actor.id, version_number=version + 1, mode=payload.mode,
        consequential_action_types=payload.consequential_action_types, context=payload.context,
    )
    session.add(configuration)
    session.flush()
    session.refresh(configuration)
    return _redacted_mode(configuration)


@router.get("/goals/{goal_id}/approval-mode-configurations", response_model=list[ApprovalModeRead])
def list_goal_approval_modes(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ApprovalModeRead]:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    _require_project_access(session, actor, goal.project_id)
    values = session.execute(
        select(ApprovalModeConfiguration).where(
            ApprovalModeConfiguration.goal_id == goal_id
        ).order_by(ApprovalModeConfiguration.version_number)
    ).scalars()
    return [_redacted_mode(value) for value in values]


@router.get("/approval-requests", response_model=list[ApprovalRequestRead])
def list_approval_requests(
    project_id: uuid.UUID | None = None,
    status: Literal["pending", "approved", "denied", "expired", "cancelled"] | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ApprovalRequestRead]:
    if actor.role != "admin" and project_id is None:
        raise HTTPException(status_code=422, detail="project_id is required for regular users")
    stmt = select(ApprovalRequest)
    if project_id is not None:
        _require_project_access(session, actor, project_id)
        stmt = stmt.where(ApprovalRequest.project_id == project_id)
    else:
        stmt = stmt.where(ApprovalRequest.team_id == ensure_default_team(session).id)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    requests = session.execute(stmt.order_by(ApprovalRequest.created_at).limit(limit)).scalars()
    return [_redacted_request(item) for item in requests]


def _redacted_request(request: ApprovalRequest) -> ApprovalRequestRead:
    result = ApprovalRequestRead.model_validate(request)
    return result.model_copy(update={
        "action_preview": redact_mapping(result.action_preview),
        "policy_evidence": redact_mapping(result.policy_evidence),
    })


@router.get("/approval-requests/{request_id}", response_model=ApprovalRequestRead)
def get_approval_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalRequestRead:
    request = session.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    _require_project_access(session, actor, request.project_id)
    return _redacted_request(request)


def _resolve_request(
    request_id: uuid.UUID, decision: Literal["approved", "denied", "expired"],
    payload: ApprovalDecisionWrite, session: Session, actor: User,
) -> ApprovalDecisionRead:
    request = session.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    _require_project_access(session, actor, request.project_id)
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="approval request is already resolved")
    now = datetime.now(timezone.utc)
    if decision == "expired" and request.expires_at is not None and request.expires_at > now:
        raise HTTPException(status_code=409, detail="approval request has not expired")
    request.status = decision
    request.resolved_at = now
    record = ApprovalDecisionRecord(
        approval_request_id=request.id, decision=decision, actor_id=actor.id,
        reason=payload.reason, context=payload.context,
        evaluated_policy_version_ids=request.policy_version_ids,
    )
    session.add(record)
    session.add(AuditEvent(
        project_id=request.project_id, goal_id=request.goal_id, task_id=request.task_id,
        run_id=request.run_id, event_type=f"approval.{decision}",
        payload={"approval_request_id": str(request.id), "actor_id": str(actor.id), "reason": payload.reason},
    ))
    session.flush()
    session.refresh(record)
    result = ApprovalDecisionRead.model_validate(record)
    return result.model_copy(update={"context": redact_mapping(result.context)})


@router.post("/approval-requests/{request_id}/approve", response_model=ApprovalDecisionRead, status_code=201)
def approve_request(
    request_id: uuid.UUID,
    payload: ApprovalDecisionWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalDecisionRead:
    return _resolve_request(request_id, "approved", payload, session, actor)


@router.post("/approval-requests/{request_id}/deny", response_model=ApprovalDecisionRead, status_code=201)
def deny_request(
    request_id: uuid.UUID,
    payload: ApprovalDecisionWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalDecisionRead:
    return _resolve_request(request_id, "denied", payload, session, actor)


@router.post("/approval-requests/{request_id}/expire", response_model=ApprovalDecisionRead, status_code=201)
def expire_request(
    request_id: uuid.UUID,
    payload: ApprovalDecisionWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ApprovalDecisionRead:
    return _resolve_request(request_id, "expired", payload, session, actor)


def _override_scope(session: Session, payload: OverrideWrite) -> tuple[Project, dict[str, uuid.UUID | None]]:
    values: dict[str, uuid.UUID | None] = {"project_id": None, "goal_id": None, "task_id": None, "run_id": None}
    if payload.scope_type == "project":
        project = session.get(Project, payload.scope_id)
    elif payload.scope_type == "goal":
        goal = session.get(Goal, payload.scope_id)
        project = session.get(Project, goal.project_id) if goal else None
        values["goal_id"] = goal.id if goal else None
    elif payload.scope_type == "task":
        task = session.get(Task, payload.scope_id)
        goal = session.get(Goal, task.goal_id) if task else None
        project = session.get(Project, goal.project_id) if goal else None
        values.update(goal_id=goal.id if goal else None, task_id=task.id if task else None)
    else:
        run = session.get(Run, payload.scope_id)
        task = session.get(Task, run.task_id) if run else None
        goal = session.get(Goal, task.goal_id) if task else None
        project = session.get(Project, goal.project_id) if goal else None
        values.update(
            goal_id=goal.id if goal else None,
            task_id=task.id if task else None,
            run_id=run.id if run else None,
        )
    if project is None:
        raise HTTPException(status_code=422, detail="override scope not found")
    values["project_id"] = project.id
    return project, values


def _redacted_override(value: AdminOverride) -> OverrideRead:
    result = OverrideRead.model_validate(value)
    return result.model_copy(update={"context": redact_mapping(result.context)})


@router.post("/admin-overrides", response_model=OverrideRead, status_code=201)
def create_admin_override(
    payload: OverrideWrite,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> OverrideRead:
    _require_admin(actor)
    project, scope = _override_scope(session, payload)
    _require_project_access(session, actor, project.id)
    starts_at = payload.starts_at or datetime.now(timezone.utc)
    if payload.expires_at <= starts_at:
        raise HTTPException(status_code=422, detail="expires_at must be after starts_at")
    override = AdminOverride(
        team_id=project.team_id, created_by=actor.id, scope_type=payload.scope_type,
        scope_id=payload.scope_id, reason=payload.reason.strip(), starts_at=starts_at,
        expires_at=payload.expires_at,
        evaluated_policy_version_ids=[
            str(value) for value in payload.evaluated_policy_version_ids
        ],
        context=payload.context, **scope,
    )
    session.add(override)
    session.add(AuditEvent(
        project_id=scope["project_id"], goal_id=scope["goal_id"], task_id=scope["task_id"],
        run_id=scope["run_id"], event_type="governance.admin_override_created",
        payload={
            "scope_type": payload.scope_type,
            "scope_id": str(payload.scope_id),
            "actor_id": str(actor.id),
            "reason": payload.reason.strip(),
        },
    ))
    session.flush()
    session.refresh(override)
    return _redacted_override(override)


@router.get("/admin-overrides", response_model=list[OverrideRead])
def list_admin_overrides(
    project_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[OverrideRead]:
    _require_admin(actor)
    stmt = select(AdminOverride).where(AdminOverride.team_id == ensure_default_team(session).id)
    if project_id is not None:
        _require_project_access(session, actor, project_id)
        stmt = stmt.where(AdminOverride.project_id == project_id)
    values = session.execute(stmt.order_by(AdminOverride.created_at).limit(limit)).scalars()
    return [_redacted_override(value) for value in values]


@router.get("/admin-overrides/{override_id}", response_model=OverrideRead)
def get_admin_override(
    override_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> OverrideRead:
    _require_admin(actor)
    value = session.get(AdminOverride, override_id)
    if value is None:
        raise HTTPException(status_code=404, detail="admin override not found")
    if value.project_id is not None:
        _require_project_access(session, actor, value.project_id)
    return _redacted_override(value)


@router.get("/governance/evidence")
def governance_evidence(
    project_id: uuid.UUID | None = None, goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session), actor: User = Depends(current_actor),
) -> dict:
    filters = {"project_id": project_id, "goal_id": goal_id, "task_id": task_id, "run_id": run_id}
    resolved_project_id = project_id
    if run_id is not None:
        run = session.get(Run, run_id)
        task = session.get(Task, run.task_id) if run else None
        goal = session.get(Goal, task.goal_id) if task else None
        resolved_project_id = goal.project_id if goal else None
    elif task_id is not None:
        task = session.get(Task, task_id); goal = session.get(Goal, task.goal_id) if task else None
        resolved_project_id = goal.project_id if goal else None
    elif goal_id is not None:
        goal = session.get(Goal, goal_id); resolved_project_id = goal.project_id if goal else None
    if resolved_project_id is None:
        raise HTTPException(status_code=422, detail="a valid project, goal, task, or run scope is required")
    _require_project_access(session, actor, resolved_project_id)

    def scoped(model):
        stmt = select(model)
        for field, value in filters.items():
            if value is not None and hasattr(model, field):
                stmt = stmt.where(getattr(model, field) == value)
        return list(session.execute(stmt.limit(limit)).scalars())

    requests = scoped(ApprovalRequest)
    decision_stmt = select(ApprovalDecisionRecord).join(ApprovalRequest).where(
        ApprovalRequest.project_id == resolved_project_id
    )
    for field, value in filters.items():
        if value is not None and hasattr(ApprovalRequest, field):
            decision_stmt = decision_stmt.where(getattr(ApprovalRequest, field) == value)
    decisions = list(session.execute(decision_stmt.limit(limit)).scalars())
    return redact_mapping({
        "approval_requests": [ApprovalRequestRead.model_validate(value).model_dump(mode="json") for value in requests],
        "approval_decisions": [
            ApprovalDecisionRead.model_validate(value).model_dump(mode="json")
            for value in decisions
        ],
        "admin_overrides": [
            OverrideRead.model_validate(value).model_dump(mode="json")
            for value in scoped(AdminOverride)
        ],
        "budget_reservations": [_evidence_record(value) for value in scoped(BudgetReservation)],
        "cost_ledger_entries": [_evidence_record(value) for value in scoped(CostLedgerEntry)],
        "audit_events": [_evidence_record(value) for value in scoped(AuditEvent)],
    })


def _evidence_record(value: object) -> dict:
    return {column.name: getattr(value, column.name) for column in value.__table__.columns}
