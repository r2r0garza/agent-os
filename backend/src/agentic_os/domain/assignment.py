from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    AuditEvent,
    Budget,
    CostLedgerEntry,
    Goal,
    Policy,
    Project,
    Task,
)
from agentic_os.worker.policy import evaluate_policy


def match_capabilities(required: dict[str, Any], manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return explicitly matched and missing required capability names."""
    required_names = sorted(name for name, required_value in required.items() if required_value)
    declared = set(manifest.get("capabilities") or [])
    return (
        [name for name in required_names if name in declared],
        [name for name in required_names if name not in declared],
    )


def assign_task(session: Session, task: Task) -> Task:
    """Select one eligible latest agent version and persist inspectable evidence."""
    goal = session.get(Goal, task.goal_id)
    project = session.get(Project, goal.project_id) if goal is not None else None
    if goal is None or project is None:
        raise ValueError(f"task {task.id} has no resolvable project")

    task_policy_reasons = _task_policy_reasons(session, task)
    versions = _latest_agent_versions(session, project.team_id)
    candidates = [
        _evaluate_candidate(session, task, agent, version, task_policy_reasons)
        for agent, version in versions
    ]
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    selected = eligible[0] if eligible else None

    task.assigned_agent_version_id = uuid.UUID(selected["agent_version_id"]) if selected else None
    blocked_candidate = any(
        not candidate["missing_capabilities"] and candidate["rejection_reasons"] for candidate in candidates
    )
    task.assignment_status = (
        "assigned"
        if selected
        else ("blocked" if task_policy_reasons or blocked_candidate else "no_eligible_agent")
    )
    task.assignment_candidates = candidates
    task.assignment_rationale = {
        "strategy": "latest-version-per-agent, then agent creation order",
        "required_capabilities": sorted(name for name, value in task.required_capabilities.items() if value),
        "selected_agent_version_id": selected["agent_version_id"] if selected else None,
        "reason": (
            "selected the first eligible latest agent version using explicit capability metadata"
            if selected
            else "no candidate satisfied capability, policy, and budget requirements"
        ),
        "task_blocking_reasons": task_policy_reasons,
    }
    task.assignment_updated_at = datetime.now(timezone.utc)
    session.add(
        AuditEvent(
            project_id=project.id,
            goal_id=goal.id,
            task_id=task.id,
            event_type="task.assignment_evaluated",
            payload={
                "status": task.assignment_status,
                "selected_agent_version_id": selected["agent_version_id"] if selected else None,
                "candidate_count": len(candidates),
            },
        )
    )
    session.flush()
    return task


def _latest_agent_versions(session: Session, team_id: uuid.UUID) -> list[tuple[Agent, AgentVersion]]:
    agents = list(
        session.execute(
            select(Agent).where(Agent.team_id == team_id).order_by(Agent.created_at, Agent.id)
        ).scalars()
    )
    latest: list[tuple[Agent, AgentVersion]] = []
    for agent in agents:
        version = session.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        if version is not None:
            latest.append((agent, version))
    return latest


def _task_policy_reasons(session: Session, task: Task) -> list[str]:
    reasons: list[str] = []
    for raw_policy_id in task.policy_ids or []:
        policy = session.get(Policy, uuid.UUID(str(raw_policy_id)))
        if policy is None:
            reasons.append(f"task_policy_not_found:{raw_policy_id}")
        elif policy.decision in {"deny", "approval_required"}:
            reasons.append(f"task_policy_{policy.decision}:{policy.id}")
    return reasons


def _evaluate_candidate(
    session: Session,
    task: Task,
    agent: Agent,
    version: AgentVersion,
    task_policy_reasons: list[str],
) -> dict[str, Any]:
    matched, missing = match_capabilities(task.required_capabilities or {}, version.capability_manifest or {})
    rejection_reasons = [f"missing_capability:{name}" for name in missing]
    rejection_reasons.extend(task_policy_reasons)

    agent_policy = evaluate_policy(session, scope_type="agent", scope_id=agent.id)
    if agent_policy in {"deny", "approval_required"}:
        rejection_reasons.append(f"agent_policy_{agent_policy}:{agent.id}")

    budget_id = task.budget_id or version.default_budget_id
    budget = session.get(Budget, budget_id) if budget_id else None
    if budget is not None and budget.agent_id != agent.id:
        budget_source = "task" if task.budget_id is not None else "default"
        rejection_reasons.append(f"{budget_source}_budget_belongs_to_other_agent:{budget.id}")
    if budget is not None and budget.enforcement_mode == "hard_stop":
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

    return {
        "agent_id": str(agent.id),
        "agent_version_id": str(version.id),
        "agent_version_number": version.version_number,
        "eligible": not rejection_reasons,
        "matched_capabilities": matched,
        "missing_capabilities": missing,
        "policy_decision": agent_policy,
        "budget_id": str(budget.id) if budget else None,
        "rejection_reasons": rejection_reasons,
    }
