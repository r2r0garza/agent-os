from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.api.secrets import encrypt_secret
from agentic_os.domain.models import ModelProfile

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])


class ModelProfileCreate(BaseModel):
    name: str
    base_url: str
    model_identifier: str
    api_key: str
    capability_metadata: dict = {}
    pricing_metadata: dict = {}


class ModelProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    model_identifier: str
    capability_metadata: dict
    pricing_metadata: dict
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=ModelProfileRead, status_code=201)
def create_model_profile(payload: ModelProfileCreate, session: Session = Depends(get_session)) -> ModelProfile:
    team = ensure_default_team(session)
    user = ensure_default_user(session)
    profile = ModelProfile(
        team_id=team.id,
        created_by=user.id,
        name=payload.name,
        base_url=payload.base_url,
        model_identifier=payload.model_identifier,
        api_key_ciphertext=encrypt_secret(payload.api_key),
        capability_metadata=payload.capability_metadata,
        pricing_metadata=payload.pricing_metadata,
    )
    session.add(profile)
    session.flush()
    session.refresh(profile)
    return profile


@router.get("", response_model=list[ModelProfileRead])
def list_model_profiles(session: Session = Depends(get_session)) -> list[ModelProfile]:
    return list(session.execute(select(ModelProfile).order_by(ModelProfile.created_at)).scalars())


@router.get("/{model_profile_id}", response_model=ModelProfileRead)
def get_model_profile(model_profile_id: uuid.UUID, session: Session = Depends(get_session)) -> ModelProfile:
    profile = session.get(ModelProfile, model_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="model profile not found")
    return profile
