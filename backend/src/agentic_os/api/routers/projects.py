from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import (
    accessible_projects,
    current_actor,
    has_team_access,
    primary_team_id,
    require_project_access,
)
from agentic_os.api.deps import get_session
from agentic_os.domain.models import AuditEvent, Project, ProjectMember, User

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=ProjectRead, status_code=201)
def create_project(
    payload: ProjectCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Project:
    team_id = primary_team_id(session, actor)
    project = Project(team_id=team_id, created_by=actor.id, name=payload.name)
    session.add(project)
    session.flush()
    session.refresh(project)
    return project


@router.get("", response_model=list[ProjectRead])
def list_projects(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[Project]:
    stmt = accessible_projects(select(Project), actor).order_by(Project.created_at)
    return list(session.execute(stmt).scalars())


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Project:
    return require_project_access(session, actor, project_id, action="project.read")


class ProjectMemberCreate(BaseModel):
    user_id: uuid.UUID


class ProjectMemberRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    granted_by: uuid.UUID | None
    created_at: datetime
    user_email: str
    user_display_name: str


def _member_to_read(member: ProjectMember, user: User) -> ProjectMemberRead:
    return ProjectMemberRead(
        id=member.id,
        project_id=member.project_id,
        user_id=member.user_id,
        granted_by=member.granted_by,
        created_at=member.created_at,
        user_email=user.email,
        user_display_name=user.display_name,
    )


def _require_grant_access(actor: User, project: Project) -> None:
    if actor.role == "admin" or project.created_by == actor.id:
        return
    raise HTTPException(status_code=403, detail="only the project creator or an admin can manage project access")


@router.get("/{project_id}/members", response_model=list[ProjectMemberRead])
def list_project_members(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ProjectMemberRead]:
    require_project_access(session, actor, project_id, action="project.member.list")
    rows = session.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at)
    ).all()
    return [_member_to_read(member, user) for member, user in rows]


@router.post("/{project_id}/members", response_model=ProjectMemberRead, status_code=201)
def grant_project_member(
    project_id: uuid.UUID,
    payload: ProjectMemberCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ProjectMemberRead:
    project = require_project_access(session, actor, project_id, action="project.member.grant")
    _require_grant_access(actor, project)
    target = session.get(User, payload.user_id)
    if target is None:
        raise HTTPException(status_code=422, detail="user not found")
    if not has_team_access(session, target, project.team_id):
        raise HTTPException(status_code=422, detail="user must be a member of the project's team")
    existing = session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == payload.user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="user already has project access")
    member = ProjectMember(project_id=project_id, user_id=payload.user_id, granted_by=actor.id)
    session.add(member)
    session.flush()
    session.add(
        AuditEvent(
            project_id=project_id,
            event_type="project.member.granted",
            payload={
                "actor_id": str(actor.id),
                "project_id": str(project_id),
                "user_id": str(payload.user_id),
            },
        )
    )
    session.flush()
    session.refresh(member)
    return _member_to_read(member, target)


@router.delete("/{project_id}/members/{user_id}", status_code=204, response_class=Response)
def revoke_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Response:
    project = require_project_access(session, actor, project_id, action="project.member.revoke")
    _require_grant_access(actor, project)
    member = session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="project member not found")
    session.delete(member)
    session.flush()
    session.add(
        AuditEvent(
            project_id=project_id,
            event_type="project.member.revoked",
            payload={
                "actor_id": str(actor.id),
                "project_id": str(project_id),
                "user_id": str(user_id),
            },
        )
    )
    session.flush()
    return Response(status_code=204)
