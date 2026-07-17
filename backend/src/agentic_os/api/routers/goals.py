from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import current_actor, require_project_access, require_resource_access
from agentic_os.api.deps import get_session
from agentic_os.domain.models import Goal, User
from agentic_os.observability import current_request_context, record_observability

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
def create_goal(
    project_id: uuid.UUID,
    payload: GoalCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Goal:
    project = require_project_access(session, actor, project_id, action="goal.create")
    goal = Goal(
        project_id=project.id,
        created_by=actor.id,
        title=payload.title,
        description=payload.description,
        status="draft",
    )
    session.add(goal)
    session.flush()
    context = current_request_context()
    if context is not None:
        record_observability(
            session,
            replace(
                context,
                team_id=project.team_id,
                user_id=actor.id,
                project_id=project.id,
                goal_id=goal.id,
            ),
            event_kind="goal",
            operation_name="goal.created",
            status=goal.status,
            attributes={"title": goal.title},
        )
    session.refresh(goal)
    return goal


@router.get("/projects/{project_id}/goals", response_model=list[GoalRead])
def list_goals(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[Goal]:
    require_project_access(session, actor, project_id, action="goal.list")
    return list(session.execute(select(Goal).where(Goal.project_id == project_id).order_by(Goal.created_at)).scalars())


@router.get("/goals/{goal_id}", response_model=GoalRead)
def get_goal(
    goal_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    require_resource_access(session, actor, goal, action="goal.read", resource_type="goal")
    return goal
