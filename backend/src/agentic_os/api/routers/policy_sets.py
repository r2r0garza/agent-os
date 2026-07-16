from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.api.ownership import require_default_team_access, require_project_access
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import PolicySet, PolicySetVersion, Project

router = APIRouter(prefix="/policy-sets", tags=["policy-sets"])


class PolicySetCreate(BaseModel):
    name: str
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None


class PolicySetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    created_at: datetime
    updated_at: datetime


class PolicySetVersionCreate(BaseModel):
    rules: list[dict] = Field(default_factory=list)


class PolicySetVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    policy_set_id: uuid.UUID
    version_number: int
    rules: list
    created_at: datetime


def _get_policy_set(session: Session, policy_set_id: uuid.UUID) -> PolicySet:
    policy_set = session.get(PolicySet, policy_set_id)
    if policy_set is None:
        raise HTTPException(status_code=404, detail="policy set not found")
    return require_default_team_access(session, policy_set, "policy set")


@router.post("", response_model=PolicySetRead, status_code=201)
def create_policy_set(payload: PolicySetCreate, session: Session = Depends(get_session)) -> PolicySet:
    team = ensure_default_team(session)
    user = ensure_default_user(session)
    if payload.team_id is not None and payload.project_id is not None:
        raise HTTPException(status_code=422, detail="policy set must have exactly one owner scope")
    if payload.team_id is not None and payload.team_id != team.id:
        raise HTTPException(status_code=403, detail="cannot create policy set for another team")
    project = require_project_access(session, payload.project_id) if payload.project_id else None
    policy_set = PolicySet(
        team_id=payload.team_id or (None if project else team.id),
        project_id=project.id if project else None,
        created_by=user.id,
        name=payload.name,
    )
    session.add(policy_set)
    session.flush()
    session.refresh(policy_set)
    return policy_set


@router.get("", response_model=list[PolicySetRead])
def list_policy_sets(session: Session = Depends(get_session)) -> list[PolicySet]:
    team_id = ensure_default_team(session).id
    return list(session.execute(
        select(PolicySet).outerjoin(Project, PolicySet.project_id == Project.id)
        .where(or_(PolicySet.team_id == team_id, Project.team_id == team_id)).order_by(PolicySet.created_at)
    ).scalars())


@router.get("/{policy_set_id}", response_model=PolicySetRead)
def get_policy_set(policy_set_id: uuid.UUID, session: Session = Depends(get_session)) -> PolicySet:
    return _get_policy_set(session, policy_set_id)


@router.post("/{policy_set_id}/versions", response_model=PolicySetVersionRead, status_code=201)
def create_policy_set_version(
    policy_set_id: uuid.UUID,
    payload: PolicySetVersionCreate,
    session: Session = Depends(get_session),
) -> PolicySetVersionRead:
    _get_policy_set(session, policy_set_id)
    number = session.execute(select(func.coalesce(func.max(PolicySetVersion.version_number), 0)).where(PolicySetVersion.policy_set_id == policy_set_id)).scalar_one() + 1
    version = PolicySetVersion(policy_set_id=policy_set_id, version_number=number, rules=payload.rules)
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


def _version_to_read(version: PolicySetVersion) -> PolicySetVersionRead:
    return PolicySetVersionRead(
        id=version.id,
        policy_set_id=version.policy_set_id,
        version_number=version.version_number,
        rules=redact_mapping(version.rules),
        created_at=version.created_at,
    )


@router.get("/{policy_set_id}/versions", response_model=list[PolicySetVersionRead])
def list_policy_set_versions(policy_set_id: uuid.UUID, session: Session = Depends(get_session)) -> list[PolicySetVersionRead]:
    _get_policy_set(session, policy_set_id)
    versions = session.execute(select(PolicySetVersion).where(PolicySetVersion.policy_set_id == policy_set_id).order_by(PolicySetVersion.version_number)).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{policy_set_id}/versions/{version_number}", response_model=PolicySetVersionRead)
def get_policy_set_version(policy_set_id: uuid.UUID, version_number: int, session: Session = Depends(get_session)) -> PolicySetVersionRead:
    _get_policy_set(session, policy_set_id)
    version = session.execute(select(PolicySetVersion).where(PolicySetVersion.policy_set_id == policy_set_id, PolicySetVersion.version_number == version_number)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="policy set version not found")
    return _version_to_read(version)
