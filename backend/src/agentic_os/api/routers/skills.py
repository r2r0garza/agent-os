from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
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
from agentic_os.api.redaction import redact_mapping
from agentic_os.domain.models import AgentVersionSkill, Skill, SkillInstallation, SkillVersion, User
from agentic_os.skill_packages import (
    redact_skill_package_export,
    validate_and_normalize_skill_package,
)

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    visibility: str = "private"


class SkillUpdate(BaseModel):
    name: str | None = None
    visibility: str | None = None


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    visibility: str
    created_at: datetime
    updated_at: datetime


class SkillInstallCreate(BaseModel):
    name: str | None = None


class SkillInstallationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    installed_skill_id: uuid.UUID
    source_skill_version_id: uuid.UUID
    installed_by: uuid.UUID
    created_at: datetime


class SkillPackageResource(BaseModel):
    path: str
    content: str
    media_type: str | None = None
    metadata: dict = Field(default_factory=dict)


class SkillVersionCreate(BaseModel):
    content_ref: str | None = None
    resource_metadata: dict = Field(default_factory=dict)
    manifest: dict | None = None
    instructions: str | None = None
    resources: list[SkillPackageResource] | None = None
    declared_capabilities: list[str] | None = None
    provenance: dict = Field(default_factory=dict)


class SkillVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    version_number: int
    content_ref: str
    resource_metadata: dict
    manifest: dict
    instructions: str | None
    resources: list[dict]
    declared_capabilities: list[str]
    provenance: dict
    package_hash: str | None
    validation_status: str
    validation_diagnostics: list[dict]
    created_at: datetime


class SkillPackageExport(BaseModel):
    format_version: int = 1
    name: str
    manifest: dict
    instructions: str | None
    resources: list[dict]
    declared_capabilities: list[str]
    provenance: dict
    package_hash: str | None
    validation_status: str
    validation_diagnostics: list[dict]


def _version_to_read(version: SkillVersion) -> SkillVersionRead:
    return SkillVersionRead(
        id=version.id,
        skill_id=version.skill_id,
        version_number=version.version_number,
        content_ref=version.content_ref,
        resource_metadata=redact_mapping(version.resource_metadata),
        manifest=redact_mapping(version.package_manifest),
        instructions=version.instructions,
        resources=redact_mapping(version.resources),
        declared_capabilities=version.declared_capabilities,
        provenance=redact_mapping(version.provenance),
        package_hash=version.package_hash,
        validation_status=version.validation_status,
        validation_diagnostics=version.validation_diagnostics,
        created_at=version.created_at,
    )


def _get_skill(session: Session, actor: User, skill_id: uuid.UUID) -> Skill:
    """Read access: home team membership, or `team`/`public` visibility, or admin."""

    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_shared_definition_access(session, actor, skill, action="skill.read", resource_type="skill")
    return skill


def _get_skill_for_mutation(session: Session, actor: User, skill_id: uuid.UUID) -> Skill:
    """Mutation access: home team membership only. Visibility never grants edit rights."""

    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    require_team_access(session, actor, skill.team_id, action="skill.write", resource_type="skill")
    return skill


def _skill_has_dependents(session: Session, skill_id: uuid.UUID) -> bool:
    version_ids = select(SkillVersion.id).where(SkillVersion.skill_id == skill_id)
    for stmt in (
        select(AgentVersionSkill.id).where(AgentVersionSkill.skill_version_id.in_(version_ids)),
        select(SkillInstallation.id).where(SkillInstallation.source_skill_version_id.in_(version_ids)),
    ):
        if session.execute(stmt.limit(1)).first() is not None:
            return True
    return False


