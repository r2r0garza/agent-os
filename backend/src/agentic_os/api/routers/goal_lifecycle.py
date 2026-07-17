from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_resource_access
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import (
    Budget,
    Goal,
    GoalLifecycleCommand,
    GoalLifecycleEvent,
    GoalSteeringRequest,
    Policy,
    Project,
    Task,
    TaskDependency,
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
STEERABLE_TASK_STATUSES = {"pending", "ready", "blocked", "failed"}


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


class SteeringTaskSpec(BaseModel):
    client_id: str
    title: str
    description: str | None = None
    required_capabilities: dict = Field(default_factory=dict)
    capability_rationale: dict = Field(default_factory=dict)
    expected_outputs: list = Field(default_factory=list)
    resource_intent: list = Field(default_factory=list)
    policy_ids: list[uuid.UUID] | None = None
    budget_id: uuid.UUID | None = None
    depends_on: list[str] | None = None

    @field_validator("client_id", "title")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class SteeringTaskChange(BaseModel):
    change_type: Literal["added", "revised", "superseded"]
    task_id: uuid.UUID | None = None
    task: SteeringTaskSpec | None = None

    @model_validator(mode="after")
    def _validate_change_shape(self) -> "SteeringTaskChange":
        if self.change_type in {"added", "revised"} and self.task is None:
            raise ValueError(f"task is required for {self.change_type} changes")
        if self.change_type == "superseded" and self.task is not None:
            raise ValueError("task must be omitted for superseded changes")
        if self.change_type in {"revised", "superseded"} and self.task_id is None:
            raise ValueError(f"task_id is required for {self.change_type} changes")
        if self.change_type == "added" and self.task_id is not None:
            raise ValueError("task_id must be omitted for added changes")
        return self


class SteeringRevisionApply(BaseModel):
    change_summary: str
    changes: list[SteeringTaskChange]

    @field_validator("change_summary")
    @classmethod
    def _validate_change_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("change_summary must not be empty")
        return value

    @field_validator("changes")
    @classmethod
    def _validate_changes(cls, value: list[SteeringTaskChange]) -> list[SteeringTaskChange]:
        if not value:
            raise ValueError("changes must not be empty")
        client_ids = [
            change.task.client_id
            for change in value
            if change.task is not None
        ]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("task client_id values must be unique")
        target_ids = [
            change.task_id
            for change in value
            if change.task_id is not None
        ]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("a task may be changed at most once per revision")
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


def _task_snapshot(task: Task) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "required_capabilities": task.required_capabilities,
        "capability_rationale": task.capability_rationale,
        "expected_outputs": task.expected_outputs,
        "resource_intent": task.resource_intent,
        "policy_ids": task.policy_ids,
        "knowledge_artifact_ids": task.knowledge_artifact_ids,
        "budget_id": str(task.budget_id) if task.budget_id else None,
        "assigned_agent_version_id": (
            str(task.assigned_agent_version_id)
            if task.assigned_agent_version_id
            else None
        ),
        "assignment_status": task.assignment_status,
        "assignment_candidates": task.assignment_candidates,
        "assignment_rationale": task.assignment_rationale,
    }


def _find_cycle(
    adjacency: dict[uuid.UUID, set[uuid.UUID]],
) -> list[uuid.UUID] | None:
    visiting: set[uuid.UUID] = set()
    visited: set[uuid.UUID] = set()
    path: list[uuid.UUID] = []

    def visit(task_id: uuid.UUID) -> list[uuid.UUID] | None:
        visiting.add(task_id)
        path.append(task_id)
        for dependency_id in adjacency.get(task_id, set()):
            if dependency_id in visiting:
                start = path.index(dependency_id)
                return path[start:] + [dependency_id]
            if dependency_id not in visited:
                cycle = visit(dependency_id)
                if cycle is not None:
                    return cycle
        path.pop()
        visiting.remove(task_id)
        visited.add(task_id)
        return None

    for task_id in adjacency:
        if task_id not in visited:
            cycle = visit(task_id)
            if cycle is not None:
                return cycle
    return None


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


