from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import Team, TeamMembership, User

DEFAULT_TEAM_NAME = "Default Team"
DEFAULT_USER_EMAIL = "operator@local"


def ensure_default_team(session: Session) -> Team:
    team = session.execute(select(Team).where(Team.name == DEFAULT_TEAM_NAME)).scalar_one_or_none()
    if team is None:
        team = Team(name=DEFAULT_TEAM_NAME)
        session.add(team)
        session.flush()
    return team


def ensure_default_user(session: Session) -> User:
    user = session.execute(select(User).where(User.email == DEFAULT_USER_EMAIL)).scalar_one_or_none()
    if user is None:
        user = User(email=DEFAULT_USER_EMAIL, display_name="Operator", role="admin")
        session.add(user)
        session.flush()
    return user


def ensure_default_team_membership(session: Session) -> tuple[Team, User]:
    """Ensure the local single-operator path is an explicit team membership.

    Earlier bootstrap logic created the default team and default user as
    independent rows with no membership linking them, so access checks that
    walk `team_memberships` found nothing for the local operator. Sprint 8
    access control must not depend on a hidden singleton assumption.
    """
    team = ensure_default_team(session)
    user = ensure_default_user(session)
    membership = session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
        )
    ).scalar_one_or_none()
    if membership is None:
        membership = TeamMembership(team_id=team.id, user_id=user.id, role="owner")
        session.add(membership)
        session.flush()
    return team, user
