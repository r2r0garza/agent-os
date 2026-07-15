from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.domain.models import Agent, AgentVersion, Budget, ModelProfile

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    visibility: str = "private"


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class AgentVersionCreate(BaseModel):
    instructions: str | None = None
    capability_manifest: dict = {}
    model_profile_id: uuid.UUID | None = None
    default_budget_id: uuid.UUID | None = None


class AgentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    instructions: str | None
    capability_manifest: dict
    model_profile_id: uuid.UUID | None
    default_budget_id: uuid.UUID | None
    created_at: datetime


@router.post("", response_model=AgentRead, status_code=201)
def create_agent(payload: AgentCreate, session: Session = Depends(get_session)) -> Agent:
    team = ensure_default_team(session)
    user = ensure_default_user(session)
    agent = Agent(team_id=team.id, created_by=user.id, name=payload.name, visibility=payload.visibility)
    session.add(agent)
    session.flush()
    session.refresh(agent)
    return agent


@router.get("", response_model=list[AgentRead])
def list_agents(session: Session = Depends(get_session)) -> list[Agent]:
    return list(session.execute(select(Agent).order_by(Agent.created_at)).scalars())


@router.get("/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: uuid.UUID, session: Session = Depends(get_session)) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


@router.post("/{agent_id}/versions", response_model=AgentVersionRead, status_code=201)
def create_agent_version(
    agent_id: uuid.UUID, payload: AgentVersionCreate, session: Session = Depends(get_session)
) -> AgentVersion:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if payload.model_profile_id is not None and session.get(ModelProfile, payload.model_profile_id) is None:
        raise HTTPException(status_code=422, detail="model profile not found")
    if payload.default_budget_id is not None and session.get(Budget, payload.default_budget_id) is None:
        raise HTTPException(status_code=422, detail="budget not found")

    next_version = (
        session.execute(
            select(func.coalesce(func.max(AgentVersion.version_number), 0)).where(
                AgentVersion.agent_id == agent_id
            )
        ).scalar_one()
        + 1
    )
    version = AgentVersion(
        agent_id=agent_id,
        version_number=next_version,
        instructions=payload.instructions,
        capability_manifest=payload.capability_manifest,
        model_profile_id=payload.model_profile_id,
        default_budget_id=payload.default_budget_id,
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    return version


@router.get("/{agent_id}/versions", response_model=list[AgentVersionRead])
def list_agent_versions(agent_id: uuid.UUID, session: Session = Depends(get_session)) -> list[AgentVersion]:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return list(
        session.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version_number)
        ).scalars()
    )


@router.get("/{agent_id}/versions/{version_number}", response_model=AgentVersionRead)
def get_agent_version(
    agent_id: uuid.UUID, version_number: int, session: Session = Depends(get_session)
) -> AgentVersion:
    version = session.execute(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent_id, AgentVersion.version_number == version_number
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="agent version not found")
    return version
