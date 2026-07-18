from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_resource_access
from agentic_os.api.deps import get_session
from agentic_os.domain.plan_execution import (
    create_plan_execution,
    get_plan_execution_record,
)
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    Goal,
    GoalPlanningSession,
    PlanningAssignment,
    PlanningCandidate,
    PlanningCapabilityRequirement,
    Project,
    Task,
    TaskGraphRevision,
    User,
)
from agentic_os.domain.planning import (
    create_planning_session,
    get_planning_record,
    materialize_planning_graph_revision,
    record_planning_override,
    update_planning_session,
)
from agentic_os.domain.team_selection import (
    evaluate_planning_candidate,
    latest_team_agent_versions,
    rank_candidate_evaluations,
)

router = APIRouter(tags=["goal-planning"])

TERMINAL_PLANNING_STATUSES = {"rejected"}


class RequirementIn(BaseModel):
    capability_key: str
    required: bool = True
    rationale: str | None = None

    @field_validator("capability_key")
    @classmethod
    def _validate_capability_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("capability_key must not be empty")
        return value


class CandidateIn(BaseModel):
    agent_version_id: uuid.UUID


class AssignmentIn(BaseModel):
    assignment_key: str
    capability_key: str | None = None
    agent_version_id: uuid.UUID | None = None
    rationale: str | None = None

    @field_validator("assignment_key")
    @classmethod
    def _validate_assignment_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("assignment_key must not be empty")
        return value


