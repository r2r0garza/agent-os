from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team
from agentic_os.domain.models import Project


def owner_team_id(session: Session, resource: Any) -> uuid.UUID | None:
    team_id = getattr(resource, "team_id", None)
    if team_id is not None:
        return team_id
    project_id = getattr(resource, "project_id", None)
    if project_id is None:
        return None
    project = session.get(Project, project_id)
    return project.team_id if project is not None else None


def require_default_team_access(session: Session, resource: Any, resource_name: str) -> Any:
    if owner_team_id(session, resource) != ensure_default_team(session).id:
        raise HTTPException(status_code=403, detail=f"{resource_name} belongs to another team")
    return resource


def require_project_access(session: Session, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=422, detail="project not found")
    return require_default_team_access(session, project, "project")
