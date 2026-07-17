from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_admin, require_project_access
from agentic_os.api.deps import get_session
from agentic_os.domain.models import (
    Run,
    Task,
    User,
    WorkspacePromotion,
    WorkspaceResource,
    WorkspaceResourceLease,
)

router = APIRouter(tags=["workspace"])


class WorkspaceLeaseRead(BaseModel):
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    resource_key: str
    owner: str
    fencing_token: int
    fencing_status: str
    expected_revision: int
    current_revision: int
    expires_at: datetime
    state: str


class WorkspaceConflictResourceRead(BaseModel):
    resource_key: str
    expected_revision: int
    actual_revision: int


class WorkspaceConflictRead(BaseModel):
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    occurred_at: datetime
    resources: list[WorkspaceConflictResourceRead]


class WorkspacePromotionDeltaRead(BaseModel):
    resource_key: str
    previous_revision: int
    resulting_revision: int | None
    revision_increment: int | None


class WorkspacePromotionRead(BaseModel):
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    status: str
    occurred_at: datetime
    resource_deltas: list[WorkspacePromotionDeltaRead]


def _lease_state(
    resource: WorkspaceResource,
    lease: WorkspaceResourceLease,
    task: Task | None,
    *,
    now: datetime,
) -> tuple[str, str]:
    fencing_status = (
        "current" if lease.fencing_token == resource.last_fencing_token else "superseded"
    )
    if fencing_status == "superseded":
        return "fenced", fencing_status
    if (
        lease.expires_at is None
        or lease.expires_at <= now
        or task is None
        or task.lease_owner != lease.lease_owner
        or task.lease_token != lease.task_lease_token
        or task.lease_expires_at is None
        or task.lease_expires_at <= now
    ):
        return "stale", fencing_status
    return "active", fencing_status