class PlanningPreviewRequest(BaseModel):
    requirements: list[RequirementIn] = Field(default_factory=list)
    candidates: list[CandidateIn] = Field(default_factory=list)
    assignments: list[AssignmentIn] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requirements")
    @classmethod
    def _validate_requirements(cls, value: list[RequirementIn]) -> list[RequirementIn]:
        keys = [item.capability_key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate requirement capability_key values")
        return value

    @field_validator("candidates")
    @classmethod
    def _validate_candidates(cls, value: list[CandidateIn]) -> list[CandidateIn]:
        ids = [item.agent_version_id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate agent_version_id values")
        return value


class PlanningOverrideRequest(BaseModel):
    assignment_key: str
    agent_version_id: uuid.UUID
    reason: str

    @field_validator("assignment_key", "reason")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be empty")
        return value


class PlanningRequirementRead(BaseModel):
    id: uuid.UUID
    capability_key: str
    required: bool
    rationale: str | None
    source_evidence: dict[str, Any]


class PlanningCandidateRead(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    eligible: bool
    matched_capabilities: list[str]
    missing_capabilities: list[str]
    rejection_reasons: list[str]
    evidence: dict[str, Any]
    constraints_snapshot: dict[str, Any]


class PlanningAssignmentRead(BaseModel):
    id: uuid.UUID
    assignment_key: str
    requirement_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    selected_by: uuid.UUID | None
    rationale: str | None
    validation_status: str
    validation_evidence: dict[str, Any]


class PlanningOverrideRead(BaseModel):
    id: uuid.UUID
    assignment_id: uuid.UUID
    actor_id: uuid.UUID | None
    requested_candidate_id: uuid.UUID | None
    reason: str | None
    prior_candidate_evidence: dict[str, Any]
    validation_status: str
    validation_evidence: dict[str, Any]


class GoalPlanningSessionRead(BaseModel):
    id: uuid.UUID
    goal_id: uuid.UUID
    revision_number: int
    status: str
    validation_status: str
    constraints_snapshot: dict[str, Any]
    requirements: list[PlanningRequirementRead]
    candidates: list[PlanningCandidateRead]
    assignments: list[PlanningAssignmentRead]
    overrides: list[PlanningOverrideRead]


class PlanExecutionProgressRead(BaseModel):
    total: int
    pending: int
    running: int
    completed: int
    failed: int
    cancelled: int


class PlanTaskContextPackageRead(BaseModel):
    id: uuid.UUID
    planning_assignment_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID | None
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    context: dict[str, Any]


class PlanExecutionRead(BaseModel):
    id: uuid.UUID
    planning_session_id: uuid.UUID
    goal_id: uuid.UUID
    graph_revision_id: uuid.UUID
    created_by: uuid.UUID | None
    status: str
    progress: PlanExecutionProgressRead
    started_at: datetime | None
    completed_at: datetime | None
    task_context_packages: list[PlanTaskContextPackageRead]


class GoalPlanningAcceptRead(GoalPlanningSessionRead):
    materialized_tasks: list[dict[str, str]] = Field(default_factory=list)
    graph_revision_id: uuid.UUID | None = None
    graph_revision_number: int | None = None
    plan_execution: PlanExecutionRead | None = None


def _load_goal(session: Session, goal_id: uuid.UUID) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return goal


def _load_planning_session(
    session: Session, goal_id: uuid.UUID, planning_session_id: uuid.UUID
) -> GoalPlanningSession:
    planning = session.get(GoalPlanningSession, planning_session_id)
    if planning is None or planning.goal_id != goal_id:
        raise HTTPException(status_code=404, detail="planning session not found")
    return planning


def _evaluate_candidate(
    session: Session,
    *,
    project: Project,
    goal: Goal,
    agent: Agent,
    version: AgentVersion,
    required_capabilities: dict[str, bool],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    return evaluate_planning_candidate(
        session,
        project=project,
        goal=goal,
        agent=agent,
        version=version,
        required_capabilities=required_capabilities,
        constraints=constraints,
    )


def _planning_requirements(
    session: Session,
    *,
    goal: Goal,
    explicit: list[RequirementIn],
) -> list[dict[str, Any]]:
    if explicit:
        return [item.model_dump() for item in explicit]

    tasks = list(
        session.execute(
            select(Task)
            .where(Task.goal_id == goal.id)
            .order_by(Task.created_at, Task.id)
        ).scalars()
    )
    task_ids_by_capability: dict[str, list[str]] = {}
    for task in tasks:
        for capability_key, required in sorted(
            (task.required_capabilities or {}).items()
        ):
            if required:
                task_ids_by_capability.setdefault(capability_key, []).append(
                    str(task.id)
                )
    return [
        {
            "capability_key": capability_key,
            "required": True,
            "rationale": "Derived from persisted goal task requirements",
            "source_evidence": {
                "source": "goal_tasks",
                "task_ids": task_ids,
            },
        }
        for capability_key, task_ids in sorted(task_ids_by_capability.items())
    ]


def _planning_assignments(
    session: Session,
    *,
    goal: Goal,
    explicit: list[AssignmentIn],
) -> list[AssignmentIn]:
    if explicit:
        return explicit
    tasks = list(
        session.execute(
            select(Task)
            .where(Task.goal_id == goal.id)
            .order_by(Task.created_at, Task.id)
        ).scalars()
    )
    assignments: list[AssignmentIn] = []
    for task in tasks:
        required = sorted(
            name
            for name, value in (task.required_capabilities or {}).items()
            if value
        )
        if not required:
            continue
        assignments.append(
            AssignmentIn(
                assignment_key=str(task.id),
                capability_key=required[0],
                rationale=(
                    "Automatically formed from persisted task requirements; "
                    f"requires {', '.join(required)}"
                ),
            )
        )
    return assignments


def _assignment_requirements(
    session: Session,
    *,
    goal: Goal,
    assignment: AssignmentIn,
    planning_requirements: dict[str, bool],
) -> dict[str, bool]:
    try:
        task_id = uuid.UUID(assignment.assignment_key)
    except ValueError:
        task_id = None
    task = session.get(Task, task_id) if task_id else None
    if task is not None and task.goal_id == goal.id:
        task_requirements = {
            name: bool(value)
            for name, value in (task.required_capabilities or {}).items()
            if value
        }
        if task_requirements:
            return task_requirements
    if assignment.capability_key:
        return {assignment.capability_key: True}
    return planning_requirements


@router.post(
    "/goals/{goal_id}/planning-sessions",
    response_model=GoalPlanningSessionRead,
    status_code=201,
)
def preview_goal_plan(
    goal_id: uuid.UUID,
    payload: PlanningPreviewRequest,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict[str, Any]:
    goal = _load_goal(session, goal_id)
    project = require_resource_access(
        session,
        actor,
        goal,
        action="goal.planning.preview",
        resource_type="goal",
    )

    requirements_payload = _planning_requirements(
        session,
        goal=goal,
        explicit=payload.requirements,
    )
    if not requirements_payload:
        raise HTTPException(
            status_code=422,
            detail=(
                "planning requires explicit capability requirements or persisted "
                "goal tasks with required capabilities"
            ),
        )
    required_capabilities = {
        item["capability_key"]: item.get("required", True)
        for item in requirements_payload
    }
    requirement_keys = set(required_capabilities)
    assignment_inputs = _planning_assignments(
        session,
        goal=goal,
        explicit=payload.assignments,
    )
    requirements_by_assignment = {
        item.assignment_key: _assignment_requirements(
            session,
            goal=goal,
            assignment=item,
            planning_requirements=required_capabilities,
        )
        for item in assignment_inputs
    }

    candidate_rows: list[tuple[Agent, AgentVersion]] = []
    if payload.candidates:
        for candidate_in in payload.candidates:
            version = session.get(AgentVersion, candidate_in.agent_version_id)
            agent = session.get(Agent, version.agent_id) if version is not None else None
            if version is None or agent is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"agent version {candidate_in.agent_version_id} does not exist",
                )
            if agent.team_id != project.team_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"agent version {candidate_in.agent_version_id} is outside "
                        "goal project team"
                    ),
                )
            candidate_rows.append((agent, version))
    else:
        candidate_rows = latest_team_agent_versions(session, project.team_id)

    candidate_assignment_evaluations: dict[
        uuid.UUID, dict[str, dict[str, Any]]
    ] = {}
    candidates_payload: list[dict[str, Any]] = []
    for agent, version in candidate_rows:
        global_evaluation = _evaluate_candidate(
            session,
            project=project,
            goal=goal,
            agent=agent,
            version=version,
            required_capabilities=required_capabilities,
            constraints=payload.constraints,
        )
        assignment_evaluations = {
            assignment_key: _evaluate_candidate(
                session,
                project=project,
                goal=goal,
                agent=agent,
                version=version,
                required_capabilities=assignment_requirements,
                constraints=payload.constraints,
            )
            for assignment_key, assignment_requirements in requirements_by_assignment.items()
        }
        candidate_assignment_evaluations[version.id] = assignment_evaluations
        if assignment_evaluations:
            global_evaluation["eligible"] = any(
                item["eligible"] for item in assignment_evaluations.values()
            )
            if global_evaluation["eligible"]:
                global_evaluation["rejection_reasons"] = []
            else:
                global_evaluation["rejection_reasons"] = list(
                    dict.fromkeys(
                        reason
                        for item in assignment_evaluations.values()
                        for reason in item["rejection_reasons"]
                    )
                )
            global_evaluation["evidence"]["assignment_evaluations"] = {
                assignment_key: {
                    "eligible": item["eligible"],
                    "matched_capabilities": item["matched_capabilities"],
                    "missing_capabilities": item["missing_capabilities"],
                    "rejection_reasons": item["rejection_reasons"],
                }
                for assignment_key, item in assignment_evaluations.items()
            }
        candidates_payload.append(global_evaluation)
    candidates_payload = rank_candidate_evaluations(candidates_payload)
    candidate_eligibility: dict[uuid.UUID, dict[str, Any]] = {}
    for evaluation in candidates_payload:
        candidate_eligibility[evaluation["agent_version_id"]] = evaluation

    assignments_payload: list[dict[str, Any]] = []
    for assignment_in in assignment_inputs:
        if assignment_in.capability_key is not None and assignment_in.capability_key not in requirement_keys:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"assignment {assignment_in.assignment_key!r} references unknown requirement "
                    f"{assignment_in.capability_key!r}"
                ),
            )
        validation_status = "pending"
        validation_evidence: dict[str, Any] = {}
        selected_version_id = assignment_in.agent_version_id
        selected_evaluation = next(
            (
                item
                for item in candidates_payload
                if candidate_assignment_evaluations[item["agent_version_id"]][
                    assignment_in.assignment_key
                ]["eligible"]
            ),
            None,
        )
        automatically_selected = (
            selected_version_id is None and selected_evaluation is not None
        )
        if automatically_selected:
            selected_version_id = selected_evaluation["agent_version_id"]
        if selected_version_id is not None:
            candidate = candidate_eligibility.get(selected_version_id)
            if candidate is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"assignment {assignment_in.assignment_key!r} selects agent version "
                        f"{selected_version_id} which is not among the submitted candidates"
                    ),
                )
            evaluation = candidate_assignment_evaluations[selected_version_id][
                assignment_in.assignment_key
            ]
            validation_evidence = {
                "matched_capabilities": evaluation["matched_capabilities"],
                "missing_capabilities": evaluation["missing_capabilities"],
                "rejection_reasons": evaluation["rejection_reasons"],
            }
            if not evaluation["eligible"]:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"assignment {assignment_in.assignment_key!r} selects ineligible candidate "
                        f"{selected_version_id}: {evaluation['rejection_reasons']}"
                    ),
                )
            validation_status = "valid"
        assignments_payload.append(
            {
                "assignment_key": assignment_in.assignment_key,
                "capability_key": assignment_in.capability_key,
                "agent_version_id": selected_version_id,
                "rationale": (
                    (
                        f"{assignment_in.rationale}; " if assignment_in.rationale else ""
                    )
                    + (
                        "selected deterministically by capability coverage and "
                        "governance eligibility"
                        if automatically_selected
                        else ""
                    )
                ).strip("; "),
                "validation_status": validation_status,
                "validation_evidence": validation_evidence,
            }
        )

    all_assignments_resolved = bool(assignments_payload) and all(
        item["validation_status"] == "valid" for item in assignments_payload
    )
    try:
        planning = create_planning_session(
            session,
            goal_id=goal.id,
            actor_id=actor.id,
            requirements=requirements_payload,
            candidates=candidates_payload,
            assignments=assignments_payload,
            constraints=payload.constraints,
            status="previewed",
            validation_status="valid" if all_assignments_resolved else "pending",
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    record = get_planning_record(session, planning.id)
    assert record is not None
    return record


@router.get("/goals/{goal_id}/planning-sessions", response_model=list[GoalPlanningSessionRead])
def list_goal_plans(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict[str, Any]]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.planning.list", resource_type="goal")
    sessions = session.execute(
        select(GoalPlanningSession)
        .where(GoalPlanningSession.goal_id == goal_id)
        .order_by(GoalPlanningSession.revision_number)
    ).scalars()
    records = [get_planning_record(session, item.id) for item in sessions]
    return [record for record in records if record is not None]


@router.get(
    "/goals/{goal_id}/planning-sessions/{planning_session_id}",
    response_model=GoalPlanningSessionRead,
)
def get_goal_plan(
    goal_id: uuid.UUID,
    planning_session_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict[str, Any]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.planning.read", resource_type="goal")
    planning = _load_planning_session(session, goal_id, planning_session_id)
    record = get_planning_record(session, planning.id)
    assert record is not None
    return record


@router.post(
    "/goals/{goal_id}/planning-sessions/{planning_session_id}/overrides",
    response_model=GoalPlanningSessionRead,
    status_code=201,
)
def override_goal_plan_assignment(
    goal_id: uuid.UUID,
    planning_session_id: uuid.UUID,
    payload: PlanningOverrideRequest,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict[str, Any]:
    goal = _load_goal(session, goal_id)
    project = require_resource_access(
        session,
        actor,
        goal,
        action="goal.planning.override",
        resource_type="goal",
    )
    planning = _load_planning_session(session, goal_id, planning_session_id)
    if planning.status in TERMINAL_PLANNING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot override a planning session in status {planning.status!r}",
        )

    candidate = session.execute(
        select(PlanningCandidate).where(
            PlanningCandidate.planning_session_id == planning.id,
            PlanningCandidate.agent_version_id == payload.agent_version_id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(
            status_code=422,
            detail=f"agent version {payload.agent_version_id} is not a candidate in this planning session",
        )
    version = session.get(AgentVersion, payload.agent_version_id)
    agent = session.get(Agent, version.agent_id) if version is not None else None
    if version is None or agent is None:
        raise HTTPException(
            status_code=422,
            detail=f"agent version {payload.agent_version_id} does not exist",
        )

    assignment = session.execute(
        select(PlanningAssignment).where(
            PlanningAssignment.planning_session_id == planning.id,
            PlanningAssignment.assignment_key == payload.assignment_key,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(
            status_code=422,
            detail=f"planning assignment {payload.assignment_key!r} does not exist",
        )
    requirement = (
        session.get(PlanningCapabilityRequirement, assignment.requirement_id)
        if assignment.requirement_id
        else None
    )
    required_capabilities = (
        {requirement.capability_key: requirement.required}
        if requirement is not None
        else {
            item.capability_key: item.required
            for item in session.execute(
                select(PlanningCapabilityRequirement).where(
                    PlanningCapabilityRequirement.planning_session_id == planning.id
                )
            ).scalars()
        }
    )
    evaluation = _evaluate_candidate(
        session,
        project=project,
        goal=goal,
        agent=agent,
        version=version,
        required_capabilities=required_capabilities,
        constraints=planning.constraints_snapshot,
    )
    validation_status = "valid" if evaluation["eligible"] else "invalid"
    validation_evidence = {
        "matched_capabilities": evaluation["matched_capabilities"],
        "missing_capabilities": evaluation["missing_capabilities"],
        "rejection_reasons": evaluation["rejection_reasons"],
    }

    if validation_status == "valid":
        try:
            record_planning_override(
                session,
                planning_session_id=planning.id,
                assignment_key=payload.assignment_key,
                actor_id=actor.id,
                requested_agent_version_id=payload.agent_version_id,
                reason=payload.reason,
                validation_status="valid",
                validation_evidence=validation_evidence,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record = get_planning_record(session, planning.id)
        assert record is not None
        return record

    with Session(bind=session.get_bind()) as audit_session:
        try:
            record_planning_override(
                audit_session,
                planning_session_id=planning.id,
                assignment_key=payload.assignment_key,
                actor_id=actor.id,
                requested_agent_version_id=payload.agent_version_id,
                reason=payload.reason,
                validation_status="invalid",
                validation_evidence=validation_evidence,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        audit_session.commit()
    raise HTTPException(
        status_code=422,
        detail={
            "message": "override candidate is not eligible",
            "assignment_key": payload.assignment_key,
            "rejection_reasons": evaluation["rejection_reasons"],
        },
    )


@router.post(
    "/goals/{goal_id}/planning-sessions/{planning_session_id}/accept",
    response_model=GoalPlanningAcceptRead,
)
def accept_goal_plan(
    goal_id: uuid.UUID,
    planning_session_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict[str, Any]:
    goal = _load_goal(session, goal_id)
    require_resource_access(session, actor, goal, action="goal.planning.accept", resource_type="goal")
    planning = session.execute(
        select(GoalPlanningSession).where(GoalPlanningSession.id == planning_session_id).with_for_update()
    ).scalar_one_or_none()
    if planning is None or planning.goal_id != goal_id:
        raise HTTPException(status_code=404, detail="planning session not found")

    if planning.status == "accepted":
        record = get_planning_record(session, planning.id)
        assert record is not None
        record["materialized_tasks"] = []
        existing_revision = session.execute(
            select(TaskGraphRevision).where(
                TaskGraphRevision.planning_session_id == planning.id
            )
        ).scalar_one_or_none()
        record["graph_revision_id"] = existing_revision.id if existing_revision else None
        record["graph_revision_number"] = (
            existing_revision.revision_number if existing_revision else None
        )
        record["plan_execution"] = get_plan_execution_record(session, planning.id)
        return record
    if planning.status in TERMINAL_PLANNING_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot accept a planning session in status {planning.status!r}",
        )

    assignments = list(
        session.execute(
            select(PlanningAssignment).where(PlanningAssignment.planning_session_id == planning.id)
        ).scalars()
    )
    if not assignments:
        raise HTTPException(status_code=422, detail="planning session has no assignments to materialize")

    unresolved = [
        item.assignment_key
        for item in assignments
        if item.candidate_id is None or item.validation_status != "valid"
    ]
    if unresolved:
        raise HTTPException(
            status_code=422,
            detail=f"assignments not resolved to a valid candidate: {unresolved}",
        )

    materialized: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)
    for assignment in assignments:
        try:
            task_id = uuid.UUID(assignment.assignment_key)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"assignment_key {assignment.assignment_key!r} is not a task id; materialization "
                    "requires assignment_key to reference an existing task"
                ),
            ) from error
        task = session.get(Task, task_id)
        if task is None or task.goal_id != goal.id:
            raise HTTPException(
                status_code=422,
                detail=f"assignment {assignment.assignment_key!r} references a task outside this goal",
            )
        candidate = session.get(PlanningCandidate, assignment.candidate_id)
        if candidate is None:
            raise HTTPException(
                status_code=422,
                detail=f"assignment {assignment.assignment_key!r} references a missing candidate",
            )
        task.assigned_agent_version_id = candidate.agent_version_id
        task.assignment_status = "assigned"
        task.assignment_candidates = [
            {
                "agent_version_id": str(candidate.agent_version_id),
                "eligible": candidate.eligible,
                "matched_capabilities": candidate.matched_capabilities,
                "missing_capabilities": candidate.missing_capabilities,
            }
        ]
        task.assignment_rationale = {
            "source": "goal_planning_session",
            "planning_session_id": str(planning.id),
            "assignment_id": str(assignment.id),
            "rationale": assignment.rationale,
        }
        task.assignment_updated_at = now
        task.planning_session_id = planning.id
        task.planning_assignment_id = assignment.id
        materialized.append({"task_id": str(task.id), "assignment_key": assignment.assignment_key})

    update_planning_session(
        session,
        planning_session_id=planning.id,
        actor_id=actor.id,
        status="accepted",
        validation_status="valid",
    )
    session.flush()
    revision = materialize_planning_graph_revision(
        session,
        planning_session_id=planning.id,
        actor_id=actor.id,
        materialized_task_ids=[uuid.UUID(item["task_id"]) for item in materialized],
    )
    create_plan_execution(
        session,
        planning_session_id=planning.id,
        graph_revision_id=revision.id,
        actor_id=actor.id,
        task_ids=[uuid.UUID(item["task_id"]) for item in materialized],
    )
    record = get_planning_record(session, planning.id)
    assert record is not None
    record["materialized_tasks"] = materialized
    record["graph_revision_id"] = revision.id
    record["graph_revision_number"] = revision.revision_number
    record["plan_execution"] = get_plan_execution_record(session, planning.id)
    return record


@router.get(
    "/goals/{goal_id}/planning-sessions/{planning_session_id}/execution",
    response_model=PlanExecutionRead,
)
def read_goal_plan_execution(
    goal_id: uuid.UUID,
    planning_session_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> dict[str, Any]:
    goal = _load_goal(session, goal_id)
    require_resource_access(
        session,
        actor,
        goal,
        action="goal.planning.execution.read",
        resource_type="goal",
    )
    planning = _load_planning_session(session, goal_id, planning_session_id)
    record = get_plan_execution_record(session, planning.id)
    if record is None:
        raise HTTPException(status_code=404, detail="plan execution not found")
    return record
