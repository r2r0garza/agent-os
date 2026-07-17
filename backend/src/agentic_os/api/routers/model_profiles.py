from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_team_membership
from agentic_os.api.deps import get_session
from agentic_os.api.ownership import require_default_team_access
from agentic_os.api.redaction import redact_mapping
from agentic_os.api.secrets import encrypt_secret
from agentic_os.domain.models import Credential, ModelProfile, ModelProfileVersion

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])


class ModelProfileCreate(BaseModel):
    name: str
    base_url: str
    model_identifier: str
    api_key: SecretStr | None = None
    credential_id: uuid.UUID | None = None
    headers: dict = Field(default_factory=dict)
    capability_metadata: dict = Field(default_factory=dict)
    pricing_metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_credential(self) -> "ModelProfileCreate":
        if (self.api_key is None) == (self.credential_id is None):
            raise ValueError("provide exactly one of api_key or credential_id")
        return self


class ModelProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    team_id: uuid.UUID
    name: str
    base_url: str
    model_identifier: str
    capability_metadata: dict
    pricing_metadata: dict
    created_at: datetime
    updated_at: datetime


class ModelProfileVersionCreate(BaseModel):
    base_url: str
    model_identifier: str
    credential_id: uuid.UUID
    headers: dict = Field(default_factory=dict)
    capability_metadata: dict = Field(default_factory=dict)
    pricing_metadata: dict = Field(default_factory=dict)


class ModelProfileVersionRead(BaseModel):
    id: uuid.UUID
    model_profile_id: uuid.UUID
    version_number: int
    base_url: str
    model_identifier: str
    credential_id: uuid.UUID | None
    headers: dict
    capability_metadata: dict
    pricing_metadata: dict
    created_at: datetime


def _get_profile(session: Session, profile_id: uuid.UUID) -> ModelProfile:
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="model profile not found")
    return require_default_team_access(session, profile, "model profile")


def _credential_for_profile(session: Session, credential_id: uuid.UUID, team_id: uuid.UUID) -> Credential:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=422, detail="credential not found")
    require_default_team_access(session, credential, "credential")
    if credential.team_id != team_id:
        raise HTTPException(status_code=422, detail="model profile requires a credential owned by its team")
    return credential


def _version_to_read(version: ModelProfileVersion) -> ModelProfileVersionRead:
    return ModelProfileVersionRead(
        id=version.id,
        model_profile_id=version.model_profile_id,
        version_number=version.version_number,
        base_url=version.base_url,
        model_identifier=version.model_identifier,
        credential_id=version.credential_id,
        headers=redact_mapping(version.headers),
        capability_metadata=redact_mapping(version.capability_metadata),
        pricing_metadata=redact_mapping(version.pricing_metadata),
        created_at=version.created_at,
    )


def _profile_to_read(profile: ModelProfile) -> ModelProfileRead:
    return ModelProfileRead(
        id=profile.id,
        team_id=profile.team_id,
        name=profile.name,
        base_url=profile.base_url,
        model_identifier=profile.model_identifier,
        capability_metadata=redact_mapping(profile.capability_metadata),
        pricing_metadata=redact_mapping(profile.pricing_metadata),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.post("", response_model=ModelProfileRead, status_code=201)
def create_model_profile(payload: ModelProfileCreate, session: Session = Depends(get_session)) -> ModelProfileRead:
    team, user = ensure_default_team_membership(session)
    credential_id = payload.credential_id
    api_key_ciphertext = encrypt_secret("")
    if payload.api_key is not None:
        secret = payload.api_key.get_secret_value()
        credential = Credential(
            team_id=team.id,
            created_by=user.id,
            name=f"{payload.name} API key",
            credential_type="api_key",
            encrypted_material=encrypt_secret(secret),
            metadata_={"managed_by": "model_profile"},
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
        api_key_ciphertext = encrypt_secret(secret)
    else:
        _credential_for_profile(session, credential_id, team.id)
    profile = ModelProfile(
        team_id=team.id,
        created_by=user.id,
        name=payload.name,
        base_url=payload.base_url,
        model_identifier=payload.model_identifier,
        api_key_ciphertext=api_key_ciphertext,
        capability_metadata=payload.capability_metadata,
        pricing_metadata=payload.pricing_metadata,
    )
    session.add(profile)
    session.flush()
    session.add(ModelProfileVersion(
        model_profile_id=profile.id,
        version_number=1,
        base_url=payload.base_url,
        model_identifier=payload.model_identifier,
        credential_id=credential_id,
        headers=payload.headers,
        capability_metadata=payload.capability_metadata,
        pricing_metadata=payload.pricing_metadata,
    ))
    session.flush()
    session.refresh(profile)
    return _profile_to_read(profile)


@router.get("", response_model=list[ModelProfileRead])
def list_model_profiles(session: Session = Depends(get_session)) -> list[ModelProfileRead]:
    team_id = ensure_default_team(session).id
    profiles = session.execute(
        select(ModelProfile).where(ModelProfile.team_id == team_id).order_by(ModelProfile.created_at)
    ).scalars()
    return [_profile_to_read(profile) for profile in profiles]


@router.get("/{model_profile_id}", response_model=ModelProfileRead)
def get_model_profile(model_profile_id: uuid.UUID, session: Session = Depends(get_session)) -> ModelProfileRead:
    return _profile_to_read(_get_profile(session, model_profile_id))


@router.post("/{model_profile_id}/versions", response_model=ModelProfileVersionRead, status_code=201)
def create_model_profile_version(model_profile_id: uuid.UUID, payload: ModelProfileVersionCreate, session: Session = Depends(get_session)) -> ModelProfileVersionRead:
    profile = _get_profile(session, model_profile_id)
    _credential_for_profile(session, payload.credential_id, profile.team_id)
    number = session.execute(select(func.coalesce(func.max(ModelProfileVersion.version_number), 0)).where(ModelProfileVersion.model_profile_id == model_profile_id)).scalar_one() + 1
    version = ModelProfileVersion(model_profile_id=model_profile_id, version_number=number, **payload.model_dump())
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


@router.get("/{model_profile_id}/versions", response_model=list[ModelProfileVersionRead])
def list_model_profile_versions(model_profile_id: uuid.UUID, session: Session = Depends(get_session)) -> list[ModelProfileVersionRead]:
    _get_profile(session, model_profile_id)
    versions = session.execute(select(ModelProfileVersion).where(ModelProfileVersion.model_profile_id == model_profile_id).order_by(ModelProfileVersion.version_number)).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{model_profile_id}/versions/{version_number}", response_model=ModelProfileVersionRead)
def get_model_profile_version(model_profile_id: uuid.UUID, version_number: int, session: Session = Depends(get_session)) -> ModelProfileVersionRead:
    _get_profile(session, model_profile_id)
    version = session.execute(select(ModelProfileVersion).where(ModelProfileVersion.model_profile_id == model_profile_id, ModelProfileVersion.version_number == version_number)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="model profile version not found")
    return _version_to_read(version)
