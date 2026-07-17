from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import actor_team_ids, current_actor, primary_team_id, require_owned_scope
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.secrets import decrypt_secret, encrypt_secret
from agentic_os.domain.models import (
    Credential,
    ModelProfile,
    ModelProfileProbe,
    ModelProfileVersion,
    User,
)
from agentic_os.model_profiles import ProbeSettings, probe_model_profile

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


class ModelProfileProbeRequest(BaseModel):
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    max_attempts: int = Field(default=2, ge=1, le=3)


class ModelProfileProbeRead(BaseModel):
    id: uuid.UUID
    model_profile_version_id: uuid.UUID
    status: str
    capability_evidence: dict
    pricing_evidence: dict
    request_metadata: dict
    diagnostics: list[dict]
    started_at: datetime
    completed_at: datetime
    created_at: datetime


def _get_profile(session: Session, actor: User, profile_id: uuid.UUID) -> ModelProfile:
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="model profile not found")
    require_owned_scope(
        session, actor, profile, action="model_profile.read", resource_type="model profile"
    )
    return profile


def _credential_for_profile(
    session: Session, actor: User, credential_id: uuid.UUID, team_id: uuid.UUID
) -> Credential:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status_code=422, detail="credential not found")
    require_owned_scope(session, actor, credential, action="credential.use", resource_type="credential")
    if credential.team_id != team_id:
        raise HTTPException(status_code=403, detail="credential is not accessible for this model profile")
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


def _probe_to_read(probe: ModelProfileProbe) -> ModelProfileProbeRead:
    return ModelProfileProbeRead(
        id=probe.id,
        model_profile_version_id=probe.model_profile_version_id,
        status=probe.status,
        capability_evidence=redact_mapping(probe.capability_evidence),
        pricing_evidence=redact_mapping(probe.pricing_evidence),
        request_metadata=redact_mapping(probe.request_metadata),
        diagnostics=redact_mapping(probe.diagnostics),
        started_at=probe.started_at,
        completed_at=probe.completed_at,
        created_at=probe.created_at,
    )


def _get_version(
    session: Session,
    model_profile_id: uuid.UUID,
    version_number: int,
) -> ModelProfileVersion:
    version = session.execute(
        select(ModelProfileVersion).where(
            ModelProfileVersion.model_profile_id == model_profile_id,
            ModelProfileVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="model profile version not found")
    return version


@router.post("", response_model=ModelProfileRead, status_code=201)
def create_model_profile(
    payload: ModelProfileCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ModelProfileRead:
    team_id = primary_team_id(session, actor)
    credential_id = payload.credential_id
    api_key_ciphertext = encrypt_secret("")
    if payload.api_key is not None:
        secret = payload.api_key.get_secret_value()
        credential = Credential(
            team_id=team_id,
            created_by=actor.id,
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
        _credential_for_profile(session, actor, credential_id, team_id)
    profile = ModelProfile(
        team_id=team_id,
        created_by=actor.id,
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
def list_model_profiles(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[ModelProfileRead]:
    stmt = select(ModelProfile)
    if actor.role != "admin":
        stmt = stmt.where(ModelProfile.team_id.in_(actor_team_ids(session, actor)))
    profiles = session.execute(stmt.order_by(ModelProfile.created_at)).scalars()
    return [_profile_to_read(profile) for profile in profiles]


@router.get("/{model_profile_id}", response_model=ModelProfileRead)
def get_model_profile(
    model_profile_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ModelProfileRead:
    return _profile_to_read(_get_profile(session, actor, model_profile_id))


@router.post("/{model_profile_id}/versions", response_model=ModelProfileVersionRead, status_code=201)
def create_model_profile_version(
    model_profile_id: uuid.UUID,
    payload: ModelProfileVersionCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ModelProfileVersionRead:
    profile = _get_profile(session, actor, model_profile_id)
    _credential_for_profile(session, actor, payload.credential_id, profile.team_id)
    number = session.execute(select(func.coalesce(func.max(ModelProfileVersion.version_number), 0)).where(ModelProfileVersion.model_profile_id == model_profile_id)).scalar_one() + 1
    version = ModelProfileVersion(model_profile_id=model_profile_id, version_number=number, **payload.model_dump())
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


@router.get("/{model_profile_id}/versions", response_model=list[ModelProfileVersionRead])
def list_model_profile_versions(
    model_profile_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ModelProfileVersionRead]:
    _get_profile(session, actor, model_profile_id)
    versions = session.execute(select(ModelProfileVersion).where(ModelProfileVersion.model_profile_id == model_profile_id).order_by(ModelProfileVersion.version_number)).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{model_profile_id}/versions/{version_number}", response_model=ModelProfileVersionRead)
def get_model_profile_version(
    model_profile_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ModelProfileVersionRead:
    _get_profile(session, actor, model_profile_id)
    return _version_to_read(_get_version(session, model_profile_id, version_number))


@router.post(
    "/{model_profile_id}/versions/{version_number}/probe",
    response_model=ModelProfileProbeRead,
    status_code=201,
)
def probe_model_profile_version(
    model_profile_id: uuid.UUID,
    version_number: int,
    payload: ModelProfileProbeRequest,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> ModelProfileProbeRead:
    profile = _get_profile(session, actor, model_profile_id)
    version = _get_version(session, model_profile_id, version_number)
    if version.credential_id is None:
        raise HTTPException(status_code=422, detail="model profile version has no credential")
    credential = _credential_for_profile(
        session, actor, version.credential_id, profile.team_id
    )
    result = probe_model_profile(
        base_url=version.base_url,
        model_identifier=version.model_identifier,
        api_key=decrypt_secret(credential.encrypted_material),
        configured_headers=version.headers,
        pricing_metadata=version.pricing_metadata,
        settings=ProbeSettings(
            timeout_seconds=payload.timeout_seconds,
            max_attempts=payload.max_attempts,
        ),
    )
    probe = ModelProfileProbe(
        model_profile_version_id=version.id,
        **result,
    )
    session.add(probe)
    session.flush()
    session.refresh(probe)
    return _probe_to_read(probe)


@router.get(
    "/{model_profile_id}/versions/{version_number}/probes",
    response_model=list[ModelProfileProbeRead],
)
def list_model_profile_probes(
    model_profile_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[ModelProfileProbeRead]:
    _get_profile(session, actor, model_profile_id)
    version = _get_version(session, model_profile_id, version_number)
    probes = session.execute(
        select(ModelProfileProbe)
        .where(ModelProfileProbe.model_profile_version_id == version.id)
        .order_by(ModelProfileProbe.created_at.desc())
    ).scalars()
    return [_probe_to_read(probe) for probe in probes]
