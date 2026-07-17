from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.artifacts import (
    artifact_storage,
    consume_task_knowledge,
    create_artifact_version,
    record_output_citations,
    verify_artifact_version,
)
from agentic_os.domain.models import (
    Artifact,
    AuditEvent,
    Goal,
    Project,
    Run,
    Task,
)
from agentic_os.observability import CorrelationContext, record_observability
from agentic_os.worker.approvals import ensure_action_approvals
from agentic_os.worker.configuration import ConfigurationSnapshotError, resolve_run_configuration
from agentic_os.worker.controls import (
    GoalControlDecision,
    GoalControlInterrupt,
    complete_controlled_run,
    observe_goal_control,
    reconcile_goal_controls,
)
from agentic_os.worker.governance import (
    BudgetActionContext,
    BudgetExhaustedError,
    mark_action_cost_uncertain,
    reconcile_action_cost,
    release_action_cost,
    reserve_action_cost,
)
from agentic_os.worker.leases import DEFAULT_LEASE_SECONDS, claim_ready_task, release_lease, renew_lease
from agentic_os.worker.sandbox_execution import SandboxControlInterrupt, execute_task_sandbox
from agentic_os.worker.tools import invoke_tool
from agentic_os.worker.workspace import promote_workspace_changes


class TaskExecutionError(RuntimeError):
    """Raised when a claimed task cannot be executed to completion."""


