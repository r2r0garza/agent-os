from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionPolicySet,
    AgentVersionSkill,
    AuditEvent,
    Budget,
    Goal,
    GoalPlanningSession,
    McpServerTool,
    McpServerVersion,
    ModelProfile,
    ModelProfileVersion,
    PlanningAssignment,
    PlanningCandidate,
    PlanningCapabilityRequirement,
    PlanningOverride,
    Policy,
    PolicySet,
    PolicySetVersion,
    Project,
    SkillVersion,
    User,
)
from agentic_os.redaction import redact_mapping


def create_planning_session(
    session: Session,
    *,
    goal_id: uuid.UUID,
    actor_id: uuid.UUID,
    requirements: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
    assignments: Iterable[dict[str, Any]] = (),
    constraints: dict[str, Any] | None = None,
    revision_number: int | None = None,
    status: str = "previewed",
    validation_status: str = "valid",
) -> GoalPlanningSession:
    """Stage a complete planning preview and its audit evidence atomically.

    The caller owns the surrounding transaction. A response must only be
    returned after that transaction commits.
    """
    goal, project = _goal_project(session, goal_id)
    if session.get(User, actor_id) is None:
        raise ValueError(f"planning actor {actor_id} does not exist")
    if revision_number is None:
        revision_number = (
            session.execute(
                select(func.coalesce(func.max(GoalPlanningSession.revision_number), 0)).where(
                    GoalPlanningSession.goal_id == goal_id
                )
            ).scalar_one()
            + 1
        )
    if revision_number < 1:
        raise ValueError("planning revision_number must be positive")

    record = GoalPlanningSession(
        goal_id=goal_id,
        created_by=actor_id,
        revision_number=revision_number,
        status=status,
        validation_status=validation_status,
        constraints_snapshot=redact_mapping(constraints or {}),
        accepted_at=datetime.now(UTC) if status == "accepted" else None,
    )
    session.add(record)
    session.flush()

    requirements_by_key: dict[str, PlanningCapabilityRequirement] = {}
    for item in requirements:
        capability_key = str(item.get("capability_key") or "").strip()
        if not capability_key:
            raise ValueError("planning requirement capability_key is required")
        if capability_key in requirements_by_key:
            raise ValueError(f"duplicate planning requirement: {capability_key}")
        requirement = PlanningCapabilityRequirement(
            planning_session_id=record.id,
            capability_key=capability_key,
            required=bool(item.get("required", True)),
            rationale=item.get("rationale"),
            source_evidence=redact_mapping(item.get("source_evidence") or {}),
        )
        session.add(requirement)
        session.flush()
        requirements_by_key[capability_key] = requirement

    candidates_by_version: dict[uuid.UUID, PlanningCandidate] = {}
    for item in candidates:
        agent_version_id = _as_uuid(item.get("agent_version_id"), "agent_version_id")
        version = session.get(AgentVersion, agent_version_id)
        agent = session.get(Agent, version.agent_id) if version is not None else None
        if version is None or agent is None:
            raise ValueError(f"agent version {agent_version_id} does not exist")
        if agent.team_id != project.team_id:
            raise ValueError(
                f"agent version {agent_version_id} is outside goal project team"
            )
        if agent_version_id in candidates_by_version:
            raise ValueError(f"duplicate planning candidate: {agent_version_id}")
        candidate = PlanningCandidate(
            planning_session_id=record.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            eligible=bool(item.get("eligible", False)),
            matched_capabilities=_string_list(item.get("matched_capabilities")),
            missing_capabilities=_string_list(item.get("missing_capabilities")),
            rejection_reasons=_string_list(item.get("rejection_reasons")),
            evidence=redact_mapping(item.get("evidence") or {}),
            constraints_snapshot=_candidate_constraints(
                session, goal=goal, project=project, agent=agent, version=version
            ),
        )
        session.add(candidate)
        session.flush()
        candidates_by_version[version.id] = candidate

    for item in assignments:
        assignment_key = str(item.get("assignment_key") or "").strip()
        if not assignment_key:
            raise ValueError("planning assignment_key is required")
        requirement_key = item.get("capability_key")
        requirement = requirements_by_key.get(str(requirement_key)) if requirement_key else None
        if requirement_key and requirement is None:
            raise ValueError(f"unknown planning requirement: {requirement_key}")
        candidate_version_id = item.get("agent_version_id")
        candidate = (
            candidates_by_version.get(
                _as_uuid(candidate_version_id, "agent_version_id")
            )
            if candidate_version_id
            else None
        )
        if candidate_version_id and candidate is None:
            raise ValueError(
                f"assignment candidate {candidate_version_id} is not in planning session"
            )
        session.add(
            PlanningAssignment(
                planning_session_id=record.id,
                assignment_key=assignment_key,
                requirement_id=requirement.id if requirement else None,
                candidate_id=candidate.id if candidate else None,
                selected_by=actor_id,
                rationale=item.get("rationale"),
                validation_status=item.get("validation_status", "pending"),
                validation_evidence=redact_mapping(
                    item.get("validation_evidence") or {}
                ),
            )
        )

    session.add(
        AuditEvent(
            project_id=project.id,
            goal_id=goal.id,
            event_type="goal.planning_session_created",
            payload={
                "planning_session_id": str(record.id),
                "revision_number": record.revision_number,
                "status": record.status,
                "actor_id": str(actor_id),
            },
        )
    )
    session.flush()
    return record


