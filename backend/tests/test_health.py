from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, text

from agentic_os.domain import create_database_engine, database_url
from agentic_os.health import (
    check_artifact_root,
    check_database,
    check_master_key,
    check_migrations,
    check_sandbox,
    deployment_health,
)

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def setUpModule() -> None:
    global TEST_DATABASE_URL
    TEST_DATABASE_URL = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    try:
        probe = create_database_engine(TEST_DATABASE_URL)
        with probe.connect():
            pass
        probe.dispose()
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(
            f"PostgreSQL is not reachable at {TEST_DATABASE_URL!r}; set "
            f"AGENTIC_OS_DATABASE_URL to run health checks: {error}"
        )
    os.environ["AGENTIC_OS_DATABASE_URL"] = TEST_DATABASE_URL


class DatabaseHealthTests(unittest.TestCase):
    def test_reachable_database_is_healthy(self) -> None:
        engine = create_database_engine(TEST_DATABASE_URL)
        try:
            result = check_database(engine)
        finally:
            engine.dispose()
        self.assertEqual(result.status, "healthy")

    def test_unreachable_database_is_reported_unavailable(self) -> None:
        engine = create_database_engine("postgresql+psycopg://user:pass@127.0.0.1:1/agentic_os")
        try:
            result = check_database(engine)
        finally:
            engine.dispose()
        self.assertEqual(result.status, "unavailable")
        self.assertIn("unreachable", result.detail)


class MigrationHealthTests(unittest.TestCase):
    def test_database_at_head_is_healthy(self) -> None:
        engine = create_database_engine(TEST_DATABASE_URL)
        try:
            result = check_migrations(engine)
        finally:
            engine.dispose()
        self.assertEqual(result.status, "healthy")
        self.assertIn("head", result.detail)

    def test_database_without_any_applied_migration_is_unavailable(self) -> None:
        # An empty schema (reached here through an isolated search_path,
        # rather than a whole extra database, to avoid the brief
        # just-created-database connection window being flaky) has no
        # alembic_version table, which is exactly the "never migrated" state.
        schema_name = f"health_scratch_{uuid.uuid4().hex[:12]}"
        setup_engine = create_database_engine(TEST_DATABASE_URL)
        with setup_engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            connection.commit()
        setup_engine.dispose()
        try:
            scratch_engine = create_engine(
                TEST_DATABASE_URL,
                future=True,
                connect_args={"options": f"-csearch_path={schema_name}"},
            )
            try:
                result = check_migrations(scratch_engine)
            finally:
                scratch_engine.dispose()
            self.assertEqual(result.status, "unavailable")
            self.assertIn("no migration has been applied", result.detail)
        finally:
            cleanup_engine = create_database_engine(TEST_DATABASE_URL)
            with cleanup_engine.connect() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
                connection.commit()
            cleanup_engine.dispose()


class ArtifactRootHealthTests(unittest.TestCase):
    def test_writable_root_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts")}, clear=False
            ):
                result = check_artifact_root()
        self.assertEqual(result.status, "healthy")

    @unittest.skipIf(_RUNNING_AS_ROOT, "permission checks are not enforced for root")
    def test_unwritable_root_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            restricted = Path(tmp) / "restricted"
            restricted.mkdir(mode=0o500)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"AGENTIC_OS_ARTIFACT_ROOT": str(restricted / "artifacts")},
                    clear=False,
                ):
                    result = check_artifact_root()
                self.assertEqual(result.status, "unavailable")
            finally:
                restricted.chmod(0o700)


class MasterKeyHealthTests(unittest.TestCase):
    def test_missing_key_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_MASTER_KEY_FILE": str(Path(tmp) / "master.key"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENTIC_OS_MASTER_KEY", None)
                result = check_master_key()
        self.assertEqual(result.status, "unavailable")

    def test_configured_key_is_healthy(self) -> None:
        from cryptography.fernet import Fernet

        with mock.patch.dict(
            os.environ, {"AGENTIC_OS_MASTER_KEY": Fernet.generate_key().decode()}, clear=False
        ):
            result = check_master_key()
        self.assertEqual(result.status, "healthy")


class SandboxHealthTests(unittest.TestCase):
    def test_reports_unavailable_when_no_runtime_is_usable(self) -> None:
        with mock.patch("agentic_os.health.runtime_available", return_value=(False, "not installed")):
            result = check_sandbox()
        self.assertEqual(result.status, "unavailable")

    def test_reports_healthy_when_any_runtime_is_usable(self) -> None:
        with mock.patch(
            "agentic_os.health.runtime_available",
            side_effect=[(True, ""), (False, "not installed")],
        ):
            result = check_sandbox()
        self.assertEqual(result.status, "healthy")


class DeploymentHealthAggregationTests(unittest.TestCase):
    def test_all_healthy_dependencies_report_overall_healthy(self) -> None:
        from cryptography.fernet import Fernet

        engine = create_database_engine(TEST_DATABASE_URL)
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                "AGENTIC_OS_MASTER_KEY": Fernet.generate_key().decode(),
            }
            try:
                with mock.patch.dict(os.environ, env, clear=False):
                    report = deployment_health(engine)
            finally:
                engine.dispose()
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(set(report["checks"]), {"database", "migrations", "artifact_root", "master_key"})

    def test_one_unhealthy_dependency_fails_the_whole_report_closed(self) -> None:
        engine = create_database_engine("postgresql+psycopg://user:pass@127.0.0.1:1/agentic_os")
        try:
            report = deployment_health(engine)
        finally:
            engine.dispose()
        self.assertEqual(report["status"], "unavailable")

    def test_include_sandbox_adds_the_sandbox_check(self) -> None:
        from cryptography.fernet import Fernet

        engine = create_database_engine(TEST_DATABASE_URL)
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTIC_OS_ARTIFACT_ROOT": str(Path(tmp) / "artifacts"),
                "AGENTIC_OS_MASTER_KEY": Fernet.generate_key().decode(),
            }
            try:
                with mock.patch.dict(os.environ, env, clear=False):
                    report = deployment_health(engine, include_sandbox=True)
            finally:
                engine.dispose()
        self.assertIn("sandbox", report["checks"])


if __name__ == "__main__":
    unittest.main()