def run_task_worker_once(
    session: Session,
    worker_id: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    on_run_started: Callable[[], None] | None = None,
    on_promoted: Callable[[], None] | None = None,
) -> Task | None:
    """Claim and execute at most one ready task.

    Returns the claimed task (completed or failed), or ``None`` if no task
    was eligible to claim. A task left ``running`` by a prior attempt whose
    lease expired is reconciled as an interrupted run before the new
    attempt starts, so restarts never silently duplicate a completed step.

    ``on_run_started``, when provided, is invoked once the new run has been
    durably committed with ``status="running"`` and before any further work
    happens. It exists so restart-recovery verification can pause a real
    worker process at a controlled, persisted mid-run point before killing
    it.

    ``on_promoted``, when provided, is invoked immediately after workspace
    promotion returns, while the promotion's resource-revision and lease
    mutations are still only staged in this transaction (not committed). It
    exists so failure-injection verification can pause a real worker process
    right after promotion runs but before the surrounding transaction
    commits, proving a kill in that window rolls the promotion back entirely
    rather than leaving a partially-applied revision.
    """
    reconcile_goal_controls(session)
    task = claim_ready_task(session, worker_id, lease_seconds=lease_seconds)
    if task is None:
        return None

    goal = session.get(Goal, task.goal_id)
    project_id = goal.project_id if goal is not None else None
    if goal is not None:
        decision = observe_goal_control(session, goal)
        if decision is not None:
            complete_controlled_run(
                session,
                goal=goal,
                task=task,
                run=None,
                worker_id=worker_id,
                project_id=project_id,
                decision=decision,
            )
            return task

    _fail_interrupted_previous_attempt(session, task, project_id=project_id)

    try:
        _execute_claimed_task(
            session,
            task,
            worker_id,
            project_id=project_id,
            on_run_started=on_run_started,
            on_promoted=on_promoted,
        )
    except GoalControlInterrupt as interrupt:
        run = session.execute(
            select(Run)
            .where(Run.task_id == task.id, Run.status == "running")
            .order_by(Run.attempt_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        complete_controlled_run(
            session,
            goal=goal,
            task=task,
            run=run,
            worker_id=worker_id,
            project_id=project_id,
            decision=interrupt.decision,
        )
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
    audit_event = AuditEvent(
        project_id=project_id,
        goal_id=task.goal_id,
        task_id=task.id,
        run_id=stale_run.id,
        event_type="run.interrupted",
        payload={
            "reason": "lease expired before completion",
            "previous_lease_token": stale_run.lease_token,
        },
    )
    session.add(audit_event)
    session.flush()
    goal = session.get(Goal, task.goal_id)
    project = session.get(Project, project_id) if project_id else None
    if goal is not None and project is not None:
        context = CorrelationContext.for_run(project=project, goal=goal, task=task, run=stale_run)
        record_observability(
            session,
            context,
            event_kind="run",
            operation_name="run.interrupted",
            status="failed",
            audit_event_id=audit_event.id,
            attributes={"reason": "lease expired before completion"},
        )


def _execute_claimed_task(
    session: Session,
    task: Task,
    worker_id: str,
    *,
    project_id: uuid.UUID | None,
    on_run_started: Callable[[], None] | None = None,
    on_promoted: Callable[[], None] | None = None,
) -> None:
    if project_id is None:
        raise TaskExecutionError(f"task {task.id} has no resolvable project through its goal")

    project = session.get(Project, project_id)
    if project is None:
        raise TaskExecutionError(f"project {project_id} not found")
    goal = session.get(Goal, task.goal_id)
    if goal is None:
        raise TaskExecutionError(f"goal {task.goal_id} not found")

    def _check_control() -> None:
        decision = observe_goal_control(session, goal)
        if decision is not None:
            raise GoalControlInterrupt(decision)

    attempt_number = (
        session.execute(
            select(func.coalesce(func.max(Run.attempt_number), 0)).where(Run.task_id == task.id)
        ).scalar_one()
        + 1
    )
    idempotency_key = f"{task.id}:{attempt_number}"

    run = Run(
        task_id=task.id,
        attempt_number=attempt_number,
        idempotency_key=idempotency_key,
        lease_token=task.lease_token,
        agent_version_id=task.assigned_agent_version_id,
        status="running",
        snapshot={"assigned_agent_version_id": str(task.assigned_agent_version_id)},
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    try:
        resolved = resolve_run_configuration(session, task=task, run=run, project=project)
    except ConfigurationSnapshotError as error:
        raise TaskExecutionError(str(error)) from error
    configuration = resolved.configuration
    agent = configuration["agent"]
    budget = resolved.budget
    enabled_tools = resolved.enabled_tools
    policy_decision = resolved.policy_decision
    policy_evaluations = resolved.policy_evaluations
    capability_manifest = agent["capability_manifest"]
    skills = configuration["skills"]
    mcp_servers = configuration["mcp_servers"]
    run.agent_version_id = uuid.UUID(agent["version_id"])

    snapshot = {
        "configuration_snapshot_id": str(resolved.snapshot_id),
        "agent_id": agent["id"],
        "agent_version_id": agent["version_id"],
        "agent_version_number": agent["version_number"],
        "model_profile_version_id": (
            configuration["model_profile"]["id"] if configuration["model_profile"] else None
        ),
        "default_budget_id": configuration["budget"]["id"] if configuration["budget"] else None,
        "skill_version_ids": [item["id"] for item in skills],
        "skill_version_id": skills[0]["id"] if len(skills) == 1 else None,
        "mcp_server_version_ids": [item["id"] for item in mcp_servers],
        "mcp_server_version_id": mcp_servers[0]["id"] if len(mcp_servers) == 1 else None,
        "enabled_tools": enabled_tools,
        "policy_decision": policy_decision,
        "policy_evaluations": policy_evaluations,
        "approval_configuration": resolved.approval_configuration,
        "assignment_rationale": configuration["assignment_rationale"],
        "capability_manifest": capability_manifest,
    }
    run.snapshot = snapshot
    session.flush()

    context = CorrelationContext.for_run(project=project, goal=goal, task=task, run=run)

    started_audit = AuditEvent(
        project_id=project_id,
        goal_id=task.goal_id,
        task_id=task.id,
        run_id=run.id,
        event_type="run.started",
        payload={
            "attempt_number": attempt_number,
            "worker_id": worker_id,
            "configuration_snapshot_id": str(resolved.snapshot_id),
            "agent_version_id": agent["version_id"],
            "model_profile_version_id": snapshot["model_profile_version_id"],
        },
    )
    session.add(started_audit)
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="policy.decision",
            payload={
                "decision": policy_decision,
                "evaluations": policy_evaluations,
                "configuration_snapshot_id": str(resolved.snapshot_id),
            },
        )
    )
    session.flush()
    record_observability(
        session,
        context,
        event_kind="run",
        operation_name="run.started",
        status="running",
        audit_event_id=started_audit.id,
        attributes={"attempt_number": attempt_number, "worker_id": worker_id},
    )
    record_observability(
        session,
        context,
        event_kind="task",
        operation_name="task.claimed",
        status="running",
        attributes={"lease_token": task.lease_token, "worker_id": worker_id},
    )

    if on_run_started is not None:
        # Commit so the run's "running" state is durably visible to other
        # connections (e.g. a verification harness polling for the process
        # to reach this point) before the callback potentially blocks. A
        # Resource exclusivity survives this commit through the durable
        # workspace resource-lease rows acquired with the task. The
        # transaction-scoped advisory locks used during claim are only race
        # guards and may be released here safely.
        record_observability(
            session,
            context,
            event_kind="checkpoint",
            operation_name="run.checkpoint.committed",
            status="committed",
            checkpoint_id=uuid.uuid4(),
            attributes={"boundary": "run_started", "attempt_number": attempt_number},
        )
        session.commit()
        on_run_started()
        _check_control()

    if policy_decision == "deny":
        raise TaskExecutionError(f"policy denied execution for agent {agent['id']}")

    approval_state, approval_requests = ensure_action_approvals(
        session,
        project=project,
        goal=goal,
        task=task,
        run=run,
        resolved=resolved,
    )
    run.snapshot = {
        **run.snapshot,
        "approval_request_ids": [str(request.id) for request in approval_requests],
    }
    for approval_request in approval_requests:
        record_observability(
            session,
            context,
            event_kind="approval",
            operation_name="approval.evaluated",
            status=approval_request.status,
            approval_request_id=approval_request.id,
            attributes={
                "action_type": approval_request.action_type,
                "policy_decision": policy_decision,
            },
        )
    if approval_state == "rejected":
        run.status = "failed"
        run.completed_at = datetime.now(timezone.utc)
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="run.approval_rejected",
                payload={"approval_request_ids": run.snapshot["approval_request_ids"]},
            )
        )
        release_lease(session, task, worker_id, status="failed")
        return

    if approval_state == "pending":
        run.status = "waiting_approval"
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="policy.approval_required",
                payload={
                    "decision": policy_decision,
                    "evaluations": policy_evaluations,
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                    "approval_request_ids": run.snapshot["approval_request_ids"],
                },
            )
        )
        session.flush()
        release_lease(session, task, worker_id, status="blocked")
        return

    storage = artifact_storage()
    consumed_knowledge = consume_task_knowledge(session, storage, task, run, project_id=project_id)

    default_currency = budget.currency if budget else "USD"
    budget_context = BudgetActionContext(
        team_id=project.team_id,
        project_id=project.id,
        goal_id=goal.id,
        task_id=task.id,
        run_id=run.id,
        agent_version_id=run.agent_version_id,
        requested_by=goal.created_by,
    )
    tool_results: dict[str, dict] = {}
    for tool_name in enabled_tools:
        _check_control()
        # Definition visibility and credential grants are mutable authority,
        # unlike the pinned configuration payload. Re-check them before any
        # budget reservation or external tool side effect so revocation fails
        # closed even when a retry reuses an older snapshot.
        resolved.validate_tool_access(session, tool_name=tool_name, project=project)
        descriptor = resolved.tool_descriptor(tool_name)
        pricing = descriptor.get("pricing") or {}
        chargeable = pricing.get("chargeable") is True
        amount_minor_units = (
            int(pricing["amount_minor_units"])
            if chargeable and pricing.get("amount_minor_units") is not None
            else None if chargeable else 0
        )
        currency = pricing.get("currency", default_currency)
        # Enter budget locking from a clean transaction. The claimed task,
        # run, snapshot, and any prior tool evidence are durable before the
        # per-budget lock is acquired, which gives concurrent workers one
        # consistent lock order and avoids task-row/budget-row deadlocks.
        session.commit()
        _check_control()
        try:
            cost = reserve_action_cost(
                session,
                budget=budget,
                context=budget_context,
                action_type="mcp_tool_call",
                amount_minor_units=amount_minor_units,
                currency=currency,
                pricing_evidence={
                    "tool": tool_name,
                    "chargeable": chargeable,
                    "declared_pricing": pricing,
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                },
            )
            record_observability(
                session,
                context,
                event_kind="budget",
                operation_name="budget.cost_reserved",
                status=cost.ledger_entry.status,
                cost_ledger_entry_id=cost.ledger_entry.id,
                attributes={
                    "action_type": "mcp_tool_call",
                    "tool": tool_name,
                    "amount_minor_units": amount_minor_units,
                    "currency": currency,
                },
            )
        except BudgetExhaustedError as error:
            session.add(
                AuditEvent(
                    project_id=project_id,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="budget.exhausted",
                    payload={
                        "tool": tool_name,
                        "budget_id": str(budget.id) if budget else None,
                        "reason": str(error),
                        "configuration_snapshot_id": str(resolved.snapshot_id),
                    },
                )
            )
            session.flush()
            raise TaskExecutionError(str(error)) from error

        if cost.warning_triggered:
            session.add(
                AuditEvent(
                    project_id=project_id,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="budget.warning_threshold",
                    payload={
                        "tool": tool_name,
                        "budget_id": str(budget.id) if budget else None,
                        "reservation_id": (
                            str(cost.reservation.id) if cost.reservation else None
                        ),
                        "configuration_snapshot_id": str(resolved.snapshot_id),
                    },
                )
            )
        if cost.override is not None:
            session.add(
                AuditEvent(
                    project_id=project_id,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="budget.override_applied",
                    payload={
                        "tool": tool_name,
                        "budget_id": str(budget.id) if budget else None,
                        "override_id": str(cost.override.id),
                        "actor_id": str(cost.override.created_by),
                        "reason": cost.override.reason,
                        "expires_at": cost.override.expires_at.isoformat(),
                    },
                )
            )
        session.flush()

        # The reservation is the durable pre-side-effect boundary. Committing
        # it releases the budget row lock so independent runs sharing a budget
        # can reserve remaining capacity and execute concurrently. A crash
        # after this commit leaves an active pessimistic reservation for
        # reconciliation instead of losing evidence or permitting overspend.
        record_observability(
            session,
            context,
            event_kind="checkpoint",
            operation_name="run.checkpoint.committed",
            status="committed",
            checkpoint_id=uuid.uuid4(),
            attributes={"boundary": "budget_reservation", "tool": tool_name},
        )
        session.commit()
        try:
            _check_control()
        except GoalControlInterrupt:
            release_action_cost(
                session,
                cost,
                reason="goal control interrupted execution before external side effect",
            )
            raise

        tool_call_id = uuid.uuid4()
        mcp_call_id = uuid.uuid4()
        try:
            result = invoke_tool(tool_name, {"task_id": str(task.id), "run_id": str(run.id)})
        except TimeoutError as error:
            record_observability(
                session,
                context,
                event_kind="mcp_call",
                operation_name="mcp.tool.invoked",
                status="failed",
                cost_ledger_entry_id=cost.ledger_entry.id,
                tool_call_id=tool_call_id,
                mcp_call_id=mcp_call_id,
                attributes={"tool": tool_name, "error_type": type(error).__name__},
            )
            mark_action_cost_uncertain(session, cost, reason=str(error))
            session.add(
                AuditEvent(
                    project_id=project_id,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    run_id=run.id,
                    event_type="budget.reconciliation_required",
                    payload={
                        "tool": tool_name,
                        "reservation_id": (
                            str(cost.reservation.id) if cost.reservation else None
                        ),
                        "reason": str(error),
                    },
                )
            )
            session.flush()
            raise TaskExecutionError(
                f"tool {tool_name!r} timed out with an uncertain external result"
            ) from error
        except Exception as error:
            record_observability(
                session,
                context,
                event_kind="mcp_call",
                operation_name="mcp.tool.invoked",
                status="failed",
                cost_ledger_entry_id=cost.ledger_entry.id,
                tool_call_id=tool_call_id,
                mcp_call_id=mcp_call_id,
                attributes={"tool": tool_name, "error_type": type(error).__name__},
            )
            release_action_cost(session, cost, reason=str(error))
            raise

        reconcile_action_cost(
            session,
            cost,
            actual_amount_minor_units=amount_minor_units or 0,
            evidence={"tool": tool_name},
        )
        record_observability(
            session,
            context,
            event_kind="mcp_call",
            operation_name="mcp.tool.invoked",
            status="completed",
            cost_ledger_entry_id=cost.ledger_entry.id,
            tool_call_id=tool_call_id,
            mcp_call_id=mcp_call_id,
            attributes={"tool": tool_name},
        )
        record_observability(
            session,
            context,
            event_kind="tool_call",
            operation_name="tool.invoked",
            status="completed",
            cost_ledger_entry_id=cost.ledger_entry.id,
            tool_call_id=tool_call_id,
            mcp_call_id=mcp_call_id,
            attributes={"tool": tool_name},
        )
        tool_results[tool_name] = result
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="tool.invoked",
                payload={
                    "tool": tool_name,
                    "result": result,
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                },
            )
        )
    session.flush()

    for skill in skills:
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="skill.invoked",
                payload={
                    "skill_version_id": skill["id"],
                    "content_ref": skill["content_ref"],
                    "configuration_snapshot_id": str(resolved.snapshot_id),
                },
            )
        )
    session.flush()

    sandbox_config = capability_manifest.get("sandbox")
    if sandbox_config:
        _check_control()
        sandbox_id = uuid.uuid4()
        try:
            sandbox_result = execute_task_sandbox(
                session,
                task,
                run.id,
                sandbox_config,
                project_id=project_id,
                control_check=lambda: (
                    (decision.action, decision.forced)
                    if (decision := observe_goal_control(session, goal)) is not None
                    else None
                ),
            )
        except SandboxControlInterrupt as interrupt:
            raise GoalControlInterrupt(
                GoalControlDecision(interrupt.action, forced=interrupt.forced)
            ) from interrupt
        if sandbox_result["exit_code"] != 0 or sandbox_result["timed_out"]:
            raise TaskExecutionError(
                f"sandbox execution for task {task.id} did not succeed: {sandbox_result}"
            )
        tool_results["sandbox"] = sandbox_result
        record_observability(
            session,
            context,
            event_kind="sandbox",
            operation_name="sandbox.execution",
            status="completed",
            sandbox_id=sandbox_id,
            attributes={
                "runtime": sandbox_config.get("runtime", "docker"),
                "exit_code": sandbox_result["exit_code"],
                "timed_out": sandbox_result["timed_out"],
            },
        )

    _check_control()
    renew_lease(session, task, worker_id)

    promote_workspace_changes(session, task, run, worker_id)
    if on_promoted is not None:
        on_promoted()
    _check_control()

    citation_summary = [
        {
            "source_artifact_id": str(item.source_artifact.id),
            "normalized_artifact_id": str(item.normalized_artifact.id),
            "citation_anchor": item.citation_anchor,
        }
        for item in consumed_knowledge
    ]
    artifact_payload = json.dumps(
        {
            "task_id": str(task.id),
            "run_id": str(run.id),
            "tool_results": tool_results,
            "citations": citation_summary,
        },
        sort_keys=True,
    )
    artifact = Artifact(
        project_id=project_id,
        goal_id=task.goal_id,
        task_id=task.id,
        run_id=run.id,
        name=f"{task.title} result",
        kind="output",
        content_type="application/json",
    )
    session.add(artifact)
    session.flush()
    artifact_version = create_artifact_version(
        session,
        storage,
        artifact,
        artifact_payload.encode(),
        version_number=1,
    )
    verify_artifact_version(storage, artifact_version)
    record_observability(
        session,
        context,
        event_kind="artifact",
        operation_name="artifact.output_published",
        status="completed",
        artifact_id=artifact.id,
        artifact_version_id=artifact_version.id,
        attributes={"kind": artifact.kind, "content_type": artifact.content_type},
    )

    citations = record_output_citations(session, task, run, artifact, consumed_knowledge)
    if citations:
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="artifact.output_published",
                payload={"artifact_id": str(artifact.id), "citation_count": len(citations)},
            )
        )
        session.flush()

    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    completed_audit = AuditEvent(
        project_id=project_id,
        goal_id=task.goal_id,
        task_id=task.id,
        run_id=run.id,
        event_type="run.completed",
        payload={"artifact_id": str(artifact.id)},
    )
    session.add(completed_audit)
    session.flush()
    record_observability(
        session,
        context,
        event_kind="run",
        operation_name="run.completed",
        status="completed",
        audit_event_id=completed_audit.id,
        artifact_id=artifact.id,
    )
    record_observability(
        session,
        context,
        event_kind="task",
        operation_name="task.completed",
        status="completed",
        artifact_id=artifact.id,
    )
    record_observability(
        session,
        context,
        event_kind="checkpoint",
        operation_name="run.checkpoint.committed",
        status="committed",
        checkpoint_id=uuid.uuid4(),
        attributes={"boundary": "run_completed"},
    )

    release_lease(session, task, worker_id, status="completed")
