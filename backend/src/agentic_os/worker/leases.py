from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agentic_os.domain.models import Task

DEFAULT_LEASE_SECONDS = 60

_CLAIMABLE_STATUSES = ("pending", "ready", "running")


class LeaseLostError(RuntimeError):
    """Raised when a task's lease is no longer held by the expected worker."""


def claim_ready_task(
    session: Session, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> Task | None:
    """Claim one task whose lease is free or expired, using a fencing token.

    Tasks left ``running`` by a worker whose lease expired (a crash or
    restart) are eligible for reclaim by a new worker; the caller is
    responsible for reconciling the previous, now-stale attempt.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Task)
        .where(Task.status.in_(_CLAIMABLE_STATUSES))
        .where(Task.assigned_agent_version_id.is_not(None))
        .where(or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now))
        .order_by(Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = session.execute(stmt).scalar_one_or_none()
    if task is None:
        return None

    task.status = "running"
    task.lease_owner = worker_id
    task.lease_token = task.lease_token + 1
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    session.flush()
    return task


def renew_lease(session: Session, task: Task, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    if task.lease_owner != worker_id:
        raise LeaseLostError(f"task {task.id} is no longer leased by worker {worker_id!r}")
    task.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    session.flush()


def release_lease(session: Session, task: Task, worker_id: str, *, status: str) -> None:
    if task.lease_owner != worker_id:
        raise LeaseLostError(f"task {task.id} is no longer leased by worker {worker_id!r}")
    task.status = status
    task.lease_owner = None
    task.lease_expires_at = None
    session.flush()
