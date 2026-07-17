"""Repository-local factories for multi-user/team test scenarios.

Sprint 8 (team access and resource sharing) needs several tests to set up
more than one user, team, or team membership. These helpers centralize that
setup so individual test modules do not each hand-roll the same boilerplate.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy.orm import Session

from agentic_os.domain.models import Project, ProjectMember, Team, TeamMembership, User


def make_team(session: Session, *, name: str | None = None) -> Team:
    team = Team(name=name or f"Team {uuid.uuid4()}")
    session.add(team)
    session.flush()
    return team


def make_user(
    session: Session,
    *,
    email: str | None = None,
    display_name: str = "User",
    role: str = "regular_user",
) -> User:
    user = User(
        email=email or f"user-{uuid.uuid4()}@example.test",
        display_name=display_name,
        role=role,
    )
    session.add(user)
    session.flush()
    return user


def make_team_membership(
    session: Session, team: Team, user: User, *, role: str = "member"
) -> TeamMembership:
    membership = TeamMembership(team_id=team.id, user_id=user.id, role=role)
    session.add(membership)
    session.flush()
    return membership


def make_project(session: Session, team: Team, creator: User, *, name: str = "Project") -> Project:
    project = Project(team_id=team.id, created_by=creator.id, name=name)
    session.add(project)
    session.flush()
    return project


def make_project_member(
    session: Session,
    project: Project,
    user: User,
    *,
    granted_by: User | None = None,
) -> ProjectMember:
    member = ProjectMember(
        project_id=project.id,
        user_id=user.id,
        granted_by=granted_by.id if granted_by is not None else None,
    )
    session.add(member)
    session.flush()
    return member


def make_team_with_members(
    session: Session,
    *,
    member_emails: Iterable[str] = (),
) -> tuple[Team, User, list[User]]:
    """Create a team with an owner membership and zero or more member memberships."""
    team = make_team(session)
    owner = make_user(session, display_name="Owner")
    make_team_membership(session, team, owner, role="owner")
    members = []
    for email in member_emails:
        member = make_user(session, email=email, display_name="Member")
        make_team_membership(session, team, member, role="member")
        members.append(member)
    return team, owner, members
