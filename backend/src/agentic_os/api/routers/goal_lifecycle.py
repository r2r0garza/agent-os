from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_resource_access
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import (
    Goal,
    GoalLifecycleCommand,
    GoalLifecycleEvent,
    GoalSteeringRequest,
    Project,
    TaskGraphRevision,
    TaskGraphRevisionTask,
    User,
)
from agentic_os.observability import current_request_context, record_observability

router = APIRouter(tags=["goal-lifecycle"])

DEFAULT_CANCELLATION_GRACE_SECONDS = 30

GOAL_LIFECYCLE_TRANSITIONS: dict[str, dict[str, object]] = {
    "pause": {"allowed_from": {"active"}, "target_status": "paused"},
    "resume": {"allowed_from": {"paused"}, "target_status": "active"},
    "cancel": {"allowed_from": {"draft", "active", "paused"}, "target_status": "cancelled"},
}

NON_STEERABLE_GOAL_STATUSES = {"completed", "cancelled", "failed"}


class LifecycleCommandRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = None


class SteeringRequestCreate(BaseModel):
    instruction: str
    base_revision_number: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = None

    @field_validator("instruction")
    @classmethod
    def _validate_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must not be empty")
        return value


class GoalLifecycleCommandRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    requested_by: uuid.UUID | None
    command_type: str
    status: str
    idempotency_key: str
    reason: str | None
    prior_goal_status: str | None
    target_goal_status: str | None
    cancellation_grace_expires_at: datetime | None
    forced_termination_requested_at: datetime | None
    forced_termination_completed_at: datetime | None
    applied_at: datetime | None
    evidence: dict
    created_at: datetime


class GoalSteeringRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    requested_by: uuid.UUID | None
    status: str
    idempotency_key: str
    instruction: str
    base_revision_number: int
    applied_revision_number: int | None
    resolved_at: datetime | None
    evidence: dict
    created_at: datetime


class TaskGraphRevisionTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revision_id: uuid.UUID
    task_id: uuid.UUID
    change_type: str
    supersedes_task_id: uuid.UUID | None
    task_snapshot: dict


class TaskGraphRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    created_by: uuid.UUID | None
    steering_request_id: uuid.UUID | None
    revision_number: int
    parent_revision_number: int | None
    change_summary: str | None
    graph_snapshot: dict
    assignment_evidence: dict
    policy_context: dict
    budget_context: dict
    created_at: datetime


class TaskGraphRevisionDetailRead(TaskGraphRevisionRead):
    tasks: list[TaskGraphRevisionTaskRead]


class GoalLifecycleEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence_number: int
    goal_id: uuid.UUID
    actor_id: uuid.UUID | None
    lifecycle_command_id: uuid.UUID | None
    steering_request_id: uuid.UUID | None
    graph_revision_id: uuid.UUID | None
    event_type: str
    prior_goal_status: str | None
    resulting_goal_status: str | None
    payload: dict
    occurred_at: datetime


def _redacted_command(command: GoalLifecycleCommand) -> GoalLifecycleCommandRead:
    result = GoalLifecycleCommandRead.model_validate(command)
    return result.model_copy(update={"evidence": redact_mapping(result.evidence)})


def _redacted_steering_request(request: GoalSteeringRequest) -> GoalSteeringRequestRead:
    result = GoalSteeringRequestRead.model_validate(request)
    return result.model_copy(update={"evidence": redact_mapping(result.evidence)})


def _redacted_revision(revision: TaskGraphRevision) -> TaskGraphRevisionRead:
    result = TaskGraphRevisionRead.model_validate(revision)
    return result.model_copy(
        update={
            "graph_snapshot": redact_mapping(result.graph_snapshot),
            "assignment_evidence": redact_mapping(result.assignment_evidence),
            "policy_context": redact_mapping(result.policy_context),
            "budget_context": redact_mapping(result.budget_context),
        }
    )


