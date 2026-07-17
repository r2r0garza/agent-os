from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import accessible_projects, current_actor, primary_team_id, require_project_access
from agentic_os.api.deps import get_session
from agentic_os.domain.models import Project, User

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
