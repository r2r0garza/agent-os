from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.artifacts.service import create_artifact_version
from agentic_os.artifacts.storage import ArtifactStorage, ArtifactStorageError
from agentic_os.domain.models import Artifact, ArtifactVersion, AuditEvent

NORMALIZATION_VERSION = "text-v1"
SUPPORTED_CONTENT_TYPES = {
    "text/plain": "text/plain",
    "text/markdown": "text/markdown",
    "text/x-markdown": "text/markdown",
}
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$")


class ArtifactNormalizationError(ValueError):
    """Raised when supported source bytes cannot be normalized safely."""


@dataclass(frozen=True)
class NormalizedContent:
    content: bytes
    content_type: str
    metadata: dict


Normalizer = Callable[[bytes, str, str], NormalizedContent]


def normalize_text_content(content: bytes, content_type: str, source_hash: str) -> NormalizedContent:
    canonical_type = SUPPORTED_CONTENT_TYPES.get(_base_content_type(content_type))
    if canonical_type is None:
        raise ArtifactNormalizationError(f"unsupported content type: {content_type}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ArtifactNormalizationError("source content is not valid UTF-8") from error

    lines = text.splitlines(keepends=True)
    if text and not lines:
        lines = [text]
    headings: list[dict] = []
    byte_offset = 0
    for line_number, line in enumerate(lines, start=1):
        encoded_line = line.encode("utf-8")
        if canonical_type == "text/markdown":
            match = _MARKDOWN_HEADING.match(line)
            if match:
                headings.append(
                    {
                        "level": len(match.group(1)),
                        "title": match.group(2).strip(),
                        "line": line_number,
                        "source_byte_span": [byte_offset, byte_offset + len(encoded_line)],
                    }
                )
        byte_offset += len(encoded_line)

    line_count = len(lines)
    metadata = {
        "normalization_version": NORMALIZATION_VERSION,
        "source_content_type": _base_content_type(content_type),
        "normalized_content_type": canonical_type,
        "source_hash": source_hash,
        "document": {
            "source_byte_span": [0, len(content)],
            "source_line_span": [1, line_count] if line_count else [0, 0],
            "line_count": line_count,
        },
        "headings": headings,
    }
    # Version 1 preserves valid UTF-8 bytes exactly so every source span is a
    # stable citation anchor into the immutable upload.
    return NormalizedContent(content=content, content_type=canonical_type, metadata=metadata)


def ingest_source_artifact(
    session: Session,
    storage: ArtifactStorage,
    source: Artifact,
    *,
    normalizer: Normalizer = normalize_text_content,
) -> Artifact | None:
    if source.kind != "source":
        raise ValueError("only source artifacts can be ingested")

    source_type = _base_content_type(source.content_type)
    if source_type not in SUPPORTED_CONTENT_TYPES:
        _set_ingestion_state(
            session,
            source,
            "unsupported",
            metadata={
                "normalization_version": NORMALIZATION_VERSION,
                "source_content_type": source_type or None,
                "reason": "unsupported content type",
            },
        )
        return None

    source_version = _latest_version(session, source.id)
    if source_version is None or source_version.storage_state != "finalized":
        _set_ingestion_state(
            session,
            source,
            "needs_reconciliation",
            error="source artifact content is not finalized",
        )
        return None

    try:
        source_content = storage.read(source_version.storage_ref)
    except ArtifactStorageError as error:
        _set_ingestion_state(session, source, "needs_reconciliation", error=str(error))
        return None

    try:
        normalized = normalizer(source_content, source.content_type or "", source_version.content_hash)
    except Exception as error:
        _set_ingestion_state(session, source, "failed", error=str(error))
        return None

    normalized_artifact = Artifact(
        project_id=source.project_id,
        goal_id=source.goal_id,
        task_id=source.task_id,
        run_id=source.run_id,
        created_by=source.created_by,
        parent_artifact_id=source.id,
        name=_normalized_name(source.name, normalized.content_type),
        kind="normalized",
        content_type=normalized.content_type,
        ingestion_status="complete",
        ingestion_metadata=normalized.metadata,
    )
    session.add(normalized_artifact)
    session.flush()
    create_artifact_version(
        session,
        storage,
        normalized_artifact,
        normalized.content,
        version_number=1,
    )
    _set_ingestion_state(session, source, "complete", metadata=normalized.metadata)
    session.add(
        AuditEvent(
            project_id=source.project_id,
            goal_id=source.goal_id,
            task_id=source.task_id,
            run_id=source.run_id,
            event_type="artifact.ingestion_completed",
            payload={
                "source_artifact_id": str(source.id),
                "normalized_artifact_id": str(normalized_artifact.id),
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
    )
    session.flush()
    return normalized_artifact


def _latest_version(session: Session, artifact_id) -> ArtifactVersion | None:
    return session.execute(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def _set_ingestion_state(
    session: Session,
    artifact: Artifact,
    status: str,
    *,
    metadata: dict | None = None,
    error: str | None = None,
) -> None:
    previous_status = artifact.ingestion_status
    artifact.ingestion_status = status
    artifact.ingestion_metadata = metadata or {}
    artifact.ingestion_error = error
    session.add(
        AuditEvent(
            project_id=artifact.project_id,
            goal_id=artifact.goal_id,
            task_id=artifact.task_id,
            run_id=artifact.run_id,
            event_type="artifact.ingestion_status_changed",
            payload={
                "artifact_id": str(artifact.id),
                "previous_status": previous_status,
                "new_status": status,
                "error": error,
            },
        )
    )
    session.flush()


def _base_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _normalized_name(source_name: str, content_type: str) -> str:
    source_path = Path(source_name)
    suffix = ".md" if content_type == "text/markdown" else ".txt"
    return f"{source_path.stem}.normalized{suffix}"
