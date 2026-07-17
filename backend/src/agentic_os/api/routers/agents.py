from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import (
    actor_team_ids,
    current_actor,
    primary_team_id,
    require_shared_definition_access,
    require_team_access,
)
from agentic_os.api.deps import get_session
from agentic_os.api.ownership import owner_team_id
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.capabilities import CAPABILITY_CATALOG
from agentic_os.domain.models import (
    Agent,
    AgentInstallation,
    AgentVersion,
    AgentVersionMcpServer,
    AgentVersionPolicySet,
    AgentVersionSkill,
    Budget,
    McpServer,
    McpServerVersion,
    ModelProfile,
    ModelProfileVersion,
    PolicySet,
    PolicySetVersion,
    Run,
    RunConfigurationSnapshot,
    Skill,
    SkillVersion,
    User,
)

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    visibility: str = "private"


class AgentUpdate(BaseModel):
    name: str | None = None
    visibility: str | None = None


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    team_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class AgentInstallCreate(BaseModel):
    name: str | None = None


class AgentInstallationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    installed_agent_id: uuid.UUID
    source_agent_version_id: uuid.UUID
    installed_by: uuid.UUID
    created_at: datetime


class VersionAttachmentCreate(BaseModel):
    version_id: uuid.UUID
    config: dict = Field(default_factory=dict)


class VersionAttachmentRead(BaseModel):
    version_id: uuid.UUID
    config: dict = Field(default_factory=dict)


