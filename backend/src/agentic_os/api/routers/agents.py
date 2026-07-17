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
    AuditEvent,
    Budget,
    McpServer,
    McpServerAttachment,
    McpServerTool,
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
    granted_by: uuid.UUID | None = None
    granted_at: datetime | None = None


class SkillGrantCreate(BaseModel):
    version_id: uuid.UUID
    resource_paths: list[str] = Field(default_factory=list)
    policy_metadata: dict = Field(default_factory=dict)

    @field_validator("resource_paths")
    @classmethod
    def _reject_duplicate_resources(cls, value: list[str]) -> list[str]:
        if not all(isinstance(path, str) and path for path in value):
            raise ValueError("resource paths must be non-empty strings")
        if len(value) != len(set(value)):
            raise ValueError("skill resources may only be granted once")
        return value


class SkillGrantRead(BaseModel):
    version_id: uuid.UUID
    skill_id: uuid.UUID
    resource_paths: list[str]
    declared_capabilities: list[str]
    package_hash: str | None
    provenance: dict
    policy_metadata: dict
    granted_by: uuid.UUID | None
    granted_at: datetime


class McpToolGrantCreate(BaseModel):
    version_id: uuid.UUID
    tool_names: list[str] = Field(min_length=1)
    policy_metadata: dict = Field(default_factory=dict)

    @field_validator("tool_names")
    @classmethod
    def _reject_duplicate_tools(cls, value: list[str]) -> list[str]:
        if not all(isinstance(name, str) and name for name in value):
            raise ValueError("tool names must be non-empty strings")
        if len(value) != len(set(value)):
            raise ValueError("MCP tools may only be granted once")
        return value


class McpGrantedToolRead(BaseModel):
    name: str
    descriptor_hash: str
    timeout_ms: int | None
    output_limit_bytes: int | None


class McpToolGrantRead(BaseModel):
    version_id: uuid.UUID
    mcp_server_id: uuid.UUID
    tools: list[McpGrantedToolRead]
    policy_metadata: dict
    credential_configured: bool
    granted_by: uuid.UUID | None
    granted_at: datetime


class AgentVersionCreate(BaseModel):
    instructions: str | None = None
    capability_manifest: dict = Field(default_factory=dict)
    model_profile_id: uuid.UUID | None = None
    model_profile_version_id: uuid.UUID | None = None
    default_budget_id: uuid.UUID | None = None
    skill_attachments: list[VersionAttachmentCreate] = Field(default_factory=list)
    mcp_server_attachments: list[VersionAttachmentCreate] = Field(default_factory=list)
    skill_grants: list[SkillGrantCreate] = Field(default_factory=list)
    mcp_tool_grants: list[McpToolGrantCreate] = Field(default_factory=list)
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

    @field_validator("skill_grants", "mcp_tool_grants")
    @classmethod
    def _reject_duplicate_grants(
        cls, value: list[SkillGrantCreate] | list[McpToolGrantCreate]
    ) -> list[SkillGrantCreate] | list[McpToolGrantCreate]:
        identifiers = [grant.version_id for grant in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("configuration versions may only be granted once")
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
    skill_grants: list[SkillGrantRead]
    mcp_tool_grants: list[McpToolGrantRead]
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


def _grant_error(
    code: str,
    message: str,
    *,
    status_code: int = 422,
    **evidence: object,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **evidence},
    )


