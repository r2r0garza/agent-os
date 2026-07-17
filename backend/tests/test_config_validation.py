from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agentic_os.config import (
    ConfigurationError,
    CheckResult,
    format_report,
    generate_master_key,
    preflight_evidence,
    resolve_master_key,
    run_preflight,
    validate_artifact_root,
    validate_backup_destination,
    validate_database_url,
    validate_deployment_mode,
    validate_master_key,
    validate_postgres_tools,
    validate_public_origin,
)

BACKEND_ROOT = Path(__file__).parents[1]
_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


class MasterKeyResolutionTests(unittest.TestCase):
    def test_missing_key_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = resolve_master_key(env={"AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key")})
        self.assertIsNone(key)

    def test_valid_env_key_is_returned(self) -> None:
        valid_key = Fernet.generate_key()
        key = resolve_master_key(env={"AGENTIC_OS_MASTER_KEY": valid_key.decode()})
        self.assertEqual(key, valid_key)

    def test_malformed_env_key_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            resolve_master_key(env={"AGENTIC_OS_MASTER_KEY": "not-a-valid-fernet-key"})

    def test_valid_key_file_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            generate_master_key(key_path)
            key = resolve_master_key(env={"AGENTIC_OS_MASTER_KEY_FILE": str(key_path)})
        self.assertIsNotNone(key)
        Fernet(key)  # does not raise

    def test_malformed_key_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            key_path.write_bytes(b"garbage")
            key_path.chmod(0o600)
            with self.assertRaises(ConfigurationError):
                resolve_master_key(env={"AGENTIC_OS_MASTER_KEY_FILE": str(key_path)})

    @unittest.skipIf(_RUNNING_AS_ROOT, "file permission bits are not enforced for root")
    def test_world_readable_key_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            generate_master_key(key_path)
            key_path.chmod(0o644)
            with self.assertRaises(ConfigurationError):
                resolve_master_key(env={"AGENTIC_OS_MASTER_KEY_FILE": str(key_path)})

    def test_env_key_material_never_appears_in_error_text(self) -> None:
        secret_looking_value = "super-secret-value-should-not-leak"
        try:
            resolve_master_key(env={"AGENTIC_OS_MASTER_KEY": secret_looking_value})
            self.fail("expected ConfigurationError")
        except ConfigurationError as error:
            self.assertNotIn(secret_looking_value, str(error))

    def test_object_storage_key_file_uri_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            resolve_master_key(env={"AGENTIC_OS_MASTER_KEY_FILE": "s3://bucket/master.key"})


class GenerateMasterKeyTests(unittest.TestCase):
    def test_generate_creates_restricted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "nested" / "master.key"
            result_path = generate_master_key(key_path)
            self.assertTrue(result_path.is_file())
            mode = result_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            Fernet(result_path.read_bytes())  # does not raise

    def test_generate_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            generate_master_key(key_path)
            with self.assertRaises(ConfigurationError):
                generate_master_key(key_path)

    def test_generate_overwrites_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            first = generate_master_key(key_path)
            original = first.read_bytes()
            second = generate_master_key(key_path, force=True)
            self.assertNotEqual(original, second.read_bytes())


class DatabaseUrlValidationTests(unittest.TestCase):
    def test_default_is_accepted(self) -> None:
        result = validate_database_url(None)
        self.assertTrue(result.ok)

    def test_valid_postgresql_url_is_accepted(self) -> None:
        result = validate_database_url("postgresql+psycopg://user:pw@localhost:5432/db")
        self.assertTrue(result.ok)

    def test_malformed_url_is_rejected(self) -> None:
        result = validate_database_url("sqlite:///not-supported.db")
        self.assertFalse(result.ok)

    def test_url_without_host_is_rejected(self) -> None:
        result = validate_database_url("postgresql:///missing-host")
        self.assertFalse(result.ok)


