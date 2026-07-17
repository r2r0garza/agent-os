from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, Header, HTTPException
from sqlalchemy import Select, exists, or_, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team_membership
from agentic_os.api.deps import get_session
from agentic_os.domain.models import (
    Artifact,
    AuditEvent,
    Goal,
    Project,
    ProjectMember,
    Run,
    Task,
    TeamMembership,
    User,
)


def current_actor(
    x_agentic_user_id: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    """Resolve the explicit API actor while preserving local-operator compatibility.

    A missing header selects the seeded local operator. Once a caller supplies an
    identity header, malformed and unknown values fail closed instead of silently
    falling back to that operator.
    """

    if x_agentic_user_id is None:
        _, actor = ensure_default_team_membership(session)
        return actor
    try:
        actor_id = uuid.UUID(x_agentic_user_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="unknown actor") from error
    actor = session.get(User, actor_id)
    if actor is None:
        raise HTTPException(status_code=401, detail="unknown actor")
    return actor


def project_access_clause(actor: User) -> Any:
    """Return a SQL clause restricting projects to the actor's effective grants."""

    if actor.role == "admin":
        return True
    team_member = exists().where(
        TeamMembership.team_id == Project.team_id,
        TeamMembership.user_id == actor.id,
    )
    explicit_grant = exists().where(
        ProjectMember.project_id == Project.id,
        ProjectMember.user_id == actor.id,
    )
    return or_(Project.created_by == actor.id, team_member & explicit_grant)


def accessible_projects(statement: Select[Any], actor: User) -> Select[Any]:
    return statement.where(project_access_clause(actor))


def _record_decision(
    session: Session,
    actor: User,
    *,
    decision: str,
    action: str,
    resource_type: str,
    project_id: uuid.UUID | None = None,
    reason: str,
    commit: bool = False,
) -> None:
    event = AuditEvent(
        project_id=project_id,
        event_type="authorization.decision",
        payload={
            "actor_id": str(actor.id),
            "actor_role": actor.role,
            "decision": decision,
            "action": action,
            "resource_type": resource_type,
            "reason": reason,
            "redaction_evidence": {
                "resource_identifier_redacted": decision == "deny",
                "credentials_redacted": True,
            },
        },
    )
    if commit:
        # A denied request is rolled back by the request session. Persist its
        # audit evidence independently so doing so cannot commit unrelated work.
        with Session(bind=session.get_bind()) as audit_session:
            audit_session.add(event)
            audit_session.commit()
    else:
        session.add(event)


def require_admin(session: Session, actor: User, *, action: str) -> None:
    if actor.role != "admin":
        _record_decision(
            session,
            actor,
            decision="deny",
            action=action,
            resource_type="installation",
            reason="admin_role_required",
            commit=True,
        )
        raise HTTPException(status_code=403, detail="admin role required")
    _record_decision(
        session,
        actor,
        decision="allow",
        action=action,
        resource_type="installation",
        reason="installation_admin",
    )


def has_team_access(session: Session, actor: User, team_id: uuid.UUID) -> bool:
    if actor.role == "admin":
        return True
    return session.execute(
        select(TeamMembership.id).where(
            TeamMembership.team_id == team_id,
            TeamMembership.user_id == actor.id,
        )
    ).first() is not None


def actor_team_ids(session: Session, actor: User) -> list[uuid.UUID]:
    return list(
        session.execute(
            select(TeamMembership.team_id)
            .where(TeamMembership.user_id == actor.id)
            .order_by(TeamMembership.created_at)
        ).scalars()
    )


def primary_team_id(session: Session, actor: User) -> uuid.UUID:
    team_ids = actor_team_ids(session, actor)
    if team_ids:
        return team_ids[0]
    if actor.role == "admin":
        team, _ = ensure_default_team_membership(session)
        return team.id
    raise HTTPException(status_code=403, detail="team membership required")


def require_team_access(
    session: Session,
    actor: User,
    team_id: uuid.UUID,
    *,
    action: str,
    resource_type: str,
) -> None:
    if has_team_access(session, actor, team_id):
        return
    _record_decision(
        session,
        actor,
        decision="deny",
        action=action,
        resource_type=resource_type,
        reason="resource_not_accessible",
        commit=True,
    )
    raise HTTPException(status_code=404, detail=f"{resource_type} not found")


def can_view_shared_definition(session: Session, actor: User, resource: Any) -> bool:
    """Read access for agent/skill definitions, which extends beyond team ownership by visibility.

    Team membership always grants access. Beyond that, `team` and `public`
    visibility grant read access to any actor with at least one team
    membership (or an admin); `private` never extends past the home team.
    Mutation endpoints must not use this helper — they require actual home
    team membership regardless of visibility.
    """

    if actor.role == "admin":
        return True
    if has_team_access(session, actor, resource.team_id):
        return True
    return resource.visibility in ("team", "public")


def require_shared_definition_access(
    session: Session,
    actor: User,
    resource: Any,
    *,
    action: str,
    resource_type: str,
) -> None:
    if can_view_shared_definition(session, actor, resource):
        return
    _record_decision(
        session,
        actor,
        decision="deny",
        action=action,
        resource_type=resource_type,
        reason="resource_not_accessible",
        commit=True,
    )
    raise HTTPException(status_code=404, detail=f"{resource_type} not found")


def can_access_owned_scope(session: Session, actor: User, resource: Any) -> bool:
    project_id = getattr(resource, "project_id", None)
    if project_id is not None:
        project = session.get(Project, project_id)
        return project is not None and can_access_project(session, actor, project)
    team_id = getattr(resource, "team_id", None)
    return team_id is not None and has_team_access(session, actor, team_id)


def require_owned_scope(
    session: Session,
    actor: User,
    resource: Any,
    *,
    action: str,
    resource_type: str,
) -> None:
    if can_access_owned_scope(session, actor, resource):
        return
    project_id = getattr(resource, "project_id", None)
    _record_decision(
        session,
        actor,
        decision="deny",
        action=action,
        resource_type=resource_type,
        project_id=project_id,
        reason="resource_not_accessible",
        commit=True,
    )
    raise HTTPException(status_code=404, detail=f"{resource_type} not found")


def can_access_project(session: Session, actor: User, project: Project) -> bool:
    if actor.role == "admin":
        return True
    if project.created_by == actor.id:
        return True
    if not has_team_access(session, actor, project.team_id):
        return False
    return session.execute(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == actor.id,
        )
    ).first() is not None