def _credential_available_for_agent(
    session: Session,
    agent: Agent,
    mcp_server_version_id: uuid.UUID,
) -> bool:
    return (
        session.execute(
            select(McpServerAttachment.id).where(
                McpServerAttachment.mcp_server_version_id == mcp_server_version_id,
                McpServerAttachment.revoked_at.is_(None),
                McpServerAttachment.credential_id.isnot(None),
                or_(
                    McpServerAttachment.agent_id == agent.id,
                    McpServerAttachment.team_id == agent.team_id,
                ),
            )
        ).first()
        is not None
    )


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
    skills = list(session.execute(select(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version.id).order_by(AgentVersionSkill.created_at)).scalars())
    mcps = list(session.execute(select(AgentVersionMcpServer).where(AgentVersionMcpServer.agent_version_id == version.id).order_by(AgentVersionMcpServer.created_at)).scalars())
    policies = session.execute(select(AgentVersionPolicySet).where(AgentVersionPolicySet.agent_version_id == version.id).order_by(AgentVersionPolicySet.created_at)).scalars()
    skill_grants = []
    for item in skills:
        skill_version = session.get(SkillVersion, item.skill_version_id)
        if skill_version is None:
            continue
        config = redact_mapping(item.attachment_config)
        skill_grants.append(
            SkillGrantRead(
                version_id=skill_version.id,
                skill_id=skill_version.skill_id,
                resource_paths=list(config.get("resource_paths", [])),
                declared_capabilities=list(skill_version.declared_capabilities),
                package_hash=skill_version.package_hash,
                provenance=redact_mapping(skill_version.provenance),
                policy_metadata=redact_mapping(config.get("policy_metadata", {})),
                granted_by=item.granted_by,
                granted_at=item.created_at,
            )
        )
    mcp_tool_grants = []
    agent = session.get(Agent, version.agent_id)
    for item in mcps:
        mcp_version = session.get(McpServerVersion, item.mcp_server_version_id)
        if mcp_version is None:
            continue
        config = redact_mapping(item.attachment_config)
        tools = [
            McpGrantedToolRead(
                name=tool["name"],
                descriptor_hash=tool.get("descriptor_hash", ""),
                timeout_ms=tool.get("timeout_ms"),
                output_limit_bytes=tool.get("output_limit_bytes"),
            )
            for tool in config.get("tools", [])
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]
        mcp_tool_grants.append(
            McpToolGrantRead(
                version_id=mcp_version.id,
                mcp_server_id=mcp_version.mcp_server_id,
                tools=tools,
                policy_metadata=redact_mapping(config.get("policy_metadata", {})),
                credential_configured=(
                    agent is not None
                    and _credential_available_for_agent(session, agent, mcp_version.id)
                ),
                granted_by=item.granted_by,
                granted_at=item.created_at,
            )
        )
    return AgentVersionRead(
        id=version.id, agent_id=version.agent_id, version_number=version.version_number,
        instructions=version.instructions, capability_manifest=redact_mapping(version.capability_manifest),
        model_profile_id=version.model_profile_id, model_profile_version_id=version.model_profile_version_id,
        default_budget_id=version.default_budget_id,
        skill_attachments=[VersionAttachmentRead(version_id=item.skill_version_id, config=redact_mapping(item.attachment_config), granted_by=item.granted_by, granted_at=item.created_at) for item in skills],
        mcp_server_attachments=[VersionAttachmentRead(version_id=item.mcp_server_version_id, config=redact_mapping(item.attachment_config), granted_by=item.granted_by, granted_at=item.created_at) for item in mcps],
        skill_grants=skill_grants,
        mcp_tool_grants=mcp_tool_grants,
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
    legacy_skill_ids = {item.version_id for item in payload.skill_attachments}
    explicit_skill_ids = {item.version_id for item in payload.skill_grants}
    if legacy_skill_ids & explicit_skill_ids:
        raise _grant_error(
            "duplicate_skill_grant",
            "a skill version cannot be supplied as both an attachment and a grant",
        )
    legacy_mcp_ids = {item.version_id for item in payload.mcp_server_attachments}
    explicit_mcp_ids = {item.version_id for item in payload.mcp_tool_grants}
    if legacy_mcp_ids & explicit_mcp_ids:
        raise _grant_error(
            "duplicate_mcp_grant",
            "an MCP server version cannot be supplied as both an attachment and a grant",
        )
    skill_rows = []
    for attachment in payload.skill_attachments:
        item = session.get(SkillVersion, attachment.version_id)
        if item is None:
            raise HTTPException(status_code=422, detail="skill version not found")
        _require_same_team(session, agent, session.get(Skill, item.skill_id), "skill")
        skill_rows.append((item, attachment.config, None))
    for grant in payload.skill_grants:
        item = session.get(SkillVersion, grant.version_id)
        if item is None:
            raise _grant_error(
                "skill_version_not_found",
                "skill version not found",
                version_id=str(grant.version_id),
            )
        skill = session.get(Skill, item.skill_id)
        if skill is None or skill.team_id != agent.team_id:
            raise _grant_error(
                "skill_access_revoked",
                "install the accessible skill version into the agent team before granting it",
                status_code=403,
                version_id=str(grant.version_id),
            )
        if item.validation_status != "valid":
            raise _grant_error(
                "skill_version_not_valid",
                "only validated skill package versions can be granted",
                version_id=str(grant.version_id),
                validation_status=item.validation_status,
            )
        available_paths = {
            resource.get("path")
            for resource in item.resources
            if isinstance(resource, dict) and isinstance(resource.get("path"), str)
        }
        missing_paths = sorted(set(grant.resource_paths) - available_paths)
        if missing_paths:
            raise _grant_error(
                "skill_resource_not_found",
                "one or more selected skill resources do not exist",
                version_id=str(grant.version_id),
                resource_paths=missing_paths,
            )
        skill_rows.append(
            (
                item,
                {
                    "grant_type": "skill_resources",
                    "resource_paths": grant.resource_paths,
                    "policy_metadata": redact_mapping(grant.policy_metadata),
                },
                actor.id,
            )
        )
    mcp_rows = []
    for attachment in payload.mcp_server_attachments:
        item = session.get(McpServerVersion, attachment.version_id)
        if item is None:
            raise HTTPException(status_code=422, detail="MCP server version not found")
        _require_mcp_definition_access(agent, session.get(McpServer, item.mcp_server_id))
        mcp_rows.append((item, attachment.config, None))
    for grant in payload.mcp_tool_grants:
        item = session.get(McpServerVersion, grant.version_id)
        if item is None:
            raise _grant_error(
                "mcp_server_version_not_found",
                "MCP server version not found",
                version_id=str(grant.version_id),
            )
        server = session.get(McpServer, item.mcp_server_id)
        if server is None or not (
            server.team_id == agent.team_id
            or (server.project_id is None and server.visibility in ("team", "public"))
        ):
            raise _grant_error(
                "mcp_definition_access_revoked",
                "MCP definition is no longer accessible to the agent team",
                status_code=403,
                version_id=str(grant.version_id),
            )
        tools = list(
            session.execute(
                select(McpServerTool)
                .where(
                    McpServerTool.mcp_server_version_id == item.id,
                    McpServerTool.tool_name.in_(grant.tool_names),
                )
                .order_by(McpServerTool.tool_name)
            ).scalars()
        )
        found_names = {tool.tool_name for tool in tools}
        missing_names = sorted(set(grant.tool_names) - found_names)
        if missing_names:
            raise _grant_error(
                "mcp_tool_not_found",
                "one or more selected MCP tools were not discovered",
                version_id=str(grant.version_id),
                tool_names=missing_names,
            )
        disabled_names = sorted(
            tool.tool_name for tool in tools if not tool.enabled or not tool.schema_valid
        )
        if disabled_names:
            raise _grant_error(
                "mcp_tool_unavailable",
                "selected MCP tools must be enabled with valid schemas",
                version_id=str(grant.version_id),
                tool_names=disabled_names,
            )
        credential_required = bool(item.connection_config.get("credential_required")) or any(
            tool.credential_scope_required for tool in tools
        )
        credential_configured = _credential_available_for_agent(session, agent, item.id)
        if credential_required and not credential_configured:
            raise _grant_error(
                "mcp_credential_missing",
                "selected MCP tools require a credential grant for the agent or its team",
                version_id=str(grant.version_id),
            )
        mcp_rows.append(
            (
                item,
                {
                    "grant_type": "mcp_tools",
                    "tools": [
                        {
                            "name": tool.tool_name,
                            "descriptor_hash": tool.descriptor_hash,
                            "timeout_ms": tool.timeout_ms,
                            "output_limit_bytes": tool.output_limit_bytes,
                        }
                        for tool in tools
                    ],
                    "policy_metadata": redact_mapping(grant.policy_metadata),
                    "credential_required": credential_required,
                },
                actor.id,
            )
        )
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
    session.add_all([AgentVersionSkill(agent_version_id=version.id, skill_version_id=item.id, attachment_config=config, granted_by=granted_by) for item, config, granted_by in skill_rows])
    session.add_all([AgentVersionMcpServer(agent_version_id=version.id, mcp_server_version_id=item.id, attachment_config=config, granted_by=granted_by) for item, config, granted_by in mcp_rows])
    session.add_all([AgentVersionPolicySet(agent_version_id=version.id, policy_set_version_id=item.id) for item in policy_rows])
    session.flush()
    if payload.skill_grants or payload.mcp_tool_grants:
        session.add(
            AuditEvent(
                event_type="agent.capability_grants.created",
                payload={
                    "actor_id": str(actor.id),
                    "agent_id": str(agent.id),
                    "agent_version_id": str(version.id),
                    "skill_grants": [
                        {
                            "skill_version_id": str(item.version_id),
                            "resource_paths": item.resource_paths,
                        }
                        for item in payload.skill_grants
                    ],
                    "mcp_tool_grants": [
                        {
                            "mcp_server_version_id": str(item.version_id),
                            "tool_names": item.tool_names,
                        }
                        for item in payload.mcp_tool_grants
                    ],
                    "credential_material_redacted": True,
                    "policy_metadata_redacted": True,
                },
            )
        )
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
