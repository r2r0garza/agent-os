from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sqlalchemy import create_engine, select

from agentic_os.domain import create_database_engine, database_url, session_factory
from agentic_os.domain.models import (
    AuditEvent,
    ObservabilityRecord,
    ProjectMember,
    Run,
    Task,
    Team,
    TeamMembership,
    TelemetryExportAttempt,
    TelemetryExportSetting,
    User,
)

BACKEND_ROOT = Path(__file__).parents[1]


def setUpModule() -> None:
    global client, SessionLocal
    from agentic_os.api.deps import _engine

    if _engine.cache_info().currsize:
        _engine().dispose()
        _engine.cache_clear()
    db_url = os.environ.get("AGENTIC_OS_DATABASE_URL", database_url())
    probe = create_database_engine(db_url)
    try:
        with probe.connect():
            pass
    except Exception as error:  # pragma: no cover - environment guard
        raise unittest.SkipTest(f"PostgreSQL is not reachable: {error}")
    finally:
        probe.dispose()
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=dict(os.environ, AGENTIC_OS_DATABASE_URL=db_url),
        check=True,
        capture_output=True,
        text=True,
    )
    os.environ["AGENTIC_OS_DATABASE_URL"] = db_url
    from fastapi.testclient import TestClient
    from agentic_os.api.app import create_app

    client = TestClient(create_app())
    SessionLocal = session_factory(create_database_engine(db_url))


class ObservabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = client.post(
            "/api/v1/projects", json={"name": f"Observed {uuid.uuid4()}"}
        ).json()
        self.goal = client.post(
            f"/api/v1/projects/{self.project['id']}/goals",
            json={"title": "Inspect this execution"},
        ).json()
        graph = client.post(
            f"/api/v1/goals/{self.goal['id']}/task-graph",
            json={"tasks": [{"client_id": "task", "title": "Observed task"}]},
        ).json()
        self.task = graph["tasks"][0]
        agent = client.post(
            "/api/v1/agents", json={"name": f"Observed agent {uuid.uuid4()}"}
        ).json()
        self.agent_version = client.post(
            f"/api/v1/agents/{agent['id']}/versions",
            json={"instructions": "Observe", "capability_manifest": {}},
        ).json()
        with SessionLocal.begin() as session:
            team = session.execute(select(Team).where(Team.name == "Default Team")).scalar_one()
            run = Run(
                task_id=uuid.UUID(self.task["id"]),
                attempt_number=1,
                idempotency_key=f"observability-{uuid.uuid4()}",
                lease_token=1,
                agent_version_id=uuid.UUID(self.agent_version["id"]),
                status="running",
            )
            session.add(run)
            session.flush()
            record = ObservabilityRecord(
                correlation_id=uuid.uuid4(),
                request_id=uuid.uuid4(),
                trace_id="a" * 32,
                span_id="b" * 16,
                event_kind="model_call",
                operation_name="model.invoke",
                status="failed",
                team_id=team.id,
                project_id=uuid.UUID(self.project["id"]),
                goal_id=uuid.UUID(self.goal["id"]),
                task_id=uuid.UUID(self.task["id"]),
                run_id=run.id,
                attributes={"model": "test", "api_key": "must-not-return"},
                capture_policy_evidence={"capture_prompts": False, "secret": "hidden"},
                redaction_evidence={"policy": "sensitive-key-redaction-v1"},
            )
            session.add(record)
            session.flush()
            session.add(
                TelemetryExportAttempt(
                    observability_record_id=record.id,
                    destination="opentelemetry",
                    attempt_number=1,
                    status="failed",
                    failure_code="sink_unavailable",
                    failure_message="failed with bearer must-not-return",
                    delivery_evidence={"api_token": "must-not-return", "retryable": True},
                )
            )
            default_user = session.execute(
                select(User).where(User.email == "operator@local")
            ).scalar_one()
            session.add(
                TelemetryExportSetting(
                    team_id=team.id,
                    created_by=default_user.id,
                    exporter_type="opentelemetry",
                    enabled=True,
                    endpoint_reference="secret://telemetry/endpoint",
                    capture_prompts=False,
                    capture_outputs=False,
                    redaction_policy_evidence={"secret": "must-not-return", "policy": "strict"},
                )
            )
            session.flush()
            self.run_id = run.id
            self.record_id = record.id

    def _regular_user(self, *, project_access: bool) -> User:
        with SessionLocal.begin() as session:
            team = session.execute(select(Team).where(Team.name == "Default Team")).scalar_one()
            user = User(
                email=f"regular-{uuid.uuid4()}@example.test",
                display_name="Regular",
                role="regular_user",
            )
            session.add(user)
            session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=user.id))
            if project_access:
                session.add(
                    ProjectMember(project_id=uuid.UUID(self.project["id"]), user_id=user.id)
                )
            session.flush()
            session.expunge(user)
            return user

    def test_project_goal_task_and_run_timelines_redact_sensitive_evidence(self) -> None:
        member = self._regular_user(project_access=True)
        headers = {"X-Agentic-User-ID": str(member.id)}
        paths = [
            f"/api/v1/projects/{self.project['id']}/observability-records",
            f"/api/v1/goals/{self.goal['id']}/observability-timeline",
            f"/api/v1/tasks/{self.task['id']}/observability-timeline",
            f"/api/v1/runs/{self.run_id}/observability-timeline",
        ]
        for path in paths:
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            item = next(
                candidate
                for candidate in response.json()
                if candidate["id"] == str(self.record_id)
            )
            self.assertEqual(item["trace_id"], "a" * 32)
            self.assertEqual(item["span_id"], "b" * 16)
            self.assertEqual(item["attributes"]["api_key"], "[REDACTED]")
            self.assertEqual(item["capture_policy_evidence"]["secret"], "[REDACTED]")
            self.assertEqual(item["telemetry_attempts"][0]["status"], "failed")
            self.assertEqual(item["telemetry_attempts"][0]["failure_message"], "[REDACTED]")
            self.assertEqual(
                item["telemetry_attempts"][0]["delivery_evidence"]["api_token"],
                "[REDACTED]",
            )

        detail = client.get(
            f"/api/v1/observability-records/{self.record_id}", headers=headers
        )
        self.assertEqual(detail.status_code, 200, detail.text)

    def test_project_ownership_and_admin_role_boundaries(self) -> None:
        outsider = self._regular_user(project_access=False)
        outsider_headers = {"X-Agentic-User-ID": str(outsider.id)}
        response = client.get(
            f"/api/v1/projects/{self.project['id']}/observability-records",
            headers=outsider_headers,
        )
        self.assertEqual(response.status_code, 404)
        detail = client.get(
            f"/api/v1/observability-records/{self.record_id}", headers=outsider_headers
        )
        self.assertEqual(detail.status_code, 404)

        member = self._regular_user(project_access=True)
        member_headers = {"X-Agentic-User-ID": str(member.id)}
        health = client.get("/api/v1/admin/observability/health", headers=member_headers)
        self.assertEqual(health.status_code, 403)
        attempts = client.get(
            "/api/v1/admin/telemetry-export-attempts?status=failed", headers=member_headers
        )
        self.assertEqual(attempts.status_code, 403)

    def test_admin_health_and_failed_delivery_views_are_safe(self) -> None:
        with SessionLocal.begin() as session:
            session.add(
                AuditEvent(
                    event_type="operations.backup_created",
                    payload={"backup": "/tmp/backup.tar.gz", "api_key": "do-not-expose"},
                )
            )
        with mock.patch(
            "agentic_os.api.routers.observability.runtime_available",
            side_effect=[(True, ""), (False, "podman unavailable")],
        ):
            response = client.get("/api/v1/admin/observability/health")
        self.assertEqual(response.status_code, 200, response.text)
        health = response.json()
        self.assertEqual(health["database"]["status"], "healthy")
        self.assertGreaterEqual(health["database"]["latency_ms"], 0)
        self.assertIn("migrations", health["deployment"]["checks"])
        self.assertIn("master_key", health["deployment"]["checks"])
        self.assertEqual(
            health["maintenance"]["events"][0]["event_type"],
            "operations.backup_created",
        )
        self.assertEqual(
            health["maintenance"]["events"][0]["evidence"]["api_key"],
            "[REDACTED]",
        )
        self.assertIn("operations backup", health["maintenance"]["commands"]["backup"])
        self.assertEqual(health["sandbox"]["status"], "degraded")
        self.assertEqual(health["sandbox"]["runtimes"]["docker"]["status"], "available")
        self.assertEqual(
            health["sandbox"]["runtimes"]["podman"]["status"], "unavailable"
        )
        self.assertEqual(health["telemetry"]["status"], "degraded")
        exporter = health["telemetry"]["exporters"][-1]
        self.assertTrue(exporter["configured"])
        self.assertNotIn("endpoint_reference", exporter)
        self.assertEqual(exporter["redaction_policy_evidence"]["secret"], "[REDACTED]")

        response = client.get("/api/v1/admin/telemetry-export-attempts?status=failed")
        self.assertEqual(response.status_code, 200, response.text)
        failure = response.json()[0]
        self.assertEqual(failure["failure_code"], "sink_unavailable")
        self.assertEqual(failure["failure_message"], "[REDACTED]")
        self.assertEqual(failure["delivery_evidence"]["api_token"], "[REDACTED]")

    def test_admin_health_reports_stale_workers_retries_failures_and_delayed_delivery(self) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as session:
            task = session.get(Task, uuid.UUID(self.task["id"]))
            task.status = "running"
            task.lease_owner = "worker-stale-secretless-id"
            task.lease_expires_at = now - timedelta(minutes=5)
            first_run = session.get(Run, self.run_id)
            first_run.status = "failed"
            session.add(
                Run(
                    task_id=task.id,
                    attempt_number=2,
                    idempotency_key=f"health-retry-{uuid.uuid4()}",
                    lease_token=task.lease_token,
                    agent_version_id=uuid.UUID(self.agent_version["id"]),
                    status="failed",
                )
            )
            attempt = session.execute(
                select(TelemetryExportAttempt).where(
                    TelemetryExportAttempt.observability_record_id == self.record_id
                )
            ).scalar_one()
            attempt.status = "delayed"
            attempt.retry_after = now + timedelta(minutes=1)
            for record in session.execute(select(ObservabilityRecord)).scalars():
                record.occurred_at = now - timedelta(minutes=5)

        with mock.patch(
            "agentic_os.api.routers.observability.runtime_available",
            return_value=(False, "runtime unavailable"),
        ):
            response = client.get("/api/v1/admin/observability/health")

        self.assertEqual(response.status_code, 200, response.text)
        health = response.json()
        self.assertEqual(health["workers"]["status"], "stale")
        self.assertIn("worker-stale-secretless-id", health["workers"]["stale_worker_ids"])
        self.assertIn(self.task["id"], health["workers"]["stale_task_ids"])
        self.assertGreaterEqual(health["workers"]["retry_count"], 1)
        self.assertGreaterEqual(health["workers"]["failure_count"], 2)
        self.assertEqual(health["sandbox"]["status"], "unavailable")
        self.assertEqual(health["event_stream"]["status"], "delayed")
        self.assertIsNotNone(health["event_stream"]["latest_correlation_id"])
        self.assertEqual(health["telemetry"]["status"], "degraded")
