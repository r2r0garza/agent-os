from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.authorization import (
    can_access_project,
    current_actor,
    has_team_access,
    primary_team_id,
    require_project_access,
    require_team_access,
)
from agentic_os.api.deps import get_session
from agentic_os.api.redaction import redact_mapping
from agentic_os.api.secrets import encrypt_secret
from agentic_os.domain.models import Credential, McpServer, McpServerVersion, Project, User

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


class McpServerCreate(BaseModel):
    name: str
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None


class McpServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID | None
    project_id: uuid.UUID | None
    name: str
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


def _version_to_read(version: McpServerVersion) -> McpServerVersionRead:
    return McpServerVersionRead(
        id=version.id,
        mcp_server_id=version.mcp_server_id,
        version_number=version.version_number,
        connection_config=redact_mapping(version.connection_config),
        credential_configured=version.credential_ciphertext is not None or version.credential_id is not None,
        credential_id=version.credential_id,
        created_at=version.created_at,
    )


def _require_server_access(session: Session, actor: User, server: McpServer, *, action: str) -> None:
    if server.project_id is not None:
        require_project_access(session, actor, server.project_id, action=action)
    elif server.team_id is not None:
        require_team_access(
            session, actor, server.team_id, action=action, resource_type="mcp server"
        )
    else:
        raise HTTPException(status_code=404, detail="mcp server not found")


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
    if team_id is not None:
        require_team_access(
            session, actor, team_id, action="mcp_server.create", resource_type="team"
        )
    if project_id is not None:
        require_project_access(session, actor, project_id, action="mcp_server.create")
    if team_id is None and project_id is None:
        team_id = primary_team_id(session, actor)
    server = McpServer(team_id=team_id, project_id=project_id, created_by=actor.id, name=payload.name)
    session.add(server)
    session.flush()
    session.refresh(server)
    return server


@router.get("", response_model=list[McpServerRead])
def list_mcp_servers(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[McpServer]:
    servers = session.execute(select(McpServer).order_by(McpServer.created_at)).scalars()
    if actor.role == "admin":
        return list(servers)
    result = []
    for server in servers:
        if server.team_id is not None and has_team_access(session, actor, server.team_id):
            result.append(server)
        elif server.project_id is not None:
            project = session.get(Project, server.project_id)
            if project is not None and can_access_project(session, actor, project):
                result.append(server)
    return result


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
    _require_server_access(session, actor, server, action="mcp_server.version.create")
    if payload.credential is not None and payload.credential_id is not None:
        raise HTTPException(status_code=422, detail="provide credential or credential_id, not both")
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
    version = McpServerVersion(
        mcp_server_id=mcp_server_id,
        version_number=next_version,
        connection_config=payload.connection_config,
        credential_ciphertext=encrypt_secret(payload.credential.get_secret_value()) if payload.credential else None,
        credential_id=payload.credential_id,
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


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
    return [_version_to_read(version) for version in versions]


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
    return _version_to_read(version)


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
