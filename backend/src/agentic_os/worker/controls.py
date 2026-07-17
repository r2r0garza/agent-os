from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    AuditEvent,
    Goal,
    GoalLifecycleCommand,
    GoalLifecycleEvent,
    Run,
    Task,
)
from agentic_os.worker.workspace import release_resource_leases


@dataclass(frozen=True)
class GoalControlDecision:
    action: str
    forced: bool = False


class GoalControlInterrupt(RuntimeError):
    """Raised at a safe boundary when a goal no longer permits execution."""

    def __init__(self, decision: GoalControlDecision) -> None:
        super().__init__(f"goal execution interrupted by {decision.action}")
        self.decision = decision


def observe_goal_control(session: Session, goal: Goal) -> GoalControlDecision | None:
    """Refresh goal control state and return the currently effective interrupt."""
    session.refresh(
        goal,
        attribute_names=[
            "status",
            "pending_control",
            "control_version",
            "cancellation_grace_expires_at",
            "forced_termination_requested_at",
            "forced_termination_completed_at",
        ],
    )
    if goal.status == "paused":
        return GoalControlDecision("pause")
    if goal.status == "cancelled":
        deadline = goal.cancellation_grace_expires_at
        forced = deadline is not None and deadline <= datetime.now(timezone.utc)
        return GoalControlDecision("cancel", forced=forced)
    return None


def reconcile_goal_controls(session: Session) -> None:
    """Apply durable controls to work that is not owned by a live worker.

    This runs before every claim. It prevents paused/cancelled goals from
    leaving expired attempts permanently in ``running`` after a restart and
    cancels queued work for cancelled goals without rewriting attempt history.
    """
    now = datetime.now(timezone.utc)
    goals = list(
        session.execute(
            select(Goal).where(
                Goal.pending_control.is_not(None),
                Goal.status.in_(("active", "paused", "cancelled")),
            )
        ).scalars()
    )
    for goal in goals:
        if goal.status == "active":
            # Resume is effective as soon as the durable state is active.
            if goal.pending_control == "resume":
                _acknowledge_control(session, goal, action="resume", forced=False)
            continue

        if goal.status == "cancelled":
            session.execute(
                update(Task)
                .where(
                    Task.goal_id == goal.id,
                    Task.status.in_(("pending", "ready", "blocked")),
                )
                .values(status="cancelled", lease_owner=None, lease_expires_at=None)
            )

        expired_tasks = list(
            session.execute(
                select(Task).where(
                    Task.goal_id == goal.id,
                    Task.status == "running",
                    Task.lease_expires_at < now,
                )
            ).scalars()
        )
        for task in expired_tasks:
            lease_owner = task.lease_owner
            run = session.execute(
                select(Run)
                .where(Run.task_id == task.id, Run.status == "running")
                .order_by(Run.attempt_number.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run is not None:
                run.status = "cancelled"
                run.completed_at = now
                session.add(
                    AuditEvent(
                        project_id=goal.project_id,
                        goal_id=goal.id,
                        task_id=task.id,
                        run_id=run.id,
                        event_type="run.control_recovered",
                        payload={"action": goal.pending_control, "reason": "expired worker lease"},
                    )
                )
            task.status = "pending" if goal.status == "paused" else "cancelled"
            if lease_owner is not None:
                release_resource_leases(session, task, lease_owner)
            task.lease_owner = None
            task.lease_expires_at = None

        active_tasks = session.execute(
            select(Task.id)
            .where(
                Task.goal_id == goal.id,
                Task.status == "running",
                Task.lease_expires_at >= now,
            )
            .limit(1)
        ).first()
        if active_tasks is None:
            forced = (
                goal.status == "cancelled"
                and goal.cancellation_grace_expires_at is not None
                and goal.cancellation_grace_expires_at <= now
            )
            _acknowledge_control(
                session,
                goal,
                action="pause" if goal.status == "paused" else "cancel",
                forced=forced,
            )
    session.flush()


def complete_controlled_run(
    session: Session,
    *,
    goal: Goal,
    task: Task,
    run: Run | None,
    worker_id: str,
    project_id: uuid.UUID | None,
    decision: GoalControlDecision,
) -> None:
    """Persist a cooperative/forced interruption without classifying it as failure."""
    now = datetime.now(timezone.utc)
    if run is not None:
        run.status = "cancelled"
        run.completed_at = now
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=goal.id,
            task_id=task.id,
            run_id=run.id if run is not None else None,
            event_type=f"run.{decision.action}_acknowledged",
            payload={
                "worker_id": worker_id,
                "control_version": goal.control_version,
                "forced": decision.forced,
            },
        )
    )
    if task.lease_owner == worker_id:
        release_resource_leases(session, task, worker_id)
    task.status = "pending" if decision.action == "pause" else "cancelled"
    task.lease_owner = None
    task.lease_expires_at = None

    if decision.action == "cancel" and decision.forced:
        _mark_forced_termination(session, goal, now)

    other_active_task = session.execute(
        select(Task.id)
        .where(Task.goal_id == goal.id, Task.status == "running", Task.id != task.id)
        .limit(1)
    ).first()
    if other_active_task is None:
        _acknowledge_control(
            session,
            goal,
            action=decision.action,
            forced=decision.forced,
        )
    session.flush()


def _mark_forced_termination(session: Session, goal: Goal, now: datetime) -> None:
    if goal.forced_termination_requested_at is None:
        goal.forced_termination_requested_at = now
    goal.forced_termination_completed_at = now
    command = session.execute(
        select(GoalLifecycleCommand)
        .where(
            GoalLifecycleCommand.goal_id == goal.id,
            GoalLifecycleCommand.command_type == "cancel",
            GoalLifecycleCommand.status == "applied",
        )
        .order_by(GoalLifecycleCommand.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if command is not None:
        if command.forced_termination_requested_at is None:
            command.forced_termination_requested_at = goal.forced_termination_requested_at
        command.forced_termination_completed_at = now


def _acknowledge_control(
    session: Session,
    goal: Goal,
    *,
    action: str,
    forced: bool,
) -> None:
    if action == "cancel" and forced:
        _mark_forced_termination(session, goal, datetime.now(timezone.utc))
    session.add(
        GoalLifecycleEvent(
            goal_id=goal.id,
            actor_id=goal.control_requested_by,
            event_type=(
                "goal.cancel.forced_termination_completed"
                if action == "cancel" and forced
                else f"goal.{action}.worker_acknowledged"
            ),
            prior_goal_status=goal.status,
            resulting_goal_status=goal.status,
            payload={"control_version": goal.control_version, "forced": forced},
        )
    )
    goal.pending_control = None
