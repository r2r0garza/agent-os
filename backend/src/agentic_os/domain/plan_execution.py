from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    AgentVersion,
    AuditEvent,
    Goal,
    GoalPlanExecution,
    GoalPlanningSession,
    PlanTaskContextPackage,
    PlanningAssignment,
    PlanningCandidate,
    Run,
    Task,
    TaskDependency,
    TaskGraphRevision,
)
from agentic_os.redaction import redact_mapping


def create_plan_execution(
    session: Session,
    *,
    planning_session_id: uuid.UUID,
    graph_revision_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_ids: Iterable[uuid.UUID],
) -> GoalPlanExecution:
    """Create the durable execution envelope for one accepted plan."""
    existing = session.execute(
        select(GoalPlanExecution).where(
            GoalPlanExecution.planning_session_id == planning_session_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    planning = session.get(GoalPlanningSession, planning_session_id)
    revision = session.get(TaskGraphRevision, graph_revision_id)
    if planning is None or planning.status != "accepted":
        raise ValueError("plan execution requires an accepted planning session")
    if revision is None or revision.planning_session_id != planning.id:
        raise ValueError("plan execution graph revision does not match planning session")

    tasks = list(
        session.execute(
            select(Task)
            .where(Task.id.in_(list(task_ids)), Task.planning_session_id == planning.id)
            .order_by(Task.created_at, Task.id)
        ).scalars()
    )
    if not tasks:
        raise ValueError("plan execution requires at least one materialized task")

    execution = GoalPlanExecution(
        planning_session_id=planning.id,
        goal_id=planning.goal_id,
        graph_revision_id=revision.id,
        created_by=actor_id,
        status="pending",
        total_tasks=len(tasks),
        pending_tasks=len(tasks),
    )
    session.add(execution)
    session.flush()

    task_ids_set = {task.id for task in tasks}
    dependencies = list(
        session.execute(
            select(TaskDependency).where(TaskDependency.task_id.in_(task_ids_set))
        ).scalars()
    )
    upstream_by_task: dict[uuid.UUID, list[str]] = {}
    for dependency in dependencies:
        upstream_by_task.setdefault(dependency.task_id, []).append(
            str(dependency.depends_on_task_id)
        )

    for task in tasks:
        if task.planning_assignment_id is None or task.assigned_agent_version_id is None:
            raise ValueError(f"materialized task {task.id} has no pinned planning assignment")
        assignment = session.get(PlanningAssignment, task.planning_assignment_id)
        version = session.get(AgentVersion, task.assigned_agent_version_id)
        candidate = (
            session.get(PlanningCandidate, assignment.candidate_id)
            if assignment is not None and assignment.candidate_id is not None
            else None
        )
        if assignment is None or version is None or candidate is None:
            raise ValueError(f"materialized task {task.id} has incomplete assignment evidence")

        context = redact_mapping(
            {
                "planning": {
                    "planning_session_id": str(planning.id),
                    "planning_revision_number": planning.revision_number,
                    "graph_revision_id": str(revision.id),
                    "graph_revision_number": revision.revision_number,
                },
                "assignment": {
                    "planning_assignment_id": str(assignment.id),
                    "assignment_key": assignment.assignment_key,
                    "selected_by": str(assignment.selected_by) if assignment.selected_by else None,
                    "rationale": assignment.rationale,
                    "validation_status": assignment.validation_status,
                    "validation_evidence": assignment.validation_evidence,
                    "agent_id": str(version.agent_id),
                    "agent_version_id": str(version.id),
                    "candidate_id": str(candidate.id),
                },
                "task": {
                    "task_id": str(task.id),
                    "title": task.title,
                    "description": task.description,
                    "required_capabilities": task.required_capabilities,
                    "capability_rationale": task.capability_rationale,
                    "expected_outputs": task.expected_outputs,
                    "resource_intent": task.resource_intent,
                    "knowledge_artifact_ids": task.knowledge_artifact_ids,
                    "dependency_task_ids": sorted(upstream_by_task.get(task.id, [])),
                    "dependency_rationale": task.capability_rationale.get(
                        "dependencies", []
                    ),
                },
                "agent_context": candidate.constraints_snapshot,
                "policy_context": {
                    "task_policy_ids": task.policy_ids,
                    "resolved_policies": candidate.constraints_snapshot.get("policies", []),
                    "resolved_policy_sets": candidate.constraints_snapshot.get(
                        "policy_sets", []
                    ),
                },
                "budget_context": {
                    "task_budget_id": str(task.budget_id) if task.budget_id else None,
                    "agent_default_budget": candidate.constraints_snapshot.get("budget"),
                },
            }
        )
        session.add(
            PlanTaskContextPackage(
                plan_execution_id=execution.id,
                planning_session_id=planning.id,
                planning_assignment_id=assignment.id,
                task_id=task.id,
                agent_id=version.agent_id,
                agent_version_id=version.id,
                context=context,
            )
        )

    goal = session.get(Goal, planning.goal_id)
    session.add(
        AuditEvent(
            project_id=goal.project_id if goal is not None else None,
            goal_id=planning.goal_id,
            event_type="goal.plan_execution_created",
            payload={
                "plan_execution_id": str(execution.id),
                "planning_session_id": str(planning.id),
                "graph_revision_id": str(revision.id),
                "actor_id": str(actor_id),
                "status": execution.status,
                "task_count": len(tasks),
            },
        )
    )
    session.flush()
    return execution


def refresh_plan_execution_progress(
    session: Session, plan_execution_id: uuid.UUID
) -> GoalPlanExecution:
    """Recompute persisted plan progress from current task and run state."""
    execution = session.get(GoalPlanExecution, plan_execution_id)
    if execution is None:
        raise ValueError(f"plan execution {plan_execution_id} does not exist")
    packages = list(
        session.execute(
            select(PlanTaskContextPackage).where(
                PlanTaskContextPackage.plan_execution_id == execution.id
            )
        ).scalars()
    )
    task_ids = [package.task_id for package in packages]
    tasks = (
        list(session.execute(select(Task).where(Task.id.in_(task_ids))).scalars())
        if task_ids
        else []
    )
    counts = Counter(task.status for task in tasks)
    execution.total_tasks = len(tasks)
    execution.pending_tasks = counts["pending"] + counts["ready"] + counts["blocked"]
    execution.running_tasks = counts["running"]
    execution.completed_tasks = counts["completed"]
    execution.failed_tasks = counts["failed"]
    execution.cancelled_tasks = counts["cancelled"]

    prior_status = execution.status
    if execution.failed_tasks:
        execution.status = "failed"
    elif execution.cancelled_tasks:
        execution.status = "cancelled"
    elif execution.total_tasks and execution.completed_tasks == execution.total_tasks:
        execution.status = "completed"
    elif execution.running_tasks or execution.completed_tasks:
        execution.status = "running"
    else:
        execution.status = "pending"
    now = datetime.now(UTC)
    if execution.status == "running" and execution.started_at is None:
        execution.started_at = now
    if execution.status in {"completed", "failed", "cancelled"}:
        execution.completed_at = execution.completed_at or now

    latest_runs = {}
    if task_ids:
        runs = list(
            session.execute(
                select(Run)
                .where(Run.task_id.in_(task_ids))
                .order_by(Run.task_id, Run.attempt_number.desc())
            ).scalars()
        )
        for run in runs:
            latest_runs.setdefault(run.task_id, run.id)
    for package in packages:
        package.run_id = latest_runs.get(package.task_id)

    if execution.status != prior_status:
        goal = session.get(Goal, execution.goal_id)
        session.add(
            AuditEvent(
                project_id=goal.project_id if goal is not None else None,
                goal_id=execution.goal_id,
                event_type="goal.plan_execution_status_updated",
                payload={
                    "plan_execution_id": str(execution.id),
                    "prior_status": prior_status,
                    "status": execution.status,
                    "attribution": "system:progress_aggregation",
                },
            )
        )
    session.flush()
    return execution


def get_plan_execution_record(
    session: Session, planning_session_id: uuid.UUID
) -> dict[str, Any] | None:
    execution = session.execute(
        select(GoalPlanExecution).where(
            GoalPlanExecution.planning_session_id == planning_session_id
        )
    ).scalar_one_or_none()
    if execution is None:
        return None
    refresh_plan_execution_progress(session, execution.id)
    packages = list(
        session.execute(
            select(PlanTaskContextPackage)
            .where(PlanTaskContextPackage.plan_execution_id == execution.id)
            .order_by(PlanTaskContextPackage.created_at, PlanTaskContextPackage.id)
        ).scalars()
    )
    return redact_mapping(
        {
            "id": str(execution.id),
            "planning_session_id": str(execution.planning_session_id),
            "goal_id": str(execution.goal_id),
            "graph_revision_id": str(execution.graph_revision_id),
            "created_by": str(execution.created_by) if execution.created_by else None,
            "status": execution.status,
            "progress": {
                "total": execution.total_tasks,
                "pending": execution.pending_tasks,
                "running": execution.running_tasks,
                "completed": execution.completed_tasks,
                "failed": execution.failed_tasks,
                "cancelled": execution.cancelled_tasks,
            },
            "started_at": execution.started_at,
            "completed_at": execution.completed_at,
            "task_context_packages": [
                {
                    "id": str(package.id),
                    "planning_assignment_id": str(package.planning_assignment_id),
                    "task_id": str(package.task_id),
                    "run_id": str(package.run_id) if package.run_id else None,
                    "agent_id": str(package.agent_id),
                    "agent_version_id": str(package.agent_version_id),
                    "context": package.context,
                }
                for package in packages
            ],
        }
    )
