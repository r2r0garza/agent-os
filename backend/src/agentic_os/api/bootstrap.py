from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from agentic_os.domain.models import Team, TeamMembership, User

DEFAULT_TEAM_NAME = "Default Team"
DEFAULT_USER_EMAIL = "operator@local"


def ensure_default_team(session: Session) -> Team:
    """Idempotently resolve the default team under concurrent cold starts.

    Concurrent requests can all miss the initial SELECT before any of them
    commits. Insert via ON CONFLICT DO NOTHING against the unique team name
    so a losing concurrent insert is absorbed instead of raising
    IntegrityError, then re-select to return the winning row.
    """
    team = session.execute(select(Team).where(Team.name == DEFAULT_TEAM_NAME)).scalar_one_or_none()
    if team is not None:
        return team
    session.execute(
        pg_insert(Team.__table__)
        .values(name=DEFAULT_TEAM_NAME)
        .on_conflict_do_nothing(constraint="uq_teams_name")
    )
    session.flush()
    return session.execute(select(Team).where(Team.name == DEFAULT_TEAM_NAME)).scalar_one()


def ensure_default_user(session: Session) -> User:
    """Idempotently resolve the default user under concurrent cold starts.

    Same INSERT ... ON CONFLICT DO NOTHING plus re-select pattern as
    `ensure_default_team`, keyed on the unique user email.
    """
    user = session.execute(select(User).where(User.email == DEFAULT_USER_EMAIL)).scalar_one_or_none()
    if user is not None:
        return user
    session.execute(
        pg_insert(User.__table__)
        .values(email=DEFAULT_USER_EMAIL, display_name="Operator", role="admin")
        .on_conflict_do_nothing(constraint="uq_users_email")
    )
    session.flush()
    return session.execute(select(User).where(User.email == DEFAULT_USER_EMAIL)).scalar_one()


def ensure_default_team_membership(session: Session) -> tuple[Team, User]:
    """Ensure the local single-operator path is an explicit team membership.

    Earlier bootstrap logic created the default team and default user as
    independent rows with no membership linking them, so access checks that
    walk `team_memberships` found nothing for the local operator. Sprint 8
    access control must not depend on a hidden singleton assumption.

    Membership creation uses the same ON CONFLICT DO NOTHING plus re-select
    pattern, keyed on the team/user unique constraint, so concurrent requests
    racing on the same team and user cannot raise IntegrityError.
    """
    team = ensure_default_team(session)
    user = ensure_default_user(session)
    membership = session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
        )
    ).scalar_one_or_none()
    if membership is None:
        session.execute(
            pg_insert(TeamMembership.__table__)
            .values(team_id=team.id, user_id=user.id, role="owner")
            .on_conflict_do_nothing(constraint="uq_team_memberships_team_user")
        )
        session.flush()
    return team, user