def _resolve_steering_dependencies(
    references: list[str],
    *,
    client_ids: dict[str, uuid.UUID],
    active_task_ids: set[uuid.UUID],
    task_label: str,
) -> set[uuid.UUID]:
    resolved: set[uuid.UUID] = set()
    for reference in references:
        if reference in client_ids:
            dependency_id = client_ids[reference]
        else:
            try:
                dependency_id = uuid.UUID(reference)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"task {task_label!r} depends on unknown reference {reference!r}",
                ) from error
            if dependency_id not in active_task_ids:
                raise HTTPException(
                    status_code=422,
                    detail=f"task {task_label!r} depends on inactive or unknown task {reference!r}",
                )
        resolved.add(dependency_id)
    return resolved


def _validate_task_context(
    session: Session,
    task_spec: SteeringTaskSpec,
) -> None:
    if task_spec.budget_id is not None and session.get(Budget, task_spec.budget_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"budget {task_spec.budget_id} not found",
        )
    for policy_id in task_spec.policy_ids or []:
        if session.get(Policy, policy_id) is None:
            raise HTTPException(status_code=422, detail=f"policy {policy_id} not found")


def _build_steering_revision(
    session: Session,
    *,
    goal: Goal,
    request: GoalSteeringRequest,
    actor: User,
    payload: SteeringRevisionApply,
) -> TaskGraphRevision:
    tasks = list(
        session.execute(
            select(Task).where(Task.goal_id == goal.id).with_for_update()
        ).scalars()
    )
    tasks_by_id = {task.id: task for task in tasks}
    target_ids = {
        change.task_id for change in payload.changes if change.task_id is not None
    }
    for target_id in target_ids:
        target = tasks_by_id.get(target_id)
        if target is None:
            raise HTTPException(
                status_code=422,
                detail=f"task {target_id} does not belong to goal",
            )
        if target.status not in STEERABLE_TASK_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"task {target_id} in status {target.status!r} cannot be changed",
            )

    client_ids = {
        change.task.client_id: uuid.uuid4()
        for change in payload.changes
        if change.task is not None
    }
    superseded_ids = {
        change.task_id
        for change in payload.changes
        if change.change_type in {"revised", "superseded"}
        and change.task_id is not None
    }
    active_task_ids = set(tasks_by_id) - superseded_ids
    active_task_ids.update(client_ids.values())

    dependency_rows = list(
        session.execute(
            select(TaskDependency).where(TaskDependency.task_id.in_(tasks_by_id))
        ).scalars()
    )
    adjacency: dict[uuid.UUID, set[uuid.UUID]] = {
        task_id: set() for task_id in active_task_ids
    }
    for dependency in dependency_rows:
        if (
            dependency.task_id in active_task_ids
            and dependency.depends_on_task_id in tasks_by_id
        ):
            adjacency[dependency.task_id].add(dependency.depends_on_task_id)

    revision_changes: list[tuple[Task, str, uuid.UUID | None]] = []
    replacement_by_target: dict[uuid.UUID, uuid.UUID] = {}
    for change in payload.changes:
        if change.task is None:
            continue
        _validate_task_context(session, change.task)
        new_task_id = client_ids[change.task.client_id]
        predecessor = change.task_id if change.change_type == "revised" else None
        inherited = tasks_by_id.get(predecessor) if predecessor is not None else None
        policy_ids = (
            inherited.policy_ids
            if inherited is not None and change.task.policy_ids is None
            else [str(policy_id) for policy_id in change.task.policy_ids or []]
        )
        budget_id = (
            inherited.budget_id
            if inherited is not None
            and "budget_id" not in change.task.model_fields_set
            else change.task.budget_id
        )
        new_task = Task(
            id=new_task_id,
            goal_id=goal.id,
            created_by=actor.id,
            title=change.task.title,
            description=change.task.description,
            required_capabilities=change.task.required_capabilities,
            capability_rationale=change.task.capability_rationale,
            expected_outputs=change.task.expected_outputs,
            resource_intent=change.task.resource_intent,
            policy_ids=policy_ids,
            knowledge_artifact_ids=(
                inherited.knowledge_artifact_ids if inherited is not None else []
            ),
            budget_id=budget_id,
            assigned_agent_version_id=None,
            assignment_status="unassigned",
            assignment_candidates=[],
            assignment_rationale={},
            assignment_updated_at=None,
        )
        session.add(new_task)
        tasks_by_id[new_task.id] = new_task
        revision_changes.append((new_task, change.change_type, predecessor))
        if predecessor is not None:
            replacement_by_target[predecessor] = new_task.id
            if change.task.depends_on is None:
                adjacency[new_task.id] = {
                    replacement_by_target.get(dependency_id, dependency_id)
                    for dependency_id in {
                        dependency.depends_on_task_id
                        for dependency in dependency_rows
                        if dependency.task_id == predecessor
                    }
                    if dependency_id in active_task_ids
                    or dependency_id in replacement_by_target
                }

    rewired_task_ids: set[uuid.UUID] = set()
    for old_task_id, replacement_id in replacement_by_target.items():
        for task_id, dependencies in adjacency.items():
            if old_task_id in dependencies:
                dependencies.remove(old_task_id)
                dependencies.add(replacement_id)
                rewired_task_ids.add(task_id)

    for change in payload.changes:
        if change.task is None:
            continue
        if change.task.depends_on is not None:
            new_task_id = client_ids[change.task.client_id]
            adjacency[new_task_id] = _resolve_steering_dependencies(
                change.task.depends_on,
                client_ids=client_ids,
                active_task_ids=active_task_ids,
                task_label=change.task.client_id,
            )

    for change in payload.changes:
        if change.change_type != "superseded" or change.task_id is None:
            continue
        active_dependents = [
            task_id
            for task_id, dependencies in adjacency.items()
            if change.task_id in dependencies
        ]
        if active_dependents:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"task {change.task_id} has active dependents; revise it with a "
                    "replacement instead of superseding it"
                ),
            )

    for task_id, dependencies in adjacency.items():
        if task_id in dependencies:
            raise HTTPException(
                status_code=422,
                detail=f"task {task_id} cannot depend on itself",
            )
    cycle = _find_cycle(adjacency)
    if cycle is not None:
        raise HTTPException(
            status_code=422,
            detail=f"task graph contains a dependency cycle: {[str(node) for node in cycle]}",
        )

    for target_id in superseded_ids:
        target = tasks_by_id[target_id]
        target.status = "cancelled"
        revision_changes.append((target, "superseded", None))

    session.flush()
    if superseded_ids:
        session.execute(
            delete(TaskDependency).where(
                (TaskDependency.task_id.in_(superseded_ids))
                | (TaskDependency.depends_on_task_id.in_(superseded_ids))
            )
        )
    changed_task_ids = set(client_ids.values()) | superseded_ids
    if changed_task_ids:
        session.execute(
            delete(TaskDependency).where(TaskDependency.task_id.in_(changed_task_ids))
        )
    for task_id, dependencies in adjacency.items():
        if task_id not in changed_task_ids and task_id not in rewired_task_ids:
            continue
        session.execute(delete(TaskDependency).where(TaskDependency.task_id == task_id))
        for dependency_id in dependencies:
            session.add(
                TaskDependency(
                    task_id=task_id,
                    depends_on_task_id=dependency_id,
                )
            )
    session.flush()

    effective_tasks = [
        task for task in tasks_by_id.values() if task.id in active_task_ids
    ]
    graph_snapshot = {
        "tasks": [_task_snapshot(task) for task in effective_tasks],
        "dependencies": [
            {
                "task_id": str(task_id),
                "depends_on_task_id": str(dependency_id),
            }
            for task_id, dependencies in adjacency.items()
            for dependency_id in dependencies
        ],
        "superseded_task_ids": [str(task_id) for task_id in superseded_ids],
    }
    revision = TaskGraphRevision(
        goal_id=goal.id,
        created_by=actor.id,
        steering_request_id=request.id,
        revision_number=goal.active_graph_revision_number + 1,
        parent_revision_number=goal.active_graph_revision_number,
        change_summary=payload.change_summary,
        graph_snapshot=graph_snapshot,
        assignment_evidence={
            str(task.id): {
                "assigned_agent_version_id": (
                    str(task.assigned_agent_version_id)
                    if task.assigned_agent_version_id
                    else None
                ),
                "assignment_status": task.assignment_status,
                "assignment_candidates": task.assignment_candidates,
                "assignment_rationale": task.assignment_rationale,
                "effective": task.id in active_task_ids,
                "replacement_task_id": (
                    str(replacement_by_target[task.id])
                    if task.id in replacement_by_target
                    else None
                ),
            }
            for task in tasks_by_id.values()
        },
        policy_context={
            str(task.id): {"policy_ids": task.policy_ids}
            for task in effective_tasks
        },
        budget_context={
            str(task.id): {
                "budget_id": str(task.budget_id) if task.budget_id else None
            }
            for task in effective_tasks
        },
    )
    session.add(revision)
    session.flush()
    for task, change_type, supersedes_task_id in revision_changes:
        session.add(
            TaskGraphRevisionTask(
                revision_id=revision.id,
                task_id=task.id,
                change_type=change_type,
                supersedes_task_id=supersedes_task_id,
                task_snapshot=_task_snapshot(task),
            )
        )
    session.flush()
    return revision