def _redacted_event(event: GoalLifecycleEvent) -> GoalLifecycleEventRead:
    result = GoalLifecycleEventRead.model_validate(event)
    return result.model_copy(update={"payload": redact_mapping(result.payload)})


def _load_goal(session: Session, goal_id: uuid.UUID) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return goal


def _record_lifecycle_event(
    session: Session,
    project: Project,
    goal: Goal,
    actor: User,
    *,
    event_type: str,
    lifecycle_command_id: uuid.UUID | None = None,
    steering_request_id: uuid.UUID | None = None,
    graph_revision_id: uuid.UUID | None = None,
    prior_status: str | None,
    resulting_status: str | None,
    payload: dict | None = None,
) -> GoalLifecycleEvent:
    event = GoalLifecycleEvent(
        goal_id=goal.id,
        actor_id=actor.id,
        lifecycle_command_id=lifecycle_command_id,
        steering_request_id=steering_request_id,
        graph_revision_id=graph_revision_id,
        event_type=event_type,
        prior_goal_status=prior_status,
        resulting_goal_status=resulting_status,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    context = current_request_context()
    if context is not None:
        record_observability(
            session,
            replace(
                context,
                team_id=project.team_id,
                user_id=actor.id,
                project_id=project.id,
                goal_id=goal.id,
            ),
            event_kind="goal",
            operation_name=event_type,
            status=resulting_status,
            attributes={
                "lifecycle_command_id": str(lifecycle_command_id) if lifecycle_command_id else None,
                "steering_request_id": str(steering_request_id) if steering_request_id else None,
            },
        )
    return event


def _persist_rejected_record(
    session: Session,
    *,
    record: GoalLifecycleCommand | GoalSteeringRequest,
    goal: Goal,
    actor: User,
    event_type: str,
    rejection_reason: str,
) -> None:
    """Persist a rejected command/request and its audit event on their own transaction.

    The main request session is rolled back whenever the endpoint raises an
    HTTPException, so a rejection recorded there would silently disappear.
    Mirrors the isolated-session pattern ``_record_decision`` uses for denied
    authorization checks.
    """
    with Session(bind=session.get_bind()) as audit_session:
        audit_session.add(record)
        audit_session.flush()
        event_kwargs: dict[str, uuid.UUID] = {}
        if isinstance(record, GoalLifecycleCommand):
            event_kwargs["lifecycle_command_id"] = record.id
        else:
            event_kwargs["steering_request_id"] = record.id
        audit_session.add(
            GoalLifecycleEvent(
                goal_id=goal.id,
                actor_id=actor.id,
                event_type=event_type,
                prior_goal_status=goal.status,
                resulting_goal_status=goal.status,
                payload={"reason": rejection_reason},
                **event_kwargs,
            )
        )
        audit_session.commit()
        audit_session.refresh(record)


def _apply_lifecycle_command(
    goal_id: uuid.UUID,
    command_type: str,
    payload: LifecycleCommandRequest,
    session: Session,
    actor: User,
) -> GoalLifecycleCommandRead:
    goal = _load_goal(session, goal_id)
    project = require_resource_access(
        session, actor, goal, action=f"goal.{command_type}", resource_type="goal"
    )

    if payload.idempotency_key is not None:
        existing = session.execute(
            select(GoalLifecycleCommand).where(
                GoalLifecycleCommand.goal_id == goal.id,
                GoalLifecycleCommand.idempotency_key == payload.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _redacted_command(existing)

    resolved_idempotency_key = payload.idempotency_key or f"{goal.id}:{command_type}:{uuid.uuid4()}"
    prior_status = goal.status
    transition = GOAL_LIFECYCLE_TRANSITIONS[command_type]

    if prior_status not in transition["allowed_from"]:
        rejected = GoalLifecycleCommand(
            goal_id=goal.id,
            requested_by=actor.id,
            command_type=command_type,
            idempotency_key=resolved_idempotency_key,
            reason=payload.reason,
            prior_goal_status=prior_status,
            status="rejected",
            evidence={"rejection_reason": "invalid_state_transition", "goal_status": prior_status},
        )
        _persist_rejected_record(
            session,
            record=rejected,
            goal=goal,
            actor=actor,
            event_type=f"goal.{command_type}.rejected",
            rejection_reason="invalid_state_transition",
        )
        raise HTTPException(
            status_code=409,
            detail=f"cannot {command_type} goal in status {prior_status!r}",
        )

    now = datetime.now(timezone.utc)
    target_status = str(transition["target_status"])

    command = GoalLifecycleCommand(
        goal_id=goal.id,
        requested_by=actor.id,
        command_type=command_type,
        idempotency_key=resolved_idempotency_key,
        reason=payload.reason,
        prior_goal_status=prior_status,
        status="applied",
        target_goal_status=target_status,
        applied_at=now,
        evidence={"applied": True},
    )
    session.add(command)
    session.flush()

    goal.status = target_status
    goal.control_version += 1
    goal.pending_control = command_type
    goal.control_requested_by = actor.id
    goal.control_requested_at = now
    if command_type == "cancel":
        goal.cancellation_grace_expires_at = now + timedelta(
            seconds=DEFAULT_CANCELLATION_GRACE_SECONDS
        )
        command.cancellation_grace_expires_at = goal.cancellation_grace_expires_at
        session.flush()

    _record_lifecycle_event(
        session,
        project,
        goal,
        actor,
        event_type=f"goal.{command_type}.applied",
        lifecycle_command_id=command.id,
        prior_status=prior_status,
        resulting_status=target_status,
        payload={"reason": payload.reason} if payload.reason else {},
    )

    return _redacted_command(command)


@router.post(
    "/goals/{goal_id}/pause",
    response_model=GoalLifecycleCommandRead,
    status_code=201,
)
def pause_goal(
    goal_id: uuid.UUID,
    payload: LifecycleCommandRequest = LifecycleCommandRequest(),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> GoalLifecycleCommandRead:
    return _apply_lifecycle_command(goal_id, "pause", payload, session, actor)


@router.post(
    "/goals/{goal_id}/resume",
    response_model=GoalLifecycleCommandRead,
    status_code=201,
)
def resume_goal(
    goal_id: uuid.UUID,
    payload: LifecycleCommandRequest = LifecycleCommandRequest(),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> GoalLifecycleCommandRead:
    return _apply_lifecycle_command(goal_id, "resume", payload, session, actor)


@router.post(
    "/goals/{goal_id}/cancel",
    response_model=GoalLifecycleCommandRead,
    status_code=201,
)
def cancel_goal(
    goal_id: uuid.UUID,
    payload: LifecycleCommandRequest = LifecycleCommandRequest(),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> GoalLifecycleCommandRead:
    return _apply_lifecycle_command(goal_id, "cancel", payload, session, actor)


@router.get(
    "/goals/{goal_id}/lifecycle-commands",
    response_model=list[GoalLifecycleCommandRead],
)
def list_lifecycle_commands(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[GoalLifecycleCommandRead]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.lifecycle_commands.list", resource_type="goal")
    commands = session.execute(
        select(GoalLifecycleCommand)
        .where(GoalLifecycleCommand.goal_id == goal_id)
        .order_by(GoalLifecycleCommand.created_at)
    ).scalars()
    return [_redacted_command(command) for command in commands]


@router.post(
    "/goals/{goal_id}/steer",
    response_model=GoalSteeringRequestRead,
    status_code=201,
)
def steer_goal(
    goal_id: uuid.UUID,
    payload: SteeringRequestCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> GoalSteeringRequestRead:
    goal = _load_goal(session, goal_id)
    project = require_resource_access(session, actor, goal, action="goal.steer", resource_type="goal")

    if payload.idempotency_key is not None:
        existing = session.execute(
            select(GoalSteeringRequest).where(
                GoalSteeringRequest.goal_id == goal.id,
                GoalSteeringRequest.idempotency_key == payload.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _redacted_steering_request(existing)

    resolved_idempotency_key = payload.idempotency_key or f"{goal.id}:steer:{uuid.uuid4()}"
    base_revision_number = (
        payload.base_revision_number
        if payload.base_revision_number is not None
        else goal.active_graph_revision_number
    )

    if goal.status in NON_STEERABLE_GOAL_STATUSES:
        rejected = GoalSteeringRequest(
            goal_id=goal.id,
            requested_by=actor.id,
            instruction=payload.instruction,
            base_revision_number=base_revision_number,
            idempotency_key=resolved_idempotency_key,
            status="rejected",
            evidence={"rejection_reason": "goal_not_steerable", "goal_status": goal.status},
        )
        _persist_rejected_record(
            session,
            record=rejected,
            goal=goal,
            actor=actor,
            event_type="goal.steering.rejected",
            rejection_reason="goal_not_steerable",
        )
        raise HTTPException(
            status_code=409,
            detail=f"cannot steer goal in status {goal.status!r}",
        )

    request = GoalSteeringRequest(
        goal_id=goal.id,
        requested_by=actor.id,
        instruction=payload.instruction,
        base_revision_number=base_revision_number,
        idempotency_key=resolved_idempotency_key,
    )
    session.add(request)
    session.flush()

    _record_lifecycle_event(
        session,
        project,
        goal,
        actor,
        event_type="goal.steering.requested",
        steering_request_id=request.id,
        prior_status=goal.status,
        resulting_status=goal.status,
        payload={"instruction": payload.instruction, "base_revision_number": base_revision_number},
    )

    return _redacted_steering_request(request)


@router.get(
    "/goals/{goal_id}/steering-requests",
    response_model=list[GoalSteeringRequestRead],
)
def list_steering_requests(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[GoalSteeringRequestRead]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.steering_requests.list", resource_type="goal")
    requests = session.execute(
        select(GoalSteeringRequest)
        .where(GoalSteeringRequest.goal_id == goal_id)
        .order_by(GoalSteeringRequest.created_at)
    ).scalars()
    return [_redacted_steering_request(request) for request in requests]


@router.get(
    "/goals/{goal_id}/graph-revisions",
    response_model=list[TaskGraphRevisionRead],
)
def list_graph_revisions(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[TaskGraphRevisionRead]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.graph_revisions.list", resource_type="goal")
    revisions = session.execute(
        select(TaskGraphRevision)
        .where(TaskGraphRevision.goal_id == goal_id)
        .order_by(TaskGraphRevision.revision_number)
    ).scalars()
    return [_redacted_revision(revision) for revision in revisions]


@router.get(
    "/goals/{goal_id}/graph-revisions/{revision_number}",
    response_model=TaskGraphRevisionDetailRead,
)
def get_graph_revision(
    goal_id: uuid.UUID,
    revision_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> TaskGraphRevisionDetailRead:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.graph_revisions.read", resource_type="goal")
    revision = session.execute(
        select(TaskGraphRevision).where(
            TaskGraphRevision.goal_id == goal_id,
            TaskGraphRevision.revision_number == revision_number,
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=404, detail="task graph revision not found")
    tasks = session.execute(
        select(TaskGraphRevisionTask).where(TaskGraphRevisionTask.revision_id == revision.id)
    ).scalars()
    base = _redacted_revision(revision)
    return TaskGraphRevisionDetailRead(
        **base.model_dump(),
        tasks=[TaskGraphRevisionTaskRead.model_validate(task) for task in tasks],
    )


@router.get(
    "/goals/{goal_id}/lifecycle-events",
    response_model=list[GoalLifecycleEventRead],
)
def list_lifecycle_events(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[GoalLifecycleEventRead]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.lifecycle_events.list", resource_type="goal")
    events = session.execute(
        select(GoalLifecycleEvent)
        .where(GoalLifecycleEvent.goal_id == goal_id)
        .order_by(GoalLifecycleEvent.sequence_number)
    ).scalars()
    return [_redacted_event(event) for event in events]