class AgentVersionCreate(BaseModel):
    instructions: str | None = None
    capability_manifest: dict = Field(default_factory=dict)
    model_profile_id: uuid.UUID | None = None
    model_profile_version_id: uuid.UUID | None = None
    default_budget_id: uuid.UUID | None = None
    skill_attachments: list[VersionAttachmentCreate] = Field(default_factory=list)
    mcp_server_attachments: list[VersionAttachmentCreate] = Field(default_factory=list)
    policy_set_version_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("capability_manifest")
    @classmethod
    def _validate_capability_manifest(cls, value: dict) -> dict:
        declared = value.get("capabilities")
        if declared is None:
            return value
        if not isinstance(declared, list) or not all(isinstance(name, str) and name for name in declared):
            raise ValueError("capability_manifest.capabilities must be a list of non-empty capability names")
        unknown = sorted(set(declared) - set(CAPABILITY_CATALOG))
        if unknown:
            raise ValueError(f"capability_manifest declares unknown capabilities: {unknown}")
        return value

    @field_validator("skill_attachments", "mcp_server_attachments")
    @classmethod
    def _reject_duplicate_attachments(cls, value: list[VersionAttachmentCreate]) -> list[VersionAttachmentCreate]:
        identifiers = [attachment.version_id for attachment in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("configuration versions may only be attached once")
        return value

    @field_validator("policy_set_version_ids")
    @classmethod
    def _reject_duplicate_policy_sets(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("policy set versions may only be attached once")
        return value


class AgentVersionRead(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    instructions: str | None
    capability_manifest: dict
    model_profile_id: uuid.UUID | None
    model_profile_version_id: uuid.UUID | None
    default_budget_id: uuid.UUID | None
    skill_attachments: list[VersionAttachmentRead]
    mcp_server_attachments: list[VersionAttachmentRead]
    policy_set_version_ids: list[uuid.UUID]
    created_at: datetime


def _get_agent(session: Session, actor: User, agent_id: uuid.UUID) -> Agent:
    """Read access: home team membership, or `team`/`public` visibility, or admin."""

    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_shared_definition_access(session, actor, agent, action="agent.read", resource_type="agent")
    return agent


def _get_agent_for_mutation(session: Session, actor: User, agent_id: uuid.UUID) -> Agent:
    """Mutation access: home team membership only. Visibility never grants edit rights."""

    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    require_team_access(session, actor, agent.team_id, action="agent.write", resource_type="agent")
    return agent


def _agent_has_dependents(session: Session, agent_id: uuid.UUID) -> bool:
    version_ids = select(AgentVersion.id).where(AgentVersion.agent_id == agent_id)
    for stmt in (
        select(Run.id).where(Run.agent_version_id.in_(version_ids)),
        select(RunConfigurationSnapshot.id).where(RunConfigurationSnapshot.agent_version_id.in_(version_ids)),
        select(AgentInstallation.id).where(AgentInstallation.source_agent_version_id.in_(version_ids)),
    ):
        if session.execute(stmt.limit(1)).first() is not None:
            return True
    return False


def _require_same_team(session: Session, agent: Agent, resource: object, label: str) -> None:
    if owner_team_id(session, resource) != agent.team_id:
        raise HTTPException(status_code=403, detail=f"{label} belongs to another team")


def _require_mcp_definition_access(agent: Agent, server: McpServer | None) -> None:
    """Allow cross-team definition reuse without granting credential access."""

    if server is None:
        raise HTTPException(status_code=422, detail="MCP server not found")
    if server.team_id == agent.team_id:
        return
    if server.project_id is None and server.visibility in ("team", "public"):
        return
    raise HTTPException(status_code=403, detail="MCP server definition is not accessible")


def _resolve_model_version(session: Session, agent: Agent, payload: AgentVersionCreate) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    profile_id = payload.model_profile_id
    version = None
    if payload.model_profile_version_id is not None:
        version = session.get(ModelProfileVersion, payload.model_profile_version_id)
        if version is None:
            raise HTTPException(status_code=422, detail="model profile version not found")
        profile = session.get(ModelProfile, version.model_profile_id)
        _require_same_team(session, agent, profile, "model profile")
        if profile_id is not None and profile_id != profile.id:
            raise HTTPException(status_code=422, detail="model profile version does not belong to model profile")
        profile_id = profile.id
    elif profile_id is not None:
        profile = session.get(ModelProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=422, detail="model profile not found")
        _require_same_team(session, agent, profile, "model profile")
        version = session.execute(select(ModelProfileVersion).where(ModelProfileVersion.model_profile_id == profile_id).order_by(ModelProfileVersion.version_number.desc())).scalars().first()
        if version is None:
            raise HTTPException(status_code=422, detail="model profile has no version to pin")
    return profile_id, version.id if version else None


def _version_to_read(session: Session, version: AgentVersion) -> AgentVersionRead:
    skills = session.execute(select(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version.id).order_by(AgentVersionSkill.created_at)).scalars()
    mcps = session.execute(select(AgentVersionMcpServer).where(AgentVersionMcpServer.agent_version_id == version.id).order_by(AgentVersionMcpServer.created_at)).scalars()
    policies = session.execute(select(AgentVersionPolicySet).where(AgentVersionPolicySet.agent_version_id == version.id).order_by(AgentVersionPolicySet.created_at)).scalars()
    return AgentVersionRead(
        id=version.id, agent_id=version.agent_id, version_number=version.version_number,
        instructions=version.instructions, capability_manifest=redact_mapping(version.capability_manifest),
        model_profile_id=version.model_profile_id, model_profile_version_id=version.model_profile_version_id,
        default_budget_id=version.default_budget_id,
        skill_attachments=[VersionAttachmentRead(version_id=item.skill_version_id, config=redact_mapping(item.attachment_config)) for item in skills],
        mcp_server_attachments=[VersionAttachmentRead(version_id=item.mcp_server_version_id, config=redact_mapping(item.attachment_config)) for item in mcps],
        policy_set_version_ids=[item.policy_set_version_id for item in policies], created_at=version.created_at,
    )


@router.post("", response_model=AgentRead, status_code=201)
def create_agent(
    payload: AgentCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Agent:
    agent = Agent(
        team_id=primary_team_id(session, actor),
        created_by=actor.id,
        name=payload.name,
        visibility=payload.visibility,
    )
    session.add(agent)
    session.flush()
    session.refresh(agent)
    return agent


@router.get("", response_model=list[AgentRead])
def list_agents(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[Agent]:
    stmt = select(Agent)
    if actor.role != "admin":
        stmt = stmt.where(or_(Agent.team_id.in_(actor_team_ids(session, actor)), Agent.visibility == "public"))
    return list(session.execute(stmt.order_by(Agent.created_at)).scalars())


@router.get("/{agent_id}", response_model=AgentRead)
def get_agent(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Agent:
    return _get_agent(session, actor, agent_id)


@router.patch("/{agent_id}", response_model=AgentRead)
def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Agent:
    agent = _get_agent_for_mutation(session, actor, agent_id)
    updates = payload.model_dump(exclude_unset=True)
    if "visibility" in updates and updates["visibility"] != agent.visibility:
        if actor.role != "admin" and agent.created_by != actor.id:
            raise HTTPException(status_code=403, detail="only the owner or an admin can change visibility")
    for key, value in updates.items():
        setattr(agent, key, value)
    session.flush()
    session.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=204, response_model=None)
def delete_agent(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> None:
    agent = _get_agent_for_mutation(session, actor, agent_id)
    if _agent_has_dependents(session, agent_id):
        raise HTTPException(status_code=409, detail="agent has runs or installations referencing its versions")
    session.delete(agent)
    session.flush()


@router.post("/{agent_id}/versions/{version_number}/install", response_model=AgentRead, status_code=201)
def install_agent_version(
    agent_id: uuid.UUID,
    version_number: int,
    payload: AgentInstallCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Agent:
    """Pin a `team`/`public` source version into a new, independently governed agent.

    The installed agent is a fresh resource owned by the installer's team; it
    starts private and is decoupled from later edits to the source agent.
    """

    source_agent = _get_agent(session, actor, agent_id)
    source_version = session.execute(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version_number == version_number)
    ).scalar_one_or_none()
    if source_version is None:
        raise HTTPException(status_code=404, detail="agent version not found")

    installed_agent = Agent(
        team_id=primary_team_id(session, actor),
        created_by=actor.id,
        name=payload.name or source_agent.name,
        visibility="private",
    )
    session.add(installed_agent)
    session.flush()
    installed_version = AgentVersion(
        agent_id=installed_agent.id,
        version_number=1,
        capability_manifest=source_version.capability_manifest,
        instructions=source_version.instructions,
    )
    session.add(installed_version)
    session.add(
        AgentInstallation(
            installed_agent_id=installed_agent.id,
            source_agent_version_id=source_version.id,
            installed_by=actor.id,
        )
    )
    session.flush()
    session.refresh(installed_agent)
    return installed_agent


@router.get("/{agent_id}/installation", response_model=AgentInstallationRead)
def get_agent_installation(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> AgentInstallation:
    _get_agent(session, actor, agent_id)
    installation = session.execute(
        select(AgentInstallation).where(AgentInstallation.installed_agent_id == agent_id)
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status_code=404, detail="agent installation not found")
    return installation


@router.post("/{agent_id}/versions", response_model=AgentVersionRead, status_code=201)
def create_agent_version(
    agent_id: uuid.UUID,
    payload: AgentVersionCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> AgentVersionRead:
    agent = _get_agent_for_mutation(session, actor, agent_id)
    profile_id, profile_version_id = _resolve_model_version(session, agent, payload)
    if payload.default_budget_id is not None:
        budget = session.get(Budget, payload.default_budget_id)
        if budget is None:
            raise HTTPException(status_code=422, detail="budget not found")
        if budget.agent_id != agent.id:
            raise HTTPException(status_code=422, detail="budget belongs to another agent")
    skill_rows = []
    for attachment in payload.skill_attachments:
        item = session.get(SkillVersion, attachment.version_id)
        if item is None:
            raise HTTPException(status_code=422, detail="skill version not found")
        _require_same_team(session, agent, session.get(Skill, item.skill_id), "skill")
        skill_rows.append((item, attachment.config))
    mcp_rows = []
    for attachment in payload.mcp_server_attachments:
        item = session.get(McpServerVersion, attachment.version_id)
        if item is None:
            raise HTTPException(status_code=422, detail="MCP server version not found")
        _require_mcp_definition_access(agent, session.get(McpServer, item.mcp_server_id))
        mcp_rows.append((item, attachment.config))
    policy_rows = []
    for version_id in payload.policy_set_version_ids:
        item = session.get(PolicySetVersion, version_id)
        if item is None:
            raise HTTPException(status_code=422, detail="policy set version not found")
        _require_same_team(session, agent, session.get(PolicySet, item.policy_set_id), "policy set")
        policy_rows.append(item)
    number = session.execute(select(func.coalesce(func.max(AgentVersion.version_number), 0)).where(AgentVersion.agent_id == agent_id)).scalar_one() + 1
    version = AgentVersion(
        agent_id=agent_id, version_number=number, instructions=payload.instructions,
        capability_manifest=payload.capability_manifest, model_profile_id=profile_id,
        model_profile_version_id=profile_version_id, default_budget_id=payload.default_budget_id,
    )
    session.add(version)
    session.flush()
    session.add_all([AgentVersionSkill(agent_version_id=version.id, skill_version_id=item.id, attachment_config=config) for item, config in skill_rows])
    session.add_all([AgentVersionMcpServer(agent_version_id=version.id, mcp_server_version_id=item.id, attachment_config=config) for item, config in mcp_rows])
    session.add_all([AgentVersionPolicySet(agent_version_id=version.id, policy_set_version_id=item.id) for item in policy_rows])
    session.flush()
    session.refresh(version)
    return _version_to_read(session, version)


@router.get("/{agent_id}/versions", response_model=list[AgentVersionRead])
def list_agent_versions(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[AgentVersionRead]:
    _get_agent(session, actor, agent_id)
    versions = session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version_number)).scalars()
    return [_version_to_read(session, version) for version in versions]


@router.get("/{agent_id}/versions/{version_number}", response_model=AgentVersionRead)
def get_agent_version(
    agent_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> AgentVersionRead:
    _get_agent(session, actor, agent_id)
    version = session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version_number == version_number)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="agent version not found")
    return _version_to_read(session, version)
