from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import (
    can_access_project,
    can_access_owned_scope,
    current_actor,
    has_team_access,
    primary_team_id,
    require_project_access,
    require_team_access,
)
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.mcp import DiscoverySettings, discover_mcp_tools
from agentic_os.secrets import decrypt_secret, encrypt_secret
from agentic_os.domain.models import (
    Agent,
    AuditEvent,
    Credential,
    McpServer,
    McpServerAttachment,
    McpServerHealthCheck,
    McpServerInstallation,
    McpServerTool,
    McpServerVersion,
    Project,
    User,
)

MIN_TIMEOUT_MS = 1
MAX_TIMEOUT_MS = 300_000
MIN_OUTPUT_LIMIT_BYTES = 1
MAX_OUTPUT_LIMIT_BYTES = 1_048_576

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


class McpServerCreate(BaseModel):
    name: str
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    visibility: str = "private"


class McpServerUpdate(BaseModel):
    name: str | None = None
    visibility: str | None = None


class McpServerInstallCreate(BaseModel):
    name: str | None = None


class McpServerInstallationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    installed_mcp_server_id: uuid.UUID
    source_mcp_server_version_id: uuid.UUID
    installed_by: uuid.UUID
    created_at: datetime


class McpServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class McpServerVersionCreate(BaseModel):
    connection_config: dict = Field(default_factory=dict)
    credential: SecretStr | None = None
    credential_id: uuid.UUID | None = None


class McpServerVersionRead(BaseModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    version_number: int
    connection_config: dict
    credential_configured: bool
    credential_id: uuid.UUID | None
    created_at: datetime


class McpServerAttachmentCreate(BaseModel):
    credential_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None


class McpServerAttachmentRead(BaseModel):
    id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    credential_configured: bool
    revoked: bool
    created_at: datetime


class McpDiscoveryRequest(BaseModel):
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    max_attempts: int = Field(default=2, ge=1, le=3)


class McpServerHealthCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    status: str
    tool_count: int
    latency_ms: int | None
    request_metadata: dict
    diagnostics: list
    checked_at: datetime
    created_at: datetime


class McpServerToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    tool_name: str
    description: str | None
    input_schema: dict
    schema_valid: bool
    schema_validation_errors: list
    descriptor_hash: str
    credential_scope_required: bool
    enabled: bool
    timeout_ms: int | None
    output_limit_bytes: int | None
    last_discovered_at: datetime
    created_at: datetime
    updated_at: datetime


class McpServerToolUpdate(BaseModel):
    enabled: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)
    output_limit_bytes: int | None = Field(
        default=None, ge=MIN_OUTPUT_LIMIT_BYTES, le=MAX_OUTPUT_LIMIT_BYTES
    )


def _version_to_read(
    session: Session, actor: User, version: McpServerVersion
) -> McpServerVersionRead:
    grants = session.execute(
        select(McpServerAttachment).where(
            McpServerAttachment.mcp_server_version_id == version.id,
            McpServerAttachment.revoked_at.is_(None),
        )
    ).scalars()
    credential_configured = any(
        grant.credential_id is not None and _can_access_attachment_target(session, actor, grant)
        for grant in grants
    )
    return McpServerVersionRead(
        id=version.id,
        mcp_server_id=version.mcp_server_id,
        version_number=version.version_number,
        connection_config=redact_mapping(version.connection_config),
        credential_configured=credential_configured,
        # Definition responses never expose the credential-to-definition link.
        credential_id=None,
        created_at=version.created_at,
    )


def _can_read_server(session: Session, actor: User, server: McpServer) -> bool:
    if actor.role == "admin":
        return True
    if server.project_id is not None:
        project = session.get(Project, server.project_id)
        return project is not None and can_access_project(session, actor, project)
    if server.team_id is not None and has_team_access(session, actor, server.team_id):
        return True
    return server.visibility in ("team", "public")


def _require_server_access(
    session: Session, actor: User, server: McpServer, *, action: str, mutate: bool = False
) -> None:
    if not mutate and _can_read_server(session, actor, server):
        return
    if server.project_id is not None:
        require_project_access(session, actor, server.project_id, action=action)
    elif server.team_id is not None:
        require_team_access(
            session, actor, server.team_id, action=action, resource_type="mcp server"
        )
    else:
        raise HTTPException(status_code=404, detail="mcp server not found")


