from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_team_membership
from agentic_os.api.deps import get_session
from agentic_os.api.ownership import require_default_team_access, require_project_access
from agentic_os.api.redaction import redact_mapping
from agentic_os.api.secrets import encrypt_secret
from agentic_os.domain.models import Credential, Project

router = APIRouter(prefix="/credentials", tags=["credentials"])


class CredentialCreate(BaseModel):
    name: str
    credential_type: str
    material: SecretStr
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)


class CredentialRead(BaseModel):
    id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    credential_type: str
    metadata: dict
    configured: bool
    created_at: datetime
    updated_at: datetime


def _to_read(credential: Credential) -> CredentialRead:
    metadata = credential.redacted_metadata()
    metadata["metadata"] = redact_mapping(metadata["metadata"])
    return CredentialRead(**metadata, created_at=credential.created_at, updated_at=credential.updated_at)


@router.post("", response_model=CredentialRead, status_code=201)
def create_credential(payload: CredentialCreate, session: Session = Depends(get_session)) -> CredentialRead:
    team, user = ensure_default_team_membership(session)
    if payload.team_id is not None and payload.project_id is not None:
        raise HTTPException(status_code=422, detail="credential must have exactly one owner scope")
    if payload.team_id is not None and payload.team_id != team.id:
        raise HTTPException(status_code=403, detail="cannot create credential for another team")
    project = require_project_access(session, payload.project_id) if payload.project_id else None
    credential = Credential(
        team_id=payload.team_id or (None if project else team.id),
        project_id=project.id if project else None,
        created_by=user.id,
        name=payload.name,
        credential_type=payload.credential_type,
        encrypted_material=encrypt_secret(payload.material.get_secret_value()),
        metadata_=payload.metadata,
    )
    session.add(credential)
    session.flush()
    session.refresh(credential)
    return _to_read(credential)


@router.get("", response_model=list[CredentialRead])
def list_credentials(session: Session = Depends(get_session)) -> list[CredentialRead]:
    team_id = ensure_default_team(session).id
    credentials = session.execute(
        select(Credential)
        .outerjoin(Project, Credential.project_id == Project.id)
        .where(or_(Credential.team_id == team_id, Project.team_id == team_id))
        .order_by(Credential.created_at)
    ).scalars()
    return [_to_read(credential) for credential in credentials]


@router.get("/{credential_id}", response_model=CredentialRead)
def get_credential(credential_id: uuid.UUID, session: Session = Depends(get_session)) -> CredentialRead:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="credential not found")
    require_default_team_access(session, credential, "credential")
    return _to_read(credential)


@router.patch("/{credential_id}", status_code=409)
def reject_credential_mutation(credential_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="credential not found")
    require_default_team_access(session, credential, "credential")
    raise HTTPException(status_code=409, detail="credentials are immutable; create a new credential and configuration version")
