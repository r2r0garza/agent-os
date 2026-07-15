from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    AuditEvent,
    Goal,
    Run,
    Task,
    WorkspacePromotion,
    WorkspaceResource,
    WorkspaceResourceLease,
)


class InvalidResourceKeyError(ValueError):
    """Raised when a resource key is not canonical and project-relative."""


class WorkspaceLeaseLostError(RuntimeError):
    """Raised when a stale worker attempts a workspace mutation."""


class WorkspaceConflictError(RuntimeError):
    """Raised when promotion observes a changed expected resource revision."""


def canonical_resource_key(raw_key: str) -> str:
    """Return a stable POSIX project-relative resource key."""
    if not isinstance(raw_key, str) or not raw_key or "\\" in raw_key or raw_key.startswith("/"):
        raise InvalidResourceKeyError(f"invalid project-relative resource key: {raw_key!r}")
    parts = raw_key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidResourceKeyError(f"resource key must already be canonical: {raw_key!r}")
    canonical = PurePosixPath(raw_key).as_posix()
    if canonical in {"", "."} or canonical != raw_key:
        raise InvalidResourceKeyError(f"resource key must already be canonical: {raw_key!r}")
    return canonical


def mutating_resource_keys(task: Task) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                canonical_resource_key(str(entry.get("resource_key")))
                for entry in (task.resource_intent or [])
                if entry.get("intent") == "write"
            }
        )
    )


def _project_id(session: Session, task: Task) -> uuid.UUID:
    goal = session.get(Goal, task.goal_id)
    if goal is None:
        raise WorkspaceLeaseLostError(f"task {task.id} has no resolvable goal")
    return goal.project_id