class ArtifactRootValidationTests(unittest.TestCase):
    def test_writable_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = validate_artifact_root(str(Path(tmp) / "artifacts"))
        self.assertTrue(result.ok)

    @unittest.skipIf(_RUNNING_AS_ROOT, "permission checks are not enforced for root")
    def test_permission_problem_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            restricted = Path(tmp) / "restricted"
            restricted.mkdir(mode=0o500)
            try:
                result = validate_artifact_root(str(restricted / "artifacts"))
                self.assertFalse(result.ok)
            finally:
                restricted.chmod(0o700)

    def test_object_storage_uri_is_rejected(self) -> None:
        result = validate_artifact_root("s3://bucket/artifacts")
        self.assertFalse(result.ok)
        self.assertIn("not yet supported", result.detail)


class DeploymentModeValidationTests(unittest.TestCase):
    def test_default_mode_is_local(self) -> None:
        result = validate_deployment_mode(env={})
        self.assertTrue(result.ok)
        self.assertIn("local", result.detail)

    def test_team_mode_is_accepted(self) -> None:
        result = validate_deployment_mode(env={"AGENTIC_OS_DEPLOYMENT_MODE": "team"})
        self.assertTrue(result.ok)

    def test_unknown_mode_is_rejected(self) -> None:
        result = validate_deployment_mode(env={"AGENTIC_OS_DEPLOYMENT_MODE": "staging"})
        self.assertFalse(result.ok)


class PublicOriginValidationTests(unittest.TestCase):
    def test_not_required_for_local_mode(self) -> None:
        result = validate_public_origin(mode="local", env={})
        self.assertTrue(result.ok)

    def test_missing_origin_fails_closed_for_team_mode(self) -> None:
        result = validate_public_origin(mode="team", env={})
        self.assertFalse(result.ok)
        self.assertIn("AGENTIC_OS_PUBLIC_ORIGIN", result.detail)

    def test_plaintext_origin_is_rejected(self) -> None:
        result = validate_public_origin(
            mode="team", env={"AGENTIC_OS_PUBLIC_ORIGIN": "http://team.example.com"}
        )
        self.assertFalse(result.ok)

    def test_https_origin_with_hostname_is_accepted(self) -> None:
        result = validate_public_origin(
            mode="team", env={"AGENTIC_OS_PUBLIC_ORIGIN": "https://team.example.com"}
        )
        self.assertTrue(result.ok)


class BackupDestinationValidationTests(unittest.TestCase):
    def test_unset_is_ok(self) -> None:
        result = validate_backup_destination(env={})
        self.assertTrue(result.ok)

    def test_remote_uri_is_rejected(self) -> None:
        result = validate_backup_destination(env={"AGENTIC_OS_BACKUP_ROOT": "s3://bucket/backups"})
        self.assertFalse(result.ok)

    def test_writable_local_directory_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "backups")
            result = validate_backup_destination(env={"AGENTIC_OS_BACKUP_ROOT": target})
        self.assertTrue(result.ok)


class PostgresToolsValidationTests(unittest.TestCase):
    def test_matches_direct_shutil_lookup(self) -> None:
        import shutil

        expected_missing = [tool for tool in ("pg_dump", "pg_restore", "pg_isready") if shutil.which(tool) is None]
        result = validate_postgres_tools()
        self.assertEqual(result.ok, not expected_missing)


class MasterKeyDeploymentModeTests(unittest.TestCase):
    def test_team_mode_forces_required_even_when_caller_says_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key"),
                "AGENTIC_OS_DEPLOYMENT_MODE": "team",
            }
            result = validate_master_key(required=False, env=env)
        self.assertFalse(result.ok)

    def test_local_mode_honors_optional_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key"),
                "AGENTIC_OS_DEPLOYMENT_MODE": "local",
            }
            result = validate_master_key(required=False, env=env)
        self.assertTrue(result.ok)


class TeamModePreflightTests(unittest.TestCase):
    def test_team_mode_adds_backup_and_postgres_checks(self) -> None:
        results = run_preflight(deployment_mode="team")
        names = {result.name for result in results}
        self.assertIn("backup_destination", names)
        self.assertIn("postgres_tools", names)
        self.assertIn("public_origin", names)
        self.assertIn("deployment_mode", names)

    def test_local_mode_omits_team_only_checks(self) -> None:
        results = run_preflight(deployment_mode="local")
        names = {result.name for result in results}
        self.assertNotIn("backup_destination", names)
        self.assertNotIn("postgres_tools", names)