@router.post("", response_model=SkillRead, status_code=201)
def create_skill(
    payload: SkillCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    skill = Skill(
        team_id=primary_team_id(session, actor),
        created_by=actor.id,
        name=payload.name,
        visibility=payload.visibility,
    )
    session.add(skill)
    session.flush()
    session.refresh(skill)
    return skill


@router.get("", response_model=list[SkillRead])
def list_skills(
    session: Session = Depends(get_session), actor: User = Depends(current_actor)
) -> list[Skill]:
    stmt = select(Skill)
    if actor.role != "admin":
        stmt = stmt.where(or_(Skill.team_id.in_(actor_team_ids(session, actor)), Skill.visibility == "public"))
    return list(session.execute(stmt.order_by(Skill.created_at)).scalars())


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    return _get_skill(session, actor, skill_id)


@router.patch("/{skill_id}", response_model=SkillRead)
def update_skill(
    skill_id: uuid.UUID,
    payload: SkillUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    skill = _get_skill_for_mutation(session, actor, skill_id)
    updates = payload.model_dump(exclude_unset=True)
    if "visibility" in updates and updates["visibility"] != skill.visibility:
        if actor.role != "admin" and skill.created_by != actor.id:
            raise HTTPException(status_code=403, detail="only the owner or an admin can change visibility")
    for key, value in updates.items():
        setattr(skill, key, value)
    session.flush()
    session.refresh(skill)
    return skill


@router.delete("/{skill_id}", status_code=204, response_model=None)
def delete_skill(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> None:
    skill = _get_skill_for_mutation(session, actor, skill_id)
    if _skill_has_dependents(session, skill_id):
        raise HTTPException(status_code=409, detail="skill has agent attachments or installations referencing its versions")
    session.delete(skill)
    session.flush()


@router.post("/{skill_id}/versions/{version_number}/install", response_model=SkillRead, status_code=201)
def install_skill_version(
    skill_id: uuid.UUID,
    version_number: int,
    payload: SkillInstallCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> Skill:
    """Pin a `team`/`public` source version into a new, independently governed skill.

    The installed skill is a fresh resource owned by the installer's team; it
    starts private and is decoupled from later edits to the source skill.
    """

    source_skill = _get_skill(session, actor, skill_id)
    source_version = session.execute(
        select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version_number == version_number)
    ).scalar_one_or_none()
    if source_version is None:
        raise HTTPException(status_code=404, detail="skill version not found")

    installed_skill = Skill(
        team_id=primary_team_id(session, actor),
        created_by=actor.id,
        name=payload.name or source_skill.name,
        visibility="private",
    )
    session.add(installed_skill)
    session.flush()
    installed_version = SkillVersion(
        skill_id=installed_skill.id,
        version_number=1,
        content_ref=source_version.content_ref,
        resource_metadata=source_version.resource_metadata,
        package_manifest=source_version.package_manifest,
        instructions=source_version.instructions,
        resources=source_version.resources,
        declared_capabilities=source_version.declared_capabilities,
        provenance=source_version.provenance,
        package_hash=source_version.package_hash,
        validation_status=source_version.validation_status,
        validation_diagnostics=source_version.validation_diagnostics,
    )
    session.add(installed_version)
    session.add(
        SkillInstallation(
            installed_skill_id=installed_skill.id,
            source_skill_version_id=source_version.id,
            installed_by=actor.id,
        )
    )
    session.flush()
    session.refresh(installed_skill)
    return installed_skill


@router.get("/{skill_id}/installation", response_model=SkillInstallationRead)
def get_skill_installation(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillInstallation:
    _get_skill(session, actor, skill_id)
    installation = session.execute(
        select(SkillInstallation).where(SkillInstallation.installed_skill_id == skill_id)
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status_code=404, detail="skill installation not found")
    return installation


@router.post("/{skill_id}/versions", response_model=SkillVersionRead, status_code=201)
def create_skill_version(
    skill_id: uuid.UUID,
    payload: SkillVersionCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillVersionRead:
    skill = _get_skill_for_mutation(session, actor, skill_id)
    package_requested = any(
        value is not None
        for value in (
            payload.manifest,
            payload.instructions,
            payload.resources,
            payload.declared_capabilities,
        )
    )
    package: dict[str, Any] | None = None
    if package_requested:
        package, diagnostics = validate_and_normalize_skill_package(
            manifest=payload.manifest,
            instructions=payload.instructions,
            resources=(
                [resource.model_dump() for resource in payload.resources]
                if payload.resources is not None
                else None
            ),
            declared_capabilities=payload.declared_capabilities,
            provenance=payload.provenance,
        )
        if diagnostics:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_skill_package",
                    "diagnostics": diagnostics,
                },
            )
        assert package is not None
    elif not payload.content_ref:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_skill_package",
                "diagnostics": [
                    {
                        "code": "package_or_content_ref_required",
                        "location": "body",
                        "message": "provide a package manifest or a legacy content_ref",
                    }
                ],
            },
        )
    next_version = (
        session.execute(
            select(func.coalesce(func.max(SkillVersion.version_number), 0)).where(
                SkillVersion.skill_id == skill_id
            )
        ).scalar_one()
        + 1
    )
    version = SkillVersion(
        skill_id=skill.id,
        version_number=next_version,
        content_ref=payload.content_ref or f"sha256:{package['package_hash']}",
        resource_metadata=payload.resource_metadata,
        package_manifest=package["manifest"] if package else {},
        instructions=package["instructions"] if package else None,
        resources=package["resources"] if package else [],
        declared_capabilities=package["declared_capabilities"] if package else [],
        provenance=package["provenance"] if package else {},
        package_hash=package["package_hash"] if package else None,
        validation_status="valid" if package else "legacy",
        validation_diagnostics=[],
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    return _version_to_read(version)


@router.get("/{skill_id}/versions", response_model=list[SkillVersionRead])
def list_skill_versions(
    skill_id: uuid.UUID,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> list[SkillVersionRead]:
    _get_skill(session, actor, skill_id)
    versions = session.execute(
        select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version_number)
    ).scalars()
    return [_version_to_read(version) for version in versions]


@router.get("/{skill_id}/versions/{version_number}", response_model=SkillVersionRead)
def get_skill_version(
    skill_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillVersionRead:
    _get_skill(session, actor, skill_id)
    version = session.execute(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill_id, SkillVersion.version_number == version_number
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="skill version not found")
    return _version_to_read(version)


@router.get(
    "/{skill_id}/versions/{version_number}/export",
    response_model=SkillPackageExport,
)
def export_skill_version(
    skill_id: uuid.UUID,
    version_number: int,
    session: Session = Depends(get_session),
    actor: User = Depends(current_actor),
) -> SkillPackageExport:
    skill = _get_skill(session, actor, skill_id)
    version = session.execute(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill_id,
            SkillVersion.version_number == version_number,
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="skill version not found")
    return SkillPackageExport(
        name=skill.name,
        manifest=redact_skill_package_export(version.package_manifest),
        instructions=version.instructions,
        resources=redact_skill_package_export(version.resources),
        declared_capabilities=version.declared_capabilities,
        provenance=redact_skill_package_export(version.provenance),
        package_hash=version.package_hash,
        validation_status=version.validation_status,
        validation_diagnostics=version.validation_diagnostics,
    )
