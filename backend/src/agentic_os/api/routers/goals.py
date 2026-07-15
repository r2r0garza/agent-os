from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.domain.models import Goal, Project

router = APIRouter(tags=["goals"])


class GoalCreate(BaseModel):
    title: str
    description: str | None = None


class GoalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    created_by: uuid.UUID
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@router.post("/projects/{project_id}/goals", response_model=GoalRead, status_code=201)
def create_goal(project_id: uuid.UUID, payload: GoalCreate, session: Session = Depends(get_session)) -> Goal:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    user = ensure_default_user(session)
    goal = Goal(
        project_id=project.id,
        created_by=user.id,
        title=payload.title,
        description=payload.description,
        status="draft",
    )
    session.add(goal)
    session.flush()
    session.refresh(goal)
    return goal


@router.get("/projects/{project_id}/goals", response_model=list[GoalRead])
def list_goals(project_id: uuid.UUID, session: Session = Depends(get_session)) -> list[Goal]:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return list(session.execute(select(Goal).where(Goal.project_id == project_id).order_by(Goal.created_at)).scalars())


@router.get("/goals/{goal_id}", response_model=GoalRead)
def get_goal(goal_id: uuid.UUID, session: Session = Depends(get_session)) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    return goal