class PreflightEvidenceTests(unittest.TestCase):
    def test_evidence_is_json_safe_and_matches_results(self) -> None:
        results = [CheckResult("example", True, "detail text")]
        evidence = preflight_evidence(results)
        self.assertEqual(evidence, [{"name": "example", "ok": True, "detail": "detail text"}])

    def test_evidence_never_includes_raw_key_bytes(self) -> None:
        key = Fernet.generate_key().decode()
        results = [CheckResult("master_key", True, "master key resolved and validated")]
        evidence = preflight_evidence(results)
        self.assertNotIn(key, str(evidence))


class PreflightAggregationTests(unittest.TestCase):
    def test_missing_master_key_fails_the_full_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key"),
                "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
            }
            old_environ = dict(os.environ)
            os.environ.pop("AGENTIC_OS_MASTER_KEY", None)
            os.environ.update(env)
            try:
                results = run_preflight()
            finally:
                os.environ.clear()
                os.environ.update(old_environ)
        master_key_result = next(r for r in results if r.name == "master_key")
        self.assertFalse(master_key_result.ok)

    def test_format_report_never_includes_raw_key_bytes(self) -> None:
        key = Fernet.generate_key().decode()
        results = [CheckResult("master_key", True, "master key resolved and validated")]
        report = format_report(results)
        self.assertNotIn(key, report)


class ConfigCliTests(unittest.TestCase):
    def _run_cli(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        full_env = dict(os.environ, PYTHONPATH=str(BACKEND_ROOT / "src"), **env)
        return subprocess.run(
            [sys.executable, "-m", "agentic_os.cli", "config", *args],
            cwd=BACKEND_ROOT,
            env=full_env,
            capture_output=True,
            text=True,
        )

    def test_check_fails_closed_without_master_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = self._run_cli(
                "check",
                env={
                    "AGENTIC_OS_MASTER_KEY": "",
                    "AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key"),
                    "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                },
            )
        self.assertEqual(process.returncode, 1)
        self.assertIn("FAIL", process.stdout)
        self.assertIn("master_key", process.stdout)

    def test_check_passes_with_valid_generated_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            generate_result = self._run_cli(
                "generate-master-key", "--path", str(key_path), env={}
            )
            self.assertEqual(generate_result.returncode, 0)
            self.assertNotIn(key_path.read_bytes().decode(), generate_result.stdout)

            process = self._run_cli(
                "check",
                env={
                    "AGENTIC_OS_MASTER_KEY": "",
                    "AGENTIC_OS_MASTER_KEY_FILE": str(key_path),
                    "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                },
            )
        self.assertEqual(process.returncode, 0)
        self.assertIn("OK", process.stdout)

    def test_generate_master_key_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            first = self._run_cli("generate-master-key", "--path", str(key_path), env={})
            self.assertEqual(first.returncode, 0)
            second = self._run_cli("generate-master-key", "--path", str(key_path), env={})
        self.assertEqual(second.returncode, 2)
        self.assertIn("already exists", second.stderr)

    def test_check_json_returns_structured_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            self._run_cli("generate-master-key", "--path", str(key_path), env={})
            process = self._run_cli(
                "check",
                "--json",
                env={
                    "AGENTIC_OS_MASTER_KEY": "",
                    "AGENTIC_OS_MASTER_KEY_FILE": str(key_path),
                    "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                },
            )
        self.assertEqual(process.returncode, 0)
        payload = json.loads(process.stdout)
        names = {entry["name"] for entry in payload}
        self.assertIn("master_key", names)
        self.assertIn("deployment_mode", names)
        for entry in payload:
            self.assertEqual(set(entry), {"name", "ok", "detail"})

    def test_team_mode_check_fails_without_public_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "master.key"
            self._run_cli("generate-master-key", "--path", str(key_path), env={})
            process = self._run_cli(
                "check",
                env={
                    "AGENTIC_OS_MASTER_KEY": "",
                    "AGENTIC_OS_MASTER_KEY_FILE": str(key_path),
                    "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                    "AGENTIC_OS_DEPLOYMENT_MODE": "team",
                },
            )
        self.assertEqual(process.returncode, 1)
        self.assertIn("public_origin", process.stdout)


if __name__ == "__main__":
    unittest.main()
