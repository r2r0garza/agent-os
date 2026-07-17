from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import AuditEvent
from agentic_os.operations import (
    OperationError,
    _record_maintenance_event,
    apply_migrations,
    create_backup,
    migration_status,
    restore_backup,
    upgrade_preflight,
    verify_backup,
)


class LocalOperationsTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "AGENTIC_OS_DATABASE_URL": "postgresql+psycopg://operator:secret@db.test:5432/agentic_os",
            "AGENTIC_OS_ARTIFACT_ROOT": str(root / "artifacts"),
            "AGENTIC_OS_MASTER_KEY_FILE": str(root / "master.key"),
        }

    @staticmethod
    def _fake_postgres(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[0] == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"custom postgres dump")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def test_backup_and_restore_happy_path_preserves_artifacts_and_hides_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts" / "sha256" / "ab"
            artifacts.mkdir(parents=True)
            (artifacts / "blob").write_bytes(b"durable bytes")
            backup = root / "backup.tar.gz"
            restored = root / "restored-artifacts"
            with mock.patch.dict(os.environ, self._environment(root), clear=False), mock.patch(
                "agentic_os.operations.subprocess.run", side_effect=self._fake_postgres
            ) as run:
                result = create_backup(backup)
                restore = restore_backup(
                    backup,
                    target_database_url="postgresql+psycopg://restore:other@clean.test/restored",
                    target_artifact_root=restored,
                )

            self.assertEqual(result["artifact_files"], 1)
            self.assertEqual((restored / "sha256" / "ab" / "blob").read_bytes(), b"durable bytes")
            self.assertFalse(restore["master_key_restored"])
            self.assertNotIn("secret", json.dumps(result))
            for call in run.call_args_list:
                self.assertNotIn("secret", " ".join(call.args[0]))
            restore_command = next(call.args[0] for call in run.call_args_list if call.args[0][0] == "pg_restore")
            self.assertIn("restored", restore_command)
            self.assertNotIn("other", " ".join(restore_command))

    def test_restore_refuses_active_targets_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            backup = root / "backup.tar.gz"
            with mock.patch.dict(os.environ, self._environment(root), clear=False), mock.patch(
                "agentic_os.operations.subprocess.run", side_effect=self._fake_postgres
            ):
                create_backup(backup)
                with self.assertRaisesRegex(OperationError, "isolated"):
                    restore_backup(
                        backup,
                        target_database_url=os.environ["AGENTIC_OS_DATABASE_URL"],
                        target_artifact_root=root / "artifacts",
                    )

    def test_verify_detects_tampered_database_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            backup = root / "backup.tar.gz"
            with mock.patch.dict(os.environ, self._environment(root), clear=False), mock.patch(
                "agentic_os.operations.subprocess.run", side_effect=self._fake_postgres
            ):
                create_backup(backup)
            unpacked = root / "unpacked"
            with tarfile.open(backup, "r:gz") as archive:
                archive.extractall(unpacked, filter="data")
            (unpacked / "agentic-os-backup" / "database.dump").write_bytes(b"tampered")
            tampered = root / "tampered.tar.gz"
            with tarfile.open(tampered, "w:gz") as archive:
                archive.add(unpacked / "agentic-os-backup", arcname="agentic-os-backup")
            with self.assertRaisesRegex(OperationError, "integrity"):
                verify_backup(tampered, root / "verify")


class BackupRootDefaultTests(unittest.TestCase):
    """AGENTIC_OS_BACKUP_ROOT lets operators omit --output on the team VM."""

    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "AGENTIC_OS_DATABASE_URL": "postgresql+psycopg://operator:secret@db.test:5432/agentic_os",
            "AGENTIC_OS_ARTIFACT_ROOT": str(root / "artifacts"),
            "AGENTIC_OS_MASTER_KEY_FILE": str(root / "master.key"),
        }

    def test_backup_without_output_uses_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            backup_root = root / "backups"
            environment = self._environment(root)
            environment["AGENTIC_OS_BACKUP_ROOT"] = str(backup_root)
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
                "agentic_os.operations.subprocess.run", side_effect=LocalOperationsTests._fake_postgres
            ):
                result = create_backup()
            backup_path = Path(result["backup"])
            self.assertEqual(backup_path.parent, backup_root.resolve())
            self.assertTrue(backup_path.name.startswith("agentic-os-"))
            self.assertTrue(backup_path.is_file())

    def test_backup_without_output_or_backup_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            environment = self._environment(root)
            with mock.patch.dict(os.environ, environment, clear=False):
                os.environ.pop("AGENTIC_OS_BACKUP_ROOT", None)
                with self.assertRaisesRegex(OperationError, "AGENTIC_OS_BACKUP_ROOT"):
                    create_backup()

    def test_backup_without_output_rejects_remote_backup_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            environment = self._environment(root)
            environment["AGENTIC_OS_BACKUP_ROOT"] = "s3://bucket/backups"
            with mock.patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(OperationError, "local/mounted"):
                    create_backup()