def _run_id_for_lease(session: Session, lease: WorkspaceResourceLease) -> uuid.UUID | None:
    if lease.task_id is None:
        return None
    return session.execute(
        select(Run.id)
        .where(
            Run.task_id == lease.task_id,
            Run.lease_token == lease.task_lease_token,
        )
        .order_by(Run.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def _list_leases(
    session: Session,
    *,
    project_id: uuid.UUID | None,
    state: str | None,
    limit: int,
) -> list[dict]:
    statement = (
        select(WorkspaceResource, WorkspaceResourceLease, Task)
        .join(
            WorkspaceResourceLease,
            WorkspaceResourceLease.resource_id == WorkspaceResource.id,
        )
        .outerjoin(Task, Task.id == WorkspaceResourceLease.task_id)
        .where(
            WorkspaceResourceLease.lease_owner.is_not(None),
            WorkspaceResourceLease.expires_at.is_not(None),
        )
    )
    if project_id is not None:
        statement = statement.where(WorkspaceResource.project_id == project_id)
    statement = statement.order_by(
        WorkspaceResource.project_id,
        WorkspaceResource.resource_key,
    )
    if state is None:
        statement = statement.limit(limit)
    rows = session.execute(statement)
    now = datetime.now(timezone.utc)
    evidence = []
    for resource, lease, task in rows:
        lease_state, fencing_status = _lease_state(resource, lease, task, now=now)
        if state is not None and lease_state != state:
            continue
        evidence.append(
            {
                "project_id": resource.project_id,
                "task_id": lease.task_id,
                "run_id": _run_id_for_lease(session, lease),
                "resource_key": resource.resource_key,
                "owner": lease.lease_owner,
                "fencing_token": lease.fencing_token,
                "fencing_status": fencing_status,
                "expected_revision": lease.expected_revision,
                "current_revision": resource.revision,
                "expires_at": lease.expires_at,
                "state": lease_state,
            }
        )
        if len(evidence) == limit:
            break
    return evidence


def _list_conflicts(
    session: Session,
    *,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[dict]:
    statement = select(WorkspacePromotion).where(WorkspacePromotion.status == "conflict")
    if project_id is not None:
        statement = statement.where(WorkspacePromotion.project_id == project_id)
    promotions = session.execute(
        statement.order_by(
            WorkspacePromotion.created_at.desc(),
            WorkspacePromotion.id.desc(),
        ).limit(limit)
    ).scalars()
    return [
        {
            "project_id": promotion.project_id,
            "task_id": promotion.task_id,
            "run_id": promotion.run_id,
            "occurred_at": promotion.created_at,
            "resources": [
                {
                    "resource_key": resource_key,
                    "expected_revision": details["expected_revision"],
                    "actual_revision": details["actual_revision"],
                }
                for resource_key, details in sorted(promotion.conflict_details.items())
            ],
        }
        for promotion in promotions
    ]


def _list_promotions(
    session: Session,
    *,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[dict]:
    statement = select(WorkspacePromotion)
    if project_id is not None:
        statement = statement.where(WorkspacePromotion.project_id == project_id)
    promotions = session.execute(
        statement.order_by(
            WorkspacePromotion.created_at.desc(),
            WorkspacePromotion.id.desc(),
        ).limit(limit)
    ).scalars()
    evidence = []
    for promotion in promotions:
        resource_keys = sorted(
            set(promotion.expected_revisions) | set(promotion.resulting_revisions)
        )
        deltas = []
        for resource_key in resource_keys:
            previous_revision = promotion.expected_revisions[resource_key]
            resulting_revision = promotion.resulting_revisions.get(resource_key)
            deltas.append(
                {
                    "resource_key": resource_key,
                    "previous_revision": previous_revision,
                    "resulting_revision": resulting_revision,
                    "revision_increment": (
                        resulting_revision - previous_revision
                        if resulting_revision is not None
                        else None
                    ),
                }
            )
        evidence.append(
            {
                "project_id": promotion.project_id,
                "task_id": promotion.task_id,
                "run_id": promotion.run_id,
                "status": promotion.status,
                "occurred_at": promotion.created_at,
                "resource_deltas": deltas,
            }
        )
    return evidence


@router.get(
    "/projects/{project_id}/workspace/leases",
    response_model=list[WorkspaceLeaseRead],
)
def list_project_workspace_leases(
    project_id: uuid.UUID,
    state: str | None = Query(default=None, pattern="^(active|stale|fenced)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_project_access(session, actor, project_id, action="workspace.lease.list")
    return _list_leases(session, project_id=project_id, state=state, limit=limit)


@router.get(
    "/projects/{project_id}/workspace/conflicts",
    response_model=list[WorkspaceConflictRead],
)
def list_project_workspace_conflicts(
    project_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_project_access(session, actor, project_id, action="workspace.conflict.list")
    return _list_conflicts(session, project_id=project_id, limit=limit)


@router.get(
    "/projects/{project_id}/workspace/promotions",
    response_model=list[WorkspacePromotionRead],
)
def list_project_workspace_promotions(
    project_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_project_access(session, actor, project_id, action="workspace.promotion.list")
    return _list_promotions(session, project_id=project_id, limit=limit)


@router.get("/admin/workspace/leases", response_model=list[WorkspaceLeaseRead])
def list_installation_workspace_leases(
    state: str | None = Query(default=None, pattern="^(active|stale|fenced)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_admin(session, actor, action="workspace.lease.list_installation")
    return _list_leases(session, project_id=None, state=state, limit=limit)


@router.get("/admin/workspace/conflicts", response_model=list[WorkspaceConflictRead])
def list_installation_workspace_conflicts(
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_admin(session, actor, action="workspace.conflict.list_installation")
    return _list_conflicts(session, project_id=None, limit=limit)


@router.get("/admin/workspace/promotions", response_model=list[WorkspacePromotionRead])
def list_installation_workspace_promotions(
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict]:
    require_admin(session, actor, action="workspace.promotion.list_installation")
    return _list_promotions(session, project_id=None, limit=limit)
