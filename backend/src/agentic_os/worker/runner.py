from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Agent,
    AgentVersion,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Budget,
    CostLedgerEntry,
    Goal,
    McpServerVersion,
    Run,
    SkillVersion,
    Task,
)
from agentic_os.worker.leases import DEFAULT_LEASE_SECONDS, claim_ready_task, release_lease, renew_lease
from agentic_os.worker.policy import evaluate_policy
from agentic_os.worker.tools import invoke_tool


class TaskExecutionError(RuntimeError):
    """Raised when a claimed task cannot be executed to completion."""


def run_task_worker_once(session: Session, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Task | None:
    """Claim and execute at most one ready task.

    Returns the claimed task (completed or failed), or ``None`` if no task
    was eligible to claim. A task left ``running`` by a prior attempt whose
    lease expired is reconciled as an interrupted run before the new
    attempt starts, so restarts never silently duplicate a completed step.
    """
    task = claim_ready_task(session, worker_id, lease_seconds=lease_seconds)
    if task is None:
        return None

    goal = session.get(Goal, task.goal_id)
    project_id = goal.project_id if goal is not None else None

    _fail_interrupted_previous_attempt(session, task, project_id=project_id)

    try:
        _execute_claimed_task(session, task, worker_id, project_id=project_id)
    except Exception as error:
        # Reconcile a run created earlier in this same failed attempt so it
        # never lingers as "running" once the attempt has already failed.
        _fail_interrupted_previous_attempt(session, task, project_id=project_id)
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                event_type="task.failed",
                payload={"error": str(error)},
            )
        )
        release_lease(session, task, worker_id, status="failed")
        session.flush()
        raise
    return task


def _fail_interrupted_previous_attempt(session: Session, task: Task, *, project_id: uuid.UUID | None) -> None:
    stale_run = session.execute(
        select(Run)
        .where(Run.task_id == task.id, Run.status == "running")
        .order_by(Run.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if stale_run is None:
        return
    stale_run.status = "failed"
    stale_run.completed_at = datetime.now(timezone.utc)
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=stale_run.id,
            event_type="run.interrupted",
            payload={"reason": "lease expired before completion", "previous_lease_token": stale_run.lease_token},
        )
    )
    session.flush()


def _execute_claimed_task(session: Session, task: Task, worker_id: str, *, project_id: uuid.UUID | None) -> None:
    if project_id is None:
        raise TaskExecutionError(f"task {task.id} has no resolvable project through its goal")

    agent_version = session.get(AgentVersion, task.assigned_agent_version_id)
    if agent_version is None:
        raise TaskExecutionError(f"assigned agent version {task.assigned_agent_version_id} not found")
    agent = session.get(Agent, agent_version.agent_id)
    if agent is None:
        raise TaskExecutionError(f"agent {agent_version.agent_id} not found")

    capability_manifest = agent_version.capability_manifest or {}
    skill_version = _load_ref(session, SkillVersion, capability_manifest.get("skill_version_id"))
    mcp_server_version = _load_ref(session, McpServerVersion, capability_manifest.get("mcp_server_version_id"))
    budget = session.get(Budget, agent_version.default_budget_id) if agent_version.default_budget_id else None
    enabled_tools = list(capability_manifest.get("enabled_tools") or [])

    policy_decision = evaluate_policy(session, scope_type="agent", scope_id=agent.id)

    attempt_number = (
        session.execute(
            select(func.coalesce(func.max(Run.attempt_number), 0)).where(Run.task_id == task.id)
        ).scalar_one()
        + 1
    )
    idempotency_key = f"{task.id}:{attempt_number}"

    snapshot = {
        "agent_id": str(agent.id),
        "agent_version_id": str(agent_version.id),
        "agent_version_number": agent_version.version_number,
        "model_profile_id": str(agent_version.model_profile_id) if agent_version.model_profile_id else None,
        "default_budget_id": str(agent_version.default_budget_id) if agent_version.default_budget_id else None,
        "skill_version_id": str(skill_version.id) if skill_version else None,
        "skill_version_number": skill_version.version_number if skill_version else None,
        "mcp_server_version_id": str(mcp_server_version.id) if mcp_server_version else None,
        "mcp_server_version_number": mcp_server_version.version_number if mcp_server_version else None,
        "enabled_tools": enabled_tools,
        "policy_decision": policy_decision,
    }

    run = Run(
        task_id=task.id,
        attempt_number=attempt_number,
        idempotency_key=idempotency_key,
        lease_token=task.lease_token,
        agent_version_id=agent_version.id,
        status="running",
        snapshot=snapshot,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="run.started",
            payload={"attempt_number": attempt_number, "worker_id": worker_id},
        )
    )
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="policy.decision",
            payload={"scope_type": "agent", "scope_id": str(agent.id), "decision": policy_decision},
        )
    )
    session.flush()

    if policy_decision == "deny":
        raise TaskExecutionError(f"policy denied execution for agent {agent.id}")

    tool_results: dict[str, dict] = {}
    for tool_name in enabled_tools:
        result = invoke_tool(tool_name, {"task_id": str(task.id), "run_id": str(run.id)})
        tool_results[tool_name] = result
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="tool.invoked",
                payload={"tool": tool_name, "result": result},
            )
        )
        session.add(
            CostLedgerEntry(
                budget_id=budget.id if budget else None,
                run_id=run.id,
                action_type="mcp_tool_call",
                reserved_amount_minor_units=0,
                actual_amount_minor_units=0,
                currency=budget.currency if budget else "USD",
                is_zero_cost=True,
                status="reconciled",
            )
        )
    session.flush()

    if skill_version is not None:
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="skill.invoked",
                payload={"skill_version_id": str(skill_version.id), "content_ref": skill_version.content_ref},
            )
        )
    session.flush()

    renew_lease(session, task, worker_id)

    artifact_payload = json.dumps(
        {"task_id": str(task.id), "run_id": str(run.id), "tool_results": tool_results}, sort_keys=True
    )
    content_hash = "sha256:" + hashlib.sha256(artifact_payload.encode()).hexdigest()
    artifact = Artifact(
        project_id=project_id,
        goal_id=task.goal_id,
        task_id=task.id,
        run_id=run.id,
        name=f"{task.title} result",
    )
    session.add(artifact)
    session.flush()
    session.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            version_number=1,
            content_hash=content_hash,
            storage_ref=f"local://artifacts/{artifact.id}/v1.json",
        )
    )
    session.flush()

    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="run.completed",
            payload={"artifact_id": str(artifact.id)},
        )
    )
    session.flush()

    release_lease(session, task, worker_id, status="completed")


def _load_ref(session: Session, model: type, raw_id: object) -> object | None:
    if not raw_id:
        return None
    return session.get(model, uuid.UUID(str(raw_id)))