class UpgradePreflightRollbackGuidanceTests(unittest.TestCase):
    """Rollback guidance names the TLS certificate/key only for team deployments."""

    def _run_preflight(self, mode: str) -> dict:
        with mock.patch("agentic_os.operations.setup_check", return_value="[OK] example: fine"), mock.patch(
            "agentic_os.operations.migration_status", return_value="database is at the migration head"
        ), mock.patch("agentic_os.operations._record_maintenance_event"), mock.patch.dict(
            os.environ, {"AGENTIC_OS_DEPLOYMENT_MODE": mode}, clear=False
        ):
            return upgrade_preflight()

    def test_team_mode_mentions_tls_certificate(self) -> None:
        result = self._run_preflight("team")
        self.assertEqual(result["deployment_mode"], "team")
        self.assertIn("TLS certificate", result["rollback"])

    def test_local_mode_omits_tls_certificate(self) -> None:
        result = self._run_preflight("local")
        self.assertEqual(result["deployment_mode"], "local")
        self.assertNotIn("TLS certificate", result["rollback"])


class MaintenanceEvidenceTests(unittest.TestCase):
    """Maintenance operations persist durable, queryable evidence.

    Requires a reachable PostgreSQL because evidence is written as an
    AuditEvent row through the real domain session, the same durable record
    every other observability view reads from.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
        try:
            probe = create_database_engine(cls.database_url)
            with probe.connect():
                pass
            probe.dispose()
        except Exception as error:  # pragma: no cover - environment guard
            raise unittest.SkipTest(
                f"PostgreSQL is not reachable at {cls.database_url!r}: {error}"
            )

    def _latest_event(self, event_type: str):
        engine = create_database_engine(self.database_url)
        try:
            with session_factory(engine)() as session:
                return session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == event_type)
                    .order_by(AuditEvent.sequence_number.desc())
                ).scalars().first()
        finally:
            engine.dispose()

    def test_maintenance_event_is_swallowed_when_database_is_unreachable(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"AGENTIC_OS_DATABASE_URL": "postgresql+psycopg://user:pass@127.0.0.1:1/agentic_os"},
            clear=False,
        ):
            _record_maintenance_event("operations.test_probe", {"ok": True})  # does not raise

    def test_migration_status_persists_queryable_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"AGENTIC_OS_DATABASE_URL": self.database_url}, clear=False):
            detail = migration_status()
        event = self._latest_event("operations.migration_status")
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["detail"], detail)

    def test_apply_migrations_persists_queryable_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"AGENTIC_OS_DATABASE_URL": self.database_url}, clear=False):
            detail = apply_migrations()
        event = self._latest_event("operations.migration_apply")
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["detail"], detail)

    def test_backup_and_restore_persist_queryable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "blob").write_bytes(b"durable bytes")
            backup = root / "backup.tar.gz"
            restored = root / "restored-artifacts"
            environment = {
                "AGENTIC_OS_DATABASE_URL": self.database_url,
                "AGENTIC_OS_ARTIFACT_ROOT": str(artifacts),
                "AGENTIC_OS_MASTER_KEY_FILE": str(root / "master.key"),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
                "agentic_os.operations.subprocess.run", side_effect=LocalOperationsTests._fake_postgres
            ):
                result = create_backup(backup)
                restore_backup(
                    backup,
                    target_database_url="postgresql+psycopg://restore:other@clean.test/restored",
                    target_artifact_root=restored,
                )

        backup_event = self._latest_event("operations.backup_created")
        self.assertIsNotNone(backup_event)
        self.assertEqual(backup_event.payload["backup"], result["backup"])

        restore_event = self._latest_event("operations.restore_completed")
        self.assertIsNotNone(restore_event)
        self.assertEqual(restore_event.payload["artifact_root"], str(restored.resolve()))


if __name__ == "__main__":
    unittest.main()