def record_planning_override(
    session: Session,
    *,
    planning_session_id: uuid.UUID,
    assignment_key: str,
    actor_id: uuid.UUID,
    requested_agent_version_id: uuid.UUID,
    reason: str,
    validation_status: str,
    validation_evidence: dict[str, Any] | None = None,
) -> PlanningOverride:
    planning = session.get(GoalPlanningSession, planning_session_id)
    if planning is None:
        raise ValueError(f"planning session {planning_session_id} does not exist")
    goal, project = _goal_project(session, planning.goal_id)
    if session.get(User, actor_id) is None:
        raise ValueError(f"planning override actor {actor_id} does not exist")
    assignment = session.execute(
        select(PlanningAssignment).where(
            PlanningAssignment.planning_session_id == planning.id,
            PlanningAssignment.assignment_key == assignment_key,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise ValueError(f"planning assignment {assignment_key!r} does not exist")
    candidate = session.execute(
        select(PlanningCandidate).where(
            PlanningCandidate.planning_session_id == planning.id,
            PlanningCandidate.agent_version_id == requested_agent_version_id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise ValueError("override candidate is not part of the planning session")
    prior = session.get(PlanningCandidate, assignment.candidate_id) if assignment.candidate_id else None
    override = PlanningOverride(
        planning_session_id=planning.id,
        assignment_id=assignment.id,
        actor_id=actor_id,
        requested_candidate_id=candidate.id,
        reason=reason,
        prior_candidate_evidence=(
            {
                "candidate_id": str(prior.id),
                "agent_version_id": str(prior.agent_version_id),
                "eligible": prior.eligible,
                "evidence": redact_mapping(prior.evidence),
            }
            if prior
            else {}
        ),
        validation_status=validation_status,
        validation_evidence=redact_mapping(validation_evidence or {}),
    )
    session.add(override)
    if validation_status == "valid":
        assignment.candidate_id = candidate.id
        assignment.selected_by = actor_id
        assignment.validation_status = "valid"
        assignment.validation_evidence = redact_mapping(validation_evidence or {})
    session.add(
        AuditEvent(
            project_id=project.id,
            goal_id=goal.id,
            event_type="goal.planning_override_recorded",
            payload={
                "planning_session_id": str(planning.id),
                "assignment_key": assignment_key,
                "actor_id": str(actor_id),
                "requested_agent_version_id": str(requested_agent_version_id),
                "validation_status": validation_status,
            },
        )
    )
    session.flush()
    return override


def update_planning_session(
    session: Session,
    *,
    planning_session_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: str,
    validation_status: str | None = None,
) -> GoalPlanningSession:
    planning = session.get(GoalPlanningSession, planning_session_id)
    if planning is None:
        raise ValueError(f"planning session {planning_session_id} does not exist")
    goal, project = _goal_project(session, planning.goal_id)
    if session.get(User, actor_id) is None:
        raise ValueError(f"planning actor {actor_id} does not exist")
    if status not in {"draft", "previewed", "accepted", "rejected"}:
        raise ValueError(f"unsupported planning status: {status}")
    if validation_status not in {None, "pending", "valid", "invalid"}:
        raise ValueError(f"unsupported planning validation status: {validation_status}")

    prior_status = planning.status
    planning.status = status
    if validation_status is not None:
        planning.validation_status = validation_status
    planning.accepted_at = datetime.now(UTC) if status == "accepted" else None
    session.add(
        AuditEvent(
            project_id=project.id,
            goal_id=goal.id,
            event_type="goal.planning_session_updated",
            payload={
                "planning_session_id": str(planning.id),
                "actor_id": str(actor_id),
                "prior_status": prior_status,
                "status": status,
                "validation_status": planning.validation_status,
            },
        )
    )
    session.flush()
    return planning


def get_planning_record(
    session: Session, planning_session_id: uuid.UUID
) -> dict[str, Any] | None:
    planning = session.get(GoalPlanningSession, planning_session_id)
    if planning is None:
        return None
    requirements = list(
        session.execute(
            select(PlanningCapabilityRequirement)
            .where(PlanningCapabilityRequirement.planning_session_id == planning.id)
            .order_by(PlanningCapabilityRequirement.created_at)
        ).scalars()
    )
    candidates = list(
        session.execute(
            select(PlanningCandidate)
            .where(PlanningCandidate.planning_session_id == planning.id)
            .order_by(PlanningCandidate.created_at)
        ).scalars()
    )
    assignments = list(
        session.execute(
            select(PlanningAssignment)
            .where(PlanningAssignment.planning_session_id == planning.id)
            .order_by(PlanningAssignment.created_at)
        ).scalars()
    )
    overrides = list(
        session.execute(
            select(PlanningOverride)
            .where(PlanningOverride.planning_session_id == planning.id)
            .order_by(PlanningOverride.created_at)
        ).scalars()
    )
    return redact_mapping(
        {
            "id": str(planning.id),
            "goal_id": str(planning.goal_id),
            "revision_number": planning.revision_number,
            "status": planning.status,
            "validation_status": planning.validation_status,
            "constraints_snapshot": planning.constraints_snapshot,
            "requirements": [
                {
                    "id": str(item.id),
                    "capability_key": item.capability_key,
                    "required": item.required,
                    "rationale": item.rationale,
                    "source_evidence": item.source_evidence,
                }
                for item in requirements
            ],
            "candidates": [
                {
                    "id": str(item.id),
                    "agent_id": str(item.agent_id),
                    "agent_version_id": str(item.agent_version_id),
                    "eligible": item.eligible,
                    "matched_capabilities": item.matched_capabilities,
                    "missing_capabilities": item.missing_capabilities,
                    "rejection_reasons": item.rejection_reasons,
                    "evidence": item.evidence,
                    "constraints_snapshot": item.constraints_snapshot,
                }
                for item in candidates
            ],
            "assignments": [
                {
                    "id": str(item.id),
                    "assignment_key": item.assignment_key,
                    "requirement_id": str(item.requirement_id) if item.requirement_id else None,
                    "candidate_id": str(item.candidate_id) if item.candidate_id else None,
                    "selected_by": str(item.selected_by) if item.selected_by else None,
                    "rationale": item.rationale,
                    "validation_status": item.validation_status,
                    "validation_evidence": item.validation_evidence,
                }
                for item in assignments
            ],
            "overrides": [
                {
                    "id": str(item.id),
                    "assignment_id": str(item.assignment_id),
                    "actor_id": str(item.actor_id) if item.actor_id else None,
                    "requested_candidate_id": (
                        str(item.requested_candidate_id)
                        if item.requested_candidate_id
                        else None
                    ),
                    "reason": item.reason,
                    "prior_candidate_evidence": item.prior_candidate_evidence,
                    "validation_status": item.validation_status,
                    "validation_evidence": item.validation_evidence,
                }
                for item in overrides
            ],
        }
    )


def _candidate_constraints(
    session: Session,
    *,
    goal: Goal,
    project: Project,
    agent: Agent,
    version: AgentVersion,
) -> dict[str, Any]:
    skill_rows = session.execute(
        select(AgentVersionSkill, SkillVersion)
        .join(SkillVersion, SkillVersion.id == AgentVersionSkill.skill_version_id)
        .where(AgentVersionSkill.agent_version_id == version.id)
        .order_by(AgentVersionSkill.created_at)
    ).all()
    mcp_rows = session.execute(
        select(AgentVersionMcpServer, McpServerVersion)
        .join(
            McpServerVersion,
            McpServerVersion.id == AgentVersionMcpServer.mcp_server_version_id,
        )
        .where(AgentVersionMcpServer.agent_version_id == version.id)
        .order_by(AgentVersionMcpServer.created_at)
    ).all()
    mcp_snapshots = []
    for attachment, server_version in mcp_rows:
        enabled_tools = list(
            session.execute(
                select(McpServerTool)
                .where(
                    McpServerTool.mcp_server_version_id == server_version.id,
                    McpServerTool.enabled.is_(True),
                )
                .order_by(McpServerTool.tool_name)
            ).scalars()
        )
        mcp_snapshots.append(
            {
                "mcp_server_version_id": str(server_version.id),
                "grant_id": str(attachment.id),
                "grant_config": redact_mapping(attachment.attachment_config or {}),
                "credential_configured": bool(
                    server_version.credential_id or server_version.credential_ciphertext
                ),
                "enabled_tools": [
                    {
                        "id": str(tool.id),
                        "name": tool.tool_name,
                        "schema_valid": tool.schema_valid,
                        "timeout_ms": tool.timeout_ms,
                        "output_limit_bytes": tool.output_limit_bytes,
                    }
                    for tool in enabled_tools
                ],
            }
        )

    model_snapshot: dict[str, Any] | None = None
    if version.model_profile_version_id:
        model_version = session.get(ModelProfileVersion, version.model_profile_version_id)
        if model_version:
            model_snapshot = {
                "model_profile_version_id": str(model_version.id),
                "model_identifier": model_version.model_identifier,
                "capability_metadata": redact_mapping(
                    model_version.capability_metadata or {}
                ),
                "pricing_metadata": redact_mapping(model_version.pricing_metadata or {}),
                "credential_configured": bool(model_version.credential_id),
            }
    elif version.model_profile_id:
        model = session.get(ModelProfile, version.model_profile_id)
        if model:
            model_snapshot = {
                "model_profile_id": str(model.id),
                "model_identifier": model.model_identifier,
                "capability_metadata": redact_mapping(model.capability_metadata or {}),
                "pricing_metadata": redact_mapping(model.pricing_metadata or {}),
                "credential_configured": bool(model.api_key_ciphertext),
            }

    budget = session.get(Budget, version.default_budget_id) if version.default_budget_id else None
    scope_filters = [
        (Policy.scope_type == "installation") & (Policy.scope_id.is_(None)),
        (Policy.scope_type == "team") & (Policy.scope_id == project.team_id),
        (Policy.scope_type == "project") & (Policy.scope_id == project.id),
        (Policy.scope_type == "goal") & (Policy.scope_id == goal.id),
        (Policy.scope_type == "agent") & (Policy.scope_id == agent.id),
    ]
    policies = list(
        session.execute(
            select(Policy).where(or_(*scope_filters)).order_by(Policy.created_at, Policy.id)
        ).scalars()
    )
    policy_set_rows = session.execute(
        select(AgentVersionPolicySet, PolicySetVersion, PolicySet)
        .join(
            PolicySetVersion,
            PolicySetVersion.id == AgentVersionPolicySet.policy_set_version_id,
        )
        .join(PolicySet, PolicySet.id == PolicySetVersion.policy_set_id)
        .where(AgentVersionPolicySet.agent_version_id == version.id)
        .order_by(AgentVersionPolicySet.created_at)
    ).all()

    return redact_mapping(
        {
            "agent_manifest": version.capability_manifest or {},
            "skills": [
                {
                    "skill_version_id": str(skill_version.id),
                    "grant_id": str(attachment.id),
                    "declared_capabilities": skill_version.declared_capabilities,
                    "resources": skill_version.resources,
                    "grant_config": attachment.attachment_config or {},
                }
                for attachment, skill_version in skill_rows
            ],
            "mcp_servers": mcp_snapshots,
            "model": model_snapshot,
            "budget": (
                {
                    "id": str(budget.id),
                    "currency": budget.currency,
                    "amount_minor_units": budget.amount_minor_units,
                    "enforcement_mode": budget.enforcement_mode,
                    "warning_threshold_percent": budget.warning_threshold_percent,
                }
                if budget
                else None
            ),
            "policies": [
                {
                    "id": str(policy.id),
                    "scope_type": policy.scope_type,
                    "scope_id": str(policy.scope_id) if policy.scope_id else None,
                    "decision": policy.decision,
                    "rule": policy.rule,
                }
                for policy in policies
            ],
            "policy_sets": [
                {
                    "policy_set_id": str(policy_set.id),
                    "policy_set_version_id": str(policy_version.id),
                    "version_number": policy_version.version_number,
                    "rules": policy_version.rules,
                }
                for _attachment, policy_version, policy_set in policy_set_rows
            ],
        }
    )


def _goal_project(session: Session, goal_id: uuid.UUID) -> tuple[Goal, Project]:
    goal = session.get(Goal, goal_id)
    project = session.get(Project, goal.project_id) if goal is not None else None
    if goal is None or project is None:
        raise ValueError(f"goal {goal_id} has no resolvable project")
    return goal, project


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a UUID") from error


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("planning evidence lists must be arrays")
    return [str(item) for item in value]
