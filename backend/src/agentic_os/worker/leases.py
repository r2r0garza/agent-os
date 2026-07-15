from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import exists

from agentic_os.domain.models import Task, TaskDependency

DEFAULT_LEASE_SECONDS = 60

_CLAIMABLE_STATUSES = ("pending", "ready", "running")

# How many earliest-created claimable tasks to consider per claim attempt
# before giving up. Bounded so a large backlog of mutually conflicting
# tasks cannot make a claim scan the entire table.
_CANDIDATE_BATCH_SIZE = 50


class LeaseLostError(RuntimeError):
    """Raised when a task's lease is no longer held by the expected worker."""


def _try_lock_resource_keys(session: Session, task: Task) -> bool:
    """Attempt to acquire a transaction-scoped PostgreSQL advisory lock for
    each resource key the task declares, returning True only if every key
    was acquired.

    A plain committed-status comparison against other "running" tasks is not
    enough here: two claim attempts racing in separate, still-uncommitted
    transactions would each see the other as not-yet-running and both
    proceed. ``pg_try_advisory_xact_lock`` is a real cross-transaction mutex,
    visible immediately (not only after commit), and every lock acquired
    during this attempt is released automatically if the caller rolls back
    to the enclosing SAVEPOINT, or when the winning transaction eventually
    commits or rolls back.

    Every declared resource key takes the same exclusive lock regardless of
    read/write intent. VISION.md explicitly allows this: "Inferred intent
    may make locking more conservative but cannot weaken an existing lock."
    The finer-grained read/write, revision, and fencing-token workspace
    protocol is the dedicated scope of a later workspace-locking milestone.
    """
    keys = sorted(
        {entry.get("resource_key") for entry in (task.resource_intent or []) if entry.get("resource_key")}
    )
    for key in keys:
        acquired = session.execute(select(func.pg_try_advisory_xact_lock(func.hashtextextended(key, 0)))).scalar_one()
        if not acquired:
            return False
    return True


def claim_ready_task(
    session: Session, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> Task | None:
    """Claim one task whose dependencies are satisfied, whose lease is free
    or expired, and whose declared resource keys are not held by another
    in-flight task, using a fencing token.

    Tasks left ``running`` by a worker whose lease expired (a crash or
    restart) are eligible for reclaim by a new worker; the caller is
    responsible for reconciling the previous, now-stale attempt.

    Candidates are considered one at a time in creation order inside a
    SAVEPOINT: a candidate whose resource keys are already locked by another
    in-flight task is rolled back immediately, releasing its row lock and any
    resource-key locks it grabbed, so a busy resource key cannot hold other
    concurrent claimers' rows locked while this worker looks for a
    different, non-conflicting task.
    """
    now = datetime.now(timezone.utc)
    dependency_task = aliased(Task)
    unmet_dependency = (
        select(TaskDependency.task_id)
        .join(dependency_task, dependency_task.id == TaskDependency.depends_on_task_id)
        .where(TaskDependency.task_id == Task.id, dependency_task.status != "completed")
    )

    excluded_ids: set = set()
    for _ in range(_CANDIDATE_BATCH_SIZE):
        stmt = (
            select(Task)
            .where(Task.status.in_(_CLAIMABLE_STATUSES))
            .where(Task.assigned_agent_version_id.is_not(None))
            .where(or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now))
            .where(~exists(unmet_dependency))
        )
        if excluded_ids:
            stmt = stmt.where(Task.id.not_in(excluded_ids))
        stmt = stmt.order_by(Task.created_at).limit(1).with_for_update(skip_locked=True)

        savepoint = session.begin_nested()
        task = session.execute(stmt).scalar_one_or_none()
        if task is None:
            savepoint.rollback()
            return None

        if not _try_lock_resource_keys(session, task):
            excluded_ids.add(task.id)
            savepoint.rollback()
            continue

        task.status = "running"
        task.lease_owner = worker_id
        task.lease_token = task.lease_token + 1
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        session.flush()
        savepoint.commit()
        return task

    return None


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