def _can_access_attachment_target(
    session: Session, actor: User, attachment: McpServerAttachment
) -> bool:
    if actor.role == "admin":
        return True
    if attachment.team_id is not None:
        return has_team_access(session, actor, attachment.team_id)
    if attachment.project_id is not None:
        project = session.get(Project, attachment.project_id)
        return project is not None and can_access_project(session, actor, project)
    if attachment.agent_id is not None:
        agent = session.get(Agent, attachment.agent_id)
        return agent is not None and has_team_access(session, actor, agent.team_id)
    return False


def _attachment_to_read(attachment: McpServerAttachment) -> McpServerAttachmentRead:
    return McpServerAttachmentRead(
        id=attachment.id,
        mcp_server_version_id=attachment.mcp_server_version_id,
        team_id=attachment.team_id,
        project_id=attachment.project_id,
        agent_id=attachment.agent_id,
        credential_configured=attachment.credential_id is not None,
        revoked=attachment.revoked_at is not None,
        created_at=attachment.created_at,
    )


def _health_check_to_read(check: McpServerHealthCheck) -> McpServerHealthCheckRead:
    return McpServerHealthCheckRead(
        id=check.id,
        mcp_server_version_id=check.mcp_server_version_id,
        status=check.status,
        tool_count=check.tool_count,
        latency_ms=check.latency_ms,
        request_metadata=redact_mapping(check.request_metadata),
        diagnostics=redact_mapping(check.diagnostics),
        checked_at=check.checked_at,
        created_at=check.created_at,
    )


