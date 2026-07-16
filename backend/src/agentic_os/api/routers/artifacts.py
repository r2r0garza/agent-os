from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.api.bootstrap import ensure_default_user
from agentic_os.api.deps import get_session
from agentic_os.artifacts import (
    ArtifactContentUnavailableError,
    artifact_storage,
    create_artifact_version,
    ingest_source_artifact,
)
from agentic_os.domain.models import Artifact, ArtifactVersion, AuditEvent, Goal, Project, Run, Task

router = APIRouter(tags=["artifacts"])

VALID_ARTIFACT_KINDS = {"source", "normalized", "output"}


class ArtifactVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    artifact_id: uuid.UUID
    version_number: int
    content_hash: str
    size_bytes: int
    storage_state: str
    previous_version_id: uuid.UUID | None
    created_at: datetime


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    goal_id: uuid.UUID | None
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_by: uuid.UUID | None
    parent_artifact_id: uuid.UUID | None
    name: str
    kind: str
    content_type: str | None
    ingestion_status: str
    ingestion_metadata: dict
    ingestion_error: str | None
    created_at: datetime
    latest_version: ArtifactVersionRead | None = None


class ArtifactLineageRead(BaseModel):
    artifact: ArtifactRead
    parent: ArtifactRead | None
    children: list[ArtifactRead]


class ArtifactUploadRequest(BaseModel):
    name: str
    content: str
    content_type: str | None = None
    goal_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


def _project_or_404(session: Session, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _resolve_scope(
    session: Session,
    project_id: uuid.UUID,
    goal_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
) -> None:
    """Validate that any referenced goal/task/run belongs to the given project."""
    resolved_goal_id = goal_id
    if task_id is not None:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        goal = session.get(Goal, task.goal_id)
        if goal is None or goal.project_id != project_id:
            raise HTTPException(status_code=404, detail="task does not belong to project")
        if resolved_goal_id is not None and resolved_goal_id != task.goal_id:
            raise HTTPException(status_code=422, detail="task does not belong to the given goal")
        resolved_goal_id = task.goal_id
    if run_id is not None:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        task = session.get(Task, run.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="run does not belong to project")
        goal = session.get(Goal, task.goal_id)
        if goal is None or goal.project_id != project_id:
            raise HTTPException(status_code=404, detail="run does not belong to project")
        if task_id is not None and task_id != run.task_id:
            raise HTTPException(status_code=422, detail="run does not belong to the given task")
    if resolved_goal_id is not None:
        goal = session.get(Goal, resolved_goal_id)
        if goal is None or goal.project_id != project_id:
            raise HTTPException(status_code=404, detail="goal does not belong to project")


def _latest_version(session: Session, artifact_id: uuid.UUID) -> ArtifactVersion | None:
    return session.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def _to_read(session: Session, artifact: Artifact) -> ArtifactRead:
    version = _latest_version(session, artifact.id)
    return ArtifactRead.model_validate(
        {
            **{column.name: getattr(artifact, column.name) for column in Artifact.__table__.columns},
            "latest_version": ArtifactVersionRead.model_validate(version) if version else None,
        }
    )


@router.post("/projects/{project_id}/artifacts", response_model=ArtifactRead, status_code=201)
def upload_artifact(
    project_id: uuid.UUID, payload: ArtifactUploadRequest, session: Session = Depends(get_session)
) -> ArtifactRead:
    _project_or_404(session, project_id)
    _resolve_scope(session, project_id, payload.goal_id, payload.task_id, payload.run_id)
    user = ensure_default_user(session)

    artifact = Artifact(
        project_id=project_id,
        goal_id=payload.goal_id,
        task_id=payload.task_id,
        run_id=payload.run_id,
        created_by=user.id,
        name=payload.name,
        kind="source",
        content_type=payload.content_type,
        ingestion_status="pending",
    )
    session.add(artifact)
    session.flush()

    storage = artifact_storage()
    create_artifact_version(session, storage, artifact, payload.content.encode(), version_number=1)
    ingest_source_artifact(session, storage, artifact)

    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=payload.goal_id,
            task_id=payload.task_id,
            run_id=payload.run_id,
            event_type="artifact.created",
            payload={"artifact_id": str(artifact.id), "kind": artifact.kind, "name": artifact.name},
        )
    )
    session.flush()
    session.refresh(artifact)
    return _to_read(session, artifact)


