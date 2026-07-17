from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import (
    actor_team_ids,
    current_actor,
    require_admin,
    require_team_access,
)
from agentic_os.api.deps import get_session
from agentic_os.domain.models import Team, TeamMembership, User

router = APIRouter(tags=["teams"])


class TeamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class TeamMembershipRead(BaseModel):
    id: uuid.UUID
    team_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    created_at: datetime
    user_email: str
    user_display_name: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: str
    created_at: datetime


def _membership_to_read(membership: TeamMembership, user: User) -> TeamMembershipRead:
    return TeamMembershipRead(
        id=membership.id,
        team_id=membership.team_id,
        user_id=membership.user_id,
        role=membership.role,
        created_at=membership.created_at,
        user_email=user.email,
        user_display_name=user.display_name,
    )


@router.get("/teams", response_model=list[TeamRead])
def list_teams(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[Team]:
    if actor.role == "admin":
        return list(session.execute(select(Team).order_by(Team.created_at)).scalars())
    team_ids = actor_team_ids(session, actor)
    if not team_ids:
        return []
    return list(
        session.execute(
            select(Team).where(Team.id.in_(team_ids)).order_by(Team.created_at)
        ).scalars()
    )


@router.get("/teams/{team_id}/memberships", response_model=list[TeamMembershipRead])
def list_team_memberships(
    team_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[TeamMembershipRead]:
    if session.get(Team, team_id) is None:
        raise HTTPException(status_code=404, detail="team not found")
    require_team_access(session, actor, team_id, action="team.membership.list", resource_type="team")
    rows = session.execute(
        select(TeamMembership, User)
        .join(User, User.id == TeamMembership.user_id)
        .where(TeamMembership.team_id == team_id)
        .order_by(TeamMembership.created_at)
    ).all()
    return [_membership_to_read(membership, user) for membership, user in rows]


@router.get("/users", response_model=list[UserRead])
def list_users(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[User]:
    require_admin(session, actor, action="user.list")
    return list(session.execute(select(User).order_by(User.created_at)).scalars())