def require_project_access(
    session: Session,
    actor: User,
    project_id: uuid.UUID,
    *,
    action: str,
) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    if can_access_project(session, actor, project):
        return project
    _record_decision(
        session,
        actor,
        decision="deny",
        action=action,
        resource_type="project",
        project_id=project.id,
        reason="resource_not_accessible",
        commit=True,
    )
    raise HTTPException(status_code=404, detail="project not found")


def project_for_goal(session: Session, goal: Goal) -> Project | None:
    return session.get(Project, goal.project_id)


def project_for_task(session: Session, task: Task) -> Project | None:
    goal = session.get(Goal, task.goal_id)
    return project_for_goal(session, goal) if goal is not None else None


def project_for_run(session: Session, run: Run) -> Project | None:
    task = session.get(Task, run.task_id)
    return project_for_task(session, task) if task is not None else None


def require_resource_access(
    session: Session,
    actor: User,
    resource: Goal | Task | Run | Artifact,
    *,
    action: str,
    resource_type: str,
) -> Project:
    if isinstance(resource, Goal):
        project = project_for_goal(session, resource)
    elif isinstance(resource, Task):
        project = project_for_task(session, resource)
    elif isinstance(resource, Run):
        project = project_for_run(session, resource)
    else:
        project = session.get(Project, resource.project_id)
    if project is None or not can_access_project(session, actor, project):
        if project is not None:
            _record_decision(
                session,
                actor,
                decision="deny",
                action=action,
                resource_type=resource_type,
                project_id=project.id,
                reason="resource_not_accessible",
                commit=True,
            )
        raise HTTPException(status_code=404, detail=f"{resource_type} not found")
    return project