@router.get("/projects/{project_id}/artifacts", response_model=list[ArtifactRead])
def list_artifacts(
    project_id: uuid.UUID,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    kind: str | None = None,
    session: Session = Depends(get_session),
) -> list[ArtifactRead]:
    _project_or_404(session, project_id)
    stmt = select(Artifact).where(Artifact.project_id == project_id)
    if goal_id is not None:
        stmt = stmt.where(Artifact.goal_id == goal_id)
    if task_id is not None:
        stmt = stmt.where(Artifact.task_id == task_id)
    if run_id is not None:
        stmt = stmt.where(Artifact.run_id == run_id)
    if kind is not None:
        if kind not in VALID_ARTIFACT_KINDS:
            raise HTTPException(status_code=422, detail=f"invalid kind {kind!r}")
        stmt = stmt.where(Artifact.kind == kind)
    artifacts = session.execute(stmt.order_by(Artifact.created_at)).scalars()
    return [_to_read(session, artifact) for artifact in artifacts]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
def get_artifact(artifact_id: uuid.UUID, session: Session = Depends(get_session)) -> ArtifactRead:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return _to_read(session, artifact)


@router.get("/artifacts/{artifact_id}/normalized", response_model=ArtifactRead)
def get_normalized_artifact(
    artifact_id: uuid.UUID, session: Session = Depends(get_session)
) -> ArtifactRead:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    normalized = session.execute(
        select(Artifact)
        .where(Artifact.parent_artifact_id == artifact_id, Artifact.kind == "normalized")
        .order_by(Artifact.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if normalized is None:
        raise HTTPException(
            status_code=409,
            detail=f"normalized artifact is unavailable; ingestion status is {artifact.ingestion_status}",
        )
    return _to_read(session, normalized)


@router.get("/artifacts/{artifact_id}/versions", response_model=list[ArtifactVersionRead])
def list_artifact_versions(artifact_id: uuid.UUID, session: Session = Depends(get_session)) -> list[ArtifactVersion]:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return list(
        session.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version_number)
        ).scalars()
    )


@router.get("/artifacts/{artifact_id}/lineage", response_model=ArtifactLineageRead)
def get_artifact_lineage(artifact_id: uuid.UUID, session: Session = Depends(get_session)) -> ArtifactLineageRead:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    parent = None
    if artifact.parent_artifact_id is not None:
        parent_artifact = session.get(Artifact, artifact.parent_artifact_id)
        parent = _to_read(session, parent_artifact) if parent_artifact else None
    children = session.execute(
        select(Artifact).where(Artifact.parent_artifact_id == artifact_id).order_by(Artifact.created_at)
    ).scalars()
    return ArtifactLineageRead(
        artifact=_to_read(session, artifact),
        parent=parent,
        children=[_to_read(session, child) for child in children],
    )


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(
    artifact_id: uuid.UUID, version: int | None = None, session: Session = Depends(get_session)
) -> Response:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    if version is not None:
        artifact_version = session.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version_number == version
            )
        ).scalar_one_or_none()
    else:
        artifact_version = _latest_version(session, artifact_id)

    if artifact_version is None:
        raise HTTPException(status_code=404, detail="artifact version not found")

    storage = artifact_storage()
    if artifact_version.storage_state != "finalized" or not storage.finalized_available(
        artifact_version.content_hash, artifact_version.size_bytes
    ):
        session.add(
            AuditEvent(
                project_id=artifact.project_id,
                goal_id=artifact.goal_id,
                task_id=artifact.task_id,
                run_id=artifact.run_id,
                event_type="artifact.retrieval_blocked",
                payload={
                    "artifact_id": str(artifact.id),
                    "version_id": str(artifact_version.id),
                    "storage_state": artifact_version.storage_state,
                },
            )
        )
        session.commit()
        raise HTTPException(status_code=409, detail="artifact content is not finalized or accessible")

    try:
        content = storage.path_for_ref(artifact_version.storage_ref).read_bytes()
    except (OSError, ArtifactContentUnavailableError) as error:
        raise HTTPException(status_code=409, detail="artifact content is not accessible") from error

    return Response(content=content, media_type=artifact.content_type or "application/octet-stream")
