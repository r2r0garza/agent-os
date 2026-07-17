from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import actor_team_ids, current_actor, primary_team_id, require_team_access
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import Skill, SkillVersion, User

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    visibility: str = "private"


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class SkillVersionCreate(BaseModel):
    content_ref: str
    resource_metadata: dict = Field(default_factory=dict)


class SkillVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    version_number: int
    content_ref: str
    resource_metadata: dict
    created_at: datetime


def _version_to_read(version: SkillVersion) -> SkillVersionRead:
    return SkillVersionRead(
        id=version.id,
        skill_id=version.skill_id,
        version_number=version.version_number,
        content_ref=version.content_ref,
        resource_metadata=redact_mapping(version.resource_metadata),
        created_at=version.created_at,
    )


@router.post("", response_model=SkillRead, status_code=201)
def create_skill(
    payload: SkillCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    skill = Skill(
        team_id=primary_team_id(session, actor),
        created_by=actor.id,
        name=payload.name,
        visibility=payload.visibility,
    )
    session.add(skill)
    session.flush()
    session.refresh(skill)
    return skill


@router.get("", response_model=list[SkillRead])
def list_skills(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[Skill]:
    stmt = select(Skill)
    if actor.role != "admin":
        stmt = stmt.where(Skill.team_id.in_(actor_team_ids(session, actor)))
    return list(session.execute(stmt.order_by(Skill.created_at)).scalars())


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_team_access(session, actor, skill.team_id, action="skill.read", resource_type="skill")
    return skill


@router.post("/{skill_id}/versions", response_model=SkillVersionRead, status_code=201)
def create_skill_version(
    skill_id: uuid.UUID,
    payload: SkillVersionCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillVersionRead:
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_team_access(session, actor, skill.team_id, action="skill.version.create", resource_type="skill")
    next_version = (
        session.execute(
            select(func.coalesce(func.max(SkillVersion.version_number), 0)).where(
                SkillVersion.skill_id == skill_id
            )
        ).scalar_one()
        + 1
    )
    version = SkillVersion(
        skill_id=skill_id,
        version_number=next_version,
        content_ref=payload.content_ref,
        resource_metadata=payload.resource_metadata,
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


@router.get("/{skill_id}/versions", response_model=list[SkillVersionRead])
def list_skill_versions(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[SkillVersionRead]:
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_team_access(session, actor, skill.team_id, action="skill.version.list", resource_type="skill")
    versions = session.execute(
        select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version_number)
    ).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{skill_id}/versions/{version_number}", response_model=SkillVersionRead)
def get_skill_version(
    skill_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillVersionRead:
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_team_access(session, actor, skill.team_id, action="skill.version.read", resource_type="skill")
    version = session.execute(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill_id, SkillVersion.version_number == version_number
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="skill version not found")
    return _version_to_read(version)
