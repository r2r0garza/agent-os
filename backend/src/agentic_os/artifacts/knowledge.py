from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.artifacts.storage import ArtifactStorage, ArtifactStorageError
from agentic_os.domain.models import (
    Artifact,
    ArtifactCitation,
    ArtifactVersion,
    AuditEvent,
    Run,
    Task,
)


class KnowledgeUnavailableError(RuntimeError):
    """Raised when task-declared project knowledge cannot be safely provided to a run."""


@dataclass(frozen=True)
class ConsumedKnowledge:
    source_artifact: Artifact
    normalized_artifact: Artifact
    normalized_version: ArtifactVersion
    content: bytes
    citation_anchor: dict


def consume_task_knowledge(
    session: Session,
    storage: ArtifactStorage,
    task: Task,
    run: Run,
    *,
    project_id: uuid.UUID,
) -> list[ConsumedKnowledge]:
    """Resolve task-declared knowledge artifacts to bounded normalized content.

    Reads only through the artifact storage/service interfaces, never the
    host filesystem directly. Any artifact that is not resolvable to
    finalized, project-scoped normalized content raises
    ``KnowledgeUnavailableError`` after recording why, so a run never
    silently proceeds without the knowledge it declared it needed.
    """
    consumed: list[ConsumedKnowledge] = []
    for raw_id in task.knowledge_artifact_ids or []:
        source_id = uuid.UUID(str(raw_id))
        source = session.get(Artifact, source_id)
        if source is None or source.project_id != project_id or source.kind != "source":
            _emit_unavailable(
                session, task, run, project_id, source_id,
                reason="knowledge artifact not found in project",
            )
            raise KnowledgeUnavailableError(
                f"knowledge artifact {source_id} is not available to project {project_id}"
            )

        normalized = session.execute(
            select(Artifact)
            .where(Artifact.parent_artifact_id == source.id, Artifact.kind == "normalized")
            .order_by(Artifact.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if normalized is None or source.ingestion_status != "complete":
            _emit_unavailable(
                session, task, run, project_id, source.id,
                reason=f"normalized knowledge unavailable (ingestion status {source.ingestion_status})",
            )
            raise KnowledgeUnavailableError(
                f"normalized knowledge for artifact {source.id} is not available"
            )

        version = session.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == normalized.id)
            .order_by(ArtifactVersion.version_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            version is None
            or version.storage_state != "finalized"
            or not storage.finalized_available(version.content_hash, version.size_bytes)
        ):
            _emit_unavailable(
                session, task, run, project_id, source.id,
                reason="normalized artifact content is not finalized or accessible",
            )
            raise KnowledgeUnavailableError(
                f"normalized artifact {normalized.id} content is unavailable"
            )

        try:
            content = storage.read(version.storage_ref)
        except ArtifactStorageError as error:
            _emit_unavailable(session, task, run, project_id, source.id, reason=str(error))
            raise KnowledgeUnavailableError(str(error)) from error

        anchor = dict((normalized.ingestion_metadata or {}).get("document") or {})
        session.add(
            AuditEvent(
                project_id=project_id,
                goal_id=task.goal_id,
                task_id=task.id,
                run_id=run.id,
                event_type="artifact.knowledge_consumed",
                payload={
                    "source_artifact_id": str(source.id),
                    "normalized_artifact_id": str(normalized.id),
                    "normalized_version_id": str(version.id),
                    "content_hash": version.content_hash,
                },
            )
        )
        session.flush()
        consumed.append(
            ConsumedKnowledge(
                source_artifact=source,
                normalized_artifact=normalized,
                normalized_version=version,
                content=content,
                citation_anchor=anchor,
            )
        )
    return consumed


def record_output_citations(
    session: Session,
    task: Task,
    run: Run,
    output_artifact: Artifact,
    consumed: list[ConsumedKnowledge],
) -> list[ArtifactCitation]:
    """Link an immutable output artifact back to the knowledge it cites."""
    citations: list[ArtifactCitation] = []
    for item in consumed:
        citation = ArtifactCitation(
            run_id=run.id,
            task_id=task.id,
            output_artifact_id=output_artifact.id,
            source_artifact_id=item.source_artifact.id,
            normalized_artifact_id=item.normalized_artifact.id,
            normalized_version_id=item.normalized_version.id,
            citation_anchor=item.citation_anchor,
        )
        session.add(citation)
        citations.append(citation)
    if not citations:
        return citations
    session.flush()
    session.add(
        AuditEvent(
            project_id=output_artifact.project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="artifact.citations_recorded",
            payload={
                "output_artifact_id": str(output_artifact.id),
                "citation_count": len(citations),
                "source_artifact_ids": [str(citation.source_artifact_id) for citation in citations],
            },
        )
    )
    session.flush()
    return citations


def _emit_unavailable(
    session: Session,
    task: Task,
    run: Run,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    session.add(
        AuditEvent(
            project_id=project_id,
            goal_id=task.goal_id,
            task_id=task.id,
            run_id=run.id,
            event_type="artifact.knowledge_unavailable",
            payload={"source_artifact_id": str(source_id), "reason": reason},
        )
    )
    session.flush()