@router.post(
    "/goals/{goal_id}/steering-requests/{request_id}/apply",
    response_model=TaskGraphRevisionDetailRead,
    status_code=201,
)
def apply_steering_request(
    goal_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: SteeringRevisionApply,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> TaskGraphRevisionDetailRead:
    goal = session.execute(
        select(Goal).where(Goal.id == goal_id).with_for_update()
    ).scalar_one_or_none()
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    project = require_resource_access(
        session,
        actor,
        goal,
        action="goal.steering_requests.apply",
        resource_type="goal",
    )
    request = session.execute(
        select(GoalSteeringRequest)
        .where(
            GoalSteeringRequest.id == request_id,
            GoalSteeringRequest.goal_id == goal_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="steering request not found")
    if request.status == "applied":
        revision = session.execute(
            select(TaskGraphRevision).where(
                TaskGraphRevision.steering_request_id == request.id
            )
        ).scalar_one()
    else:
        if request.status != "requested":
            raise HTTPException(status_code=409, detail="steering request is not pending")
        if goal.status in NON_STEERABLE_GOAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"cannot apply steering to goal in status {goal.status!r}",
            )
        if request.base_revision_number != goal.active_graph_revision_number:
            raise HTTPException(
                status_code=409,
                detail=(
                    "steering request base revision is stale: "
                    f"expected {goal.active_graph_revision_number}, "
                    f"received {request.base_revision_number}"
                ),
            )
        revision = _build_steering_revision(
            session,
            goal=goal,
            request=request,
            actor=actor,
            payload=payload,
        )
        now = datetime.now(timezone.utc)
        request.status = "applied"
        request.applied_revision_number = revision.revision_number
        request.resolved_at = now
        request.evidence = {
            "change_summary": payload.change_summary,
            "change_count": len(payload.changes),
            "graph_revision_id": str(revision.id),
            "applied_by": str(actor.id),
        }
        goal.active_graph_revision_number = revision.revision_number
        _record_lifecycle_event(
            session,
            project,
            goal,
            actor,
            event_type="goal.steering.applied",
            steering_request_id=request.id,
            graph_revision_id=revision.id,
            prior_status=goal.status,
            resulting_status=goal.status,
            payload={
                "change_summary": payload.change_summary,
                "revision_number": revision.revision_number,
                "parent_revision_number": revision.parent_revision_number,
            },
        )

    revision_tasks = list(
        session.execute(
            select(TaskGraphRevisionTask).where(
                TaskGraphRevisionTask.revision_id == revision.id
            )
        ).scalars()
    )
    base = _redacted_revision(revision)
    return TaskGraphRevisionDetailRead(
        **base.model_dump(),
        tasks=[
            TaskGraphRevisionTaskRead.model_validate(task)
            for task in revision_tasks
        ],
    )


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
