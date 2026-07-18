from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_resource_access
from agentic_os.api.deps import get_session
from agentic_os.domain.assignment import match_capabilities
from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    Budget,
    CostLedgerEntry,
    Goal,
    GoalPlanningSession,
    McpServerTool,
    ModelProfile,
    ModelProfileVersion,
    PlanningAssignment,
    PlanningCandidate,
    PlanningCapabilityRequirement,
    Project,
    Task,
    User,
)
from agentic_os.domain.planning import (
    create_planning_session,
    get_planning_record,
    record_planning_override,
    update_planning_session,
)
from agentic_os.worker.policy import evaluate_policy

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
    requirements: list[RequirementIn]
    candidates: list[CandidateIn]
    assignments: list[AssignmentIn] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requirements")
    @classmethod
    def _validate_requirements(cls, value: list[RequirementIn]) -> list[RequirementIn]:
        if not value:
            raise ValueError("requirements must not be empty")
        keys = [item.capability_key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate requirement capability_key values")
        return value

    @field_validator("candidates")
    @classmethod
    def _validate_candidates(cls, value: list[CandidateIn]) -> list[CandidateIn]:
        if not value:
            raise ValueError("candidates must not be empty")
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


class GoalPlanningAcceptRead(GoalPlanningSessionRead):
    materialized_tasks: list[dict[str, str]] = Field(default_factory=list)


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


def _enabled_tool_names(session: Session, version: AgentVersion) -> set[str]:
    rows = session.execute(
        select(McpServerTool.tool_name)
        .join(
            AgentVersionMcpServer,
            AgentVersionMcpServer.mcp_server_version_id == McpServerTool.mcp_server_version_id,
        )
        .where(
            AgentVersionMcpServer.agent_version_id == version.id,
            McpServerTool.enabled.is_(True),
        )
    ).scalars()
    return set(rows)


def _model_capability_metadata(session: Session, version: AgentVersion) -> dict[str, Any]:
    if version.model_profile_version_id:
        model_version = session.get(ModelProfileVersion, version.model_profile_version_id)
        return dict(model_version.capability_metadata or {}) if model_version else {}
    if version.model_profile_id:
        model = session.get(ModelProfile, version.model_profile_id)
        return dict(model.capability_metadata or {}) if model else {}
    return {}


def _evaluate_candidate(
    session: Session,
    *,
    agent: Agent,
    version: AgentVersion,
    required_capabilities: dict[str, bool],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    matched, missing = match_capabilities(required_capabilities, version.capability_manifest or {})
    rejection_reasons = [f"missing_capability:{name}" for name in missing]

    agent_policy = evaluate_policy(session, scope_type="agent", scope_id=agent.id)
    if agent_policy in {"deny", "approval_required"}:
        rejection_reasons.append(f"agent_policy_{agent_policy}:{agent.id}")

    budget = None
    raw_budget_id = constraints.get("budget_id")
    if raw_budget_id:
        try:
            budget = session.get(Budget, uuid.UUID(str(raw_budget_id)))
        except (TypeError, ValueError):
            budget = None
        if budget is None:
            rejection_reasons.append(f"budget_not_found:{raw_budget_id}")
        elif budget.agent_id != agent.id:
            rejection_reasons.append(f"budget_belongs_to_other_agent:{budget.id}")
        elif budget.enforcement_mode == "hard_stop":
            consumed = session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            func.coalesce(
                                CostLedgerEntry.actual_amount_minor_units,
                                CostLedgerEntry.reserved_amount_minor_units,
                            )
                        ),
                        0,
                    )
                ).where(CostLedgerEntry.budget_id == budget.id, CostLedgerEntry.status != "void")
            ).scalar_one()
            if consumed >= budget.amount_minor_units:
                rejection_reasons.append(f"budget_exhausted:{budget.id}")

    required_tools = set(constraints.get("required_tools") or [])
    if required_tools:
        enabled_tool_names = _enabled_tool_names(session, version)
        for tool_name in sorted(required_tools):
            if tool_name not in enabled_tool_names:
                rejection_reasons.append(f"mcp_tool_disabled_or_missing:{tool_name}")

    required_model_capabilities = set(constraints.get("required_model_capabilities") or [])
    if required_model_capabilities:
        model_capability_metadata = _model_capability_metadata(session, version)
        for capability_name in sorted(required_model_capabilities):
            if not model_capability_metadata.get(capability_name):
                rejection_reasons.append(f"model_incompatible:{capability_name}")

    return {
        "agent_version_id": version.id,
        "eligible": not rejection_reasons,
        "matched_capabilities": matched,
        "missing_capabilities": missing,
        "rejection_reasons": rejection_reasons,
        "evidence": {
            "policy_decision": agent_policy,
            "budget_id": str(budget.id) if budget else None,
        },
    }


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
    project = require_resource_access(session, actor, goal, action="goal.planning.preview", resource_type="goal")

    required_capabilities = {item.capability_key: item.required for item in payload.requirements}
    requirement_keys = set(required_capabilities)

    candidates_payload: list[dict[str, Any]] = []
    candidate_eligibility: dict[uuid.UUID, dict[str, Any]] = {}
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
                detail=f"agent version {candidate_in.agent_version_id} is outside goal project team",
            )
        evaluation = _evaluate_candidate(
            session,
            agent=agent,
            version=version,
            required_capabilities=required_capabilities,
            constraints=payload.constraints,
        )
        candidate_eligibility[version.id] = evaluation
        candidates_payload.append(evaluation)

    assignments_payload: list[dict[str, Any]] = []
    for assignment_in in payload.assignments:
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
        if assignment_in.agent_version_id is not None:
            evaluation = candidate_eligibility.get(assignment_in.agent_version_id)
            if evaluation is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"assignment {assignment_in.assignment_key!r} selects agent version "
                        f"{assignment_in.agent_version_id} which is not among the submitted candidates"
                    ),
                )
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
                        f"{assignment_in.agent_version_id}: {evaluation['rejection_reasons']}"
                    ),
                )
            validation_status = "valid"
        assignments_payload.append(
            {
                "assignment_key": assignment_in.assignment_key,
                "capability_key": assignment_in.capability_key,
                "agent_version_id": assignment_in.agent_version_id,
                "rationale": assignment_in.rationale,
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
            requirements=[item.model_dump() for item in payload.requirements],
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
    require_resource_access(session, actor, goal, action="goal.planning.override", resource_type="goal")
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
        raise HTTPException(status_code=422, detail=f"agent version {payload.agent_version_id} does not exist")

    requirements = session.execute(
        select(PlanningCapabilityRequirement).where(
            PlanningCapabilityRequirement.planning_session_id == planning.id
        )
    ).scalars()
    required_capabilities = {item.capability_key: item.required for item in requirements}
    evaluation = _evaluate_candidate(
        session,
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
        materialized.append({"task_id": str(task.id), "assignment_key": assignment.assignment_key})

    update_planning_session(
        session,
        planning_session_id=planning.id,
        actor_id=actor.id,
        status="accepted",
        validation_status="valid",
    )
    session.flush()
    record = get_planning_record(session, planning.id)
    assert record is not None
    record["materialized_tasks"] = materialized
    return record
