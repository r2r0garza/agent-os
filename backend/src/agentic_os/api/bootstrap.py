from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.domain.models import Team, User

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