def acquire_resource_leases(
    session: Session,
    task: Task,
    worker_id: str,
    *,
    expires_at: datetime,
) -> bool:
    """Acquire every declared write resource atomically for a claimed task."""
    keys = mutating_resource_keys(task)
    if not keys:
        return True
    now = datetime.now(timezone.utc)
    project_id = _project_id(session, task)

    for key in keys:
        resource = session.execute(
            select(WorkspaceResource)
            .where(
                WorkspaceResource.project_id == project_id,
                WorkspaceResource.resource_key == key,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if resource is None:
            resource = WorkspaceResource(project_id=project_id, resource_key=key)
            session.add(resource)
            session.flush()

        lease = session.execute(
            select(WorkspaceResourceLease)
            .where(WorkspaceResourceLease.resource_id == resource.id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            lease is not None
            and lease.lease_owner is not None
            and lease.expires_at is not None
            and lease.expires_at >= now
            and lease.task_id != task.id
        ):
            return False

        resource.last_fencing_token += 1
        if lease is None:
            lease = WorkspaceResourceLease(
                resource_id=resource.id,
                task_id=task.id,
                lease_owner=worker_id,
                task_lease_token=task.lease_token,
                fencing_token=resource.last_fencing_token,
                expected_revision=resource.revision,
                expires_at=expires_at,
            )
            session.add(lease)
        else:
            lease.task_id = task.id
            lease.lease_owner = worker_id
            lease.task_lease_token = task.lease_token
            lease.fencing_token = resource.last_fencing_token
            lease.expected_revision = resource.revision
            lease.expires_at = expires_at
            lease.released_at = None

        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                event_type="workspace.lease_acquired",
                payload={
                    "resource_key": key,
                    "worker_id": worker_id,
                    "fencing_token": resource.last_fencing_token,
                    "expected_revision": resource.revision,
                },
            )
        )
    session.flush()
    return True


def renew_resource_leases(
    session: Session, task: Task, worker_id: str, *, expires_at: datetime
) -> None:
    keys = mutating_resource_keys(task)
    if not keys:
        return
    project_id = _project_id(session, task)
    now = datetime.now(timezone.utc)
    rows = list(
        session.execute(
            select(WorkspaceResource, WorkspaceResourceLease)
            .join(WorkspaceResourceLease, WorkspaceResourceLease.resource_id == WorkspaceResource.id)
            .where(
                WorkspaceResource.project_id == project_id,
                WorkspaceResource.resource_key.in_(keys),
            )
            .with_for_update()
        )
    )
    if len(rows) != len(keys):
        raise WorkspaceLeaseLostError(f"task {task.id} no longer holds every workspace resource lease")
    for resource, lease in rows:
        if (
            lease.task_id != task.id
            or lease.lease_owner != worker_id
            or lease.task_lease_token != task.lease_token
            or lease.expires_at is None
            or lease.expires_at < now
        ):
            raise WorkspaceLeaseLostError(
                f"stale workspace lease for task {task.id} and resource {resource.resource_key!r}"
            )
        lease.expires_at = expires_at
    session.flush()


def release_resource_leases(session: Session, task: Task, worker_id: str) -> None:
    keys = mutating_resource_keys(task)
    if not keys:
        return
    project_id = _project_id(session, task)
    now = datetime.now(timezone.utc)
    rows = list(
        session.execute(
            select(WorkspaceResource, WorkspaceResourceLease)
            .join(WorkspaceResourceLease, WorkspaceResourceLease.resource_id == WorkspaceResource.id)
            .where(
                WorkspaceResource.project_id == project_id,
                WorkspaceResource.resource_key.in_(keys),
            )
            .with_for_update()
        )
    )
    for resource, lease in rows:
        if lease.task_id == task.id and lease.lease_owner == worker_id:
            lease.lease_owner = None
            lease.expires_at = None
            lease.released_at = now
            session.add(
                AuditEvent(
                    project_id=project_id,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    event_type="workspace.lease_released",
                    payload={
                        "resource_key": resource.resource_key,
                        "fencing_token": lease.fencing_token,
                    },
                )
            )
    session.flush()


def promote_workspace_changes(
    session: Session,
    task: Task,
    run: Run,
    worker_id: str,
    *,
    expected_revisions: dict[str, int] | None = None,
) -> WorkspacePromotion | None:
    """Atomically validate leases/revisions and advance all written resources."""
    keys = mutating_resource_keys(task)
    if not keys:
        return None
    project_id = _project_id(session, task)
    now = datetime.now(timezone.utc)
    rows = list(
        session.execute(
            select(WorkspaceResource, WorkspaceResourceLease)
            .join(WorkspaceResourceLease, WorkspaceResourceLease.resource_id == WorkspaceResource.id)
            .where(
                WorkspaceResource.project_id == project_id,
                WorkspaceResource.resource_key.in_(keys),
            )
            .order_by(WorkspaceResource.resource_key)
            .with_for_update()
        )
    )
    by_key = {resource.resource_key: (resource, lease) for resource, lease in rows}
    stale: dict[str, str] = {}
    conflicts: dict[str, dict[str, int]] = {}
    expected: dict[str, int] = {}

    for key in keys:
        pair = by_key.get(key)
        if pair is None:
            stale[key] = "resource lease missing"
            continue
        resource, lease = pair
        wanted = (
            int(expected_revisions[key])
            if expected_revisions is not None and key in expected_revisions
            else lease.expected_revision
        )
        expected[key] = wanted
        if (
            task.lease_owner != worker_id
            or task.lease_expires_at is None
            or task.lease_expires_at < now
            or run.lease_token != task.lease_token
            or lease.task_id != task.id
            or lease.lease_owner != worker_id
            or lease.task_lease_token != task.lease_token
            or lease.fencing_token != resource.last_fencing_token
            or lease.expires_at is None
            or lease.expires_at < now
        ):
            stale[key] = "lease owner, expiry, or fencing token is stale"
        elif resource.revision != wanted:
            conflicts[key] = {"expected_revision": wanted, "actual_revision": resource.revision}

    if stale:
        promotion = WorkspacePromotion(
            project_id=project_id,
            task_id=task.id,
            run_id=run.id,
            status="denied",
            expected_revisions=expected,
            conflict_details=stale,
        )
        session.add(promotion)
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="workspace.promotion_denied",
                payload={"resources": stale},
            )
        )
        session.flush()
        raise WorkspaceLeaseLostError(f"workspace promotion denied for stale task {task.id}: {stale}")

    if conflicts:
        promotion = WorkspacePromotion(
            project_id=project_id,
            task_id=task.id,
            run_id=run.id,
            status="conflict",
            expected_revisions=expected,
            conflict_details=conflicts,
        )
        session.add(promotion)
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="workspace.promotion_conflict",
                payload={"resources": conflicts},
            )
        )
        session.flush()
        raise WorkspaceConflictError(f"workspace promotion conflict for task {task.id}: {conflicts}")

    resulting: dict[str, int] = {}
    for key in keys:
        resource, lease = by_key[key]
        resource.revision += 1
        resulting[key] = resource.revision
        lease.lease_owner = None
        lease.expires_at = None
        lease.released_at = now

    promotion = WorkspacePromotion(
        project_id=project_id,
        task_id=task.id,
        run_id=run.id,
        status="promoted",
        expected_revisions=expected,
        resulting_revisions=resulting,
    )
    session.add(promotion)
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="workspace.promoted",
            payload={"expected_revisions": expected, "resulting_revisions": resulting},
        )
    )
    session.flush()
    return promotion
