"""Repository-local factories for durable domain test scenarios.

These helpers centralize common multi-user, lifecycle-control, and graph
revision setup so individual test modules do not hand-roll the same records.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy.orm import Session

from agentic_os.domain.models import (
    Goal,
    GoalLifecycleCommand,
    GoalSteeringRequest,
    Project,
    ProjectMember,
    TaskGraphRevision,
    Team,
    TeamMembership,
    User,
)


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


def make_goal(
    session: Session,
    project: Project,
    creator: User,
    *,
    title: str = "Goal",
    status: str = "active",
) -> Goal:
    goal = Goal(
        project_id=project.id,
        created_by=creator.id,
        title=title,
        status=status,
    )
    session.add(goal)
    session.flush()
    return goal


def make_lifecycle_command(
    session: Session,
    goal: Goal,
    actor: User,
    *,
    command_type: str,
    idempotency_key: str | None = None,
) -> GoalLifecycleCommand:
    command = GoalLifecycleCommand(
        goal_id=goal.id,
        requested_by=actor.id,
        command_type=command_type,
        idempotency_key=idempotency_key or f"{goal.id}:{command_type}:{uuid.uuid4()}",
        prior_goal_status=goal.status,
    )
    session.add(command)
    session.flush()
    return command


def make_steering_request(
    session: Session,
    goal: Goal,
    actor: User,
    *,
    instruction: str = "Revise the unfinished work",
    base_revision_number: int | None = None,
    idempotency_key: str | None = None,
) -> GoalSteeringRequest:
    request = GoalSteeringRequest(
        goal_id=goal.id,
        requested_by=actor.id,
        instruction=instruction,
        base_revision_number=(
            goal.active_graph_revision_number
            if base_revision_number is None
            else base_revision_number
        ),
        idempotency_key=idempotency_key or f"{goal.id}:steer:{uuid.uuid4()}",
    )
    session.add(request)
    session.flush()
    return request


def make_task_graph_revision(
    session: Session,
    goal: Goal,
    actor: User,
    *,
    revision_number: int,
    parent_revision_number: int | None = None,
    steering_request: GoalSteeringRequest | None = None,
) -> TaskGraphRevision:
    revision = TaskGraphRevision(
        goal_id=goal.id,
        created_by=actor.id,
        steering_request_id=steering_request.id if steering_request is not None else None,
        revision_number=revision_number,
        parent_revision_number=parent_revision_number,
        graph_snapshot={"tasks": [], "dependencies": []},
    )
    session.add(revision)
    session.flush()
    return revision


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
