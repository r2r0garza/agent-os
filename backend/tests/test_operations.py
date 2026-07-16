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

from agentic_os.operations import OperationError, create_backup, restore_backup, verify_backup


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


if __name__ == "__main__":
    unittest.main()
