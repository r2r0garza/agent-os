from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.artifacts import (
    ArtifactContentUnavailableError,
    ContentVerificationError,
    LocalArtifactStorage,
    create_artifact_version,
    reconcile_artifact_storage,
    verify_artifact_version,
)
from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Artifact, ArtifactBlob, ArtifactVersion, AuditEvent, Project, Team, User

BACKEND_ROOT = Path(__file__).parents[1]


def _apply_migrations_from_zero(db_url: str) -> None:
    env = dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url)
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def setUpModule() -> None:
    global TEST_DATABASE_URL
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}: {error}")
    _apply_migrations_from_zero(TEST_DATABASE_URL)


class LocalArtifactStorageTests(unittest.TestCase):
    def test_staging_and_finalization_are_verified_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(directory)
            content = b"durable artifact bytes"

            staged = storage.stage(content)
            retried_stage = storage.stage(
                content, expected_hash=staged.content_hash, expected_size=staged.size_bytes
            )
            self.assertEqual(retried_stage, staged)

            storage_ref = storage.finalize(staged)
            self.assertTrue(storage.finalized_available(staged.content_hash, staged.size_bytes))
            self.assertEqual(storage.path_for_ref(storage_ref).read_bytes(), content)

            finalized_retry = storage.stage(content)
            self.assertTrue(finalized_retry.is_finalized)
            self.assertEqual(storage.finalize(finalized_retry), storage_ref)

    def test_hash_and_size_mismatches_never_stage_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalArtifactStorage(directory)
            with self.assertRaises(ContentVerificationError):
                storage.stage(b"actual", expected_hash="sha256:" + "0" * 64)
            with self.assertRaises(ContentVerificationError):
                storage.stage(b"actual", expected_size=100)
            self.assertEqual(storage.iter_staged(), ())


class ArtifactPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = LocalArtifactStorage(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()
        self.engine.dispose()

    def _artifact(self, session) -> Artifact:
        team = Team(name=f"Artifact Team {uuid.uuid4()}")
        session.add(team)
        session.flush()
        user = User(email=f"artifact-{uuid.uuid4()}@example.test", display_name="Artifact Operator")
        session.add(user)
        session.flush()
        project = Project(team_id=team.id, created_by=user.id, name="Artifact Project")
        session.add(project)
        session.flush()
        artifact = Artifact(project_id=project.id, created_by=user.id, name="result.txt")
        session.add(artifact)
        session.flush()
        return artifact

    def test_finalized_blob_metadata_and_artifact_version_commit_together(self) -> None:
        with self.Session() as session:
            artifact = self._artifact(session)
            version = create_artifact_version(
                session, self.storage, artifact, b"committed content", version_number=1
            )
            session.commit()
            version_id = version.id

        with self.Session() as session:
            version = session.get(ArtifactVersion, version_id)
            blob = session.get(ArtifactBlob, version.blob_id)
            self.assertEqual(version.storage_state, "finalized")
            self.assertEqual(version.size_bytes, len(b"committed content"))
            self.assertEqual(blob.state, "finalized")
            self.assertEqual(blob.content_hash, version.content_hash)
            verify_artifact_version(self.storage, version)

            self.storage.path_for_ref(version.storage_ref).unlink()
            with self.assertRaises(ArtifactContentUnavailableError):
                verify_artifact_version(self.storage, version)
            result = reconcile_artifact_storage(session, self.storage)
            session.commit()
            self.assertEqual(result.missing, 1)

        with self.Session() as session:
            version = session.get(ArtifactVersion, version_id)
            blob = session.get(ArtifactBlob, version.blob_id)
            self.assertEqual(version.storage_state, "missing")
            self.assertEqual(blob.state, "missing")

    def test_reconciliation_records_audit_event_on_storage_state_change(self) -> None:
        with self.Session() as session:
            artifact = self._artifact(session)
            version = create_artifact_version(
                session, self.storage, artifact, b"reconciled content", version_number=1
            )
            session.commit()
            version_id = version.id
            artifact_id = artifact.id
            project_id = artifact.project_id

        with self.Session() as session:
            version = session.get(ArtifactVersion, version_id)
            self.storage.path_for_ref(version.storage_ref).unlink()
            reconcile_artifact_storage(session, self.storage)
            session.commit()

        with self.Session() as session:
            events = list(
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "artifact.reconciliation_status_changed",
                        AuditEvent.project_id == project_id,
                    )
                ).scalars()
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].project_id, project_id)
            self.assertEqual(events[0].payload["artifact_id"], str(artifact_id))
            self.assertEqual(events[0].payload["previous_state"], "finalized")
            self.assertEqual(events[0].payload["new_state"], "missing")

    def test_reconciliation_marks_and_cleans_orphaned_staging(self) -> None:
        staged = self.storage.stage(b"abandoned content")
        untracked = self.storage.stage(b"untracked abandoned content")
        with self.Session() as session:
            blob = ArtifactBlob(
                content_hash=staged.content_hash,
                size_bytes=staged.size_bytes,
                state="staged",
            )
            session.add(blob)
            session.commit()
            blob_id = blob.id

        with self.Session() as session:
            result = reconcile_artifact_storage(session, self.storage, staged_grace_seconds=0)
            session.commit()
            self.assertEqual(result.orphaned, 1)
            self.assertEqual(result.cleaned_untracked_staged, 1)

        with self.Session() as session:
            blob = session.get(ArtifactBlob, blob_id)
            self.assertEqual(blob.state, "orphaned")
        self.assertFalse(self.storage.staged_available(staged.content_hash, staged.size_bytes))
        self.assertFalse(self.storage.staged_available(untracked.content_hash, untracked.size_bytes))

    def test_reconciliation_restores_metadata_when_finalized_content_returns(self) -> None:
        with self.Session() as session:
            artifact = self._artifact(session)
            version = create_artifact_version(
                session, self.storage, artifact, b"temporarily unavailable", version_number=1
            )
            session.commit()
            version_id = version.id
            storage_ref = version.storage_ref
            content = self.storage.path_for_ref(storage_ref).read_bytes()
            self.storage.path_for_ref(storage_ref).unlink()

        with self.Session() as session:
            reconcile_artifact_storage(session, self.storage)
            session.commit()
            version = session.get(ArtifactVersion, version_id)
            self.assertEqual(version.storage_state, "missing")

        self.storage.finalize(self.storage.stage(content))
        with self.Session() as session:
            result = reconcile_artifact_storage(session, self.storage)
            session.commit()
            version = session.get(ArtifactVersion, version_id)
            blob = session.get(ArtifactBlob, version.blob_id)
            self.assertEqual(result.restored, 1)
            self.assertEqual(version.storage_state, "finalized")
            self.assertEqual(blob.state, "finalized")

    def test_retry_reuses_one_content_blob(self) -> None:
        with self.Session() as session:
            artifact = self._artifact(session)
            first = create_artifact_version(session, self.storage, artifact, b"same bytes", version_number=1)
            second = create_artifact_version(session, self.storage, artifact, b"same bytes", version_number=2)
            session.commit()
            self.assertEqual(first.blob_id, second.blob_id)
            count = len(
                list(
                    session.execute(
                        select(ArtifactBlob).where(ArtifactBlob.content_hash == first.content_hash)
                    ).scalars()
                )
            )
            self.assertEqual(count, 1)

    def test_reconciliation_cleans_finalized_content_left_by_database_rollback(self) -> None:
        with self.Session() as session:
            artifact = self._artifact(session)
            version = create_artifact_version(
                session, self.storage, artifact, b"rolled back content", version_number=1
            )
            content_hash = version.content_hash
            size_bytes = version.size_bytes
            session.rollback()

        self.assertTrue(self.storage.finalized_available(content_hash, size_bytes))
        with self.Session() as session:
            result = reconcile_artifact_storage(session, self.storage, staged_grace_seconds=0)
            session.commit()
            self.assertEqual(result.cleaned_untracked_finalized, 1)
        self.assertFalse(self.storage.finalized_available(content_hash, size_bytes))
