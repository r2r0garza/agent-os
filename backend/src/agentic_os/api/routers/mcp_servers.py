from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_team, ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.api.secrets import encrypt_secret
from agentic_os.domain.models import McpServer, McpServerVersion, Project

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
    connection_config: dict = {}
    credential: str | None = None


class McpServerVersionRead(BaseModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    version_number: int
    connection_config: dict
    credential_configured: bool
    created_at: datetime


def _version_to_read(version: McpServerVersion) -> McpServerVersionRead:
    return McpServerVersionRead(
        id=version.id,
        mcp_server_id=version.mcp_server_id,
        version_number=version.version_number,
        connection_config=version.connection_config,
        credential_configured=version.credential_ciphertext is not None,
        created_at=version.created_at,
    )


@router.post("", response_model=McpServerRead, status_code=201)
def create_mcp_server(payload: McpServerCreate, session: Session = Depends(get_session)) -> McpServer:
    user = ensure_default_user(session)
    team_id = payload.team_id
    project_id = payload.project_id
    if project_id is not None and session.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail="project not found")
    if team_id is None and project_id is None:
        team_id = ensure_default_team(session).id
    server = McpServer(team_id=team_id, project_id=project_id, created_by=user.id, name=payload.name)
    session.add(server)
    session.flush()
    session.refresh(server)
    return server


@router.get("", response_model=list[McpServerRead])
def list_mcp_servers(session: Session = Depends(get_session)) -> list[McpServer]:
    return list(session.execute(select(McpServer).order_by(McpServer.created_at)).scalars())


@router.get("/{mcp_server_id}", response_model=McpServerRead)
def get_mcp_server(mcp_server_id: uuid.UUID, session: Session = Depends(get_session)) -> McpServer:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.post("/{mcp_server_id}/versions", response_model=McpServerVersionRead, status_code=201)
def create_mcp_server_version(
    mcp_server_id: uuid.UUID, payload: McpServerVersionCreate, session: Session = Depends(get_session)
) -> McpServerVersionRead:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
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
        credential_ciphertext=encrypt_secret(payload.credential) if payload.credential else None,
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


@router.get("/{mcp_server_id}/versions", response_model=list[McpServerVersionRead])
def list_mcp_server_versions(
    mcp_server_id: uuid.UUID, session: Session = Depends(get_session)
) -> list[McpServerVersionRead]:
    server = session.get(McpServer, mcp_server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    versions = session.execute(
        select(McpServerVersion)
        .where(McpServerVersion.mcp_server_id == mcp_server_id)
        .order_by(McpServerVersion.version_number)
    ).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{mcp_server_id}/versions/{version_number}", response_model=McpServerVersionRead)
def get_mcp_server_version(
    mcp_server_id: uuid.UUID, version_number: int, session: Session = Depends(get_session)
) -> McpServerVersionRead:
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
    mcp_server_id: uuid.UUID, version_number: int, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    version = session.execute(
        select(McpServerVersion).where(
            McpServerVersion.mcp_server_id == mcp_server_id,
            McpServerVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="mcp server version not found")
    return version.connection_config.get("tools", [])
