from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentic_os.artifacts.storage import ArtifactStorage, StagedContent
from agentic_os.domain.models import Artifact, ArtifactBlob, ArtifactVersion


class ArtifactContentUnavailableError(RuntimeError):
    """Raised when product state would reference unavailable artifact bytes."""


@dataclass(frozen=True)
class ReconciliationResult:
    restored: int = 0
    missing: int = 0
    orphaned: int = 0
    cleaned_untracked_staged: int = 0
    cleaned_untracked_finalized: int = 0


def create_artifact_version(
    session: Session,
    storage: ArtifactStorage,
    artifact: Artifact,
    content: bytes,
    *,
    version_number: int,
    expected_hash: str | None = None,
    expected_size: int | None = None,
) -> ArtifactVersion:
    staged = storage.stage(content, expected_hash=expected_hash, expected_size=expected_size)
    blob = _upsert_blob(session, staged)
    storage_ref = staged.storage_ref or storage.finalize(staged)
    if not storage.finalized_available(staged.content_hash, staged.size_bytes):
        blob.state = "missing"
        blob.storage_ref = storage_ref
        blob.last_verified_at = datetime.now(timezone.utc)
        blob.reconciliation_details = {"reason": "content disappeared during finalization"}
        session.flush()
        raise ArtifactContentUnavailableError(
            f"finalized artifact content is unavailable: {staged.content_hash}"
        )
    blob.storage_ref = storage_ref
    blob.state = "finalized"
    blob.finalized_at = blob.finalized_at or datetime.now(timezone.utc)
    blob.last_verified_at = datetime.now(timezone.utc)
    blob.reconciliation_details = {}
    session.flush()

    version = ArtifactVersion(
        artifact_id=artifact.id,
        version_number=version_number,
        blob_id=blob.id,
        content_hash=blob.content_hash,
        size_bytes=blob.size_bytes,
        storage_ref=storage_ref,
        storage_state="finalized",
    )
    session.add(version)
    session.flush()
    verify_artifact_version(storage, version)
    return version


def verify_artifact_version(storage: ArtifactStorage, version: ArtifactVersion) -> None:
    if version.storage_state != "finalized" or not storage.finalized_available(
        version.content_hash, version.size_bytes
    ):
        raise ArtifactContentUnavailableError(
            f"artifact version {version.id} does not have verified finalized content"
        )


def reconcile_artifact_storage(
    session: Session,
    storage: ArtifactStorage,
    *,
    staged_grace_seconds: float = 3600,
) -> ReconciliationResult:
    now = datetime.now(timezone.utc)
    cutoff_timestamp = time.time() - staged_grace_seconds
    restored = missing = orphaned = cleaned_staged = cleaned_finalized = 0
    blobs = list(session.execute(select(ArtifactBlob)).scalars())
    known_hashes = {blob.content_hash for blob in blobs}
    staged_entries = {content_hash: modified_at for content_hash, _size, modified_at in storage.iter_staged()}

    for blob in blobs:
        finalized_available = storage.finalized_available(blob.content_hash, blob.size_bytes)
        if finalized_available:
            if blob.state != "finalized":
                restored += 1
            blob.state = "finalized"
            blob.storage_ref = f"local://sha256/{blob.content_hash.removeprefix('sha256:')}"
            blob.finalized_at = blob.finalized_at or now
            blob.last_verified_at = now
            blob.reconciliation_details = {}
            for version in session.execute(
                select(ArtifactVersion).where(ArtifactVersion.blob_id == blob.id)
            ).scalars():
                version.storage_state = "finalized"
            continue

        if blob.state == "staged" and storage.staged_available(blob.content_hash, blob.size_bytes):
            if staged_entries.get(blob.content_hash, time.time()) <= cutoff_timestamp:
                storage.delete_staged(blob.content_hash)
                blob.state = "orphaned"
                blob.reconciliation_details = {"reason": "staged content exceeded reconciliation grace period"}
                orphaned += 1
            continue

        if blob.state != "missing":
            missing += 1
        blob.state = "missing"
        blob.last_verified_at = now
        blob.reconciliation_details = {"reason": "finalized content is unavailable"}
        for version in session.execute(
            select(ArtifactVersion).where(ArtifactVersion.blob_id == blob.id)
        ).scalars():
            version.storage_state = "missing"

    for content_hash, modified_at in staged_entries.items():
        if content_hash not in known_hashes and modified_at <= cutoff_timestamp:
            storage.delete_staged(content_hash)
            cleaned_staged += 1

    for content_hash, _size_bytes, modified_at in storage.iter_finalized():
        if content_hash not in known_hashes and modified_at <= cutoff_timestamp:
            storage.delete_finalized(content_hash)
            cleaned_finalized += 1

    session.flush()
    return ReconciliationResult(restored, missing, orphaned, cleaned_staged, cleaned_finalized)


def _upsert_blob(session: Session, staged: StagedContent) -> ArtifactBlob:
    blob = session.execute(
        select(ArtifactBlob).where(ArtifactBlob.content_hash == staged.content_hash)
    ).scalar_one_or_none()
    if blob is None:
        blob = ArtifactBlob(
            content_hash=staged.content_hash,
            size_bytes=staged.size_bytes,
            storage_ref=staged.storage_ref,
            state="finalized" if staged.is_finalized else "staged",
            finalized_at=datetime.now(timezone.utc) if staged.is_finalized else None,
        )
        session.add(blob)
        session.flush()
    elif blob.size_bytes != staged.size_bytes:
        raise ValueError(f"content hash {staged.content_hash} already has a different recorded size")
    else:
        blob.state = "finalized" if staged.is_finalized else "staged"
        blob.storage_ref = staged.storage_ref
        blob.reconciliation_details = {}
    return blob