def _tool_to_read(tool: McpServerTool) -> McpServerToolRead:
    return McpServerToolRead(
        id=tool.id,
        mcp_server_version_id=tool.mcp_server_version_id,
        tool_name=tool.tool_name,
        description=tool.description,
        input_schema=redact_mapping(tool.input_schema),
        schema_valid=tool.schema_valid,
        schema_validation_errors=redact_mapping(tool.schema_validation_errors),
        descriptor_hash=tool.descriptor_hash,
        credential_scope_required=tool.credential_scope_required,
        enabled=tool.enabled,
        timeout_ms=tool.timeout_ms,
        output_limit_bytes=tool.output_limit_bytes,
        last_discovered_at=tool.last_discovered_at,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _discovery_credential_value(
    session: Session, server: McpServer, version: McpServerVersion
) -> str | None:
    grants = session.execute(
        select(McpServerAttachment).where(
            McpServerAttachment.mcp_server_version_id == version.id,
            McpServerAttachment.revoked_at.is_(None),
            McpServerAttachment.credential_id.isnot(None),
        )
    ).scalars()
    for grant in grants:
        owner_scope_matches = (
            server.team_id is not None and grant.team_id == server.team_id
        ) or (server.project_id is not None and grant.project_id == server.project_id)
        if not owner_scope_matches:
            continue
        credential = session.get(Credential, grant.credential_id)
        if credential is not None:
            return decrypt_secret(credential.encrypted_material)
    return None


@router.post("", response_model=McpServerRead, status_code=201)
def create_mcp_server(
    payload: McpServerCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServer:
    team_id = payload.team_id
    project_id = payload.project_id
    if project_id is not None and session.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail="project not found")
    if team_id is not None and project_id is not None:
        raise HTTPException(status_code=422, detail="MCP server must have exactly one owner scope")
    if project_id is not None and payload.visibility != "private":
        raise HTTPException(status_code=422, detail="project MCP definitions must remain private")
    if team_id is not None:
        require_team_access(
            session, actor, team_id, action="mcp_server.create", resource_type="team"
        )
    if project_id is not None:
        require_project_access(session, actor, project_id, action="mcp_server.create")
    if team_id is None and project_id is None:
        team_id = primary_team_id(session, actor)
    server = McpServer(
        team_id=team_id,
        project_id=project_id,
        created_by=actor.id,
        name=payload.name,
        visibility=payload.visibility,
    )
    session.add(server)
    session.flush()
    session.refresh(server)
    return server


@router.get("", response_model=list[McpServerRead])
def list_mcp_servers(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[McpServer]:
    servers = session.execute(select(McpServer).order_by(McpServer.created_at)).scalars()
    return [server for server in servers if _can_read_server(session, actor, server)]


@router.get("/{mcp_server_id}", response_model=McpServerRead)
def get_mcp_server(
    mcp_server_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServer:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.read")
    return server


@router.patch("/{mcp_server_id}", response_model=McpServerRead)
def update_mcp_server(
    mcp_server_id: uuid.UUID,
    payload: McpServerUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServer:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(
        session, actor, server, action="mcp_server.update", mutate=True
    )
    updates = payload.model_dump(exclude_unset=True)
    if server.project_id is not None and updates.get("visibility", "private") != "private":
        raise HTTPException(status_code=422, detail="project MCP definitions must remain private")
    if "visibility" in updates and updates["visibility"] != server.visibility:
        if actor.role != "admin" and server.created_by != actor.id:
            raise HTTPException(status_code=403, detail="only the owner or an admin can change visibility")
    for key, value in updates.items():
        setattr(server, key, value)
    session.flush()
    session.refresh(server)
    return server


@router.post(
    "/{mcp_server_id}/versions/{version_number}/install",
    response_model=McpServerRead,
    status_code=201,
)
def install_mcp_server_version(
    mcp_server_id: uuid.UUID,
    version_number: int,
    payload: McpServerInstallCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServer:
    """Install a visible definition without copying credential authority."""

    source = session.get(McpServer, mcp_server_id)
    if source is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, source, action="mcp_server.install")
    source_version = _get_version(session, mcp_server_id, version_number)
    target_team_id = primary_team_id(session, actor)

    installed = McpServer(
        team_id=target_team_id,
        project_id=None,
        created_by=actor.id,
        name=payload.name or source.name,
        visibility="private",
    )
    session.add(installed)
    session.flush()
    installed_version = McpServerVersion(
        mcp_server_id=installed.id,
        version_number=1,
        connection_config=redact_mapping(source_version.connection_config),
        credential_ciphertext=None,
        credential_id=None,
    )
    session.add(installed_version)
    session.flush()

    source_tools = session.execute(
        select(McpServerTool)
        .where(McpServerTool.mcp_server_version_id == source_version.id)
        .order_by(McpServerTool.tool_name)
    ).scalars()
    for tool in source_tools:
        session.add(
            McpServerTool(
                mcp_server_version_id=installed_version.id,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=redact_mapping(tool.input_schema),
                schema_valid=tool.schema_valid,
                schema_validation_errors=redact_mapping(tool.schema_validation_errors),
                descriptor_hash=tool.descriptor_hash,
                credential_scope_required=tool.credential_scope_required,
                enabled=tool.enabled,
                timeout_ms=tool.timeout_ms,
                output_limit_bytes=tool.output_limit_bytes,
                last_discovered_at=tool.last_discovered_at,
            )
        )
    installation = McpServerInstallation(
        installed_mcp_server_id=installed.id,
        source_mcp_server_version_id=source_version.id,
        installed_by=actor.id,
    )
    session.add(installation)
    session.add(
        AuditEvent(
            event_type="mcp.definition.installed",
            payload={
                "actor_id": str(actor.id),
                "installed_mcp_server_id": str(installed.id),
                "source_mcp_server_version_id": str(source_version.id),
                "target_team_id": str(target_team_id),
                "credentials_copied": False,
            },
        )
    )
    session.flush()
    session.refresh(installed)
    return installed


@router.get(
    "/{mcp_server_id}/installation",
    response_model=McpServerInstallationRead,
)
def get_mcp_server_installation(
    mcp_server_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerInstallation:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.installation.read")
    installation = session.execute(
        select(McpServerInstallation).where(
            McpServerInstallation.installed_mcp_server_id == mcp_server_id
        )
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status_code=404, detail="mcp server installation not found")
    return installation


@router.post("/{mcp_server_id}/versions", response_model=McpServerVersionRead, status_code=201)
def create_mcp_server_version(
    mcp_server_id: uuid.UUID,
    payload: McpServerVersionCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerVersionRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(
        session, actor, server, action="mcp_server.version.create", mutate=True
    )
    if payload.credential is not None and payload.credential_id is not None:
        raise HTTPException(status_code=422, detail="provide credential or credential_id, not both")
    credential = None
    if payload.credential_id is not None:
        credential = session.get(Credential, payload.credential_id)
        if credential is None:
            raise HTTPException(status_code=422, detail="credential not found")
        if credential.project_id is not None:
            require_project_access(session, actor, credential.project_id, action="credential.attach")
        elif credential.team_id is not None:
            require_team_access(
                session, actor, credential.team_id, action="credential.attach", resource_type="credential"
            )
        if server.project_id is not None and credential.project_id not in (None, server.project_id):
            raise HTTPException(status_code=422, detail="credential belongs to another project")
        if server.team_id is not None and credential.team_id != server.team_id:
            raise HTTPException(status_code=422, detail="team MCP server requires a team credential")
    next_version = (
        session.execute(
            select(func.coalesce(func.max(McpServerVersion.version_number), 0)).where(
                McpServerVersion.mcp_server_id == mcp_server_id
            )
        ).scalar_one()
        + 1
    )
    connection_config = redact_mapping(payload.connection_config)
    if payload.credential is not None or payload.credential_id is not None:
        connection_config["credential_required"] = True
    version = McpServerVersion(
        mcp_server_id=mcp_server_id,
        version_number=next_version,
        connection_config=connection_config,
        credential_ciphertext=None,
        credential_id=None,
    )
    session.add(version)
    session.flush()
    if payload.credential is not None:
        credential = Credential(
            team_id=server.team_id,
            project_id=server.project_id,
            created_by=actor.id,
            name=f"{server.name} credential v{next_version}",
            credential_type="mcp_inline",
            encrypted_material=encrypt_secret(payload.credential.get_secret_value()),
            metadata_={"mcp_server_version_id": str(version.id)},
        )
        session.add(credential)
        session.flush()
    if credential is not None:
        grant = McpServerAttachment(
            mcp_server_version_id=version.id,
            credential_id=credential.id,
            team_id=server.team_id,
            project_id=server.project_id,
            created_by=actor.id,
        )
        session.add(grant)
        session.flush()
    session.refresh(version)
    return _version_to_read(session, actor, version)


@router.get("/{mcp_server_id}/versions", response_model=list[McpServerVersionRead])
def list_mcp_server_versions(
    mcp_server_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[McpServerVersionRead]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.version.list")
    versions = session.execute(
        select(McpServerVersion)
        .where(McpServerVersion.mcp_server_id == mcp_server_id)
        .order_by(McpServerVersion.version_number)
    ).scalars()
    return [_version_to_read(session, actor, version) for version in versions]


@router.get("/{mcp_server_id}/versions/{version_number}", response_model=McpServerVersionRead)
def get_mcp_server_version(
    mcp_server_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerVersionRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.version.read")
    version = session.execute(
        select(McpServerVersion).where(
            McpServerVersion.mcp_server_id == mcp_server_id,
            McpServerVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="mcp server version not found")
    return _version_to_read(session, actor, version)


def _get_version(
    session: Session, mcp_server_id: uuid.UUID, version_number: int
) -> McpServerVersion:
    version = session.execute(
        select(McpServerVersion).where(
            McpServerVersion.mcp_server_id == mcp_server_id,
            McpServerVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="mcp server version not found")
    return version


@router.post(
    "/{mcp_server_id}/versions/{version_number}/attachments",
    response_model=McpServerAttachmentRead,
    status_code=201,
)
def create_mcp_server_attachment(
    mcp_server_id: uuid.UUID,
    version_number: int,
    payload: McpServerAttachmentCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerAttachmentRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.attach")
    version = _get_version(session, mcp_server_id, version_number)
    targets = [payload.team_id, payload.project_id, payload.agent_id]
    if sum(item is not None for item in targets) != 1:
        raise HTTPException(status_code=422, detail="attachment must have exactly one target scope")

    target_team_id = payload.team_id
    target_project = None
    if payload.team_id is not None:
        require_team_access(
            session, actor, payload.team_id, action="mcp_server.attach", resource_type="team"
        )
    elif payload.project_id is not None:
        target_project = require_project_access(
            session, actor, payload.project_id, action="mcp_server.attach"
        )
        target_team_id = target_project.team_id
    else:
        target_agent = session.get(Agent, payload.agent_id)
        if target_agent is None:
            raise HTTPException(status_code=422, detail="agent not found")
        require_team_access(
            session,
            actor,
            target_agent.team_id,
            action="mcp_server.attach",
            resource_type="agent",
        )
        target_team_id = target_agent.team_id

    credential = None
    if payload.credential_id is not None:
        credential = session.get(Credential, payload.credential_id)
        if credential is None or not can_access_owned_scope(session, actor, credential):
            raise HTTPException(status_code=404, detail="credential not found")
        credential_matches = credential.team_id == target_team_id
        if target_project is not None:
            credential_matches = credential_matches or credential.project_id == target_project.id
        if not credential_matches:
            raise HTTPException(status_code=403, detail="credential is outside the attachment scope")

    attachment = McpServerAttachment(
        mcp_server_version_id=version.id,
        credential_id=credential.id if credential else None,
        team_id=payload.team_id,
        project_id=payload.project_id,
        agent_id=payload.agent_id,
        created_by=actor.id,
    )
    session.add(attachment)
    session.flush()
    session.add(
        AuditEvent(
            project_id=payload.project_id,
            event_type="mcp.attachment.created",
            payload={
                "actor_id": str(actor.id),
                "mcp_server_id": str(server.id),
                "mcp_server_version_id": str(version.id),
                "attachment_id": str(attachment.id),
                "target_scope": (
                    "team" if payload.team_id else "project" if payload.project_id else "agent"
                ),
                "credential_configured": credential is not None,
                "credential_material_redacted": True,
            },
        )
    )
    session.refresh(attachment)
    return _attachment_to_read(attachment)


@router.get(
    "/{mcp_server_id}/versions/{version_number}/attachments",
    response_model=list[McpServerAttachmentRead],
)
def list_mcp_server_attachments(
    mcp_server_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[McpServerAttachmentRead]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.attachment.list")
    version = _get_version(session, mcp_server_id, version_number)
    attachments = session.execute(
        select(McpServerAttachment)
        .where(McpServerAttachment.mcp_server_version_id == version.id)
        .order_by(McpServerAttachment.created_at)
    ).scalars()
    return [
        _attachment_to_read(item)
        for item in attachments
        if _can_access_attachment_target(session, actor, item)
    ]


@router.delete(
    "/{mcp_server_id}/versions/{version_number}/attachments/{attachment_id}",
    response_model=McpServerAttachmentRead,
)
def revoke_mcp_server_attachment(
    mcp_server_id: uuid.UUID,
    version_number: int,
    attachment_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerAttachmentRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.attachment.revoke")
    version = _get_version(session, mcp_server_id, version_number)
    attachment = session.get(McpServerAttachment, attachment_id)
    if attachment is None or attachment.mcp_server_version_id != version.id:
        raise HTTPException(status_code=404, detail="mcp server attachment not found")
    if not _can_access_attachment_target(session, actor, attachment):
        raise HTTPException(status_code=404, detail="mcp server attachment not found")
    if attachment.revoked_at is None:
        attachment.revoked_at = datetime.now(timezone.utc)
        session.add(
            AuditEvent(
                project_id=attachment.project_id,
                event_type="mcp.attachment.revoked",
                payload={
                    "actor_id": str(actor.id),
                    "mcp_server_id": str(server.id),
                    "mcp_server_version_id": str(version.id),
                    "attachment_id": str(attachment.id),
                    "credential_material_redacted": True,
                },
            )
        )
        session.flush()
    return _attachment_to_read(attachment)


@router.get("/{mcp_server_id}/versions/{version_number}/tools", response_model=list[dict[str, Any]])
def get_mcp_server_tools(
    mcp_server_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[dict[str, Any]]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.tools.read")
    version = session.execute(
        select(McpServerVersion).where(
            McpServerVersion.mcp_server_id == mcp_server_id,
            McpServerVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="mcp server version not found")
    return redact_mapping(version.connection_config.get("tools", []))


@router.post(
    "/{mcp_server_id}/versions/{version_number}/health-checks",
    response_model=McpServerHealthCheckRead,
    status_code=201,
)
def run_mcp_server_discovery(
    mcp_server_id: uuid.UUID,
    version_number: int,
    payload: McpDiscoveryRequest,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerHealthCheckRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.discovery.run")
    version = _get_version(session, mcp_server_id, version_number)

    connection_config = version.connection_config or {}
    credential_value = _discovery_credential_value(session, server, version)
    result = discover_mcp_tools(
        url=connection_config.get("url"),
        headers=connection_config.get("headers") or {},
        credential_value=credential_value,
        settings=DiscoverySettings(
            timeout_seconds=payload.timeout_seconds, max_attempts=payload.max_attempts
        ),
    )

    check = McpServerHealthCheck(
        mcp_server_version_id=version.id,
        status=result["status"],
        tool_count=result["tool_count"],
        latency_ms=result["latency_ms"],
        request_metadata=result["request_metadata"],
        diagnostics=result["diagnostics"],
        checked_at=result["checked_at"],
        triggered_by=actor.id,
    )
    session.add(check)

    if result["status"] in ("healthy", "degraded"):
        for discovered in result["tools"]:
            existing = session.execute(
                select(McpServerTool).where(
                    McpServerTool.mcp_server_version_id == version.id,
                    McpServerTool.tool_name == discovered["name"],
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    McpServerTool(
                        mcp_server_version_id=version.id,
                        tool_name=discovered["name"],
                        description=discovered["description"],
                        input_schema=discovered["input_schema"],
                        schema_valid=discovered["schema_valid"],
                        schema_validation_errors=discovered["schema_validation_errors"],
                        descriptor_hash=discovered["descriptor_hash"],
                        credential_scope_required=discovered["credential_scope_required"],
                        last_discovered_at=result["checked_at"],
                    )
                )
            else:
                existing.description = discovered["description"]
                existing.input_schema = discovered["input_schema"]
                existing.schema_valid = discovered["schema_valid"]
                existing.schema_validation_errors = discovered["schema_validation_errors"]
                existing.descriptor_hash = discovered["descriptor_hash"]
                existing.credential_scope_required = discovered["credential_scope_required"]
                existing.last_discovered_at = result["checked_at"]

    session.flush()
    session.add(
        AuditEvent(
            project_id=server.project_id,
            event_type="mcp.health_check.recorded",
            payload={
                "actor_id": str(actor.id),
                "mcp_server_id": str(server.id),
                "mcp_server_version_id": str(version.id),
                "health_check_id": str(check.id),
                "status": check.status,
                "tool_count": check.tool_count,
                "credential_material_redacted": True,
            },
        )
    )
    session.flush()
    session.refresh(check)
    return _health_check_to_read(check)


@router.get(
    "/{mcp_server_id}/versions/{version_number}/health-checks",
    response_model=list[McpServerHealthCheckRead],
)
def list_mcp_server_health_checks(
    mcp_server_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[McpServerHealthCheckRead]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.discovery.read")
    version = _get_version(session, mcp_server_id, version_number)
    checks = session.execute(
        select(McpServerHealthCheck)
        .where(McpServerHealthCheck.mcp_server_version_id == version.id)
        .order_by(McpServerHealthCheck.checked_at.desc())
    ).scalars()
    return [_health_check_to_read(check) for check in checks]


@router.get(
    "/{mcp_server_id}/versions/{version_number}/discovered-tools",
    response_model=list[McpServerToolRead],
)
def list_mcp_server_discovered_tools(
    mcp_server_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[McpServerToolRead]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(session, actor, server, action="mcp_server.discovery.read")
    version = _get_version(session, mcp_server_id, version_number)
    tools = session.execute(
        select(McpServerTool)
        .where(McpServerTool.mcp_server_version_id == version.id)
        .order_by(McpServerTool.tool_name)
    ).scalars()
    return [_tool_to_read(tool) for tool in tools]


@router.patch(
    "/{mcp_server_id}/versions/{version_number}/discovered-tools/{tool_name}",
    response_model=McpServerToolRead,
)
def update_mcp_server_discovered_tool(
    mcp_server_id: uuid.UUID,
    version_number: int,
    tool_name: str,
    payload: McpServerToolUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> McpServerToolRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    _require_server_access(
        session, actor, server, action="mcp_server.tool.update", mutate=True
    )
    version = _get_version(session, mcp_server_id, version_number)
    tool = session.execute(
        select(McpServerTool).where(
            McpServerTool.mcp_server_version_id == version.id,
            McpServerTool.tool_name == tool_name,
        )
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="mcp server tool not found")
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(tool, key, value)
    session.flush()
    session.add(
        AuditEvent(
            project_id=server.project_id,
            event_type="mcp.tool.enablement_updated",
            payload={
                "actor_id": str(actor.id),
                "mcp_server_id": str(server.id),
                "mcp_server_version_id": str(version.id),
                "tool_name": tool_name,
                "updates": updates,
            },
        )
    )
    session.flush()
    session.refresh(tool)
    return _tool_to_read(tool)
