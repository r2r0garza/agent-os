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

from agentic_os.artifacts import LocalArtifactStorage, create_artifact_version, ingest_source_artifact
from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import Artifact, ArtifactVersion, Project, Team, User

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


class ArtifactIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_database_engine(TEST_DATABASE_URL)
        self.Session = session_factory(self.engine)
        self.directory = tempfile.TemporaryDirectory()
        self.storage = LocalArtifactStorage(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()
        self.engine.dispose()

    def _source(self, session, content: bytes, content_type: str) -> Artifact:
        team = Team(name=f"Ingestion Team {uuid.uuid4()}")
        user = User(email=f"ingestion-{uuid.uuid4()}@example.test", display_name="Operator")
        session.add_all([team, user])
        session.flush()
        project = Project(team_id=team.id, created_by=user.id, name="Knowledge Project")
        session.add(project)
        session.flush()
        source = Artifact(
            project_id=project.id,
            created_by=user.id,
            name="knowledge.md",
            kind="source",
            content_type=content_type,
            ingestion_status="pending",
        )
        session.add(source)
        session.flush()
        create_artifact_version(session, self.storage, source, content, version_number=1)
        return source

    def test_markdown_normalization_is_deterministic_with_heading_spans_and_lineage(self) -> None:
        content = "# Café\n\n## Details\nBody\n".encode()
        with self.Session() as session:
            source = self._source(session, content, "text/markdown; charset=utf-8")
            normalized = ingest_source_artifact(session, self.storage, source)
            session.commit()

            self.assertIsNotNone(normalized)
            self.assertEqual(source.ingestion_status, "complete")
            self.assertEqual(normalized.parent_artifact_id, source.id)
            self.assertEqual(normalized.content_type, "text/markdown")
            self.assertEqual(normalized.ingestion_metadata["source_hash"], source.ingestion_metadata["source_hash"])
            self.assertEqual(
                normalized.ingestion_metadata["headings"],
                [
                    {"level": 1, "title": "Café", "line": 1, "source_byte_span": [0, 8]},
                    {"level": 2, "title": "Details", "line": 3, "source_byte_span": [9, 20]},
                ],
            )
            version = session.execute(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id == normalized.id)
            ).scalar_one()
            self.assertEqual(self.storage.read(version.storage_ref), content)

    def test_plain_text_normalization_preserves_bytes_and_document_spans(self) -> None:
        content = b"first\r\nsecond"
        with self.Session() as session:
            source = self._source(session, content, "text/plain")
            normalized = ingest_source_artifact(session, self.storage, source)
            session.commit()
            self.assertEqual(normalized.ingestion_metadata["document"]["source_byte_span"], [0, 13])
            self.assertEqual(normalized.ingestion_metadata["document"]["source_line_span"], [1, 2])
            self.assertEqual(normalized.ingestion_metadata["headings"], [])

    def test_invalid_utf8_marks_ingestion_failed_without_losing_source(self) -> None:
        content = b"not utf-8: \xff"
        with self.Session() as session:
            source = self._source(session, content, "text/plain")
            normalized = ingest_source_artifact(session, self.storage, source)
            session.commit()
            self.assertIsNone(normalized)
            self.assertEqual(source.ingestion_status, "failed")
            self.assertIn("valid UTF-8", source.ingestion_error)
            version = session.execute(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id == source.id)
            ).scalar_one()
            self.assertEqual(self.storage.read(version.storage_ref), content)

    def test_missing_source_content_needs_reconciliation(self) -> None:
        with self.Session() as session:
            source = self._source(session, b"temporarily missing", "text/plain")
            version = session.execute(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id == source.id)
            ).scalar_one()
            self.storage.path_for_ref(version.storage_ref).unlink()
            normalized = ingest_source_artifact(session, self.storage, source)
            session.commit()
            self.assertIsNone(normalized)
            self.assertEqual(source.ingestion_status, "needs_reconciliation")


if __name__ == "__main__":
    unittest.main()
